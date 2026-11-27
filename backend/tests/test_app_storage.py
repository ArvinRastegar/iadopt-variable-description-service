"""app-storage: the application's one SQLite database (increment 1, tests gate G-018).

Behavioural tests of ``components/app-storage.md`` (approved under G-017) through its public
seams: ``app.storage``, application startup, the stores' routes and ``python -m app.cli db``.
Every test runs on temporary files. Files "made by the previous image" are built by replaying the
SQL captured from ``de1a7af`` (``tests/fixtures/previous_image``). Free space is simulated through
``shutil.disk_usage``; nothing reaches a model or a registry.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import previous_image
from increment1_support import BACKEND, MiB, TempDatabaseTestCase, free_space, run_cli, started_app
from test_records import CLIENT, DRAFT, EDITED, MATRIX, PIPELINE_RESULT

from app import storage
from app.routers import decompose as decompose_router
from app.services.call_log import start_call

AUTH_TABLES = ("users", "sessions", "audit_events", "saved_orcids")
RECORD_TABLES = (
    "record_items", "record_runs", "record_llm_calls", "record_contents",
    "record_versions", "client_contexts", "record_events", "record_changes",
)
BASELINE_TABLES = AUTH_TABLES + ("record_schema",) + RECORD_TABLES
DEDUPLICATED = ("record_contents", "client_contexts")
BASELINE_INDEXES = (
    "idx_sessions_user_id", "idx_audit_created_at", "idx_audit_user_id", "idx_audit_action",
    "idx_record_items_user", "idx_record_versions_content", "idx_record_events_type",
    "idx_record_events_nanopub", "idx_record_llm_calls_item",
)
APPEND_ONLY_TRIGGERS = tuple(f"{table}_no_{kind}" for table in RECORD_TABLES for kind in ("update", "delete", "overwrite"))
BACKUP_NAME = re.compile(r"^iadopt-(\d{8}T\d{6}Z)-(pre-migration|manual)\.sqlite3$")
STORAGE_DIR = BACKEND / "app" / "storage"


# ------------------------------------------------------------------------------------------------
# Helpers: what is in a file, made without the code under test
# ------------------------------------------------------------------------------------------------


def normalized(sql: str) -> str:
    """SQL text without layout differences: one space between words, none around brackets and commas."""
    text = " ".join(sql.split())
    return re.sub(r"\s*([(),])\s*", r"\1", text).lower()


def master(db: pathlib.Path) -> List[tuple]:
    """Every schema object of a file: type, name, table and SQL."""
    conn = sqlite3.connect(db)
    try:
        return sorted(tuple(row) for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
    finally:
        conn.close()


def table_names(db: pathlib.Path) -> List[str]:
    """The file's own tables (SQLite's internal ones left out)."""
    return sorted(name for kind, name, _, _ in master(db) if kind == "table" and not name.startswith("sqlite_"))


def columns(db: pathlib.Path, table: str) -> List[tuple]:
    """``PRAGMA table_info`` without the column numbers: name, type, not null, default, primary key."""
    conn = sqlite3.connect(db)
    try:
        return [tuple(row[1:]) for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def dump(db: pathlib.Path, only: Dict[str, List[str]] | None = None) -> Dict[str, List[tuple]]:
    """Every row of every table, with its rowid, in rowid order; ``only`` limits tables and columns."""
    conn = sqlite3.connect(db)
    try:
        names = list(only) if only is not None else table_names(db)
        result = {}
        for name in names:
            wanted = ", ".join(f'"{column}"' for column in only[name]) if only else "*"
            result[name] = [tuple(row) for row in conn.execute(f"SELECT rowid, {wanted} FROM {name} ORDER BY rowid")]
        return result
    finally:
        conn.close()


def digest(path: pathlib.Path) -> str:
    """SHA-256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backups(db: pathlib.Path) -> List[str]:
    """Names of the files in the database's backup directory."""
    folder = db.parent / "backups"
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


def backup_named(db: pathlib.Path) -> List[str]:
    """The files in the backup directory that carry a backup's name."""
    return [name for name in backups(db) if BACKUP_NAME.match(name)]


def reference_file(folder: pathlib.Path) -> pathlib.Path:
    """The ``de1a7af`` schema, empty, as ``AuthStore`` and ``RecordStore`` create it."""
    path = folder / "reference.sqlite3"
    if not path.exists():
        conn = previous_image.connect(path)
        try:
            conn.executescript(previous_image.sql("auth_schema"))
            conn.executescript(previous_image.sql("record_schema"))
            conn.execute("INSERT INTO record_schema (version) VALUES (1)")
            conn.commit()
        finally:
            conn.close()
    return path


def baseline_columns(folder: pathlib.Path) -> Dict[str, List[str]]:
    """Each baseline table's columns as ``de1a7af`` creates them."""
    reference = reference_file(folder)
    return {table: [column[0] for column in columns(reference, table)] for table in BASELINE_TABLES}


def logical_bytes(conn: sqlite3.Connection, table: str) -> int:
    """Section 6's logical size: the sum over rows and columns of ``length(CAST(value AS BLOB))``."""
    names = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    expression = " + ".join(f'COALESCE(length(CAST("{name}" AS BLOB)), 0)' for name in names)
    return int(conn.execute(f"SELECT COALESCE(SUM({expression}), 0) FROM {table}").fetchone()[0])


def own_tables(report: Dict[str, Any]) -> Dict[str, Any]:
    """A status report's tables without SQLite's internal ones, which section 6 does not ask for."""
    return {name: figures for name, figures in report["tables"].items() if not name.startswith("sqlite_")}


def migration(migration_id: str, sql: str, kind: str = "sql") -> storage.Migration:
    """A migration the test adds after the code's own (the agreed seam: append to MIGRATIONS)."""
    return storage.Migration(id=migration_id, kind=kind, sql=sql)


