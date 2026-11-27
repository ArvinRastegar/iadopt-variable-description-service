"""Accounts, identities, the owner and admin roles and blocks (``components/accounts-roles.md``).

Contract approved under gate G-017: who may change whom (section 4), the owner (section 5), the
account side of the identity-provider login that ``oidc-identity`` calls from increment 2 (section
7) and the offline cutover commands (section 8). Like ``RecordStore``, the store follows the auth
store's database path on every call. Each change reads what decides it, changes the account and
writes its history row in one immediate transaction.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import storage
from .access.policy import MESSAGES
from .account_model import (
    ACCOUNT_COLUMNS,
    DEVELOPMENT_ACTOR,
    LOCAL_ISSUER,
    UNUSABLE_PASSWORD,
    account_from_row,
    add_history,
    find_account,
    owner_id,
)
from .auth import AuthStore, utc_iso

MAX_REASON_CHARS = 500
"""Longest reason for a role or block change."""

MAX_SUBJECT_CHARS = 255
"""Longest identity-provider subject."""

MAX_EMAIL_CHARS = 254
MAX_DISPLAY_NAME_CHARS = 200
MAX_USERNAME_CHARS = 120
"""How much of an identity provider's claims an account keeps."""

MIN_OWNER_PASSWORD_CHARS = 15
MAX_OWNER_PASSWORD_CHARS = 1024
"""The length of the owner's password that ``owner password`` sets (owner-sign-in, section 6; D-054)."""

_REFUSALS: Dict[str, Tuple[int, str]] = {
    "blocked": (403, MESSAGES["blocked"]),
    "admin_required": (403, MESSAGES["admin_required"]),
    "owner_protected": (403, "The owner cannot be blocked or lose admin."),
    "own_account": (403, "You cannot block or unblock your own account."),
    "owner_required": (403, MESSAGES["owner_required"]),
    "provider_account": (400, "This account's email and sign-in belong to its identity provider."),
    "target_blocked": (400, "Unblock the account before granting admin."),
    "approval_not_needed": (400, "A local account needs no approval."),
    "approval_unchanged": (400, "This account is already approved; see its history."),
}
"""Section 4's refusals: 403 for who may not act on whom, 400 for what the target does not allow. The
first two are access-policy's, for an actor who lost the capability while the request waited."""

_APPROVAL_MESSAGES: Dict[str, str] = {
    "owner_protected": "The owner needs no approval.",
    "own_account": "You cannot approve your own account or withdraw its approval.",
}
"""What the same refusals say about an approval."""

_NOT_APPROVED = "This account is not approved; see its history."

_HELD_DATA = {
    "record items": "SELECT COUNT(*) FROM record_items WHERE user_id = ?",
    "client contexts": "SELECT COUNT(*) FROM client_contexts WHERE user_id = ?",
    "saved ORCID iDs": "SELECT COUNT(*) FROM saved_orcids WHERE user_id = ?",
    "history rows": "SELECT COUNT(*) FROM account_history WHERE user_id = ?",
}
"""What keeps an account from being folded into another, with the query that counts it."""


class AccountRefused(Exception):
    """A change section 4 does not allow, with its reason code, status and message."""

    def __init__(self, reason: str, message: Optional[str] = None) -> None:
        """Look up the reason's status and message.

        Args:
            reason: One of section 4's reason codes.
            message: What to say instead of the reason's own message.
        """
        self.reason = reason
        self.status_code, default = _REFUSALS[reason]
        self.message = message or default
        super().__init__(self.message)


class UnknownAccount(LookupError):
    """No account has that id."""


class CommandRefused(Exception):
    """An offline command's refusal; nothing was changed."""


def check_identity(issuer: str, subject: str) -> None:
    """Check that an identity can be one of an identity provider's.

    Args:
        issuer: An absolute ``http`` or ``https`` URL, so never ``local``.
        subject: 1 to 255 characters.

    Raises:
        ValueError: If either is not.
    """
    if not isinstance(issuer, str) or not _is_absolute_url(issuer):
        raise ValueError("The issuer must be an absolute http or https URL.")
    if not isinstance(subject, str) or not 1 <= len(subject) <= MAX_SUBJECT_CHARS:
        raise ValueError(f"The subject must have 1 to {MAX_SUBJECT_CHARS} characters.")


def _is_absolute_url(text: str) -> bool:
    """Return whether a text is an absolute ``http`` or ``https`` URL."""
    parts = urlsplit(text)
    # urlsplit forgives spaces and control characters that no URL has.
    clean = all(character.isprintable() and not character.isspace() for character in text)
    return clean and parts.scheme in ("http", "https") and bool(parts.hostname)


def _claim(claims: Mapping[str, Any], name: str) -> str:
    """A text claim, trimmed; empty when it is absent or not text."""
    value = claims.get(name)
    return value.strip() if isinstance(value, str) else ""


