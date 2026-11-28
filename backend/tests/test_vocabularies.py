"""The vocabulary searches: measured requests, strict parsing, the IRI gate and pooling.

The requests are compared with the ones the sibling ``iadopt-linking`` project measured, and the parsers are run on
three real responses recorded by that project (fixtures/PROVENANCE.md). Fully offline: the HTTP session is replaced.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import threading

import pytest
import requests

from app.services import vocabularies as v

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
VOCAB = FIXTURES / "vocabularies"
REQUESTS_GOLDEN = json.loads((FIXTURES / "vocabulary_requests_golden.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((VOCAB / "depth.expected.json").read_text(encoding="utf-8"))
KEY = "soil moisture of the topsoil"

QUDT_IRI = "http://qudt.org/vocab/quantitykind/Depth"
OM_IRI = "http://www.ontology-of-units-of-measure.org/resource/om-2/Depth"


def real(source):
    return (VOCAB / f"depth.{source}.json").read_text(encoding="utf-8")


def wikidata_body(items, success=1):
    return json.dumps({"searchinfo": {"search": "x"}, "search": items, "success": success})


def qudt_body(rows):
    return json.dumps({"head": {"vars": []}, "results": {"bindings": rows}})


def qudt_row(iri=QUDT_IRI, label="Depth", length=5, lang="en", description=None):
    row = {"qk": {"type": "uri", "value": iri}, "len": {"type": "literal", "value": str(length)},
           "label": {"type": "literal", "xml:lang": lang, "value": label}}
    if description is not None:
        row["description"] = {"type": "literal", "value": description}
    return row


def qudt_tweaked(path, value, description="d"):
    """A QUDT body whose one row has the value at `path` replaced, however wrong that makes it."""
    row = qudt_row(description=description)
    target = row
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return qudt_body([row])


def om_body(docs, found=None, status=0):
    return json.dumps({"responseHeader": {"status": status},
                       "response": {"numFound": len(docs) if found is None else found, "docs": docs}})


def om_doc(iri=OM_IRI, label="depth", description=None, ontology="om"):
    doc = {"iri": iri, "label": label, "ontology_name": ontology}
    if description is not None:
        doc["description"] = description
    return doc


# --- requests ------------------------------------------------------------------------------------------------------ #

@pytest.mark.parametrize("source", ["wikidata", "qudt", "om"])
def test_requests_match_the_measured_ones(source):
    request = v.search_request(source, KEY)
    golden = REQUESTS_GOLDEN[source]
    assert (request.url, request.params, request.headers) == (golden["url"], golden["params"], golden["headers"])


def test_the_qudt_query_carries_nothing_from_the_key_but_its_words():
    hostile = 'soil" ) } UNION { ?s ?p ?o } # moisture\\ \'x\' <script> \n ; DROP'
    query = v.search_request("qudt", hostile).params["query"]
    literals = re.findall(r'CONTAINS\(LCASE\(STR\(\?l\)\), "([^"]*)"\)', query)
    assert literals == ["soil", "union", "s", "p", "o", "moisture", "x", "script", "drop"]
    assert query.count("FILTER(CONTAINS") == len(literals)


# --- parsing ------------------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("source", ["wikidata", "qudt", "om"])
def test_real_responses_parse_to_the_candidates_the_sibling_read(source):
    parsed = v.parse_response(source, real(source))
    assert [(c.iri, c.label, c.description) for c in parsed] == [
        (c["iri"], c["label"], c["description"]) for c in EXPECTED[source]
    ]
    assert all(c.source == source for c in parsed)


def test_a_wikidata_iri_is_rebuilt_from_the_item_id_and_label_and_description_may_be_missing():
    text = wikidata_body([{"id": "Q42", "concepturi": "https://elsewhere.example/Q42"}])
    (candidate,) = v.parse_response("wikidata", text)
    assert (candidate.iri, candidate.local_id) == ("http://www.wikidata.org/entity/Q42", "Q42")
    assert (candidate.label, candidate.description) == ("", "")


def test_empty_results_are_a_genuine_nothing_found():
    assert v.parse_response("wikidata", wikidata_body([])) == []
    assert v.parse_response("qudt", qudt_body([])) == []
    assert v.parse_response("om", om_body([])) == []


def test_qudt_rows_of_one_kind_become_one_candidate_with_the_english_label():
    rows = [
        qudt_row(label="Depth", lang="en-US", description="  Distance\n below  a surface "),
        qudt_row(label="Depth", lang="en"),
        qudt_row(label="Profondeur", lang="fr"),
    ]
    (candidate,) = v.parse_response("qudt", qudt_body(rows))
    assert (candidate.label, candidate.description) == ("Depth", "Distance below a surface")


def test_qudt_orders_by_label_length_then_iri():
    rows = [qudt_row(iri=QUDT_IRI.replace("Depth", "Zed"), length=9), qudt_row(iri=QUDT_IRI.replace("Depth", "Abc"), length=9),
            qudt_row(iri=QUDT_IRI, length=5)]
    assert [c.local_id for c in v.parse_response("qudt", qudt_body(rows))] == ["Depth", "Abc", "Zed"]


def test_the_om_description_is_the_first_non_empty_one_in_single_spaces():
    (candidate,) = v.parse_response("om", om_body([om_doc(description=["", " A  measure\nof depth "])]))
    assert candidate.description == "A measure of depth"


def test_ten_candidates_are_accepted_and_eleven_are_refused():
    ten = [{"id": f"Q{n}", "label": "l"} for n in range(10)]
    assert len(v.parse_response("wikidata", wikidata_body(ten))) == 10
    with pytest.raises(v.VocabularyError):
        v.parse_response("wikidata", wikidata_body(ten + [{"id": "Q99"}]))


@pytest.mark.parametrize(
    "source, text",
    [
        ("wikidata", "not json"),
        ("wikidata", json.dumps({"error": {"code": "x"}, "search": []})),
        ("wikidata", wikidata_body([], success=0)),
        ("wikidata", wikidata_body([{"id": "P31"}])),
        ("wikidata", wikidata_body([{"id": "Q1", "label": 5}])),
        ("qudt", "{}"),
        ("qudt", qudt_body([qudt_row(iri="https://qudt.org/vocab/quantitykind/Depth")])),
        ("qudt", qudt_body([qudt_row(iri="http://qudt.org/vocab/quantitykind/De pth")])),
        ("qudt", qudt_body([qudt_row(iri="http://qudt.org/vocab/unit/M")])),
        ("qudt", qudt_tweaked(("label", "value"), 5)),
        ("qudt", qudt_tweaked(("len", "value"), float("inf"))),
        ("om", om_body([om_doc()], status=1)),
        ("om", om_body([om_doc()], found=2)),
        ("om", om_body([om_doc(ontology="qudt")])),
        ("om", om_body([om_doc(iri="https://www.ontology-of-units-of-measure.org/resource/om-2/Depth")])),
        ("om", om_body([om_doc(label="")])),
    ],
)
def test_a_response_of_the_wrong_shape_is_refused_not_read_as_empty(source, text):
    with pytest.raises(v.VocabularyError):
        v.parse_response(source, text)


def test_a_refusal_never_quotes_the_response():
    secret = "SECRET-RESPONSE-TEXT"
    for source, body in (("wikidata", json.dumps({"error": {"info": secret}, "search": []})),
                         ("qudt", json.dumps({"x": secret})), ("om", json.dumps({"x": secret}))):
        with pytest.raises(v.VocabularyError) as caught:
            v.parse_response(source, body)
        assert secret not in str(caught.value)


# --- the IRI gate -------------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("iri", [QUDT_IRI, OM_IRI, "http://qudt.org/vocab/quantitykind/Mass-Flow_Rate.v2"])
def test_a_canonical_vocabulary_iri_passes_unchanged(iri):
    assert v.quantity_vocabulary_iri(iri) == iri


@pytest.mark.parametrize(
    "iri",
    [
        None, "", "https://qudt.org/vocab/quantitykind/Depth", "http://qudt.org/vocab/quantitykind/",
        "http://qudt.org/vocab/quantitykind/Depth> a <x", "http://qudt.org/vocab/quantitykind/De pth",
        "http://qudt.org/vocab/quantitykind/Depth\n", "http://qudt.org/vocab/quantitykind/Dep\"th",
        "http://qudt.org/vocab/unit/M", "http://qudt.org.evil.example/vocab/quantitykind/Depth",
        "https://www.ontology-of-units-of-measure.org/resource/om-2/Depth", "http://www.wikidata.org/entity/Q42",
        "javascript:alert(1)",
    ],
)
def test_anything_else_is_kept_out_of_turtle(iri):
    assert v.quantity_vocabulary_iri(iri) is None


# --- searching and pooling ----------------------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, content=b"", status_code=200):
        self.content = content if isinstance(content, bytes) else content.encode("utf-8")
        self.status_code = status_code


class FakeSession:
    """Answers a GET by the host of its URL: a response, or an exception to raise."""

    def __init__(self, by_host, barrier=None):
        self.by_host = by_host
        self.barrier = barrier
        self.calls = []
        self._lock = threading.Lock()

    def get(self, url, params=None, headers=None, timeout=None):
        with self._lock:
            self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if self.barrier is not None:
            self.barrier.wait()  # returns only once every party is waiting at the same time
        outcome = next(r for host, r in self.by_host.items() if host in url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def all_real():
    return {"wikidata.org": FakeResponse(real("wikidata")), "qudt.org": FakeResponse(real("qudt")),
            "ebi.ac.uk": FakeResponse(real("om"))}


@pytest.fixture
def session(monkeypatch):
    def install(by_host, barrier=None):
        fake = FakeSession(by_host, barrier)
        monkeypatch.setattr(v, "get_http_session", lambda: fake)
        return fake
    return install


def test_a_search_sends_the_measured_request_with_a_timeout(session):
    fake = session(all_real())
    assert [c.key for c in v.search_source("om", KEY)] == ["om:Depth"]
    (call,) = fake.calls
    assert call["url"] == REQUESTS_GOLDEN["om"]["url"] and call["params"] == REQUESTS_GOLDEN["om"]["params"]
    assert call["headers"] == REQUESTS_GOLDEN["om"]["headers"] and call["timeout"] == v.REQUEST_TIMEOUT_S


def test_a_status_other_than_200_is_a_failure_not_an_empty_answer(session):
    session({"wikidata.org": FakeResponse(b"{}", status_code=503)})
    with pytest.raises(v.VocabularyError, match="HTTP 503"):
        v.search_source("wikidata", "depth")


def test_a_transport_error_is_a_failure_named_by_its_class_only(session):
    session({"wikidata.org": requests.ConnectionError("down at 10.0.0.1")})
    with pytest.raises(v.VocabularyError) as caught:
        v.search_source("wikidata", "depth")
    assert "ConnectionError" in str(caught.value) and "10.0.0.1" not in str(caught.value)


def test_a_body_that_is_not_utf8_is_a_failure_and_non_ascii_labels_survive(session):
    session({"wikidata.org": FakeResponse(b"\xff\xfe\x00")})
    with pytest.raises(v.VocabularyError, match="UTF-8"):
        v.search_source("wikidata", "depth")
    body = wikidata_body([{"id": "Q1", "label": "µm – α-diversity", "description": "d"}])
    session({"wikidata.org": FakeResponse(body.encode("utf-8"))})
    (candidate,) = v.search_source("wikidata", "x")
    assert candidate.label == "µm – α-diversity"


def test_the_pool_holds_every_source_in_pooling_order(session):
    fake = session(all_real())
    pool, failed = v.retrieve_pool("depth")
    assert failed == []
    assert [c.source for c in pool] == ["wikidata"] * 10 + ["qudt"] * 2 + ["om"]
    assert len(fake.calls) == 3


def test_the_three_searches_are_in_flight_at_the_same_time(session):
    """Each fake request waits for the other two; run one after another, the barrier would break."""
    session(all_real(), barrier=threading.Barrier(3, timeout=5))
    pool, failed = v.retrieve_pool("depth")
    assert failed == [] and len(pool) == 13


def test_a_failed_source_is_left_out_and_named_but_the_others_are_pooled(session, caplog):
    session({**all_real(), "qudt.org": FakeResponse(b"{}", status_code=503)})
    with caplog.at_level(logging.WARNING, logger=v.logger.name):
        pool, failed = v.retrieve_pool("a-very-particular-key")
    assert failed == ["qudt"]
    assert [c.source for c in pool] == ["wikidata"] * 10 + ["om"]
    assert "qudt" in caplog.text and "503" in caplog.text
    assert "a-very-particular-key" not in caplog.text


def test_a_source_that_breaks_the_format_is_a_failed_source_not_a_failed_decomposition(session):
    session({**all_real(), "qudt.org": FakeResponse(qudt_tweaked(("len", "value"), float("inf")))})
    pool, failed = v.retrieve_pool("depth")
    assert failed == ["qudt"]
    assert [c.source for c in pool] == ["wikidata"] * 10 + ["om"]


def test_all_sources_failing_gives_an_empty_pool_and_three_names(session):
    session({"wikidata.org": requests.Timeout(), "qudt.org": FakeResponse(b"x", 500), "ebi.ac.uk": FakeResponse(b"{}")})
    assert v.retrieve_pool("depth") == ([], ["wikidata", "qudt", "om"])


def test_sources_that_answer_with_nothing_are_not_failures(session):
    session({"wikidata.org": FakeResponse(wikidata_body([])), "qudt.org": FakeResponse(qudt_body([])),
             "ebi.ac.uk": FakeResponse(om_body([]))})
    assert v.retrieve_pool("depth") == ([], [])


def test_a_bug_in_a_search_is_not_hidden_as_a_failed_source(session, monkeypatch):
    session(all_real())

    def boom(source, key):
        raise ZeroDivisionError("a bug, not a vocabulary failure")

    monkeypatch.setattr(v, "search_source", boom)
    with pytest.raises(ZeroDivisionError):
        v.retrieve_pool("depth")
