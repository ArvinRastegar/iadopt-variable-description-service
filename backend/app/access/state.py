"""The adapter that builds a request's access state from the database (access-policy, section 2).

Skeleton of the increment-1 tests gate. :func:`access_state` is used as a FastAPI dependency, so
tests replace it through ``app.dependency_overrides``. The state is built on every request, with
D-045's interim values until the modules that own an input exist.
"""

from __future__ import annotations

from fastapi import Request

from .policy import AccessState


def access_state(request: Request) -> AccessState:
    """Build the access state of a request from its session and the database; nothing is cached.

    Args:
        request: The incoming request.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError("access-policy is specified (components/access-policy.md) but not implemented yet.")