def pad(db: pathlib.Path, megabytes: int) -> None:
    """Grow a file with audit rows (a baseline table) so that space figures are clearly apart."""
    conn = previous_image.connect(db)
    try:
        payload = "x" * 4096
        conn.executemany(
            "INSERT INTO audit_events (created_at, action, response_payload) VALUES (?, ?, ?)",
            [("2026-11-23T10:00:00+00:00", "padding", payload)] * (megabytes * 256),
        )
        conn.commit()
    finally:
        conn.close()


def size_of(db: pathlib.Path) -> int:
    """The database's size as section 5 counts it: the file plus its WAL."""
    wal = pathlib.Path(f"{db}-wal")
    return db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)


def sqlite_connect_calls(path: pathlib.Path) -> List[int]:
    """Lines of a module that call ``sqlite3.connect``, however it was imported."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules, functions = {"sqlite3"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.asname or alias.name for alias in node.names if alias.name == "sqlite3"}
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            functions |= {alias.asname or alias.name for alias in node.names if alias.name == "connect"}
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "connect" and isinstance(func.value, ast.Name) and func.value.id in modules:
                lines.append(node.lineno)
            elif isinstance(func, ast.Name) and func.id in functions:
                lines.append(node.lineno)
    return lines


def start_together(script: str, *args: str) -> subprocess.Popen:
    """Start a second process that imports what it needs, says ``ready`` and waits for ``go``."""
    process = subprocess.Popen(
        [sys.executable, "-c", script, *args], cwd=BACKEND,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    ready = process.stdout.readline().strip()
    if ready != "ready":
        process.kill()
        raise AssertionError(f"the second process did not start: {ready!r} {process.communicate()[1]}")
    return process


def go_and_wait(process: subprocess.Popen, then: Any = None) -> str:
    """Let the second process run, run ``then`` in this one meanwhile, and return its stderr."""
    assert process.stdin is not None
    process.stdin.write("go\n")
    process.stdin.flush()
    if then is not None:
        then()
    _, stderr = process.communicate(timeout=120)
    return stderr


class CorruptCopy(sqlite3.Connection):
    """A connection whose backups lose their audit rows: a copy that must fail verification."""

    def backup(self, target, *args, **kwargs):  # type: ignore[override]
        super().backup(target, *args, **kwargs)
        target.execute("DELETE FROM audit_events")
        target.commit()


class FailingCopy(sqlite3.Connection):
    """A connection whose backups fail while copying."""

    def backup(self, target, *args, **kwargs):  # type: ignore[override]
        raise sqlite3.OperationalError("disk I/O error")


@contextmanager
def connections_made_by(factory: type) -> Iterator[None]:
    """Open every connection with ``factory``; the storage package opens them through ``sqlite3.connect``."""
    real = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs.setdefault("factory", factory)
        return real(*args, **kwargs)

    with patch.object(sqlite3, "connect", connect):
        yield


class StorageTestCase(TempDatabaseTestCase):
    def de1a7af_file(self) -> pathlib.Path:
        """``self.db`` as the previous image leaves it: its schema and the captured run's rows."""
        previous_image.make_de1a7af_file(self.db)
        return self.db

    def migrations_by_id(self) -> Dict[str, storage.Migration]:
        return {item.id: item for item in storage.MIGRATIONS}

    def applied(self, db: pathlib.Path | None = None) -> Dict[str, str]:
        return {row["id"]: row["checksum"] for row in self.rows("SELECT id, checksum FROM schema_migrations", db=db)}

    def assert_baseline_schema(self, db: pathlib.Path) -> None:
        """Every table, index and trigger of section 2 exactly as ``de1a7af`` creates them."""
        reference = {(kind, name): sql for kind, name, _, sql in master(reference_file(self.dir))}
        got = {(kind, name): sql for kind, name, _, sql in master(db)}
        for table in BASELINE_TABLES:
            with self.subTest(table=table):
                self.assertIn(("table", table), got)
                if table == "users":
                    # accounts-roles adds users.email_verified, which rewrites the stored SQL.
                    reference_columns = columns(reference_file(self.dir), "users")
                    self.assertEqual(columns(db, "users")[: len(reference_columns)], reference_columns)
                    self.assertIn("AUTOINCREMENT", got[("table", "users")].upper())
                elif table == "record_runs":
                    # record-choices (0007) adds record_runs.choices last, which rewrites the stored SQL.
                    reference_runs = normalized(reference[("table", table)])
                    self.assertIn(normalized(got[("table", table)]),
                                  (reference_runs, reference_runs[:-1] + ",choices text)"))
                else:
                    self.assertEqual(normalized(got[("table", table)]), normalized(reference[("table", table)]))
        for kind, names in (("index", BASELINE_INDEXES), ("trigger", APPEND_ONLY_TRIGGERS)):
            for name in names:
                with self.subTest(kind=kind, name=name):
                    self.assertIn((kind, name), got)
                    self.assertEqual(normalized(got[(kind, name)]), normalized(reference[(kind, name)]))
        self.assertEqual(self.rows("SELECT version FROM record_schema", db=db), [{"version": 1}])


# ------------------------------------------------------------------------------------------------
# Section 3: connections and transactions
# ------------------------------------------------------------------------------------------------


