"""Ordered, checksummed, additive migrations with a verified backup first (app-storage, section 4)."""

from __future__ import annotations

import hashlib
import logging
import pathlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Set, Tuple

from .backups import make_backup, quoted
from .connections import busy_as_storage_busy, connect, database_path, look, transaction
from .errors import MigrationChanged, MigrationFailed, SchemaMismatch, StorageBusy
from .locks import only_one_process
from .schema import (
    ACCOUNT_APPROVAL_SQL,
    ACCOUNTS_ROLES_SQL,
    BASELINE_SQL,
    DAILY_DECOMPOSITIONS_SQL,
    INCREMENTAL_VACUUM_SQL,
    OIDC_IDENTITY_SQL,
    RECORD_CHOICES_SQL,
)

logger = logging.getLogger(__name__)

SQL = "sql"
MAINTENANCE = "maintenance"
INCREMENTAL = 2
"""``PRAGMA auto_vacuum`` when it is incremental."""


@dataclass(frozen=True)
class Migration:
    """One schema change: an id ``NNNN_name``, a kind (``sql`` or ``maintenance``) and its SQL.

    Attributes:
        id: ``NNNN_name``, ``NNNN`` increasing from ``0001``.
        kind: ``sql`` (one immediate transaction with its bookkeeping row) or ``maintenance``
            (outside a transaction, idempotent, such as ``VACUUM``).
        sql: The SQL text; its SHA-256 in UTF-8 is the checksum.
    """

    id: str
    kind: str
    sql: str

    @property
    def checksum(self) -> str:
        """The SHA-256 of :attr:`sql` in UTF-8, as hex."""
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


BASELINE_ID = "0001_baseline"
INCREMENTAL_VACUUM_ID = "0002_incremental_vacuum"

MIGRATIONS: List[Migration] = [
    Migration(BASELINE_ID, SQL, BASELINE_SQL),
    Migration(INCREMENTAL_VACUUM_ID, MAINTENANCE, INCREMENTAL_VACUUM_SQL),
    Migration("0003_accounts_roles", SQL, ACCOUNTS_ROLES_SQL),
    Migration("0004_oidc_identity", SQL, OIDC_IDENTITY_SQL),
    Migration("0005_daily_decompositions", SQL, DAILY_DECOMPOSITIONS_SQL),
    Migration("0006_account_approval", SQL, ACCOUNT_APPROVAL_SQL),
    Migration("0007_record_choices", SQL, RECORD_CHOICES_SQL),
]
"""The ordered migrations: app-storage's own two, then those of the modules that own the tables."""

_BOOKKEEPING = (
    "CREATE TABLE IF NOT EXISTS schema_migrations"
    " (id TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
)


def migrate(path: Optional[pathlib.Path] = None) -> List[str]:
    """Apply the pending migrations of :data:`MIGRATIONS` in order, after a verified backup.

    Section 4: refuses a changed checksum, warns about unknown applied ids, does nothing when
    nothing is pending, checks the space and makes a ``pre-migration`` backup when the file holds a
    table, and raises ``MigrationFailed`` naming a failing migration. Raises
    :class:`MigrationChanged`, :class:`InsufficientSpace`, :class:`BackupFailed`,
    :class:`SchemaMismatch` or :class:`MigrationFailed`; each leaves the file as it was before the
    migration that failed.

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.

    Returns:
        The ids applied now.
    """
    db = database_path(path)
    db.parent.mkdir(parents=True, exist_ok=True)
    applied, _ = _read(db)
    unknown = sorted(set(applied) - {migration.id for migration in MIGRATIONS})
    if unknown:
        logger.warning("The database holds migrations this code does not know: %s.", ", ".join(unknown))
    if not _pending(applied):
        return []
    with only_one_process(db.parent) as alone:
        applied, holds_tables = _read(db)  # again: another process may just have migrated
        pending = _pending(applied)
        if pending and holds_tables:
            if BASELINE_ID in (migration.id for migration in pending):
                # Before the backup: refused starts must not replace good backups with this file.
                with look(db) as conn:
                    _check_baseline_shape(conn, complete=False)
            rewrite = any(migration.kind == MAINTENANCE for migration in pending)
            make_backup("pre-migration", db, alone=alone, rewrite=rewrite)
        return [migration.id for migration in pending if _apply(db, migration)]


def _read(db: pathlib.Path) -> Tuple[Dict[str, str], bool]:
    """Read the applied migrations' checksums, and whether the file holds a table; nothing is written."""
    if not db.exists():
        return {}, False
    with look(db) as conn:
        return {row["id"]: row["checksum"] for row in applied_migrations(conn)}, bool(table_names(conn))


