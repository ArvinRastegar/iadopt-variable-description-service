"""Tests for the vocabulary searches: requests, strict parsing, the IRI gate and pooling.

Contract: iadopt-variable-description-service-docs/components/entity-linking-llm.md — D-032.

The requests are compared with the ones the sibling `iadopt-linking` project measured, and the
parsers are run on three real responses recorded by that project (fixtures/PROVENANCE.md).
Fully offline: the HTTP session is replaced everywhere.
"""

from __future__ import annotations

import hashlib
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
# SHA-256 of the response bodies as the sibling stored them; guards the fixtures against reformatting.
RECORDED_HASHES = {
    "wikidata": "56590959945f1bb5c71ac0bf33202032a59abe7c4c54666c4929a8fd17af66f7",
    "qudt": "04a2731983e23a265a5a82e6f40d04349f3404d57ee6572193fc1014db49319a",
    "om": "c018fe9031bd5f17366e4796880293fe70c432be4246dbe002bf0e38620d36a1",
}
KEY = "soil moisture of the topsoil"


def real(source):
    return (VOCAB / f"depth.{source}.json").read_text(encoding="utf-8")


QUDT_IRI = "http://qudt.org/vocab/quantitykind/Depth"
OM_IRI = "http://www.ontology-of-units-of-measure.org/resource/om-2/Depth"


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


# --- requests ---------------------------------------------------------------- #

@pytest.mark.parametrize("source", ["wikidata", "qudt", "om"])
def test_requests_match_the_measured_ones(source):
    request = v.search_request(source, KEY)
    golden = REQUESTS_GOLDEN[source]
    assert (request.url, request.params, request.headers) == (golden["url"], golden["params"], golden["headers"])


def test_only_qudt_can_decline_to_search():
    assert v.search_request("qudt", "of the") is None
    assert v.search_request("wikidata", "of the") is not None
    assert v.search_request("om", "of the") is not None
    assert REQUESTS_GOLDEN["_no_searchable_word"] == {"wikidata": "request", "qudt": None, "om": "request"}


def test_each_source_asks_for_at_most_ten_candidates():
    assert v.RESULT_LIMIT == REQUESTS_GOLDEN["_result_limit"] == 10
    assert v.search_request("wikidata", KEY).params["limit"] == "10"
    assert v.search_request("om", KEY).params["rows"] == "10"
    assert "LIMIT 10" in v.search_request("qudt", KEY).params["query"]


def test_qudt_query_carries_nothing_from_the_key_but_its_words():
    hostile = 'soil" ) } UNION { ?s ?p ?o } # moisture\\ \'x\' <script> \n ; DROP'
    query = v.search_request("qudt", hostile).params["query"]
    literals = re.findall(r'CONTAINS\(LCASE\(STR\(\?l\)\), "([^"]*)"\)', query)
    assert literals and all(re.fullmatch(r"[^\W_]+", word) for word in literals)
    assert literals == ["soil", "union", "s", "p", "o", "moisture", "x", "script", "drop"]
    assert query.count("FILTER(CONTAINS") == len(literals)


def test_qudt_query_drops_stop_words():
    query = v.search_request("qudt", "amount of the substance and a concentration").params["query"]
    assert re.findall(r'CONTAINS\(LCASE\(STR\(\?l\)\), "([^"]*)"\)', query) == ["amount", "substance", "concentration"]


# --- real responses ---------------------------------------------------------- #

@pytest.mark.parametrize("source", ["wikidata", "qudt", "om"])
def test_fixtures_are_the_recorded_bytes(source):
    assert hashlib.sha256(real(source).encode("utf-8")).hexdigest() == RECORDED_HASHES[source]


@pytest.mark.parametrize("source", ["wikidata", "qudt", "om"])
def test_real_responses_parse_to_the_candidates_the_sibling_read(source):
    parsed = v.parse_response(source, real(source))
    assert [(c.iri, c.label, c.description) for c in parsed] == [
        (c["iri"], c["label"], c["description"]) for c in EXPECTED[source]
    ]
    assert all(c.source == source for c in parsed)


def test_real_responses_cover_the_ten_candidate_limit():
    assert [len(v.parse_response(s, real(s))) for s in v.SOURCES] == [10, 2, 1]


def test_candidate_keys_name_vocabulary_and_local_id():
    keys = [c.key for s in v.SOURCES for c in v.parse_response(s, real(s))]
    assert keys[0].startswith("wikidata:Q") and "qudt:Depth" in keys and "om:Depth" in keys
    assert len(set(keys)) == len(keys)


def test_wikidata_iri_is_rebuilt_from_the_item_id():
    text = wikidata_body([{"id": "Q42", "concepturi": "https://elsewhere.example/Q42", "label": "l", "description": "d"}])
    (candidate,) = v.parse_response("wikidata", text)
    assert (candidate.iri, candidate.local_id) == ("http://www.wikidata.org/entity/Q42", "Q42")


