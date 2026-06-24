from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException, Request, Response, status


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: Optional[datetime] = None) -> str:
    return (value or utc_now()).isoformat()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AuthStore:
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
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.enabled and not self.session_secret:
            raise RuntimeError("IADOPT_SESSION_SECRET must be set when IADOPT_AUTH_ENABLED=true.")

        with self._connect() as conn:
            self._create_schema(conn)
            self._seed_bootstrap_admin(conn)

        self.cleanup_old_audit(force=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                roles TEXT NOT NULL DEFAULT '["user"]',
                is_active INTEGER NOT NULL DEFAULT 1,
                auth_provider TEXT NOT NULL DEFAULT 'local',
                external_subject TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER,
                username TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                status_code INTEGER,
                latency_ms INTEGER,
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                request_payload TEXT,
                response_payload TEXT,
                metadata TEXT,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_events(user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action);
            """
        )

    def _seed_bootstrap_admin(self, conn: sqlite3.Connection) -> None:
        if not self.enabled:
            return

        existing_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        username = (os.getenv("IADOPT_BOOTSTRAP_ADMIN_USERNAME") or "").strip()
        password = os.getenv("IADOPT_BOOTSTRAP_ADMIN_PASSWORD") or ""
        display_name = (os.getenv("IADOPT_BOOTSTRAP_ADMIN_DISPLAY_NAME") or username or "Administrator").strip()
        email = (os.getenv("IADOPT_BOOTSTRAP_ADMIN_EMAIL") or "").strip()

        if not username or not password:
            if existing_count == 0:
                raise RuntimeError(
                    "No users exist. Set IADOPT_BOOTSTRAP_ADMIN_USERNAME and "
                    "IADOPT_BOOTSTRAP_ADMIN_PASSWORD for the first startup."
                )
            return

        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        now = utc_iso()
        if existing:
            conn.execute(
                """
                UPDATE users
                   SET roles = ?, is_active = 1, display_name = COALESCE(NULLIF(display_name, ''), ?),
                       email = COALESCE(NULLIF(email, ''), ?), updated_at = ?
                 WHERE id = ?
                """,
                (json.dumps(["admin", "user"]), display_name, email, now, existing["id"]),
            )
            return

        conn.execute(
            """
            INSERT INTO users (
                username, password_hash, display_name, email, roles, is_active,
                auth_provider, external_subject, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, 'local', ?, ?, ?)
            """,
            (
                username,
                self.hash_password(password),
                display_name,
                email,
                json.dumps(["admin", "user"]),
                username,
                now,
                now,
            ),
        )

    def hash_password(self, password: str) -> str:
        salt = secrets.token_bytes(16)
        iterations = 390_000
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return "pbkdf2_sha256${}${}${}".format(
            iterations,
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )

    def verify_password(self, password: str, password_hash: str) -> bool:
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
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
            if not row or not row["is_active"]:
                return None
            if not self.verify_password(password, row["password_hash"]):
                return None
            conn.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (utc_iso(), utc_iso(), row["id"]))
            refreshed = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
            return self._user_from_row(refreshed)

    def create_session(self, user_id: int, request: Request) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_session_token(token)
        now = utc_now()
        expires_at = now + self.session_ttl

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (token_hash, user_id, created_at, expires_at, ip_address, user_agent)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    user_id,
                    utc_iso(now),
                    utc_iso(expires_at),
                    self.client_ip(request),
                    request.headers.get("user-agent", ""),
                ),
            )

        return f"{token}.{self._sign_session_token(token)}"

    def set_session_cookie(self, response: Response, cookie_value: str) -> None:
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
        response.delete_cookie(self.cookie_name, path="/")

    def delete_session(self, request: Request) -> None:
        token = self._session_token_from_request(request)
        if not token:
            return
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash_session_token(token),))

    def user_from_request(self, request: Request) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return {
                "id": 0,
                "username": "development",
                "display_name": "Development User",
                "email": "",
                "roles": ["admin", "user"],
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
                """
                SELECT users.*
                  FROM sessions
                  JOIN users ON users.id = sessions.user_id
                 WHERE sessions.token_hash = ?
                   AND users.is_active = 1
                   AND sessions.expires_at > ?
                """,
                (token_hash, utc_iso()),
            ).fetchone()
            if not row:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None

        user = self._user_from_row(row)
        request.state.current_user = user
        return user

    def require_user(self, request: Request) -> Dict[str, Any]:
        user = self.user_from_request(request)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
        return user

    def require_admin(self, request: Request) -> Dict[str, Any]:
        user = self.require_user(request)
        if "admin" not in user.get("roles", []):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
        return user

    def create_user(
        self,
        *,
        username: str,
        password: str,
        display_name: str = "",
        email: str = "",
        roles: Optional[Iterable[str]] = None,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        username = self._normalize_username(username)
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters long.")

        now = utc_iso()
        normalized_roles = self._normalize_roles(roles)
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO users (
                        username, password_hash, display_name, email, roles, is_active,
                        auth_provider, external_subject, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'local', ?, ?, ?)
                    """,
                    (
                        username,
                        self.hash_password(password),
                        display_name.strip(),
                        email.strip(),
                        json.dumps(normalized_roles),
                        1 if is_active else 0,
                        username,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"User '{username}' already exists.") from e
            row = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return self._user_from_row(row)

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM users
                 ORDER BY is_active DESC, username COLLATE NOCASE ASC
                """
            ).fetchall()
        return [self._user_from_row(row) for row in rows]

    def update_user(self, user_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"username", "password", "display_name", "email", "roles", "is_active"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Unsupported user field(s): {', '.join(sorted(unknown))}")

        assignments: List[str] = []
        values: List[Any] = []

        if "username" in updates:
            assignments.append("username = ?")
            values.append(self._normalize_username(str(updates["username"])))
        if "password" in updates and updates["password"]:
            password = str(updates["password"])
            if len(password) < 8:
                raise ValueError("Password must be at least 8 characters long.")
            assignments.append("password_hash = ?")
            values.append(self.hash_password(password))
        if "display_name" in updates:
            assignments.append("display_name = ?")
            values.append(str(updates["display_name"] or "").strip())
        if "email" in updates:
            assignments.append("email = ?")
            values.append(str(updates["email"] or "").strip())
        if "roles" in updates:
            assignments.append("roles = ?")
            values.append(json.dumps(self._normalize_roles(updates["roles"])))
        if "is_active" in updates:
            assignments.append("is_active = ?")
            values.append(1 if bool(updates["is_active"]) else 0)

        if not assignments:
            return self.get_user(user_id)

        assignments.append("updated_at = ?")
        values.append(utc_iso())
        values.append(user_id)

        with self._lock, self._connect() as conn:
            self._assert_not_removing_last_admin(conn, user_id, updates)
            try:
                conn.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", values)
            except sqlite3.IntegrityError as e:
                raise ValueError("A user with that username already exists.") from e
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise ValueError("User not found.")
            if "is_active" in updates and not bool(updates["is_active"]):
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return self._user_from_row(row)

    def get_user(self, user_id: int) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise ValueError("User not found.")
        return self._user_from_row(row)

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
        if not request:
            return ""
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
        return request.client.host if request.client else ""

    def _session_token_from_request(self, request: Request) -> Optional[str]:
        value = request.cookies.get(self.cookie_name)
        if not value or "." not in value:
            return None
        token, signature = value.rsplit(".", 1)
        expected = self._sign_session_token(token)
        if not hmac.compare_digest(signature, expected):
            return None
        return token

    def _sign_session_token(self, token: str) -> str:
        return hmac.new(self.session_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _hash_session_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _normalize_username(self, username: str) -> str:
        normalized = username.strip()
        if not normalized:
            raise ValueError("Username is required.")
        if len(normalized) > 120:
            raise ValueError("Username must be 120 characters or fewer.")
        return normalized

    def _normalize_roles(self, roles: Optional[Iterable[str]]) -> List[str]:
        normalized = sorted({str(role).strip().lower() for role in (roles or ["user"]) if str(role).strip()})
        if "admin" in normalized and "user" not in normalized:
            normalized.append("user")
            normalized = sorted(normalized)
        allowed = {"admin", "user"}
        invalid = set(normalized) - allowed
        if invalid:
            raise ValueError(f"Unsupported role(s): {', '.join(sorted(invalid))}")
        return normalized or ["user"]

    def _assert_not_removing_last_admin(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        updates: Dict[str, Any],
    ) -> None:
        if "roles" not in updates and "is_active" not in updates:
            return

        current = conn.execute("SELECT id, roles, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
        if not current:
            return

        current_roles = json.loads(current["roles"] or "[]")
        next_roles = self._normalize_roles(updates["roles"]) if "roles" in updates else current_roles
        next_active = bool(updates["is_active"]) if "is_active" in updates else bool(current["is_active"])
        if "admin" not in current_roles or ("admin" in next_roles and next_active):
            return

        rows = conn.execute("SELECT id, roles FROM users WHERE is_active = 1 AND id != ?", (user_id,)).fetchall()
        has_other_admin = any("admin" in json.loads(row["roles"] or "[]") for row in rows)
        if not has_other_admin:
            raise ValueError("Cannot disable or remove the role from the last active admin.")

    def _user_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        roles = json.loads(row["roles"] or "[]")
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
        }

    def _event_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
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
