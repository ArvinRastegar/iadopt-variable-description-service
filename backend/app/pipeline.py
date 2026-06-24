"""End-to-end decomposition pipeline orchestration.

Ties the services together: build the prompt, call the LLM (streamed or not),
parse + validate, enrich with Wikidata URIs, and serialize to Turtle. Exposes the
two entry points the routers use (``run_pipeline``, ``stream_pipeline_events``) and
the startup warmup (``warmup_assets``).

This is the fan-out hub of the service layer: it imports llm, validation,
enrichment, rdf_ttl, prompts, clients, and core. Nothing imports it except the
routers and the app factory, so the dependency graph stays acyclic.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx
from jsonschema import Draft202012Validator
from openai import APIStatusError, OpenAIError

from .clients.http import get_http_session
from .clients.openai_client import get_openai_client
from .core.config import OPENROUTER_MODEL_PROVIDER, PSNC_MODEL_PROVIDER, settings
from .core.state import app_state
from .services.enrichment import enrich_with_uris_reranker
from .services.llm import (
    build_chat_completion_request_kwargs,
    call_llm_loose,
    call_model,
    call_psnc_model,
    extract_stream_text_deltas,
    parse_llm_json,
    resolve_model_name,
    resolve_model_provider,
    stream_event,
    stream_psnc_model,
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
        RuntimeError: If no prompt files are found.

    Side effects:
        May construct the OpenRouter client; reads schema/prompt/example files.
    """
    # OpenRouter is only initialized when it is enabled for this deployment.
    if OPENROUTER_MODEL_PROVIDER in settings.enabled_model_providers and settings.openrouter_api_key:
        get_openai_client()

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

    # Prime HTTP session
    get_http_session()


