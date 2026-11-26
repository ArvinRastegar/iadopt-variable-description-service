"""The adapter that builds a request's access state from the database (access-policy, section 2).

:func:`access_state` is used as a FastAPI dependency, so tests replace it through
``app.dependency_overrides``. The state is built on every request, with interim values (D-045, and the
owner's of 2026-11-25) until the modules that own an input exist.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Request

from .. import storage, usage
from ..account_model import is_legacy
from ..core.dependencies import auth_store
from .policy import AccessState


def access_state(request: Request) -> AccessState:
    """Build the access state of a request from its session and the database; nothing is cached.

    Args:
        request: The incoming request.
    """
    user = auth_store.user_from_request(request)
    if user is None:
        return AccessState(account_id=None)
    if not auth_store.enabled:
        return AccessState(
            account_id=user["id"],
            account_backed=False,
            role=user["role"],
            email_verified=True,
            terms_accepted=True,
            application="approved",
        )
    limit = usage.daily_limit()
    if limit is None:
        return account_state(user, None)
    with storage.transaction(path=auth_store.db_path) as conn:
        return account_state(user, usage.allowance(conn, user["id"], limit, usage.now()))


def account_state(account: Dict[str, Any], allowance: Optional[usage.Allowance]) -> AccessState:
    """Build the access state of an account, the same on a development machine and in production.

    Until ``onboarding`` exists (the owner, 2026-11-25), people accept the terms by email and an admin
    then approves their account, so no account is asked for the terms here. A legacy local account (a
    usable password and no identity-provider identity) counts as approved. An account that is not
    approved waits; only with the daily limit switched on may it decompose before, so many times a day.
    One that has neither, after the password purge, waits for nothing: nobody can approve it.

    Args:
        account: The account as the database holds it.
        allowance: What it has left of its day, ``None`` while the daily limit is switched off.
    """
    approved = is_legacy(account) or account["approved_at"] is not None
    return AccessState(
        account_id=account["id"],
        role=account["role"],
        blocked=not account["is_active"],
        email_verified=account["email_verified"],
        terms_accepted=True,
        application="approved" if approved else "none",
        uses_left_today=allowance.left if allowance else None,
        resets_at=allowance.day.end if allowance else None,
        approval_first=allowance is None,
        password_ended=not account["local"] and not account["has_identity"],
    )
