"""Regression tests pinning existing Wikidata entity-linking behaviour.

Contract: docs/components/entity-linking.md — D-006.

These document what the module already does; they do not drive a change. Entity
linking was never covered by the experiments, so its steps stay exactly as they
are. What matters here is that it consumes the measured decomposition shapes —
in particular three-key asymmetric systems — without modification.

Fully stubbed: no test here makes a network call.
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from app.services import enrichment

BACKEND = pathlib.Path(__file__).resolve().parents[1]
DEMOS = json.loads((BACKEND / "data/measured/demonstrations-top25.json").read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture
def link_everything(monkeypatch):
    """Every lookup resolves to Q1 with a score above the threshold."""
    monkeypatch.setattr(
        enrichment, "get_http_session",
        lambda: type("S", (), {"get": staticmethod(lambda url, timeout=20: FakeResponse(
            {"search": [{"id": "Q1", "label": "hit", "description": "d"}]}))})(),
    )
    monkeypatch.setattr(enrichment, "call_psnc_reranker", lambda query, documents: [0.99] * len(documents))


@pytest.fixture
def link_nothing(monkeypatch):
    """Every lookup scores below the threshold."""
    monkeypatch.setattr(
        enrichment, "get_http_session",
        lambda: type("S", (), {"get": staticmethod(lambda url, timeout=20: FakeResponse(
            {"search": [{"id": "Q1", "label": "hit", "description": "d"}]}))})(),
    )
    monkeypatch.setattr(enrichment, "call_psnc_reranker", lambda query, documents: [0.0] * len(documents))


def pred(entity):
    return {"definition": "d", "hasProperty": "count", "hasObjectOfInterest": entity,
            "hasMatrix": "", "hasContextObject": "", "hasStatisticalModifier": "", "hasConstraint": []}


# --- the measured shapes ----------------------------------------------------- #

def test_numerator_denominator_links_its_three_present_keys(link_everything):
    out = enrichment.enrich_with_uris_reranker(
        pred({"AsymmetricSystem": "a / b", "hasNumerator": "a", "hasDenominator": "b"}))
    entity = out["hasObjectOfInterest"]
    assert entity["AsymmetricSystemURI"] == "https://www.wikidata.org/wiki/Q1"
    assert entity["hasNumeratorURI"] and entity["hasDenominatorURI"]
    assert "hasSourceURI" not in entity
    assert "hasTargetURI" not in entity


def test_source_target_links_its_three_present_keys(link_everything):
    out = enrichment.enrich_with_uris_reranker(
        pred({"AsymmetricSystem": "a → b", "hasSource": "a", "hasTarget": "b"}))
    entity = out["hasObjectOfInterest"]
    assert entity["hasSourceURI"] and entity["hasTargetURI"]
    assert "hasNumeratorURI" not in entity
    assert "hasDenominatorURI" not in entity


def test_symmetric_system_links_container_and_parts(link_everything):
    out = enrichment.enrich_with_uris_reranker(
        pred({"SymmetricSystem": "a + b", "hasPart": ["a", "b"]}))
    entity = out["hasObjectOfInterest"]
    assert entity["SymmetricSystemURI"]
    assert entity["hasPartURIs"] == ["https://www.wikidata.org/wiki/Q1"] * 2


def test_part_uris_absent_when_nothing_links(link_nothing):
    out = enrichment.enrich_with_uris_reranker(
        pred({"SymmetricSystem": "a + b", "hasPart": ["a", "b"]}))
    assert "hasPartURIs" not in out["hasObjectOfInterest"]


def test_all_measured_decompositions_pass_through(link_everything):
    for demo in DEMOS:
        doc = dict(demo["decomposition"], definition=demo["definition"])
        enrichment.enrich_with_uris_reranker(doc)


# --- invariants -------------------------------------------------------------- #

def test_input_is_never_mutated(link_everything):
    original = pred({"AsymmetricSystem": "a / b", "hasNumerator": "a", "hasDenominator": "b"})
    untouched = copy.deepcopy(original)
    enrichment.enrich_with_uris_reranker(original)
    assert original == untouched


def test_below_threshold_adds_no_key(link_nothing):
    out = enrichment.enrich_with_uris_reranker(pred("water"))
    assert "hasObjectOfInterestURI" not in out


def test_empty_entity_adds_no_key(link_everything):
    out = enrichment.enrich_with_uris_reranker(pred(""))
    assert "hasObjectOfInterestURI" not in out


def test_dict_on_has_property_is_left_untouched(link_everything):
    doc = pred("water")
    doc["hasProperty"] = {"label": "count"}
    out = enrichment.enrich_with_uris_reranker(doc)
    assert out["hasProperty"] == {"label": "count"}
    assert "hasPropertyURI" not in out


def test_reranker_exception_propagates(monkeypatch):
    """Caught one level up by _finalize_pipeline_output, not swallowed here."""
    monkeypatch.setattr(
        enrichment, "get_http_session",
        lambda: type("S", (), {"get": staticmethod(lambda url, timeout=20: FakeResponse(
            {"search": [{"id": "Q1", "label": "h", "description": "d"}]}))})(),
    )

    def boom(query, documents):
        raise RuntimeError("reranker mismatch")

    monkeypatch.setattr(enrichment, "call_psnc_reranker", boom)
    with pytest.raises(RuntimeError):
        enrichment.enrich_with_uris_reranker(pred("water"))


# --- helpers ----------------------------------------------------------------- #

def test_to_wiki_url_normalizes_a_qid():
    assert enrichment.to_wiki_url("Q42") == "https://www.wikidata.org/wiki/Q42"
    assert enrichment.to_wiki_url("http://www.wikidata.org/entity/Q42") == "https://www.wikidata.org/wiki/Q42"
    assert enrichment.to_wiki_url(None) is None


def test_qid_extraction():
    assert enrichment.qid_from_uri_or_text("http://www.wikidata.org/entity/Q42") == "Q42"
    assert enrichment.qid_from_uri_or_text("no identifier here") is None
    assert enrichment.qid_from_uri_or_text(None) is None
