"""End-to-end decomposition pipeline orchestration.

Ties the services together: build the prompt, call the LLM (streamed or not),
parse + validate, link entities to vocabulary IRIs, and serialize to Turtle. Exposes the
two entry points the routers use (``run_pipeline``, ``stream_pipeline_events``) and
the startup warmup (``warmup_assets``).

On the measured path it runs the lab's best pipeline: the 25 most similar corpus variables as
examples (``example_retrieval``) and the matrix finder's statement (``matrix_finder``), asked side
by side; then the fill prompt three times side by side, keeping the most typical answer
(``answer_vote``). The lab measured 0.694 Close F1 on PSNC for it, against 0.590 for the fixed
25 examples.

This is the fan-out hub of the service layer: it imports llm, validation, the three services
above, enrichment, rdf_ttl, prompts, clients, and core. Nothing imports it except the
routers and the app factory, so the dependency graph stays acyclic.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, Generator, Iterable, Iterator, List, Optional, Sequence, Tuple

import httpx
from jsonschema import Draft202012Validator
from openai import APIStatusError, OpenAIError

from .clients.http import get_http_session
from .clients.openai_client import get_openai_client
from .core.config import OPENROUTER_MODEL_PROVIDER, PSNC_MODEL_PROVIDER, settings
from .core.provider_keys import without_keys
from .core.state import app_state
from .services import answer_vote, example_retrieval, matrix_finder
from .services.call_log import CallLog, PendingCall, mark_unusable, start_call
from .services.enrichment import enrich_with_uris
from .services.entity_linking import Linker
from .services.label_comment import generate_label_and_comment
from .services.llm import (
    build_chat_completion_request_kwargs,
    call_error,
    call_llm_loose,
    FillAnswer,
    coerce_prediction,
    call_model,
    call_psnc_model,
    extract_stream_text_deltas,
    parse_llm_json,
    resolve_model_name,
    resolve_model_provider,
    send_psnc_fill,
    stream_event,
    stream_psnc_model,
)
from .services.measured_prompt import (
    load_demonstrations,
    load_lexical_schema_text,
    load_measured_template,
    render_measured_prompt,
)
from .services.prompts import build_prompt, list_prompt_versions, load_examples
from .services.rdf_ttl import json_to_ttl_repo_style
from .services.validation import (
    get_constraint_semantic_validation_errors,
    get_schema_validation_errors,
    load_schema,
    patch_schema_for_pipeline,
)


def warmup_assets() -> None:
    """Prime the lazily-loaded clients and warmup caches at application startup.

    Builds the OpenRouter client (when enabled), the schema validator, the prompt
    version and five-shot examples, and the shared HTTP session — storing the
    caches in ``app.core.state.app_state``.

    Raises:
        RuntimeError: If no prompt files are found, or a measured artifact is
            missing while the measured configuration is enabled, or the example pool or the
            matrix finder's data is missing, is no JSON or breaks one of its rules.

    Side effects:
        May construct the OpenRouter client; reads schema/prompt/example files.
    """
    # OpenRouter is only initialized when it is enabled for this deployment.
    if OPENROUTER_MODEL_PROVIDER in settings.enabled_model_providers and settings.openrouter_api_key:
        try:
            get_openai_client()
        except RuntimeError as e:
            # A key that cannot be sent stops the runs that need it, not the service and the other provider.
            print(f"OpenRouter cannot be used: {without_keys(e)}")

    # Cache schema validator (shared via app.core.state so services can read it
    # without importing this module).
    app_state.schema_cache = patch_schema_for_pipeline(load_schema(settings.schema_path))
    app_state.validator_cache = Draft202012Validator(app_state.schema_cache)

    # Cache prompt version + examples
    versions = list_prompt_versions(settings.prompt_dir)
    if not versions:
        raise RuntimeError(f"No prompt files found in: {settings.prompt_dir}")
    app_state.prompt_version_cache = versions[0]
    app_state.examples_5_cache = load_examples(settings.five_shot_dir, 5)

    # Cache the measured artifacts. A failure here is fatal by design: the app must
    # not start serving a configuration it cannot render byte-exactly.
    if settings.use_measured_configuration:
        template, schema_text, demonstrations = (
            load_measured_template(),
            load_lexical_schema_text(),
            load_demonstrations(),
        )
        app_state.measured_assets_cache = (template, schema_text, demonstrations)
        # The best pipeline's examples and matrix finder: data it cannot trust stops the start too.
        pool = example_retrieval.warm_up(demonstrations)
        matrix_finder.warm_up(pool.ids)
    else:
        app_state.measured_assets_cache = None
        app_state.example_pool_cache = None
        app_state.finder_cache = None

    # Prime HTTP session
    get_http_session()


def _finalize_pipeline_output(
    raw_llm_output: str,
    pred: Dict[str, Any],
    *,
    linker: Linker,
) -> Dict[str, Any]:
    """Validate, enrich, and serialize a prediction into the response payload.

    Args:
        raw_llm_output: The raw model text to echo back.
        pred: The parsed prediction.
        linker: The model that made the decomposition, which also answers the linking
            question (D-038).

    Returns:
        The ``DecomposeResponse``-shaped dict.

    Raises:
        RuntimeError: If ``pred`` is empty.

    Side effects:
        May call Wikidata, QUDT, EBI OLS and the linker's provider (entity linking).
    """
    if not pred:
        raise RuntimeError("Could not extract valid JSON from the model output.")

    validation_errors = get_schema_validation_errors(pred, label_for_logs=pred.get("label"))
    validation_errors.extend(get_constraint_semantic_validation_errors(pred))
    schema_valid = len(validation_errors) == 0

    if settings.enable_wikidata_linking:
        try:
            enriched = enrich_with_uris(pred, linker=linker)
        except Exception as e:
            print(f"Entity linking failed: {without_keys(e)}")
            enriched = pred
    else:
        enriched = pred

    ttl = json_to_ttl_repo_style(enriched)

    return {
        "raw_llm_output": raw_llm_output,
        "parsed_json": pred,
        "schema_valid": schema_valid,
        "validation_errors": validation_errors,
        "enriched_json": enriched,
        "ttl": ttl,
    }


def use_measured_path(provider: str) -> bool:
    """Report whether the measured configuration applies to this provider.

    The measured study is PSNC-only, so rendering the measured prompt into an
    OpenRouter request would produce an unmeasured configuration wearing a
    measured prompt. Both conditions are required.

    Args:
        provider: The already-resolved provider key.

    Returns:
        True when the measured configuration is enabled and the provider is PSNC.
    """
    return settings.use_measured_configuration and provider == PSNC_MODEL_PROVIDER


def merge_measured_prediction(
    six_field: Dict[str, Any],
    *,
    label: str,
    comment: str,
    definition: str,
) -> Dict[str, Any]:
    """Merge a six-field measured decomposition into the eight-field prediction.

    The definition written into the result is always the caller's, never the
    model's — the measured prompt forbids regenerating it, and any value the model
    emits anyway is discarded.

    Args:
        six_field: The parsed model output.
        label: The label from ``label-comment``.
        comment: The comment from ``label-comment``.
        definition: The stripped caller definition.

    Returns:
        The eight-field prediction the rest of the pipeline expects.
    """
    merged = coerce_prediction(six_field)
    merged["definition"] = definition
    merged["label"] = label
    merged["comment"] = comment
    return merged


def _labelled_prediction(
    pred: Dict[str, Any],
    *,
    provider: str,
    model: str,
    definition: str,
    calls: Optional[CallLog] = None,
) -> Dict[str, Any]:
    """Add label and comment to a measured prediction, leaving others untouched.

    Skipped entirely when the decomposition produced nothing: a failed
    decomposition must not spend a second call labelling a result nobody will see.

    Args:
        pred: The parsed prediction, possibly empty.
        provider: The resolved provider.
        model: The resolved model name.
        definition: The stripped caller definition.
        calls: The run's call log; the label call is recorded there.

    Returns:
        The eight-field prediction on the measured path, otherwise ``pred``
        unchanged.

    Side effects:
        One additional LLM call on the measured path.
    """
    if not pred or not use_measured_path(provider):
        return pred

    label, comment = generate_label_and_comment(
        definition,
        model_provider=provider,
        model=model,
        temperature=settings.temperature,
        calls=calls,
    )
    return merge_measured_prediction(pred, label=label, comment=comment, definition=definition)


def _examples_and_matrix(
    definition: str, *, model: str, leave_out: Iterable[str], calls: Optional[CallLog]
) -> Tuple[example_retrieval.Examples, matrix_finder.MatrixFinding]:
    """Ask for the examples and for the matrix side by side; both calls go into the run's log, in this order.

    Args:
        definition: The stripped definition.
        model: The run's PSNC model, which the matrix finder asks too.
        leave_out: Lab ids neither may show.
        calls: The run's call log; what was chosen is kept there.

    Returns:
        The examples and the matrix finding; neither request's failure fails the run. Each request names its own
        failures; an error neither foresaw falls back to the fixed 25 or states no matrix as well.
    """
    logs = (CallLog(), CallLog()) if calls is not None else (None, None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        examples_future = pool.submit(example_retrieval.retrieve_examples, definition, leave_out=leave_out,
                                      calls=logs[0])
        finding_future = pool.submit(matrix_finder.find_matrix, definition, model=model, leave_out=leave_out,
                                     calls=logs[1])
        try:
            examples = examples_future.result()
        except Exception as e:  # the fixed 25 can still be shown; a pool that cannot be read raises again here
            print(f"Example retrieval failed: {without_keys(e)}")
            examples = example_retrieval.fallback_examples(leave_out=leave_out)
        try:
            finding = finding_future.result()
        except Exception as e:  # nothing is stated
            print(f"Matrix finder failed: {without_keys(e)}")
            finding = matrix_finder.NO_FINDING
    if calls is not None:
        for log in logs:
            if log is not None:
                calls.adopt(log)
        calls.update_choices(examples=examples.as_record(), matrix=finding.matrix)
    return examples, finding


def _prepare_pipeline_inputs(
    definition: str,
    model_name: Optional[str] = None,
    model_provider: Optional[str] = None,
    *,
    leave_out: Iterable[str] = frozenset(),
    calls: Optional[CallLog] = None,
) -> Tuple[str, str, str]:
    """Resolve the prompt, provider, and model for a decomposition request.

    On the measured path the prompt shows the 25 corpus variables most similar to the definition
    (today's fixed 25 when PSNC's embedding gives no vector to use) and, after the definition, the
    matrix finder's statement (nothing when the finder gives no usable answer).

    Args:
        definition: The variable definition (must be non-empty after stripping).
        model_name: Optional requested model name.
        model_provider: Optional requested provider.
        leave_out: Lab ids of corpus variables neither the examples nor the finder may show, for the
            quality gate; no route sets it.
        calls: The run's call log; the embedding and finder requests are recorded there.

    Returns:
        A ``(prompt, provider, model_name)`` tuple.

    Raises:
        ValueError: If the definition is empty.
        RuntimeError: If no prompt files are available, or the example pool or the finder's data
            is missing or broken.

    Side effects:
        On the measured path, an embedding request and the matrix finder's request to PSNC.
    """
    definition = definition.strip()
    if not definition:
        raise ValueError("Definition must not be empty.")

    # The provider is resolved first: which prompt to build depends on it.
    selected_model_provider = resolve_model_provider(model_provider)
    selected_model_name = resolve_model_name(model_name, model_provider=selected_model_provider)

    if use_measured_path(selected_model_provider):
        cached = app_state.measured_assets_cache
        template, schema_text = (cached[0], cached[1]) if cached is not None else (None, None)
        examples, finding = _examples_and_matrix(
            definition, model=selected_model_name, leave_out=leave_out, calls=calls
        )
        prompt = render_measured_prompt(
            definition + finding.statement,
            template=template,
            schema_text=schema_text,
            demonstrations=list(examples.demonstrations),
        )
        return prompt, selected_model_provider, selected_model_name

    prompt_version = app_state.prompt_version_cache
    if not prompt_version:
        prompt_versions = list_prompt_versions(settings.prompt_dir)
        if not prompt_versions:
            raise RuntimeError(f"No prompt files found in: {settings.prompt_dir}")
        prompt_version = prompt_versions[0]

    examples_5 = (
        app_state.examples_5_cache if app_state.examples_5_cache is not None else load_examples(settings.five_shot_dir, 5)
    )
    prompt = build_prompt(definition, prompt_version=prompt_version, examples=examples_5)

    return prompt, selected_model_provider, selected_model_name


PROMPT_PLACEHOLDER = "{definition}"
"""Stands in for the definition when a prompt setup is fingerprinted."""


MEASURED_SETUP = "measured-best-pipeline"
"""The measured path's setup: the lab's best pipeline (examples by similarity, the matrix finder, the most typical
of three answers), with today's 25 examples as the fallback."""

