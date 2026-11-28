"""The measured prompt renderer reproduces the prompt the lab measured, byte for byte.

The golden is the layout-B prompt of D-042 for one target definition. That the golden itself is right is checked in
``test_measured_provenance.py``.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.services import measured_prompt as mp

BACKEND = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = (BACKEND / "tests/fixtures/measured_prompt_golden.txt").read_text(encoding="utf-8")
TARGET = (BACKEND / "tests/fixtures/measured_target_definition.txt").read_text(encoding="utf-8").rstrip("\n")


def test_rendering_reproduces_the_golden_prompt_byte_for_byte():
    assert mp.render_measured_prompt(TARGET) == GOLDEN
    # The warm-up path passes the artifacts it cached instead of loading them.
    cached = mp.render_measured_prompt(
        TARGET,
        template=mp.load_measured_template(),
        schema_text=mp.load_lexical_schema_text(),
        demonstrations=mp.load_demonstrations(),
    )
    assert cached == GOLDEN


def test_the_definition_is_inserted_verbatim_and_never_expanded_again():
    assert "TARGET DEFINITION\n  spaced  \n" in mp.render_measured_prompt("  spaced  ")
    prompt = mp.render_measured_prompt("{{schema}}")
    assert "TARGET DEFINITION\n{{schema}}\n" in prompt
    assert prompt.count('"$id": "https://iadopt-lab.local/schemas/lexical-decomposition-v1"') == 1


@pytest.mark.parametrize("bad", ["", " \n\t"])
def test_an_empty_definition_is_refused(bad):
    with pytest.raises(ValueError):
        mp.render_measured_prompt(bad)


def test_a_missing_artifact_stops_with_a_runtime_error(monkeypatch, tmp_path):
    monkeypatch.setattr(type(mp.settings), "measured_template_path",
                        property(lambda self: tmp_path / "absent.txt"), raising=False)
    with pytest.raises(RuntimeError):
        mp.load_measured_template()


def test_a_wrong_number_of_demonstrations_stops_with_a_runtime_error(monkeypatch, tmp_path):
    short = tmp_path / "short.json"
    short.write_text(json.dumps([{"demonstration": 1, "definition": "d", "decomposition": {}}]), encoding="utf-8")
    monkeypatch.setattr(type(mp.settings), "measured_demonstrations_path",
                        property(lambda self: short), raising=False)
    with pytest.raises(RuntimeError):
        mp.load_demonstrations()


def test_a_template_without_its_placeholders_stops_with_a_runtime_error():
    with pytest.raises(RuntimeError):
        mp.render_measured_prompt(TARGET, template="no placeholders here", schema_text="{}", demonstrations=[])
