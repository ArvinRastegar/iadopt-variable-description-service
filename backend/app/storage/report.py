"""The database's status report (app-storage, section 6)."""

from __future__ import annotations

import pathlib
import sqlite3
from typing import Any, Dict, Optional

from .backups import quoted
from .connections import database_path, look
from .migrations import MIGRATIONS, applied_migrations, table_names
from .space import disk_headroom, file_sizes

_AUTO_VACUUM = {0: "none", 1: "full", 2: "incremental"}


def status(path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Report the database's size, space and migrations without writing (section 6).

    Keys: ``path``, ``file_bytes``, ``wal_bytes``, ``free_bytes``, ``page_size``,
    ``page_count``, ``free_pages``, ``auto_vacuum`` (``none``, ``full`` or ``incremental``),
    ``applied`` (``[{"id", "applied_at"}]``), ``pending`` (ids) and ``tables``
    (``{name: {"rows", "logical_bytes"}}``, SQLite's internal tables left out). A file that does
    not exist reports zeros and stays missing.

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.
    """
    db = database_path(path)
    report: Dict[str, Any] = {
        "path": str(db),
        **file_sizes(db),
        "free_bytes": disk_headroom(db),
        "page_size": 0,
        "page_count": 0,
        "free_pages": 0,
        "auto_vacuum": "none",
        "applied": [],
        "pending": [],
        "tables": {},
    }
    if db.exists():
        with look(db) as conn:
            report["page_size"] = _pragma(conn, "page_size")
            report["page_count"] = _pragma(conn, "page_count")
            report["free_pages"] = _pragma(conn, "freelist_count")
            report["auto_vacuum"] = _AUTO_VACUUM[_pragma(conn, "auto_vacuum")]
            report["applied"] = [{"id": row["id"], "applied_at": row["applied_at"]} for row in applied_migrations(conn)]
            report["tables"] = {
                name: _table_figures(conn, name) for name in sorted(table_names(conn)) if not name.startswith("sqlite_")
            }
    applied = {item["id"] for item in report["applied"]}
    report["pending"] = [migration.id for migration in MIGRATIONS if migration.id not in applied]
    return report


def _pragma(conn: sqlite3.Connection, name: str) -> int:
    """Read one numeric pragma."""
    return int(conn.execute(f"PRAGMA {name}").fetchone()[0])


def _table_figures(conn: sqlite3.Connection, table: str) -> Dict[str, int]:
    """A table's row count and logical bytes: the length of every value as a blob, NULL counting 0."""
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({quoted(table)})")]
    lengths = " + ".join(f"COALESCE(length(CAST({quoted(column)} AS BLOB)), 0)" for column in columns)
    row = conn.execute(f"SELECT COUNT(*), COALESCE(SUM({lengths}), 0) FROM {quoted(table)}").fetchone()
    return {"rows": int(row[0]), "logical_bytes": int(row[1])}
