"""Capture the SQL the previous image (application commit ``de1a7af``) runs, as a test fixture.

The increment-1 tests replay these statements on files the new code migrated
(``AC-app-storage-011``, ``AC-accounts-roles-016``): the previous image must keep working on a
migrated file until the owner purges legacy passwords (D-017).

The script runs the stores of ``backend/app`` through a connection that logs every call, in the
order the previous image makes them: startup twice (creating, then re-promoting the bootstrap
admin), accounts, login and sessions, admin changes, audit rows, saved ORCID iDs, every kind of
record step, and the admin export. It refuses to run unless the store sources it imports are
byte-identical to ``de1a7af``, so it can never capture what the new code does.

Run from ``backend/``::

    ../.venv/bin/python tests/fixtures/previous_image/capture.py

It writes ``de1a7af_statements.json`` beside itself. Nothing here reaches a model or a registry.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import platform
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

HERE = pathlib.Path(__file__).resolve().parent
BACKEND = HERE.parents[2]
REPO = BACKEND.parent
OUTPUT = HERE / "de1a7af_statements.json"
COMMIT = "de1a7af"
SOURCES = (
    "backend/app/auth.py",
    "backend/app/records.py",
    "backend/app/record_export.py",
    "backend/app/services/call_log.py",
    "backend/app/schemas/common.py",
)

DRAFT = """@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://example.org/variable/1> a iop:Variable ;
    rdfs:label "dissolved oxygen in water" ;
    iop:hasMatrix <https://www.wikidata.org/entity/Q283> .
<https://www.wikidata.org/entity/Q283> rdfs:label "water" .
"""
EDITED = DRAFT.replace('rdfs:label "water"', 'rdfs:label "sea water"')
WITH_METADATA = EDITED + (
    "<https://example.org/variable/1> <http://purl.org/dc/terms/conformsTo> <https://w3id.org/iadopt/ont/1.1> .\n"
)
CLIENT = {
    "screen": {"width": 1512, "height": 982, "pixel_ratio": 2},
    "device": {"platform": "macOS", "mobile": False, "touch_points": 0},
}
MATRIX = "https://www.wikidata.org/entity/Q283"
ORCID = "https://orcid.org/0000-0002-1825-0097"
STARTED = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)


def git(*args: str) -> str:
    """Run git in the repository and return its output."""
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout.strip()


def check_sources() -> Dict[str, str]:
    """Refuse to capture unless every imported store source is the ``de1a7af`` blob."""
    digests = {}
    for source in SOURCES:
        expected = git("rev-parse", f"{COMMIT}:{source}")
        actual = git("hash-object", source)
        if expected != actual:
            raise SystemExit(f"{source} differs from {COMMIT}; the previous image can no longer be captured from this tree.")
        digests[source] = hashlib.sha256((REPO / source).read_bytes()).hexdigest()
    return digests


class Tracer:
    """Hands out logging connections and keeps the log."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self.op = "setup"
        self.count = 0
        self.real_connect = sqlite3.connect

    def log(self, number: int, call: str, **fields: Any) -> Dict[str, Any]:
        event = {"op": self.op, "conn": number, "call": call, **fields}
        self.events.append(event)
        return event

    def connect(self, *args: Any, **kwargs: Any) -> "TracedConnection":
        conn = self.real_connect(*args, **kwargs)
        self.count += 1
        self.log(self.count, "connect", timeout=kwargs.get("timeout"))
        return TracedConnection(self, conn, self.count)


class TracedConnection:
    """A ``sqlite3.Connection`` that logs every statement, commit, rollback and close."""

    def __init__(self, tracer: Tracer, conn: sqlite3.Connection, number: int) -> None:
        self.__dict__.update(_tracer=tracer, _conn=conn, _number=number)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._conn, name, value)

    def _run(self, call: str, sql: Optional[str], params: Any, action: Any) -> Any:
        event = self._tracer.log(self._number, call, sql=sql, params=params)
        try:
            return action()
        except Exception as e:  # recorded, then raised: the capture must not contain failures
            event["error"] = f"{type(e).__name__}: {e}"
            raise

    def execute(self, sql: str, params: Any = ()) -> Any:
        values = list(params)
        return self._run("execute", sql, values, lambda: self._conn.execute(sql, values))

    def executemany(self, sql: str, rows: Any) -> Any:
        values = [list(row) for row in rows]
        return self._run("executemany", sql, values, lambda: self._conn.executemany(sql, values))

    def executescript(self, script: str) -> Any:
        return self._run("executescript", script, None, lambda: self._conn.executescript(script))

    def commit(self) -> None:
        self._run("commit", None, None, self._conn.commit)

    def rollback(self) -> None:
        self._run("rollback", None, None, self._conn.rollback)

    def close(self) -> None:
        self._run("close", None, None, self._conn.close)

    def __enter__(self) -> "TracedConnection":
        self._tracer.log(self._number, "enter")
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        self._tracer.log(self._number, "exit", commit=exc_type is None)
        return self._conn.__exit__(exc_type, exc, tb)


