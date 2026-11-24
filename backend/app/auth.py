"""Authentication store: local users, sessions, and audit logging.

Owns the SQLite-backed user store, PBKDF2-SHA256 password hashing, HMAC-signed
opaque session tokens, and the audit-event log. This module is security-sensitive:
it handles password hashing, session signing, and (via the bootstrap admin) initial
credentials. It is a leaf below the service/router layers — nothing in ``app.auth``
imports services or routers. The tables come from the migrations of ``app.storage``;
roles, blocks and the owner belong to ``app.accounts``.

Authentication is local username/password (NOT ORCID, NOT JWT). Sessions are random
``secrets.token_urlsafe(32)`` tokens, HMAC-SHA256-signed with ``IADOPT_SESSION_SECRET``
and stored SHA-256-hashed in SQLite; only the hash is persisted. See
iadopt-variable-description-service-docs/CONTRACTS.md for the full contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import secrets
import sqlite3
import threading
import time
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, Response, status

from . import storage
from .account_model import ACCOUNT_COLUMNS, account_from_row, add_history, find_account
from .schemas.common import canonical_orcid_uri

logger = logging.getLogger(__name__)

MAX_SAVED_ORCIDS = 20
"""How many ORCID iDs one account can keep for publishing and retracting."""


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable using the IADOPT truthiness rule.

    Truthy values are ``{"1", "true", "yes", "on"}`` (case-insensitive).

    Args:
        name: The environment variable name.
        default: Value to return when the variable is unset.

    Returns:
        The parsed boolean.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def utc_iso(value: Optional[datetime] = None) -> str:
    """Return an ISO-8601 UTC timestamp string.

    Args:
        value: The datetime to format; defaults to now when omitted.

    Returns:
        The ISO-8601 string.
    """
    return (value or utc_now()).isoformat()


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into a UTC-aware datetime.

    Naive timestamps are assumed UTC.

    Args:
        value: The ISO-8601 timestamp string.

    Returns:
        The timezone-aware UTC datetime.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AuthStore:
    """SQLite-backed user store, session manager, and audit logger.

    A single instance is shared process-wide (see ``app.core.dependencies``). All
    mutating operations are guarded by an ``RLock`` and run against the configured
    SQLite database through ``app.storage``, whose immediate transactions keep
    check-and-write sections correct across processes.
    """

    def __init__(
        self,
        *,
        db_path: pathlib.Path,
        enabled: bool,
        session_secret: str,
        cookie_secure: bool,
        cookie_name: str = "iadopt_session",
        session_ttl_hours: int = 12,
        audit_retention_days: int = 30,
        audit_max_payload_bytes: int = 1_000_000,
    ) -> None:
        """Configure the store.

        Args:
            db_path: Path to the SQLite database file.
            enabled: Whether authentication is enforced.
            session_secret: HMAC secret for signing session tokens (required when enabled).
            cookie_secure: Whether session cookies set the ``Secure`` flag.
            cookie_name: The session cookie name.
            session_ttl_hours: Session lifetime in hours.
            audit_retention_days: Audit log retention in days (0 = keep forever).
            audit_max_payload_bytes: Max bytes of an audited payload before truncation.
        """
        self.db_path = db_path
        self.enabled = enabled
        self.session_secret = session_secret
        self.cookie_secure = cookie_secure
        self.cookie_name = cookie_name
        self.session_ttl = timedelta(hours=session_ttl_hours)
        self.audit_retention_days = audit_retention_days
        self.audit_max_payload_bytes = audit_max_payload_bytes
        self._lock = threading.RLock()
        self._last_cleanup = 0.0

    def init(self) -> None:
        """Migrate the database, create the bootstrap admin if it holds no account, prune old audit rows.

        Raises:
            RuntimeError: If auth is enabled but no ``session_secret`` is configured.
        """
        if self.enabled and not self.session_secret:
            raise RuntimeError("IADOPT_SESSION_SECRET must be set when IADOPT_AUTH_ENABLED=true.")
        storage.migrate(self.db_path)
        self._seed_bootstrap_admin()
        self.cleanup_old_audit(force=True)

    def _connect(self, *, immediate: bool = False) -> AbstractContextManager[sqlite3.Connection]:
        """Open a transaction on the store's database; immediate for check-and-write sections."""
        return storage.transaction(immediate, path=self.db_path)

    def _seed_bootstrap_admin(self) -> None:
        """Create the configured bootstrap admin on a database without accounts (D-045).

        Reads the ``IADOPT_BOOTSTRAP_ADMIN_*`` env vars. Once any account exists they change
        nothing, so a restart never undoes the owner's block or revocation.

        Raises:
            RuntimeError: If no users exist and no bootstrap credentials are configured.
        """
        if not self.enabled:
            return

        username = (os.getenv("IADOPT_BOOTSTRAP_ADMIN_USERNAME") or "").strip()
        password = os.getenv("IADOPT_BOOTSTRAP_ADMIN_PASSWORD") or ""
        display_name = (os.getenv("IADOPT_BOOTSTRAP_ADMIN_DISPLAY_NAME") or username or "Administrator").strip()
        email = (os.getenv("IADOPT_BOOTSTRAP_ADMIN_EMAIL") or "").strip()
        configured = bool(username and password)

        with self._connect() as conn:
            accounts = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if accounts:
            if configured:
                logger.info("Ignored the bootstrap admin settings: the database already holds accounts.")
            return
        if not configured:
            raise RuntimeError(
                "No users exist. Set IADOPT_BOOTSTRAP_ADMIN_USERNAME and "
                "IADOPT_BOOTSTRAP_ADMIN_PASSWORD for the first startup."
            )

        password_hash = self.hash_password(password)
        now = utc_iso()
        with self._lock, self._connect(immediate=True) as conn:
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                return  # another process created the first account meanwhile
            cursor = conn.execute(
                """
                INSERT INTO users (
                    username, password_hash, display_name, email, roles, is_active,
                    auth_provider, external_subject, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, 'local', ?, ?, ?)
                """,
                (username, password_hash, display_name, email, json.dumps(["admin", "user"]), username, now, now),
            )
            add_history(
                conn, user_id=int(cursor.lastrowid or 0), action="admin_granted", actor_kind="bootstrap", at=now
            )

    def hash_password(self, password: str) -> str:
        """Hash a password with PBKDF2-SHA256 (390k iterations, 16-byte salt).

        Args:
            password: The plaintext password.

        Returns:
            The hash string ``pbkdf2_sha256$<iterations>$<salt>$<digest>`` (base64 parts).
        """
        salt = secrets.token_bytes(16)
        iterations = 390_000
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return "pbkdf2_sha256${}${}${}".format(
            iterations,
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )

    def hash_new_password(self, password: str) -> str:
        """Hash a password someone sets, refusing one that is too short.

        Args:
            password: The plaintext password.

        Raises:
            ValueError: If it has fewer than 8 characters.
        """
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        return self.hash_password(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        """Verify a plaintext password against a stored hash, constant-time.

        Args:
            password: The plaintext password to check.
            password_hash: The stored hash string.

        Returns:
            True if the password matches; False on any mismatch or malformed hash.
        """
        try:
            algorithm, iterations_raw, salt_raw, digest_raw = password_hash.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            iterations = int(iterations_raw)
            salt = base64.b64decode(salt_raw)
            expected = base64.b64decode(digest_raw)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
            return hmac.compare_digest(actual, expected)
        except Exception:
            return False

    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """Validate credentials and return the user record on success.

        Args:
            username: The login name (case-insensitive).
            password: The plaintext password.

        Returns:
            The user dict on success, or ``None`` if the user is missing/inactive
            or the password is wrong. Updates ``last_login_at`` on success.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id, is_active, password_hash FROM users WHERE username = ?", (username.strip(),)
            ).fetchone()
        if not row or not row["is_active"]:
            return None
        # Checked outside the write transaction: hashing a password takes a while.
        if not self.verify_password(password, row["password_hash"]):
            return None
        now = utc_iso()
        with self._lock, self._connect(immediate=True) as conn:
            signed_in = conn.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ? AND is_active = 1 AND password_hash = ?",
                (now, now, row["id"], row["password_hash"]),
            )
            # Nothing updated: the account was blocked or its password changed meanwhile.
            return find_account(conn, row["id"]) if signed_in.rowcount else None

    def create_session(self, user_id: int, request: Request) -> str:
        """Create a session for a user and return the signed cookie value.

        Args:
            user_id: The user to create the session for.
            request: The request (used for IP/user-agent capture).

        Returns:
            The ``<token>.<signature>`` cookie value; only the SHA-256 of the token
            is stored in the database. A blocked account gets no session, so its value
            opens nothing.
        """
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_session_token(token)
        now = utc_now()
        expires_at = now + self.session_ttl

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (token_hash, user_id, created_at, expires_at, ip_address, user_agent)
                SELECT ?, id, ?, ?, ?, ? FROM users WHERE id = ? AND is_active = 1
                """,
                (
                    token_hash,
                    utc_iso(now),
                    utc_iso(expires_at),
                    self.client_ip(request),
                    request.headers.get("user-agent", ""),
                    user_id,
                ),
            )

        return f"{token}.{self._sign_session_token(token)}"

    def set_session_cookie(self, response: Response, cookie_value: str) -> None:
        """Set the session cookie on a response (httponly, samesite=lax).

        Args:
            response: The response to attach the cookie to.
            cookie_value: The signed session cookie value.
        """
        max_age = int(self.session_ttl.total_seconds())
        response.set_cookie(
            self.cookie_name,
            cookie_value,
            max_age=max_age,
            httponly=True,
            secure=self.cookie_secure,
            samesite="lax",
            path="/",
        )

    def clear_session_cookie(self, response: Response) -> None:
        """Delete the session cookie from a response.

        Args:
            response: The response to clear the cookie on.
        """
        response.delete_cookie(self.cookie_name, path="/")

    def delete_session(self, request: Request) -> None:
        """Delete the request's session from the database (no-op if absent).

        Args:
            request: The request carrying the session cookie.
        """
        token = self._session_token_from_request(request)
        if not token:
            return
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash_session_token(token),))

    def user_from_request(self, request: Request) -> Optional[Dict[str, Any]]:
        """Resolve the user from the request's session cookie.

        When auth is disabled, returns a synthetic development admin. Otherwise
        validates the signed cookie against the sessions table and caches the
        result on ``request.state.current_user``.

        Args:
            request: The request carrying the session cookie.

        Returns:
            The user dict, or ``None`` if no valid session is present (and auth
            is enabled). Expired sessions are deleted on access.
        """
        if not self.enabled:
            return {
                "id": 0,
                "username": "development",
                "display_name": "Development User",
                "email": "",
                "roles": ["admin", "user"],
                "role": "admin",
                "is_active": True,
                "auth_provider": "local",
                "external_subject": "development",
            }

        cached = getattr(request.state, "current_user", None)
        if cached:
            return cached

        token = self._session_token_from_request(request)
        if not token:
            return None

        token_hash = self._hash_session_token(token)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT {ACCOUNT_COLUMNS}
                  FROM sessions
                  JOIN users ON users.id = sessions.user_id
                 WHERE sessions.token_hash = ?
                   AND users.is_active = 1
                   AND sessions.expires_at > ?
                """,
                (token_hash, utc_iso()),
            ).fetchone()
        if not row:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM sessions
                     WHERE token_hash = ?
                       AND NOT EXISTS (
                           SELECT 1 FROM users
                            WHERE users.id = sessions.user_id AND users.is_active = 1 AND sessions.expires_at > ?
                       )
                    """,
                    (token_hash, utc_iso()),
                )
            return None

        user = account_from_row(row)
        request.state.current_user = user
        return user

    def require_user(self, request: Request) -> Dict[str, Any]:
        """Return the authenticated user or raise 401.

        Args:
            request: The request carrying the session cookie.

        Returns:
            The authenticated user dict.

        Raises:
            HTTPException: 401 if no valid session is present.
        """
        user = self.user_from_request(request)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
        return user

    def create_user(self, *, username: str, password: str, display_name: str = "", email: str = "") -> Dict[str, Any]:
        """Create an ordinary local user; ``ValueError`` for a short password or a taken username.

        Args:
            username: Login name (1-120 chars, case-insensitive unique).
            password: Plaintext password (>= 8 chars).
            display_name: Optional display name.
            email: Optional email.

        Returns:
            The created user dict.
        """
        username = self.normalize_username(username)
        password_hash = self.hash_new_password(password)
        with self._lock, self._connect() as conn:
            return self.insert_user(
                conn, username=username, password_hash=password_hash, display_name=display_name, email=email
            )

    def insert_user(
        self, conn: sqlite3.Connection, *, username: str, password_hash: str, display_name: str, email: str
    ) -> Dict[str, Any]:
        """Insert an ordinary local user in the caller's transaction.

        Args:
            conn: The transaction's connection.
            username: A normalized login name.
            password_hash: The hash of its password.
            display_name: The display name, empty for none.
            email: The email, empty for none.

        Returns:
            The created user dict.

        Raises:
            ValueError: If the username is taken.
        """
        now = utc_iso()
        try:
            cursor = conn.execute(
                """
                INSERT INTO users (
                    username, password_hash, display_name, email, roles, is_active,
                    auth_provider, external_subject, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, 'local', ?, ?, ?)
                """,
                (
                    username,
                    password_hash,
                    display_name.strip(),
                    email.strip(),
                    json.dumps(["user"]),
                    username,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"User '{username}' already exists.") from e
        return self._account(conn, int(cursor.lastrowid or 0))

    def list_users(self) -> List[Dict[str, Any]]:
        """Return all users, active first then by case-insensitive username."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {ACCOUNT_COLUMNS} FROM users
                 ORDER BY users.is_active DESC, users.username COLLATE NOCASE ASC
                """
            ).fetchall()
        return [account_from_row(row) for row in rows]

    def get_user(self, user_id: int) -> Dict[str, Any]:
        """Return a user by id.

        Args:
            user_id: The user id.

        Returns:
            The user dict; ``ValueError`` if no user has that id.
        """
        with self._lock, self._connect() as conn:
            return self._account(conn, user_id)

    def _account(self, conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
        """Read a user on an open connection.

        Raises:
            ValueError: If no user has that id.
        """
        account = find_account(conn, user_id)
        if account is None:
            raise ValueError("User not found.")
        return account

    def list_orcids(self, user_id: int) -> List[Dict[str, Any]]:
        """Return the ORCID iDs a user saved, most recently used first.

        Args:
            user_id: The account owner.

        Returns:
            Dicts with ``orcid`` (canonical URI), ``name`` (public ORCID name or ``None``),
            ``created_at`` and ``last_used_at``.
        """
        with self._lock, self._connect() as conn:
            return self._list_orcids(conn, user_id)

    def save_orcid(self, user_id: int, orcid: str, name: Optional[str] = None) -> Tuple[List[Dict[str, Any]], bool]:
        """Save an ORCID iD to an account, or mark an already saved one as just used.

        Args:
            user_id: The account owner (a real account; the routes refuse the
                auth-disabled development user).
            orcid: A bare iD or orcid.org URL; it is stored in canonical form.
            name: The public ORCID name, if known; an unknown name never replaces one
                saved earlier.

        Returns:
            The account's saved iDs after the change (see :meth:`list_orcids`), and whether
            the iD was newly saved.

        Raises:
            ValueError: If the iD is malformed or its check digit is wrong, or the account
                already holds ``MAX_SAVED_ORCIDS`` other iDs.
        """
        orcid = canonical_orcid_uri(orcid)
        now = utc_iso()
        with self._lock, self._connect(immediate=True) as conn:
            updated = conn.execute(
                "UPDATE saved_orcids SET last_used_at = ?, name = COALESCE(?, name) WHERE user_id = ? AND orcid = ?",
                (now, name, user_id, orcid),
            )
            created = not updated.rowcount
            if created:
                count = conn.execute("SELECT COUNT(*) FROM saved_orcids WHERE user_id = ?", (user_id,)).fetchone()[0]
                if count >= MAX_SAVED_ORCIDS:
                    raise ValueError(f"You can save up to {MAX_SAVED_ORCIDS} ORCID iDs. Remove one first.")
                conn.execute(
                    "INSERT INTO saved_orcids (user_id, orcid, name, created_at, last_used_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, orcid, name, now, now),
                )
            return self._list_orcids(conn, user_id), created

    def remove_orcid(self, user_id: int, orcid: str) -> List[Dict[str, Any]]:
        """Remove a saved ORCID iD from an account.

        Args:
            user_id: The account owner.
            orcid: A bare iD or orcid.org URL; a malformed one makes
                :func:`canonical_orcid_uri` raise ``ValueError``.

        Returns:
            The account's saved iDs after the change (see :meth:`list_orcids`).

        Raises:
            LookupError: If the account has not saved that iD.
        """
        orcid = canonical_orcid_uri(orcid)
        with self._lock, self._connect() as conn:
            deleted = conn.execute("DELETE FROM saved_orcids WHERE user_id = ? AND orcid = ?", (user_id, orcid))
            if not deleted.rowcount:
                raise LookupError("That ORCID iD is not saved to your account.")
            return self._list_orcids(conn, user_id)

    def _list_orcids(self, conn: sqlite3.Connection, user_id: int) -> List[Dict[str, Any]]:
        """Read a user's saved iDs on an open connection, most recently used first."""
        rows = conn.execute(
            """
            SELECT orcid, name, created_at, last_used_at
              FROM saved_orcids
             WHERE user_id = ?
             ORDER BY last_used_at DESC, orcid ASC
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def audit_event(
        self,
        *,
        action: str,
        user: Optional[Dict[str, Any]] = None,
        request: Optional[Request] = None,
        status_code: Optional[int] = None,
        latency_ms: Optional[int] = None,
        request_payload: Any = None,
        response_payload: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record an audit event, truncating large payloads.

        When ``user`` is omitted but ``request`` is provided, the user is resolved
        from the request. Payloads/metadata are JSON-serialized and truncated to
        ``audit_max_payload_bytes``. Failures are logged, never raised.

        Args:
            action: The action name (e.g. ``"auth.login"``, ``"decompose"``).
            user: The acting user, or ``None`` to resolve from ``request``.
            request: The request (for method/path/IP/user-agent), or ``None``.
            status_code: The HTTP status code, or ``None``.
            latency_ms: Server-side latency in ms, or ``None``.
            request_payload: The request payload (serialized/truncated), or ``None``.
            response_payload: The response payload (serialized/truncated), or ``None``.
            metadata: Free-form metadata (serialized/truncated), or ``None``.
            error: An error message, or ``None``.
        """
        try:
            if request is not None and user is None:
                user = self.user_from_request(request)

            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_events (
                        created_at, user_id, username, action, method, path, status_code,
                        latency_ms, ip_address, user_agent, request_payload, response_payload,
                        metadata, error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_iso(),
                        user.get("id") if user else None,
                        user.get("username", "") if user else "",
                        action,
                        request.method if request else "",
                        request.url.path if request else "",
                        status_code,
                        latency_ms,
                        self.client_ip(request) if request else "",
                        request.headers.get("user-agent", "") if request else "",
                        self._payload_to_text(request_payload),
                        self._payload_to_text(response_payload),
                        self._payload_to_text(metadata or {}),
                        error,
                    ),
                )
            self.cleanup_old_audit()
        except Exception as e:
            print(f"Audit logging failed for {action}: {e}")

    def get_audit_events(self, *, limit: int = 100, offset: int = 0, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return a page of audit events, newest first.

        Args:
            limit: Page size (clamped to 1..500).
            offset: Page offset (>= 0).
            user_id: Optional filter to a single user.

        Returns:
            The list of audit-event dicts.
        """
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        params: List[Any] = []
        where = ""
        if user_id is not None:
            where = "WHERE user_id = ?"
            params.append(user_id)
        params.extend([limit, offset])

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM audit_events
                 {where}
                 ORDER BY created_at DESC, id DESC
                 LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def stats(self) -> Dict[str, Any]:
        """Return aggregate usage/audit statistics over the most recent ~5000 events.

        Returns:
            A dict with user counts, recent action/failure/latency aggregates, model
            usage, and the 25 most-recent events. (The ``readiness`` block is added
            by the admin route, not here.)
        """
        with self._lock, self._connect() as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0]
            recent_rows = conn.execute(
                """
                SELECT * FROM audit_events
                 ORDER BY created_at DESC, id DESC
                 LIMIT 5000
                """
            ).fetchall()

        events = [self._event_from_row(row) for row in recent_rows]
        by_action: Dict[str, int] = {}
        failures = 0
        latencies: List[int] = []
        model_usage: Dict[str, int] = {}
        active_usernames = set()

        for event in events:
            by_action[event["action"]] = by_action.get(event["action"], 0) + 1
            if event.get("username"):
                active_usernames.add(event["username"])
            if event.get("status_code") and event["status_code"] >= 400:
                failures += 1
            if event.get("error"):
                failures += 1
            if isinstance(event.get("latency_ms"), int):
                latencies.append(event["latency_ms"])
            metadata = event.get("metadata_json") or {}
            model_provider = metadata.get("model_provider")
            model_name = metadata.get("model_name")
            if model_provider or model_name:
                key = " / ".join(value for value in [model_provider, model_name] if value)
                model_usage[key] = model_usage.get(key, 0) + 1

        return {
            "auth_enabled": self.enabled,
            "total_users": total_users,
            "active_users": active_users,
            "active_usernames_30d": sorted(active_usernames),
            "event_count_30d": len(events),
            "failures_30d": failures,
            "average_latency_ms_30d": round(sum(latencies) / len(latencies)) if latencies else 0,
            "events_by_action_30d": by_action,
            "model_usage_30d": model_usage,
            "recent_events": events[:25],
        }

    def cleanup_old_audit(self, *, force: bool = False) -> None:
        """Delete audit rows older than the retention window and expired sessions.

        Runs at most hourly unless ``force``; a no-op when retention is <= 0.

        Args:
            force: When true, run immediately regardless of the hourly throttle.
        """
        if self.audit_retention_days <= 0:
            return
        now = time.time()
        if not force and now - self._last_cleanup < 3600:
            return
        cutoff = utc_iso(utc_now() - timedelta(days=self.audit_retention_days))
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM audit_events WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (utc_iso(),))
        self._last_cleanup = now

    def client_ip(self, request: Optional[Request]) -> str:
        """Extract the client IP, preferring the first ``X-Forwarded-For`` value.

        Args:
            request: The request, or ``None``.

        Returns:
            The client IP string, or ``""`` when unavailable.
        """
        if not request:
            return ""
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
        return request.client.host if request.client else ""

    def _session_token_from_request(self, request: Request) -> Optional[str]:
        """Extract and signature-verify the session token from the request cookie.

        Args:
            request: The request carrying the session cookie.

        Returns:
            The bare token if the signature is valid, else ``None``.
        """
        value = request.cookies.get(self.cookie_name)
        if not value or "." not in value:
            return None
        token, signature = value.rsplit(".", 1)
        expected = self._sign_session_token(token)
        if not hmac.compare_digest(signature, expected):
            return None
        return token

    def _sign_session_token(self, token: str) -> str:
        """HMAC-SHA256-sign a token with the session secret."""
        return hmac.new(self.session_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _hash_session_token(self, token: str) -> str:
        """SHA-256-hash a token for database storage (only the hash is persisted)."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def normalize_username(self, username: str) -> str:
        """Strip and validate a username (1-120 chars).

        Args:
            username: The username as given.

        Raises:
            ValueError: If empty or longer than 120 characters.
        """
        normalized = username.strip()
        if not normalized:
            raise ValueError("Username is required.")
        if len(normalized) > 120:
            raise ValueError("Username must be 120 characters or fewer.")
        return normalized

    def _event_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert an ``audit_events`` row into the audit-event dict (metadata parsed)."""
        metadata_json: Dict[str, Any] = {}
        if row["metadata"]:
            try:
                metadata_json = json.loads(row["metadata"])
            except Exception:
                metadata_json = {}
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "user_id": row["user_id"],
            "username": row["username"],
            "action": row["action"],
            "method": row["method"],
            "path": row["path"],
            "status_code": row["status_code"],
            "latency_ms": row["latency_ms"],
            "ip_address": row["ip_address"],
            "user_agent": row["user_agent"],
            "request_payload": row["request_payload"],
            "response_payload": row["response_payload"],
            "metadata": row["metadata"],
            "metadata_json": metadata_json,
            "error": row["error"],
        }

    def _payload_to_text(self, payload: Any) -> Optional[str]:
        """Serialize a payload to text, truncating to ``audit_max_payload_bytes``.

        Args:
            payload: The payload (str passed through; anything else is JSON-serialized).

        Returns:
            The (possibly truncated) text, or ``None`` when the payload is ``None``.
        """
        if payload is None:
            return None
        if isinstance(payload, str):
            text = payload
        else:
            text = json.dumps(payload, ensure_ascii=False, default=str)

        raw = text.encode("utf-8")
        if self.audit_max_payload_bytes > 0 and len(raw) > self.audit_max_payload_bytes:
            truncated = raw[: self.audit_max_payload_bytes].decode("utf-8", errors="ignore")
            return f"{truncated}\n...[truncated at {self.audit_max_payload_bytes} bytes]"
        return text
