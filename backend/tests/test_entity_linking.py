"""How entity linking walks a prediction and what it writes into it.

How one label is linked is tested in test_entity_linking_flow.py; here that step is replaced by a stub, so these tests
pin the traversal, the keys and the stored link forms. The measured decomposition shapes, in particular three-key
asymmetric systems, are consumed without modification. Fully offline.
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


# --- the measured shapes ------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "system, linked, absent",
    [
        ({"AsymmetricSystem": "a / b", "hasNumerator": "a", "hasDenominator": "b"},
         {"AsymmetricSystemURI", "hasNumeratorURI", "hasDenominatorURI"}, {"hasSourceURI", "hasTargetURI"}),
        ({"AsymmetricSystem": "a → b", "hasSource": "a", "hasTarget": "b"},
         {"AsymmetricSystemURI", "hasSourceURI", "hasTargetURI"}, {"hasNumeratorURI", "hasDenominatorURI"}),
        ({"SymmetricSystem": "a + b", "hasPart": ["a", "b"]}, {"SymmetricSystemURI", "hasPartURIs"}, set()),
    ],
    ids=["numerator-denominator", "source-target", "symmetric"],
)
def test_each_system_shape_links_exactly_the_keys_it_has(link_everything, system, linked, absent):
    entity = enrichment.enrich_with_uris(pred(system))["hasObjectOfInterest"]
    assert linked <= set(entity) and not (absent & set(entity))


def test_part_links_stay_aligned_with_the_parts_and_are_left_out_when_none_links(monkeypatch):
    monkeypatch.setattr(enrichment, "choose_candidate",
                        lambda term, definition, linker=None: candidate() if term in ("a", "b") else None)
    out = enrichment.enrich_with_uris(pred({"SymmetricSystem": "s", "hasPart": ["a", "  ", None, 5, "c", "b"]}))
    assert out["hasObjectOfInterest"]["hasPartURIs"] == [WIKI_Q1, None, None, None, None, WIKI_Q1]
    monkeypatch.setattr(enrichment, "choose_candidate", lambda term, definition, linker=None: None)
    out = enrichment.enrich_with_uris(pred({"SymmetricSystem": "s", "hasPart": ["a", "b"]}))
    assert "hasPartURIs" not in out["hasObjectOfInterest"]


def test_all_measured_decompositions_pass_through(link_everything):
    for demo in DEMOS:
        enrichment.enrich_with_uris(dict(demo["decomposition"], definition=demo["definition"]))


def test_every_simple_field_is_linked(link_everything):
    doc = {"definition": "d", "hasProperty": "p", "hasMatrix": "m", "hasObjectOfInterest": "o",
           "hasContextObject": "c", "hasStatisticalModifier": "s", "hasConstraint": []}
    out = enrichment.enrich_with_uris(doc)
    for field in ("hasProperty", "hasMatrix", "hasObjectOfInterest", "hasContextObject", "hasStatisticalModifier"):
        assert out[f"{field}URI"] == WIKI_Q1


def test_systems_are_linked_on_the_matrix_and_the_context_object_too(link_everything):
    doc = pred("")
    doc["hasMatrix"] = {"AsymmetricSystem": "x", "hasSource": "a", "hasTarget": "b"}
    doc["hasContextObject"] = {"SymmetricSystem": "y", "hasPart": ["c"]}
    out = enrichment.enrich_with_uris(doc)
    assert out["hasMatrix"]["hasSourceURI"] and out["hasMatrix"]["hasTargetURI"]
    assert out["hasContextObject"]["hasPartURIs"] == [WIKI_Q1]


# --- what is asked and what is kept -------------------------------------------------------------------------------- #

def test_the_input_is_never_changed(link_everything):
    original = pred({"AsymmetricSystem": "a / b", "hasNumerator": "a", "hasDenominator": "b"})
    untouched = copy.deepcopy(original)
    enrichment.enrich_with_uris(original)
    assert original == untouched


def test_an_empty_label_adds_no_key_and_asks_nothing(link_everything):
    out = enrichment.enrich_with_uris(pred("   "))
    assert "hasObjectOfInterestURI" not in out
    assert [term for term, _ in link_everything] == ["count"]


def test_malformed_values_are_skipped_without_failing(link_everything):
    doc = pred({"AsymmetricSystem": ["not", "text"], "hasNumerator": 5, "hasDenominator": "b"})
    doc["hasProperty"] = {"label": "count"}
    out = enrichment.enrich_with_uris(doc)
    assert out["hasProperty"] == {"label": "count"} and "hasPropertyURI" not in out
    entity = out["hasObjectOfInterest"]
    assert "AsymmetricSystemURI" not in entity and "hasNumeratorURI" not in entity
    assert entity["hasDenominatorURI"] == WIKI_Q1


def test_the_definition_travels_with_every_lookup_and_a_missing_one_is_empty(link_everything):
    enrichment.enrich_with_uris(dict(pred("water"), definition="a very particular definition"))
    assert link_everything and all(definition == "a very particular definition" for _, definition in link_everything)
    link_everything.clear()
    doc = pred("water")
    del doc["definition"]
    enrichment.enrich_with_uris(doc)
    assert link_everything and all(definition == "" for _, definition in link_everything)


def test_a_label_that_occurs_twice_is_linked_once(link_everything):
    doc = pred({"AsymmetricSystem": "water", "hasSource": "water", "hasTarget": "air"})
    doc["hasMatrix"] = "water"
    out = enrichment.enrich_with_uris(doc)
    assert sorted(term for term, _ in link_everything) == ["air", "count", "water"]
    assert out["hasMatrixURI"] == out["hasObjectOfInterest"]["hasSourceURI"] == out["hasObjectOfInterest"]["AsymmetricSystemURI"]


def test_spellings_that_normalise_alike_share_one_lookup_but_case_does_not(link_everything):
    doc = pred("soil  moisture")
    doc["hasMatrix"] = "soil moisture "
    doc["hasContextObject"] = "Soil moisture"
    enrichment.enrich_with_uris(doc)
    # Fields are visited in a fixed order: the matrix comes before the object, so its spelling is the one asked.
    assert [term for term, _ in link_everything] == ["count", "soil moisture ", "Soil moisture"]


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
    assert emitted == untouched


def test_only_links_the_linker_chose_survive(link_everything):
    out = enrichment.enrich_with_uris(model_links())
    assert set(uri_keys(out)) == {"hasPropertyURI", "AsymmetricSystemURI", "hasNumeratorURI", "hasDenominatorURI",
                                  "SymmetricSystemURI", "hasPartURIs"}
    assert out["hasPropertyURI"] == WIKI_Q1
    assert out["hasObjectOfInterest"]["hasNumeratorURI"] == WIKI_Q1
    assert out["hasMatrix"]["hasPartURIs"] == [WIKI_Q1]


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
    assert enrichment.enrich_with_uris(pred("temperature"))["hasObjectOfInterestURI"] == stored


# --- the pipeline around it ---------------------------------------------------------------------------------------- #

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


def test_a_linking_failure_leaves_the_decomposition_intact(monkeypatch, finalize, capsys):
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


# --- the model that answers: the decomposition's (D-038) ----------------------------------------------------------- #

def test_enrichment_asks_with_the_linker_it_was_given_and_by_default_the_measured_one(monkeypatch):
    seen = []

    def choose(term, definition, linker=None):
        seen.append(linker)
        return candidate()

    monkeypatch.setattr(enrichment, "choose_candidate", choose)
    run = Linker("openrouter", "qwen/qwen3.5-flash-02-23")
    enrichment.enrich_with_uris(pred("water"), linker=run)
    assert seen and all(linker == run for linker in seen)
    seen.clear()
    enrichment.enrich_with_uris(pred("water"))
    assert seen and all(linker == Linker("psnc", "Qwen3.8-27B") for linker in seen)


def test_run_pipeline_links_with_the_model_that_decomposed(monkeypatch, finalize):
    monkeypatch.setattr(settings, "enable_wikidata_linking", True)
    monkeypatch.setattr(pipeline, "_prepare_pipeline_inputs",
                        lambda definition, model_name=None, model_provider=None, **_: ("prompt", "openrouter",
                                                                                       "qwen/qwen3.5-flash-02-23"))
    monkeypatch.setattr(pipeline, "call_llm_loose", lambda *args, **kwargs: ("raw", pred("water")))
    monkeypatch.setattr(pipeline, "_labelled_prediction", lambda prediction, **kwargs: prediction)
    seen = []
    monkeypatch.setattr(pipeline, "enrich_with_uris", lambda prediction, linker=None: seen.append(linker) or prediction)
    # Nothing requested: the linker must come from the resolved pair, not from the request.
    pipeline.run_pipeline("water temperature", model_provider=None, model_name=None)
    assert seen == [Linker("openrouter", "qwen/qwen3.5-flash-02-23")]


def test_the_streamed_pipeline_links_with_the_model_that_decomposed(monkeypatch, finalize):
    monkeypatch.setattr(settings, "enable_wikidata_linking", True)
    monkeypatch.setattr(pipeline, "_prepare_pipeline_inputs",
                        lambda definition, model_name=None, model_provider=None, **_: ("prompt", "psnc",
                                                                                       "Qwen3.5-397B-A17B"))
    deltas = [("", "{}")]
    monkeypatch.setattr(pipeline, "stream_psnc_model", lambda *args, **kwargs: iter(deltas))
    monkeypatch.setattr(pipeline, "extract_stream_text_deltas", lambda chunk: chunk)
    monkeypatch.setattr(pipeline, "parse_llm_json", lambda raw, definition: pred("water"))
    monkeypatch.setattr(pipeline, "_labelled_prediction", lambda prediction, **kwargs: prediction)
    seen = []
    monkeypatch.setattr(pipeline, "enrich_with_uris", lambda prediction, linker=None: seen.append(linker) or prediction)
    events = [json.loads(line) for line in pipeline.stream_pipeline_events("water temperature",
                                                                           model_provider=None, model_name=None)]
    assert events[-1]["type"] == "final"
    assert seen == [Linker("psnc", "Qwen3.5-397B-A17B")]


def test_the_non_streamed_fallback_asks_and_links_with_the_model_that_decomposed(monkeypatch, finalize):
    """The stream fails before its first chunk, so the pipeline asks again without streaming."""
    monkeypatch.setattr(settings, "enable_wikidata_linking", True)
    monkeypatch.setattr(pipeline, "_prepare_pipeline_inputs",
                        lambda definition, model_name=None, model_provider=None, **_: ("prompt", "openrouter",
                                                                                       "qwen/qwen3.5-flash-02-23"))

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
    assert asked == ["openrouter"] and events[-1]["type"] == "final"
    assert seen == [Linker("openrouter", "qwen/qwen3.5-flash-02-23")]
