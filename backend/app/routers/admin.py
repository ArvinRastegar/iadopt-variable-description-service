"""Admin routes: stats, audit log, user management and the records export (admin-only)."""

from __future__ import annotations

import tempfile
from typing import Any, BinaryIO, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from ..auth import utc_now
from ..core.dependencies import API_PREFIX, auth_store, public_user, record_store, require_admin_user
from ..record_export import export_file_name, iter_file, write_export
from ..schemas import AdminStatsResponse, AuditResponse, UserResponse, UsersResponse
from ..schemas.requests import AdminCreateUserRequest, AdminUpdateUserRequest
from .system import readiness_checks

router = APIRouter()


class ExportResponse(StreamingResponse):
    """Send an export's temporary file in chunks and close it when the response ends.

    Starlette does not close the chunk iterator when a client leaves mid-download, so without
    this the file and its disk space would stay until garbage collection.
    """

    def __init__(self, spool: BinaryIO, **kwargs: Any) -> None:
        """Stream ``spool`` from its start.

        Args:
            spool: The file the export was written to; this response owns it from now on.
            **kwargs: Passed to ``StreamingResponse`` (media type, headers).
        """
        super().__init__(iter_file(spool), **kwargs)
        self.spool = spool

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the file, then close it, also when the client left before the end."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.spool.close()


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
    # The OpenAPI document reads the default status code from the class's own signature.
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "Every record: summary.csv, edits.csv and records.jsonld in one ZIP.",
        }
    },
    tags=["Admin"],
)
def admin_export_records(request: Request, admin: Dict[str, Any] = Depends(require_admin_user)) -> ExportResponse:
    """Download every decomposition record in one ZIP (D-041).

    ``summary.csv`` and ``edits.csv`` give the records and their changes in plain words, and
    ``records.jsonld`` every record in full. The files name each record's account (name,
    username and email); IP addresses, browsers and devices stay in the database. The ZIP is
    written to a temporary file and sent in chunks, so memory use does not grow with the number
    of records. The download is audited like every admin action, with the row counts as its
    only payload. While authentication is disabled every visitor counts as an admin, so the
    export is refused then.

    Raises:
        HTTPException: 409 while authentication is disabled.
    """
    if not auth_store.enabled:
        raise HTTPException(status_code=409, detail="Records need accounts, and authentication is disabled.")
    exported_at = utc_now()
    spool = tempfile.TemporaryFile()
    try:
        with record_store.snapshot() as conn:
            counts = write_export(conn, spool, exported_at=exported_at)
        auth_store.audit_event(
            action="admin.records_exported",
            user=admin,
            request=request,
            status_code=200,
            response_payload={"rows": counts},
        )
        return ExportResponse(
            spool,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{export_file_name(exported_at)}"',
                "Cache-Control": "no-store",
            },
        )
    except BaseException:
        spool.close()
        raise


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