def _pending(applied: Dict[str, str]) -> List[Migration]:
    """Return the migrations still to apply, in order.

    Raises:
        MigrationChanged: If an applied migration's checksum differs from the code's.
    """
    changed = [migration.id for migration in MIGRATIONS if applied.get(migration.id, migration.checksum) != migration.checksum]
    if changed:
        raise MigrationChanged(f"Applied with another text than this code's: {', '.join(changed)}.")
    return [migration for migration in MIGRATIONS if migration.id not in applied]


def table_names(conn: sqlite3.Connection) -> Set[str]:
    """Return the names of the file's tables.

    Args:
        conn: A connection to the file.
    """
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def applied_migrations(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Return the applied migrations in order, none before the first migration.

    Args:
        conn: A connection to the file.
    """
    if "schema_migrations" not in table_names(conn):
        return []
    return conn.execute("SELECT id, checksum, applied_at FROM schema_migrations ORDER BY id").fetchall()


def _apply(db: pathlib.Path, migration: Migration) -> bool:
    """Apply one migration and record it, unless another process already did.

    Returns:
        Whether it was applied here.

    Raises:
        MigrationFailed: If one of its statements fails; an ``sql`` migration is rolled back.
        StorageBusy: If the lock was not obtained within the busy timeout.
    """
    try:
        if migration.kind == MAINTENANCE:
            _maintain(db, migration)
        with transaction(immediate=True, path=db) as conn:
            conn.execute(_BOOKKEEPING)
            if conn.execute("SELECT 1 FROM schema_migrations WHERE id = ?", (migration.id,)).fetchone():
                return False
            if migration.kind == SQL:
                _change_schema(conn, migration)
            conn.execute(
                "INSERT INTO schema_migrations (id, checksum, applied_at) VALUES (?, ?, ?)",
                (migration.id, migration.checksum, datetime.now(timezone.utc).isoformat()),
            )
    except StorageBusy:
        raise
    except sqlite3.Error as e:
        raise MigrationFailed(f"Migration {migration.id} failed: {e}") from e
    logger.info("Applied migration %s.", migration.id)
    return True


def _change_schema(conn: sqlite3.Connection, migration: Migration) -> None:
    """Run an ``sql`` migration's statements inside its transaction."""
    baseline = migration.id == BASELINE_ID
    if baseline:
        # The file as it was: the baseline's IF NOT EXISTS would put a dropped trigger back.
        _check_baseline_shape(conn, complete=False)
    for statement in _statements(migration.sql):
        conn.execute(statement)
    if baseline:
        _check_baseline_shape(conn, complete=True)


def _maintain(db: pathlib.Path, migration: Migration) -> None:
    """Run a ``maintenance`` migration's statements outside a transaction."""
    conn = connect(db)
    try:
        conn.isolation_level = None
        if migration.id == INCREMENTAL_VACUUM_ID and conn.execute("PRAGMA auto_vacuum").fetchone()[0] == INCREMENTAL:
            return
        with busy_as_storage_busy():
            for statement in _statements(migration.sql):
                conn.execute(statement).fetchall()
    finally:
        conn.close()


def _statements(sql: str) -> Iterator[str]:
    """Split a migration's text into its statements.

    ``executescript`` would commit the migration's transaction, so each statement runs on its own.

    Yields:
        One statement after the other, in order.
    """
    start = 0
    for index, character in enumerate(sql):
        if character == ";" and sqlite3.complete_statement(sql[start : index + 1]):
            yield sql[start : index + 1]
            start = index + 1
    if sql[start:].strip():
        yield sql[start:]


def _check_baseline_shape(conn: sqlite3.Connection, *, complete: bool) -> None:
    """Check that the file's baseline tables have their columns, and its record tables their triggers.

    Args:
        conn: The migration's connection.
        complete: Whether every baseline table must be there; otherwise a missing one is fine,
            because the baseline creates it.

    Raises:
        SchemaMismatch: If a baseline table lacks a column or a trigger, or is missing.
    """
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(BASELINE_SQL)
        expected = _shape(reference)
    finally:
        reference.close()
    found = _shape(conn)
    problems = []
    for table, (columns, triggers) in sorted(expected.items()):
        if table not in found:
            if complete:
                problems.append(f"the table {table} is missing")
            continue
        found_columns, found_triggers = found[table]
        problems += [f"{table} lacks the column {name}" for name in sorted(columns - found_columns)]
        problems += [f"{table} lacks the trigger {name}" for name in sorted(triggers - found_triggers)]
    if problems:
        raise SchemaMismatch(f"This is not a database this code can take over: {'; '.join(problems)}.")


def _shape(conn: sqlite3.Connection) -> Dict[str, Tuple[Set[str], Set[str]]]:
    """Every table's column and trigger names."""
    triggers: Dict[str, Set[str]] = {}
    for row in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'").fetchall():
        triggers.setdefault(row[1], set()).add(row[0])
    return {
        table: ({row[1] for row in conn.execute(f"PRAGMA table_info({quoted(table)})")}, triggers.get(table, set()))
        for table in table_names(conn)
    }