def _display_name(claims: Mapping[str, Any]) -> str:
    """The display name of an identity provider's claims (section 7)."""
    full_name = " ".join(part for part in (_claim(claims, "given_name"), _claim(claims, "family_name")) if part)
    return (_claim(claims, "name") or full_name or _claim(claims, "preferred_username"))[:MAX_DISPLAY_NAME_CHARS]


def _wanted_username(claims: Mapping[str, Any]) -> str:
    """The username an identity provider's claims ask for, before it is made unique (section 7)."""
    wanted = _claim(claims, "preferred_username") or _claim(claims, "email").rpartition("@")[0] or "user"
    return wanted[:MAX_USERNAME_CHARS].strip()


def _reason(reason: Optional[str], *, required: bool = False) -> Optional[str]:
    """Trim and check the reason of a role or block change; an empty optional one counts as none.

    Raises:
        ValueError: If a required reason is missing, or the reason is too long.
    """
    text = (reason or "").strip()
    if len(text) > MAX_REASON_CHARS or (required and not text):
        raise ValueError(f"Give a reason of 1 to {MAX_REASON_CHARS} characters.")
    return text or None


class AccountStore:
    """Accounts kept in the auth database: their identities, roles, blocks and history."""

    def __init__(self, auth: AuthStore) -> None:
        """Bind the store to the auth store whose database and accounts it shares.

        Args:
            auth: The process-wide auth store.
        """
        self.auth = auth

    def _transaction(self, *, immediate: bool = False) -> AbstractContextManager[sqlite3.Connection]:
        """Open a transaction on the auth database; immediate for check-and-write sections."""
        return storage.transaction(immediate, path=self.auth.db_path)

    # ------------------------------------------------------------------ #
    # Views
    # ------------------------------------------------------------------ #

    def list_accounts(self) -> List[Dict[str, Any]]:
        """Return all accounts with their identities, active first then by username."""
        with self._transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT {ACCOUNT_COLUMNS} FROM users
                 ORDER BY users.is_active DESC, users.username COLLATE NOCASE ASC
                """
            ).fetchall()
            identities: Dict[int, List[Dict[str, Any]]] = {}
            for row in conn.execute(
                "SELECT user_id, issuer, subject, last_login_at FROM account_identities ORDER BY created_at, issuer, subject"
            ):
                identity = {key: row[key] for key in ("issuer", "subject", "last_login_at")}
                identities.setdefault(row["user_id"], []).append(identity)
        accounts = [account_from_row(row) for row in rows]
        return [{**account, "identities": identities.get(account["id"], [])} for account in accounts]

    def history(self, user_id: int) -> List[Dict[str, Any]]:
        """Return an account's changes, newest first; ``UnknownAccount`` when there is no such account.

        Args:
            user_id: The account's id.

        Returns:
            Dicts with ``action``, ``actor`` (the acting account's username, or ``offline`` or
            ``bootstrap``), ``reason``, ``details`` and ``created_at``.
        """
        with self._transaction() as conn:
            self._account(conn, user_id)
            return self._history(conn, user_id)

    def _history(self, conn: sqlite3.Connection, user_id: int) -> List[Dict[str, Any]]:
        """Read an account's changes on an open connection, newest first."""
        rows = conn.execute(
            """
            SELECT account_history.*, users.username AS actor_username
              FROM account_history
              LEFT JOIN users ON users.id = account_history.actor_user_id
             WHERE account_history.user_id = ?
             ORDER BY account_history.id DESC
            """,
            (user_id,),
        ).fetchall()
        return [
            {
                "action": row["action"],
                # Accounts are never deleted: an acting account without a row is the development user.
                "actor": row["actor_username"] or (DEVELOPMENT_ACTOR if row["actor_kind"] == "account" else row["actor_kind"]),
                "reason": row["reason"],
                "details": json.loads(row["details"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _account(self, conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
        """Return an account with its identities.

        Raises:
            UnknownAccount: If no account has that id.
        """
        account = find_account(conn, user_id)
        if account is None:
            raise UnknownAccount("User not found.")
        return {**account, "identities": self._identities(conn, user_id)}

    def _identities(self, conn: sqlite3.Connection, user_id: int) -> List[Dict[str, Any]]:
        """Read an account's identities, oldest first."""
        rows = conn.execute(
            """
            SELECT issuer, subject, last_login_at FROM account_identities
             WHERE user_id = ?
             ORDER BY created_at, issuer, subject
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Who may change whom (section 4)
    # ------------------------------------------------------------------ #

    def grant_admin(
        self, *, actor: Dict[str, Any], user_id: int, reason: Optional[str] = None
    ) -> Tuple[Dict[str, Any], bool]:
        """Make an ordinary account an admin; only the owner may.

        Args:
            actor: The signed-in user.
            user_id: The account to change; ``UnknownAccount`` when there is none.
            reason: Why, optionally.

        Returns:
            The account, and whether it changed.

        Raises:
            AccountRefused: ``owner_required`` for anyone but the owner, ``target_blocked`` for a
                blocked account.
        """
        reason = _reason(reason)
        with self._transaction(immediate=True) as conn:
            target = self._account(conn, user_id)
            if owner_id(conn) != actor["id"]:
                raise AccountRefused("owner_required")
            if not target["is_active"]:
                raise AccountRefused("target_blocked")
            if target["role"] != "user":
                return target, False
            return self._set_admin(conn, target, True, actor=actor, action="admin_granted", reason=reason), True

    def revoke_admin(
        self, *, actor: Dict[str, Any], user_id: int, reason: Optional[str] = None
    ) -> Tuple[Dict[str, Any], bool]:
        """Make an admin an ordinary account; only the owner may, and never the owner's own account.

        Args:
            actor: The signed-in user.
            user_id: The account to change; ``UnknownAccount`` when there is none.
            reason: Why, optionally.

        Returns:
            The account, and whether it changed.

        Raises:
            AccountRefused: ``owner_required`` for anyone but the owner, ``owner_protected`` for
                the owner's account.
        """
        reason = _reason(reason)
        with self._transaction(immediate=True) as conn:
            target = self._account(conn, user_id)
            owner = owner_id(conn)
            if owner != actor["id"]:
                raise AccountRefused("owner_required")
            if target["id"] == owner:
                raise AccountRefused("owner_protected")
            if target["role"] != "admin":
                return target, False
            return self._set_admin(conn, target, False, actor=actor, action="admin_revoked", reason=reason), True

    def block(self, *, actor: Dict[str, Any], user_id: int, reason: Optional[str]) -> Tuple[Dict[str, Any], bool]:
        """Block an account, which ends its sessions at once. Refusals are those of section 4.

        Args:
            actor: The signed-in admin or owner.
            user_id: The account to block; ``UnknownAccount`` when there is none.
            reason: Why, 1 to 500 characters.

        Returns:
            The account, and whether it changed.
        """
        reason = _reason(reason, required=True)
        with self._transaction(immediate=True) as conn:
            self._check_actor(conn, actor)
            target = self._account(conn, user_id)
            self._check_block(conn, actor, target)
            if not target["is_active"]:
                return target, False
            return self._set_active(conn, target, False, actor=actor, action="blocked", reason=reason), True

    def unblock(
        self, *, actor: Dict[str, Any], user_id: int, reason: Optional[str] = None
    ) -> Tuple[Dict[str, Any], bool]:
        """Unblock an account; its data is as it was. Refusals are those of section 4.

        Args:
            actor: The signed-in admin or owner.
            user_id: The account to unblock; ``UnknownAccount`` when there is none.
            reason: Why, optionally.

        Returns:
            The account, and whether it changed.
        """
        reason = _reason(reason)
        with self._transaction(immediate=True) as conn:
            self._check_actor(conn, actor)
            target = self._account(conn, user_id)
            self._check_block(conn, actor, target)
            if target["is_active"]:
                return target, False
            return self._set_active(conn, target, True, actor=actor, action="unblocked", reason=reason), True

    def approve(
        self, *, actor: Dict[str, Any], user_id: int, reason: Optional[str] = None
    ) -> Tuple[Dict[str, Any], bool]:
        """Approve an account that an identity provider signs in: it may then do what a local account may.

        Who may act on whom is as with a block, and an account without an identity needs no approval.

        Args:
            actor: The signed-in admin or owner.
            user_id: The account to approve; ``UnknownAccount`` when there is none.
            reason: A note, such as how the terms were accepted; it is kept or the change is refused.

        Returns:
            The account, and whether it changed.
        """
        return self._change_approval(True, actor=actor, user_id=user_id, reason=reason)

    def withdraw_approval(
        self, *, actor: Dict[str, Any], user_id: int, reason: Optional[str] = None
    ) -> Tuple[Dict[str, Any], bool]:
        """Withdraw an account's approval, which reaches its sessions at their next request.

        Args:
            actor: The signed-in admin or owner.
            user_id: The account; ``UnknownAccount`` when there is none.
            reason: A note; it is kept or the change is refused.

        Returns:
            The account, and whether it changed.
        """
        return self._change_approval(False, actor=actor, user_id=user_id, reason=reason)

    def _change_approval(
        self, approved: bool, *, actor: Dict[str, Any], user_id: int, reason: Optional[str]
    ) -> Tuple[Dict[str, Any], bool]:
        """Store or remove an account's approval, with its history row.

        Raises:
            AccountRefused: For whom the actor may not act on, ``approval_not_needed`` for an account
                without an identity, and ``approval_unchanged`` for a note with nothing to keep it with.
        """
        reason = _reason(reason)
        with self._transaction(immediate=True) as conn:
            self._check_actor(conn, actor)
            target = self._account(conn, user_id)
            self._check_block(conn, actor, target, _APPROVAL_MESSAGES)
            if not target["has_identity"]:
                raise AccountRefused("approval_not_needed")
            if bool(target["approved_at"]) == approved:
                if reason:
                    raise AccountRefused("approval_unchanged", None if approved else _NOT_APPROVED)
                return target, False
            now = utc_iso()
            conn.execute(
                "UPDATE users SET approved_at = ?, approved_by = ?, updated_at = ? WHERE id = ?",
                (now if approved else None, actor["id"] if approved else None, now, user_id),
            )
            add_history(
                conn,
                user_id=user_id,
                action="approved" if approved else "approval_withdrawn",
                actor_kind="account",
                actor_user_id=actor["id"],
                reason=reason,
                at=now,
            )
            return self._account(conn, user_id), True

    def _check_actor(self, conn: sqlite3.Connection, actor: Dict[str, Any]) -> None:
        """Refuse an actor who was blocked or lost admin while the request waited for the write lock.

        Raises:
            AccountRefused: ``blocked`` or ``admin_required``.
        """
        if not self.auth.enabled:
            return  # the development user has no account
        current = find_account(conn, actor["id"])
        if current is None or not current["is_active"]:
            raise AccountRefused("blocked")
        if current["role"] == "user":
            raise AccountRefused("admin_required")

    def _check_block(
        self,
        conn: sqlite3.Connection,
        actor: Dict[str, Any],
        target: Dict[str, Any],
        messages: Mapping[str, str] = {},
    ) -> None:
        """Refuse a block, unblock or approval change section 4 does not allow; the first refusal of its table wins.

        Args:
            conn: The transaction's connection.
            actor: Who acts.
            target: The account acted on.
            messages: What the refusals say instead of what they say about a block.

        Raises:
            AccountRefused: ``owner_protected``, ``own_account`` or ``owner_required``.
        """
        owner = owner_id(conn)
        if target["id"] == owner:
            raise AccountRefused("owner_protected", messages.get("owner_protected"))
        if target["id"] == actor["id"]:
            raise AccountRefused("own_account", messages.get("own_account"))
        if target["role"] == "admin" and actor["id"] != owner:
            raise AccountRefused("owner_required", messages.get("owner_required"))

    def _set_admin(
        self,
        conn: sqlite3.Connection,
        target: Dict[str, Any],
        admin: bool,
        *,
        actor: Dict[str, Any],
        action: str,
        reason: Optional[str],
    ) -> Dict[str, Any]:
        """Change whether an account is an admin, with its history row; return the account after it."""
        now = utc_iso()
        self._write_roles(conn, target, admin=admin, at=now)
        add_history(
            conn, user_id=target["id"], action=action, actor_kind="account", actor_user_id=actor["id"], reason=reason, at=now
        )
        return self._account(conn, target["id"])

    def _set_active(
        self,
        conn: sqlite3.Connection,
        target: Dict[str, Any],
        active: bool,
        *,
        actor: Dict[str, Any],
        action: str,
        reason: Optional[str],
    ) -> Dict[str, Any]:
        """Change whether an account is blocked, with its history row; return the account after it."""
        now = utc_iso()
        self._write_active(conn, target["id"], active, at=now)
        add_history(
            conn, user_id=target["id"], action=action, actor_kind="account", actor_user_id=actor["id"], reason=reason, at=now
        )
        return self._account(conn, target["id"])

    def _write_roles(self, conn: sqlite3.Connection, target: Dict[str, Any], *, admin: bool, at: str) -> None:
        """Store an account's roles with or without ``admin``, as the previous image stores them."""
        roles = {role for role in target["roles"] if role != "admin"} | {"user"} | ({"admin"} if admin else set())
        conn.execute("UPDATE users SET roles = ?, updated_at = ? WHERE id = ?", (json.dumps(sorted(roles)), at, target["id"]))

    def _write_active(self, conn: sqlite3.Connection, user_id: int, active: bool, *, at: str) -> None:
        """Store an account's block state; a block ends its sessions of both kinds."""
        conn.execute("UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?", (int(active), at, user_id))
        if not active:
            self._end_sessions(conn, user_id)

    def _end_sessions(self, conn: sqlite3.Connection, user_id: int) -> None:
        """End an account's sessions of both kinds: a password's and ROHub's."""
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM oidc_sessions WHERE user_id = ?", (user_id,))

    # ------------------------------------------------------------------ #
    # Local accounts, until the cutover (D-045)
    # ------------------------------------------------------------------ #

    def create_local(
        self,
        *,
        actor: Dict[str, Any],
        username: str,
        password: str,
        display_name: str = "",
        email: str = "",
        roles: Sequence[str] = ("user",),
        is_active: bool = True,
    ) -> Dict[str, Any]:
        """Create an ordinary local account; the owner grants admin afterwards.

        Args:
            actor: The signed-in admin or owner.
            username: Login name.
            password: Plaintext password.
            display_name: Optional display name.
            email: Optional email.
            roles: Only ``["user"]`` is accepted.
            is_active: Only true is accepted.

        Returns:
            The account with its identities.

        Raises:
            ValueError: If other roles or a blocked account are asked for, a value is not allowed,
                or the username is taken.
        """
        if list(roles) != ["user"]:
            raise ValueError("A new account is an ordinary user. The owner grants admin afterwards.")
        if not is_active:
            raise ValueError("A new account is active. Block it afterwards.")
        username = self.auth.normalize_username(username)
        password_hash = self.auth.hash_new_password(password)
        with self._transaction(immediate=True) as conn:
            self._check_actor(conn, actor)
            account = self.auth.insert_user(
                conn, username=username, password_hash=password_hash, display_name=display_name, email=email
            )
            return self._account(conn, account["id"])

    def update_local(self, *, actor: Dict[str, Any], user_id: int, updates: Mapping[str, Any]) -> Dict[str, Any]:
        """Edit a local account's username, display name or email, or reset its password.

        An admin may edit ordinary accounts and their own, the owner any local account.

        Args:
            actor: The signed-in admin or owner.
            user_id: The account to edit; ``UnknownAccount`` when there is none.
            updates: Only the fields to change.

        Returns:
            The account with its identities.

        Raises:
            ValueError: If ``roles`` or ``is_active`` is given, a value is not allowed, or the
                username is taken.
            AccountRefused: ``owner_required`` for an admin acting on another admin or the owner,
                ``provider_account`` for an account without a usable password.
        """
        if "roles" in updates:
            raise ValueError("Use grant-admin or revoke-admin.")
        if "is_active" in updates:
            raise ValueError("Use block or unblock.")
        values: Dict[str, Any] = {}
        if updates.get("username") is not None:
            values["username"] = self.auth.normalize_username(str(updates["username"]))
        if updates.get("password"):
            values["password_hash"] = self.auth.hash_new_password(str(updates["password"]))
        for field in ("display_name", "email"):
            if field in updates:
                values[field] = str(updates[field] or "").strip()

        with self._transaction(immediate=True) as conn:
            self._check_actor(conn, actor)
            target = self._account(conn, user_id)
            if target["role"] != "user" and actor["id"] not in (target["id"], owner_id(conn)):
                raise AccountRefused("owner_required")
            if not target["local"]:
                raise AccountRefused("provider_account")
            if not values:
                return target
            values["updated_at"] = utc_iso()
            assignments = ", ".join(f"{column} = ?" for column in values)
            try:
                conn.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*values.values(), user_id))
            except sqlite3.IntegrityError as e:
                raise ValueError("A user with that username already exists.") from e
            return self._account(conn, user_id)

    # ------------------------------------------------------------------ #
    # Identity-provider accounts (section 7)
    # ------------------------------------------------------------------ #

    def account_for_identity(self, issuer: str, subject: str) -> Optional[Dict[str, Any]]:
        """Return the account holding the identity, or ``None``; never create one (section 7).

        Args:
            issuer: The identity provider's issuer URL.
            subject: The subject the provider gives the person.
        """
        with self._transaction() as conn:
            return self._holder(conn, issuer, subject)

    def ensure_account(self, issuer: str, subject: str, claims: Mapping[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """Return the account holding the identity, creating an ordinary one when there is none.

        Section 7: refreshes email, ``email_verified``, display name and last login of a found
        account without touching its role or block state; never matches by email, username or
        name, and never reads roles or groups from the claims. The account is the auth store's
        user dict with ``email_verified`` added. ``ValueError`` when the issuer or subject cannot
        be an identity provider's (:func:`check_identity`).

        Args:
            issuer: An absolute ``http`` or ``https`` URL, never ``local``.
            subject: 1 to 255 characters.
            claims: The identity provider's claims.

        Returns:
            The account, and whether it was created.
        """
        check_identity(issuer, subject)
        email = _claim(claims, "email")[:MAX_EMAIL_CHARS]
        email_verified = int(claims.get("email_verified") is True)
        display_name = _display_name(claims)
        now = utc_iso()
        with self._transaction(immediate=True) as conn:
            account = self._holder(conn, issuer, subject)
            created = account is None
            if account is None:
                cursor = conn.execute(
                    """
                    INSERT INTO users (
                        username, password_hash, display_name, email, email_verified, roles, is_active,
                        auth_provider, external_subject, created_at, updated_at, last_login_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 1, 'oidc', ?, ?, ?, ?)
                    """,
                    (
                        self._unique_username(conn, _wanted_username(claims)),
                        UNUSABLE_PASSWORD,
                        display_name,
                        email,
                        email_verified,
                        json.dumps(["user"]),
                        subject,
                        now,
                        now,
                        now,
                    ),
                )
                user_id = int(cursor.lastrowid or 0)
                conn.execute(
                    "INSERT INTO account_identities (issuer, subject, user_id, created_at, last_login_at) VALUES (?, ?, ?, ?, ?)",
                    (issuer, subject, user_id, now, now),
                )
            else:
                user_id = int(account["id"])
                conn.execute(
                    """
                    UPDATE users
                       SET email = ?, email_verified = ?, display_name = ?, last_login_at = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (email, email_verified, display_name, now, now, user_id),
                )
                conn.execute(
                    "UPDATE account_identities SET last_login_at = ? WHERE issuer = ? AND subject = ?", (now, issuer, subject)
                )
            return self._account(conn, user_id), created

    def _holder(self, conn: sqlite3.Connection, issuer: str, subject: str) -> Optional[Dict[str, Any]]:
        """Return the account that holds an identity, or ``None``."""
        row = conn.execute(
            "SELECT user_id FROM account_identities WHERE issuer = ? AND subject = ?", (issuer, subject)
        ).fetchone()
        return self._account(conn, row["user_id"]) if row else None

    def _unique_username(self, conn: sqlite3.Connection, wanted: str) -> str:
        """Return the wanted username, with ``-2``, ``-3`` and so on appended while it is taken, ignoring case."""
        # SQLite's NOCASE knows only the case of ASCII letters, but it decides what is unique.
        taken = {row["username"].casefold() for row in conn.execute("SELECT username FROM users")}
        username, number = wanted, 1
        while (
            username.casefold() in taken
            or conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        ):
            number += 1
            suffix = f"-{number}"
            username = wanted[: MAX_USERNAME_CHARS - len(suffix)] + suffix
        return username

    # ------------------------------------------------------------------ #
    # The owner (section 5): offline commands only
    # ------------------------------------------------------------------ #

    def bind_owner(
        self,
        *,
        username: Optional[str] = None,
        account_id: Optional[int] = None,
        identity: Optional[Tuple[str, str]] = None,
    ) -> Dict[str, Any]:
        """Bind the owner to an existing account, which becomes active and an admin; its sessions end.

        A session opened before the binding never starts to count by it (owner-sign-in, section 4).

        Args:
            username: The account, by its username.
            account_id: The account, by its id.
            identity: The account, by an identity (issuer, subject) it already holds.

        Returns:
            The owner's account.

        Raises:
            CommandRefused: If an owner is already bound or there is no such account.
        """
        with self._transaction(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM owner_binding").fetchone():
                raise CommandRefused("An owner is already bound. Use owner rebind to move the binding.")
            account, binding = self._owner_target(conn, username=username, account_id=account_id, identity=identity)
            return self._bind(conn, account, binding, action="owner_bound")

    def rebind_owner(
        self,
        *,
        reason: str,
        username: Optional[str] = None,
        account_id: Optional[int] = None,
        identity: Optional[Tuple[str, str]] = None,
    ) -> Dict[str, Any]:
        """Move the owner binding; also restores an owner account blocked or demoted outside the rules.

        The previous owner stays an admin until the new owner revokes it. The sessions of both accounts
        end (owner-sign-in, section 4).

        Args:
            reason: Why.
            username: The new owner's account, by its username.
            account_id: The new owner's account, by its id.
            identity: The new owner's account, by an identity (issuer, subject) it already holds.

        Returns:
            The owner's account.

        Raises:
            CommandRefused: If no owner is bound or there is no such account.
        """
        with self._transaction(immediate=True) as conn:
            previous = conn.execute("SELECT issuer, subject FROM owner_binding").fetchone()
            if not previous:
                raise CommandRefused("No owner is bound. Use owner bind.")
            previous_id = owner_id(conn)
            previous_account = find_account(conn, previous_id) if previous_id is not None else None
            account, binding = self._owner_target(conn, username=username, account_id=account_id, identity=identity)
            details = {
                "previous_owner": {
                    "account_id": previous_id,
                    "username": previous_account["username"] if previous_account else None,
                    "issuer": previous["issuer"],
                    "subject": previous["subject"],
                }
            }
            if previous_id is not None:
                self._end_sessions(conn, previous_id)
            return self._bind(conn, account, binding, action="owner_rebound", reason=reason, details=details)

    def _owner_target(
        self,
        conn: sqlite3.Connection,
        *,
        username: Optional[str],
        account_id: Optional[int],
        identity: Optional[Tuple[str, str]],
    ) -> Tuple[Dict[str, Any], Tuple[str, str]]:
        """Return the account an owner command names, and the (issuer, subject) that binds it.

        Raises:
            CommandRefused: If no account holds the identity.
        """
        if identity is None:
            account = self._named_account(conn, username=username, account_id=account_id)
            return account, (LOCAL_ISSUER, str(account["id"]))
        holder = self._holder(conn, *identity)
        if holder is None:
            raise CommandRefused(f"No account holds the subject {identity[1]} of {identity[0]}.")
        return holder, identity

    def _bind(
        self,
        conn: sqlite3.Connection,
        account: Dict[str, Any],
        binding: Tuple[str, str],
        *,
        action: str,
        reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Store the owner binding and make the account an active admin, with its history row; end its sessions."""
        now = utc_iso()
        conn.execute(
            """
            INSERT INTO owner_binding (id, issuer, subject, bound_at) VALUES (1, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET issuer = excluded.issuer, subject = excluded.subject, bound_at = excluded.bound_at
            """,
            (*binding, now),
        )
        self._write_roles(conn, account, admin=True, at=now)
        self._write_active(conn, account["id"], True, at=now)
        self._end_sessions(conn, account["id"])
        add_history(
            conn,
            user_id=account["id"],
            action=action,
            actor_kind="offline",
            reason=reason,
            details={
                "issuer": binding[0],
                "subject": binding[1],
                "was_blocked": not account["is_active"],
                "was_admin": "admin" in account["roles"],
                **(details or {}),
            },
            at=now,
        )
        return self._account(conn, account["id"])

    def owner_account(self) -> Dict[str, Any]:
        """Return the account the owner binding resolves to; ``CommandRefused`` when no owner is bound."""
        with self._transaction() as conn:
            return self._account(conn, self._check_owner_bound(conn))

    def set_owner_password(self, password: str) -> Dict[str, Any]:
        """Set the password of the account the owner binding resolves to, whatever identities it holds.

        Records ``owner_password_changed`` in its history and ends its sessions of both kinds
        (owner-sign-in, section 6).

        Args:
            password: The new password, 15 to 1,024 characters.

        Returns:
            The owner's account.

        Raises:
            CommandRefused: If the password is too short or too long, or no owner is bound.
        """
        if not MIN_OWNER_PASSWORD_CHARS <= len(password) <= MAX_OWNER_PASSWORD_CHARS:
            raise CommandRefused(
                f"The password must have {MIN_OWNER_PASSWORD_CHARS} to {MAX_OWNER_PASSWORD_CHARS} characters. "
                "Nothing was changed."
            )
        password_hash = self.auth.hash_password(password)  # outside the transaction: hashing takes a while
        with self._transaction(immediate=True) as conn:
            owner = self._check_owner_bound(conn)
            now = utc_iso()
            conn.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (password_hash, now, owner))
            add_history(conn, user_id=owner, action="owner_password_changed", actor_kind="offline", at=now)
            self._end_sessions(conn, owner)
            return self._account(conn, owner)

    # ------------------------------------------------------------------ #
    # Cutover commands (section 8): offline
    # ------------------------------------------------------------------ #

    def describe(self, *, username: Optional[str] = None, account_id: Optional[int] = None) -> Dict[str, Any]:
        """Return what ``legacy show`` prints about an account; ``CommandRefused`` when there is none.

        Args:
            username: The account, by its username.
            account_id: The account, by its id.
        """
        with self._transaction() as conn:
            account = self._named_account(conn, username=username, account_id=account_id)
            held = {name: self._held(conn, name, account["id"]) for name in ("saved ORCID iDs", "record items")}
            return {
                "account": account["id"],
                "username": account["username"],
                "display_name": account["display_name"],
                "email": account["email"],
                "role": account["role"],
                "blocked": not account["is_active"],
                "local": account["local"],
                "identities": account["identities"],
                "saved_orcid_ids": held["saved ORCID iDs"],
                "records": held["record items"],
                "last_login_at": account["last_login_at"],
                "history": self._history(conn, account["id"]),
            }

    def link_identity(
        self, *, account_id: int, issuer: str, subject: str, reason: str, keep_admin: bool = False
    ) -> Dict[str, Any]:
        """Pre-link an identity-provider subject to a legacy local account.

        The subject's first login then lands in that account, with its id, records and saved ORCID
        iDs. The account keeps ``admin`` only with ``keep_admin``, or as the owner's account.

        Args:
            account_id: The legacy local account.
            issuer: The identity provider's issuer URL.
            subject: The subject the provider gives the person.
            reason: Why.
            keep_admin: Whether an admin stays one.

        Returns:
            The account.

        Raises:
            CommandRefused: If no owner is bound, the identity cannot be one, it is already held, or
                the account is not a legacy local account.
        """
        try:
            check_identity(issuer, subject)
        except ValueError as e:
            raise CommandRefused(str(e)) from e
        with self._transaction(immediate=True) as conn:
            self._check_owner_bound(conn)
            account = self._legacy_account(conn, account_id)
            holder = self._holder(conn, issuer, subject)
            if holder is not None:
                raise CommandRefused(
                    f"That identity already belongs to account {holder['id']} ({holder['username']}). Use legacy fold."
                )
            now = utc_iso()
            conn.execute(
                "INSERT INTO account_identities (issuer, subject, user_id, created_at) VALUES (?, ?, ?, ?)",
                (issuer, subject, account_id, now),
            )
            removed = self._drop_admin(conn, account, keep_admin=keep_admin, at=now)
            add_history(
                conn,
                user_id=account_id,
                action="identity_linked",
                actor_kind="offline",
                reason=reason,
                details={"issuer": issuer, "subject": subject, "admin_removed": removed},
                at=now,
            )
            return self._account(conn, account_id)

    def fold_account(self, *, from_id: int, into_id: int, reason: str, keep_admin: bool = False) -> Dict[str, Any]:
        """Move every identity of a new identity-provider account into a legacy local account.

        The new account must be empty and not the owner's; it is blocked and kept.

        Args:
            from_id: The new identity-provider account.
            into_id: The legacy local account.
            reason: Why.
            keep_admin: Whether an admin stays one.

        Returns:
            The legacy account.

        Raises:
            CommandRefused: If no owner is bound, or either account is not what it must be; the
                message names what stands in the way.
        """
        with self._transaction(immediate=True) as conn:
            self._check_owner_bound(conn)
            emptied = self._named_account(conn, account_id=from_id)
            if emptied["local"] or not emptied["identities"]:
                raise CommandRefused(f"Account {from_id} ({emptied['username']}) is not an identity-provider account.")
            account = self._legacy_account(conn, into_id)
            counts = {name: self._held(conn, name, from_id) for name in _HELD_DATA}
            obstacles = [f"it holds {count} {name}" for name, count in counts.items() if count]
            if owner_id(conn) == from_id:
                obstacles.append("it is the owner's account (use owner rebind first)")
            if obstacles:
                raise CommandRefused(
                    f"Account {from_id} ({emptied['username']}) cannot be folded: {'; '.join(obstacles)}."
                )
            now = utc_iso()
            identities = [{"issuer": item["issuer"], "subject": item["subject"]} for item in emptied["identities"]]
            conn.execute("UPDATE account_identities SET user_id = ? WHERE user_id = ?", (into_id, from_id))
            self._write_active(conn, from_id, False, at=now)
            removed = self._drop_admin(conn, account, keep_admin=keep_admin, at=now)
            for user_id, details in (
                (from_id, {"into": into_id, "identities": identities, "blocked": True}),
                (into_id, {"from": from_id, "identities": identities, "admin_removed": removed}),
            ):
                add_history(
                    conn,
                    user_id=user_id,
                    action="account_folded",
                    actor_kind="offline",
                    reason=reason,
                    details=details,
                    at=now,
                )
            return self._account(conn, into_id)

    def count_usable_passwords(self) -> int:
        """Return how many accounts the purge would change: a usable password, the owner's account not counted."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM users WHERE password_hash <> ? AND id IS NOT ?", (UNUSABLE_PASSWORD, owner_id(conn))
            ).fetchone()
        return int(row[0])

    def purge_passwords(self) -> List[Dict[str, Any]]:
        """Replace every usable password hash but the owner's, so that no other password login succeeds (D-017).

        The owner keeps a password sign-in of their own (owner-sign-in, section 6). ``CommandRefused`` when
        no owner is bound.

        Returns:
            The accounts whose password was purged.
        """
        with self._transaction(immediate=True) as conn:
            owner = self._check_owner_bound(conn)
            now = utc_iso()
            rows = conn.execute(
                "SELECT id FROM users WHERE password_hash <> ? AND id <> ? ORDER BY id", (UNUSABLE_PASSWORD, owner)
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (UNUSABLE_PASSWORD, now, row["id"])
                )
                add_history(conn, user_id=row["id"], action="password_purged", actor_kind="offline", at=now)
            return [self._account(conn, row["id"]) for row in rows]

    def _check_owner_bound(self, conn: sqlite3.Connection) -> int:
        """Refuse a cutover command that changes accounts before an owner is bound: they are the owner's.

        Without an owner nobody can revoke admin (section 4), so no command may remove it either.

        Returns:
            The id of the owner's account.

        Raises:
            CommandRefused: If no owner is bound.
        """
        owner = owner_id(conn)
        if owner is None:
            raise CommandRefused("No owner is bound. Use owner bind first.")
        return owner

    def _named_account(
        self, conn: sqlite3.Connection, *, username: Optional[str] = None, account_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Return the account a command names by username or id.

        Raises:
            CommandRefused: If there is none.
        """
        if username is not None:
            row = conn.execute("SELECT id FROM users WHERE username = ?", (username.strip(),)).fetchone()
            if not row:
                raise CommandRefused(f"No account has the username {username!r}.")
            account_id = int(row["id"])
        try:
            return self._account(conn, int(account_id or 0))
        except UnknownAccount as e:
            raise CommandRefused(f"No account has the id {account_id}.") from e

    def _legacy_account(self, conn: sqlite3.Connection, account_id: int) -> Dict[str, Any]:
        """Return a legacy local account: a usable password and no identity-provider identity.

        Raises:
            CommandRefused: If there is no such account.
        """
        account = self._named_account(conn, account_id=account_id)
        if not account["local"] or account["identities"]:
            raise CommandRefused(
                f"Account {account_id} ({account['username']}) is not a legacy local account: "
                f"{'it already holds an identity' if account['identities'] else 'it has no usable password'}."
            )
        return account

    def _drop_admin(self, conn: sqlite3.Connection, account: Dict[str, Any], *, keep_admin: bool, at: str) -> bool:
        """Remove ``admin`` from a linked account unless it is kept or the owner's; return whether it was removed."""
        if keep_admin or account["role"] != "admin":
            return False
        self._write_roles(conn, account, admin=False, at=at)
        return True

    def _held(self, conn: sqlite3.Connection, name: str, user_id: int) -> int:
        """Count one kind of data an account holds (see :data:`_HELD_DATA`)."""
        return int(conn.execute(_HELD_DATA[name], (user_id,)).fetchone()[0])
