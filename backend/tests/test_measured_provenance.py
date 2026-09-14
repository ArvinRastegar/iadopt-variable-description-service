"""Provenance tests for the measured-configuration artifacts.

These guard the one failure a byte-exactness suite cannot catch by itself: a
golden fixture extracted wrongly, which every other test then agrees with. See
``backend/tests/fixtures/PROVENANCE.md`` and ``docs/decisions.md`` D-008.

Three of the four checks need only files committed to this repo and always run.
The fourth compares against the lab source document and skips when no lab checkout
is reachable.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
MEASURED = BACKEND / "data" / "measured"
FIXTURES = BACKEND / "tests" / "fixtures"

TEMPLATE = MEASURED / "matrix-decomposition-v1.txt"
SCHEMA = MEASURED / "lexical-decomposition.schema.json"
DEMOS = MEASURED / "demonstrations-top25.json"
GOLDEN = FIXTURES / "measured_prompt_golden.txt"
TARGET = FIXTURES / "measured_target_definition.txt"

EXPECTED_PROMPT_CHARS = 15_791
EXPECTED_DEMONSTRATIONS = 25

# Recorded 2026-09-14 by backend/tools/extract_measured_artifacts.py.
CHECKSUMS = {
    "prompt": "4cf16c956a267ea1a063ec01b7c8f12db572f2996b305045ad0b4ebc45b2a79b",
    "demonstrations": "019b953878d776140edc36862512ef2b7342c402a9303b5d14fe6faf46da8e2d",
    "schema": "a3b5ec8ae6bcde4bd79c5ba29b5f8947ad5537f7b9517b499d053926c34c7a8b",
    "template": "689447d848fc86c94c7c1d32e314a876d288fff2580b8ae49d34cf6a387425f5",
}

ENCODING = dict(ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))

DEFAULT_LAB = pathlib.Path.home() / "Documents/GitHub/i-adopt-llm-based-service/iadopt-lab"
DOC_RELPATH = "few-shot-selection/example_selection/best-configuration-session.md"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def lab_document() -> str | None:
    """Return the lab source document text, or None when no checkout is reachable."""
    configured = os.environ.get("IADOPT_LAB_PATH")
    root = pathlib.Path(configured).expanduser() if configured else DEFAULT_LAB
    doc = root / DOC_RELPATH
    return read(doc) if doc.exists() else None


# --------------------------------------------------------------------------- #
# Always-on checks: repo files only
# --------------------------------------------------------------------------- #

def test_every_artifact_exists():
    for path in (TEMPLATE, SCHEMA, DEMOS, GOLDEN, TARGET):
        assert path.exists(), f"missing artifact: {path}"


@pytest.mark.parametrize(
    "name,path",
    [("prompt", GOLDEN), ("schema", SCHEMA), ("template", TEMPLATE)],
)
def test_recorded_checksum_matches(name, path):
    """A drifted artifact fails here before it can reach the byte-exactness tests."""
    assert sha256(read(path)) == CHECKSUMS[name], f"{name} no longer matches its recorded SHA-256"


def test_demonstrations_encode_to_the_recorded_checksum():
    encoded = json.dumps(json.loads(read(DEMOS)), **ENCODING)
    assert sha256(encoded) == CHECKSUMS["demonstrations"]


def test_golden_is_the_recorded_length_and_ends_in_one_newline():
    golden = read(GOLDEN)
    assert len(golden) == EXPECTED_PROMPT_CHARS
    assert golden.endswith("\n")
    assert not golden.endswith("\n\n")


def test_golden_rebuilds_from_the_committed_artifacts():
    """The golden must be derivable from template + schema + demonstrations.

    This is the check that makes the fixture verifiable rather than merely
    asserted: the prompt is reconstructed from its three independent sources and
    compared, so a wrongly-extracted golden cannot agree with itself.
    """
    rebuilt = (
        read(TEMPLATE)
        .replace("{{schema}}", read(SCHEMA))
        .replace("{{demonstrations}}", json.dumps(json.loads(read(DEMOS)), **ENCODING))
        .replace("{{target_definition}}", read(TARGET).rstrip("\n"))
    )
    assert rebuilt == read(GOLDEN)


def test_golden_keeps_the_three_newline_schema_boundary():
    """D-008: the schema's own trailing newline is inside the prompt."""
    golden = read(GOLDEN)
    assert "}\n\n\nORDERED DEMONSTRATIONS" in golden
    assert "}\n\nORDERED DEMONSTRATIONS" not in golden


def test_schema_artifact_ends_in_exactly_one_newline():
    schema = read(SCHEMA)
    assert schema.endswith("\n")
    assert not schema.endswith("\n\n")


def test_demonstrations_are_twenty_five_numbered_in_order():
    demos = json.loads(read(DEMOS))
    assert len(demos) == EXPECTED_DEMONSTRATIONS
    assert [d["demonstration"] for d in demos] == list(range(1, EXPECTED_DEMONSTRATIONS + 1))
    for d in demos:
        assert set(d) == {"demonstration", "definition", "decomposition"}


def test_template_has_each_placeholder_exactly_once():
    template = read(TEMPLATE)
    for placeholder in ("{{schema}}", "{{demonstrations}}", "{{target_definition}}"):
        assert template.count(placeholder) == 1


def test_default_json_separators_would_not_match():
    """Proves the encoding assertions would actually catch a regression.

    Without this, a test comparing two values both produced with default spacing
    would pass while the prompt was wrong.
    """
    demos = json.loads(read(DEMOS))
    default_spacing = json.dumps(demos, ensure_ascii=False, sort_keys=True)
    assert sha256(default_spacing) != CHECKSUMS["demonstrations"]
    assert default_spacing not in read(GOLDEN)


# --------------------------------------------------------------------------- #
# Source-document check: runs only with a lab checkout
# --------------------------------------------------------------------------- #

def test_golden_matches_the_lab_source_document():
    """Compare the fixture against the prompt printed in the lab document itself.

    This is the provenance check proper: it does not trust the committed
    artifacts, it re-reads the document those artifacts were extracted from.
    """
    doc = lab_document()
    if doc is None:
        pytest.skip("no iadopt-lab checkout reachable; set IADOPT_LAB_PATH to enable")

    match = re.search(
        r"^````text\n(Follow the JSON-Schema exactly\..*?)````\s*$", doc, re.M | re.S
    )
    assert match, "could not locate the section 4 prompt fence in the lab document"

    body = match.group(1)
    assert body.endswith("\n\n"), "prompt fence did not end with the expected two newlines"
    from_document = body[:-1]  # strip exactly one newline; the fence contributed the other

    assert from_document == read(GOLDEN)
    assert len(from_document) == EXPECTED_PROMPT_CHARS
