"""The application's one SQLite database: connections, transactions, migrations, backups, space.

Contract: ``components/app-storage.md`` in the private workflow repository, approved under gate
G-017. The modules follow its sections: ``connections`` (section 3), ``migrations`` with their text
in ``schema`` (section 4), ``backups`` (section 5), ``space`` and ``report`` (section 6) and
``errors`` (section 9).

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

from .backups import BACKUP_REASONS, backup
from .connections import BUSY_TIMEOUT_SECONDS, connect, snapshot, transaction
from .errors import (
    BackupFailed,
    InsufficientSpace,
    MigrationChanged,
    MigrationFailed,
    SchemaMismatch,
    StorageBusy,
    StorageError,
)
from .migrations import MIGRATIONS, Migration, migrate
from .report import status
from .space import SPACE_RESERVE_BYTES, disk_headroom, reclaim_space

__all__ = [
    "BACKUP_REASONS",
    "BUSY_TIMEOUT_SECONDS",
    "MIGRATIONS",
    "SPACE_RESERVE_BYTES",
    "BackupFailed",
    "InsufficientSpace",
    "Migration",
    "MigrationChanged",
    "MigrationFailed",
    "SchemaMismatch",
    "StorageBusy",
    "StorageError",
    "backup",
    "connect",
    "disk_headroom",
    "migrate",
    "reclaim_space",
    "snapshot",
    "status",
    "transaction",
]