VOTE_NOTE = "\n\n[Kept answer {kept} of {asked}: the one that agrees most with the others.]\n\n"
"""Shown on the stream before the kept answer when it is not the one that streamed."""

NOT_KEPT = "not kept: the most typical of the answers was kept instead."

STREAM_CLOSED = "The stream closed before the answer was complete."

CLOSED_BEFORE_VOTE = "The stream closed before the answers were compared."


def prompt_setup(model_provider: Optional[str] = None) -> Tuple[str, str]:
    """Name and fingerprint the prompt setup a run with this provider gets, without its definition or any request.

    On the measured path the setup is ``measured-best-pipeline``, fingerprinted by the SHA-256 of a canonical JSON
    object of everything that decides its prompts and answers: the fallback prompt rendered for
    :data:`PROMPT_PLACEHOLDER`, the example pool, the finder's data and request, the embedding model, the number and
    order of the examples, the number of answers and the rule that keeps one. Otherwise it is
    ``few-shot:<prompt version>``, fingerprinted by the SHA-256 of the prompt rendered for :data:`PROMPT_PLACEHOLDER`.
    Errors are those of :func:`_prepare_pipeline_inputs`.

    Args:
        model_provider: The requested provider; ``None`` uses the default.

    Returns:
        ``(setup, sha256)``.
    """
    if use_measured_path(resolve_model_provider(model_provider)):
        cached = app_state.measured_assets_cache
        template, schema_text = (cached[0], cached[1]) if cached is not None else (None, None)
        fallback = example_retrieval.fallback_examples()
        prompt = render_measured_prompt(
            PROMPT_PLACEHOLDER, template=template, schema_text=schema_text, demonstrations=list(fallback.demonstrations)
        )
        finder = matrix_finder.current_finder()
        canonical = {
            "fallback_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "pool_sha256": example_retrieval.current_pool().sha256,
            "finder_sha256": finder.sha256,
            "finder_request": finder.request,
            "embedding_model": example_retrieval.EMBEDDING_MODEL,
            "examples": example_retrieval.EXAMPLES,
            "order": example_retrieval.ORDER,
            "answers": answer_vote.ANSWERS,
            "vote": answer_vote.VOTE_RULE,
        }
        text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return MEASURED_SETUP, hashlib.sha256(text.encode("utf-8")).hexdigest()
    prompt, _, _ = _prepare_pipeline_inputs(PROMPT_PLACEHOLDER, model_provider=model_provider)
    versions = list_prompt_versions(settings.prompt_dir)
    setup = f"few-shot:{app_state.prompt_version_cache or (versions[0] if versions else '')}"
    return setup, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _fill_answers(prompt: str, model: str, calls: Optional[CallLog], count: int) -> List[FillAnswer]:
    """Send the fill prompt ``count`` times side by side; the calls stay open for :func:`_keep_most_typical`."""
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(send_psnc_fill, model, prompt, settings.temperature, calls=calls) for _ in range(count)]
        return [future.result() for future in futures]


