"""Sizes, free space and reclaiming it (app-storage, section 6)."""

from __future__ import annotations

import pathlib
import shutil
import sqlite3
from typing import Dict, Optional

from .connections import BUSY_TIMEOUT_SECONDS, busy_as_storage_busy, connect, database_path
from .errors import StorageError

SPACE_RESERVE_BYTES = 64 * 1024 * 1024
"""Free space kept beyond a backup and any rewrite by a maintenance migration (section 5)."""

CHECKPOINT_WAIT_SECONDS = 1.0
"""How long reclaiming waits for a reader at a checkpoint: writers wait as long."""


def disk_headroom(path: Optional[pathlib.Path] = None) -> int:
    """Return the free bytes of the file system holding the database (``shutil.disk_usage``).

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.
    """
    folder = database_path(path).absolute().parent
    while not folder.exists():
        folder = folder.parent
    return int(shutil.disk_usage(folder).free)


def file_sizes(db: pathlib.Path) -> Dict[str, int]:
    """Return the bytes of the database file and of its WAL, 0 for one that does not exist.

    Args:
        db: The database file.
    """
    return {"file_bytes": size_of(db), "wal_bytes": size_of(pathlib.Path(f"{db}-wal"))}


def size_of(path: pathlib.Path) -> int:
    """Return a file's bytes, 0 when it does not exist.

    Args:
        path: The file.
    """
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def reclaim_space(path: Optional[pathlib.Path] = None) -> Dict[str, Dict[str, int]]:
    """Checkpoint the WAL (``TRUNCATE``) and run ``PRAGMA incremental_vacuum``; delete no row.

    Safe while the backend runs: while a reader holds an older snapshot the checkpoint gives up
    soon, and the file shrinks at a later one. Returns ``{"before": {"file_bytes", "wal_bytes"},
    "after": {"file_bytes", "wal_bytes"}}``.

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.

    Raises:
        StorageError: When the file does not exist.
    """
    db = database_path(path)
    if not db.exists():
        raise StorageError(f"There is no database at {db}.")
    before = file_sizes(db)
    conn = connect(db)
    try:
        conn.isolation_level = None
        with busy_as_storage_busy():
            completed = _checkpoint(conn)
            # Read to its end: the vacuum frees one page per step.
            conn.execute("PRAGMA incremental_vacuum").fetchall()
            if completed:
                _checkpoint(conn)  # now the file itself shrinks
        after = file_sizes(db)
    finally:
        conn.close()
    return {"before": before, "after": after}


def _checkpoint(conn: sqlite3.Connection) -> bool:
    """Write the WAL into the file and empty it, without keeping writers waiting for long.

    Returns:
        Whether it completed; not while a reader holds an older snapshot.
    """
    conn.execute(f"PRAGMA busy_timeout = {int(CHECKPOINT_WAIT_SECONDS * 1000)}")
    try:
        return bool(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0)
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
