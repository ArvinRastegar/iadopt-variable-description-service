"""Names for the best pipeline's requests to PSNC that bring nothing to use, each with a short hint.

Every failure of the embedding request and of the matrix finder is recorded in the run's call log as
``<cause>. <hint>.``: what happened, then what it most likely means. A provider's response body and the keys are never
part of it.

Depends only on ``requests``.
"""

from __future__ import annotations

from typing import Optional

import requests


def named(cause: str, hint: str) -> str:
    """Join a failure's cause and its hint: ``<cause>. <hint>``."""
    return f"{cause} {hint}" if cause.endswith(".") else f"{cause}. {hint}"


def status_hint(status: Optional[int]) -> str:
    """What an HTTP status from PSNC most likely means, in a few words."""
    if status in (401, 403):
        return "The PSNC key was refused."
    if status == 429:
        return "PSNC's limit or budget was reached."
    if status is not None and 500 <= status < 600:
        return "PSNC had a server error."
    return "PSNC refused the request."


def refused(what: str, status: Optional[int]) -> str:
    """A request PSNC answered with a status other than 200: ``<what> refused: HTTP <status>.`` and its hint."""
    return f"{what} refused: HTTP {status}. {status_hint(status)}"


def request_failure(what: str, error: BaseException, seconds: float, slow: str) -> str:
    """Name a request to PSNC that brought no answer.

    Args:
        what: The request, such as ``embedding request`` or ``matrix finder``.
        error: What sending it raised.
        seconds: Its timeout.
        slow: The hint for a timeout.

    Returns:
        ``<what> timed out after <seconds> s.``, ``<what> refused: HTTP <status>.`` or ``<what> failed: <error
        name>.``, each with its hint.
    """
    if isinstance(error, requests.Timeout):
        return f"{what} timed out after {seconds:g} s. {slow}"
    if isinstance(error, requests.HTTPError):
        return refused(what, getattr(getattr(error, "response", None), "status_code", None))
    if isinstance(error, requests.RequestException):
        return f"{what} failed: {type(error).__name__}. No connection to PSNC."
    return f"{what} failed: {type(error).__name__}. The backend could not send it."