def _keep_most_typical(
    answers: List[FillAnswer], definition: str, calls: Optional[CallLog]
) -> Optional[Tuple[int, str, Dict[str, Any], Optional[int]]]:
    """Keep the most typical of the answers and finish their calls in order.

    The answer kept is the one with the highest mean agreement with the others by the scorer's Exact F1 (ties to the
    first the lab's check accepts, else the first); when it cannot be read, the first answer that can. Its call is ``ok``; another answer that can be read is
    ``unusable``, not kept; one that cannot is ``unusable`` with today's reason; one that did not come is ``error``.

    Args:
        answers: The answers, in the order they were asked for.
        definition: The stripped definition, written into the prediction.
        calls: The run's call log.

    Returns:
        ``(position, text, prediction, attempt of its call)``, or ``None`` when no answer can be read.
    """
    predictions: List[Optional[Dict[str, Any]]] = []
    reasons: List[Tuple[str, str]] = []
    for answer in answers:
        prediction: Optional[Dict[str, Any]] = None
        if answer.text is None:
            reason = ("error", call_error(answer.error) if answer.error is not None else "No answer came.")
        elif answer.text.strip().startswith(("<!DOCTYPE html", "<html")):
            reason = ("unusable", "The model returned HTML.")
        elif not answer.text.strip():
            reason = ("unusable", "The model returned no text.")
        else:
            try:
                prediction = parse_llm_json(answer.text, definition)
                reason = ("ok", "")
            except Exception as e:
                reason = ("unusable", f"Not valid JSON: {without_keys(e)}")
        predictions.append(prediction)
        reasons.append(reason)
    readings = [answer_vote.lab_answer(answer.text) if predictions[i] is not None else None
                for i, answer in enumerate(answers)]
    kept: Optional[int] = answer_vote.pick(readings)
    if kept is not None and predictions[kept] is None:
        kept = next((i for i, prediction in enumerate(predictions) if prediction is not None), None)
    kept_attempt: Optional[int] = None
    for i, answer in enumerate(answers):
        if answer.pending is None:
            continue
        if i == kept:
            answer.pending.finish("ok", usage=answer.usage)
            kept_attempt = calls.calls()[-1].attempt if calls is not None else None
        elif predictions[i] is not None:
            answer.pending.finish("unusable", error=NOT_KEPT, usage=answer.usage)
        else:
            status, why = reasons[i]
            answer.pending.finish(status, error=why, usage=answer.usage)
    if calls is not None:
        calls.update_choices(answers={"asked": len(answers), "kept": None if kept is None else kept + 1})
    if kept is None:
        return None
    text = answers[kept].text or ""
    prediction = predictions[kept]
    assert prediction is not None
    return kept, text, prediction, kept_attempt


