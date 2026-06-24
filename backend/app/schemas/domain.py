"""Domain models for the I-ADOPT decomposition pipeline.

These describe the objects that currently travel between pipeline functions as
``Dict[str, Any]``: the LLM decomposition *prediction*, its *enriched* form after
Wikidata linking, and the structured constraint/system parts. They are the typed
contract the Phase-2 service split (``services/llm.py``, ``services/enrichment.py``,
``services/rdf_ttl.py``) will pass around.

Important boundary note (kept ``Dict[str, Any]`` on purpose elsewhere):
the *raw* parsed LLM JSON and the final ``parsed_json`` / ``enriched_json`` fields
of :class:`~app.schemas.responses.DecomposeResponse` remain free-form dicts. The
LLM can return any of three ``entityOrSystem`` variants (string, AsymmetricSystem,
SymmetricSystem), and the enrichment step adds dynamic ``*URI`` / ``*URIs`` keys
whose presence depends on Wikidata hits. Modeling those as closed schemas and
wiring them as response types would risk rejecting valid-but-unusual output, so the
models here are a *documentation/typing* contract, not a serialization gate.
"""

from __future__ import annotations

from typing import Annotated, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class Constraint(BaseModel):
    """A single constraint qualifier extracted from a variable definition.

    Attributes:
        label: Short cleaned phrase describing the restriction (e.g. ``"dry"``,
            ``"per mol"``).
        on: The property/entity label this constraint applies to. Must match a
            real target label in the prediction (enforced semantically by
            ``_get_constraint_semantic_validation_errors``, not by this model).
    """

    model_config = ConfigDict(extra="allow")

    label: str
    on: str


class AsymmetricSystem(BaseModel):
    """A directional entity system (from a source to a target, or a ratio).

    Mirrors the ``AsymmetricSystem`` branch of the JSON-Schema ``entityOrSystem``
    definition. ``*URI`` fields are added dynamically by Wikidata enrichment and so
    are optional here.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    asymmetric_system: str = Field(alias="AsymmetricSystem")
    has_source: Optional[str] = Field(default=None, alias="hasSource")
    has_target: Optional[str] = Field(default=None, alias="hasTarget")
    has_numerator: Optional[str] = Field(default=None, alias="hasNumerator")
    has_denominator: Optional[str] = Field(default=None, alias="hasDenominator")


class SymmetricSystem(BaseModel):
    """A non-directional entity system composed of interchangeable parts.

    Mirrors the ``SymmetricSystem`` branch of the JSON-Schema ``entityOrSystem``
    definition.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    symmetric_system: str = Field(alias="SymmetricSystem")
    has_part: List[str] = Field(default_factory=list, alias="hasPart")


# An entity-or-system slot is a bare label, an asymmetric system, or a symmetric one.
EntityOrSystem = Union[str, AsymmetricSystem, SymmetricSystem]


class Prediction(BaseModel):
    """The LLM decomposition of a variable, after ``coerce_prediction`` normalization.

    This is the shape produced by ``parse_llm_json`` → ``coerce_prediction`` and
    validated against ``data/Json_schema.json``. ``extra="allow"`` is set because
    the model may legitimately include keys this closed list does not enumerate
    (e.g. enrichment ``*URI`` keys once present). Required-ness here is descriptive;
    the authoritative validation is the JSON-Schema validator.

    Attributes:
        label: Human-readable variable name (``rdfs:label`` / ``skos:prefLabel``).
        definition: Full natural-language definition (echoed back from the request).
        comment: Short summary of the definition.
        has_property: The main measurable property.
        has_object_of_interest: The thing that has the property (entity or system).
        has_statistical_modifier: Optional statistical qualifier (e.g. ``"maximum"``).
        has_matrix: Optional medium in which the object occurs.
        has_context_object: Optional contextual entity/system.
        has_constraint: List of constraint qualifiers.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    label: str = ""
    definition: str = ""
    comment: str = ""
    has_property: str = Field(default="", alias="hasProperty")
    has_object_of_interest: EntityOrSystem = Field(default="", alias="hasObjectOfInterest")
    has_statistical_modifier: str = Field(default="", alias="hasStatisticalModifier")
    has_matrix: Optional[EntityOrSystem] = Field(default=None, alias="hasMatrix")
    has_context_object: Optional[EntityOrSystem] = Field(default=None, alias="hasContextObject")
    has_constraint: List[Constraint] = Field(default_factory=list, alias="hasConstraint")


# The enriched prediction is the same shape plus dynamic Wikidata `*URI`/`*URIs`
# keys (e.g. hasPropertyURI, hasObjectOfInterestURI, hasPartURIs). Because those
# keys are open-ended, the enriched form is documented as a free-form mapping with
# the Prediction fields as a known subset.
EnrichedPredictionDict = Annotated[
    Dict[str, object],
    Field(description="Prediction fields plus dynamically added Wikidata *URI/*URIs keys."),
]
