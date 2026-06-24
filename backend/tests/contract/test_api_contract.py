"""Live API contract regression tests (replay captured shapes against a stack).

These tests are SKIPPED unless ``IADOPT_CONTRACT_BASE_URL`` points at a running
deployment (e.g. ``http://localhost:5173`` for the docker-compose stack, which
proxies ``/api`` to the backend through nginx). Authed tests additionally need
``IADOPT_CONTRACT_USERNAME`` / ``IADOPT_CONTRACT_PASSWORD``.

They assert that each endpoint still returns the response *shape* captured at the
Phase 0 baseline. Scalar leaf values are compared by type, not value, so that
LLM output, timestamps, and counts do not cause spurious failures.

Coverage and deliberate exclusions:
* Read-only / deterministic routes: asserted fully.
* ``/api/decompose`` and ``/api/decompose/stream``: shape-asserted (these make a
  real LLM call; enabled only when ``IADOPT_CONTRACT_RUN_LLM=1``).
* ``/api/nanopub/publish`` and ``/api/nanopub/retract``: only the 401/422 error
  paths are asserted. The success paths write irreversibly to the public nanopub
  registry and are NEVER exercised here (see docs/CONTRACTS.md).
"""

from __future__ import annotations

import json
import os

import pytest

from conftest import API_DIR, assert_same_shape, load_json

requests = pytest.importorskip("requests")


# --------------------------------------------------------------------------- #
# Public (unauthenticated) routes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,fixture",
    [
        ("/api/livez", "livez.200.json"),
        ("/api/readyz", "readyz.200.json"),
        ("/api/health", "health.200.json"),
    ],
)
def test_public_health_routes(base_url, path, fixture):
    """Health routes are public and return their captured shape."""
    resp = requests.get(f"{base_url}{path}", timeout=30)
    assert resp.status_code == 200
    assert_same_shape(resp.json(), load_json(API_DIR / fixture))


def test_unauth_is_401(base_url):
    """A protected route without a session returns the captured 401 shape."""
    resp = requests.get(f"{base_url}/api/model-options", timeout=30)
    assert resp.status_code == 401
    assert_same_shape(resp.json(), load_json(API_DIR / "_401_unauth.json"))


# --------------------------------------------------------------------------- #
# Authenticated GET routes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,fixture",
    [
        ("/api/auth/me", "auth_me.200.json"),
        ("/api/model-options", "model_options.200.json"),
        ("/api/nanopub/preparation-options", "nanopub_preparation_options.200.json"),
        ("/api/admin/users", "admin_users.200.json"),
        ("/api/admin/stats", "admin_stats.200.json"),
    ],
)
def test_authed_get_routes(base_url, auth_cookies, path, fixture):
    """Authenticated GET routes return their captured shape."""
    resp = requests.get(f"{base_url}{path}", cookies=auth_cookies, timeout=30)
    assert resp.status_code == 200
    assert_same_shape(resp.json(), load_json(API_DIR / fixture))


def test_auth_verify_returns_204(base_url, auth_cookies):
    """The nginx auth_request endpoint returns 204 No Content when authed."""
    resp = requests.get(f"{base_url}/api/auth/verify", cookies=auth_cookies, timeout=30)
    assert resp.status_code == 204
    assert resp.text == ""


# --------------------------------------------------------------------------- #
# Validation error shapes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,body,fixture",
    [
        ("/api/decompose", {"definition": ""}, "decompose.422_empty_definition.json"),
        ("/api/decompose", {}, "decompose.422_missing_definition.json"),
        ("/api/nanopub/publish", {}, "nanopub_publish.422_missing_ttl.json"),
        ("/api/nanopub/retract", {}, "nanopub_retract.422_missing_uri.json"),
    ],
)
def test_validation_error_shapes(base_url, auth_cookies, path, body, fixture):
    """422 validation errors keep the FastAPI detail-list shape."""
    resp = requests.post(f"{base_url}{path}", cookies=auth_cookies, json=body, timeout=30)
    assert resp.status_code == 422
    assert_same_shape(resp.json(), load_json(API_DIR / fixture))


# --------------------------------------------------------------------------- #
# LLM-dependent routes (opt-in)
# --------------------------------------------------------------------------- #
RUN_LLM = os.getenv("IADOPT_CONTRACT_RUN_LLM") == "1"
SAMPLE_DEFINITION = "Maximum daily air temperature at 2 meters above ground level"


@pytest.mark.skipif(not RUN_LLM, reason="set IADOPT_CONTRACT_RUN_LLM=1 to exercise real LLM calls")
def test_decompose_shape(base_url, auth_cookies):
    """Non-streaming decompose returns the DecomposeResponse shape."""
    resp = requests.post(
        f"{base_url}/api/decompose",
        cookies=auth_cookies,
        json={"definition": SAMPLE_DEFINITION},
        timeout=300,
    )
    assert resp.status_code == 200
    assert_same_shape(resp.json(), load_json(API_DIR / "decompose.200.json"))


@pytest.mark.skipif(not RUN_LLM, reason="set IADOPT_CONTRACT_RUN_LLM=1 to exercise real LLM calls")
def test_decompose_stream_event_sequence(base_url, auth_cookies):
    """The NDJSON stream is N raw_delta events followed by exactly one final event."""
    resp = requests.post(
        f"{base_url}/api/decompose/stream",
        cookies=auth_cookies,
        json={"definition": SAMPLE_DEFINITION},
        timeout=300,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    types = [e["type"] for e in events]
    assert set(types) <= {"raw_delta", "final", "error"}
    assert types[-1] in {"final", "error"}
    if types[-1] == "final":
        assert types.count("final") == 1
        assert {"raw_llm_output", "parsed_json", "schema_valid", "validation_errors", "enriched_json", "ttl"} == set(
            events[-1]["data"].keys()
        )
        for e in events[:-1]:
            assert e["type"] == "raw_delta"
            assert set(e.keys()) == {"type", "delta"}
