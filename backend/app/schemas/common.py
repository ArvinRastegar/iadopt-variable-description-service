"""Shared field types, constraints, and normalization helpers for the schemas package.

This module is a leaf dependency: it imports nothing from the rest of the app, so
``schemas`` stays at the bottom of the Phase-2 layering (routers → services →
clients → core/schemas).

It defines the reusable ``Annotated`` types and the ORCID/URI normalization
functions that the brief calls for. The normalization helpers mirror the existing
behavior in ``app.main`` (``_normalize_orcid``, ``_orcid_suffix``) exactly so that
introducing them changes no output. They are intended for *internal/domain* models;
they are deliberately NOT attached as mutating validators to request models, because
that would rewrite caller-supplied values (e.g. ``creator_orcid_id``) before they are
echoed back into the audit log — a behavior change.
"""

from __future__ import annotations

import re
from typing import Annotated, Optional

from pydantic import Field

# --------------------------------------------------------------------------- #
# Reusable constrained field types
# --------------------------------------------------------------------------- #

NonEmptyStr = Annotated[str, Field(min_length=1)]
"""A string that must contain at least one character."""

Username = Annotated[str, Field(min_length=1, max_length=120)]
"""A username: 1–120 characters (matches AuthStore._normalize_username bounds)."""

Password = Annotated[str, Field(min_length=8)]
"""A password: at least 8 characters (matches AuthStore.create_user check)."""

ActionName = Annotated[str, Field(min_length=1, max_length=120)]
"""A frontend event action name: 1–120 characters."""


# --------------------------------------------------------------------------- #
# ORCID / URI normalization (pure functions, behavior-identical to app.main)
# --------------------------------------------------------------------------- #


def normalize_orcid(orcid_id: Optional[str]) -> Optional[str]:
    """Normalize an ORCID identifier into its canonical ``https://orcid.org/<id>`` URI.

    Mirrors ``app.main._normalize_orcid``: pass-through for values that already
    carry an ``http(s)://`` scheme, otherwise prefix the ORCID resolver host.

    Args:
        orcid_id: A bare ORCID (e.g. ``0000-0002-1825-0097``), a full ORCID URI,
            or ``None``/empty.

    Returns:
        The canonical ORCID URI, or ``None`` when the input is falsy.
    """
    if not orcid_id:
        return None
    if orcid_id.startswith("http://") or orcid_id.startswith("https://"):
        return orcid_id
    return f"https://orcid.org/{orcid_id}"


def orcid_suffix(orcid_id: Optional[str]) -> Optional[str]:
    """Extract the bare ORCID identifier from any ORCID form.

    Mirrors ``app.main._orcid_suffix``: normalize first, then take the final
    path segment so TTL prefix forms stay stable.

    Args:
        orcid_id: A bare ORCID, a full ORCID URI, or ``None``/empty.

    Returns:
        The bare identifier (e.g. ``0000-0002-1825-0097``), or ``None`` when the
        input is falsy.
    """
    normalized = normalize_orcid(orcid_id)
    if not normalized:
        return None
    return normalized.rstrip("/").rsplit("/", 1)[-1]


# ASCII digits only: ``\d`` would also accept other scripts' digits, which int() reads.
_ORCID_ID_PATTERN = re.compile(r"[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]")


def _orcid_check_digit(base_digits: str) -> str:
    """Return the ISO 7064 MOD 11-2 check digit ORCID computes over the first 15 digits."""
    total = 0
    for digit in base_digits:
        total = (total + int(digit)) * 2
    result = (12 - total % 11) % 11
    return "X" if result == 10 else str(result)


def canonical_orcid_uri(value: Optional[str]) -> str:
    """Validate an ORCID iD strictly and return its canonical ``https://orcid.org/`` URI.

    Unlike :func:`normalize_orcid`, which keeps the legacy pass-through for any URL, this
    accepts only a bare iD or an orcid.org URL whose check digit matches. It is the one
    check for saved iDs, used by the auth store and its routes alike.

    Args:
        value: A bare iD (``0000-0002-1825-0097``) or ``http(s)://orcid.org/<iD>``.

    Returns:
        The canonical URI, e.g. ``https://orcid.org/0000-0002-1825-0097``.

    Raises:
        ValueError: If the value is not a well-formed ORCID iD or its check digit is wrong.
    """
    suffix = re.sub(r"^https?://orcid\.org/", "", (value or "").strip(), flags=re.IGNORECASE).upper()
    if not _ORCID_ID_PATTERN.fullmatch(suffix):
        raise ValueError("Enter a valid ORCID iD such as 0000-0002-1825-0097.")
    digits = suffix.replace("-", "")
    if _orcid_check_digit(digits[:15]) != digits[15]:
        raise ValueError("The ORCID check digit is invalid.")
    return f"https://orcid.org/{suffix}"
