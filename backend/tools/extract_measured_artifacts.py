#!/usr/bin/env python3
"""Regenerate the measured-configuration artifacts from the lab source document.

Every file this writes is a mechanical extraction from one source of truth:

    iadopt-lab/few-shot-selection/example_selection/best-configuration-session.md

Nothing here is hand-transcribed, so the artifacts can be re-derived and compared
byte-for-byte at any time. Run it with the lab checkout reachable::

    python backend/tools/extract_measured_artifacts.py --lab /path/to/iadopt-lab

It refuses to write anything unless every self-check below passes, so a partial or
drifted extraction fails loudly instead of silently shipping a wrong baseline:

* the demonstrations array re-encodes to exactly the bytes found in the document
* the schema block inside the prompt is byte-identical to the lab schema file
* the template skeleton around the three substitutions is byte-identical to the
  lab template file
* the rendered prompt is exactly 15,791 characters, as the document records

See ``backend/tests/fixtures/PROVENANCE.md`` for the recorded checksums and
``backend/tests/test_measured_provenance.py`` for the tests that re-verify them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

DOC_RELPATH = "few-shot-selection/example_selection/best-configuration-session.md"
TEMPLATE_RELPATH = "prompts/matrix-decomposition-v1.txt"
SCHEMA_RELPATH = "schemas/lexical-decomposition.schema.json"

EXPECTED_PROMPT_CHARS = 15_791
EXPECTED_DEMONSTRATIONS = 25

ENCODING_KWARGS = dict(ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def sha256(text: str) -> str:
    """Return the hex SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_prompt(doc: str) -> str:
    """Pull the verbatim rendered prompt out of section 4 of the document.

    The prompt sits in a four-backtick ``text`` fence. The prompt itself ends in a
    single newline and the closing fence contributes another, so exactly one
    trailing newline is stripped -- the caveat the document states in section 4.

    Args:
        doc: Full text of ``best-configuration-session.md``.

    Returns:
        The prompt, ending in exactly one newline.

    Raises:
        RuntimeError: If the fence cannot be located.
    """
    match = re.search(
        r"^````text\n(Follow the JSON-Schema exactly\..*?)````\s*$",
        doc,
        re.M | re.S,
    )
    if not match:
        raise RuntimeError("Could not locate the section 4 prompt fence.")

    body = match.group(1)
    if not body.endswith("\n\n"):
        raise RuntimeError("Prompt fence did not end with the expected two newlines.")
    return body[:-1]


def extract_demonstrations(prompt: str) -> tuple[list, str]:
    """Pull the demonstrations array out of the rendered prompt.

    Args:
        prompt: The verbatim rendered prompt.

    Returns:
        A ``(parsed, raw_line)`` tuple.

    Raises:
        RuntimeError: If the block is absent or is not exactly 25 entries.
    """
    match = re.search(r"^ORDERED DEMONSTRATIONS\n(\[.*?\])\n", prompt, re.M | re.S)
    if not match:
        raise RuntimeError("Could not locate the ORDERED DEMONSTRATIONS block.")

    raw_line = match.group(1)
    parsed = json.loads(raw_line)

    if len(parsed) != EXPECTED_DEMONSTRATIONS:
        raise RuntimeError(f"Expected {EXPECTED_DEMONSTRATIONS} demonstrations, found {len(parsed)}.")
    if [d["demonstration"] for d in parsed] != list(range(1, EXPECTED_DEMONSTRATIONS + 1)):
        raise RuntimeError("Demonstration numbers are not 1..25 in order.")

    return parsed, raw_line


def extract_schema_text(prompt: str) -> str:
    """Pull the schema block out of the rendered prompt (between its two markers)."""
    match = re.search(r"^LEXICAL JSON SCHEMA\n(\{.*?\})\n\n\nORDERED DEMONSTRATIONS$", prompt, re.M | re.S)
    if not match:
        raise RuntimeError("Could not locate the LEXICAL JSON SCHEMA block.")
    return match.group(1)


