"""Tests pinning how entity linking walks a prediction and what it writes into it.

Contract: iadopt-variable-description-service-docs/components/entity-linking-llm.md — D-032,
which replaced the Wikidata-search-plus-reranker step documented in components/entity-linking.md
(D-006). The shape handling is the part that stayed: the measured decomposition shapes,
in particular three-key asymmetric systems, are consumed without modification.

How one label is linked is tested in test_entity_linking_flow.py; here that step is replaced by
a stub, so these tests pin only the traversal and the keys. Fully offline.
"""

from __future__ import annotations

import copy
import json
import pathlib
from types import SimpleNamespace

import pytest

from app import pipeline
from app.core.config import settings
from app.services import enrichment
from app.services.entity_linking import MEASURED_LINKER, Linker
from app.services.vocabularies import Candidate

BACKEND = pathlib.Path(__file__).resolve().parents[1]
DEMOS = json.loads((BACKEND / "data/measured/demonstrations-top25.json").read_text(encoding="utf-8"))

WIKI_Q1 = "https://www.wikidata.org/wiki/Q1"
QUDT_IRI = "http://qudt.org/vocab/quantitykind/Temperature"
OM_IRI = "http://www.ontology-of-units-of-measure.org/resource/om-2/Temperature"


def candidate(source="wikidata", local_id="Q1"):
    iri = {
        "wikidata": f"http://www.wikidata.org/entity/{local_id}",
        "qudt": f"http://qudt.org/vocab/quantitykind/{local_id}",
        "om": f"http://www.ontology-of-units-of-measure.org/resource/om-2/{local_id}",
    }[source]
    return Candidate(source, iri, local_id, "hit", "d")


@pytest.fixture
def link_everything(monkeypatch):
    """Every label is linked to Wikidata item Q1; the calls are recorded."""
    calls = []

    def choose(term, definition, linker=None):
        calls.append((term, definition))
        return candidate()

    monkeypatch.setattr(enrichment, "choose_candidate", choose)
    return calls


@pytest.fixture
def link_nothing(monkeypatch):
    """The model names no entry for any label."""
    monkeypatch.setattr(enrichment, "choose_candidate", lambda term, definition, linker=None: None)


def pred(entity):
    return {"definition": "d", "hasProperty": "count", "hasObjectOfInterest": entity,
            "hasMatrix": "", "hasContextObject": "", "hasStatisticalModifier": "", "hasConstraint": []}


# --- the measured shapes ----------------------------------------------------- #

def test_numerator_denominator_links_its_three_present_keys(link_everything):
    out = enrichment.enrich_with_uris(
        pred({"AsymmetricSystem": "a / b", "hasNumerator": "a", "hasDenominator": "b"}))
    entity = out["hasObjectOfInterest"]
    assert entity["AsymmetricSystemURI"] == WIKI_Q1
    assert entity["hasNumeratorURI"] and entity["hasDenominatorURI"]
    assert "hasSourceURI" not in entity
    assert "hasTargetURI" not in entity


def test_source_target_links_its_three_present_keys(link_everything):
    out = enrichment.enrich_with_uris(
        pred({"AsymmetricSystem": "a → b", "hasSource": "a", "hasTarget": "b"}))
    entity = out["hasObjectOfInterest"]
    assert entity["hasSourceURI"] and entity["hasTargetURI"]
    assert "hasNumeratorURI" not in entity
    assert "hasDenominatorURI" not in entity


def test_symmetric_system_links_container_and_parts(link_everything):
    out = enrichment.enrich_with_uris(
        pred({"SymmetricSystem": "a + b", "hasPart": ["a", "b"]}))
    entity = out["hasObjectOfInterest"]
    assert entity["SymmetricSystemURI"]
    assert entity["hasPartURIs"] == [WIKI_Q1] * 2


def test_part_uris_absent_when_nothing_links(link_nothing):
    out = enrichment.enrich_with_uris(
        pred({"SymmetricSystem": "a + b", "hasPart": ["a", "b"]}))
    assert "hasPartURIs" not in out["hasObjectOfInterest"]


def test_part_uris_stay_aligned_with_parts_when_only_one_links(monkeypatch):
    monkeypatch.setattr(enrichment, "choose_candidate", lambda term, definition, linker=None: candidate() if term == "a" else None)
    out = enrichment.enrich_with_uris(pred({"SymmetricSystem": "a + b", "hasPart": ["a", "b"]}))
    assert out["hasObjectOfInterest"]["hasPartURIs"] == [WIKI_Q1, None]


