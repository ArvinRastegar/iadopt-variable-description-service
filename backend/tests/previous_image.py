"""The previous image (application commit ``de1a7af``) as the increment-1 tests need it.

Its SQL comes from ``fixtures/previous_image/de1a7af_statements.json`` (see the README there),
replayed call by call. Its two Python rules that decide a password login, and the rule that makes
an account an admin, are copied verbatim from ``backend/app/auth.py`` at ``de1a7af``
(``AuthStore.verify_password``, ``AuthStore.authenticate``, ``AuthStore.require_admin``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import pathlib
import sqlite3
from typing import Any, Callable, Dict, List, Optional

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "previous_image" / "de1a7af_statements.json"
_DATA: Optional[Dict[str, Any]] = None
READS = ("SELECT", "PRAGMA", "BEGIN")
"""Leading words of the statements that only read (a snapshot's ``BEGIN`` included)."""


def data() -> Dict[str, Any]:
    """The captured fixture, loaded once."""
    global _DATA
    if _DATA is None:
        _DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return _DATA


def sql(name: str) -> str:
    """One named statement of the capture, such as ``login_select_user``."""
    return data()["named"][name]["sql"]


def is_read(event: Dict[str, Any]) -> bool:
    """Whether a captured statement only reads."""
    return event["call"] == "execute" and event["sql"].lstrip().upper().startswith(READS)


def replay(db_path: pathlib.Path, include: Callable[[Dict[str, Any]], bool] = lambda event: True) -> List[str]:
    """Replay the captured calls on ``db_path`` in order and return every failure.

    Connections open, commit and close as they did in the previous image; only the statements
    ``include`` selects run. Each failure is ``"<op>: <statement start>: <error>"``.

    Args:
        db_path: The database file.
        include: Selects the statement events to run.

    Returns:
        The failures, empty when every selected statement succeeded.
    """
    events = data()["events"]
    last = {event["conn"]: index for index, event in enumerate(events)}
    open_: Dict[int, sqlite3.Connection] = {}
    failures: List[str] = []
    try:
        for index, event in enumerate(events):
            number, call = event["conn"], event["call"]
            if call == "connect":
                open_[number] = sqlite3.connect(db_path, timeout=event.get("timeout") or 5)
            conn = open_.get(number)
            if conn is None:
                continue
            try:
                if call in ("execute", "executemany", "executescript"):
                    if include(event):
                        if call == "execute":
                            conn.execute(event["sql"], event["params"])
                        elif call == "executemany":
                            conn.executemany(event["sql"], event["params"])
                        else:
                            conn.executescript(event["sql"])
                elif call == "commit" or (call == "exit" and event["commit"]):
                    conn.commit()
                elif call == "rollback" or (call == "exit" and not event["commit"]):
                    conn.rollback()
                elif call == "close":
                    conn.close()
                    del open_[number]
                    continue
            except sqlite3.Error as e:
                failures.append(f"{event['op']}: {' '.join((event.get('sql') or call).split())[:90]}: {type(e).__name__}: {e}")
                conn.rollback()
            if last[number] == index and number in open_:
                open_.pop(number).close()
    finally:
        for conn in open_.values():
            conn.close()
    return failures


def connect(db_path: pathlib.Path) -> sqlite3.Connection:
    """A connection as the previous image opened them (``AuthStore._connect`` at ``de1a7af``)."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def make_de1a7af_file(db_path: pathlib.Path) -> None:
    """Make the file the previous image leaves behind after the captured run: its schema, filled."""
    failures = replay(db_path)
    if failures:
        raise AssertionError(f"the previous image's own run failed on a fresh file: {failures[:3]}")


# ---------------------------------------------------------------------------------------------
# Verbatim from backend/app/auth.py at de1a7af.
# ---------------------------------------------------------------------------------------------


def verify_password(password: str, password_hash: str) -> bool:
    """``AuthStore.verify_password`` at ``de1a7af``."""
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


def password_login(conn: sqlite3.Connection, username: str, password: str) -> bool:
    """The decision of ``AuthStore.authenticate`` at ``de1a7af``: would this login succeed?"""
    row = conn.execute(sql("login_select_user"), (username.strip(),)).fetchone()
    if not row or not row["is_active"]:
        return False
    if not verify_password(password, row["password_hash"]):
        return False
    return True


def is_admin(conn: sqlite3.Connection, username: str) -> bool:
    """``AuthStore.require_admin`` at ``de1a7af``: ``"admin"`` in the account's roles."""
    row = conn.execute(sql("login_select_user"), (username,)).fetchone()
    return bool(row) and "admin" in json.loads(row["roles"] or "[]")


def start(db_path: pathlib.Path) -> List[str]:
    """The previous image's startup on a file that holds its bootstrap admin (``admin``, id 1).

    Replays the captured second start: the schema statements (all ``IF NOT EXISTS``), the
    bootstrap admin's re-enabling and re-promotion, audit pruning and the record tables' check.
    """
    return replay(db_path, include=lambda event: event["op"] == "startup_again")
