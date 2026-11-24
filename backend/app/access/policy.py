"""The access decision: one pure function from an access state to a decision per capability.

Contract: ``components/access-policy.md`` in the private workflow repository, sections 2 to 6,
approved under gate G-017. This module imports only the standard library and reads no clock,
database, settings or network. This is the skeleton of the increment-1 tests gate: the names and
fields the behavioural tests in ``tests/test_access_policy.py`` use; :func:`decide` raises
``NotImplementedError`` until the implementation gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Optional


class Capability(str, Enum):
    """The capabilities of section 3; each value is its key in ``GET /api/auth/me``."""

    PUBLIC_TOOLS = "public_tools"
    SESSION = "session"
    ACCOUNT_DATA = "account_data"
    DECOMPOSE = "decompose"
    """``use_feature(decompose)``: one logical decomposition."""
    SAVE_AND_REVIEW = "save_and_review"
    PUBLISH = "publish"
    RETRACT_OWN = "retract_own"
    REVIEW_APPLICATIONS = "review_applications"
    MANAGE_USERS = "manage_users"
    VIEW_ACTIVITY = "view_activity"
    RETRACT_ANY = "retract_any"
    EXPORT_RESEARCH = "export_research"
    MANAGE_ADMINS = "manage_admins"


@dataclass(frozen=True)
class AccessState:
    """The inputs of section 2.

    Attributes:
        account_id: The signed-in account's id, or ``None`` for an anonymous visitor. The
            development user of a deployment with authentication disabled has id 0.
        account_backed: False only for that development user.
        role: ``user``, ``admin`` or ``owner``.
        blocked: Whether the account is blocked.
        email_verified: True, False, or ``None`` when unknown (an email an admin entered).
        terms_accepted: Whether the current terms version is accepted.
        application: ``none``, ``pending``, ``approved``, ``rejected`` or ``withdrawn``.
        uses_left_today: Decompositions left today, at least 0.
        resets_at: When the daily count resets, or ``None`` when unknown.
    """

    account_id: Optional[int]
    account_backed: bool = True
    role: str = "user"
    blocked: bool = False
    email_verified: Optional[bool] = None
    terms_accepted: bool = False
    application: str = "none"
    uses_left_today: int = 0
    resets_at: Optional[datetime] = None


@dataclass(frozen=True)
class Decision:
    """One capability's decision; a refusal carries its reason code and message (section 6).

    Attributes:
        allowed: Whether the capability is granted.
        reason: The reason code of a refusal, else ``None``.
        message: The message of a refusal, else ``None``.
        uses_left_today: For ``decompose`` under a daily cap, the uses left; ``None`` uncapped.
        resets_at: For ``decompose`` under a daily cap, when the count resets; ``None`` uncapped.
    """

    allowed: bool
    reason: Optional[str] = None
    message: Optional[str] = None
    uses_left_today: Optional[int] = None
    resets_at: Optional[datetime] = None


def decide(state: AccessState) -> Dict[Capability, Decision]:
    """Decide every capability for an access state by the first matching rule of section 4.

    Args:
        state: The request's access state.

    Raises:
        NotImplementedError: Until the implementation gate.
    """
    raise NotImplementedError("access-policy is specified (components/access-policy.md) but not implemented yet.")
