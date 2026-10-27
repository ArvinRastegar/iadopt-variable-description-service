"""Authentication routes: login, verify, me, logout, and the account's saved ORCID iDs.

Local username/password authentication with HMAC-signed session cookies; all
backed by the ``auth_store`` singleton in ``core.dependencies``.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..core.dependencies import API_PREFIX, auth_store, public_user, require_current_user
from ..schemas import AuthUserResponse, SavedOrcidsResponse, StatusOkResponse
from ..schemas.requests import LoginRequest, SaveOrcidRequest
from ..services.orcid import cached_orcid_display_name, canonical_orcid_uri

router = APIRouter()


@router.post(f"{API_PREFIX}/auth/login", response_model=AuthUserResponse, tags=["Auth"])
def login(req: LoginRequest, request: Request) -> JSONResponse:
    """Authenticate a user and set the session cookie; audit success/failure.

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


@router.get(f"{API_PREFIX}/auth/verify", status_code=204, tags=["Auth"])
def verify_auth(request: Request) -> Response:
    """Return 204 if the request carries a valid session, else 401 (nginx auth_request).

    Raises:
        HTTPException: 401 if no valid session is present.
    """
    user = auth_store.user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return Response(status_code=204)


@router.get(f"{API_PREFIX}/auth/me", response_model=AuthUserResponse, tags=["Auth"])
def current_user(user: Dict[str, Any] = Depends(require_current_user)) -> Dict[str, Any]:
    """Return the authenticated user and whether auth is enabled."""
    return {"user": public_user(user), "auth_enabled": auth_store.enabled}


@router.post(f"{API_PREFIX}/auth/logout", response_model=StatusOkResponse, tags=["Auth"])
def logout(request: Request, user: Dict[str, Any] = Depends(require_current_user)) -> JSONResponse:
    """Delete the session, clear the cookie, and audit the logout."""
    auth_store.delete_session(request)
    response = JSONResponse({"status": "ok"})
    auth_store.clear_session_cookie(response)
    auth_store.audit_event(action="auth.logout", user=user, request=request, status_code=200)
    return response


def require_account_user(user: Dict[str, Any] = Depends(require_current_user)) -> Dict[str, Any]:
    """FastAPI dependency: the signed-in account, or 409 while authentication is disabled.

    Saved ORCID iDs belong to accounts, and the auth-disabled development user has none.

    Raises:
        HTTPException: 401 without a session; 409 when authentication is disabled.
    """
    if not auth_store.enabled:
        raise HTTPException(status_code=409, detail="Saved ORCID iDs need accounts, and authentication is disabled.")
    return user


@router.get(f"{API_PREFIX}/auth/me/orcids", response_model=SavedOrcidsResponse, tags=["Auth"])
def saved_orcids(user: Dict[str, Any] = Depends(require_account_user)) -> Dict[str, Any]:
    """Return the ORCID iDs the signed-in user saved, most recently used first."""
    return {"orcids": auth_store.list_orcids(user["id"])}


@router.post(f"{API_PREFIX}/auth/me/orcids", response_model=SavedOrcidsResponse, tags=["Auth"])
def save_orcid(
    req: SaveOrcidRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_account_user),
) -> Dict[str, Any]:
    """Save an ORCID iD to the signed-in account, or mark a saved one as just used.

    A newly saved iD is audited; marking one as used is not. The public name comes from
    the names publish and retract already resolved in this process, so this route never
    calls ORCID.

    Raises:
        HTTPException: 400 on a malformed iD or a full list; 409 when authentication is disabled.
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


@router.delete(f"{API_PREFIX}/auth/me/orcids/{{orcid_id}}", response_model=SavedOrcidsResponse, tags=["Auth"])
def remove_saved_orcid(
    orcid_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(require_account_user),
) -> Dict[str, Any]:
    """Remove a saved ORCID iD, given as the bare iD, from the signed-in account; audit it.

    Raises:
        HTTPException: 400 on a malformed iD; 404 if it is not saved; 409 when
            authentication is disabled.
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
