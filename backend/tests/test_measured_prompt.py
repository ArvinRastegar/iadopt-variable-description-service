"""Byte-exactness tests for the measured prompt renderer.

Contract: docs/components/measured-prompt.md
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
    """The test this module exists for."""
    assert mp.render_measured_prompt(TARGET) == GOLDEN


def test_rendered_prompt_is_the_recorded_length():
    assert len(mp.render_measured_prompt(TARGET)) == 15_791


def test_encoded_demonstrations_appear_verbatim_in_the_golden():
    encoded = mp.encode_demonstrations(mp.load_demonstrations())
    assert encoded in GOLDEN


def test_default_separators_would_not_appear_in_the_golden():
    """Guards the previous test against passing vacuously."""
    demos = mp.load_demonstrations()
    assert json.dumps(demos, ensure_ascii=False, sort_keys=True) not in GOLDEN


def test_substitution_is_a_single_pass():
    """A definition containing a placeholder must not be re-expanded."""
    prompt = mp.render_measured_prompt("{{schema}}")
    assert "TARGET DEFINITION\n{{schema}}\n" in prompt
    assert prompt.count('"$id": "https://iadopt-lab.local/schemas/lexical-decomposition-v1"') == 1


def test_prompt_ends_in_exactly_one_newline():
    prompt = mp.render_measured_prompt(TARGET)
    assert prompt.endswith("\n")
    assert not prompt.endswith("\n\n")


def test_schema_text_is_verbatim_including_trailing_newline():
    """D-008: stripping this newline costs a byte the measured prompt has."""
    schema_text = mp.load_lexical_schema_text()
    on_disk = (BACKEND / "data/measured/lexical-decomposition.schema.json").read_text(encoding="utf-8")
    assert schema_text == on_disk
    assert schema_text.endswith("\n")
    assert not schema_text.endswith("\n\n")


def test_rendered_prompt_keeps_the_three_newline_schema_boundary():
    prompt = mp.render_measured_prompt(TARGET)
    assert "}\n\n\nORDERED DEMONSTRATIONS" in prompt


def test_definition_is_inserted_verbatim():
    prompt = mp.render_measured_prompt("  spaced  ")
    assert "TARGET DEFINITION\n  spaced  \n" in prompt


def test_rendering_is_deterministic():
    assert mp.render_measured_prompt(TARGET) == mp.render_measured_prompt(TARGET)


@pytest.mark.parametrize("bad", ["", "   ", "\n", "\t "])
def test_empty_definition_raises_value_error(bad):
    with pytest.raises(ValueError):
        mp.render_measured_prompt(bad)


def test_demonstrations_are_exactly_twenty_five():
    assert len(mp.load_demonstrations()) == 25


def test_missing_template_raises_runtime_error(monkeypatch, tmp_path):
    monkeypatch.setattr(type(mp.settings), "measured_template_path",
                        property(lambda self: tmp_path / "absent.txt"), raising=False)
    with pytest.raises(RuntimeError):
        mp.load_measured_template()


def test_missing_demonstrations_raises_runtime_error(monkeypatch, tmp_path):
    monkeypatch.setattr(type(mp.settings), "measured_demonstrations_path",
                        property(lambda self: tmp_path / "absent.json"), raising=False)
    with pytest.raises(RuntimeError):
        mp.load_demonstrations()


def test_wrong_demonstration_count_raises_runtime_error(monkeypatch, tmp_path):
    short = tmp_path / "short.json"
    short.write_text(json.dumps([{"demonstration": 1, "definition": "d", "decomposition": {}}]), encoding="utf-8")
    monkeypatch.setattr(type(mp.settings), "measured_demonstrations_path",
                        property(lambda self: short), raising=False)
    with pytest.raises(RuntimeError):
        mp.load_demonstrations()


def test_template_missing_placeholder_raises_runtime_error():
    with pytest.raises(RuntimeError):
        mp.render_measured_prompt(TARGET, template="no placeholders here", schema_text="{}", demonstrations=[])


def test_cached_artifacts_produce_the_same_output():
    cached = mp.render_measured_prompt(
        TARGET,
        template=mp.load_measured_template(),
        schema_text=mp.load_lexical_schema_text(),
        demonstrations=mp.load_demonstrations(),
    )
    assert cached == GOLDEN
