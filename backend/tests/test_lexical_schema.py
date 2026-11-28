"""The shapes the validation schema accepts, observed through the real seam: Draft202012Validator over the
pipeline-patched schema (D-005).
"""

from __future__ import annotations

import json
import pathlib

import pytest
from jsonschema import Draft202012Validator

from app.core.config import settings
from app.services.prompts import strip_all_uri_fields
from app.services.validation import load_schema, patch_schema_for_pipeline

BACKEND = pathlib.Path(__file__).resolve().parents[1]
DEMOS = json.loads((BACKEND / "data/measured/demonstrations-top25.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator():
    return Draft202012Validator(patch_schema_for_pipeline(load_schema(settings.schema_path)))


def wrap(entity):
    """Minimal valid prediction carrying `entity` as the object of interest."""
    return {
        "label": "X", "definition": "d", "comment": "c",
        "hasProperty": "count", "hasObjectOfInterest": entity,
        "hasStatisticalModifier": "", "hasMatrix": "", "hasContextObject": "", "hasConstraint": [],
    }


def errors(validator, doc):
    return list(validator.iter_errors(doc))


@pytest.mark.parametrize("demo", DEMOS, ids=[str(d["demonstration"]) for d in DEMOS])
def test_every_measured_decomposition_validates(validator, demo):
    doc = dict(demo["decomposition"], label="X", definition=demo["definition"], comment="c")
    assert errors(validator, doc) == []


def test_the_five_shot_examples_validate_in_the_form_the_prompt_uses(validator):
    """D-009: the stored files are enriched; the prompt builder strips their links first."""
    for path in sorted((BACKEND / "data/Json_preferred/five_shot").glob("*.json")):
        doc = strip_all_uri_fields(json.loads(path.read_text(encoding="utf-8")))
        assert errors(validator, doc) == [], f"{path.name} does not validate"


@pytest.mark.parametrize(
    "entity",
    [
        "water",
        {"AsymmetricSystem": "a → b", "hasSource": "a", "hasTarget": "b"},
        {"AsymmetricSystem": "a / b", "hasNumerator": "a", "hasDenominator": "b"},
        {"SymmetricSystem": "a + b", "hasPart": ["a", "b"]},
        {"SymmetricSystem": "s", "hasPart": ["a"]},  # minItems stays 1, not the lab's 2 (D-005)
    ],
    ids=["string", "source-target", "numerator-denominator", "symmetric", "one-part"],
)
def test_the_accepted_entity_shapes_validate(validator, entity):
    assert errors(validator, wrap(entity)) == []


@pytest.mark.parametrize(
    "entity",
    [
        {"AsymmetricSystem": "x", "hasSource": "a", "hasNumerator": "b"},
        {"AsymmetricSystem": "x", "hasSource": "a", "hasTarget": "b", "hasNumerator": "c", "hasDenominator": "d"},
        {"AsymmetricSystem": "x", "hasSource": "a"},
        {"AsymmetricSystem": "x", "hasSource": "a", "hasTarget": "b", "surprise": "z"},
    ],
    ids=["mixed-roles", "old-five-keys", "incomplete-pair", "unexpected-key"],
)
def test_other_entity_shapes_are_rejected(validator, entity):
    assert errors(validator, wrap(entity)) != []


def test_only_the_five_core_fields_are_required(validator):
    core = {"label": "X", "definition": "d", "comment": "c", "hasProperty": "count", "hasObjectOfInterest": "water"}
    assert errors(validator, core) == []
    for field in core:
        assert errors(validator, {k: v for k, v in core.items() if k != field}) != [], field