class ConnectionPolicyTests(StorageTestCase):
    def assert_policy(self, conn: sqlite3.Connection) -> None:
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30_000)
        self.assertEqual(conn.execute("SELECT 7 AS seven").fetchone()["seven"], 7)

    def test_every_connection_has_the_connection_policy(self):
        """Covers: AC-app-storage-001 (foreign keys, WAL, 30 s busy timeout, rows by column name)."""
        conn = storage.connect(self.db)
        try:
            self.assert_policy(conn)
        finally:
            conn.close()
        for name, opened in (
            ("deferred", lambda: storage.transaction(path=self.db)),
            ("immediate", lambda: storage.transaction(immediate=True, path=self.db)),
            ("snapshot", lambda: storage.snapshot(path=self.db)),
        ):
            with self.subTest(connection=name):
                with opened() as conn:
                    self.assert_policy(conn)

    def test_no_module_outside_the_storage_package_calls_sqlite3_connect(self):
        """Covers: AC-app-storage-001 (in the source)."""
        offenders = []
        for path in sorted((BACKEND / "app").rglob("*.py")):
            if STORAGE_DIR in path.parents:
                continue
            offenders += [f"{path.relative_to(BACKEND)}:{line}" for line in sqlite_connect_calls(path)]
        self.assertEqual(offenders, [])

    def test_a_full_flow_opens_every_connection_through_the_storage_package(self):
        """Covers: AC-app-storage-001 (at run time: login, saved iDs, records, export, admin reads)."""
        storage.migrate(self.db)
        self.store.init()
        self.store.create_user(username="bob", password="password-123")
        callers: List[str] = []
        real = sqlite3.connect

        def tracking(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            callers.append(sys._getframe(1).f_code.co_filename)
            return real(*args, **kwargs)

        with patch.object(sqlite3, "connect", tracking):
            client = self.client()
            self.login(client, "bob")
            self.assertEqual(client.post("/api/auth/me/orcids", json={"orcid": "0000-0002-1825-0097"}).status_code, 200)
            started = client.post("/api/records", json={"turtle": DRAFT, "client": CLIENT})
            self.assertEqual(started.status_code, 201, started.text)
            record_id = started.json()["record_id"]
            self.assertEqual(client.get(f"/api/records/{record_id}").status_code, 200)
            admin = self.client()
            self.login(admin, "admin", "admin-password")
            for path in ("/api/admin/users", "/api/admin/stats", "/api/admin/audit", "/api/admin/records/export"):
                self.assertEqual(admin.get(path).status_code, 200, path)
        self.assertTrue(callers, "the flow opened no connection")
        outside = sorted({caller for caller in callers if STORAGE_DIR not in pathlib.Path(caller).resolve().parents})
        self.assertEqual(outside, [])


class TransactionTests(StorageTestCase):
    def setUp(self):
        super().setUp()
        conn = previous_image.connect(self.db)
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("CREATE TABLE counter (n INTEGER NOT NULL)")
        conn.execute("INSERT INTO counter (n) VALUES (0)")
        conn.commit()
        conn.close()

    def count(self) -> int:
        return self.rows("SELECT COUNT(*) AS n FROM t")[0]["n"]

    def test_a_transaction_is_all_or_nothing_and_always_closes(self):
        """Covers: AC-app-storage-002."""
        for immediate in (False, True):
            with self.subTest(immediate=immediate):
                with self.assertRaisesRegex(RuntimeError, "stop here"):
                    with storage.transaction(immediate=immediate, path=self.db) as conn:
                        conn.execute("INSERT INTO t (x) VALUES (1)")
                        conn.execute("INSERT INTO t (x) VALUES (2)")
                        raise RuntimeError("stop here")
                self.assertEqual(self.count(), 0)
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")  # closed after the block

        with storage.transaction(path=self.db) as conn:
            conn.execute("INSERT INTO t (x) VALUES (1)")
            conn.execute("INSERT INTO t (x) VALUES (2)")
        self.assertEqual(self.count(), 2)
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_immediate_read_and_write_sections_lose_no_update(self):
        """Covers: AC-app-storage-003 (real threads and a second process on one real file)."""
        threads, per_thread, per_process = 6, 20, 50
        script = (
            "import pathlib, sys, time\n"
            "from app import storage\n"
            "db = pathlib.Path(sys.argv[1])\n"
            "print('ready', flush=True)\n"
            "sys.stdin.readline()\n"
            "for _ in range(int(sys.argv[2])):\n"
            "    with storage.transaction(immediate=True, path=db) as conn:\n"
            "        n = conn.execute('SELECT n FROM counter').fetchone()[0]\n"
            "        time.sleep(0.002)\n"
            "        conn.execute('UPDATE counter SET n = ?', (n + 1,))\n"
        )
        process = start_together(script, str(self.db), str(per_process))
        errors: List[BaseException] = []
        barrier = threading.Barrier(threads + 1)

        def increment() -> None:
            try:
                barrier.wait()
                for _ in range(per_thread):
                    with storage.transaction(immediate=True, path=self.db) as conn:
                        n = conn.execute("SELECT n FROM counter").fetchone()[0]
                        time.sleep(0.002)
                        conn.execute("UPDATE counter SET n = ?", (n + 1,))
            except BaseException as e:  # reported below
                errors.append(e)

        workers = [threading.Thread(target=increment) for _ in range(threads)]
        for worker in workers:
            worker.start()
        barrier.wait()  # the threads and the second process start at the same moment
        stderr = go_and_wait(process)
        for worker in workers:
            worker.join(timeout=120)
        self.assertEqual(errors, [])
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(self.rows("SELECT n FROM counter")[0]["n"], threads * per_thread + per_process)

    def test_the_record_store_numbers_steps_from_two_processes_without_a_clash(self):
        """Covers: AC-app-storage-003 (section 3's check-and-write rule, D-014: the next step number is
        read and written in one immediate transaction, so correctness does not rest on in-process locks)."""
        self.store.init()
        bob = self.store.create_user(username="bob", password="password-123")
        record_id, _ = self.records.start_import(user=bob, request=None, turtle=DRAFT, occurred_at=None, client=CLIENT)
        per_side = 40
        check = {"operation": "confirm", "role": "hasMatrix", "component": MATRIX}
        script = (
            "import pathlib, sys\n"
            "from app.auth import AuthStore\n"
            "from app.records import RecordStore\n"
            "auth = AuthStore(db_path=pathlib.Path(sys.argv[1]), enabled=True, session_secret='x', cookie_secure=False)\n"
            "records = RecordStore(auth)\n"
            "user = {'id': int(sys.argv[2]), 'roles': ['user']}\n"
            "print('ready', flush=True)\n"
            "sys.stdin.readline()\n"
            "for _ in range(int(sys.argv[4])):\n"
            f"    records.append_event(user=user, request=None, record_id=sys.argv[3], type_='check', version=0,\n"
            f"                         occurred_at=None, changes=[{check!r}])\n"
        )
        process = start_together(script, str(self.db), str(bob["id"]), record_id, str(per_side))
        errors: List[BaseException] = []
        stderr = go_and_wait(process, then=lambda: self.append_checks(bob, record_id, per_side, check, errors))
        self.assertEqual(errors, [])
        self.assertEqual(process.returncode, 0, stderr)
        steps = [row["seq"] for row in self.rows(
            "SELECT e.seq FROM record_events e JOIN record_items i ON i.id = e.item_id WHERE i.public_id = ? ORDER BY e.seq",
            record_id)]
        self.assertEqual(steps, list(range(1, 2 + 2 * per_side)))

    def append_checks(self, user: Dict[str, Any], record_id: str, count: int, check: Dict[str, str], errors: List[BaseException]) -> None:
        try:
            for _ in range(count):
                self.records.append_event(user=user, request=None, record_id=record_id, type_="check", version=0,
                                          occurred_at=None, changes=[check])
        except BaseException as e:  # reported by the test
            errors.append(e)

    def test_a_transaction_without_the_lock_in_time_raises_storage_busy(self):
        """Covers: AC-app-storage-019 (timeout shortened to 0.2 s), and section 3's Busy rule."""
        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            for immediate in (True, False):
                with self.subTest(immediate=immediate):
                    started = time.monotonic()
                    with self.assertRaises(storage.StorageBusy) as caught:
                        with storage.transaction(immediate=immediate, path=self.db, busy_timeout=0.2) as conn:
                            conn.execute("INSERT INTO t (x) VALUES (1)")
                    self.assertIsInstance(caught.exception, sqlite3.OperationalError)
                    self.assertLess(time.monotonic() - started, 10, "the shortened timeout was not used")
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(self.count(), 0)

    def test_storage_busy_is_an_operational_error_and_the_others_are_storage_errors(self):
        """Covers: AC-app-storage-019 (section 9: which errors subclass which)."""
        self.assertTrue(issubclass(storage.StorageBusy, sqlite3.OperationalError))
        for error in (storage.SchemaMismatch, storage.MigrationChanged, storage.MigrationFailed,
                      storage.BackupFailed, storage.InsufficientSpace):
            with self.subTest(error=error.__name__):
                self.assertTrue(issubclass(error, storage.StorageError))


class SnapshotTests(StorageTestCase):
    def test_a_snapshot_reads_one_moment_and_cannot_write(self):
        """Covers: AC-app-storage-004."""
        conn = previous_image.connect(self.db)
        conn.execute("CREATE TABLE a (x INTEGER)")
        conn.execute("CREATE TABLE b (x INTEGER)")
        conn.execute("INSERT INTO a VALUES (1)")
        conn.execute("INSERT INTO b VALUES (1)")
        conn.commit()
        conn.close()

        with storage.snapshot(path=self.db) as snap:
            self.assertEqual(snap.execute("SELECT COUNT(*) FROM a").fetchone()[0], 1)
            # Another connection commits to both tables between the snapshot's queries.
            with storage.transaction(path=self.db) as writer:
                writer.execute("INSERT INTO a VALUES (2)")
                writer.execute("INSERT INTO b VALUES (2)")
            # Table b, read for the first time after that commit, still shows the snapshot's moment.
            self.assertEqual(snap.execute("SELECT COUNT(*) FROM a").fetchone()[0], 1)
            self.assertEqual(snap.execute("SELECT COUNT(*) FROM b").fetchone()[0], 1)
            with self.assertRaises(sqlite3.Error):
                snap.execute("INSERT INTO a VALUES (3)")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM a"), [{"n": 2}])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM b"), [{"n": 2}])