def extract_target_definition(prompt: str) -> str:
    """Pull the target definition out of the rendered prompt."""
    match = re.search(r"^TARGET DEFINITION\n(.*?)\n\nReturn exactly one JSON object", prompt, re.M | re.S)
    if not match:
        raise RuntimeError("Could not locate the TARGET DEFINITION block.")
    return match.group(1)


def main() -> int:
    """Extract, self-check, and write every measured artifact. Returns an exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab", required=True, type=pathlib.Path, help="Path to the iadopt-lab checkout")
    parser.add_argument("--check", action="store_true", help="Verify only; write nothing")
    args = parser.parse_args()

    lab = args.lab.expanduser().resolve()
    repo = pathlib.Path(__file__).resolve().parents[2]
    out_data = repo / "backend" / "data" / "measured"
    out_fixtures = repo / "backend" / "tests" / "fixtures"

    doc_path = lab / DOC_RELPATH
    if not doc_path.exists():
        print(f"error: source document not found: {doc_path}", file=sys.stderr)
        return 2

    doc = doc_path.read_text(encoding="utf-8")
    prompt = extract_prompt(doc)
    demonstrations, raw_demo_line = extract_demonstrations(prompt)
    schema_text = extract_schema_text(prompt)
    target_definition = extract_target_definition(prompt)

    lab_template = (lab / TEMPLATE_RELPATH).read_text(encoding="utf-8")
    lab_schema = (lab / SCHEMA_RELPATH).read_text(encoding="utf-8")

    # --- self-checks: each one independently pins a different part of the extraction ---
    problems = []

    if json.dumps(demonstrations, **ENCODING_KWARGS) != raw_demo_line:
        problems.append("demonstrations do not re-encode to the bytes found in the document")

    if schema_text != lab_schema.rstrip("\n"):
        problems.append("prompt schema block differs from the lab schema file")

    # D-008: {{schema}} carries the schema file's own trailing newline. The measured
    # prompt has three newlines between the closing brace and ORDERED DEMONSTRATIONS;
    # the template supplies two and the schema file the third.
    rebuilt = (
        lab_template
        .replace("{{schema}}", lab_schema)
        .replace("{{demonstrations}}", raw_demo_line)
        .replace("{{target_definition}}", target_definition)
    )
    if rebuilt != prompt:
        problems.append("lab template does not rebuild the document's prompt")

    if len(prompt) != EXPECTED_PROMPT_CHARS:
        problems.append(f"prompt is {len(prompt)} chars, document records {EXPECTED_PROMPT_CHARS}")

    if not prompt.endswith("\n") or prompt.endswith("\n\n"):
        problems.append("prompt does not end in exactly one newline")

    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 1

    print("All self-checks passed.")
    print(f"  prompt           {len(prompt)} chars   sha256 {sha256(prompt)}")
    print(f"  demonstrations   {len(raw_demo_line)} chars   sha256 {sha256(raw_demo_line)}")
    print(f"  schema           {len(lab_schema)} chars   sha256 {sha256(lab_schema)}")
    print(f"  template         {len(lab_template)} chars   sha256 {sha256(lab_template)}")
    print(f"  target definition: {target_definition!r}")

    if args.check:
        return 0

    out_data.mkdir(parents=True, exist_ok=True)
    out_fixtures.mkdir(parents=True, exist_ok=True)

    (out_data / "matrix-decomposition-v1.txt").write_text(lab_template, encoding="utf-8")
    (out_data / "lexical-decomposition.schema.json").write_text(lab_schema, encoding="utf-8")
    (out_data / "demonstrations-top25.json").write_text(
        json.dumps(demonstrations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_fixtures / "measured_prompt_golden.txt").write_text(prompt, encoding="utf-8")
    (out_fixtures / "measured_target_definition.txt").write_text(target_definition + "\n", encoding="utf-8")

    print(f"\nWrote artifacts to {out_data} and fixtures to {out_fixtures}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
