"""Tests for measured-path selection and the six-to-eight field merge.

Contract: docs/components/pipeline-wiring.md
"""

from __future__ import annotations

import copy

import pytest

from app import pipeline
from app.core.config import settings

SIX_FIELD = {
    "hasStatisticalModifier": "", "hasProperty": "count",
    "hasObjectOfInterest": "person", "hasMatrix": "",
    "hasContextObject": "urban area",
    "hasConstraint": [{"label": "condition: receiving welfare", "on": "person"}],
}
DEFN = "number of persons receiving welfare in a statistical unit"


def _set_flag(monkeypatch, value):
    """Set the measured-configuration flag, failing clearly if the field is absent.

    Before `measured-config` is implemented pydantic rejects the assignment; a
    plain failure names the reason instead of erroring during fixture setup.
    """
    try:
        monkeypatch.setattr(settings, "use_measured_configuration", value)
    except (ValueError, AttributeError):
        pytest.fail("Settings has no `use_measured_configuration` field yet (measured-config unimplemented)")


@pytest.fixture
def measured_on(monkeypatch):
    _set_flag(monkeypatch, True)


@pytest.fixture
def measured_off(monkeypatch):
    _set_flag(monkeypatch, False)


# --- path selection ---------------------------------------------------------- #

def test_psnc_takes_the_measured_path(measured_on):
    assert pipeline.use_measured_path("psnc") is True


def test_openrouter_never_takes_the_measured_path(measured_on):
    """The study is PSNC-only; the measured prompt must not reach OpenRouter."""
    assert pipeline.use_measured_path("openrouter") is False


def test_flag_off_disables_the_measured_path(measured_off):
    assert pipeline.use_measured_path("psnc") is False


# --- the merge --------------------------------------------------------------- #

def test_merged_prediction_has_the_eight_expected_keys():
    merged = pipeline.merge_measured_prediction(SIX_FIELD, label="L", comment="C", definition=DEFN)
    assert set(merged) == {
        "hasStatisticalModifier", "hasProperty", "hasObjectOfInterest", "hasMatrix",
        "hasContextObject", "hasConstraint", "label", "definition", "comment",
    }


def test_definition_is_the_callers_not_the_models():
    polluted = dict(SIX_FIELD, definition="something the model invented")
    merged = pipeline.merge_measured_prediction(polluted, label="L", comment="C", definition=DEFN)
    assert merged["definition"] == DEFN


def test_label_and_comment_are_carried_through():
    merged = pipeline.merge_measured_prediction(SIX_FIELD, label="L", comment="C", definition=DEFN)
    assert merged["label"] == "L"
    assert merged["comment"] == "C"


def test_merge_does_not_mutate_its_input():
    original = copy.deepcopy(SIX_FIELD)
    pipeline.merge_measured_prediction(SIX_FIELD, label="L", comment="C", definition=DEFN)
    assert SIX_FIELD == original


def test_merge_applies_existing_coercion_defaults():
    """Missing onto keys get the same defaults coerce_prediction already gives."""
    merged = pipeline.merge_measured_prediction(
        {"hasProperty": "count"}, label="L", comment="C", definition=DEFN)
    assert merged["hasConstraint"] == []
    assert merged["hasMatrix"] == ""
    assert merged["hasStatisticalModifier"] == ""


def test_merge_flattens_a_dict_has_property():
    merged = pipeline.merge_measured_prediction(
        dict(SIX_FIELD, hasProperty={"label": "count"}), label="L", comment="C", definition=DEFN)
    assert merged["hasProperty"] == "count"


# --- warmup ------------------------------------------------------------------ #

def test_warmup_caches_measured_assets_when_enabled(measured_on):
    pipeline.warmup_assets()
    cache = pipeline.app_state.measured_assets_cache
    assert cache is not None
    template, schema_text, demonstrations = cache
    assert "{{demonstrations}}" in template
    assert schema_text.endswith("\n")
    assert len(demonstrations) == 25


def test_warmup_skips_measured_assets_when_disabled(measured_off):
    pipeline.app_state.measured_assets_cache = None
    pipeline.warmup_assets()
    assert pipeline.app_state.measured_assets_cache is None


def test_warmup_raises_when_an_artifact_is_missing(measured_on, monkeypatch, tmp_path):
    monkeypatch.setattr(type(settings), "measured_template_path",
                        property(lambda self: tmp_path / "absent.txt"), raising=False)
    with pytest.raises(RuntimeError):
        pipeline.warmup_assets()
