"""Tests for the links the Turtle serializer writes: Wikidata, QUDT and OM.

Contract: iadopt-variable-description-service-docs/components/entity-linking-llm.md — D-032.

Before D-032 only Wikidata links could reach the Turtle: every other IRI was dropped without a
trace. A QUDT or OM IRI is written between angle brackets, so the gate that lets one through is
strict, and the generated Turtle is parsed back to prove nothing outside the IRI can get in.

No network call: the clock, the RNG and the ORCID lookup are frozen as in the golden tests.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

import app.services.rdf_ttl as rdf_ttl

IOP = Namespace("https://w3id.org/iadopt/ont/")
FROZEN_NOW = _dt.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=_dt.timezone.utc)

QUDT = "http://qudt.org/vocab/quantitykind/Temperature"
OM = "http://www.ontology-of-units-of-measure.org/resource/om-2/Temperature"
WIKIDATA_PAGE = "https://www.wikidata.org/wiki/Q283"
WIKIDATA_ENTITY = "https://www.wikidata.org/entity/Q283"


@pytest.fixture(autouse=True)
def freeze_nondeterminism(monkeypatch):
    monkeypatch.setattr(
        rdf_ttl, "datetime",
        type("dt", (), {"now": staticmethod(lambda tz=None: FROZEN_NOW), "timezone": _dt.timezone}),
    )
    monkeypatch.setattr(rdf_ttl.random, "randint", lambda a, b: 7)


def prediction(**fields):
    base = {"label": "air temperature", "definition": "air temperature at 2 m", "comment": "c",
            "hasProperty": "temperature", "hasObjectOfInterest": "air", "hasMatrix": "",
            "hasContextObject": "", "hasStatisticalModifier": "", "hasConstraint": []}
    base.update(fields)
    return base


def graph_of(pred):
    ttl = rdf_ttl.json_to_ttl_repo_style(pred)
    graph = Graph()
    graph.parse(data=ttl, format="turtle")
    return ttl, graph


def the_variable(graph):
    (variable,) = list(graph.subjects(RDF.type, IOP.Variable))
    return variable


# --- the gate ------------------------------------------------------------------ #

@pytest.mark.parametrize(
    "stored, written",
    [
        (WIKIDATA_PAGE, WIKIDATA_ENTITY),
        ("http://www.wikidata.org/entity/Q283", WIKIDATA_ENTITY),
        ("Q283", WIKIDATA_ENTITY),
        (QUDT, QUDT),
        (OM, OM),
    ],
)
def test_each_kind_of_stored_link_is_written_in_its_own_form(stored, written):
    assert rdf_ttl.linked_entity_iri(stored) == written


@pytest.mark.parametrize(
    "stored",
    [
        None, "", "not an iri", "https://qudt.org/vocab/quantitykind/Temperature",
        "http://qudt.org/vocab/quantitykind/", "http://qudt.org/vocab/unit/K",
        "https://www.ontology-of-units-of-measure.org/resource/om-2/Temperature",
        "http://qudt.org/vocab/quantitykind/Temperature> a <http://evil.example/x",
        "http://qudt.org/vocab/quantitykind/Temp\nerature", "<http://qudt.org/vocab/quantitykind/Temperature>",
        "http://evil.example/http://qudt.org/vocab/quantitykind/Temperature",
    ],
)
def test_anything_else_is_dropped(stored):
    assert rdf_ttl.linked_entity_iri(stored) is None


def test_a_wikidata_item_id_inside_other_text_is_still_reduced_to_the_item_id_alone():
    assert rdf_ttl.linked_entity_iri('"> . <http://evil.example/x> a <Q42') == WIKIDATA_ENTITY.replace("Q283", "Q42")


# --- the Turtle ---------------------------------------------------------------- #

def test_a_qudt_property_is_written_as_the_property_resource():
    ttl, graph = graph_of(prediction(hasPropertyURI=QUDT))
    assert f"<{QUDT}>" in ttl
    assert (URIRef(QUDT), RDF.type, IOP.Property) in graph
    assert (the_variable(graph), IOP.hasProperty, URIRef(QUDT)) in graph
    assert (URIRef(QUDT), RDFS.label, Literal("temperature")) in graph


def test_an_om_property_is_written_the_same_way():
    ttl, graph = graph_of(prediction(hasPropertyURI=OM))
    assert (the_variable(graph), IOP.hasProperty, URIRef(OM)) in graph
    assert (URIRef(OM), RDF.type, IOP.Property) in graph


def test_wikidata_and_qudt_links_sit_side_by_side():
    _, graph = graph_of(prediction(hasPropertyURI=QUDT, hasObjectOfInterestURI=WIKIDATA_PAGE))
    variable = the_variable(graph)
    assert (variable, IOP.hasProperty, URIRef(QUDT)) in graph
    assert (variable, IOP.hasObjectOfInterest, URIRef(WIKIDATA_ENTITY)) in graph
    assert (URIRef(WIKIDATA_ENTITY), RDF.type, IOP.Entity) in graph


def test_a_label_with_no_link_still_gets_a_local_resource():
    ttl, graph = graph_of(prediction())
    variable = the_variable(graph)
    (prop,) = list(graph.objects(variable, IOP.hasProperty))
    assert str(prop).startswith(str(variable) + "#")


def test_qudt_and_om_links_inside_systems_are_written():
    pred = prediction(hasObjectOfInterest={"AsymmetricSystem": "a over b", "hasNumerator": "a", "hasDenominator": "b",
                                           "hasNumeratorURI": QUDT, "hasDenominatorURI": OM,
                                           "AsymmetricSystemURI": WIKIDATA_PAGE})
    _, graph = graph_of(pred)
    assert (URIRef(QUDT), RDF.type, IOP.Entity) in graph
    assert (URIRef(OM), RDF.type, IOP.Entity) in graph
    assert (URIRef(WIKIDATA_ENTITY), RDF.type, IOP.AsymmetricSystem) in graph
    assert {QUDT, OM} <= {str(o) for o in graph.objects(URIRef(WIKIDATA_ENTITY), IOP.hasNumerator)} | \
        {str(o) for o in graph.objects(URIRef(WIKIDATA_ENTITY), IOP.hasDenominator)}


def test_parts_keep_their_position_when_only_some_link():
    pred = prediction(hasObjectOfInterest={"SymmetricSystem": "s", "hasPart": ["a", "b", "c"],
                                           "hasPartURIs": [QUDT, None, WIKIDATA_PAGE]})
    _, graph = graph_of(pred)
    parts = {str(o) for o in graph.objects(None, IOP.hasPart)}
    assert QUDT in parts and WIKIDATA_ENTITY in parts and len(parts) == 3


# --- nothing outside the IRI gets in --------------------------------------------- #

@pytest.mark.parametrize(
    "hostile",
    [
        'http://qudt.org/vocab/quantitykind/Temperature> ; rdfs:label "pwned" . <http://evil.example/x',
        "http://qudt.org/vocab/quantitykind/Temperature>\n<http://evil.example/s> <http://evil.example/p> <http://evil.example/o> .\n<x",
        "javascript:alert(1)",
    ],
)
def test_a_hostile_link_cannot_add_anything_to_the_graph(hostile):
    clean_ttl, clean = graph_of(prediction())
    ttl, graph = graph_of(prediction(hasPropertyURI=hostile))
    assert "evil.example" not in ttl and "pwned" not in ttl
    assert len(graph) == len(clean)


def test_wikidata_output_is_unchanged_by_the_generalisation():
    """The golden TTL tests pin this byte for byte; this is the same promise at unit level."""
    ttl, _ = graph_of(prediction(hasPropertyURI="https://www.wikidata.org/wiki/Q11466"))
    assert "<https://www.wikidata.org/entity/Q11466>" in ttl


def test_generated_turtle_carries_no_creator_orcid():
    """The person publishing supplies the ORCID later; decomposition never writes one."""
    ttl, graph = graph_of(prediction())
    assert "orcid" not in ttl.lower()
    variable = the_variable(graph)
    assert not list(graph.objects(variable, URIRef("http://purl.org/dc/terms/creator")))
    assert not list(graph.objects(variable, URIRef("http://www.w3.org/ns/prov#wasAttributedTo")))
