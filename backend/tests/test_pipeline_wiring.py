"""Which runs take the measured path, and how its six fields become the eight-field prediction."""

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


def test_only_psnc_takes_the_measured_path_and_only_while_it_is_switched_on(monkeypatch):
    monkeypatch.setattr(settings, "use_measured_configuration", True)
    assert pipeline.use_measured_path("psnc") is True
    # The study is PSNC-only; the measured prompt must not reach OpenRouter.
    assert pipeline.use_measured_path("openrouter") is False
    monkeypatch.setattr(settings, "use_measured_configuration", False)
    assert pipeline.use_measured_path("psnc") is False


def test_the_merge_adds_the_label_the_comment_and_the_callers_definition():
    polluted = dict(SIX_FIELD, definition="something the model invented")
    merged = pipeline.merge_measured_prediction(polluted, label="L", comment="C", definition=DEFN)
    assert set(merged) == set(SIX_FIELD) | {"label", "definition", "comment"}
    assert (merged["label"], merged["comment"], merged["definition"]) == ("L", "C", DEFN)


def test_the_merge_does_not_change_its_input():
    original = copy.deepcopy(SIX_FIELD)
    pipeline.merge_measured_prediction(SIX_FIELD, label="L", comment="C", definition=DEFN)
    assert SIX_FIELD == original


def test_the_merge_fills_missing_fields_and_flattens_a_dict_property():
    merged = pipeline.merge_measured_prediction(
        {"hasProperty": {"label": "count"}}, label="L", comment="C", definition=DEFN)
    assert merged["hasProperty"] == "count"
    assert (merged["hasConstraint"], merged["hasMatrix"], merged["hasStatisticalModifier"]) == ([], "", "")


def test_warmup_caches_the_measured_artifacts_only_when_switched_on(monkeypatch):
    monkeypatch.setattr(settings, "use_measured_configuration", True)
    pipeline.warmup_assets()
    template, schema_text, demonstrations = pipeline.app_state.measured_assets_cache
    assert "{{demonstrations}}" in template and schema_text.endswith("\n") and len(demonstrations) == 25
    monkeypatch.setattr(settings, "use_measured_configuration", False)
    pipeline.app_state.measured_assets_cache = None
    pipeline.warmup_assets()
    assert pipeline.app_state.measured_assets_cache is None


def test_warmup_stops_when_an_artifact_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "use_measured_configuration", True)
    monkeypatch.setattr(type(settings), "measured_template_path",
                        property(lambda self: tmp_path / "absent.txt"), raising=False)
    with pytest.raises(RuntimeError):
        pipeline.warmup_assets()
