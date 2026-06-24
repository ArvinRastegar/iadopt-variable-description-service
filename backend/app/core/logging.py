"""Structured logging setup for the backend.

The codebase historically used bare ``print()`` for diagnostics. This module
centralizes a minimal, dependency-free logging configuration that routes through
the stdlib ``logging`` module with a consistent format, so future code can use
``logging.getLogger(__name__)`` instead of ``print``.

``configure_logging`` is idempotent and called once from the app factory.
"""

from __future__ import annotations

import logging
import os

_configured = False


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once with a consistent format.

    Args:
        level: Optional level name override (e.g. ``"DEBUG"``); defaults to the
            ``IADOPT_LOG_LEVEL`` env var, then ``"INFO"``.
    """
    global _configured
    if _configured:
        return

    resolved = (level or os.getenv("IADOPT_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, configuring logging on first use.

    Args:
        name: The logger name (typically ``__name__``).

    Returns:
        A configured ``logging.Logger``.
    """
    configure_logging()
    return logging.getLogger(name)
