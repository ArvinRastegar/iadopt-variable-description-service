"""Shared ``requests`` session used by all outbound HTTP (ORCID, vocabularies, PSNC).

A single pooled session with a descriptive User-Agent is created lazily and reused, so
connection pooling and headers are consistent across services. Callers must go
through :func:`get_http_session` rather than holding their own session.

The User-Agent names the client and gives the project's public URL, as Wikimedia's
User-Agent policy asks. It matters in practice: measured from a production-shaped
container on 2026-10-17, Wikidata throttled the earlier generic agent after about ten
requests in five seconds (HTTP 429, ``Retry-After`` growing to 56 s) and did not throttle
a descriptive one in thirty requests over fourteen seconds (``decisions.md`` D-032).
"""

from __future__ import annotations

from typing import Optional

import requests

USER_AGENT = (
    "IADOPT-Linker/1.0 (https://github.com/ArvinRastegar/iadopt-variable-description-service; "
    "I-ADOPT research tool)"
)

_http_session: Optional[requests.Session] = None


def get_http_session() -> requests.Session:
    """Return the process-wide pooled HTTP session, creating it on first use.

    Returns:
        A ``requests.Session`` with the descriptive :data:`USER_AGENT` header set.
    """
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        _http_session.headers.update({"User-Agent": USER_AGENT})
    return _http_session