# ------------------------------------------------------------------------------------------------
# Section 4: migrations
# ------------------------------------------------------------------------------------------------


class MigrationTests(StorageTestCase):
    def test_an_empty_file_gets_the_baseline_schema_through_recorded_migrations(self):
        """Covers: AC-app-storage-005."""
        applied = storage.migrate(self.db)

        ids = [item.id for item in storage.MIGRATIONS]
        self.assertEqual(ids[:2], ["0001_baseline", "0002_incremental_vacuum"])
        self.assertEqual(applied, ids)
        self.assert_baseline_schema(self.db)
        for item in storage.MIGRATIONS:
            with self.subTest(migration=item.id):
                self.assertRegex(item.id, r"^\d{4}_[a-z0-9_]+$")
                self.assertIn(item.kind, ("sql", "maintenance"))
                self.assertEqual(item.checksum, hashlib.sha256(item.sql.encode("utf-8")).hexdigest())
        self.assertEqual(self.applied(), {item.id: item.checksum for item in storage.MIGRATIONS})
        self.assertTrue(all(row["applied_at"] for row in self.rows("SELECT applied_at FROM schema_migrations")))
        self.assertEqual(self.rows("PRAGMA auto_vacuum"), [{"auto_vacuum": 2}])  # incremental
        self.assertEqual(backups(self.db), [])

    def test_a_missing_directory_is_created(self):
        """Covers: AC-app-storage-005 (section 4, step 1)."""
        db = self.dir / "state" / "nested" / "iadopt.sqlite3"
        storage.migrate(db)
        self.assertTrue(db.exists())
        self.assertIn("users", table_names(db))

    def test_an_older_file_gets_the_tables_the_previous_image_would_add(self):
        """Covers: AC-app-storage-005 (section 4: an older file gets exactly what was missing)."""
        conn = previous_image.connect(self.db)
        conn.executescript(previous_image.sql("auth_schema"))
        conn.execute("DROP TABLE saved_orcids")  # a file from before D-035 and D-037
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('early', 'x', 't', 't')"
        )
        conn.commit()
        conn.close()
        users_before = dump(self.db, {"users": [column[0] for column in columns(self.db, "users")]})

        storage.migrate(self.db)

        self.assert_baseline_schema(self.db)
        self.assertEqual(dump(self.db, {"users": [column for column, *_ in columns(reference_file(self.dir), "users")]}),
                         users_before)

    def test_a_de1a7af_file_keeps_every_row_and_gets_one_verified_backup_first(self):
        """Covers: AC-app-storage-006."""
        self.de1a7af_file()
        baseline = baseline_columns(self.dir)
        before = dump(self.db, baseline)
        everything_before = dump(self.db)
        self.assertTrue(all(before[table] for table in BASELINE_TABLES), "the fixture leaves a row in every table")

        storage.migrate(self.db)

        self.assertEqual(dump(self.db, baseline), before)
        self.assertEqual(self.applied().get("0001_baseline"), self.migrations_by_id()["0001_baseline"].checksum)
        made = backup_named(self.db)
        self.assertEqual(len(made), 1, made)
        self.assertEqual(BACKUP_NAME.match(made[0]).group(2), "pre-migration")
        copy = self.db.parent / "backups" / made[0]
        self.assertEqual(self.rows("PRAGMA integrity_check", db=copy), [{"integrity_check": "ok"}])
        # Taken before any change: the previous image's file exactly, with no bookkeeping yet.
        self.assertEqual(dump(copy), everything_before)
        self.assertNotIn("schema_migrations", table_names(copy))
        self.assertEqual(self.rows("PRAGMA auto_vacuum", db=copy), [{"auto_vacuum": 0}])

    def test_a_file_missing_a_baseline_column_or_a_trigger_is_refused_unchanged(self):
        """Covers: AC-app-storage-007."""
        missing_column = self.dir / "missing-column.sqlite3"
        script = previous_image.sql("auth_schema")
        without = re.sub(r",\s*last_login_at TEXT", "", script, count=1)
        self.assertNotEqual(without, script)
        conn = previous_image.connect(missing_column)
        conn.executescript(without)
        conn.executescript(previous_image.sql("record_schema"))
        conn.execute("INSERT INTO record_schema (version) VALUES (1)")
        conn.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('kept', 'x', 't', 't')")
        conn.commit()
        conn.close()

        missing_trigger = self.de1a7af_file()
        self.execute("DROP TRIGGER record_events_no_delete")

        for name, db in (("column", missing_column), ("trigger", missing_trigger)):
            with self.subTest(missing=name):
                schema, rows = master(db), dump(db)
                with self.assertRaises(storage.SchemaMismatch):
                    storage.migrate(db)
                self.assertEqual(master(db), schema)
                self.assertEqual(dump(db), rows)
                self.assertNotIn("schema_migrations", table_names(db))

    def test_a_changed_checksum_stops_migration_before_any_write(self):
        """Covers: AC-app-storage-008."""
        storage.migrate(self.db)
        self.execute("UPDATE schema_migrations SET checksum = ? WHERE id = '0001_baseline'", "0" * 64)
        storage.MIGRATIONS.append(migration("0900_added_later", "CREATE TABLE added_later (x INTEGER)"))
        file_before, files_before = digest(self.db), backups(self.db)

        with self.assertRaises(storage.MigrationChanged):
            storage.migrate(self.db)

        self.assertEqual(digest(self.db), file_before)
        self.assertEqual(backups(self.db), files_before)
        self.assertNotIn("added_later", table_names(self.db))

    def test_a_failing_migration_rolls_back_alone_and_is_named(self):
        """Covers: AC-app-storage-009 (no partial table, no bookkeeping row, earlier ones kept)."""
        storage.migrate(self.db)
        storage.MIGRATIONS.append(migration("0901_good", "CREATE TABLE good_t (x INTEGER)"))
        storage.MIGRATIONS.append(migration(
            "0902_bad", "CREATE TABLE half_t (x INTEGER); INSERT INTO no_such_table (x) VALUES (1);"
        ))

        with self.assertRaises(storage.MigrationFailed) as caught:
            storage.migrate(self.db)

        self.assertIn("0902_bad", str(caught.exception))
        names = table_names(self.db)
        self.assertIn("good_t", names)
        self.assertNotIn("half_t", names)
        self.assertIn("0901_good", self.applied())
        self.assertNotIn("0902_bad", self.applied())

    def test_startup_fails_on_a_failing_migration_and_names_it(self):
        """Covers: AC-app-storage-009 (section 4: when startup's migrate() raises, the application does not start)."""
        storage.MIGRATIONS.append(migration(
            "0902_bad", "CREATE TABLE half_t (x INTEGER); INSERT INTO no_such_table (x) VALUES (1);"
        ))
        with self.assertRaises(storage.MigrationFailed) as caught:
            with started_app():
                self.fail("the application started")
        self.assertIn("0902_bad", str(caught.exception))
        self.assertNotIn("half_t", table_names(self.db))

    def test_startup_stops_on_a_file_it_must_not_migrate(self):
        """Covers: AC-app-storage-007, AC-app-storage-008 (section 9: startup stops)."""
        self.de1a7af_file()
        self.execute("DROP TRIGGER record_events_no_delete")
        with self.assertRaises(storage.SchemaMismatch):
            with started_app():
                self.fail("the application started")

        changed = self.dir / "changed.sqlite3"
        storage.migrate(changed)
        self.execute("UPDATE schema_migrations SET checksum = ? WHERE id = '0001_baseline'", "0" * 64, db=changed)
        self.use_database(changed)
        with self.assertRaises(storage.MigrationChanged):
            with started_app():
                self.fail("the application started")

    def test_record_tables_stay_append_only_after_migration(self):
        """Covers: AC-app-storage-010."""
        self.de1a7af_file()
        storage.migrate(self.db)
        conn = sqlite3.connect(self.db)
        try:
            for table in RECORD_TABLES:
                first = conn.execute(f"PRAGMA table_info({table})").fetchone()[1]
                stored = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                self.assertTrue(stored, f"{table} has rows to protect")
                with self.subTest(table=table, statement="UPDATE"):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                        conn.execute(f'UPDATE {table} SET "{first}" = "{first}"')
                with self.subTest(table=table, statement="DELETE"):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                        conn.execute(f"DELETE FROM {table}")
                for verb in ("INSERT", "INSERT OR REPLACE"):
                    with self.subTest(table=table, statement=verb):
                        duplicate = f"{verb} INTO {table} SELECT * FROM {table} ORDER BY rowid LIMIT 1"
                        if table in DEDUPLICATED:
                            conn.execute(duplicate)  # dropped without an error
                        else:
                            with self.assertRaises(sqlite3.IntegrityError):
                                conn.execute(duplicate)
                conn.commit()
                self.assertEqual(conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall(), stored)
        finally:
            conn.close()

    def test_the_previous_image_still_runs_on_a_migrated_file(self):
        """Covers: AC-app-storage-011."""
        with self.subTest(file="empty, migrated by this code"):
            storage.migrate(self.db)
            self.assertEqual(previous_image.replay(self.db), [])
        with self.subTest(file="made by de1a7af, migrated by this code"):
            filled = self.dir / "filled.sqlite3"
            previous_image.make_de1a7af_file(filled)
            storage.migrate(filled)
            self.assertEqual(previous_image.start(filled), [])
            self.assertEqual(previous_image.replay(filled, include=previous_image.is_read), [])

    def test_an_applied_migration_this_code_does_not_know_is_only_a_warning(self):
        """Covers: AC-app-storage-012."""
        storage.migrate(self.db)
        self.execute(
            "INSERT INTO schema_migrations (id, checksum, applied_at) VALUES (?, ?, ?)",
            "9999_from_a_newer_image", "f" * 64, "2026-11-23T10:00:00+00:00",
        )
        storage.MIGRATIONS.append(migration("0903_after_newer", "CREATE TABLE after_newer (x INTEGER)"))

        with self.assertLogs(level="WARNING") as logged:
            applied = storage.migrate(self.db)

        self.assertIn("9999_from_a_newer_image", "\n".join(logged.output))
        self.assertEqual(applied, ["0903_after_newer"])
        self.assertIn("after_newer", table_names(self.db))

    def test_nothing_pending_means_no_backup_and_no_write(self):
        """Covers: AC-app-storage-005, AC-app-storage-006 (section 4, step 4)."""
        self.de1a7af_file()
        storage.migrate(self.db)
        files, file_before = backups(self.db), digest(self.db)
        self.assertEqual(storage.migrate(self.db), [])
        self.assertEqual(backups(self.db), files)
        self.assertEqual(digest(self.db), file_before)


