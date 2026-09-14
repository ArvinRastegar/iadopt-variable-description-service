"""Tests for the entityOrSystem shapes the validation schema accepts.

Contract: docs/components/lexical-schema.md — D-005.
Observed through the real seam: Draft202012Validator over the pipeline-patched schema.
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


# --- the measured corpus ---------------------------------------------------- #

@pytest.mark.parametrize("demo", DEMOS, ids=[str(d["demonstration"]) for d in DEMOS])
def test_every_measured_decomposition_validates(validator, demo):
    """Currently 10 of 25 fail: 8 numerator/denominator, 2 source/target."""
    doc = dict(demo["decomposition"], label="X", definition=demo["definition"], comment="c")
    assert errors(validator, doc) == []


def test_service_five_shot_examples_validate_as_the_prompt_uses_them(validator):
    """D-009: the stored files are enriched; the schema is pre-enrichment.

    `strip_all_uri_fields` is what the prompt builder applies, so that is the form
    the schema must accept. Asserting the stored form validates would assert that
    enrichment output must satisfy the pre-enrichment schema.
    """
    for path in sorted((BACKEND / "data/Json_preferred/five_shot").glob("*.json")):
        doc = strip_all_uri_fields(json.loads(path.read_text(encoding="utf-8")))
        assert errors(validator, doc) == [], f"{path.name} does not validate"


def test_stored_five_shot_examples_carry_uri_keys(validator):
    """Guards the test above from passing vacuously if the URIs ever disappear."""
    for path in sorted((BACKEND / "data/Json_preferred/five_shot").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert any("URI" in key for key in doc), f"{path.name} has no URI keys to strip"
        assert errors(validator, doc) != [], f"{path.name} unexpectedly validates while enriched"


# --- the four accepted shapes ----------------------------------------------- #

def test_source_target_pair_validates(validator):
    assert errors(validator, wrap({"AsymmetricSystem": "a → b", "hasSource": "a", "hasTarget": "b"})) == []


def test_numerator_denominator_pair_validates(validator):
    assert errors(validator, wrap({"AsymmetricSystem": "a / b", "hasNumerator": "a", "hasDenominator": "b"})) == []


def test_symmetric_system_validates(validator):
    assert errors(validator, wrap({"SymmetricSystem": "a + b", "hasPart": ["a", "b"]})) == []


def test_plain_string_entity_validates(validator):
    assert errors(validator, wrap("water")) == []


# --- the shapes that must be rejected --------------------------------------- #

def test_mixed_role_pairs_are_rejected(validator):
    entity = {"AsymmetricSystem": "x", "hasSource": "a", "hasNumerator": "b"}
    assert errors(validator, wrap(entity)) != []


def test_old_five_key_shape_is_rejected(validator):
    entity = {"AsymmetricSystem": "x", "hasSource": "a", "hasTarget": "b",
              "hasNumerator": "c", "hasDenominator": "d"}
    assert errors(validator, wrap(entity)) != []


def test_incomplete_pair_is_rejected(validator):
    assert errors(validator, wrap({"AsymmetricSystem": "x", "hasSource": "a"})) != []


def test_unexpected_key_is_rejected(validator):
    entity = {"AsymmetricSystem": "x", "hasSource": "a", "hasTarget": "b", "surprise": "z"}
    assert errors(validator, wrap(entity)) != []


# --- deliberate non-changes -------------------------------------------------- #

def test_single_part_symmetric_system_still_validates(validator):
    """hasPart.minItems stays 1, not the lab's 2 — D-005."""
    assert errors(validator, wrap({"SymmetricSystem": "s", "hasPart": ["a"]})) == []


def test_top_level_required_fields_are_unchanged(validator):
    schema = patch_schema_for_pipeline(load_schema(settings.schema_path))
    assert set(schema["required"]) == {
        "label", "definition", "comment", "hasProperty", "hasObjectOfInterest"
    }
