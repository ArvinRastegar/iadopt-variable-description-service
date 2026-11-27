"""Keep every test away from the developer's own database and sign-in settings.

``Settings.auth_db_path`` defaults to ``backend/state/iadopt.sqlite3``, the local stack's real
database. Before any test module imports the application, point ``IADOPT_DB_PATH`` at a file in a
temporary directory for the whole session, so that code reaching the default path (a fresh
``Settings()``, a store built from settings, a subprocess that inherits the environment) touches
that file and never the real one. Tests still set their own database as before. Added at the
increment-1 tests gate (G-018), whose commands write to the database (migrations, backups,
``legacy purge-passwords``).

The sign-in, first-admin, request-guard and daily-limit settings are set for the whole session too,
empty, which counts as unset: the application never overrides a variable that is set, so nothing in a
developer's ``.env`` reaches a test, and no test reaches an identity provider but the fake one it
starts itself. A test that needs a first admin sets ``increment1_support.BOOTSTRAP`` itself, as CI
has no ``.env``.

The best pipeline's extra requests (the examples' embedding, the matrix finder, two more fill answers) are switched
off in every test module but ``test_best_pipeline.py``, which tests them (``retrieval_support.py``): the modules
written before it script one decomposition answer at a time.
"""

from __future__ import annotations

import atexit
import os
import pathlib
import shutil
import tempfile
from typing import Iterator

import pytest

_SESSION_DIR = pathlib.Path(tempfile.mkdtemp(prefix="iadopt-tests-"))
atexit.register(shutil.rmtree, _SESSION_DIR, ignore_errors=True)
os.environ["IADOPT_DB_PATH"] = str(_SESSION_DIR / "iadopt.sqlite3")
for _name in (
    "IADOPT_ENVIRONMENT", "IADOPT_AUTH_MODE", "IADOPT_OIDC_ISSUER", "IADOPT_OIDC_CLIENT_ID", "IADOPT_PUBLIC_URL",
    "IADOPT_OIDC_POST_LOGOUT", "IADOPT_TRUSTED_PROXY_HOPS", "IADOPT_SIGN_IN_LIMIT", "IADOPT_DAILY_DECOMPOSITIONS",
    "IADOPT_BOOTSTRAP_ADMIN_USERNAME", "IADOPT_BOOTSTRAP_ADMIN_PASSWORD", "IADOPT_BOOTSTRAP_ADMIN_DISPLAY_NAME",
    "IADOPT_BOOTSTRAP_ADMIN_EMAIL",
):
    os.environ[_name] = ""


@pytest.fixture(autouse=True)
def _best_pipeline_off(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Today's prompt and one fill answer, without the best pipeline's extra requests, outside its own tests."""
    from retrieval_support import BEST_PIPELINE_TESTS, switch_best_pipeline_off

    if request.module.__name__.rsplit(".", 1)[-1] != BEST_PIPELINE_TESTS:
        switch_best_pipeline_off(monkeypatch)
    yield