# ------------------------------------------------------------------------------------------------
# Section 5: backups and space
# ------------------------------------------------------------------------------------------------


class BackupTests(StorageTestCase):
    def older_backups(self, *names: str) -> List[pathlib.Path]:
        folder = self.db.parent / "backups"
        folder.mkdir(exist_ok=True)
        paths = []
        for name in names:
            path = folder / name
            shutil.copyfile(self.db, path)
            paths.append(path)
        return paths

    def test_too_little_space_stops_migration_before_anything_is_deleted_or_written(self):
        """Covers: AC-app-storage-013 (the space rule), AC-app-storage-014 (retention counts)."""
        self.de1a7af_file()
        pad(self.db, 4)
        size = size_of(self.db)
        self.assertGreater(size, 4 * MiB)
        older = self.older_backups(
            "iadopt-20260101T000000Z-pre-migration.sqlite3",
            "iadopt-20260201T000000Z-pre-migration.sqlite3",
            "iadopt-20260115T000000Z-manual.sqlite3",
        )
        freed = older[0].stat().st_size  # retention would delete only the oldest pre-migration backup
        # 0002 is pending on a de1a7af file: the copy, the vacuum's rewrite and the reserve.
        needed = size + size + storage.SPACE_RESERVE_BYTES - freed
        files, file_before = backups(self.db), digest(self.db)

        with free_space(needed - MiB):
            self.assertEqual(storage.disk_headroom(self.db), needed - MiB)
            with self.assertRaises(storage.InsufficientSpace):
                storage.migrate(self.db)
        self.assertEqual(backups(self.db), files)
        self.assertEqual(digest(self.db), file_before)
        self.assertNotIn("schema_migrations", table_names(self.db))

        with free_space(needed + MiB):
            storage.migrate(self.db)
        self.assertIn("0001_baseline", self.applied())

    def test_without_a_pending_rewrite_the_check_is_the_copy_and_the_reserve(self):
        """Covers: AC-app-storage-013 (no maintenance migration pending)."""
        self.de1a7af_file()
        pad(self.db, 4)
        storage.migrate(self.db)  # one pre-migration backup now exists; retention frees nothing
        storage.MIGRATIONS.append(migration("0904_more", "CREATE TABLE more_t (x INTEGER)"))
        size = size_of(self.db)
        needed = size + storage.SPACE_RESERVE_BYTES

        with free_space(needed - MiB):
            with self.assertRaises(storage.InsufficientSpace):
                storage.migrate(self.db)
        self.assertNotIn("more_t", table_names(self.db))
        with free_space(needed + MiB):  # less than twice the size plus the reserve
            self.assertEqual(storage.migrate(self.db), ["0904_more"])

    def test_a_backup_is_a_verified_copy_under_a_dated_name(self):
        """Covers: AC-app-storage-013 (a verified backup)."""
        self.de1a7af_file()
        storage.migrate(self.db)
        path = storage.backup("manual", path=self.db)

        self.assertEqual(path.parent, self.db.parent / "backups")
        match = BACKUP_NAME.match(path.name)
        self.assertIsNotNone(match, path.name)
        self.assertEqual(match.group(2), "manual")
        self.assertEqual(self.rows("PRAGMA integrity_check", db=path), [{"integrity_check": "ok"}])
        self.assertEqual(table_names(path), table_names(self.db))
        self.assertEqual(self.rows("SELECT * FROM schema_migrations ORDER BY id", db=path),
                         self.rows("SELECT * FROM schema_migrations ORDER BY id"))
        for table in table_names(self.db):
            with self.subTest(table=table):
                self.assertEqual(self.rows(f"SELECT COUNT(*) AS n FROM {table}", db=path),
                                 self.rows(f"SELECT COUNT(*) AS n FROM {table}"))

    def test_a_copy_that_fails_or_fails_verification_never_gets_a_backup_name(self):
        """Covers: AC-app-storage-013 (BackupFailed; no migration runs after it)."""
        self.de1a7af_file()
        schema, rows = master(self.db), dump(self.db)
        for factory in (CorruptCopy, FailingCopy):
            with self.subTest(copy=factory.__name__):
                with connections_made_by(factory):
                    with self.assertRaises(storage.BackupFailed):
                        storage.migrate(self.db)
                    with self.assertRaises(storage.BackupFailed):
                        storage.backup("manual", path=self.db)
                self.assertEqual(backup_named(self.db), [])
                self.assertEqual(master(self.db), schema)
                self.assertEqual(dump(self.db), rows)

    def test_retention_keeps_two_pre_migration_backups_and_every_manual_one(self):
        """Covers: AC-app-storage-014."""
        self.de1a7af_file()
        self.older_backups(
            "iadopt-20260101T000000Z-pre-migration.sqlite3",
            "iadopt-20260201T000000Z-pre-migration.sqlite3",
            "iadopt-20260301T000000Z-pre-migration.sqlite3",
            "iadopt-20260115T000000Z-manual.sqlite3",
            "iadopt-20260215T000000Z-manual.sqlite3",
        )
        storage.migrate(self.db)

        pre = [name for name in backup_named(self.db) if name.endswith("-pre-migration.sqlite3")]
        manual = [name for name in backup_named(self.db) if name.endswith("-manual.sqlite3")]
        self.assertEqual(len(pre), 2, pre)
        self.assertEqual(pre[0], "iadopt-20260301T000000Z-pre-migration.sqlite3")
        self.assertGreater(pre[1], pre[0])  # the new one
        self.assertEqual(manual, ["iadopt-20260115T000000Z-manual.sqlite3", "iadopt-20260215T000000Z-manual.sqlite3"])


