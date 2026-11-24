"""One capability per route: the FastAPI dependency and the route table (access-policy, section 5).

Skeleton of the increment-1 tests gate. :func:`require` returns a dependency that carries the
capability it checks as ``.capability`` and depends on :func:`app.access.state.access_state`;
a refusal becomes the status and body of section 6. :data:`ROUTE_CAPABILITIES` maps every
``(method, path)`` of the application to its capability, or to ``None`` for a public route.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Tuple

from .policy import Capability, Decision

ROUTE_CAPABILITIES: Dict[Tuple[str, str], Optional[Capability]] = {}
"""Section 5's table, keyed by ``(method, path)``. Empty in this skeleton."""


def require(capability: Capability) -> Callable[..., Decision]:
    """Return the FastAPI dependency that refuses a request without ``capability``.

    Args:
        capability: The capability the route needs.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError("access-policy is specified (components/access-policy.md) but not implemented yet.")