def test_blank_and_non_string_parts_keep_their_place(link_everything):
    out = enrichment.enrich_with_uris(pred({"SymmetricSystem": "s", "hasPart": ["a", "  ", None, 5, "b"]}))
    assert out["hasObjectOfInterest"]["hasPartURIs"] == [WIKI_Q1, None, None, None, WIKI_Q1]


def test_all_measured_decompositions_pass_through(link_everything):
    for demo in DEMOS:
        doc = dict(demo["decomposition"], definition=demo["definition"])
        enrichment.enrich_with_uris(doc)


def test_every_simple_field_is_linked(link_everything):
    doc = {"definition": "d", "hasProperty": "p", "hasMatrix": "m", "hasObjectOfInterest": "o",
           "hasContextObject": "c", "hasStatisticalModifier": "s", "hasConstraint": []}
    out = enrichment.enrich_with_uris(doc)
    for field in ("hasProperty", "hasMatrix", "hasObjectOfInterest", "hasContextObject", "hasStatisticalModifier"):
        assert out[f"{field}URI"] == WIKI_Q1


def test_system_fields_are_linked_on_matrix_and_context_object_too(link_everything):
    doc = pred("")
    doc["hasMatrix"] = {"AsymmetricSystem": "x", "hasSource": "a", "hasTarget": "b"}
    doc["hasContextObject"] = {"SymmetricSystem": "y", "hasPart": ["c"]}
    out = enrichment.enrich_with_uris(doc)
    assert out["hasMatrix"]["hasSourceURI"] and out["hasMatrix"]["hasTargetURI"]
    assert out["hasContextObject"]["hasPartURIs"] == [WIKI_Q1]


# --- invariants -------------------------------------------------------------- #

def test_input_is_never_mutated(link_everything):
    original = pred({"AsymmetricSystem": "a / b", "hasNumerator": "a", "hasDenominator": "b"})
    untouched = copy.deepcopy(original)
    enrichment.enrich_with_uris(original)
    assert original == untouched


def test_a_label_nothing_is_chosen_for_adds_no_key(link_nothing):
    out = enrichment.enrich_with_uris(pred("water"))
    assert "hasObjectOfInterestURI" not in out


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_empty_entity_adds_no_key_and_issues_no_lookup(link_everything, blank):
    out = enrichment.enrich_with_uris(pred(blank))
    assert "hasObjectOfInterestURI" not in out
    assert [term for term, _ in link_everything] == ["count"]


def test_dict_on_has_property_is_left_untouched(link_everything):
    doc = pred("water")
    doc["hasProperty"] = {"label": "count"}
    out = enrichment.enrich_with_uris(doc)
    assert out["hasProperty"] == {"label": "count"}
    assert "hasPropertyURI" not in out


def test_a_non_string_system_label_is_skipped_not_fatal(link_everything):
    out = enrichment.enrich_with_uris(
        pred({"AsymmetricSystem": ["not", "text"], "hasNumerator": 5, "hasDenominator": "b"}))
    entity = out["hasObjectOfInterest"]
    assert "AsymmetricSystemURI" not in entity and "hasNumeratorURI" not in entity
    assert entity["hasDenominatorURI"] == WIKI_Q1


def test_the_definition_travels_with_every_lookup(link_everything):
    enrichment.enrich_with_uris(dict(pred("water"), definition="a very particular definition"))
    assert link_everything and all(definition == "a very particular definition" for _, definition in link_everything)


def test_a_missing_definition_is_an_empty_context_not_an_error(link_everything):
    doc = pred("water")
    del doc["definition"]
    enrichment.enrich_with_uris(doc)
    assert all(definition == "" for _, definition in link_everything)


def test_a_label_that_occurs_twice_is_linked_once(link_everything):
    doc = pred({"AsymmetricSystem": "water", "hasSource": "water", "hasTarget": "air"})
    doc["hasMatrix"] = "water"
    out = enrichment.enrich_with_uris(doc)
    assert sorted(term for term, _ in link_everything) == ["air", "count", "water"]
    assert out["hasMatrixURI"] == out["hasObjectOfInterest"]["hasSourceURI"] == out["hasObjectOfInterest"]["AsymmetricSystemURI"]


def test_spellings_that_normalise_alike_share_one_lookup_but_case_does_not(link_everything):
    doc = pred("soil  moisture")
    doc["hasMatrix"] = "soil moisture "
    doc["hasContextObject"] = "Soil moisture"
    enrichment.enrich_with_uris(doc)
    # Fields are visited in a fixed order: the matrix comes before the object, so its spelling is the one asked.
    assert [term for term, _ in link_everything] == ["count", "soil moisture ", "Soil moisture"]


