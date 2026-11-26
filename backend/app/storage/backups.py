"""Verified backups beside the database (app-storage, section 5)."""

from __future__ import annotations

import pathlib
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from .connections import database_path, snapshot
from .errors import BackupFailed, InsufficientSpace
from .locks import only_one_process
from .space import SPACE_RESERVE_BYTES, disk_headroom, file_sizes, size_of

BACKUP_REASONS = ("pre-migration", "manual")
"""The reasons a backup's name can carry (section 5)."""

KEPT_PRE_MIGRATION_BACKUPS = 2
"""Most ``pre-migration`` backups on the volume: the new one and the newest before it."""

STALE_PARTIAL_SECONDS = 3600
"""Age after which an unfinished copy is a leftover where no lock says that nobody is writing it."""

_PRE_MIGRATION_NAME = re.compile(r"^iadopt-\d{8}T\d{6}Z-pre-migration\.sqlite3$")


def backup(reason: str, *, path: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Copy the database with SQLite's online backup API and verify the copy (section 5).

    Writes ``<database directory>/backups/iadopt-<UTC yyyymmddThhmmssZ>-<reason>.sqlite3``;
    a ``pre-migration`` backup first deletes older ``pre-migration`` backups down to the newest.

    Args:
        reason: ``pre-migration`` or ``manual``.
        path: The database file; ``None`` for ``Settings.auth_db_path``.

    Returns:
        The backup's path.

    Raises:
        ValueError: If the reason is neither of :data:`BACKUP_REASONS`.
        BackupFailed: If there is no database, or the copy or its verification fails.
    """
    if reason not in BACKUP_REASONS:
        raise ValueError(f"A backup is {' or '.join(BACKUP_REASONS)}, not {reason!r}.")
    db = database_path(path)
    if not db.exists():
        raise BackupFailed(f"There is no database at {db}.")
    with only_one_process(db.parent) as alone:
        return make_backup(reason, db, alone=alone)


def make_backup(reason: str, db: pathlib.Path, *, alone: bool, rewrite: bool = False) -> pathlib.Path:
    """Check the space, apply the retention rule, and make a verified backup.

    Args:
        reason: ``pre-migration`` or ``manual``.
        db: The database file, which exists.
        alone: Whether the caller holds the lock of :func:`only_one_process`.
        rewrite: Whether a pending maintenance migration will rewrite the file, which needs its
            size once more.

    Returns:
        The backup's path.

    Raises:
        BackupFailed: If the copy or its verification fails.
        InsufficientSpace: If the file system has no room; nothing was deleted or written.
    """
    folder = db.parent / "backups"
    expired = _expired(folder) if reason == "pre-migration" else []
    removable = expired + _leftovers(folder, alone=alone)
    size = sum(file_sizes(db).values())
    needed = size * (2 if rewrite else 1) + SPACE_RESERVE_BYTES
    available = disk_headroom(db) + sum(size_of(path) for path in removable)
    if available < needed:
        raise InsufficientSpace(
            f"A backup of {db} needs {needed} bytes free, {SPACE_RESERVE_BYTES} of them as a reserve"
            f"{' and the database size twice, because a pending migration rewrites the file' if rewrite else ''}; "
            f"{available} are available."
        )
    try:
        folder.mkdir(exist_ok=True)
        for path in removable:
            path.unlink(missing_ok=True)
        return _verified_copy(db, folder, reason)
    except (sqlite3.Error, OSError) as e:
        raise BackupFailed(f"The backup of {db} failed: {e}") from e


def _expired(folder: pathlib.Path) -> List[pathlib.Path]:
    """The ``pre-migration`` backups the retention rule deletes before a new one is copied."""
    if not folder.is_dir():
        return []
    existing = sorted(path for path in folder.iterdir() if _PRE_MIGRATION_NAME.match(path.name))
    return existing[: max(0, len(existing) - (KEPT_PRE_MIGRATION_BACKUPS - 1))]


def _leftovers(folder: pathlib.Path, *, alone: bool) -> List[pathlib.Path]:
    """The unfinished copies that interrupted backups left behind."""
    return [path for path in folder.glob(".*.partial*") if alone or _age(path) > STALE_PARTIAL_SECONDS]


def _age(path: pathlib.Path) -> float:
    """How many seconds ago a file was written to; 0 when it is gone."""
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return 0


def _verified_copy(db: pathlib.Path, folder: pathlib.Path, reason: str) -> pathlib.Path:
    """Copy the database at one moment under a temporary name; rename the copy once it is verified."""
    partial = folder / f".{uuid.uuid4().hex}.partial"
    try:
        # A plain connection: this file is not the application's database.
        target = sqlite3.connect(partial)
        try:
            with snapshot(path=db) as source:
                expected = _contents(source)
                source.backup(target)
        finally:
            target.close()
        _verify(partial, expected)
        final = _unused_name(folder, reason)
        partial.rename(final)
        return final
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            pathlib.Path(f"{partial}{suffix}").unlink(missing_ok=True)


def _verify(copy: pathlib.Path, expected: Any) -> None:
    """Check a finished copy: ``integrity_check``, and the source's tables, migrations and row counts.

    Raises:
        BackupFailed: If the copy is damaged or differs from the source.
    """
    # Immutable: reading the copy must not leave journal files beside it.
    conn = sqlite3.connect(f"{copy.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        problems = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
        if problems != ["ok"]:
            raise BackupFailed(f"The copy failed its integrity check: {'; '.join(problems[:3])}")
        if _contents(conn) != expected:
            raise BackupFailed("The copy does not hold the tables, migrations and row counts of the database.")
    finally:
        conn.close()


def _contents(conn: sqlite3.Connection) -> Tuple[List[Tuple[str, int]], List[Tuple[Any, ...]]]:
    """What a copy must share with its source: every table's row count, and the migration rows."""
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    counts = [(name, int(conn.execute(f"SELECT COUNT(*) FROM {quoted(name)}").fetchone()[0])) for name in names]
    migrations: List[Tuple[Any, ...]] = []
    if "schema_migrations" in names:
        rows = conn.execute("SELECT id, checksum, applied_at FROM schema_migrations ORDER BY id").fetchall()
        migrations = [tuple(row) for row in rows]
    return counts, migrations


def quoted(name: str) -> str:
    """Return a table or column name as an SQL identifier.

    Args:
        name: The name.
    """
    return '"' + name.replace('"', '""') + '"'


def _unused_name(folder: pathlib.Path, reason: str) -> pathlib.Path:
    """The backup's dated name; a second backup within one second waits for the next."""
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = folder / f"iadopt-{stamp}-{reason}.sqlite3"
        if not path.exists():
            return path
        time.sleep(0.1)
