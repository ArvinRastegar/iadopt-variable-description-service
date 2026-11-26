"""Admitting one decomposition under the daily limit (access-policy, section 5; D-021).

The route's own check (``require``) is quick and may be out of date when the run begins. :func:`admit`
decides again from what the database holds and reserves the use in the same immediate transaction, so
requests at the same moment never pass the limit together. While the limit is switched off it touches
nothing. How the run ended decides whether the use is kept or given back; an end that the database does
not take at once is tried again until it does.
"""

from __future__ import annotations

import logging
import pathlib
import threading
from typing import Dict, Optional, Set, Tuple

from .. import storage, usage
from ..account_model import find_account
from ..core.dependencies import auth_store
from .dependencies import refusal_of
from .policy import AccessState, Capability, decide
from .state import account_state

logger = logging.getLogger(__name__)

RETRY_SECONDS = 10.0
"""How long an end that could not be stored waits for the next try."""

_waiting: Dict[Tuple[pathlib.Path, int], usage.Outcome] = {}
"""The ends that are not stored yet, by database and use."""

_retried: Set[pathlib.Path] = set()
"""The databases that a timer tries again."""

_waiting_lock = threading.Lock()


class Use:
    """One admitted decomposition; only an account under the daily limit has a use to finish."""

    def __init__(self, stored: Optional[Tuple[pathlib.Path, int]] = None) -> None:
        """Remember the stored use, if there is one.

        Args:
            stored: The database and the id of the stored use; ``None`` when nothing is counted.
        """
        self._stored = stored

    def finish(self, outcome: usage.Outcome) -> None:
        """Say how the decomposition ended; only the first call counts, and it never fails the decomposition.

        Args:
            outcome: How it ended.
        """
        stored, self._stored = self._stored, None
        if stored is None:
            return
        with _waiting_lock:
            _waiting[stored] = outcome
        store_ends(stored[0])


NOT_COUNTED = Use()
"""A decomposition that the daily limit does not hold for."""


def store_ends(path: pathlib.Path) -> None:
    """Store the ends that wait for it; when the database does not take them now, try again in a while.

    Until then such a use counts. It is lost, as if its run had been interrupted, only when the server
    is killed before.

    Args:
        path: The database.
    """
    with _waiting_lock:
        ends = {use_id: outcome for (database, use_id), outcome in _waiting.items() if database == path}
    if not ends:
        return
    try:
        with storage.transaction(path=path) as conn:
            for use_id, outcome in ends.items():
                usage.finish_use(conn, use_id, outcome)
    except Exception:
        logger.exception("Could not store how a decomposition under the daily limit ended; it is tried again.")
        _try_again(path)
        return
    with _waiting_lock:
        for use_id in ends:
            _waiting.pop((path, use_id), None)


def _try_again(path: pathlib.Path) -> None:
    """Let one timer try a database again."""

    def retry() -> None:
        with _waiting_lock:
            _retried.discard(path)
        store_ends(path)

    with _waiting_lock:
        if path in _retried:
            return
        _retried.add(path)
    timer = threading.Timer(RETRY_SECONDS, retry)
    timer.daemon = True
    try:
        timer.start()
    except RuntimeError:  # no thread is to be had; the next end tries again
        with _waiting_lock:
            _retried.discard(path)


def admit(account_id: int) -> Use:
    """Decide whether an account may decompose now, and reserve its use when the daily limit holds for it.

    An account that may not, such as after its last use of the day, gets the refusal of
    :func:`~app.access.dependencies.refusal_of`, an ``AccessRefused``, and nothing is stored.

    Args:
        account_id: The signed-in account.

    Returns:
        What to finish when the run ends.
    """
    limit = usage.daily_limit()
    if limit is None or not auth_store.enabled:
        return NOT_COUNTED
    with storage.transaction(immediate=True, path=auth_store.db_path) as conn:
        # The clock after the lock: whoever waited for it past midnight has a new day.
        moment = usage.now()
        account = find_account(conn, account_id)
        today = usage.allowance(conn, account_id, limit, moment)
        state = account_state(account, today) if account else AccessState(account_id=None)
        decision = decide(state)[Capability.DECOMPOSE]
        if not decision.allowed:
            refused = refusal_of(decision)
            raise refused
        if decision.uses_left_today is None:
            return NOT_COUNTED
        return Use((auth_store.db_path, usage.add_use(conn, account_id, today.day, moment)))


def release_interrupted() -> None:
    """At the start, give back the uses of the runs that the previous process did not end.

    One process serves the application (D-014), so at its start none of its runs is under way. Should an
    old process still be finishing one, the end it stores replaces the interruption.
    """
    if usage.daily_limit() is None:
        return
    with storage.transaction(path=auth_store.db_path) as conn:
        released = usage.release_running(conn)
    if released:
        logger.info("Gave back %s decompositions that a restart interrupted.", released)