def test_a_wikidata_item_may_lack_label_and_description():
    (candidate,) = v.parse_response("wikidata", wikidata_body([{"id": "Q7"}]))
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


def test_om_description_is_the_first_non_empty_one_in_single_spaces():
    (candidate,) = v.parse_response("om", om_body([om_doc(description=["", " A  measure\nof depth "])]))
    assert candidate.description == "A measure of depth"


# --- strictness -------------------------------------------------------------- #

def test_ten_candidates_are_accepted_and_eleven_are_refused():
    ten = [{"id": f"Q{n}", "label": "l"} for n in range(10)]
    assert len(v.parse_response("wikidata", wikidata_body(ten))) == 10
    with pytest.raises(v.VocabularyError):
        v.parse_response("wikidata", wikidata_body(ten + [{"id": "Q99"}]))


MALFORMED = [
    ("wikidata", "not json"),
    ("wikidata", "[]"),
    ("wikidata", json.dumps({"error": {"code": "x"}, "search": []})),
    ("wikidata", json.dumps({"success": 1})),
    ("wikidata", wikidata_body([], success=0)),
    ("wikidata", wikidata_body({"id": "Q1"})),
    ("wikidata", wikidata_body(["Q1"])),
    ("wikidata", wikidata_body([{"id": "P31"}])),
    ("wikidata", wikidata_body([{"id": "Q"}])),
    ("wikidata", wikidata_body([{"id": "Q1x"}])),
    ("wikidata", wikidata_body([{"id": 1}])),
    ("wikidata", wikidata_body([{"id": "Q1", "label": 5}])),
    ("wikidata", wikidata_body([{"id": "Q1", "description": None}])),
    ("qudt", "not json"),
    ("qudt", "{}"),
    ("qudt", json.dumps({"results": {"bindings": {}}})),
    ("qudt", json.dumps({"results": {"bindings": [{"qk": {"value": QUDT_IRI}}]}})),
    ("qudt", qudt_body([qudt_row(iri="https://qudt.org/vocab/quantitykind/Depth")])),
    ("qudt", qudt_body([qudt_row(iri="http://qudt.org/vocab/quantitykind/De pth")])),
    ("qudt", qudt_body([qudt_row(iri="http://qudt.org/vocab/quantitykind/Depth>")])),
    ("qudt", qudt_body([qudt_row(iri="http://qudt.org/vocab/unit/M")])),
    ("qudt", qudt_body([qudt_row(iri=f"http://qudt.org/vocab/quantitykind/K{n}") for n in range(11)])),
    ("qudt", qudt_body([qudt_row(length="x")])),
    ("qudt", qudt_tweaked(("label", "value"), 5)),
    ("qudt", qudt_tweaked(("label", "value"), None)),
    ("qudt", qudt_tweaked(("label", "value"), True)),
    ("qudt", qudt_tweaked(("label", "value"), ["Depth"])),
    ("qudt", qudt_tweaked(("label", "xml:lang"), 5)),
    ("qudt", qudt_tweaked(("description", "value"), 5)),
    ("qudt", qudt_tweaked(("description", "value"), ["d"])),
    ("qudt", qudt_tweaked(("len", "value"), float("inf"))),
    ("qudt", qudt_tweaked(("len", "value"), None)),
    ("qudt", qudt_tweaked(("qk", "value"), [QUDT_IRI])),
    ("qudt", qudt_tweaked(("qk", "value"), 7)),
    ("om", "not json"),
    ("om", "{}"),
    ("om", om_body([om_doc()], status=1)),
    ("om", om_body([om_doc()], found=2)),
    ("om", om_body([om_doc(), om_doc(iri=OM_IRI + "2")], found=1)),
    ("om", om_body([om_doc(ontology="qudt")])),
    ("om", om_body([om_doc(iri="")])),
    ("om", om_body([om_doc(iri="http://www.ontology-of-units-of-measure.org/resource/om-2/")])),
    ("om", om_body([om_doc(iri="https://www.ontology-of-units-of-measure.org/resource/om-2/Depth")])),
    ("om", om_body([om_doc(iri="http://www.wikidata.org/entity/Q1")])),
    ("om", om_body([om_doc(label="")])),
    ("om", om_body([om_doc(label=5)])),
    ("om", om_body([om_doc(description="not a list")])),
    ("om", om_body([om_doc(description=[1])])),
    ("om", om_body([om_doc(iri=f"{OM_IRI}{n}") for n in range(11)], found=11)),
]


@pytest.mark.parametrize("source, text", MALFORMED)
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


# --- the IRI gate ------------------------------------------------------------ #

