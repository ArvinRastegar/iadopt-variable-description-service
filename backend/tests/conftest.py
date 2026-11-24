"""Keep every test away from the developer's own database.

``Settings.auth_db_path`` defaults to ``backend/state/iadopt.sqlite3``, the local stack's real
database. Before any test module imports the application, point ``IADOPT_DB_PATH`` at a file in a
temporary directory for the whole session, so that code reaching the default path (a fresh
``Settings()``, a store built from settings, a subprocess that inherits the environment) touches
that file and never the real one. Tests still set their own database as before. Added at the
increment-1 tests gate (G-018), whose commands write to the database (migrations, backups,
``legacy purge-passwords``).
"""

from __future__ import annotations

import atexit
import os
import pathlib
import shutil
import tempfile

_SESSION_DIR = pathlib.Path(tempfile.mkdtemp(prefix="iadopt-tests-"))
atexit.register(shutil.rmtree, _SESSION_DIR, ignore_errors=True)
os.environ["IADOPT_DB_PATH"] = str(_SESSION_DIR / "iadopt.sqlite3")
