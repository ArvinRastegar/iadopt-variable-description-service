"""The storage errors (app-storage, section 9)."""

from __future__ import annotations

import sqlite3


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


def is_busy(error: BaseException) -> bool:
    """Return whether an error is SQLite's refusal to wait any longer for a lock.

    Args:
        error: The exception to look at.
    """
    # The primary code: a busy snapshot (a deferred transaction that cannot become a writer) counts too.
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(error, sqlite3.OperationalError) and code is not None and code & 0xFF == sqlite3.SQLITE_BUSY