def _finalize_pipeline_output(
    raw_llm_output: str,
    pred: Dict[str, Any],
    *,
    creator_orcid_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate, enrich, and serialize a prediction into the response payload.

    Args:
        raw_llm_output: The raw model text to echo back.
        pred: The parsed prediction.
        creator_orcid_id: Optional ORCID override for TTL provenance.

    Returns:
        The ``DecomposeResponse``-shaped dict.

    Raises:
        RuntimeError: If ``pred`` is empty.

    Side effects:
        May call Wikidata/PSNC (enrichment) and ORCID (TTL creator metadata).
    """
    if not pred:
        raise RuntimeError("Could not extract valid JSON from the model output.")

    validation_errors = get_schema_validation_errors(pred, label_for_logs=pred.get("label"))
    validation_errors.extend(get_constraint_semantic_validation_errors(pred))
    schema_valid = len(validation_errors) == 0

    if settings.enable_wikidata_linking:
        try:
            enriched = enrich_with_uris_reranker(pred, threshold=settings.rerank_threshold)
        except Exception as e:
            print(f"Wikidata enrichment failed: {e}")
            enriched = pred
    else:
        enriched = pred

    ttl = json_to_ttl_repo_style(
        enriched,
        creator_orcid_id=creator_orcid_id,
    )

    return {
        "raw_llm_output": raw_llm_output,
        "parsed_json": pred,
        "schema_valid": schema_valid,
        "validation_errors": validation_errors,
        "enriched_json": enriched,
        "ttl": ttl,
    }


def _prepare_pipeline_inputs(
    definition: str,
    model_name: Optional[str] = None,
    model_provider: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Resolve the prompt, provider, and model for a decomposition request.

    Args:
        definition: The variable definition (must be non-empty after stripping).
        model_name: Optional requested model name.
        model_provider: Optional requested provider.

    Returns:
        A ``(prompt, provider, model_name)`` tuple.

    Raises:
        ValueError: If the definition is empty.
        RuntimeError: If no prompt files are available.
    """
    definition = definition.strip()
    if not definition:
        raise ValueError("Definition must not be empty.")

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
    selected_model_provider = resolve_model_provider(model_provider)
    selected_model_name = resolve_model_name(model_name, model_provider=selected_model_provider)

    return prompt, selected_model_provider, selected_model_name


def stream_pipeline_events(
    definition: str,
    *,
    disable_thinking: bool = True,
    model_provider: Optional[str] = None,
    model_name: Optional[str] = None,
    creator_orcid_id: Optional[str] = None,
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
        creator_orcid_id: Optional ORCID override for TTL provenance.

    Yields:
        NDJSON event line strings (``raw_delta`` / ``final`` / ``error``).

    Side effects:
        Performs LLM, reranker, Wikidata, and ORCID calls.
    """
    try:
        definition = definition.strip()
        prompt, selected_model_provider, selected_model_name = _prepare_pipeline_inputs(
            definition,
            model_name=model_name,
            model_provider=model_provider,
        )
        all_display_parts: List[str] = []
        last_error_message = "Could not extract valid JSON from the model output."

        for attempt in range(1, 4):
            attempt_display_parts: List[str] = []
            attempt_content_parts: List[str] = []
            saw_reasoning = False
            started_content = False
            streamed_any_chunk = False

            if attempt > 1:
                retry_note = "\n\n[Retrying after the previous streamed response did not yield valid JSON.]\n\n"
                all_display_parts.append(retry_note)
                yield stream_event("raw_delta", delta=retry_note)

            try:
                if selected_model_provider == PSNC_MODEL_PROVIDER:
                    stream = stream_psnc_model(
                        selected_model_name,
                        prompt,
                        settings.temperature,
                        disable_thinking=disable_thinking,
                    )
                else:
                    client = get_openai_client()
                    stream = (
                        extract_stream_text_deltas(chunk)
                        for chunk in client.chat.completions.create(
                            **build_chat_completion_request_kwargs(
                                selected_model_name,
                                prompt,
                                settings.temperature,
                                disable_thinking=disable_thinking,
                                stream=True,
                            )
                        )
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
                    continue
                if not stripped_content:
                    last_error_message = "The streamed model response was empty."
                    continue

                try:
                    pred = parse_llm_json(attempt_content, definition)
                except Exception as e:
                    last_error_message = str(e)
                    print(f"LLM parse attempt {attempt} failed: {e}")
                    continue

                final_payload = _finalize_pipeline_output(
                    "".join(all_display_parts),
                    pred,
                    creator_orcid_id=creator_orcid_id,
                )
                yield stream_event("final", data=final_payload)
                return

            except APIStatusError as e:
                last_error_message = str(e)
                print(f"APIStatusError attempt {attempt}: {e}")
            except (OpenAIError, httpx.HTTPError) as e:
                last_error_message = str(e)
                print(f"Transport error attempt {attempt}: {e}")
            except Exception as e:
                last_error_message = str(e)
                print(f"Unexpected streaming error attempt {attempt}: {e}")

            if not streamed_any_chunk:
                fallback_raw = (
                    call_psnc_model(
                        selected_model_name,
                        prompt,
                        settings.temperature,
                        disable_thinking=disable_thinking,
                    )
                    if selected_model_provider == PSNC_MODEL_PROVIDER
                    else call_model(
                        selected_model_name,
                        prompt,
                        settings.temperature,
                        disable_thinking=disable_thinking,
                    )
                )
                fallback_display = fallback_raw or ""
                if fallback_display:
                    all_display_parts.append(fallback_display)
                    yield stream_event("raw_delta", delta=fallback_display)
                    try:
                        pred = parse_llm_json(fallback_raw, definition)
                        final_payload = _finalize_pipeline_output(
                            "".join(all_display_parts),
                            pred,
                            creator_orcid_id=creator_orcid_id,
                        )
                        yield stream_event("final", data=final_payload)
                        return
                    except Exception as e:
                        last_error_message = str(e)
                        print(f"Fallback parse attempt {attempt} failed: {e}")

        yield stream_event(
            "error", detail=f"Could not extract valid JSON from the model output. Last error: {last_error_message}"
        )
    except ValueError as e:
        yield stream_event("error", detail=str(e))
    except RuntimeError as e:
        yield stream_event("error", detail=str(e))
    except Exception as e:
        yield stream_event("error", detail=f"Unexpected backend error: {e}")


def run_pipeline(
    definition: str,
    disable_thinking: bool = True,
    model_provider: Optional[str] = None,
    model_name: Optional[str] = None,
    creator_orcid_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the decomposition pipeline and return one final payload (non-streaming).

    Args:
        definition: The variable definition.
        disable_thinking: Forwarded to the provider call.
        model_provider: Optional provider override.
        model_name: Optional model override.
        creator_orcid_id: Optional ORCID override for TTL provenance.

    Returns:
        The ``DecomposeResponse``-shaped dict.

    Side effects:
        Performs LLM, reranker, Wikidata, and ORCID calls.
    """
    definition = definition.strip()
    prompt, selected_model_provider, selected_model_name = _prepare_pipeline_inputs(
        definition,
        model_name=model_name,
        model_provider=model_provider,
    )

    raw_llm_output, pred = call_llm_loose(
        selected_model_provider,
        selected_model_name,
        prompt,
        definition=definition,
        temperature=settings.temperature,
        disable_thinking=disable_thinking,
    )
    return _finalize_pipeline_output(
        raw_llm_output,
        pred,
        creator_orcid_id=creator_orcid_id,
    )