class _BackgroundAnswers:
    """The calls of the stream's background answers, so that a client who leaves leaves none unrecorded.

    Each background answer starts its call through :meth:`start`; :meth:`close` finishes every call started and keeps
    the others from being sent.
    """

    def __init__(self, calls: Optional[CallLog], model: str) -> None:
        self._calls = calls
        self._model = model
        self._lock = threading.Lock()
        self._closed = False
        self._started: List[PendingCall] = []

    def start(self) -> Optional[PendingCall]:
        """Start a background answer's call, or give ``None`` once the stream has closed: then nothing is sent."""
        with self._lock:
            if self._closed:
                return None
            pending = start_call(self._calls, "decomposition", PSNC_MODEL_PROVIDER, self._model)
            self._started.append(pending)
            return pending

    def close(self, futures: Sequence["Future[FillAnswer]"]) -> None:
        """The client left before the answers were compared: finish every background call.

        An answer that came is ``unusable``, one that did not come is ``error``; a request still on its way keeps
        running, but its call is finished here, and an answer not yet asked for is not sent.

        Args:
            futures: The background answers.
        """
        with self._lock:
            self._closed = True
            started = list(self._started)
        for future in futures:
            if not future.done() or future.cancelled() or future.exception() is not None:
                continue
            answer = future.result()
            if answer.pending is None:
                continue
            if answer.text is not None:
                answer.pending.finish("unusable", error=CLOSED_BEFORE_VOTE, usage=answer.usage)
            else:
                reason = call_error(answer.error) if answer.error is not None else "No answer came."
                answer.pending.finish("error", error=reason, usage=answer.usage)
        for pending in started:
            pending.finish("error", error=STREAM_CLOSED)