def make_request(cookie: Optional[str] = None) -> Any:
    """A request as the routes pass it to the stores: browser, address and, optionally, a session."""
    from starlette.requests import Request

    headers = [(b"user-agent", b"PreviousImageCapture/1.0"), (b"x-forwarded-for", b"192.0.2.10")]
    if cookie:
        headers.append((b"cookie", f"iadopt_session={cookie}".encode("ascii")))
    return Request({
        "type": "http", "method": "POST", "path": "/api/capture", "raw_path": b"/api/capture", "root_path": "",
        "scheme": "http", "query_string": b"", "headers": headers, "client": ("192.0.2.10", 50000),
        "server": ("testserver", 80), "state": {},
    })


def run_previous_image(tracer: Tracer, db_path: pathlib.Path) -> None:
    """Drive every store operation the previous image performs, labelling each with its op."""
    from app.auth import AuthStore
    from app.record_export import write_export
    from app.records import RecordStore, parse_turtle
    from app.services.call_log import ModelCall

    os.environ.update({
        "IADOPT_BOOTSTRAP_ADMIN_USERNAME": "admin",
        "IADOPT_BOOTSTRAP_ADMIN_PASSWORD": "admin-password",
        "IADOPT_BOOTSTRAP_ADMIN_DISPLAY_NAME": "Admin User",
        "IADOPT_BOOTSTRAP_ADMIN_EMAIL": "admin@example.org",
    })
    auth = AuthStore(db_path=db_path, enabled=True, session_secret="capture-secret", cookie_secure=False)
    records = RecordStore(auth)

    def op(name: str) -> None:
        tracer.op = name

    op("startup")  # first start: tables, the bootstrap admin, audit pruning, record tables
    auth.init()
    records.init()
    op("startup_again")  # every later start re-enables and re-promotes the bootstrap admin
    auth.init()
    records.init()

    op("account_create")
    bob = auth.create_user(username="bob", password="password-123", display_name="Bob Example", email="bob@example.org")
    carol = auth.create_user(username="carol", password="password-123")
    op("login")
    auth.authenticate("bob", "password-123")
    op("session_create")
    cookie = auth.create_session(bob["id"], make_request())
    auth.create_session(bob["id"], make_request())  # a second session, still open when the run ends
    op("session_lookup")
    auth.user_from_request(make_request(cookie))
    op("account_read")
    auth.list_users()
    auth.get_user(bob["id"])
    op("account_update")
    auth.update_user(carol["id"], {"display_name": "Carol Example", "email": "carol@example.org", "password": "password-456"})
    auth.update_user(carol["id"], {"roles": ["admin", "user"]})
    auth.update_user(carol["id"], {"roles": ["user"], "is_active": False})
    auth.update_user(carol["id"], {"is_active": True})

    op("audit_write")
    auth.audit_event(action="auth.login", user=bob, request=make_request(), status_code=200, latency_ms=12,
                     request_payload={"username": "bob"}, response_payload={"user": {"id": bob["id"]}},
                     metadata={"model_provider": "psnc", "model_name": "Qwen3.8-27B"})
    op("audit_read")
    auth.get_audit_events(limit=50)
    auth.get_audit_events(limit=50, user_id=bob["id"])
    auth.stats()
    op("audit_prune")
    auth.cleanup_old_audit(force=True)

    op("orcid_save")
    auth.save_orcid(bob["id"], ORCID, "Josiah Carberry")
    auth.save_orcid(bob["id"], "https://orcid.org/0000-0001-5109-3700", None)
    auth.save_orcid(bob["id"], ORCID, None)  # an iD saved before is marked as used
    op("orcid_read")
    auth.list_orcids(bob["id"])
    op("orcid_remove")
    auth.remove_orcid(bob["id"], "https://orcid.org/0000-0001-5109-3700")

    op("record_decomposition")
    calls = [
        ModelCall("decomposition", 1, "openrouter", "qwen/qwen3-32b", "2026-11-18T10:00:00+00:00", 1538, 2805, 126, "ok", None),
        ModelCall("linking", 1, "openrouter", "qwen/qwen3-32b", "2026-11-18T10:00:02+00:00", 640, 669, 1, "unusable",
                  "The text is not one of the offered labels."),
    ]
    result = {"raw_llm_output": "{}", "parsed_json": {"label": "dissolved oxygen in water"}, "schema_valid": True,
              "validation_errors": [], "enriched_json": {}, "ttl": DRAFT}
    records.record_decomposition(
        user=bob, request=make_request(), definition="dissolved oxygen in water", provider="openrouter",
        model="qwen/qwen3-32b", disable_thinking=True, prompt_setup="measured", prompt_sha256="abc123",
        started_at=STARTED, ended_at=STARTED + timedelta(seconds=3), result=result, error=None, client=CLIENT, calls=calls,
    )
    records.record_decomposition(
        user=bob, request=make_request(), definition="air temperature", provider="psnc", model="Qwen3.8-27B",
        disable_thinking=True, prompt_setup="measured", prompt_sha256=None, started_at=STARTED,
        ended_at=STARTED + timedelta(seconds=1), result=None, error="HTTP 401",
    )
    op("record_import")
    record_id, _ = records.start_import(user=bob, request=make_request(), turtle=DRAFT, occurred_at=STARTED, client=CLIENT)
    op("record_steps")
    records.append_event(user=bob, request=make_request(), record_id=record_id, type_="edit", version=0, occurred_at=STARTED,
                         turtle=EDITED, client=CLIENT, changes=[
                             {"operation": "relabel", "role": "hasMatrix", "component": MATRIX, "before": "water", "after": "sea water"},
                         ])
    records.append_event(user=bob, request=make_request(), record_id=record_id, type_="check", version=1, occurred_at=STARTED,
                         client=CLIENT, changes=[{"operation": "confirm", "role": "hasMatrix", "component": MATRIX}])
    records.append_event(user=bob, request=make_request(), record_id=record_id, type_="metadata", version=1, occurred_at=STARTED,
                         turtle=WITH_METADATA, client=CLIENT)
    records.append_event(user=bob, request=make_request(), record_id=record_id, type_="mark_best", version=2,
                         occurred_at=STARTED, note="the matrix is sea water", unchecked_components=0, client=CLIENT)
    op("record_read")
    records.summary(user=bob, record_id=record_id)
    op("record_publish")
    target = records.publish_target(user=bob, record_id=record_id, graph=parse_turtle(WITH_METADATA))
    records.record_publish(user=bob, request=make_request(), target=target, turtle=WITH_METADATA,
                           nanopub_uri="https://w3id.org/np/RAexampleCapturedNanopub", orcid=ORCID, client=CLIENT)

    op("record_export")
    with records.snapshot() as conn:
        write_export(conn, io.BytesIO(), exported_at=STARTED)

    op("session_end")
    auth.delete_session(make_request(cookie))


