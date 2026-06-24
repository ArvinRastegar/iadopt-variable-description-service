"""Dependency-free text normalization helpers shared across services.

These three helpers are the most cross-cutting in the codebase: they are used by
the ORCID, nanopub, RDF/TTL, and validation services. Keeping them in this leaf
module (which imports nothing from the app) is what prevents those services from
forming an import cycle — see docs/CONTRACTS.md and the Phase-2 dependency map.

Behavior is identical to the original ``app.main`` definitions; they were moved
verbatim, not rewritten.
"""

from __future__ import annotations

import json
import re
from typing import Optional


def ttl_quote(text: Optional[str]) -> str:
    """Escape arbitrary text once so labels/comments/definitions stay valid Turtle.

    Args:
        text: Raw text to embed as a Turtle literal (``None`` treated as empty).

    Returns:
        A JSON-quoted string usable directly as a Turtle literal.
    """
    return json.dumps((text or "").strip(), ensure_ascii=False)


def normalize_text(text: Optional[str]) -> str:
    """Collapse repeated whitespace so generated labels read naturally.

    Args:
        text: Raw text (``None`` treated as empty).

    Returns:
        The trimmed text with internal whitespace runs collapsed to single spaces.
    """
    return re.sub(r"\s+", " ", (text or "").strip())


def lookup_key(text: Optional[str]) -> str:
    """Normalize a label so constraints can resolve targets by human-readable name.

    Args:
        text: Raw label text.

    Returns:
        The normalized, lower-cased lookup key.
    """
    return normalize_text(text).lower()
