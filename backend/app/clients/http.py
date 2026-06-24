"""Shared ``requests`` session used by all outbound HTTP (ORCID, Wikidata, PSNC).

A single pooled session with a stable User-Agent is created lazily and reused, so
connection pooling and headers are consistent across services. Callers must go
through :func:`get_http_session` rather than holding their own session.
"""

from __future__ import annotations

from typing import Optional

import requests

_http_session: Optional[requests.Session] = None


def get_http_session() -> requests.Session:
    """Return the process-wide pooled HTTP session, creating it on first use.

    Returns:
        A ``requests.Session`` with the IADOPT-Linker User-Agent header set.
    """
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        _http_session.headers.update({"User-Agent": "IADOPT-Linker/1.0 (+fastapi)"})
    return _http_session
