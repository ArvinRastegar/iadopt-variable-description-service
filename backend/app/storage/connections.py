"""Connections and transactions (app-storage, section 3).

Every connection the application opens to its database comes from here: foreign keys on, the WAL
journal, a busy timeout of 30 seconds and rows readable by column name. ``sqlite3.connect`` is
looked up when it is called, so tests can watch or replace it.
"""

from __future__ import annotations

import pathlib
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

from ..core.config import settings
from .errors import StorageBusy, is_busy

BUSY_TIMEOUT_SECONDS = 30.0
"""How long a connection waits for a lock before :class:`StorageBusy` (section 3)."""


def database_path(path: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Return the database file: ``path``, or the one ``Settings.auth_db_path`` resolves.

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.
    """
    return settings.auth_db_path if path is None else pathlib.Path(path)


@contextmanager
def busy_as_storage_busy() -> Iterator[None]:
    """Turn a lock that was not obtained in time into :class:`StorageBusy`.

    Yields:
        Nothing; it wraps the statements that may have to wait.

    Raises:
        StorageBusy: When the lock was not obtained within the busy timeout.
        sqlite3.OperationalError: Any other operational error, unchanged.
    """
    try:
        yield
    except sqlite3.OperationalError as e:
        if is_busy(e) and not isinstance(e, StorageBusy):
            raise StorageBusy(str(e)) from e
        raise


def _prepare(conn: sqlite3.Connection) -> None:
    """Set what every connection has besides its busy timeout and journal."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")


def connect(path: Optional[pathlib.Path] = None, *, busy_timeout: float = BUSY_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Open a connection with the connection policy of section 3.

    Foreign keys on, the WAL journal, the busy timeout and rows readable by column name. Every
    module gets its connections here; nothing else in ``app/`` calls ``sqlite3.connect``. Raises
    :class:`StorageBusy` when the journal mode cannot be set within the busy timeout.

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.
        busy_timeout: Seconds to wait for a lock.
    """
    conn = sqlite3.connect(database_path(path), timeout=busy_timeout)
    try:
        _prepare(conn)
        with busy_as_storage_busy():
            conn.execute("PRAGMA journal_mode = WAL")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def begun(conn: sqlite3.Connection, begin: str) -> Iterator[None]:
    """Run a block inside one transaction of ``conn``: commit at its end, roll back on an exception.

    The connection must be in autocommit mode (``isolation_level`` ``None``), so that nothing but
    ``begin`` opens a transaction.

    Args:
        conn: The connection.
        begin: The statement that opens the transaction.

    Yields:
        Nothing; the caller already holds the connection.
    """
    with busy_as_storage_busy():
        try:
            conn.execute(begin)
            yield
            if conn.in_transaction:
                conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


@contextmanager
def transaction(
    immediate: bool = False,
    *,
    path: Optional[pathlib.Path] = None,
    busy_timeout: Optional[float] = None,
) -> Iterator[sqlite3.Connection]:
    """Open a connection and a transaction: commit at a normal end, roll back on an exception.

    Immediate mode begins with ``BEGIN IMMEDIATE``. A lock not obtained within the busy timeout
    raises :class:`StorageBusy` and leaves no change. The connection is always closed.

    Args:
        immediate: Take the write lock at the start (check-and-write sections).
        path: The database file; ``None`` for ``Settings.auth_db_path``.
        busy_timeout: Seconds to wait for a lock; ``None`` for 30.

    Yields:
        The open connection, inside the transaction.
    """
    conn = connect(path, busy_timeout=BUSY_TIMEOUT_SECONDS if busy_timeout is None else busy_timeout)
    try:
        conn.isolation_level = None
        with begun(conn, "BEGIN IMMEDIATE" if immediate else "BEGIN"):
            yield conn
    finally:
        conn.close()


@contextmanager
def snapshot(*, path: Optional[pathlib.Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a read-only connection inside one read transaction (``PRAGMA query_only = ON``).

    Every query sees the file as it was when the snapshot opened, while other connections keep
    writing; a write through it fails.

    Args:
        path: The database file; ``None`` for ``Settings.auth_db_path``.

    Yields:
        The open connection.
    """
    conn = connect(path)
    try:
        with _reading(conn):
            yield conn
    finally:
        conn.close()


@contextmanager
def look(path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """Open an existing file for reading in one moment, changing nothing, not even its journal mode.

    Unlike :func:`snapshot` it never creates the file, and a WAL that a killed process left behind
    stays as it is.

    Args:
        path: The database file, which exists.

    Yields:
        The open connection.
    """
    uri = f"{path.resolve().as_uri()}?mode=rw"
    has_wal = pathlib.Path(f"{path}-wal").exists()
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        if has_wal:
            # The last connection to close would write it into the file and remove it.
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE)
        _prepare(conn)
        with _reading(conn):
            yield conn
    finally:
        conn.close()


@contextmanager
def _reading(conn: sqlite3.Connection) -> Iterator[None]:
    """Make a connection read-only and keep it at one moment for the whole block."""
    conn.isolation_level = None
    conn.execute("PRAGMA query_only = ON")
    with begun(conn, "BEGIN"):
        # BEGIN alone takes no snapshot; the first read does.
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        yield
