"""The golden prompt and its three artifacts are what the lab measured.

These guard the one failure a byte-exactness test cannot catch by itself: a golden produced wrongly, which the
renderer's test then agrees with. The artifacts (template, schema and the 25 demonstrations) were extracted from the
lab's study document in September: rebuilt in that layout (A) they must still give the prompt printed there. The
golden is the layout-B prompt of D-042: it must rebuild from the same artifacts under layout B's rules, written out
here independently of the app, and carry the checksum of the prompt the 2026-11-18 side test sent. See
``fixtures/PROVENANCE.md``.

The check against the lab document skips when no lab checkout is reachable (CI has none).
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

CHECKSUMS = {
    # Layout B (D-042): the prompt the side test sent for TARGET, and its examples block.
    "prompt": "6b6f4823ca76178b027d1b07436140b9915eb66229a7f6369a07c310beff8da4",
    "examples_block": "691caf47264f071ba5c7dca1ade48c66d8a928e8fa1821f73df727d20708eef3",
    # Recorded 2026-09-14 by backend/tools/extract_measured_artifacts.py.
    "layout_a_prompt": "4cf16c956a267ea1a063ec01b7c8f12db572f2996b305045ad0b4ebc45b2a79b",
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


def layout_b_block() -> str:
    demos = json.loads(read(DEMOS))
    return "\n\n".join(
        f"Example {n}\nDefinition: {d['definition']}\nOutput:\n"
        + json.dumps(ordered(d["decomposition"]), ensure_ascii=False, indent=2)
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


def test_the_artifacts_still_rebuild_the_september_prompt():
    assert sha256(layout_a_prompt()) == CHECKSUMS["layout_a_prompt"]


def test_the_golden_rebuilds_from_the_artifacts_and_is_the_prompt_the_side_test_sent():
    block = layout_b_block()
    assert sha256(block) == CHECKSUMS["examples_block"]
    assert rebuild(block) == read(GOLDEN)
    assert sha256(read(GOLDEN)) == CHECKSUMS["prompt"]


def test_golden_matches_the_lab_source_document():
    """Rebuilt in layout A, as the lab document prints them, the artifacts equal its prompt."""
    doc = lab_document()
    if doc is None:
        pytest.skip("no iadopt-lab checkout reachable; set IADOPT_LAB_PATH to enable")

    match = re.search(
        r"^````text\n(Follow the JSON-Schema exactly\..*?)````\s*$", doc, re.M | re.S
    )
    assert match, "could not locate the section 4 prompt fence in the lab document"

    body = match.group(1)
    assert body.endswith("\n\n"), "prompt fence did not end with the expected two newlines"
    assert body[:-1] == layout_a_prompt()  # strip exactly one newline; the fence contributed the other
