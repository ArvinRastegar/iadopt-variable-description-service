"""Authentication routes: login, verify, me, logout.

Local username/password authentication with HMAC-signed session cookies; all
backed by the ``auth_store`` singleton in ``core.dependencies``.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..core.dependencies import API_PREFIX, auth_store, public_user, require_current_user
from ..schemas import AuthUserResponse, StatusOkResponse
from ..schemas.requests import LoginRequest

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
