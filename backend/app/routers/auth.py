"""Authentication routes: login, verify, me, logout, and the account's saved ORCID iDs.

Local username/password authentication and, where it is switched on, the trip to ROHub and back
(``app.oidc.flow``), both with HMAC-signed session cookies backed by the ``auth_store`` singleton in
``core.dependencies``.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..access.dependencies import require
from ..access.policy import AccessState, Capability, Decision, decide
from ..access.state import access_state
from ..auth import PASSWORD, ROHUB
from ..core.dependencies import API_PREFIX, auth_store, public_user, require_current_user
from ..oidc import flow
from ..schemas import AuthUserResponse, CurrentUserResponse, SavedOrcidsResponse, SignOutResponse
from ..schemas.requests import LoginRequest, SaveOrcidRequest
from ..services.orcid import cached_orcid_display_name, canonical_orcid_uri

router = APIRouter()

END_SESSION_PATH = f"{API_PREFIX}/auth/end-session"


async def request_body(request: Request) -> bytes:
    """FastAPI dependency: the request's body as it came.

    Args:
        request: The request.
    """
    return await request.body()


@router.post(f"{API_PREFIX}/auth/login", response_model=AuthUserResponse, tags=["Auth"])
def login(req: LoginRequest, request: Request) -> JSONResponse:
    """Authenticate a user and set the session cookie; audit success/failure.

    It also ends a ROHub session of the same browser. The authentication middleware refuses it where
    only ROHub signs people in.

    Raises:
        HTTPException: 401 if the credentials are invalid.
    """
    start = time.perf_counter()
    user = auth_store.authenticate(req.username, req.password) if auth_store.enabled else auth_store.user_from_request(request)
    if not user:
        auth_store.audit_event(
            action="auth.login_failed",
            request=request,
            status_code=401,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload={"username": req.username},
            error="Invalid username or password.",
        )
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    response = JSONResponse({"user": public_user(user), "auth_enabled": auth_store.enabled})
    if auth_store.enabled:
        auth_store.end_rohub_session(request, response)
        auth_store.set_session_cookie(response, auth_store.create_session(user["id"], request))
    auth_store.audit_event(
        action="auth.login",
        user=user,
        request=request,
        status_code=200,
        latency_ms=round((time.perf_counter() - start) * 1000),
        request_payload={"username": req.username},
        response_payload={"user": public_user(user)},
    )
    return response


@router.get(
    f"{API_PREFIX}/auth/verify",
    status_code=204,
    dependencies=[Depends(require(Capability.SESSION))],
    tags=["Auth"],
)
def verify_auth() -> Response:
    """Return 204 if the request carries a valid session, else 401 (nginx auth_request)."""
    return Response(status_code=204)


@router.get(
    f"{API_PREFIX}/auth/verify/admin",
    status_code=204,
    dependencies=[Depends(require(Capability.VIEW_ACTIVITY))],
    tags=["Auth"],
)
def verify_admin() -> Response:
    """Return 204 for an admin or the owner, 401 without a session, 403 otherwise (nginx auth_request)."""
    return Response(status_code=204)


@router.get(
    f"{API_PREFIX}/auth/login",
    status_code=303,
    response_class=Response,
    dependencies=[Depends(flow.rohub_switched_on)],
    tags=["Auth"],
)
def start_rohub_sign_in(request: Request) -> Response:
    """Send the browser to ROHub to sign in; ``redirect`` is the page of the application to come back to."""
    return flow.start(request)


@router.get(
    f"{API_PREFIX}/auth/callback",
    status_code=303,
    response_class=Response,
    dependencies=[Depends(flow.rohub_switched_on)],
    tags=["Auth"],
)
def finish_rohub_sign_in(request: Request) -> Response:
    """Take the browser back from ROHub and start its session, or send it to the sign-in page with a code."""
    return flow.finish(request)


@router.get(
    END_SESSION_PATH,
    status_code=303,
    response_class=Response,
    dependencies=[Depends(flow.rohub_switched_on)],
    tags=["Auth"],
)
def end_rohub_session(request: Request) -> Response:
    """Send the browser to ROHub's sign-out page."""
    return flow.end_session(request)


@router.post(
    f"{API_PREFIX}/auth/backchannel-logout",
    dependencies=[Depends(flow.rohub_switched_on)],
    tags=["Auth"],
)
def rohub_backchannel_logout(request: Request, body: bytes = Depends(request_body)) -> Response:
    """End the sessions ROHub's logout token names; called by ROHub itself."""
    return flow.backchannel_logout(request, body)