def _openrouter_deltas(chunks: Iterable[Any], usage: Dict[str, Any]) -> Iterator[Tuple[str, str]]:
    """Turn OpenRouter stream chunks into deltas, keeping the token counts of the last chunk.

    Args:
        chunks: The chunks of a streamed chat completion.
        usage: Filled with ``prompt_tokens`` and ``completion_tokens`` from any chunk that
            carries ``usage``.

    Yields:
        ``(reasoning_delta, content_delta)`` per chunk.
    """
    for chunk in chunks:
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage["prompt_tokens"] = getattr(chunk_usage, "prompt_tokens", None)
            usage["completion_tokens"] = getattr(chunk_usage, "completion_tokens", None)
        yield extract_stream_text_deltas(chunk)


def _stream_voted(
    prompt: str,
    *,
    provider: str,
    model: str,
    definition: str,
    disable_thinking: bool,
    calls: Optional[CallLog],
    shown: List[str],
) -> Generator[str, None, bool]:
    """The stream's first try on the measured path: one answer streamed, the others asked in the background.

    The streamed answer's deltas go out as they come; once every answer is in, the most typical is kept
    (:func:`_keep_most_typical`). When it is not the streamed one, a note and the kept answer go out as one
    ``raw_delta``; then the label, the linking and ``final``.

    Args:
        prompt: The fill prompt.
        provider: The resolved provider (PSNC).
        model: The resolved model.
        definition: The stripped definition.
        disable_thinking: Forwarded to the streamed request.
        calls: The run's call log.
        shown: The displayed text so far, extended in place.

    Returns:
        Through ``StopIteration.value``: True when ``final`` went out, False when no answer could be read and the
        caller's tries go on.

    Yields:
        NDJSON event lines.

    Raises:
        GeneratorExit: When the client closes the stream mid-answer; the streamed call is recorded as an error first.
    """
    count = answer_vote.ANSWERS
    background = _BackgroundAnswers(calls, model)
    pool = ThreadPoolExecutor(max_workers=count - 1)
    try:
        others = [pool.submit(send_psnc_fill, model, prompt, settings.temperature, calls=calls, start=background.start)
                  for _ in range(count - 1)]
        display: List[str] = []
        content: List[str] = []
        usage: Dict[str, Any] = {}
        pending: Optional[PendingCall] = None
        error: Optional[BaseException] = None
        saw_reasoning = started_content = False
        try:
            stream = stream_psnc_model(model, prompt, settings.temperature, disable_thinking=disable_thinking,
                                       measured=True, usage=usage)
            pending = start_call(calls, "decomposition", provider, model)
            for reasoning_delta, content_delta in stream:
                if reasoning_delta:
                    saw_reasoning = True
                    display.append(reasoning_delta)
                    yield stream_event("raw_delta", delta=reasoning_delta)
                if content_delta:
                    if saw_reasoning and not started_content:
                        display.append("\n\n")
                        yield stream_event("raw_delta", delta="\n\n")
                    started_content = True
                    display.append(content_delta)
                    content.append(content_delta)
                    yield stream_event("raw_delta", delta=content_delta)
        except GeneratorExit:
            if pending is not None:
                pending.finish("error", error=STREAM_CLOSED, usage=usage or None)
            background.close(others)
            raise
        except Exception as e:  # the other answers may still be used
            error = e
            print(f"Streamed answer failed: {without_keys(e)}")
        if pending is not None:
            pending.answered()
        if display:
            shown.append("".join(display))
        first = FillAnswer("".join(content) if error is None and pending is not None else None, usage or None,
                           error, pending)
        answers = [first] + [future.result() for future in others]
    finally:
        pool.shutdown(wait=False)
    kept = _keep_most_typical(answers, definition, calls)
    if kept is None:
        return False
    position, text, prediction, kept_attempt = kept
    if position != 0:
        note = VOTE_NOTE.format(kept=position + 1, asked=count) + text
        shown.append(note)
        yield stream_event("raw_delta", delta=note)
    try:
        prediction = _labelled_prediction(prediction, provider=provider, model=model, definition=definition, calls=calls)
        final_payload = _finalize_pipeline_output("".join(shown), prediction, linker=Linker(provider, model, calls=calls))
    except Exception as e:  # as a streamed try today: the answer is marked and the tries go on
        mark_unusable(calls, "decomposition", f"The answer could not be used: {type(e).__name__}", attempt=kept_attempt)
        if calls is not None:
            calls.update_choices(answers={"asked": count, "kept": None})
        print(f"Kept answer could not be used: {without_keys(e)}")
        return False
    yield stream_event("final", data=final_payload)
    return True


