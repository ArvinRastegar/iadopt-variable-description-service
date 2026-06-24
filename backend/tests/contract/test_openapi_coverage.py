"""Assert the OpenAPI schema fully describes the functional API surface.

Phase 1 brought every functional route into the schema with a ``response_model``.
This test locks that in: it fails if a functional route silently drops out of the
schema (e.g. a stray ``include_in_schema=False``) during a later phase.

The three meta-endpoints ``/api/docs``, ``/api/redoc``, ``/api/openapi.json`` are
intentionally excluded — they serve the documentation itself.
"""

from __future__ import annotations

import app.main as m  # type: ignore[import-not-found]

EXPECTED_PATHS = {
    "/api/admin/audit",
    "/api/admin/stats",
    "/api/admin/users",
    "/api/admin/users/{user_id}",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/auth/verify",
    "/api/decompose",
    "/api/decompose/stream",
    "/api/events",
    "/api/health",
    "/api/livez",
    "/api/model-options",
    "/api/nanopub/preparation-options",
    "/api/nanopub/publish",
    "/api/nanopub/retract",
    "/api/readyz",
}

# Every functional route now declares a response_model → these schemas must exist.
EXPECTED_SCHEMAS = {
    "DecomposeRequest",
    "DecomposeResponse",
    "ModelOptionsResponse",
    "NanopubPreparationOptionsResponse",
    "PublishNanopubRequest",
    "PublishNanopubResponse",
    "RetractNanopubRequest",
    "RetractNanopubResponse",
    "LoginRequest",
    "AuthUserResponse",
    "PublicUser",
    "FrontendEventRequest",
    "AdminCreateUserRequest",
    "AdminUpdateUserRequest",
    "UserResponse",
    "UsersResponse",
    "AuditResponse",
    "AuditEvent",
    "AdminStatsResponse",
    "ReadyzResponse",
    "StatusOkResponse",
}


def test_openapi_documents_all_functional_routes():
    """All functional routes appear in the OpenAPI paths."""
    spec = m.app.openapi()
    documented = set(spec["paths"].keys())
    missing = EXPECTED_PATHS - documented
    assert not missing, f"functional routes missing from OpenAPI: {sorted(missing)}"


def test_meta_endpoints_remain_hidden():
    """The documentation meta-endpoints stay out of the schema by design."""
    spec = m.app.openapi()
    documented = set(spec["paths"].keys())
    for hidden in ("/api/docs", "/api/redoc", "/api/openapi.json"):
        assert hidden not in documented


def test_openapi_exposes_contract_schemas():
    """The request/response models are present as component schemas."""
    spec = m.app.openapi()
    schemas = set(spec.get("components", {}).get("schemas", {}).keys())
    missing = EXPECTED_SCHEMAS - schemas
    assert not missing, f"expected component schemas missing: {sorted(missing)}"
