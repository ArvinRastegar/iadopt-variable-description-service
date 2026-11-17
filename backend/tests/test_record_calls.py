"""Every request a decomposition run sends to a model, recorded one by one (D-037, stage 2).

A run's call log is filled where each request is made (the call helpers, the streamed and
fallback decomposition, the label call and each linking question) and written with the run.
No model, registry or vocabulary is reached: providers, sessions and the pipeline are faked.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import openai
import pytest
import requests

from app import pipeline
from app.core.config import settings
from app.routers import decompose as decompose_router
from app.services import call_log as cl
from app.services import entity_linking, label_comment, llm
from app.services.call_log import CallLog, ModelCall, start_call
from app.services.entity_linking import Linker
from app.services.llm import stream_event
from app.services.vocabularies import Candidate
from test_records import PIPELINE_RESULT, RecordTestCase


def by_fields(calls, *fields):
    return [tuple(getattr(call, field) for field in fields) for call in calls]


# --- the call log ------------------------------------------------------------------- #

def test_tries_are_numbered_per_purpose_and_per_key():
    log = CallLog()
    for purpose, key in [("decomposition", None), ("linking", "air"), ("linking", "air"),
                         ("linking", "water"), ("decomposition", None), ("label", None)]:
        start_call(log, purpose, "openrouter", "m", key=key).finish("ok")
    assert by_fields(log.calls(), "purpose", "attempt") == [
        ("decomposition", 1), ("linking", 1), ("linking", 2), ("linking", 1), ("decomposition", 2), ("label", 1)]


def test_a_call_is_recorded_once_with_its_tokens_and_reason():
    log = CallLog()
    pending = start_call(log, "decomposition", "psnc", "Qwen3.8-27B")
    pending.finish("error", error=RuntimeError("401   Unauthorized\nfor url"), usage={"prompt_tokens": 3})
    pending.finish("ok")
    (call,) = log.calls()
    assert (call.status, call.error, call.input_tokens, call.output_tokens) == ("error", "401 Unauthorized for url", 3, None)
    assert call.latency_ms >= 0 and call.started_at.endswith("+00:00")


def test_an_ok_call_keeps_no_reason_and_bad_counts_are_dropped():
    log = CallLog()
    start_call(log, "label", "psnc", "m").finish("ok", error="ignored", usage={"prompt_tokens": -1, "completion_tokens": True})
    (call,) = log.calls()
    assert (call.error, call.input_tokens, call.output_tokens) == (None, None, None)
    assert cl.usage_tokens(SimpleNamespace(prompt_tokens=5, completion_tokens=2)) == (5, 2)


def test_reasons_are_one_short_line():
    assert cl.short_reason("x" * 1000).endswith("…") and len(cl.short_reason("x" * 1000)) == cl.MAX_ERROR_CHARS
    assert cl.short_reason(ValueError()) == "ValueError"
    assert cl.short_reason("  ") is None and cl.short_reason(None) is None


def test_only_the_last_call_of_a_purpose_can_turn_unusable():
    log = CallLog()
    start_call(log, "decomposition", "p", "m").finish("ok")
    start_call(log, "label", "p", "m").finish("ok")
    log.mark_unusable("decomposition", "Not valid JSON")
    start_call(log, "decomposition", "p", "m").finish("error", error="down")
    log.mark_unusable("decomposition", "would hide the error")
    assert by_fields(log.calls(), "purpose", "status", "error") == [
        ("decomposition", "unusable", "Not valid JSON"), ("label", "ok", None), ("decomposition", "error", "down")]


def test_without_a_log_nothing_is_kept_and_names_are_checked():
    start_call(None, "linking", "p", "m").finish("ok")
    cl.mark_unusable(None, "linking", "nothing to mark")
    with pytest.raises(ValueError):
        start_call(None, "chat", "p", "m")
    with pytest.raises(ValueError):
        start_call(None, "label", "p", "m").finish("fine")


# --- the call helpers ------------------------------------------------------------------ #

class FakeResponse:
    def __init__(self, body=None, status_code=200, lines=()):
        self.status_code, self._body, self._lines, self.text = status_code, body, lines, "secret body"

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


class FakeSession:
    def __init__(self, responses):
        self.responses, self.posts = list(responses), []

    def post(self, url, headers=None, json=None, timeout=None, stream=False):
        self.posts.append(json)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def answer(content, usage=None):
    body = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = usage
    return body


def fake_client(*outcomes, seen=None):
    """An OpenRouter client whose create() answers from a script of results and exceptions."""
    script = list(outcomes)

    def create(**kwargs):
        if seen is not None:
            seen.append(kwargs)
        outcome = script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def completion(content, prompt_tokens=None, completion_tokens=None):
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens) if prompt_tokens else None
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


@pytest.fixture
def psnc_session(monkeypatch):
    monkeypatch.setattr(settings, "psnc_api_key", "test-psnc-key")

    def install(*responses):
        session = FakeSession(responses)
        monkeypatch.setattr(llm, "get_http_session", lambda: session)
        return session

    return install


def test_each_psnc_try_is_recorded_without_the_response_body(psnc_session):
    psnc_session(FakeResponse(status_code=401), FakeResponse(answer("{}", {"prompt_tokens": 900, "completion_tokens": 40})))
    log = CallLog()
    assert llm.call_psnc_model("Qwen3.8-27B", "prompt", 0.5, calls=log) == "{}"
    assert by_fields(log.calls(), "purpose", "attempt", "provider", "status", "error", "input_tokens", "output_tokens") == [
        ("decomposition", 1, "psnc", "error", "HTTP 401", None, None),
        ("decomposition", 2, "psnc", "ok", None, 900, 40)]


def test_each_openrouter_try_is_recorded(monkeypatch):
    client = fake_client(RuntimeError("upstream down"), completion("<html>busy</html>"), completion("{}", 1200, 300))
    monkeypatch.setattr(llm, "get_openai_client", lambda: client)
    log = CallLog()
    assert llm.call_model("qwen/qwen3-32b", "prompt", 0.5, calls=log, purpose="label") == "{}"
    assert by_fields(log.calls(), "purpose", "attempt", "status", "error", "input_tokens") == [
        ("label", 1, "error", "RuntimeError: upstream down", None),
        ("label", 2, "unusable", "The model returned HTML.", None),
        ("label", 3, "ok", None, 1200)]


def test_an_answer_that_is_not_json_turns_its_call_unusable(monkeypatch):
    client = fake_client(completion("nope"), completion("{}"))
    monkeypatch.setattr(llm, "get_openai_client", lambda: client)
    monkeypatch.setattr(llm, "parse_llm_json", lambda raw, definition: json.loads(raw))
    log = CallLog()
    assert llm.call_llm_loose("openrouter", "m", "prompt", "d", 0.5, calls=log) == ("{}", {})
    assert by_fields(log.calls(), "attempt", "status") == [(1, "unusable"), (2, "ok")]
    assert log.calls()[0].error.startswith("Not valid JSON")


def test_streamed_openrouter_requests_ask_for_their_token_counts():
    streamed = llm.build_chat_completion_request_kwargs("m", "p", 0.5, stream=True)
    assert streamed["stream_options"] == {"include_usage": True}
    assert "stream_options" not in llm.build_chat_completion_request_kwargs("m", "p", 0.5)


def test_a_psnc_stream_keeps_token_counts_it_is_sent_without_asking(psnc_session):
    events = ['data: {"choices": [{"delta": {"content": "{}"}}]}',
              'data: {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}}', "data: [DONE]"]
    session = psnc_session(FakeResponse(lines=events))
    usage = {}
    deltas = list(llm.stream_psnc_model("Qwen3.8-27B", "prompt", 0.5, measured=True, usage=usage))
    assert deltas[0] == ("", "{}") and usage == {"prompt_tokens": 9, "completion_tokens": 4}
    # The measured body is unchanged: it does not ask for the counts (D-003).
    assert set(session.posts[0]) == {"model", "messages", "stream", "temperature", "top_p", "max_tokens", "chat_template_kwargs"}


# --- the pipeline ------------------------------------------------------------------------- #

PRED = {"definition": "air temperature", "hasProperty": "temperature"}


@pytest.fixture
def run(monkeypatch):
    """A pipeline on a chosen provider with the label, linking and Turtle steps replaced.

    Returns what those steps saw: the linkers and the logs the label step was given. A
    ``finalize_errors`` list makes the Turtle step raise its next item once per call.
    """
    seen = SimpleNamespace(linkers=[], label_logs=[], finalize_errors=[])

    def install(provider="openrouter", model="qwen/qwen3-32b"):
        monkeypatch.setattr(pipeline, "_prepare_pipeline_inputs",
                            lambda definition, model_name=None, model_provider=None: ("prompt", provider, model))

        def label(prediction, **kwargs):
            seen.label_logs.append(kwargs.get("calls"))
            return prediction

        monkeypatch.setattr(pipeline, "_labelled_prediction", label)
        monkeypatch.setattr(pipeline, "parse_llm_json",
                            lambda raw, definition: (_ for _ in ()).throw(ValueError("Expecting value")) if raw == "nope" else dict(PRED))

        def finalize(raw, prediction, *, linker):
            seen.linkers.append(linker)
            if seen.finalize_errors:
                raise seen.finalize_errors.pop(0)
            return {"ttl": ""}

        monkeypatch.setattr(pipeline, "_finalize_pipeline_output", finalize)
        return seen

    return install


def chunk(content=None, usage=None):
    choices = [SimpleNamespace(delta=SimpleNamespace(content=content))] if content is not None else []
    return SimpleNamespace(choices=choices, usage=usage)


def events_of(log, provider=None):
    return [json.loads(line) for line in pipeline.stream_pipeline_events("air temperature", calls=log)]


def test_a_streamed_openrouter_try_is_recorded_with_its_tokens(monkeypatch, run):
    seen_steps = run()
    seen = []
    client = fake_client(iter([chunk("{}"), chunk(usage=SimpleNamespace(prompt_tokens=1500, completion_tokens=320))]), seen=seen)
    monkeypatch.setattr(pipeline, "get_openai_client", lambda: client)
    log = CallLog()
    assert events_of(log)[-1]["type"] == "final"
    assert by_fields(log.calls(), "purpose", "attempt", "provider", "model", "status", "input_tokens", "output_tokens") == [
        ("decomposition", 1, "openrouter", "qwen/qwen3-32b", "ok", 1500, 320)]
    assert seen[0]["stream_options"] == {"include_usage": True}
    # The label step gets the same log, and linking gets it through the linker.
    assert seen_steps.label_logs == [log] and seen_steps.linkers[0].calls is log


def test_failed_and_unusable_streamed_tries_are_recorded_before_the_one_that_worked(monkeypatch, run):
    run()
    client = fake_client(iter([chunk("nope")]), iter([chunk("{}")]))
    monkeypatch.setattr(pipeline, "get_openai_client", lambda: client)
    log = CallLog()
    assert events_of(log)[-1]["type"] == "final"
    assert by_fields(log.calls(), "attempt", "status") == [(1, "unusable"), (2, "ok")]
    assert log.calls()[0].error == "Not valid JSON: Expecting value"


def test_the_non_streamed_fallback_records_its_own_tries(monkeypatch, run):
    seen_steps = run()
    client = fake_client(RuntimeError("stream refused"), completion("{}", 800, 60))
    monkeypatch.setattr(pipeline, "get_openai_client", lambda: client)
    monkeypatch.setattr(llm, "get_openai_client", lambda: client)
    log = CallLog()
    assert events_of(log)[-1]["type"] == "final"
    assert by_fields(log.calls(), "attempt", "status", "error", "input_tokens") == [
        (1, "error", "RuntimeError: stream refused", None), (2, "ok", None, 800)]
    assert seen_steps.label_logs == [log] and seen_steps.linkers[0].calls is log


def test_a_fallback_answer_that_is_not_json_is_marked_unusable(monkeypatch, run):
    run()
    client = fake_client(RuntimeError("stream refused"), completion("nope"), iter([chunk("{}")]))
    monkeypatch.setattr(pipeline, "get_openai_client", lambda: client)
    monkeypatch.setattr(llm, "get_openai_client", lambda: client)
    log = CallLog()
    assert events_of(log)[-1]["type"] == "final"
    assert by_fields(log.calls(), "attempt", "status", "error") == [
        (1, "error", "RuntimeError: stream refused"), (2, "unusable", "Not valid JSON: Expecting value"), (3, "ok", None)]


def test_an_answer_a_later_step_throws_away_is_marked_unusable(monkeypatch, run):
    seen_steps = run()
    seen_steps.finalize_errors.append(AttributeError("'int' object has no attribute 'strip'"))
    client = fake_client(iter([chunk("{}")]), iter([chunk("{}")]))
    monkeypatch.setattr(pipeline, "get_openai_client", lambda: client)
    log = CallLog()
    assert events_of(log)[-1]["type"] == "final"
    assert by_fields(log.calls(), "attempt", "status", "error") == [
        (1, "unusable", "The answer could not be used: AttributeError"), (2, "ok", None)]


def test_an_openrouter_error_body_is_never_stored(monkeypatch, run):
    run()
    flagged = 'Error code: 403 - {"error": {"metadata": {"flagged_input": "oxygen in the private pond of Mrs X"}}}'
    response = httpx.Response(403, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    refusal = openai.APIStatusError(flagged, response=response, body=None)
    client = fake_client(*[refusal] * 12)
    monkeypatch.setattr(pipeline, "get_openai_client", lambda: client)
    monkeypatch.setattr(llm, "get_openai_client", lambda: client)
    log = CallLog()
    events_of(log)
    assert {call.error for call in log.calls()} == {"HTTP 403"}


def test_a_stream_closed_mid_answer_is_recorded(monkeypatch, run):
    run()
    client = fake_client(iter([chunk("{"), chunk("}")]))
    monkeypatch.setattr(pipeline, "get_openai_client", lambda: client)
    log = CallLog()
    events = pipeline.stream_pipeline_events("air temperature", calls=log)
    assert json.loads(next(events))["type"] == "raw_delta"
    events.close()
    assert by_fields(log.calls(), "attempt", "status", "error") == [
        (1, "error", "The stream closed before the answer was complete.")]


def test_a_missing_key_records_no_call(monkeypatch, run):
    run()

    def no_key():
        raise RuntimeError("OPENROUTER_API_KEY is not set.")

    monkeypatch.setattr(pipeline, "get_openai_client", no_key)
    monkeypatch.setattr(llm, "get_openai_client", no_key)
    log = CallLog()
    assert events_of(log)[-1] == {"type": "error", "detail": "OPENROUTER_API_KEY is not set."}
    assert log.calls() == []


def test_a_missing_psnc_key_records_no_call(monkeypatch, run):
    run(provider="psnc", model="Qwen3.8-27B")
    monkeypatch.setattr(settings, "psnc_api_key", None)
    log = CallLog()
    assert events_of(log)[-1] == {"type": "error", "detail": "PSNC_API_KEY is not set."}
    assert log.calls() == []


def test_a_psnc_stream_records_counts_only_when_psnc_sends_them(monkeypatch, run):
    run(provider="psnc", model="Qwen3.8-27B")

    def stream(*args, usage=None, **kwargs):
        usage.update({"prompt_tokens": 7, "completion_tokens": 3})
        yield "", "{}"

    monkeypatch.setattr(pipeline, "stream_psnc_model", stream)
    log = CallLog()
    events_of(log)
    assert by_fields(log.calls(), "provider", "status", "input_tokens", "output_tokens") == [("psnc", "ok", 7, 3)]


def test_a_run_that_never_answers_keeps_every_try(monkeypatch, run):
    run()
    client = fake_client(*[RuntimeError("401 Missing Authentication header")] * 12)
    monkeypatch.setattr(pipeline, "get_openai_client", lambda: client)
    monkeypatch.setattr(llm, "get_openai_client", lambda: client)
    log = CallLog()
    assert events_of(log)[-1]["type"] == "error"
    calls = log.calls()
    # Three streamed tries, each followed by a fallback that tries three times.
    assert len(calls) == 12 and {call.status for call in calls} == {"error"}
    assert [call.attempt for call in calls] == list(range(1, 13))


def test_run_pipeline_records_its_calls(monkeypatch, run):
    seen_steps = run()
    client = fake_client(completion("{}", 1000, 100))
    monkeypatch.setattr(llm, "get_openai_client", lambda: client)
    log = CallLog()
    pipeline.run_pipeline("air temperature", calls=log)
    assert by_fields(log.calls(), "purpose", "status", "input_tokens") == [("decomposition", "ok", 1000)]
    assert seen_steps.label_logs == [log] and seen_steps.linkers[0].calls is log


def test_run_pipeline_marks_its_call_unusable_when_no_result_comes_of_it(monkeypatch, run):
    seen_steps = run()
    seen_steps.finalize_errors.append(KeyError("hasProperty"))
    client = fake_client(completion("{}"))
    monkeypatch.setattr(llm, "get_openai_client", lambda: client)
    log = CallLog()
    with pytest.raises(KeyError):
        pipeline.run_pipeline("air temperature", calls=log)
    assert by_fields(log.calls(), "status", "error") == [("unusable", "The answer could not be used: KeyError")]


# --- the label call and the linking questions -------------------------------------------- #

def test_the_label_call_is_recorded_and_an_unreadable_answer_marked(psnc_session):
    psnc_session(FakeResponse(answer('{"label": "L", "comment": "C"}', {"prompt_tokens": 50, "completion_tokens": 9})),
                 FakeResponse(answer("no json here")))
    log = CallLog()
    kwargs = dict(model_provider="psnc", model="Qwen3.8-27B", temperature=0.5, calls=log)
    assert label_comment.generate_label_and_comment("air temperature at 2 m", **kwargs) == ("L", "C")
    label_comment.generate_label_and_comment("air temperature at 2 m", **kwargs)
    assert by_fields(log.calls(), "purpose", "attempt", "status", "input_tokens") == [
        ("label", 1, "ok", 50), ("label", 2, "unusable", None)]


SHOWN = [Candidate("wikidata", "http://www.wikidata.org/entity/Q3230", "Q3230", "air", "mixture of gases"),
         Candidate("wikidata", "http://www.wikidata.org/entity/Q1", "Q1", "AIR", "software")]


@pytest.fixture
def linking(monkeypatch):
    monkeypatch.setattr(settings, "psnc_api_key", "test-psnc-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr(entity_linking, "_sleep", lambda seconds: None)

    def install(*responses):
        session = FakeSession(responses)
        monkeypatch.setattr(entity_linking, "get_http_session", lambda: session)
        return session

    return install


def test_each_linking_question_is_recorded_per_term(linking):
    linking(FakeResponse(answer("C")), FakeResponse(answer("1", {"prompt_tokens": 480, "completion_tokens": 1})),
            FakeResponse(answer("0")))
    log = CallLog()
    run = Linker("openrouter", "qwen/qwen3-32b", calls=log)
    assert entity_linking.ask_for_choice("air", "air temperature", SHOWN, run) == 1
    assert entity_linking.ask_for_choice("water", "air temperature", SHOWN, run) == 0
    assert by_fields(log.calls(), "purpose", "attempt", "provider", "status", "error", "input_tokens") == [
        ("linking", 1, "openrouter", "unusable", "the text is not one of the offered labels", None),
        ("linking", 2, "openrouter", "ok", None, 480),
        ("linking", 1, "openrouter", "ok", None, None)]


def test_a_refused_linking_question_is_recorded_before_linking_stops(linking):
    linking(FakeResponse(status_code=401))
    log = CallLog()
    with pytest.raises(RuntimeError, match="PSNC refused"):
        entity_linking.ask_for_choice("air", "d", SHOWN, Linker("psnc", "Qwen3.8-27B", calls=log))
    assert by_fields(log.calls(), "status", "error") == [("error", "HTTP 401")]


def test_a_linking_question_never_sent_is_not_recorded(linking, monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    log = CallLog()
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        entity_linking.ask_for_choice("air", "d", SHOWN, Linker("openrouter", "m", calls=log))
    assert log.calls() == []


def test_the_log_is_not_part_of_the_linkers_identity():
    assert Linker("psnc", "Qwen3.8-27B", calls=CallLog()) == Linker("psnc", "Qwen3.8-27B")


# --- the record --------------------------------------------------------------------------- #

def two_calls(log):
    start_call(log, "decomposition", "openrouter", "qwen/qwen3-32b").finish("error", error="HTTP 503")
    start_call(log, "decomposition", "openrouter", "qwen/qwen3-32b").finish(
        "ok", usage={"prompt_tokens": 1500, "completion_tokens": 320})
    start_call(log, "linking", "openrouter", "qwen/qwen3-32b", key="air").finish(
        "ok", usage={"prompt_tokens": 480, "completion_tokens": 1})


class RecordedCallTests(RecordTestCase):
    def calls_of(self, record_id):
        return self.rows(
            """SELECT c.purpose, c.attempt, c.provider, c.model, c.latency_ms, c.input_tokens, c.output_tokens,
                      c.status, c.error, c.started_at
               FROM record_llm_calls c JOIN record_items i ON i.id = c.item_id
               WHERE i.public_id = ? ORDER BY c.id""",
            record_id,
        )

    def test_a_streamed_run_is_recorded_with_every_call(self):
        self.login()

        def stream(definition, calls=None, **kwargs):
            two_calls(calls)
            yield stream_event("final", data=dict(PIPELINE_RESULT))

        with patch.object(decompose_router, "stream_pipeline_events", side_effect=stream):
            response = self.client.post("/api/decompose/stream", json={"definition": "dissolved oxygen in water"})
        record_id = json.loads(response.text.splitlines()[-1])["data"]["record_id"]
        rows = self.calls_of(record_id)
        self.assertEqual(
            [(r["purpose"], r["attempt"], r["status"], r["error"], r["input_tokens"], r["output_tokens"]) for r in rows],
            [("decomposition", 1, "error", "HTTP 503", None, None),
             ("decomposition", 2, "ok", None, 1500, 320),
             ("linking", 1, "ok", None, 480, 1)],
        )
        self.assertTrue(all(r["provider"] == "openrouter" and r["latency_ms"] >= 0 and r["started_at"] for r in rows))

    def test_a_failed_run_keeps_its_calls(self):
        self.login()

        def failing(definition, calls=None, **kwargs):
            start_call(calls, "decomposition", "psnc", "Qwen3.8-27B").finish("error", error="HTTP 401")
            raise RuntimeError("model unavailable")

        with patch.object(decompose_router, "run_pipeline", side_effect=failing):
            response = self.client.post("/api/decompose", json={"definition": "dissolved oxygen in water"})
        self.assertEqual(response.status_code, 500)
        [run] = self.rows("SELECT item_id, status FROM record_runs")
        self.assertEqual(run["status"], "error")
        calls = self.rows("SELECT purpose, status, error FROM record_llm_calls WHERE item_id = ?", run["item_id"])
        self.assertEqual(calls, [{"purpose": "decomposition", "status": "error", "error": "HTTP 401"}])

    def test_a_non_streamed_run_is_recorded_with_its_calls(self):
        self.login()

        def succeeding(definition, calls=None, **kwargs):
            two_calls(calls)
            return dict(PIPELINE_RESULT)

        with patch.object(decompose_router, "run_pipeline", side_effect=succeeding):
            response = self.client.post("/api/decompose", json={"definition": "dissolved oxygen in water"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.calls_of(response.json()["record_id"])), 3)

    def test_recorded_calls_cannot_be_changed_or_deleted(self):
        self.login()

        def succeeding(definition, calls=None, **kwargs):
            two_calls(calls)
            return dict(PIPELINE_RESULT)

        with patch.object(decompose_router, "run_pipeline", side_effect=succeeding):
            self.client.post("/api/decompose", json={"definition": "dissolved oxygen in water"})
        conn = sqlite3.connect(self.store.db_path)
        try:
            for sql in ("UPDATE record_llm_calls SET status = 'ok'", "DELETE FROM record_llm_calls"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(sql)
        finally:
            conn.close()

    def test_calls_are_written_as_given(self):
        call = ModelCall("label", 1, "psnc", "Qwen3.8-27B", "2026-11-17T10:00:00+00:00", 812, 50, 9, "ok", None)
        record_id = self.store_run_with([call])
        [row] = self.calls_of(record_id)
        self.assertEqual((row["latency_ms"], row["started_at"], row["model"]), (812, "2026-11-17T10:00:00+00:00", "Qwen3.8-27B"))

    def store_run_with(self, calls):
        from app.auth import utc_now
        from app.core import dependencies as core_deps

        return core_deps.record_store.record_decomposition(
            user=self.bob, request=None, definition="dissolved oxygen in water", provider="psnc",
            model="Qwen3.8-27B", disable_thinking=True, prompt_setup="measured", prompt_sha256=None,
            started_at=utc_now(), ended_at=utc_now(), result=dict(PIPELINE_RESULT), error=None, calls=calls,
        )
