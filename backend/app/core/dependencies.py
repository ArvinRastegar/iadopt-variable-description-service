"""Shared FastAPI dependencies, the auth store singleton, and auth middleware.

Every router imports the ``auth_store`` singleton and the ``require_current_user``
dependency from here, so there is exactly one auth store per process and routers never
import the app factory (which would be circular). What a user may do is ``app.access``'s.

The authentication middleware is defined here as a plain coroutine and registered
by the app factory via ``app.middleware("http")``.
"""

from __future__ import annotations

import hmac
from typing import Any, Awaitable, Callable, Dict

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ..accounts import AccountStore
from ..auth import AuthStore
from ..guards import route_path
from ..oidc.settings import current as sign_in_settings
from ..records import RecordStore
from .config import settings

API_PREFIX = "/api"

SIGN_IN_HEADER = "X-IADOPT-Sign-In"
"""Names the sign-in methods a page may offer, on every 401 that means "sign in" and on ``/api/auth/me``."""

CSRF_HEADER = "X-CSRF-Token"
CSRF_FAILED = {"detail": "This page is out of date. Reload it and try again.", "reason": "csrf_failed"}
PASSWORD_OFF = {
    "detail": "Password sign-in is switched off. Sign in with ROHub.",
    "reason": "password_sign_in_disabled",
}
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOGIN_PATH = f"{API_PREFIX}/auth/login"
OWNER_LOGIN_PATH = f"{API_PREFIX}/auth/owner-login"
ME_PATH = f"{API_PREFIX}/auth/me"
BACKCHANNEL_LOGOUT_PATH = f"{API_PREFIX}/auth/backchannel-logout"

# Unsafe requests that use no session, so a session asks for nothing with them.
SESSIONLESS_REQUESTS = {("POST", LOGIN_PATH), ("POST", OWNER_LOGIN_PATH), ("POST", BACKCHANNEL_LOGOUT_PATH)}

# Where a 401 refuses a name and password, not a request without a session: no sign-in header.
PASSWORD_ROUTES = {LOGIN_PATH, OWNER_LOGIN_PATH}

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

# Decomposition records (D-036) and accounts share the auth database and follow its path.
record_store = RecordStore(auth_store)
account_store = AccountStore(auth_store)

# API paths that bypass the authentication middleware.
PUBLIC_API_PATHS = {
    LOGIN_PATH,
    OWNER_LOGIN_PATH,
    f"{API_PREFIX}/auth/verify",
    f"{API_PREFIX}/livez",
    f"{API_PREFIX}/readyz",
    f"{API_PREFIX}/health",
}

# Read-only configuration used by the public visualizer and Turtle preparation, and the sign-in's browser routes.
PUBLIC_GET_API_PATHS = {
    f"{API_PREFIX}/model-options",
    f"{API_PREFIX}/nanopub/preparation-options",
    f"{API_PREFIX}/auth/callback",
    f"{API_PREFIX}/auth/end-session",
}


def is_public_api_path(path: str, method: str) -> bool:
    """Return whether the request can bypass authentication.

    Args:
        path: The incoming API path.
        method: The incoming HTTP method.
    """
    return (
        path in PUBLIC_API_PATHS
        or (method == "GET" and path in PUBLIC_GET_API_PATHS)
        or (method, path) == ("POST", BACKCHANNEL_LOGOUT_PATH)
    )


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    """Project an internal user record to its public, response-safe shape.

    Args:
        user: The internal user dict (from ``AuthStore``).

    Returns:
        The public user dict (id, username, display_name, email, roles,
        is_active, auth_provider, external_subject, role).
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
        "role": user["role"],
    }


def require_current_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency: return the authenticated user or raise 401."""
    return auth_store.require_user(request)


async def authentication_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """HTTP middleware enforcing authentication on protected ``/api`` paths.

    OPTIONS requests pass through. When auth is enabled, protected API paths
    require a valid session (else 401), and with a session that carries a CSRF token
    an unsafe request needs it and the application's origin (else 403); for any API
    path a resolved user is cached on ``request.state.current_user``.

    Args:
        request: The incoming request.
        call_next: The downstream handler.

    Returns:
        The downstream response, or a ``JSONResponse`` that refuses the request.
    """
    path = route_path(request.scope)
    if request.method == "OPTIONS" or not path.startswith(API_PREFIX):
        return await call_next(request)

    response = refuse_request(request, path)
    if response is None:
        response = await call_next(request)
    if path == ME_PATH or (response.status_code == 401 and path not in PASSWORD_ROUTES):
        response.headers[SIGN_IN_HEADER] = sign_in_settings().methods
    if path == ME_PATH:
        response.headers["Cache-Control"] = "no-store"
    return response


def refuse_request(request: Request, path: str) -> Response | None:
    """Refuse an API request without the session or the CSRF token it needs; otherwise cache its user.

    A password is refused here where only ROHub signs people in, before anything looks at it; the
    owner's own route takes it in every mode (owner-sign-in, section 3).

    Args:
        request: The incoming request.
        path: Its path.
    """
    if (request.method, path) == ("POST", LOGIN_PATH) and not sign_in_settings().password:
        return JSONResponse(PASSWORD_OFF, status_code=403)
    user = auth_store.user_from_request(request)
    if user:
        request.state.current_user = user
    elif auth_store.enabled and not is_public_api_path(path, request.method):
        return JSONResponse({"detail": "Authentication required."}, status_code=401)
    if request.method in SAFE_METHODS or (request.method, path) in SESSIONLESS_REQUESTS or passes_csrf(request):
        return None
    return JSONResponse(CSRF_FAILED, status_code=403)


def passes_csrf(request: Request) -> bool:
    """Return whether an unsafe request comes from this application's own page.

    With a session that carries a token it needs that token and the public URL's origin: a ROHub
    session, and in ``oidc`` the owner's password session (owner-sign-in, section 4). Other sessions
    a password started need neither (D-046).

    Args:
        request: The incoming request.
    """
    session = auth_store.session_of(request)
    if session is None or session.csrf_token is None:
        return True
    given, origins = request.headers.getlist(CSRF_HEADER), request.headers.getlist("Origin")
    return (
        len(given) == 1
        and hmac.compare_digest(given[0].encode("utf-8"), session.csrf_token.encode("ascii"))
        and origins == [sign_in_settings().public_url]
    )