def test_a_failure_to_link_is_remembered_too(monkeypatch):
    calls = []
    monkeypatch.setattr(enrichment, "choose_candidate", lambda term, definition, linker=None: calls.append(term))
    doc = pred("water")
    doc["hasMatrix"] = "water"
    enrichment.enrich_with_uris(doc)
    assert calls.count("water") == 1


def model_links():
    """A prediction as the decomposition model could emit it, carrying links of its own."""
    return {"definition": "d", "hasProperty": "count", "hasPropertyURI": QUDT_IRI, "hasObjectOfInterest":
            {"AsymmetricSystem": "a / b", "AsymmetricSystemURI": QUDT_IRI, "hasNumerator": "a", "hasNumeratorURI": OM_IRI,
             "hasDenominator": "b", "hasDenominatorURI": "https://www.wikidata.org/wiki/Q999"},
            "hasMatrix": {"SymmetricSystem": "s", "hasPart": ["x"], "hasPartURIs": [QUDT_IRI]},
            "hasContextObject": "", "hasStatisticalModifier": "", "hasConstraint": []}


def uri_keys(value, found=None):
    found = [] if found is None else found
    if isinstance(value, dict):
        for key, inner in value.items():
            if "URI" in key:
                found.append(key)
            uri_keys(inner, found)
    elif isinstance(value, list):
        for inner in value:
            uri_keys(inner, found)
    return found


def test_links_the_decomposition_model_emitted_are_dropped_when_the_linker_finds_nothing(link_nothing):
    emitted = model_links()
    untouched = copy.deepcopy(emitted)
    out = enrichment.enrich_with_uris(emitted)
    assert uri_keys(out) == []
    assert emitted == untouched and len(uri_keys(emitted)) == 5


def test_only_links_the_linker_chose_survive(link_everything):
    out = enrichment.enrich_with_uris(model_links())
    assert set(uri_keys(out)) == {"hasPropertyURI", "AsymmetricSystemURI", "hasNumeratorURI", "hasDenominatorURI",
                                  "SymmetricSystemURI", "hasPartURIs"}
    assert out["hasPropertyURI"] == WIKI_Q1
    assert out["hasObjectOfInterest"]["hasNumeratorURI"] == WIKI_Q1
    assert out["hasMatrix"]["hasPartURIs"] == [WIKI_Q1]


def test_a_failure_while_linking_propagates(monkeypatch):
    """Caught one level up by _finalize_pipeline_output, not swallowed here."""
    def boom(term, definition, linker=None):
        raise RuntimeError("PSNC did not answer the entity-linking request.")

    monkeypatch.setattr(enrichment, "choose_candidate", boom)
    with pytest.raises(RuntimeError):
        enrichment.enrich_with_uris(pred("water"))


# --- what is stored ------------------------------------------------------------ #

@pytest.mark.parametrize(
    "chosen, stored",
    [
        (candidate("wikidata", "Q11466"), "https://www.wikidata.org/wiki/Q11466"),
        (candidate("qudt", "Temperature"), QUDT_IRI),
        (candidate("om", "Temperature"), OM_IRI),
    ],
)
def test_each_vocabulary_is_stored_in_its_own_form(monkeypatch, chosen, stored):
    monkeypatch.setattr(enrichment, "choose_candidate", lambda term, definition, linker=None: chosen)
    out = enrichment.enrich_with_uris(pred("temperature"))
    assert out["hasObjectOfInterestURI"] == stored
    assert enrichment.candidate_link(chosen) == stored


def test_qudt_and_om_iris_are_never_upgraded_to_https(monkeypatch):
    chosen = {"a": candidate("qudt", "Temperature"), "b": candidate("om", "Temperature")}
    monkeypatch.setattr(enrichment, "choose_candidate", lambda term, definition, linker=None: chosen.get(term))
    out = enrichment.enrich_with_uris(pred({"SymmetricSystem": "s", "hasPart": ["a", "b"]}))
    parts = out["hasObjectOfInterest"]["hasPartURIs"]
    assert parts == [QUDT_IRI, OM_IRI] and all(uri.startswith("http://") for uri in parts)


def test_sources_can_be_mixed_within_one_prediction(monkeypatch):
    by_term = {"count": candidate("qudt", "Count"), "water": candidate("wikidata", "Q283")}
    monkeypatch.setattr(enrichment, "choose_candidate", lambda term, definition, linker=None: by_term[term])
    out = enrichment.enrich_with_uris(pred("water"))
    assert out["hasPropertyURI"] == "http://qudt.org/vocab/quantitykind/Count"
    assert out["hasObjectOfInterestURI"] == "https://www.wikidata.org/wiki/Q283"


