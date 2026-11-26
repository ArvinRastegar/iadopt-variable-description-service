"""Shared helpers of the increment-1 tests (app-storage, access-policy, accounts-roles).

The tests reconfigure the process-wide auth store in place, as ``test_records`` does, and point
``Settings.auth_db_path`` at the same temporary file, so the routes, the stores, startup and the
operator commands all use one database.
"""

from __future__ import annotations

import collections
import io
import os
import pathlib
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from app import main
from app.core import config as core_config
from app.core import dependencies as core_deps

BACKEND = pathlib.Path(__file__).resolve().parents[1]
MiB = 1024 * 1024
BOOTSTRAP = {
    "IADOPT_BOOTSTRAP_ADMIN_USERNAME": "admin",
    "IADOPT_BOOTSTRAP_ADMIN_PASSWORD": "admin-password",
    "IADOPT_BOOTSTRAP_ADMIN_DISPLAY_NAME": "Admin User",
    "IADOPT_BOOTSTRAP_ADMIN_EMAIL": "admin@example.org",
}
ISSUER = "https://keycloak-dev.example.org/auth/realms/rohub"
"""A synthetic identity-provider issuer: nothing here talks to a real provider."""

_Usage = collections.namedtuple("_Usage", "total used free")


class TempDatabaseTestCase(unittest.TestCase):
    """A temporary directory with ``self.db`` in it, and the shared stores pointed at it."""

    bootstrap_env: Optional[Dict[str, str]] = BOOTSTRAP

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = pathlib.Path(temp.name)
        self.db = self.dir / "iadopt.sqlite3"
        self.store = core_deps.auth_store
        self.records = core_deps.record_store
        saved = {attr: getattr(self.store, attr) for attr in ("db_path", "enabled", "session_secret", "cookie_secure")}
        self.addCleanup(lambda: [setattr(self.store, attr, value) for attr, value in saved.items()])
        # patch.dict restores the whole environment afterwards, removed names included.
        env = patch.dict(os.environ, dict(self.bootstrap_env or {}))
        env.start()
        self.addCleanup(env.stop)
        if not self.bootstrap_env:
            for name in BOOTSTRAP:
                os.environ.pop(name, None)
        self.use_database(self.db)
        self.store.enabled = True
        self.store.session_secret = "test-secret"
        self.store.cookie_secure = False
        from app import storage

        migrations = list(storage.MIGRATIONS)
        self.addCleanup(lambda: storage.MIGRATIONS.__setitem__(slice(None), migrations))

    def use_database(self, db_path: pathlib.Path) -> None:
        """Point the auth store (and with it the record store), the settings and the environment
        at ``db_path``, so that a freshly built ``Settings()`` finds it too."""
        self.store.db_path = db_path
        settings_patch = patch.object(core_config.settings, "db_path_raw", str(db_path))
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        env_patch = patch.dict(os.environ, {"IADOPT_DB_PATH": str(db_path)})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def client(self, **kwargs: Any) -> TestClient:
        """A client of the application, without running its startup."""
        return TestClient(main.app, headers={"User-Agent": "Increment1Test/1.0"}, **kwargs)

    def login(self, client: TestClient, username: str, password: str = "password-123") -> Dict[str, Any]:
        """Sign ``client`` in through the login route and return the user it answers."""
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def rows(self, sql: str, *params: Any, db: Optional[pathlib.Path] = None) -> List[Dict[str, Any]]:
        """Read rows of the database in use with a plain connection, outside the code under test."""
        conn = sqlite3.connect(db or self.store.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def execute(self, sql: str, *params: Any, db: Optional[pathlib.Path] = None) -> None:
        """Write to the database in use with a plain connection (a test's own arrangement)."""
        conn = sqlite3.connect(db or self.store.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def fake_request(cookie: Optional[str] = None) -> Request:
    """A request as the routes hand it to the stores, optionally carrying a session cookie."""
    headers = [(b"user-agent", b"Increment1Test/1.0")]
    if cookie:
        headers.append((b"cookie", f"iadopt_session={cookie}".encode("ascii")))
    return Request({
        "type": "http", "method": "GET", "path": "/api/test", "raw_path": b"/api/test", "root_path": "",
        "scheme": "http", "query_string": b"", "headers": headers, "client": ("192.0.2.20", 50000),
        "server": ("testserver", 80), "state": {},
    })


def sign_in_as(client: TestClient, account_id: int) -> None:
    """Give ``client`` a session for any account, also one without a password (an identity-provider
    account), through the auth store's own session mechanism."""
    cookie = core_deps.auth_store.create_session(account_id, fake_request())
    client.cookies.set(core_deps.auth_store.cookie_name, cookie)


def run_cli(*argv: str) -> Tuple[int, str]:
    """Run ``python -m app.cli`` in this process and return its exit code and everything it printed."""
    from app import cli

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        try:
            code = cli.main(list(argv))
        except SystemExit as exit_:
            code = exit_.code if isinstance(exit_.code, int) else (0 if exit_.code is None else 1)
    return code, out.getvalue()


@contextmanager
def free_space(free_bytes: int) -> Iterator[None]:
    """Make every ``shutil.disk_usage`` report ``free_bytes`` free (the agreed space seam)."""
    usage = _Usage(total=10 * 1024 * MiB, used=10 * 1024 * MiB - free_bytes, free=free_bytes)
    with patch.object(shutil, "disk_usage", return_value=usage):
        yield


@contextmanager
def started_app() -> Iterator[TestClient]:
    """Run the application's startup (and shutdown) around a client; asset warm-up is skipped."""
    with patch.object(main, "warmup_assets"):
        with TestClient(main.app, headers={"User-Agent": "Increment1Test/1.0"}) as client:
            yield client
