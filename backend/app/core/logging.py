"""Structured logging setup for the backend.

The codebase historically used bare ``print()`` for diagnostics. This module
centralizes a minimal logging configuration that routes through the stdlib
``logging`` module with a consistent format, so future code can use
``logging.getLogger(__name__)`` instead of ``print``. The model providers' keys are
taken out of what it writes (:class:`KeysFilter`).

``configure_logging`` is idempotent and called once from the app factory.
"""

from __future__ import annotations

import copy
import logging
import os

from .provider_keys import without_keys

_configured = False

WITHOUT_QUERY = ("/api/auth/login", "/api/auth/callback", "/api/auth/end-session")
"""The browser routes of the ROHub sign-in, whose query the access log must not hold."""


class SignInQueryFilter(logging.Filter):
    """Cut the query off uvicorn's access-log lines of the sign-in trip."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite one line's path; every line is kept.

        Args:
            record: The line: client, method, path with query, HTTP version, status.
        """
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            path, _, query = record.args[2].partition("?")
            if query and path.rstrip("/").endswith(WITHOUT_QUERY):  # a root path may come first
                record.args = (*record.args[:2], path, *record.args[3:])
        return True


class ForeignHeadersFilter(logging.Filter):
    """Drop the HTTP library's lines that quote a malformed answer: they hold whatever another server sent."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether a line may be written.

        Args:
            record: The line.
        """
        return not str(record.msg).startswith("Failed to parse headers")


class KeysFilter(logging.Filter):
    """Take the model providers' keys out of what a handler writes, the traceback included."""

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        """Return the line without them; every line is kept, and other handlers see it as it was.

        Args:
            record: The line.
        """
        cleaned = copy.copy(record)
        try:
            message = record.getMessage()
        except Exception:  # the handler would report the arguments as they are
            message = ""
            cleaned.msg, cleaned.args = f"A line of the log does not fit its arguments: {record.pathname}:{record.lineno}", None
        if (shown := without_keys(message)) != message:
            cleaned.msg, cleaned.args = shown, None
        if record.exc_info and not record.exc_text:
            cleaned.exc_text = logging.Formatter().formatException(record.exc_info)
        cleaned.exc_text = cleaned.exc_text and without_keys(cleaned.exc_text)
        cleaned.stack_info = record.stack_info and without_keys(record.stack_info)
        return cleaned


class AccessKeysFilter(logging.Filter):
    """Take the keys out of the server's access lines, whose formatter needs each argument in its place."""

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        """Return the line without them; every line is kept, and other handlers see it as it was.

        Args:
            record: The line: client, method, path with query, HTTP version, status.
        """
        cleaned = copy.copy(record)
        if isinstance(record.args, tuple):
            cleaned.args = tuple(without_keys(value) if isinstance(value, str) else value for value in record.args)
        return cleaned


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
    logging.getLogger("uvicorn.access").addFilter(SignInQueryFilter())
    logging.getLogger("urllib3.connection").addFilter(ForeignHeadersFilter())
    for name in (None, "uvicorn"):  # the server's own lines, errors among them, do not go through the root's handlers
        for handler in logging.getLogger(name).handlers:
            handler.addFilter(KeysFilter())
    for handler in logging.getLogger("uvicorn.access").handlers:
        handler.addFilter(AccessKeysFilter())
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