# --- the pipeline around it ------------------------------------------------------ #

@pytest.fixture
def finalize(monkeypatch):
    """_finalize_pipeline_output with validation and Turtle stubbed; returns the enriched input it saw."""
    seen = {}
    monkeypatch.setattr(pipeline, "get_schema_validation_errors", lambda p, label_for_logs=None: [])
    monkeypatch.setattr(pipeline, "get_constraint_semantic_validation_errors", lambda p: [])

    def fake_ttl(enriched):
        seen["enriched"] = enriched
        return "TTL"

    monkeypatch.setattr(pipeline, "json_to_ttl_repo_style", fake_ttl)
    return seen


def test_linking_failure_leaves_the_decomposition_intact(monkeypatch, finalize, capsys):
    monkeypatch.setattr(settings, "enable_wikidata_linking", True)

    def boom(prediction, linker=None):
        raise RuntimeError("PSNC did not answer the entity-linking request.")

    monkeypatch.setattr(pipeline, "enrich_with_uris", boom)
    prediction = pred("water")
    result = pipeline._finalize_pipeline_output("raw", prediction, linker=MEASURED_LINKER)
    assert result["enriched_json"] == prediction == result["parsed_json"]
    assert result["ttl"] == "TTL" and result["schema_valid"] is True
    assert "Entity linking failed" in capsys.readouterr().out


def test_linking_is_skipped_when_disabled(monkeypatch, finalize):
    monkeypatch.setattr(settings, "enable_wikidata_linking", False)
    monkeypatch.setattr(pipeline, "enrich_with_uris", lambda prediction, linker=None: pytest.fail("linking must not run"))
    prediction = pred("water")
    assert pipeline._finalize_pipeline_output("raw", prediction, linker=MEASURED_LINKER)["enriched_json"] == prediction


def test_the_pipeline_passes_the_enriched_prediction_on_to_the_turtle(monkeypatch, finalize, link_everything):
    monkeypatch.setattr(settings, "enable_wikidata_linking", True)
    result = pipeline._finalize_pipeline_output("raw", pred("water"), linker=MEASURED_LINKER)
    assert result["enriched_json"]["hasObjectOfInterestURI"] == WIKI_Q1
    assert finalize["enriched"] is result["enriched_json"]
    assert "hasObjectOfInterestURI" not in result["parsed_json"]


# --- helpers ----------------------------------------------------------------- #

def test_to_wiki_url_normalizes_a_qid():
    assert enrichment.to_wiki_url("Q42") == "https://www.wikidata.org/wiki/Q42"
    assert enrichment.to_wiki_url("http://www.wikidata.org/entity/Q42") == "https://www.wikidata.org/wiki/Q42"
    assert enrichment.to_wiki_url(None) is None


def test_qid_extraction():
    assert enrichment.qid_from_uri_or_text("http://www.wikidata.org/entity/Q42") == "Q42"
    assert enrichment.qid_from_uri_or_text("no identifier here") is None
    assert enrichment.qid_from_uri_or_text(None) is None


# --- the model that answers: the decomposition's (D-038) ------------------------ #

def test_enrichment_asks_with_the_linker_it_was_given(monkeypatch):
    seen = []

    def choose(term, definition, linker=None):
        seen.append(linker)
        return candidate()

    monkeypatch.setattr(enrichment, "choose_candidate", choose)
    run = Linker("openrouter", "qwen/qwen3.5-flash-02-23")
    enrichment.enrich_with_uris(pred("water"), linker=run)
    assert seen and all(linker == run for linker in seen)


def test_enrichment_without_a_linker_asks_the_measured_one(monkeypatch):
    seen = []
    monkeypatch.setattr(enrichment, "choose_candidate", lambda term, definition, linker=None: seen.append(linker))
    enrichment.enrich_with_uris(pred("water"))
    assert seen and all(linker == MEASURED_LINKER for linker in seen)
    assert MEASURED_LINKER == Linker("psnc", "Qwen3.8-27B")


def test_the_pipeline_hands_its_linker_to_enrichment(monkeypatch, finalize):
    monkeypatch.setattr(settings, "enable_wikidata_linking", True)
    seen = []
    monkeypatch.setattr(pipeline, "enrich_with_uris", lambda prediction, linker=None: seen.append(linker) or prediction)
    run = Linker("psnc", "Qwen3.5-397B-A17B")
    pipeline._finalize_pipeline_output("raw", pred("water"), linker=run)
    assert seen == [run]


