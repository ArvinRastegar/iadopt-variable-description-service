"""Admin routes: stats, audit log, user management and the records export (admin-only)."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..auth import utc_now
from ..core.dependencies import API_PREFIX, auth_store, public_user, record_store, require_admin_user
from ..record_export import build_export, export_file_name
from ..schemas import AdminStatsResponse, AuditResponse, UserResponse, UsersResponse
from ..schemas.requests import AdminCreateUserRequest, AdminUpdateUserRequest
from .system import readiness_checks

router = APIRouter()


@router.get(f"{API_PREFIX}/admin/stats", response_model=AdminStatsResponse, tags=["Admin"])
def admin_stats(_: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    """Return usage/audit statistics plus a readiness summary."""
    checks = readiness_checks()
    stats = auth_store.stats()
    stats["readiness"] = {
        "status": "ready" if all(checks.values()) else "not_ready",
        "checks": checks,
    }
    return stats


@router.get(f"{API_PREFIX}/admin/audit", response_model=AuditResponse, tags=["Admin"])
def admin_audit(
    limit: int = 100,
    offset: int = 0,
    _: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    """Return a page of audit events."""
    return {"events": auth_store.get_audit_events(limit=limit, offset=offset)}


@router.get(
    f"{API_PREFIX}/admin/records/export",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}, "description": "Every record as CSV tables in one ZIP."}},
    tags=["Admin"],
)
def admin_export_records(request: Request, admin: Dict[str, Any] = Depends(require_admin_user)) -> Response:
    """Download every decomposition record as CSV tables in one ZIP (D-040).

    The files name each record's account (name, username and email); IP addresses, browsers
    and devices stay in the database. The download is audited like every admin action, with the
    row counts as its only payload. While authentication is disabled every visitor counts as an
    admin, so the export is refused then.

    Raises:
        HTTPException: 409 while authentication is disabled.
    """
    if not auth_store.enabled:
        raise HTTPException(status_code=409, detail="Records need accounts, and authentication is disabled.")
    exported_at = utc_now()
    with record_store.snapshot() as conn:
        content, counts = build_export(conn, exported_at=exported_at)
    auth_store.audit_event(
        action="admin.records_exported",
        user=admin,
        request=request,
        status_code=200,
        response_payload={"rows": counts},
    )
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{export_file_name(exported_at)}"',
            "Cache-Control": "no-store",
        },
    )


@router.get(f"{API_PREFIX}/admin/users", response_model=UsersResponse, tags=["Admin"])
def admin_users(_: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    """List all users (public projection)."""
    return {"users": [public_user(user) for user in auth_store.list_users()]}


@router.post(f"{API_PREFIX}/admin/users", response_model=UserResponse, tags=["Admin"])
def admin_create_user(
    req: AdminCreateUserRequest,
    request: Request,
    admin: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    """Create a new user; audit with the password redacted.

    Raises:
        HTTPException: 400 if the username is taken or the password is invalid.
    """
    try:
        user = auth_store.create_user(**req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    auth_store.audit_event(
        action="admin.user_created",
        user=admin,
        request=request,
        status_code=200,
        request_payload={**req.model_dump(exclude={"password"}), "password": "[redacted]"},
        response_payload={"user": public_user(user)},
    )
    return {"user": public_user(user)}


@router.patch(f"{API_PREFIX}/admin/users/{{user_id}}", response_model=UserResponse, tags=["Admin"])
def admin_update_user(
    user_id: int,
    req: AdminUpdateUserRequest,
    request: Request,
    admin: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    """Partially update a user; audit with the password redacted.

    Raises:
        HTTPException: 400 on unknown fields, short password, missing/duplicate
            user, or attempting to disable the last active admin.
    """
    updates = req.model_dump(exclude_unset=True)
    try:
        user = auth_store.update_user(user_id, updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    audit_payload = dict(updates)
    if "password" in audit_payload:
        audit_payload["password"] = "[redacted]"
    auth_store.audit_event(
        action="admin.user_updated",
        user=admin,
        request=request,
        status_code=200,
        request_payload={"user_id": user_id, "updates": audit_payload},
        response_payload={"user": public_user(user)},
    )
    return {"user": public_user(user)}
