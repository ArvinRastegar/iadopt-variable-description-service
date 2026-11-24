"""The adapter that builds a request's access state from the database (access-policy, section 2).

:func:`access_state` is used as a FastAPI dependency, so tests replace it through
``app.dependency_overrides``. The state is built on every request, with D-045's interim values
until the modules that own an input exist.
"""

from __future__ import annotations

from fastapi import Request

from ..core.dependencies import auth_store
from .policy import AccessState


def access_state(request: Request) -> AccessState:
    """Build the access state of a request from its session and the database; nothing is cached.

    Until ``onboarding`` and ``usage-ledger`` exist, a legacy local account (a usable password and
    no identity-provider identity) counts as approved with the current terms, and any other account
    takes the most restrictive value of their inputs.

    Args:
        request: The incoming request.
    """
    user = auth_store.user_from_request(request)
    if user is None:
        return AccessState(account_id=None)
    if not auth_store.enabled:
        return AccessState(
            account_id=user["id"],
            account_backed=False,
            role=user["role"],
            email_verified=True,
            terms_accepted=True,
            application="approved",
        )
    legacy = user["local"] and not user["has_identity"]
    return AccessState(
        account_id=user["id"],
        role=user["role"],
        blocked=not user["is_active"],
        email_verified=user["email_verified"],
        terms_accepted=legacy,
        application="approved" if legacy else "none",
    )