# ------------------------------------------------------------------------------------------------
# Section 6: space figures
# ------------------------------------------------------------------------------------------------


class StatusTests(StorageTestCase):
    def expected_tables(self, db: pathlib.Path) -> Dict[str, Dict[str, int]]:
        conn = sqlite3.connect(db)
        try:
            return {
                table: {"rows": conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], "logical_bytes": logical_bytes(conn, table)}
                for table in table_names(db)
            }
        finally:
            conn.close()

    def test_status_reports_sizes_space_migrations_and_tables_and_writes_nothing(self):
        """Covers: AC-app-storage-015."""
        self.de1a7af_file()
        storage.migrate(self.db)
        # Keep committed rows in the WAL (no checkpoint) while status() runs.
        writer = sqlite3.connect(self.db)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO audit_events (created_at, action, request_payload) VALUES ('2026-11-23', 'wal', 'é')")
        writer.commit()
        try:
            file_before = digest(self.db)
            expected = self.expected_tables(self.db)
            with free_space(123_456_789):
                report = storage.status(self.db)
            self.assertEqual(digest(self.db), file_before)

            self.assertEqual(report["path"], str(self.db))
            self.assertEqual(report["file_bytes"], self.db.stat().st_size)
            self.assertEqual(report["wal_bytes"], pathlib.Path(f"{self.db}-wal").stat().st_size)
            self.assertGreater(report["wal_bytes"], 0)
            self.assertEqual(report["free_bytes"], 123_456_789)
            pragmas = {name: self.rows(f"PRAGMA {name}")[0][name] for name in ("page_size", "page_count", "freelist_count")}
            self.assertEqual((report["page_size"], report["page_count"], report["free_pages"]),
                             (pragmas["page_size"], pragmas["page_count"], pragmas["freelist_count"]))
            self.assertEqual(report["auto_vacuum"], "incremental")
            self.assertEqual([item["id"] for item in report["applied"]], [item.id for item in storage.MIGRATIONS])
            self.assertTrue(all(item["applied_at"] for item in report["applied"]))
            self.assertEqual(report["pending"], [])
            self.assertEqual(own_tables(report), expected)
        finally:
            writer.close()

    def test_status_of_an_unmigrated_file_changes_nothing(self):
        """Covers: AC-app-storage-015 (also on an unmigrated file)."""
        self.de1a7af_file()
        file_before, schema = digest(self.db), master(self.db)
        report = storage.status(self.db)
        self.assertEqual(digest(self.db), file_before)
        self.assertEqual(master(self.db), schema)
        self.assertEqual(report["applied"], [])
        self.assertEqual(report["pending"], [item.id for item in storage.MIGRATIONS])
        self.assertEqual(report["auto_vacuum"], "none")
        self.assertEqual(own_tables(report), self.expected_tables(self.db))


