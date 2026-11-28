"""Choosing a candidate: searching, asking the model, and asking again.

The model cannot be restricted to answer in exactly the right form, so an answer is checked strictly and the identical
request is sent again, up to four attempts in all. These tests pin which failures are retried, which abort, and what a
label ends up with when the attempts run out.

Fully offline: the HTTP session is replaced in both modules that use it, and retries do not wait.
"""

from __future__ import annotations

import copy
import json
import logging
import pathlib
import re

import pytest
import requests

from app.core.config import settings
from app.services import entity_choice, entity_linking, vocabularies

VOCAB = pathlib.Path(__file__).resolve().parent / "fixtures" / "vocabularies"
TERM = "depth"
KEY = "depth"
DEFINITION = "depth of the water column at the sampling station"


def real(source):
    return (VOCAB / f"depth.{source}.json").read_text(encoding="utf-8")


def chat(content, finish_reason="length", **message):
    return {"choices": [{"finish_reason": finish_reason,
                         "message": {"role": "assistant", "content": content, **message}}]}


class FakeResponse:
    def __init__(self, body=None, status_code=200, headers=None, content=None, not_json=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self._not_json = not_json
        if content is None:
            content = json.dumps(body)
        self.content = content.encode("utf-8") if isinstance(content, str) else content

    def json(self):
        if self._not_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class FakeSession:
    """GET answers by host; POST answers from a script of responses and exceptions, in order.

    The last scripted POST outcome repeats once the script is used up.
    """

    def __init__(self, gets, posts):
        self.gets, self.posts = gets, list(posts)
        self.get_calls, self.post_calls = [], []
        self.sleeps = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append(url)
        outcome = next(r for host, r in self.gets.items() if host in url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def post(self, url, headers=None, json=None, timeout=None):
        # A snapshot, not a reference: the code under test must not be able to change what was "sent" afterwards.
        self.post_calls.append({"url": url, "headers": headers, "json": copy.deepcopy(json), "timeout": timeout})
        outcome = self.posts.pop(0) if len(self.posts) > 1 else self.posts[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def real_gets():
    return {"wikidata.org": FakeResponse(content=real("wikidata")), "qudt.org": FakeResponse(content=real("qudt")),
            "ebi.ac.uk": FakeResponse(content=real("om"))}


def empty_gets():
    return {"wikidata.org": FakeResponse(body={"search": [], "success": 1}),
            "qudt.org": FakeResponse(body={"results": {"bindings": []}}),
            "ebi.ac.uk": FakeResponse(body={"responseHeader": {"status": 0}, "response": {"numFound": 0, "docs": []}})}


@pytest.fixture
def world(monkeypatch):
    """Install a fake session, a PSNC key and a recording sleep; return a function to script it."""
    sleeps = []
    monkeypatch.setattr(settings, "psnc_api_key", "test-key")
    monkeypatch.setattr(entity_linking, "_sleep", sleeps.append)

    def install(posts, gets=None):
        fake = FakeSession(real_gets() if gets is None else gets, posts)
        fake.sleeps = sleeps
        monkeypatch.setattr(entity_linking, "get_http_session", lambda: fake)
        monkeypatch.setattr(vocabularies, "get_http_session", lambda: fake)
        return fake

    return install


def option_lines(user_message):
    """The text after the label of every candidate line of a user message ("none of these" excluded)."""
    return [line.split(". ", 1)[1] for line in user_message.splitlines() if re.fullmatch(r"[1-9A-Z]\. .*", line)]


def shown_for(skip=()):
    """The candidates in the order the model is shown them, rebuilt from the recorded responses."""
    pool = []
    for source in vocabularies.SOURCES:
        if source not in skip:
            pool.extend(vocabularies.parse_response(source, real(source)))
    return [pool[i] for i in entity_choice.presentation_order(pool, KEY)]


def label_of(candidate_key, skip=()):
    keys = [c.key for c in shown_for(skip)]
    return entity_choice.option_label(keys.index(candidate_key) + 1)


# --- a valid answer ------------------------------------------------------------------------------------------------ #

@pytest.mark.parametrize("candidate_key", ["om:Depth", "qudt:Depth"])
def test_a_candidate_of_any_vocabulary_can_be_chosen(world, candidate_key):
    fake = world([FakeResponse(chat(label_of(candidate_key)))])
    chosen = entity_linking.choose_candidate(TERM, DEFINITION)
    assert chosen.key == candidate_key
    assert chosen.iri == {"om:Depth": "http://www.ontology-of-units-of-measure.org/resource/om-2/Depth",
                          "qudt:Depth": "http://qudt.org/vocab/quantitykind/Depth"}[candidate_key]
    assert len(fake.post_calls) == 1 and fake.sleeps == []


def test_none_of_these_is_no_link_and_is_not_asked_again(world):
    fake = world([FakeResponse(chat("0"))])
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert len(fake.post_calls) == 1


def test_the_request_is_the_measured_one_with_the_configured_key(world):
    fake = world([FakeResponse(chat("0"))])
    entity_linking.choose_candidate("  Depth ", DEFINITION)
    (call,) = fake.post_calls
    assert call["url"].endswith("/v1/chat/completions")
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["timeout"] == entity_linking.CHAT_TIMEOUT_S
    body = call["json"]
    assert body["model"] == "Qwen3.8-27B" and body["temperature"] == 0 and body["max_tokens"] == 1
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    user = body["messages"][1]["content"]
    assert user.startswith(f"term: Depth\nvariable_definition: {DEFINITION}\n\ncandidates:\n")
    assert len(option_lines(user)) == 13
    assert user.endswith("\n\nAnswer with one label.")


def test_candidates_are_shown_in_the_seeded_order_not_source_order(world):
    fake = world([FakeResponse(chat("0"))])
    entity_linking.choose_candidate(TERM, DEFINITION)
    shown = option_lines(fake.post_calls[0]["json"]["messages"][1]["content"])
    assert shown == [entity_choice.shown_candidate(c) for c in shown_for()]
    by_source = sorted(shown_for(), key=lambda c: vocabularies.SOURCES.index(c.source))
    assert shown != [entity_choice.shown_candidate(c) for c in by_source]


# --- asking again -------------------------------------------------------------------------------------------------- #

def test_an_invalid_answer_is_asked_again_at_once_with_the_identical_request(world):
    fake = world([FakeResponse(chat("Option 3")), FakeResponse(chat(label_of("om:Depth")))])
    assert entity_linking.choose_candidate(TERM, DEFINITION).key == "om:Depth"
    assert len(fake.post_calls) == 2
    assert fake.post_calls[0]["json"] == fake.post_calls[1]["json"]
    assert fake.sleeps == []


def test_after_four_invalid_answers_the_label_has_no_link(world, caplog):
    fake = world([FakeResponse(chat("no"))])
    with caplog.at_level(logging.WARNING, logger=entity_linking.logger.name):
        assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert len(fake.post_calls) == 4
    assert caplog.text.count("gave no valid answer") == 4


def test_the_fourth_attempt_can_still_succeed(world):
    fake = world([FakeResponse(chat("no"))] * 3 + [FakeResponse(chat(label_of("om:Depth")))])
    assert entity_linking.choose_candidate(TERM, DEFINITION).key == "om:Depth"
    assert len(fake.post_calls) == 4


def test_every_attempt_sends_the_same_bytes_whatever_failed_before(world):
    fake = world([FakeResponse(status_code=503), requests.Timeout(), FakeResponse(chat("no")), FakeResponse(chat("0"))])
    entity_linking.choose_candidate(TERM, DEFINITION)
    sent = {json.dumps(call["json"], sort_keys=True) for call in fake.post_calls}
    assert len(fake.post_calls) == 4 and len(sent) == 1


def test_a_200_that_is_not_json_is_an_invalid_answer(world):
    fake = world([FakeResponse(not_json=True, content=b"<html>proxy page</html>")])
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert len(fake.post_calls) == 4


# --- transient failures and refusals ------------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "failure",
    [FakeResponse(status_code=429), FakeResponse(status_code=503), requests.Timeout("slow")],
    ids=["429", "503", "timeout"],
)
def test_a_transient_failure_is_retried_after_a_pause(world, failure):
    fake = world([failure, FakeResponse(chat(label_of("om:Depth")))])
    assert entity_linking.choose_candidate(TERM, DEFINITION).key == "om:Depth"
    assert len(fake.post_calls) == 2 and fake.sleeps == [0.5]


def test_pauses_double_between_attempts(world):
    fake = world([FakeResponse(status_code=503)] * 3 + [FakeResponse(chat("0"))])
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert fake.sleeps == [0.5, 1.0, 2.0]


@pytest.mark.parametrize("header, expected", [("3", 3.0), ("9999", 10.0)])
def test_retry_after_is_honoured_up_to_ten_seconds(world, header, expected):
    fake = world([FakeResponse(status_code=429, headers={"Retry-After": header}), FakeResponse(chat("0"))])
    entity_linking.choose_candidate(TERM, DEFINITION)
    assert fake.sleeps == [expected]


@pytest.mark.parametrize("header", ["Wed, 21 Oct 2026 07:28:00 GMT", "٣", "-3"])
def test_a_retry_after_that_is_not_plain_seconds_falls_back_to_the_backoff(world, header):
    fake = world([FakeResponse(status_code=503, headers={"Retry-After": header}), FakeResponse(chat("0"))])
    entity_linking.choose_candidate(TERM, DEFINITION)
    assert fake.sleeps == [0.5]


def test_an_unreachable_service_aborts_after_four_attempts_and_logs_no_address(world, caplog):
    fake = world([requests.ConnectionError("no route to 10.0.0.1")])
    with caplog.at_level(logging.WARNING, logger=entity_linking.logger.name):
        with pytest.raises(RuntimeError, match="did not answer"):
            entity_linking.choose_candidate(TERM, DEFINITION)
    assert len(fake.post_calls) == 4 and fake.sleeps == [0.5, 1.0, 2.0]
    assert "10.0.0.1" not in caplog.text and "ConnectionError" in caplog.text


def test_persistent_server_errors_abort_as_well(world):
    fake = world([FakeResponse(status_code=500)])
    with pytest.raises(RuntimeError):
        entity_linking.choose_candidate(TERM, DEFINITION)
    assert len(fake.post_calls) == 4


def test_a_model_that_answered_once_is_not_treated_as_unreachable(world):
    """Answered (invalidly) at least once, then nothing: the label gets no link, linking carries on."""
    fake = world([requests.Timeout(), FakeResponse(chat("no")), requests.Timeout(), requests.Timeout()])
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert len(fake.post_calls) == 4


@pytest.mark.parametrize("status", [401, 422])
def test_a_refusal_is_not_retried_and_aborts_linking(world, status):
    fake = world([FakeResponse(status_code=status)])
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        entity_linking.choose_candidate(TERM, DEFINITION)
    assert len(fake.post_calls) == 1 and fake.sleeps == []


def test_a_missing_key_aborts_before_any_request(world, monkeypatch):
    fake = world([FakeResponse(chat("0"))])
    monkeypatch.setattr(settings, "psnc_api_key", None)
    with pytest.raises(RuntimeError, match="PSNC_API_KEY"):
        entity_linking.choose_candidate(TERM, DEFINITION)
    assert fake.post_calls == []


# --- what is searched and what is asked ---------------------------------------------------------------------------- #

def test_a_term_nothing_offers_a_candidate_for_is_not_put_to_the_model(world):
    fake = world([FakeResponse(chat("1"))], gets=empty_gets())
    assert entity_linking.choose_candidate("zzyzx", DEFINITION) is None
    assert len(fake.get_calls) == 3 and fake.post_calls == []


def test_the_model_chooses_among_what_remains_when_a_source_failed(world):
    gets = real_gets()
    gets["wikidata.org"] = requests.Timeout()
    fake = world([FakeResponse(chat(label_of("om:Depth", skip=("wikidata",))))], gets=gets)
    assert entity_linking.choose_candidate(TERM, DEFINITION).key == "om:Depth"
    assert len(option_lines(fake.post_calls[0]["json"]["messages"][1]["content"])) == 3


def test_every_source_failing_means_no_link_and_no_question(world):
    gets = {"wikidata.org": requests.Timeout(), "qudt.org": FakeResponse(status_code=500, content=b""),
            "ebi.ac.uk": FakeResponse(content=b"not json")}
    fake = world([FakeResponse(chat("1"))], gets=gets)
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert fake.post_calls == []


def test_a_blank_label_is_neither_searched_nor_asked_about(world):
    fake = world([FakeResponse(chat("1"))])
    assert entity_linking.choose_candidate(" \n\t", DEFINITION) is None
    assert fake.get_calls == [] and fake.post_calls == []


def test_a_label_with_no_word_skips_only_the_qudt_search(world):
    fake = world([FakeResponse(chat("0"))], gets=empty_gets())
    entity_linking.choose_candidate("–", DEFINITION)
    assert len(fake.get_calls) == 2 and not any("qudt.org" in url for url in fake.get_calls)


def test_a_label_up_to_the_cap_is_searched_and_a_longer_one_is_neither_searched_nor_logged(world, caplog):
    fake = world([FakeResponse(chat("0"))], gets=empty_gets())
    entity_linking.choose_candidate("a" * entity_linking.MAX_TERM_CHARS, DEFINITION)
    assert len(fake.get_calls) == 3
    fake = world([FakeResponse(chat("1"))])
    with caplog.at_level(logging.WARNING, logger=entity_linking.logger.name):
        assert entity_linking.choose_candidate("water " * 50, DEFINITION) is None
    assert fake.get_calls == [] and fake.post_calls == []
    assert "water" not in caplog.text


def test_logs_never_carry_the_label_the_definition_or_the_key(world, caplog):
    fake = world([requests.ConnectionError("boom"), FakeResponse(chat("no")), FakeResponse(chat("no")),
                  FakeResponse(chat("no"))])
    with caplog.at_level(logging.DEBUG):
        entity_linking.choose_candidate("distinctivelabel", "distinctive definition text")
    assert fake.post_calls
    assert "distinctivelabel" not in caplog.text and "distinctive definition" not in caplog.text
    assert "test-key" not in caplog.text


# --- the model that answers: the decomposition's (D-038) ----------------------------------------------------------- #

OPENROUTER_RUN = entity_linking.Linker("openrouter", "qwen/qwen3.5-flash-02-23")


def test_an_openrouter_run_asks_openrouter_with_its_key_and_model(world, monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    fake = world([FakeResponse(chat(label_of("om:Depth"), reasoning=None))])
    assert entity_linking.choose_candidate(TERM, DEFINITION, OPENROUTER_RUN).key == "om:Depth"
    (call,) = fake.post_calls
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer test-openrouter-key"
    assert call["json"]["model"] == "qwen/qwen3.5-flash-02-23"
    assert call["json"]["reasoning"] == {"effort": "none"} and "chat_template_kwargs" not in call["json"]


def test_a_psnc_run_on_another_model_asks_that_model(world):
    fake = world([FakeResponse(chat("0"))])
    entity_linking.choose_candidate(TERM, DEFINITION, entity_linking.Linker("psnc", "Qwen3.5-397B-A17B"))
    (call,) = fake.post_calls
    assert call["json"]["model"] == "Qwen3.5-397B-A17B"
    assert call["url"].endswith("/v1/chat/completions") and "openrouter" not in call["url"]


def test_an_openrouter_refusal_aborts_linking_and_names_openrouter(world, monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-openrouter-key")
    fake = world([FakeResponse(status_code=401)])
    with pytest.raises(RuntimeError, match=r"^OpenRouter refused the entity-linking request \(HTTP 401\)\.$"):
        entity_linking.choose_candidate(TERM, DEFINITION, OPENROUTER_RUN)
    assert len(fake.post_calls) == 1


def test_an_openrouter_run_without_a_key_asks_nobody(world, monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    fake = world([FakeResponse(chat("0"))])
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
        entity_linking.choose_candidate(TERM, DEFINITION, OPENROUTER_RUN)
    assert fake.post_calls == []


def test_a_provider_linking_cannot_ask_aborts_before_any_request(world):
    fake = world([FakeResponse(chat("0"))])
    with pytest.raises(RuntimeError, match="cannot ask"):
        entity_linking.choose_candidate(TERM, DEFINITION, entity_linking.Linker("elsewhere", "m"))
    assert fake.post_calls == []
