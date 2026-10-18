"""Provenance tests for the entity-linking fixtures.

These guard the one failure the golden-request tests cannot catch by themselves: a fixture
edited by hand until a test turns green, which every other test then agrees with. See
``fixtures/PROVENANCE.md`` and ``decisions.md`` D-032 in the private workflow repository.

Everything here reads files committed to this repository, so it always runs.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from app.services import entity_choice

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# Recorded 2026-10-16; see fixtures/PROVENANCE.md.
CHECKSUMS = {
    "entity_link_request_golden.json": "f0207c9df125df550e8a36f173b8d2648926aac6a823813711f94b92e85a6f2c",
    "entity_choice_helpers_golden.json": "92a7f5641605cde4e0ac5e9371859171683986c88996467f6e22e0ab6c222d88",
    "vocabulary_requests_golden.json": "36bcbe32cbc325cfadf1a5128a568be286c188b4c221ea02d9f9bc8b003b59fc",
    "vocabularies/depth.expected.json": "a0058b7e3ad11e92d0e6cff097c7d18a6d99f18ee87732420fcef44adcb38833",
    "vocabularies/depth.wikidata.json": "56590959945f1bb5c71ac0bf33202032a59abe7c4c54666c4929a8fd17af66f7",
    "vocabularies/depth.qudt.json": "04a2731983e23a265a5a82e6f40d04349f3404d57ee6572193fc1014db49319a",
    "vocabularies/depth.om.json": "c018fe9031bd5f17366e4796880293fe70c432be4246dbe002bf0e38620d36a1",
}


@pytest.mark.parametrize("name", sorted(CHECKSUMS))
def test_fixture_is_the_recorded_bytes(name):
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == CHECKSUMS[name]


def test_the_request_golden_records_the_measured_prompt():
    provenance = json.loads((FIXTURES / "entity_link_request_golden.json").read_text(encoding="utf-8"))["provenance"]
    assert provenance["prompt_version"] == entity_choice.PROMPT_VERSION == "psnc-chat-entity-link-v1"
    assert provenance["prompt_fingerprint"].startswith("20d69e9a52e2")
    assert provenance["removed_keys"] == ["logprobs", "top_logprobs"]


def test_every_fixture_the_provenance_note_lists_exists():
    note = (FIXTURES / "PROVENANCE.md").read_text(encoding="utf-8")
    for name in CHECKSUMS:
        assert f"`{name}`" in note and CHECKSUMS[name] in note
