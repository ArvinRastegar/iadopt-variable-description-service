"""One process at a time makes backups and migrations of a database (app-storage, sections 4 and 5)."""

from __future__ import annotations

import logging
import os
import pathlib
import time
from contextlib import contextmanager
from typing import Iterator

from .errors import StorageBusy

try:
    import fcntl
except ImportError:  # no flock on Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

LOCK_WAIT_SECONDS = 300.0
"""How long a migration or backup waits for another: a start must outlast an operator's backup."""


@contextmanager
def only_one_process(folder: pathlib.Path) -> Iterator[bool]:
    """Keep other processes from backing up or migrating the databases of a directory meanwhile.

    Each migration and each copy is safe without it. The lock keeps a second process from backing
    up a half-migrated file as ``pre-migration``, from leaving more backups than the retention rule
    allows and from taking the same backup name, and it makes an unfinished copy that is found
    while it is held the leftover of an interrupted backup. It is a lock on the directory, so it
    leaves no file behind and ends with the process.

    Args:
        folder: The database's directory, which exists.

    Yields:
        Whether the lock is held; not on a system or file system without such locks.

    Raises:
        StorageBusy: If another process still holds the lock after :data:`LOCK_WAIT_SECONDS`.
    """
    if fcntl is None:
        yield False
        return
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        held = True
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as e:
                if time.monotonic() >= deadline:
                    raise StorageBusy("Another process is migrating or backing up the database.") from e
                time.sleep(0.05)
            except OSError as e:
                logger.warning("No lock on %s: %s", folder, e)
                held = False
                break
        yield held
    finally:
        os.close(descriptor)
