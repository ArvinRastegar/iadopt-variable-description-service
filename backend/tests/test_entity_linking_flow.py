"""Tests for choosing a candidate: searching, asking the model, and asking again.

Contract: iadopt-variable-description-service-docs/components/entity-linking-llm.md — D-032.

The model cannot be restricted to answer in exactly the right form, so an answer is checked
strictly and the identical request is sent again, up to four attempts in all. These tests pin
which failures are retried, which abort, and what a label ends up with when the attempts run out.

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


# --- a valid answer ------------------------------------------------------------- #

@pytest.mark.parametrize("candidate_key", ["om:Depth", "qudt:Depth"])
def test_qudt_and_om_candidates_can_be_chosen(world, candidate_key):
    fake = world([FakeResponse(chat(label_of(candidate_key)))])
    chosen = entity_linking.choose_candidate(TERM, DEFINITION)
    assert chosen.key == candidate_key
    assert chosen.iri == {"om:Depth": "http://www.ontology-of-units-of-measure.org/resource/om-2/Depth",
                          "qudt:Depth": "http://qudt.org/vocab/quantitykind/Depth"}[candidate_key]
    assert len(fake.post_calls) == 1 and fake.sleeps == []


def test_a_wikidata_item_can_be_chosen(world):
    first_wikidata = next(c for c in shown_for() if c.source == "wikidata")
    world([FakeResponse(chat(label_of(first_wikidata.key)))])
    chosen = entity_linking.choose_candidate(TERM, DEFINITION)
    assert chosen.source == "wikidata" and chosen.iri.startswith("http://www.wikidata.org/entity/Q")


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
    assert set(body) == {"model", "messages", "stream", "temperature", "max_tokens", "chat_template_kwargs"}
    assert body["model"] == "Qwen3.8-27B" and body["temperature"] == 0 and body["max_tokens"] == 1
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    user = body["messages"][1]["content"]
    assert user.startswith(f"term: Depth\nvariable_definition: {DEFINITION}\n\ncandidates:\n")
    assert len(option_lines(user)) == 13
    assert user.count("\n0. none of these: no candidate is the same concept as the term\n") == 1
    assert user.endswith("\n\nAnswer with one label.")


def test_candidates_are_shown_in_the_seeded_order_not_source_order(world):
    fake = world([FakeResponse(chat("0"))])
    entity_linking.choose_candidate(TERM, DEFINITION)
    shown = option_lines(fake.post_calls[0]["json"]["messages"][1]["content"])
    assert shown == [entity_choice.shown_candidate(c) for c in shown_for()]
    by_source = sorted(shown_for(), key=lambda c: vocabularies.SOURCES.index(c.source))
    assert shown != [entity_choice.shown_candidate(c) for c in by_source]


# --- asking again ---------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["", "Option 3", "1.", "a", "ZZ", "**1**", "The answer is 1"])
def test_an_invalid_answer_is_asked_again_with_the_identical_request(world, bad):
    fake = world([FakeResponse(chat(bad)), FakeResponse(chat(label_of("om:Depth")))])
    assert entity_linking.choose_candidate(TERM, DEFINITION).key == "om:Depth"
    assert len(fake.post_calls) == 2
    assert fake.post_calls[0]["json"] == fake.post_calls[1]["json"]
    assert fake.sleeps == []


def test_three_retries_make_four_attempts_and_then_the_label_has_no_link(world, caplog):
    fake = world([FakeResponse(chat("no"))])
    with caplog.at_level(logging.WARNING, logger=entity_linking.logger.name):
        assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert len(fake.post_calls) == entity_linking.MAX_ATTEMPTS == 4
    assert all(call["json"] == fake.post_calls[0]["json"] for call in fake.post_calls)
    assert fake.sleeps == []
    assert caplog.text.count("gave no valid answer") == 4


def test_every_attempt_sends_the_same_bytes_whatever_failed_before(world):
    fake = world([FakeResponse(status_code=503), requests.Timeout(), FakeResponse(chat("no")), FakeResponse(chat("0"))])
    entity_linking.choose_candidate(TERM, DEFINITION)
    sent = {json.dumps(call["json"], sort_keys=True) for call in fake.post_calls}
    assert len(fake.post_calls) == 4 and len(sent) == 1


def test_the_fourth_attempt_can_still_succeed(world):
    fake = world([FakeResponse(chat("no"))] * 3 + [FakeResponse(chat(label_of("om:Depth")))])
    assert entity_linking.choose_candidate(TERM, DEFINITION).key == "om:Depth"
    assert len(fake.post_calls) == 4


def test_reasoning_text_makes_an_answer_invalid(world):
    fake = world([FakeResponse(chat(label_of("om:Depth"), reasoning_content="Let me think."))])
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert len(fake.post_calls) == 4


def test_a_label_beyond_the_offered_ones_is_invalid(world):
    fake = world([FakeResponse(chat("Z"))])
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert len(fake.post_calls) == 4


def test_a_200_that_is_not_json_is_an_invalid_answer(world):
    fake = world([FakeResponse(not_json=True, content=b"<html>proxy page</html>")])
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert len(fake.post_calls) == 4


# --- transient failures ------------------------------------------------------------ #

@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503])
def test_a_transient_status_is_retried_after_a_pause(world, status):
    fake = world([FakeResponse(status_code=status), FakeResponse(chat(label_of("om:Depth")))])
    assert entity_linking.choose_candidate(TERM, DEFINITION).key == "om:Depth"
    assert len(fake.post_calls) == 2 and fake.sleeps == [0.5]


def test_a_timeout_is_retried(world):
    fake = world([requests.Timeout("slow"), FakeResponse(chat(label_of("om:Depth")))])
    assert entity_linking.choose_candidate(TERM, DEFINITION).key == "om:Depth"
    assert len(fake.post_calls) == 2


def test_pauses_grow_and_are_capped(world):
    fake = world([FakeResponse(status_code=503)] * 3 + [FakeResponse(chat("0"))])
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert fake.sleeps == [0.5, 1.0, 2.0]
    assert [entity_linking._pause_after(n, None) for n in (1, 2, 3, 4, 5, 9)] == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0]


@pytest.mark.parametrize("header, expected", [("3", 3.0), ("0", 0.0), ("9999", 10.0)])
def test_retry_after_is_honoured_up_to_a_cap(world, header, expected):
    fake = world([FakeResponse(status_code=429, headers={"Retry-After": header}), FakeResponse(chat("0"))])
    entity_linking.choose_candidate(TERM, DEFINITION)
    assert fake.sleeps == [expected]
    assert entity_linking.RETRY_AFTER_CAP_S == 10.0


@pytest.mark.parametrize("header", ["Wed, 21 Oct 2026 07:28:00 GMT", "\u00b2", "\u0663", "-3", "1.5", ""])
def test_a_retry_after_that_is_not_plain_seconds_falls_back_to_the_backoff(world, header):
    fake = world([FakeResponse(status_code=503, headers={"Retry-After": header}),
                  FakeResponse(chat("0"))])
    entity_linking.choose_candidate(TERM, DEFINITION)
    assert fake.sleeps == [0.5]


def test_an_unreachable_service_aborts_after_four_attempts(world, caplog):
    fake = world([requests.ConnectionError("no route to 10.0.0.1")])
    with caplog.at_level(logging.WARNING, logger=entity_linking.logger.name):
        with pytest.raises(RuntimeError, match="did not answer"):
            entity_linking.choose_candidate(TERM, DEFINITION)
    assert len(fake.post_calls) == 4 and fake.sleeps == [0.5, 1.0, 2.0]
    assert "no route" not in caplog.text and "10.0.0.1" not in caplog.text
    assert "ConnectionError" in caplog.text


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


# --- failures that are not retried -------------------------------------------------- #

@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
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


# --- what is searched and what is not ------------------------------------------------- #

def test_a_term_nothing_offers_a_candidate_for_is_not_put_to_the_model(world):
    fake = world([FakeResponse(chat("1"))], gets=empty_gets())
    assert entity_linking.choose_candidate("zzyzx", DEFINITION) is None
    assert len(fake.get_calls) == 3 and fake.post_calls == []


def test_a_source_that_breaks_the_format_costs_only_that_source(world):
    gets = real_gets()
    gets["qudt.org"] = FakeResponse(body={"results": {"bindings": [
        {"qk": {"value": "http://qudt.org/vocab/quantitykind/Depth"}, "len": {"value": 5},
         "label": {"xml:lang": "en", "value": 5}}]}})
    fake = world([FakeResponse(chat("0"))], gets=gets)
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert len(option_lines(fake.post_calls[0]["json"]["messages"][1]["content"])) == 11


def test_a_failed_source_does_not_stop_the_others_being_offered(world):
    gets = real_gets()
    gets["qudt.org"] = FakeResponse(content=b"oops", status_code=503)
    fake = world([FakeResponse(chat("0"))], gets=gets)
    assert entity_linking.choose_candidate(TERM, DEFINITION) is None
    assert len(option_lines(fake.post_calls[0]["json"]["messages"][1]["content"])) == 11


def test_the_model_can_choose_among_what_remains_when_wikidata_failed(world):
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


@pytest.mark.parametrize("term", ["", "   ", "\n\t"])
def test_a_blank_label_is_neither_searched_nor_asked_about(world, term):
    fake = world([FakeResponse(chat("1"))])
    assert entity_linking.choose_candidate(term, DEFINITION) is None
    assert fake.get_calls == [] and fake.post_calls == []


def test_a_label_with_no_word_skips_only_the_qudt_search(world):
    fake = world([FakeResponse(chat("0"))], gets=empty_gets())
    entity_linking.choose_candidate("–", DEFINITION)
    assert len(fake.get_calls) == 2 and not any("qudt.org" in url for url in fake.get_calls)


def test_a_label_longer_than_the_cap_is_not_searched(world, caplog):
    fake = world([FakeResponse(chat("1"))])
    long_label = "water " * 50
    with caplog.at_level(logging.WARNING, logger=entity_linking.logger.name):
        assert entity_linking.choose_candidate(long_label, DEFINITION) is None
    assert fake.get_calls == [] and fake.post_calls == []
    assert "water" not in caplog.text


def test_a_label_at_the_cap_is_searched(world):
    fake = world([FakeResponse(chat("0"))], gets=empty_gets())
    entity_linking.choose_candidate("a" * entity_linking.MAX_TERM_CHARS, DEFINITION)
    assert len(fake.get_calls) == 3


def test_logs_never_carry_the_label_or_the_definition(world, caplog):
    fake = world([requests.ConnectionError("boom"), FakeResponse(chat("no")), FakeResponse(chat("no")),
                  FakeResponse(chat("no"))])
    with caplog.at_level(logging.DEBUG):
        entity_linking.choose_candidate("distinctivelabel", "distinctive definition text")
    assert fake.post_calls
    assert "distinctivelabel" not in caplog.text and "distinctive definition" not in caplog.text
    assert "test-key" not in caplog.text
