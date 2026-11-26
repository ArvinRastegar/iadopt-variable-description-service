"""Keep every test away from the developer's own database and sign-in settings.

``Settings.auth_db_path`` defaults to ``backend/state/iadopt.sqlite3``, the local stack's real
database. Before any test module imports the application, point ``IADOPT_DB_PATH`` at a file in a
temporary directory for the whole session, so that code reaching the default path (a fresh
``Settings()``, a store built from settings, a subprocess that inherits the environment) touches
that file and never the real one. Tests still set their own database as before. Added at the
increment-1 tests gate (G-018), whose commands write to the database (migrations, backups,
``legacy purge-passwords``).

The sign-in, request-guard and daily-limit settings are set for the whole session too, empty, which
counts as unset: the application never overrides a variable that is set, so nothing in a developer's
``.env`` reaches a test, and no test reaches an identity provider but the fake one it starts itself.
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
for _name in (
    "IADOPT_ENVIRONMENT", "IADOPT_AUTH_MODE", "IADOPT_OIDC_ISSUER", "IADOPT_OIDC_CLIENT_ID", "IADOPT_PUBLIC_URL",
    "IADOPT_OIDC_POST_LOGOUT", "IADOPT_TRUSTED_PROXY_HOPS", "IADOPT_SIGN_IN_LIMIT", "IADOPT_DAILY_DECOMPOSITIONS",
):
    os.environ[_name] = ""