class ReclaimTests(StorageTestCase):
    def test_reclaim_returns_free_pages_and_loses_no_row(self):
        """Covers: AC-app-storage-016."""
        self.de1a7af_file()
        storage.migrate(self.db)
        pad(self.db, 4)
        self.execute("DELETE FROM audit_events WHERE action = 'padding'")  # pruned rows of a prunable table
        counts = {table: self.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] for table in table_names(self.db)}
        size_before = self.db.stat().st_size
        self.assertGreater(self.rows("PRAGMA freelist_count")[0]["freelist_count"], 0)

        result = storage.reclaim_space(self.db)

        self.assertLess(result["after"]["file_bytes"], result["before"]["file_bytes"])
        self.assertEqual(result["before"]["file_bytes"], size_before)
        self.assertLess(self.db.stat().st_size, size_before - 2 * MiB)
        self.assertEqual(self.rows("PRAGMA freelist_count")[0]["freelist_count"], 0)
        self.assertEqual({table: self.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] for table in table_names(self.db)}, counts)


# ------------------------------------------------------------------------------------------------
# Section 7: operator commands
# ------------------------------------------------------------------------------------------------


class CommandTests(StorageTestCase):
    def test_db_commands_answer_0_1_or_2(self):
        """Covers: AC-app-storage-017."""
        self.de1a7af_file()

        code, out = run_cli("db", "status")
        self.assertEqual(code, 0, out)
        self.assertIn(str(self.db), out)

        code, out = run_cli("db", "migrate")
        self.assertEqual(code, 0, out)
        self.assertIn("0001_baseline", out)
        self.assertEqual(run_cli("db", "migrate")[0], 0)  # nothing pending

        code, out = run_cli("db", "backup")
        self.assertEqual(code, 0, out)
        [made] = [name for name in backup_named(self.db) if name.endswith("-manual.sqlite3")]
        self.assertIn(made, out)

        code, out = run_cli("db", "reclaim")
        self.assertEqual(code, 0, out)

        with free_space(1):
            code, out = run_cli("db", "backup")
        self.assertEqual(code, 1, out)
        self.assertIn("InsufficientSpace", out)

        self.execute("UPDATE schema_migrations SET checksum = ? WHERE id = '0001_baseline'", "0" * 64)
        code, out = run_cli("db", "migrate")
        self.assertEqual(code, 1, out)
        self.assertIn("MigrationChanged", out)

        for argv in ((), ("db",), ("db", "explode"), ("db", "status", "--no-such-option")):
            with self.subTest(argv=argv):
                self.assertEqual(run_cli(*argv)[0], 2)


