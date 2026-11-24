"""Admin routes: stats, audit log, accounts and the records export, each behind its capability."""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from typing import Any, BinaryIO, Callable, Dict, Iterator, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from .. import storage
from ..access.dependencies import AccessRefused, require
from ..access.policy import Capability
from ..accounts import AccountRefused, UnknownAccount
from ..auth import utc_now
from ..core.dependencies import API_PREFIX, account_store, auth_store, public_user, require_current_user
from ..record_export import export_file_name, iter_file, write_export
from ..schemas import AccountHistoryResponse, AdminStatsResponse, AuditResponse, UserResponse, UsersResponse
from ..schemas.requests import AccountActionRequest, AdminCreateUserRequest, AdminUpdateUserRequest
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


def admin_view(account: Dict[str, Any]) -> Dict[str, Any]:
    """Project an account to what admins see of it (accounts-roles, section 9).

    Args:
        account: An account with its identities, from the account store.
    """
    return {
        **public_user(account),
        "blocked": not account["is_active"],
        "email_verified": account["email_verified"],
        "local": account["local"],
        "identities": account["identities"],
        "created_at": account["created_at"],
        "last_login_at": account["last_login_at"],
    }


@contextmanager
def account_errors() -> Iterator[None]:
    """Answer the account store's refusals: 404, 400, or section 4's status with its reason.

    Yields:
        Nothing; it wraps the call to the account store.

    Raises:
        HTTPException: 404 for an unknown account, 400 for a malformed request.
        AccessRefused: When section 4 does not allow the change.
    """
    try:
        yield
    except UnknownAccount as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except AccountRefused as e:
        raise AccessRefused(e.status_code, {"detail": e.message, "reason": e.reason}) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get(
    f"{API_PREFIX}/admin/stats",
    response_model=AdminStatsResponse,
    dependencies=[Depends(require(Capability.VIEW_ACTIVITY))],
    tags=["Admin"],
)
def admin_stats() -> Dict[str, Any]:
    """Return usage/audit statistics plus a readiness summary."""
    checks = readiness_checks()
    stats = auth_store.stats()
    stats["readiness"] = {
        "status": "ready" if all(checks.values()) else "not_ready",
        "checks": checks,
    }
    return stats


@router.get(
    f"{API_PREFIX}/admin/audit",
    response_model=AuditResponse,
    dependencies=[Depends(require(Capability.VIEW_ACTIVITY))],
    tags=["Admin"],
)
def admin_audit(limit: int = 100, offset: int = 0) -> Dict[str, Any]:
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
    dependencies=[Depends(require(Capability.EXPORT_RESEARCH))],
    tags=["Admin"],
)
def admin_export_records(request: Request, admin: Dict[str, Any] = Depends(require_current_user)) -> ExportResponse:
    """Download every decomposition record in one ZIP (D-041).

    ``summary.csv`` and ``edits.csv`` give the records and their changes in plain words, and
    ``records.jsonld`` every record in full. The files name each record's account (name,
    username and email); IP addresses, browsers and devices stay in the database. The ZIP is
    written to a temporary file and sent in chunks, so memory use does not grow with the number
    of records. The download is audited like every admin action, with the row counts as its
    only payload. While authentication is disabled every visitor counts as an admin, so the
    capability refuses the export then.
    """
    exported_at = utc_now()
    spool = tempfile.TemporaryFile()
    try:
        with storage.snapshot(path=auth_store.db_path) as conn:
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


@router.get(
    f"{API_PREFIX}/admin/users",
    response_model=UsersResponse,
    dependencies=[Depends(require(Capability.MANAGE_USERS))],
    tags=["Admin"],
)
def admin_users() -> Dict[str, Any]:
    """List all accounts."""
    return {"users": [admin_view(account) for account in account_store.list_accounts()]}


@router.post(
    f"{API_PREFIX}/admin/users",
    response_model=UserResponse,
    dependencies=[Depends(require(Capability.MANAGE_USERS))],
    tags=["Admin"],
)
def admin_create_user(
    req: AdminCreateUserRequest,
    request: Request,
    admin: Dict[str, Any] = Depends(require_current_user),
) -> Dict[str, Any]:
    """Create an ordinary local account, until the cutover; audit with the password redacted.

    A taken username, an invalid password, other roles or a blocked account answer 400.
    """
    with account_errors():
        account = account_store.create_local(actor=admin, **req.model_dump())
    auth_store.audit_event(
        action="admin.user_created",
        user=admin,
        request=request,
        status_code=200,
        request_payload={**req.model_dump(exclude={"password"}), "password": "[redacted]"},
        response_payload={"user": public_user(account)},
    )
    return {"user": admin_view(account)}