def stream_pipeline_events(
    definition: str,
    *,
    disable_thinking: bool = True,
    model_provider: Optional[str] = None,
    model_name: Optional[str] = None,
    calls: Optional[CallLog] = None,
) -> Iterator[str]:
    """Run the decomposition pipeline, yielding NDJSON event lines.

    Streams ``raw_delta`` events while the model responds, then a terminal
    ``final`` event with the full payload, or an ``error`` event on failure.
    Retries up to 3 times, with a non-streamed fallback per attempt.

    Args:
        definition: The variable definition.
        disable_thinking: Forwarded to the provider call.
        model_provider: Optional provider override.
        model_name: Optional model override.
        calls: The run's call log; every request to a model is recorded there.

    Yields:
        NDJSON event line strings (``raw_delta`` / ``final`` / ``error``).

    Side effects:
        Performs LLM and entity-linking (vocabulary search and the run's model provider) calls.
    """
    try:
        definition = definition.strip()
        prompt, selected_model_provider, selected_model_name = _prepare_pipeline_inputs(
            definition,
            model_name=model_name,
            model_provider=model_provider,
            calls=calls,
        )
        all_display_parts: List[str] = []
        last_error_message = "Could not extract valid JSON from the model output."
        first_attempt = 1
        if use_measured_path(selected_model_provider) and answer_vote.ANSWERS > 1:
            finished = yield from _stream_voted(
                prompt,
                provider=selected_model_provider,
                model=selected_model_name,
                definition=definition,
                disable_thinking=disable_thinking,
                calls=calls,
                shown=all_display_parts,
            )
            if finished:
                return
            first_attempt = 2

        for attempt in range(first_attempt, 4):
            attempt_display_parts: List[str] = []
            attempt_content_parts: List[str] = []
            saw_reasoning = False
            started_content = False
            streamed_any_chunk = False

            if attempt > 1:
                retry_note = "\n\n[Retrying after the previous streamed response did not yield valid JSON.]\n\n"
                all_display_parts.append(retry_note)
                yield stream_event("raw_delta", delta=retry_note)

            # A try is recorded once its request is about to go out: a missing or malformed key fails before.
            pending: Optional[PendingCall] = None
            usage: Dict[str, Any] = {}
            try:
                if selected_model_provider == PSNC_MODEL_PROVIDER:
                    stream = stream_psnc_model(
                        selected_model_name,
                        prompt,
                        settings.temperature,
                        disable_thinking=disable_thinking,
                        measured=use_measured_path(selected_model_provider),
                        usage=usage,
                    )
                    pending = start_call(calls, "decomposition", selected_model_provider, selected_model_name)
                else:
                    client = get_openai_client()
                    pending = start_call(calls, "decomposition", selected_model_provider, selected_model_name)
                    stream = _openrouter_deltas(
                        client.chat.completions.create(
                            **build_chat_completion_request_kwargs(
                                selected_model_name,
                                prompt,
                                settings.temperature,
                                disable_thinking=disable_thinking,
                                stream=True,
                            )
                        ),
                        usage,
                    )

                for reasoning_delta, content_delta in stream:
                    if reasoning_delta:
                        streamed_any_chunk = True
                        saw_reasoning = True
                        attempt_display_parts.append(reasoning_delta)
                        yield stream_event("raw_delta", delta=reasoning_delta)

                    if content_delta:
                        streamed_any_chunk = True
                        if saw_reasoning and not started_content:
                            separator = "\n\n"
                            attempt_display_parts.append(separator)
                            yield stream_event("raw_delta", delta=separator)
                        started_content = True
                        attempt_display_parts.append(content_delta)
                        attempt_content_parts.append(content_delta)
                        yield stream_event("raw_delta", delta=content_delta)

                attempt_display = "".join(attempt_display_parts)
                attempt_content = "".join(attempt_content_parts)

                if attempt_display:
                    all_display_parts.append(attempt_display)

                stripped_content = attempt_content.strip()
                if stripped_content.startswith("<!DOCTYPE html") or stripped_content.startswith("<html"):
                    last_error_message = "The model returned HTML instead of JSON."
                    pending.finish("unusable", error=last_error_message, usage=usage or None)
                    continue
                if not stripped_content:
                    last_error_message = "The streamed model response was empty."
                    pending.finish("unusable", error=last_error_message, usage=usage or None)
                    continue

                try:
                    pred = parse_llm_json(attempt_content, definition)
                except Exception as e:
                    last_error_message = without_keys(e)
                    pending.finish("unusable", error=f"Not valid JSON: {last_error_message}", usage=usage or None)
                    print(f"LLM parse attempt {attempt} failed: {last_error_message}")
                    continue

                pending.finish("ok", usage=usage or None)
                try:
                    pred = _labelled_prediction(
                        pred,
                        provider=selected_model_provider,
                        model=selected_model_name,
                        definition=definition,
                        calls=calls,
                    )
                    final_payload = _finalize_pipeline_output(
                        "".join(all_display_parts),
                        pred,
                        linker=Linker(selected_model_provider, selected_model_name, calls=calls),
                    )
                except Exception as e:
                    mark_unusable(calls, "decomposition", f"The answer could not be used: {type(e).__name__}")
                    raise
                yield stream_event("final", data=final_payload)
                return

            except APIStatusError as e:
                last_error_message = without_keys(e)
                if pending is not None:
                    pending.finish("error", error=call_error(e), usage=usage or None)
                print(f"APIStatusError attempt {attempt}: {last_error_message}")
            except (OpenAIError, httpx.HTTPError) as e:
                last_error_message = without_keys(e)
                if pending is not None:
                    pending.finish("error", error=call_error(e), usage=usage or None)
                print(f"Transport error attempt {attempt}: {last_error_message}")
            except Exception as e:
                last_error_message = without_keys(e)
                if pending is not None:
                    pending.finish("error", error=call_error(e), usage=usage or None)
                print(f"Unexpected streaming error attempt {attempt}: {last_error_message}")
            finally:
                # A stream the client closes mid-answer ends here with GeneratorExit, which no
                # handler above catches; every other exit has finished the call already.
                if pending is not None:
                    pending.finish("error", error="The stream closed before the answer was complete.", usage=usage or None)

            if not streamed_any_chunk:
                fallback_raw = (
                    call_psnc_model(
                        selected_model_name,
                        prompt,
                        settings.temperature,
                        disable_thinking=disable_thinking,
                        calls=calls,
                    )
                    if selected_model_provider == PSNC_MODEL_PROVIDER
                    else call_model(
                        selected_model_name,
                        prompt,
                        settings.temperature,
                        disable_thinking=disable_thinking,
                        calls=calls,
                    )
                )
                fallback_display = fallback_raw or ""
                if fallback_display:
                    all_display_parts.append(fallback_display)
                    yield stream_event("raw_delta", delta=fallback_display)
                    try:
                        pred = parse_llm_json(fallback_raw, definition)
                    except Exception as e:
                        last_error_message = without_keys(e)
                        mark_unusable(calls, "decomposition", f"Not valid JSON: {last_error_message}")
                        print(f"Fallback parse attempt {attempt} failed: {last_error_message}")
                        continue
                    try:
                        pred = _labelled_prediction(
                            pred,
                            provider=selected_model_provider,
                            model=selected_model_name,
                            definition=definition,
                            calls=calls,
                        )
                        final_payload = _finalize_pipeline_output(
                            "".join(all_display_parts),
                            pred,
                            linker=Linker(selected_model_provider, selected_model_name, calls=calls),
                        )
                    except Exception as e:
                        mark_unusable(calls, "decomposition", f"The answer could not be used: {type(e).__name__}")
                        last_error_message = without_keys(e)
                        print(f"Fallback parse attempt {attempt} failed: {last_error_message}")
                        continue
                    yield stream_event("final", data=final_payload)
                    return

        yield stream_event(
            "error", detail=f"Could not extract valid JSON from the model output. Last error: {last_error_message}"
        )
    except ValueError as e:
        yield stream_event("error", detail=without_keys(e))
    except RuntimeError as e:
        yield stream_event("error", detail=without_keys(e))
    except Exception as e:
        yield stream_event("error", detail=f"Unexpected backend error: {without_keys(e)}")


