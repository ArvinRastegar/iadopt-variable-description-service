"""Shared FastAPI dependencies, the auth store singleton, and auth middleware.

Every router imports the ``auth_store`` singleton and the ``require_current_user`` /
``require_admin_user`` dependencies from here, so there is exactly one auth store
per process and routers never import the app factory (which would be circular).

The authentication middleware is defined here as a plain coroutine and registered
by the app factory via ``app.middleware("http")``.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ..auth import AuthStore
from .config import settings

API_PREFIX = "/api"

# The single process-wide auth store, configured from settings.
auth_store = AuthStore(
    db_path=settings.auth_db_path,
    enabled=settings.auth_enabled,
    session_secret=settings.session_secret,
    cookie_secure=settings.cookie_secure,
    session_ttl_hours=settings.session_ttl_hours,
    audit_retention_days=settings.audit_retention_days,
    audit_max_payload_bytes=settings.audit_max_payload_bytes,
)

# API paths that bypass the authentication middleware.
PUBLIC_API_PATHS = {
    f"{API_PREFIX}/auth/login",
    f"{API_PREFIX}/auth/verify",
    f"{API_PREFIX}/livez",
    f"{API_PREFIX}/readyz",
    f"{API_PREFIX}/health",
}


def is_public_api_path(path: str) -> bool:
    """Return True if the path is exempt from authentication."""
    return path in PUBLIC_API_PATHS


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    """Project an internal user record to its public, response-safe shape.

    Args:
        user: The internal user dict (from ``AuthStore``).

    Returns:
        The public user dict (id, username, display_name, email, roles,
        is_active, auth_provider, external_subject).
    """
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "email": user.get("email") or "",
        "roles": user.get("roles", []),
        "is_active": user.get("is_active", True),
        "auth_provider": user.get("auth_provider", "local"),
        "external_subject": user.get("external_subject"),
    }


def require_current_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency: return the authenticated user or raise 401."""
    return auth_store.require_user(request)


def require_admin_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency: return the authenticated admin user or raise 401/403."""
    return auth_store.require_admin(request)


async def authentication_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """HTTP middleware enforcing authentication on protected ``/api`` paths.

    OPTIONS requests pass through. When auth is enabled, protected API paths
    require a valid session (else 401); for any API path a resolved user is cached
    on ``request.state.current_user``.

    Args:
        request: The incoming request.
        call_next: The downstream handler.

    Returns:
        The downstream response, or a 401 ``JSONResponse`` when unauthenticated.
    """
    if request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    if auth_store.enabled and path.startswith(API_PREFIX) and not is_public_api_path(path):
        user = auth_store.user_from_request(request)
        if not user:
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        request.state.current_user = user
    elif path.startswith(API_PREFIX):
        user = auth_store.user_from_request(request)
        if user:
            request.state.current_user = user

    return await call_next(request)