@pytest.mark.parametrize("iri", [QUDT_IRI, OM_IRI, "http://qudt.org/vocab/quantitykind/Mass-Flow_Rate.v2"])
def test_a_canonical_vocabulary_iri_passes_unchanged(iri):
    assert v.quantity_vocabulary_iri(iri) == iri


@pytest.mark.parametrize(
    "iri",
    [
        None, 5, "", " ", "https://qudt.org/vocab/quantitykind/Depth", "http://qudt.org/vocab/quantitykind/",
        "http://qudt.org/vocab/quantitykind/Depth/", "http://qudt.org/vocab/quantitykind/Depth>",
        "http://qudt.org/vocab/quantitykind/Depth> a <x", "http://qudt.org/vocab/quantitykind/De pth",
        "http://qudt.org/vocab/quantitykind/Depth\n", "<http://qudt.org/vocab/quantitykind/Depth>",
        "http://qudt.org/vocab/quantitykind/Dep\"th", "http://qudt.org/vocab/unit/M",
        "http://evil.example/qudt.org/vocab/quantitykind/Depth", "http://qudt.org.evil.example/vocab/quantitykind/Depth",
        "https://www.ontology-of-units-of-measure.org/resource/om-2/Depth",
        "http://www.ontology-of-units-of-measure.org/resource/om-2/", "http://www.wikidata.org/entity/Q42",
        "https://www.wikidata.org/wiki/Q42", "javascript:alert(1)",
    ],
)
def test_anything_else_is_kept_out_of_turtle(iri):
    assert v.quantity_vocabulary_iri(iri) is None


# --- searching and pooling --------------------------------------------------- #

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


def test_search_source_sends_the_measured_request_with_a_timeout(session):
    fake = session(all_real())
    candidates = v.search_source("om", KEY)
    assert [c.key for c in candidates] == ["om:Depth"]
    (call,) = fake.calls
    assert call["url"] == REQUESTS_GOLDEN["om"]["url"] and call["params"] == REQUESTS_GOLDEN["om"]["params"]
    assert call["headers"] == REQUESTS_GOLDEN["om"]["headers"] and call["timeout"] == v.REQUEST_TIMEOUT_S


def test_a_key_qudt_cannot_search_makes_no_request(session):
    fake = session(all_real())
    assert v.search_source("qudt", "of the") == []
    assert fake.calls == []


@pytest.mark.parametrize("status", [400, 403, 429, 500, 503])
def test_a_status_other_than_200_is_a_failure_not_an_empty_answer(session, status):
    session({"wikidata.org": FakeResponse(b"{}", status_code=status)})
    with pytest.raises(v.VocabularyError, match=f"HTTP {status}"):
        v.search_source("wikidata", "depth")


@pytest.mark.parametrize("error", [requests.Timeout("slow"), requests.ConnectionError("down")])
def test_a_transport_error_is_a_failure_named_by_class_only(session, error):
    session({"wikidata.org": error})
    with pytest.raises(v.VocabularyError) as caught:
        v.search_source("wikidata", "depth")
    assert type(error).__name__ in str(caught.value)
    assert "slow" not in str(caught.value) and "down" not in str(caught.value)


def test_a_body_that_is_not_utf8_is_a_failure(session):
    session({"wikidata.org": FakeResponse(b"\xff\xfe\x00")})
    with pytest.raises(v.VocabularyError, match="UTF-8"):
        v.search_source("wikidata", "depth")


def test_non_ascii_labels_survive_decoding(session):
    body = wikidata_body([{"id": "Q1", "label": "µm – α-diversity", "description": "d"}])
    session({"wikidata.org": FakeResponse(body.encode("utf-8"))})
    (candidate,) = v.search_source("wikidata", "x")
    assert candidate.label == "µm – α-diversity"


def test_pool_holds_every_source_in_pooling_order(session):
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


def test_a_source_that_breaks_the_format_is_a_failed_source_not_a_failed_decomposition(session, caplog):
    """A QUDT row with a label that is not text, or a length of Infinity, must not escape as another exception."""
    for broken in (qudt_tweaked(("label", "value"), 5), qudt_tweaked(("len", "value"), float("inf"))):
        session({**all_real(), "qudt.org": FakeResponse(broken)})
        with caplog.at_level(logging.WARNING, logger=v.logger.name):
            pool, failed = v.retrieve_pool("depth")
        assert failed == ["qudt"]
        assert [c.source for c in pool] == ["wikidata"] * 10 + ["om"]


def test_all_sources_failing_gives_an_empty_pool_and_three_names(session):
    session({"wikidata.org": requests.Timeout(), "qudt.org": FakeResponse(b"x", 500), "ebi.ac.uk": FakeResponse(b"{}")})
    pool, failed = v.retrieve_pool("depth")
    assert pool == [] and failed == ["wikidata", "qudt", "om"]


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