def run_pipeline(
    definition: str,
    disable_thinking: bool = True,
    model_provider: Optional[str] = None,
    model_name: Optional[str] = None,
    calls: Optional[CallLog] = None,
    *,
    leave_out: Iterable[str] = frozenset(),
) -> Dict[str, Any]:
    """Run the decomposition pipeline and return one final payload (non-streaming).

    On the measured path the fill prompt goes out three times side by side and the most typical answer is kept;
    when none can be read, today's tries follow.

    Args:
        definition: The variable definition.
        disable_thinking: Forwarded to the provider call.
        model_provider: Optional provider override.
        model_name: Optional model override.
        calls: The run's call log; every request to a model is recorded there.
        leave_out: Lab ids of corpus variables the prompt's examples and the matrix finder may not show; the quality
            gate leaves out the variable it scores. No route sets it.

    Returns:
        The ``DecomposeResponse``-shaped dict.

    Side effects:
        Performs LLM and entity-linking (vocabulary search and the run's model provider) calls.
    """
    definition = definition.strip()
    prompt, selected_model_provider, selected_model_name = _prepare_pipeline_inputs(
        definition,
        model_name=model_name,
        model_provider=model_provider,
        leave_out=leave_out,
        calls=calls,
    )

    kept = None
    if use_measured_path(selected_model_provider) and answer_vote.ANSWERS > 1:
        answers = _fill_answers(prompt, selected_model_name, calls, answer_vote.ANSWERS)
        kept = _keep_most_typical(answers, definition, calls)
    if kept is not None:
        _, raw_llm_output, pred, kept_attempt = kept
    else:
        kept_attempt = None
        raw_llm_output, pred = call_llm_loose(
            selected_model_provider,
            selected_model_name,
            prompt,
            definition=definition,
            temperature=settings.temperature,
            disable_thinking=disable_thinking,
            measured=use_measured_path(selected_model_provider),
            calls=calls,
        )
    try:
        pred = _labelled_prediction(
            pred,
            provider=selected_model_provider,
            model=selected_model_name,
            definition=definition,
            calls=calls,
        )
        return _finalize_pipeline_output(
            raw_llm_output,
            pred,
            linker=Linker(selected_model_provider, selected_model_name, calls=calls),
        )
    except Exception as e:
        # The decomposition call returned text, but no result could be made from it.
        mark_unusable(calls, "decomposition", f"The answer could not be used: {type(e).__name__}",
                      attempt=kept_attempt)
        if kept is not None and calls is not None:  # none of the answers asked for made the result
            calls.update_choices(answers={"asked": answer_vote.ANSWERS, "kept": None})
        raise