def named_statements(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """The statements the tests run with their own parameters, found by op and leading text."""
    wanted = {
        "auth_schema": ("startup", "executescript", "CREATE TABLE IF NOT EXISTS users"),
        "record_schema": ("startup", "executescript", "\nCREATE TABLE IF NOT EXISTS record_schema"),
        "bootstrap_count": ("startup", "execute", "SELECT COUNT(*) FROM users"),
        "bootstrap_find": ("startup", "execute", "SELECT id FROM users WHERE username = ?"),
        "bootstrap_insert": ("startup", "execute", "INSERT INTO users"),
        "bootstrap_update": ("startup_again", "execute", "UPDATE users"),
        "login_select_user": ("login", "execute", "SELECT * FROM users WHERE username = ?"),
        "login_update": ("login", "execute", "UPDATE users SET last_login_at"),
        "session_insert": ("session_create", "execute", "INSERT INTO sessions"),
        "session_lookup": ("session_lookup", "execute", "SELECT users.*"),
    }
    found: Dict[str, Dict[str, str]] = {}
    for name, (op, call, start) in wanted.items():
        matches = {
            event["sql"] for event in events
            if event["op"] == op and event["call"] == call and event["sql"].strip().startswith(start.strip())
        }
        if len(matches) != 1:
            raise SystemExit(f"{name}: expected one statement, found {len(matches)}")
        found[name] = {"op": op, "sql": matches.pop()}
    return found


def main() -> None:
    """Capture and write the fixture."""
    digests = check_sources()
    sys.path.insert(0, str(BACKEND))
    tracer = Tracer()
    with tempfile.TemporaryDirectory() as directory:
        sqlite3.connect = tracer.connect  # type: ignore[assignment]
        try:
            run_previous_image(tracer, pathlib.Path(directory) / "iadopt.sqlite3")
        finally:
            sqlite3.connect = tracer.real_connect
    failures = [event for event in tracer.events if "error" in event]
    if failures:
        raise SystemExit(f"The capture hit {len(failures)} failing statements: {failures[:3]}")
    fixture = {
        "commit": COMMIT,
        "captured_from": git("rev-parse", "--short", "HEAD"),
        "captured_on": datetime.now(timezone.utc).date().isoformat(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "sources_sha256": digests,
        "named": named_statements(tracer.events),
        "events": tracer.events,
    }
    OUTPUT.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(tracer.events)} events to {OUTPUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
