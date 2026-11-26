"""The daily limit on decompositions: its switch, the day it counts by, and the uses it counts (D-021, D-033).

A slice of ``usage-ledger``. One setting, ``IADOPT_DAILY_DECOMPOSITIONS``, switches the limit on; while it
is empty nothing is counted or stored, and an account that is not approved may not decompose at all. Whom
the limit holds for is the access policy's decision: ``app.access.admission`` asks for it and reserves a
use in one transaction, before a model is called. The functions that read or write uses take the
connection of their caller's transaction.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import config

# ------------------------------------------------------------------------------------------------
# The day
# ------------------------------------------------------------------------------------------------

TIME_ZONE = "Europe/Berlin"


def now() -> datetime:
    """Return the server's clock; tests replace it."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Day:
    """A calendar day in Europe/Berlin, which has 23 or 25 hours when the clocks change.

    Attributes:
        date: Its date as ISO-8601 writes it.
        end: When the next day begins.
    """

    date: str
    end: datetime


def day_of(moment: datetime) -> Day:
    """Return the day in Europe/Berlin that a moment belongs to.

    Args:
        moment: A time with its zone.
    """
    zone = ZoneInfo(TIME_ZONE)
    date = moment.astimezone(zone).date()
    return Day(date.isoformat(), datetime.combine(date + timedelta(days=1), time.min, tzinfo=zone))


# ------------------------------------------------------------------------------------------------
# The switch
# ------------------------------------------------------------------------------------------------

MAX_DAILY_LIMIT = 1000

_LIMIT_RULE = (
    "IADOPT_DAILY_DECOMPOSITIONS must be empty, so that only approved accounts decompose, or a whole "
    f"number from 1 to {MAX_DAILY_LIMIT}."
)
_ZONE_RULE = "IADOPT_DAILY_DECOMPOSITIONS needs the time zone {}, which this system does not know."


def daily_limit() -> Optional[int]:
    """Return how many decompositions an account under the limit has in a day; ``None`` when it is switched off.

    Raises:
        ValueError: If the setting is neither empty nor a whole number from 1 to the maximum.
    """
    text = config.settings.daily_decompositions_raw
    if not text:
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,3}", text) or int(text) > MAX_DAILY_LIMIT:
        raise ValueError(_LIMIT_RULE)
    return int(text)


def usage_problems() -> List[str]:
    """Return one sentence for each setting of this module that stops the start."""
    try:
        limit = daily_limit()
    except ValueError as e:
        return [str(e)]
    if limit is not None:
        try:
            ZoneInfo(TIME_ZONE)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return [_ZONE_RULE.format(TIME_ZONE)]
    return []


# ------------------------------------------------------------------------------------------------
# The uses
# ------------------------------------------------------------------------------------------------


class Outcome(Enum):
    """How a counted decomposition ended, and whether its use is given back (D-021).

    Attributes:
        label: The class the use keeps.
        released: Whether the use is given back.
    """

    SUCCESS = ("success", False)
    FAILED = ("failed", False)
    """The model, the way to it or its output failed."""
    CLIENT_LEFT = ("client_left", False)
    """The browser went away before the end."""
    NOT_SENT = ("not_sent", True)
    """No request to a model went out: the service is not set up for it, or failed before."""
    REFUSED = ("refused", True)
    """The provider refused every request: the service's credentials or configuration, or the input."""
    INTERRUPTED = ("interrupted", True)
    """The server stopped in the middle, as the next start supposes."""

    def __init__(self, label: str, released: bool) -> None:
        """Name the class and say whether it gives the use back."""
        self.label = label
        self.released = released


@dataclass(frozen=True)
class Allowance:
    """What an account has left of its day.

    Attributes:
        left: The uses left, at least 0.
        day: The day.
    """

    left: int
    day: Day


def allowance(conn: sqlite3.Connection, user_id: int, limit: int, moment: datetime) -> Allowance:
    """Return what an account has left; a decomposition that is still running counts.

    Args:
        conn: An open connection.
        user_id: The account.
        limit: How many uses a day has.
        moment: Now, with its zone.
    """
    day = day_of(moment)
    row = conn.execute(
        "SELECT COUNT(*) FROM decomposition_uses WHERE user_id = ? AND berlin_date = ? AND released = 0",
        (user_id, day.date),
    ).fetchone()
    return Allowance(max(0, limit - int(row[0])), day)


def add_use(conn: sqlite3.Connection, user_id: int, day: Day, moment: datetime) -> int:
    """Store a use that is about to run.

    Args:
        conn: The connection of the immediate transaction that found a use left.
        user_id: The account.
        day: The day it counts for.
        moment: Now, with its zone.

    Returns:
        The use's id.
    """
    cursor = conn.execute(
        "INSERT INTO decomposition_uses (user_id, berlin_date, reserved_at) VALUES (?, ?, ?)",
        (user_id, day.date, moment.astimezone(timezone.utc).isoformat()),
    )
    return int(cursor.lastrowid or 0)


def finish_use(conn: sqlite3.Connection, use_id: int, outcome: Outcome) -> None:
    """Store how a use ended; one that has ended already stays as it is.

    Only an interruption is not final: while two processes overlap, the new one's start supposes it of a
    run that then ends in the old one.

    Args:
        conn: A transaction's connection.
        use_id: The use.
        outcome: How it ended.
    """
    conn.execute(
        "UPDATE decomposition_uses SET outcome = ?, released = ? WHERE id = ? AND (outcome IS NULL OR outcome = ?)",
        (outcome.label, int(outcome.released), use_id, Outcome.INTERRUPTED.label),
    )


def release_running(conn: sqlite3.Connection) -> int:
    """Give back every use that is still running, supposing that its run ended with the process that began it.

    Args:
        conn: A transaction's connection.

    Returns:
        How many uses were given back.
    """
    cursor = conn.execute(
        "UPDATE decomposition_uses SET outcome = ?, released = ? WHERE outcome IS NULL",
        (Outcome.INTERRUPTED.label, int(Outcome.INTERRUPTED.released)),
    )
    return cursor.rowcount
