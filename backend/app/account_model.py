"""What an account is in the database (accounts-roles, section 3).

The leaf below the auth store, which signs accounts in, and the account store, which changes them:
how a role is derived, what an account's dict holds, and how its history is written.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Mapping, Optional

UNUSABLE_PASSWORD = "!"
"""The password hash no password matches, in this image and the previous one."""

MAX_ACCOUNT_ID = 2**63 - 1
"""The largest id SQLite can store; any larger one is unknown."""

LOCAL_ISSUER = "local"
"""The issuer an owner binding uses for an account's own id; never an identity's issuer."""

_IS_OWNER = f"""EXISTS (
    SELECT 1 FROM owner_binding
     WHERE (owner_binding.issuer = '{LOCAL_ISSUER}' AND owner_binding.subject = CAST(users.id AS TEXT))
        OR EXISTS (
            SELECT 1 FROM account_identities
             WHERE account_identities.issuer = owner_binding.issuer
               AND account_identities.subject = owner_binding.subject
               AND account_identities.user_id = users.id
        )
)"""

ACCOUNT_COLUMNS = f"""users.*, {_IS_OWNER} AS is_owner,
    EXISTS (SELECT 1 FROM account_identities WHERE account_identities.user_id = users.id) AS has_identity,
    (SELECT approver.username FROM users AS approver WHERE approver.id = users.approved_by) AS approver_username"""
"""What a query selects from ``users`` for :func:`account_from_row`."""

DEVELOPMENT_ACTOR = "development"
"""Who acted when it was the development user, who has no account."""


def account_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a row selected with :data:`ACCOUNT_COLUMNS` into the internal user dict.

    The role is derived: the account that holds the bound identity is the owner, else ``admin`` in
    ``roles`` makes an admin, else it is a user. ``email_verified`` is ``None`` when unknown,
    ``local`` says whether the account has a usable password, and ``has_identity`` whether an
    identity provider's identity leads to it. ``approved_at`` and ``approved_by`` (a username) are
    ``None`` without an admin's approval.

    Args:
        row: The row.
    """
    roles = json.loads(row["roles"] or "[]")
    approved_at = row["approved_at"]
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "email": row["email"],
        "roles": roles,
        "is_active": bool(row["is_active"]),
        "auth_provider": row["auth_provider"],
        "external_subject": row["external_subject"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
        "role": "owner" if row["is_owner"] else "admin" if "admin" in roles else "user",
        "email_verified": None if row["email_verified"] is None else bool(row["email_verified"]),
        "local": row["password_hash"] != UNUSABLE_PASSWORD,
        "has_identity": bool(row["has_identity"]),
        "approved_at": approved_at,
        "approved_by": (row["approver_username"] or DEVELOPMENT_ACTOR) if approved_at else None,
    }


def is_legacy(account: Mapping[str, Any]) -> bool:
    """Return whether an account is a legacy local one: a usable password and no identity (accounts-roles, section 3).

    Args:
        account: The account's dict.
    """
    return bool(account["local"] and not account["has_identity"])


def approval_of(account: Mapping[str, Any]) -> str:
    """Return where an account stands: ``approved`` by an admin, ``not_needed`` or ``waiting``.

    Legacy local accounts, admins and the owner count as approved by what they are. Without an
    identity nobody waits: nobody can sign in to a local account whose password was purged.

    Args:
        account: The account's dict.
    """
    if account["approved_at"]:
        return "approved"
    return "waiting" if account["has_identity"] and account["role"] == "user" else "not_needed"


def find_account(conn: sqlite3.Connection, user_id: int) -> Optional[Dict[str, Any]]:
    """Return an account by id, or ``None``.

    Args:
        conn: An open connection.
        user_id: The account's id.
    """
    if abs(user_id) > MAX_ACCOUNT_ID:
        return None
    row = conn.execute(f"SELECT {ACCOUNT_COLUMNS} FROM users WHERE users.id = ?", (user_id,)).fetchone()
    return account_from_row(row) if row else None


def owner_id(conn: sqlite3.Connection) -> Optional[int]:
    """Return the id of the account that holds the owner binding, or ``None``.

    Args:
        conn: An open connection.
    """
    row = conn.execute(f"SELECT users.id FROM users WHERE {_IS_OWNER}").fetchone()
    return int(row["id"]) if row else None


def add_history(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    action: str,
    actor_kind: str,
    at: str,
    actor_user_id: Optional[int] = None,
    reason: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    """Add a row to an account's append-only history, in the transaction of the change it records.

    Args:
        conn: The transaction's connection.
        user_id: The account that changed.
        action: What changed, such as ``blocked``.
        actor_kind: ``account``, ``offline`` or ``bootstrap``.
        at: When, as ISO-8601 UTC.
        actor_user_id: The account that acted, when ``actor_kind`` is ``account``.
        reason: The reason given, if any.
        details: What else belongs to the change.
    """
    conn.execute(
        """
        INSERT INTO account_history (user_id, action, actor_user_id, actor_kind, reason, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            action,
            actor_user_id,
            actor_kind,
            reason,
            json.dumps(dict(details or {}), ensure_ascii=False, sort_keys=True),
            at,
        ),
    )
