"""The application's one SQLite database: connections, transactions, migrations, backups, space.

Contract: ``components/app-storage.md`` in the private workflow repository, approved under gate
G-017. This is the skeleton of the increment-1 tests gate: the public names and signatures the
behavioural tests in ``tests/test_app_storage.py`` call. Every body raises
``NotImplementedError`` until the implementation gate.

Seams agreed at the tests gate (beyond the contract's names):

* Every function takes ``path``; ``None`` means the path ``Settings.auth_db_path`` resolves.
* ``transaction`` and ``connect`` take ``busy_timeout`` in seconds (30 unless a caller passes
  another).
* ``MIGRATIONS`` is a list of :class:`Migration`; tests add a migration by appending to it.
* Free space is read through ``shutil.disk_usage`` on the database's directory, so tests can
  simulate a full volume.
* ``AuthStore.init()`` runs :func:`migrate` on its database before it uses it.
"""

from __future__ import annotations

import pathlib
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

BUSY_TIMEOUT_SECONDS = 30.0
"""How long a connection waits for a lock before :class:`StorageBusy` (section 3)."""

SPACE_RESERVE_BYTES = 64 * 1024 * 1024
"""Free space kept beyond a backup and any rewrite by a maintenance migration (section 5)."""

BACKUP_REASONS = ("pre-migration", "manual")
"""The reasons a backup's name can carry (section 5)."""

_TODO = "app-storage is specified (components/app-storage.md) but not implemented yet."


class StorageError(Exception):
    """Base of the storage errors of section 9, except :class:`StorageBusy`."""


class SchemaMismatch(StorageError):
    """A baseline table lacks a baseline column, or a record table lacks a trigger."""


class MigrationChanged(StorageError):
    """An applied migration's checksum differs from the code's."""


class MigrationFailed(StorageError):
    """A migration's statements failed; the message names the migration."""


class BackupFailed(StorageError):
    """A backup's copy or its verification failed."""


class InsufficientSpace(StorageError):
    """The file system has no room for a backup, any rewrite and the reserve."""


class StorageBusy(sqlite3.OperationalError):
    """The write lock was not obtained within the busy timeout."""


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
        """The SHA-256 of :attr:`sql` in UTF-8, as hex.

        Raises:
            NotImplementedError: Until the implementation gate.
        """
        raise NotImplementedError(_TODO)


MIGRATIONS: List[Migration] = []
"""The ordered migrations: ``0001_baseline``, ``0002_incremental_vacuum``, then those of later
modules (``accounts-roles`` adds its own). Empty in this skeleton."""


def connect(path: Optional[pathlib.Path] = None, *, busy_timeout: float = BUSY_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Open a connection with the connection policy of section 3.

    Foreign keys on, the WAL journal, the busy timeout and rows readable by column name. Every
    module gets its connections here; nothing else in ``app/`` calls ``sqlite3.connect``.

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.
        busy_timeout: Seconds to wait for a lock.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError(_TODO)


def transaction(
    immediate: bool = False,
    *,
    path: Optional[pathlib.Path] = None,
    busy_timeout: Optional[float] = None,
) -> AbstractContextManager[sqlite3.Connection]:
    """Open a connection and a transaction: commit at a normal end, roll back on an exception.

    Immediate mode begins with ``BEGIN IMMEDIATE``. A lock not obtained within the busy timeout
    raises ``StorageBusy`` and leaves no change. The connection is always closed.

    Args:
        immediate: Take the write lock at the start (check-and-write sections).
        path: The database file; ``None`` for ``Settings.auth_db_path``.
        busy_timeout: Seconds to wait for a lock; ``None`` for 30.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError(_TODO)


def snapshot(*, path: Optional[pathlib.Path] = None) -> AbstractContextManager[sqlite3.Connection]:
    """Open a read-only connection inside one read transaction (``PRAGMA query_only = ON``).

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError(_TODO)


def migrate(path: Optional[pathlib.Path] = None) -> List[str]:
    """Apply the pending migrations of :data:`MIGRATIONS` in order, after a verified backup.

    Section 4: refuses a changed checksum (``MigrationChanged``), warns about unknown applied
    ids, does nothing when nothing is pending, checks the space and makes a ``pre-migration``
    backup when the file holds a table, and raises ``MigrationFailed`` naming a failing
    migration.

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError(_TODO)


def backup(reason: str, *, path: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Copy the database with SQLite's online backup API and verify the copy (section 5).

    Writes ``<database directory>/backups/iadopt-<UTC yyyymmddThhmmssZ>-<reason>.sqlite3``;
    a ``pre-migration`` backup first deletes older ``pre-migration`` backups down to the newest.

    Args:
        reason: ``pre-migration`` or ``manual``.
        path: The database file; ``None`` for ``Settings.auth_db_path``.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError(_TODO)


def status(path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Report the database's size, space and migrations without writing (section 6).

    Keys: ``path``, ``file_bytes``, ``wal_bytes``, ``free_bytes``, ``page_size``,
    ``page_count``, ``free_pages``, ``auto_vacuum`` (``none``, ``full`` or ``incremental``),
    ``applied`` (``[{"id", "applied_at"}]``), ``pending`` (ids) and ``tables``
    (``{name: {"rows", "logical_bytes"}}``).

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError(_TODO)


def disk_headroom(path: Optional[pathlib.Path] = None) -> int:
    """Return the free bytes of the file system holding the database (``shutil.disk_usage``).

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError(_TODO)


def reclaim_space(path: Optional[pathlib.Path] = None) -> Dict[str, Dict[str, int]]:
    """Checkpoint the WAL (``TRUNCATE``) and run ``PRAGMA incremental_vacuum``; delete no row.

    Returns ``{"before": {"file_bytes", "wal_bytes"}, "after": {"file_bytes", "wal_bytes"}}``.

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError(_TODO)