# ------------------------------------------------------------------------------------------------
# Startup and the stores (sections 4 and 11)
# ------------------------------------------------------------------------------------------------


class StartupTests(StorageTestCase):
    def test_startup_on_an_empty_file_migrates_and_every_store_route_works(self):
        """Covers: AC-app-storage-018."""
        db = self.dir / "state" / "iadopt.sqlite3"  # its directory does not exist yet
        self.use_database(db)

        def two_calls(definition: str, calls: Any = None, **kwargs: Any) -> Dict[str, Any]:
            start_call(calls, "decomposition", "openrouter", "qwen/qwen3-32b").finish(
                "ok", usage={"prompt_tokens": 1500, "completion_tokens": 320})
            start_call(calls, "linking", "openrouter", "qwen/qwen3-32b", key="water").finish(
                "ok", usage={"prompt_tokens": 480, "completion_tokens": 1})
            return dict(PIPELINE_RESULT)

        with started_app() as client:
            self.assertEqual(sorted(self.applied(db=db)), sorted(item.id for item in storage.MIGRATIONS))
            self.assertTrue(storage.MIGRATIONS)
            self.login(client, "admin", "admin-password")
            self.store.create_user(username="bob", password="password-123")
            bob = self.client()
            self.login(bob, "bob")
            self.assertEqual(bob.post("/api/auth/me/orcids", json={"orcid": "0000-0002-1825-0097"}).status_code, 200)
            self.assertEqual(len(bob.get("/api/auth/me/orcids").json()["orcids"]), 1)

            with patch.object(decompose_router, "run_pipeline", side_effect=two_calls):
                decomposed = bob.post("/api/decompose", json={"definition": "dissolved oxygen in water", "client": CLIENT})
            self.assertEqual(decomposed.status_code, 200, decomposed.text)
            self.assertEqual(len(self.rows("SELECT * FROM record_llm_calls", db=db)), 2)

            started = bob.post("/api/records", json={"turtle": DRAFT, "client": CLIENT})
            self.assertEqual(started.status_code, 201, started.text)
            record_id = started.json()["record_id"]
            for body in (
                {"type": "edit", "version": 0, "turtle": EDITED, "changes": [
                    {"operation": "relabel", "role": "hasMatrix", "component": MATRIX, "before": "water", "after": "sea water"}]},
                {"type": "check", "version": 1, "changes": [{"operation": "confirm", "role": "hasMatrix", "component": MATRIX}]},
                {"type": "mark_best", "version": 1, "unchecked_components": 0},
            ):
                response = bob.post(f"/api/records/{record_id}/events", json={**body, "client": CLIENT})
                self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(bob.get(f"/api/records/{record_id}").json()["best_version"], 1)

            exported = client.get("/api/admin/records/export")
            self.assertEqual(exported.status_code, 200, exported.text)
            with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                self.assertEqual(len(json.loads(archive.read("records.jsonld"))["@graph"]), 2)

    def test_the_stores_create_no_table_on_an_unmigrated_file(self):
        """Covers: AC-app-storage-018 (section 4: stores never create or alter tables; section 11)."""
        with self.assertRaises(sqlite3.OperationalError):
            self.records.summary(user={"id": 1, "roles": []}, record_id="0123456789abcdef")
        with self.assertRaises(sqlite3.OperationalError):
            self.store.list_users()
        self.assertEqual(table_names(self.db) if self.db.exists() else [], [])

    def test_a_file_replaced_while_running_is_migrated_at_the_next_start(self):
        """Covers: AC-app-storage-018 (section 4: a replaced file is migrated at the next start)."""
        with started_app():
            pass
        os.remove(self.db)
        for suffix in ("-wal", "-shm"):
            pathlib.Path(f"{self.db}{suffix}").unlink(missing_ok=True)
        with started_app() as client:
            self.assertEqual(sorted(self.applied()), sorted(item.id for item in storage.MIGRATIONS))
            self.login(client, "admin", "admin-password")