@pytest.mark.parametrize("provider, model", [("openrouter", "qwen/qwen3.5-flash-02-23"), ("psnc", "Qwen3.8-27B")])
def test_run_pipeline_links_with_the_model_that_decomposed(monkeypatch, finalize, provider, model):
    monkeypatch.setattr(settings, "enable_wikidata_linking", True)
    monkeypatch.setattr(pipeline, "_prepare_pipeline_inputs", lambda definition, model_name=None, model_provider=None: ("prompt", provider, model))
    monkeypatch.setattr(pipeline, "call_llm_loose", lambda *args, **kwargs: ("raw", pred("water")))
    monkeypatch.setattr(pipeline, "_labelled_prediction", lambda prediction, **kwargs: prediction)
    seen = []
    monkeypatch.setattr(pipeline, "enrich_with_uris", lambda prediction, linker=None: seen.append(linker) or prediction)
    # Nothing requested: the linker must come from the resolved pair, not from the request.
    pipeline.run_pipeline("water temperature", model_provider=None, model_name=None)
    assert seen == [Linker(provider, model)]


@pytest.mark.parametrize("provider, model", [("openrouter", "qwen/qwen3.5-flash-02-23"), ("psnc", "Qwen3.5-397B-A17B")])
def test_the_streamed_pipeline_links_with_the_model_that_decomposed(monkeypatch, finalize, provider, model):
    monkeypatch.setattr(settings, "enable_wikidata_linking", True)
    monkeypatch.setattr(pipeline, "_prepare_pipeline_inputs",
                        lambda definition, model_name=None, model_provider=None: ("prompt", provider, model))
    deltas = [("", "{}")]
    monkeypatch.setattr(pipeline, "stream_psnc_model", lambda *args, **kwargs: iter(deltas))
    completions = SimpleNamespace(create=lambda **kwargs: iter(deltas))
    monkeypatch.setattr(pipeline, "get_openai_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    monkeypatch.setattr(pipeline, "extract_stream_text_deltas", lambda chunk: chunk)
    monkeypatch.setattr(pipeline, "parse_llm_json", lambda raw, definition: pred("water"))
    monkeypatch.setattr(pipeline, "_labelled_prediction", lambda prediction, **kwargs: prediction)
    seen = []
    monkeypatch.setattr(pipeline, "enrich_with_uris", lambda prediction, linker=None: seen.append(linker) or prediction)
    # Nothing requested: the linker must come from the resolved pair, not from the request.
    events = [json.loads(line) for line in pipeline.stream_pipeline_events("water temperature",
                                                                           model_provider=None, model_name=None)]
    assert events[-1]["type"] == "final"
    assert seen == [Linker(provider, model)]


@pytest.mark.parametrize("provider, model", [("openrouter", "qwen/qwen3.5-flash-02-23"), ("psnc", "Qwen3.5-397B-A17B")])
def test_the_non_streamed_fallback_links_with_the_model_that_decomposed(monkeypatch, finalize, provider, model):
    """The stream fails before its first chunk, so the pipeline asks again without streaming."""
    monkeypatch.setattr(settings, "enable_wikidata_linking", True)
    monkeypatch.setattr(pipeline, "_prepare_pipeline_inputs",
                        lambda definition, model_name=None, model_provider=None: ("prompt", provider, model))

    def broken_stream(*args, **kwargs):
        raise RuntimeError("the stream broke before its first chunk")

    monkeypatch.setattr(pipeline, "stream_psnc_model", broken_stream)
    monkeypatch.setattr(pipeline, "get_openai_client",
                        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken_stream))))
    asked = []
    monkeypatch.setattr(pipeline, "call_psnc_model", lambda *args, **kwargs: asked.append("psnc") or "{}")
    monkeypatch.setattr(pipeline, "call_model", lambda *args, **kwargs: asked.append("openrouter") or "{}")
    monkeypatch.setattr(pipeline, "parse_llm_json", lambda raw, definition: pred("water"))
    monkeypatch.setattr(pipeline, "_labelled_prediction", lambda prediction, **kwargs: prediction)
    seen = []
    monkeypatch.setattr(pipeline, "enrich_with_uris", lambda prediction, linker=None: seen.append(linker) or prediction)
    events = [json.loads(line) for line in pipeline.stream_pipeline_events("water temperature",
                                                                           model_provider=None, model_name=None)]
    assert asked == [provider] and events[-1]["type"] == "final"
    assert seen == [Linker(provider, model)]

