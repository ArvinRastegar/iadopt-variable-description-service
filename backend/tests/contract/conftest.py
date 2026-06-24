"""Shared fixtures and helpers for the Phase 0 contract regression suite.

These tests are the behavior baseline captured before the refactor. They come in
two tiers:

* **Golden transform tests** (``test_golden_ttl.py``) exercise the deterministic
  RDF/TTL generation and JSON-Schema validation directly against ``app.main``.
  They make no network calls and run anywhere the backend package is importable
  (e.g. inside the backend container with ``PYTHONPATH=/app``).
* **Live contract tests** (``test_api_contract.py``) replay captured request
  shapes against a running stack and assert the response shapes still match the
  saved fixtures. They are skipped unless ``IADOPT_CONTRACT_BASE_URL`` is set.

Nothing here mutates state on external services; nanopub publish/retract success
paths are intentionally NOT exercised live (see docs/CONTRACTS.md).
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import pytest

CONTRACT_DIR = pathlib.Path(__file__).resolve().parent
API_DIR = CONTRACT_DIR / "api"
GOLDEN_DIR = CONTRACT_DIR / "golden"


def load_json(path: pathlib.Path) -> Any:
    """Read and parse a JSON fixture file."""
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def base_url() -> str:
    """Return the base URL for live contract tests, or skip if not configured."""
    url = os.getenv("IADOPT_CONTRACT_BASE_URL")
    if not url:
        pytest.skip("IADOPT_CONTRACT_BASE_URL not set; skipping live contract tests.")
    return url.rstrip("/")


@pytest.fixture(scope="session")
def auth_cookies(base_url: str):
    """Authenticate once with the bootstrap admin and return session cookies.

    Credentials come from IADOPT_CONTRACT_USERNAME / IADOPT_CONTRACT_PASSWORD.
    """
    import requests  # local import so golden tests don't require requests

    username = os.getenv("IADOPT_CONTRACT_USERNAME")
    password = os.getenv("IADOPT_CONTRACT_PASSWORD")
    if not username or not password:
        pytest.skip("IADOPT_CONTRACT_USERNAME/PASSWORD not set; skipping authed contract tests.")

    resp = requests.post(
        f"{base_url}/api/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.cookies


# Fields whose *contents* are free-form (arbitrary, caller-supplied JSON), or
# whose KEY SET is data-dependent (histograms keyed by whatever actions/models
# happened to be recorded). We assert these are the right container type but do
# not recurse into or compare their keys.
FREEFORM_KEYS = {
    "metadata_json",
    "payload",
    "metadata",
    # Dynamic-key histograms in /admin/stats: keys depend on runtime usage.
    "events_by_action_30d",
    "model_usage_30d",
}


def assert_same_shape(observed: Any, expected: Any, path: str = "$") -> None:
    """Assert two JSON values have the same *structure* (keys and value types).

    Leaf scalar values are compared by type only, not by value, so that
    LLM-dependent or timestamped content does not cause spurious failures. Dict
    key sets must match exactly; lists must be non-empty on both sides and their
    first elements compared recursively. ``None`` on either side acts as a
    wildcard, because many captured fields (e.g. ``latency_ms``, ``status_code``,
    ``error``) are legitimately nullable and vary per request.
    """
    # Nullable fields: a None on either side matches any value of the same slot.
    if observed is None or expected is None:
        return

    assert type(observed) is type(expected), f"{path}: type {type(observed).__name__} != {type(expected).__name__}"

    if isinstance(expected, dict):
        assert set(observed.keys()) == set(expected.keys()), (
            f"{path}: keys {sorted(observed.keys())} != {sorted(expected.keys())}"
        )
        for key in expected:
            # Free-form fields: confirm container type only, don't recurse into keys.
            if key in FREEFORM_KEYS:
                assert type(observed[key]) is type(expected[key]), (
                    f"{path}.{key}: type {type(observed[key]).__name__} != {type(expected[key]).__name__}"
                )
                continue
            assert_same_shape(observed[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        if expected and observed:
            assert_same_shape(observed[0], expected[0], f"{path}[0]")
