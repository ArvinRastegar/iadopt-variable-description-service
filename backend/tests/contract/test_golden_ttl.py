"""Golden tests for the deterministic transforms: RDF/TTL generation + validation.

These pin the byte-for-byte output of ``json_to_ttl_repo_style`` and the result
of ``get_schema_validation_errors`` / ``_get_constraint_semantic_validation_errors``
for a small set of representative inputs. They are the regression net that the
Phase 2 module move (rdf_ttl.py, validation.py) must keep green.

Determinism: ``json_to_ttl_repo_style`` reads the wall clock and ``random`` when
building the variable identity (see ``_make_variable_identity``). Both are frozen
here so the output is reproducible. ORCID display-name lookup (a live HTTP call)
is monkeypatched to a constant so the tests make no network calls.

Run inside the backend container:

    docker compose exec -T -w /app -e PYTHONPATH=/app backend \
        python -m pytest tests/contract/test_golden_ttl.py -q
"""

from __future__ import annotations

import datetime as _dt
import pathlib

import pytest

import app.services.rdf_ttl as rdf_ttl  # type: ignore[import-not-found]
import app.services.validation as validation  # type: ignore[import-not-found]

from conftest import GOLDEN_DIR, load_json

FROZEN_NOW = _dt.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=_dt.timezone.utc)
FROZEN_RANDINT = 7
CREATOR_ORCID = "0000-0003-2195-3997"

GOLDEN_CASES = ["simple_air_temperature", "asymmetric_soil_moisture"]


@pytest.fixture(autouse=True)
def freeze_nondeterminism(monkeypatch):
    """Freeze clock + RNG + ORCID lookup so TTL generation is byte-reproducible.

    The variable-identity clock/RNG live in app.services.rdf_ttl, and ORCID name
    resolution lives in app.services.orcid; patch each where it is used so the
    generated TTL is deterministic and makes no real HTTP lookup.
    """
    import app.services.orcid as orcid_service
    import app.services.rdf_ttl as rdf_ttl

    monkeypatch.setattr(
        rdf_ttl,
        "datetime",
        type("dt", (), {"now": staticmethod(lambda tz=None: FROZEN_NOW), "timezone": _dt.timezone}),
    )
    monkeypatch.setattr(rdf_ttl.random, "randint", lambda a, b: FROZEN_RANDINT)
    monkeypatch.setattr(orcid_service, "lookup_orcid_display_name", lambda orcid: "Test Creator")


@pytest.mark.parametrize("case", GOLDEN_CASES)
def test_ttl_matches_golden(case: str):
    """The generated TTL must match the saved golden byte-for-byte."""
    pred = load_json(GOLDEN_DIR / f"{case}.input.json")
    expected_ttl = (GOLDEN_DIR / f"{case}.expected.ttl").read_text(encoding="utf-8")

    ttl = rdf_ttl.json_to_ttl_repo_style(pred, creator_orcid_id=CREATOR_ORCID)

    assert ttl == expected_ttl


@pytest.mark.parametrize("case", GOLDEN_CASES)
def test_ttl_is_deterministic(case: str):
    """Regenerating with frozen clock/RNG yields identical output."""
    pred = load_json(GOLDEN_DIR / f"{case}.input.json")
    first = rdf_ttl.json_to_ttl_repo_style(pred, creator_orcid_id=CREATOR_ORCID)
    second = rdf_ttl.json_to_ttl_repo_style(pred, creator_orcid_id=CREATOR_ORCID)
    assert first == second


@pytest.mark.parametrize("case", GOLDEN_CASES)
def test_validation_matches_golden(case: str):
    """Schema + semantic validation result must match the saved golden."""
    pred = load_json(GOLDEN_DIR / f"{case}.input.json")
    expected = load_json(GOLDEN_DIR / f"{case}.validation.json")

    errors = validation.get_schema_validation_errors(pred, label_for_logs=pred.get("label"))
    errors = errors + validation.get_constraint_semantic_validation_errors(pred)

    assert (len(errors) == 0) == expected["schema_valid"]
    assert errors == expected["validation_errors"]
