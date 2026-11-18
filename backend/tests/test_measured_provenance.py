"""Provenance tests for the measured-configuration artifacts and the golden prompt.

These guard the one failure a byte-exactness suite cannot catch by itself: a golden
fixture produced wrongly, which every other test then agrees with. See
``backend/tests/fixtures/PROVENANCE.md`` and
``iadopt-variable-description-service-docs/decisions.md`` D-008 and D-042.

The three artifacts (template, schema and the 25 demonstrations) were extracted from the
lab's study document in September. The earlier prompt rebuilt from them in that layout
(layout A) must still equal the prompt printed there. The golden is the layout-B prompt of
D-042: it must rebuild from the same artifacts under the layout-B rules, implemented here
independently of the app, and carry the checksum of the prompt the 2026-11-18 side test
sent. That the side test sent exactly these bytes was verified against the lab's stored call
records, which the lab does not track in git; PROVENANCE.md records it and how to re-check.

Most checks need only files committed to this repo and always run. The check against the
lab document skips when no lab checkout is reachable.
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

GOLDEN_CHARS = 17_393
LAYOUT_A_CHARS = 15_791
EXPECTED_DEMONSTRATIONS = 25

CHECKSUMS = {
    # Layout B (D-042): the prompt the side test sent for TARGET, and its examples block.
    "prompt": "6b6f4823ca76178b027d1b07436140b9915eb66229a7f6369a07c310beff8da4",
    "examples_block": "691caf47264f071ba5c7dca1ade48c66d8a928e8fa1821f73df727d20708eef3",
    # Recorded 2026-09-14 by backend/tools/extract_measured_artifacts.py.
    "layout_a_prompt": "4cf16c956a267ea1a063ec01b7c8f12db572f2996b305045ad0b4ebc45b2a79b",
    "demonstrations": "019b953878d776140edc36862512ef2b7342c402a9303b5d14fe6faf46da8e2d",
    "schema": "a3b5ec8ae6bcde4bd79c5ba29b5f8947ad5537f7b9517b499d053926c34c7a8b",
    "template": "689447d848fc86c94c7c1d32e314a876d288fff2580b8ae49d34cf6a387425f5",
}

# Layout A: the encoding the lab document uses for the demonstrations.
ENCODING = dict(ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))

# Layout B's key order, written out here again so the rebuild does not trust the app.
TOP_KEYS = ("hasStatisticalModifier", "hasProperty", "hasObjectOfInterest", "hasMatrix",
            "hasContextObject", "hasConstraint")
NESTED_KEYS = ("SymmetricSystem", "hasPart", "AsymmetricSystem", "hasSource", "hasTarget",
               "hasNumerator", "hasDenominator", "label", "on")

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


def ordered(value, order=TOP_KEYS):
    if isinstance(value, dict):
        keys = [k for k in order if k in value] + sorted(k for k in value if k not in order)
        return {k: ordered(value[k], NESTED_KEYS) for k in keys}
    if isinstance(value, list):
        return [ordered(item, NESTED_KEYS) for item in value]
    return value


def layout_b_block(**dumps) -> str:
    demos = json.loads(read(DEMOS))
    return "\n\n".join(
        f"Example {n}\nDefinition: {d['definition']}\nOutput:\n"
        + json.dumps(ordered(d["decomposition"]), **{"ensure_ascii": False, "indent": 2, **dumps})
        for n, d in enumerate(demos, 1)
    )


def rebuild(demonstrations: str) -> str:
    return (
        read(TEMPLATE)
        .replace("{{schema}}", read(SCHEMA))
        .replace("{{demonstrations}}", demonstrations)
        .replace("{{target_definition}}", read(TARGET).rstrip("\n"))
    )


def layout_a_prompt() -> str:
    return rebuild(json.dumps(json.loads(read(DEMOS)), **ENCODING))


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


def test_artifacts_still_rebuild_the_september_prompt():
    """The extracted artifacts are intact: in layout A they rebuild the recorded prompt."""
    prompt = layout_a_prompt()
    assert sha256(prompt) == CHECKSUMS["layout_a_prompt"]
    assert len(prompt) == LAYOUT_A_CHARS


def test_golden_is_the_recorded_length_and_ends_in_one_newline():
    golden = read(GOLDEN)
    assert len(golden) == GOLDEN_CHARS
    assert golden.endswith("\n")
    assert not golden.endswith("\n\n")


def test_golden_rebuilds_from_the_committed_artifacts():
    """The golden must be derivable from template + schema + demonstrations.

    This is the check that makes the fixture verifiable rather than merely asserted: the
    prompt is reconstructed from its three independent sources with layout B's rules,
    implemented in this file, and compared, so a wrongly produced golden cannot agree
    with itself.
    """
    block = layout_b_block()
    assert sha256(block) == CHECKSUMS["examples_block"]
    assert rebuild(block) == read(GOLDEN)


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


def test_sorted_keys_would_not_rebuild_the_golden():
    """Proves the key order is checked: the same layout with sorted keys is another prompt."""
    assert rebuild(layout_b_block(sort_keys=True)) != read(GOLDEN)


# --------------------------------------------------------------------------- #
# Source checks: run only with a lab checkout
# --------------------------------------------------------------------------- #

def test_golden_matches_the_lab_source_document():
    """Compare the golden's sources against the prompt printed in the lab document itself.

    The artifacts the golden is rebuilt from are checked against the document they were
    extracted from: rebuilt in layout A, as the document prints them, they must equal its
    prompt. This does not trust the committed checksums.
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

    assert from_document == layout_a_prompt()
    assert len(from_document) == LAYOUT_A_CHARS