def capability_entry(capability: Capability, decision: Decision) -> Dict[str, Any]:
    """Return one capability's entry in ``GET /api/auth/me`` (access-policy, section 7).

    Args:
        capability: The capability.
        decision: Its decision for the session.
    """
    entry: Dict[str, Any] = {"allowed": decision.allowed, "reason": decision.reason, "message": decision.message}
    if capability is Capability.DECOMPOSE:
        entry["uses_left_today"] = decision.uses_left_today
        entry["resets_at"] = decision.resets_at.isoformat() if decision.resets_at else None
    return entry


@router.get(
    f"{API_PREFIX}/auth/me",
    response_model=CurrentUserResponse,
    # Only decompose has uses_left_today and resets_at.
    response_model_exclude_unset=True,
    dependencies=[Depends(require(Capability.SESSION))],
    tags=["Auth"],
)
def current_user(
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
    state: AccessState = Depends(access_state),
) -> Dict[str, Any]:
    """Return the authenticated user, whether auth is enabled, what the session may do, and its CSRF token."""
    session = auth_store.session_of(request)
    return {
        "user": public_user(user),
        "auth_enabled": auth_store.enabled,
        "capabilities": {
            capability.value: capability_entry(capability, decision) for capability, decision in decide(state).items()
        },
        "sign_in": session.kind if session else None,
        "csrf_token": session.csrf_token if session else None,
    }


@router.post(
    f"{API_PREFIX}/auth/logout",
    response_model=SignOutResponse,
    dependencies=[Depends(require(Capability.SESSION))],
    tags=["Auth"],
)
def logout(request: Request, user: Dict[str, Any] = Depends(require_current_user)) -> JSONResponse:
    """Delete the browser's sessions, clear their cookies, and audit the logout; nothing is asked of ROHub."""
    session = auth_store.session_of(request)
    auth_store.delete_session(request)
    response = JSONResponse(
        {"status": "ok", "end_session_url": END_SESSION_PATH if session and session.kind == ROHUB else None}
    )
    if session is None or session.kind == PASSWORD or auth_store.cookie_name in request.cookies:
        auth_store.clear_session_cookie(response)
    auth_store.clear_rohub_cookie(request, response)
    auth_store.audit_event(action="auth.logout", user=user, request=request, status_code=200)
    return response


@router.get(
    f"{API_PREFIX}/auth/me/orcids",
    response_model=SavedOrcidsResponse,
    dependencies=[Depends(require(Capability.ACCOUNT_DATA))],
    tags=["Auth"],
)
def saved_orcids(user: Dict[str, Any] = Depends(require_current_user)) -> Dict[str, Any]:
    """Return the ORCID iDs the signed-in user saved, most recently used first."""
    return {"orcids": auth_store.list_orcids(user["id"])}


@router.post(
    f"{API_PREFIX}/auth/me/orcids",
    response_model=SavedOrcidsResponse,
    dependencies=[Depends(require(Capability.ACCOUNT_DATA))],
    tags=["Auth"],
)
def save_orcid(
    req: SaveOrcidRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
) -> Dict[str, Any]:
    """Save an ORCID iD to the signed-in account, or mark a saved one as just used.

    A newly saved iD is audited; marking one as used is not. The public name comes from
    the names publish and retract already resolved in this process, so this route never
    calls ORCID.

    Raises:
        HTTPException: 400 on a malformed iD or a full list.
    """
    try:
        orcid = canonical_orcid_uri(req.orcid)
        orcids, created = auth_store.save_orcid(user["id"], orcid, cached_orcid_display_name(orcid))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if created:
        auth_store.audit_event(
            action="auth.orcid_saved",
            user=user,
            request=request,
            status_code=200,
            request_payload=req.model_dump(),
            response_payload={"orcid": orcid},
        )
    return {"orcids": orcids}


@router.delete(
    f"{API_PREFIX}/auth/me/orcids/{{orcid_id}}",
    response_model=SavedOrcidsResponse,
    dependencies=[Depends(require(Capability.ACCOUNT_DATA))],
    tags=["Auth"],
)
def remove_saved_orcid(
    orcid_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
) -> Dict[str, Any]:
    """Remove a saved ORCID iD, given as the bare iD, from the signed-in account; audit it.

    Raises:
        HTTPException: 400 on a malformed iD; 404 if it is not saved.
    """
    try:
        orcid = canonical_orcid_uri(orcid_id)
        orcids = auth_store.remove_orcid(user["id"], orcid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    auth_store.audit_event(
        action="auth.orcid_removed",
        user=user,
        request=request,
        status_code=200,
        request_payload={"orcid_id": orcid_id},
        response_payload={"orcid": orcid},
    )
    return {"orcids": orcids}
