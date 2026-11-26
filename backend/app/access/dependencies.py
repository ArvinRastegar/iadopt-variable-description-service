"""One capability per route: the FastAPI dependency and the route table (access-policy, section 5).

:func:`require` returns a dependency that carries the capability it checks as ``.capability`` and
depends on :func:`app.access.state.access_state`; a refusal becomes the status and body of section
6. :data:`ROUTE_CAPABILITIES` maps every ``(method, path)`` of the application to its capability, or
to ``None`` for a public route.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from .. import usage
from ..core.dependencies import API_PREFIX
from .policy import AccessState, Capability, Decision, decide
from .state import access_state

ROUTE_CAPABILITIES: Dict[Tuple[str, str], Optional[Capability]] = {
    ("GET", f"{API_PREFIX}/livez"): None,
    ("GET", f"{API_PREFIX}/readyz"): None,
    ("GET", f"{API_PREFIX}/health"): None,
    ("POST", f"{API_PREFIX}/auth/login"): None,
    ("GET", f"{API_PREFIX}/auth/login"): None,
    ("GET", f"{API_PREFIX}/auth/callback"): None,
    ("GET", f"{API_PREFIX}/auth/end-session"): None,
    ("POST", f"{API_PREFIX}/auth/backchannel-logout"): None,
    ("GET", f"{API_PREFIX}/model-options"): Capability.PUBLIC_TOOLS,
    ("GET", f"{API_PREFIX}/nanopub/preparation-options"): Capability.PUBLIC_TOOLS,
    ("GET", f"{API_PREFIX}/auth/verify"): Capability.SESSION,
    ("GET", f"{API_PREFIX}/auth/me"): Capability.SESSION,
    ("POST", f"{API_PREFIX}/auth/logout"): Capability.SESSION,
    ("POST", f"{API_PREFIX}/events"): Capability.SESSION,
    ("GET", f"{API_PREFIX}/docs"): Capability.SESSION,
    ("GET", f"{API_PREFIX}/redoc"): Capability.SESSION,
    ("GET", f"{API_PREFIX}/openapi.json"): Capability.SESSION,
    ("GET", f"{API_PREFIX}/auth/me/orcids"): Capability.ACCOUNT_DATA,
    ("POST", f"{API_PREFIX}/auth/me/orcids"): Capability.ACCOUNT_DATA,
    ("DELETE", f"{API_PREFIX}/auth/me/orcids/{{orcid_id}}"): Capability.ACCOUNT_DATA,
    ("POST", f"{API_PREFIX}/decompose"): Capability.DECOMPOSE,
    ("POST", f"{API_PREFIX}/decompose/stream"): Capability.DECOMPOSE,
    ("POST", f"{API_PREFIX}/records"): Capability.SAVE_AND_REVIEW,
    ("POST", f"{API_PREFIX}/records/{{record_id}}/events"): Capability.SAVE_AND_REVIEW,
    # Another account's record also needs VIEW_ACTIVITY, which the route checks itself.
    ("GET", f"{API_PREFIX}/records/{{record_id}}"): Capability.SAVE_AND_REVIEW,
    ("GET", f"{API_PREFIX}/auth/verify/admin"): Capability.VIEW_ACTIVITY,
    ("GET", f"{API_PREFIX}/admin/stats"): Capability.VIEW_ACTIVITY,
    ("GET", f"{API_PREFIX}/admin/audit"): Capability.VIEW_ACTIVITY,
    ("POST", f"{API_PREFIX}/nanopub/publish"): Capability.PUBLISH,
    ("POST", f"{API_PREFIX}/nanopub/retract"): Capability.RETRACT_OWN,
    ("GET", f"{API_PREFIX}/admin/records/export"): Capability.EXPORT_RESEARCH,
    ("GET", f"{API_PREFIX}/admin/users"): Capability.MANAGE_USERS,
    ("POST", f"{API_PREFIX}/admin/users"): Capability.MANAGE_USERS,
    ("PATCH", f"{API_PREFIX}/admin/users/{{user_id}}"): Capability.MANAGE_USERS,
    ("GET", f"{API_PREFIX}/admin/users/{{user_id}}/history"): Capability.MANAGE_USERS,
    ("POST", f"{API_PREFIX}/admin/users/{{user_id}}/block"): Capability.MANAGE_USERS,
    ("POST", f"{API_PREFIX}/admin/users/{{user_id}}/unblock"): Capability.MANAGE_USERS,
    ("POST", f"{API_PREFIX}/admin/users/{{user_id}}/approve"): Capability.REVIEW_APPLICATIONS,
    ("POST", f"{API_PREFIX}/admin/users/{{user_id}}/withdraw-approval"): Capability.REVIEW_APPLICATIONS,
    ("POST", f"{API_PREFIX}/admin/users/{{user_id}}/grant-admin"): Capability.MANAGE_ADMINS,
    ("POST", f"{API_PREFIX}/admin/users/{{user_id}}/revoke-admin"): Capability.MANAGE_ADMINS,
}
"""Section 5's table, keyed by ``(method, path)``."""

_STATUS = {"sign_in_required": 401, "daily_limit": 429, "accounts_disabled": 409}
"""The status of each reason that is not a 403 (section 6)."""


class AccessRefused(Exception):
    """A refusal over HTTP: its status, its body and its headers."""

    def __init__(self, status_code: int, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        """Describe the response.

        Args:
            status_code: The HTTP status.
            body: The JSON body: ``detail``, and ``reason`` for every refusal but a 401.
            headers: Headers besides the content type.
        """
        super().__init__(body["detail"])
        self.status_code = status_code
        self.body = body
        self.headers = headers


def access_refused_response(_: Request, refusal: Exception) -> JSONResponse:
    """Exception handler: answer an :class:`AccessRefused`.

    Args:
        _: The refused request.
        refusal: The refusal.
    """
    assert isinstance(refusal, AccessRefused)
    return JSONResponse(refusal.body, status_code=refusal.status_code, headers=refusal.headers)


def refusal_of(decision: Decision) -> AccessRefused:
    """Return the HTTP refusal of a denied decision (section 6).

    Args:
        decision: The denied decision.
    """
    reason = decision.reason or ""
    body: Dict[str, Any] = {"detail": decision.message}
    headers: Dict[str, str] = {}
    if reason != "sign_in_required":
        body["reason"] = reason
    if reason == "daily_limit":
        body["resets_at"] = decision.resets_at.isoformat() if decision.resets_at else None
        if decision.resets_at:
            # Timestamps: a difference of two times in Berlin would miss a change of its clocks.
            seconds = decision.resets_at.timestamp() - usage.now().timestamp()
            headers["Retry-After"] = str(max(0, math.ceil(seconds)))
    return AccessRefused(_STATUS.get(reason, 403), body, headers)


def require(capability: Capability) -> Callable[..., Decision]:
    """Return the FastAPI dependency that refuses a request without ``capability``.

    It runs before the route and before its body is validated, so a refused request does nothing.

    Args:
        capability: The capability the route needs.
    """

    def check(request: Request, state: AccessState = Depends(access_state)) -> Decision:
        decision = decide(state)[capability]
        if not decision.allowed:
            if AccessRefused not in request.app.exception_handlers:
                # The application registers the handler; any other app the dependency is used in
                # gets it here, or its refusals would end as server errors.
                request.scope["starlette.exception_handlers"][0][AccessRefused] = access_refused_response
            raise refusal_of(decision)
        return decision

    check.capability = capability  # type: ignore[attr-defined]
    return check