@router.patch(
    f"{API_PREFIX}/admin/users/{{user_id}}",
    response_model=UserResponse,
    dependencies=[Depends(require(Capability.MANAGE_USERS))],
    tags=["Admin"],
)
def admin_update_user(
    user_id: int,
    req: AdminUpdateUserRequest,
    request: Request,
    admin: Dict[str, Any] = Depends(require_current_user),
) -> Dict[str, Any]:
    """Edit a local account's username, display name, email or password, until the cutover.

    Audited with the password redacted. Roles and blocks have their own routes, so ``roles`` and
    ``is_active`` answer 400, as an invalid value does.
    """
    updates = req.model_dump(exclude_unset=True)
    with account_errors():
        account = account_store.update_local(actor=admin, user_id=user_id, updates=updates)
    audit_payload = dict(updates)
    if "password" in audit_payload:
        audit_payload["password"] = "[redacted]"
    auth_store.audit_event(
        action="admin.user_updated",
        user=admin,
        request=request,
        status_code=200,
        request_payload={"user_id": user_id, "updates": audit_payload},
        response_payload={"user": public_user(account)},
    )
    return {"user": admin_view(account)}


@router.get(
    f"{API_PREFIX}/admin/users/{{user_id}}/history",
    response_model=AccountHistoryResponse,
    dependencies=[Depends(require(Capability.MANAGE_USERS))],
    tags=["Admin"],
)
def admin_user_history(user_id: int) -> Dict[str, Any]:
    """List an account's role, block and identity changes, newest first."""
    with account_errors():
        return {"history": account_store.history(user_id)}


def _change_account(
    change: Callable[..., Tuple[Dict[str, Any], bool]],
    *,
    audit_action: str,
    user_id: int,
    req: Optional[AccountActionRequest],
    request: Request,
    admin: Dict[str, Any],
) -> Dict[str, Any]:
    """Run one role or block change and audit it; a refused or no-op change writes no audit event."""
    reason = req.reason if req else None
    with account_errors():
        account, changed = change(actor=admin, user_id=user_id, reason=reason)
    if changed:
        auth_store.audit_event(
            action=audit_action,
            user=admin,
            request=request,
            status_code=200,
            request_payload={"user_id": user_id, "reason": reason},
            response_payload={"user": public_user(account)},
        )
    return {"user": admin_view(account)}


@router.post(
    f"{API_PREFIX}/admin/users/{{user_id}}/block",
    response_model=UserResponse,
    dependencies=[Depends(require(Capability.MANAGE_USERS))],
    tags=["Admin"],
)
def admin_block_user(
    user_id: int,
    request: Request,
    req: Optional[AccountActionRequest] = None,
    admin: Dict[str, Any] = Depends(require_current_user),
) -> Dict[str, Any]:
    """Block an account, with a reason; its sessions end at once and its data stays."""
    return _change_account(
        account_store.block,
        audit_action="admin.user_blocked",
        user_id=user_id,
        req=req,
        request=request,
        admin=admin,
    )


@router.post(
    f"{API_PREFIX}/admin/users/{{user_id}}/unblock",
    response_model=UserResponse,
    dependencies=[Depends(require(Capability.MANAGE_USERS))],
    tags=["Admin"],
)
def admin_unblock_user(
    user_id: int,
    request: Request,
    req: Optional[AccountActionRequest] = None,
    admin: Dict[str, Any] = Depends(require_current_user),
) -> Dict[str, Any]:
    """Unblock an account, with an optional reason."""
    return _change_account(
        account_store.unblock,
        audit_action="admin.user_unblocked",
        user_id=user_id,
        req=req,
        request=request,
        admin=admin,
    )


@router.post(
    f"{API_PREFIX}/admin/users/{{user_id}}/grant-admin",
    response_model=UserResponse,
    dependencies=[Depends(require(Capability.MANAGE_ADMINS))],
    tags=["Admin"],
)
def admin_grant_admin(
    user_id: int,
    request: Request,
    req: Optional[AccountActionRequest] = None,
    admin: Dict[str, Any] = Depends(require_current_user),
) -> Dict[str, Any]:
    """Make an ordinary account an admin, with an optional reason; the owner only."""
    return _change_account(
        account_store.grant_admin,
        audit_action="admin.admin_granted",
        user_id=user_id,
        req=req,
        request=request,
        admin=admin,
    )


@router.post(
    f"{API_PREFIX}/admin/users/{{user_id}}/revoke-admin",
    response_model=UserResponse,
    dependencies=[Depends(require(Capability.MANAGE_ADMINS))],
    tags=["Admin"],
)
def admin_revoke_admin(
    user_id: int,
    request: Request,
    req: Optional[AccountActionRequest] = None,
    admin: Dict[str, Any] = Depends(require_current_user),
) -> Dict[str, Any]:
    """Make an admin an ordinary account, with an optional reason; the owner only."""
    return _change_account(
        account_store.revoke_admin,
        audit_action="admin.admin_revoked",
        user_id=user_id,
        req=req,
        request=request,
        admin=admin,
    )
