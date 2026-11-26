"""The access decision: one pure function from an access state to a decision per capability.

Contract: ``components/access-policy.md`` in the private workflow repository, sections 2 to 6,
approved under gate G-017. This module imports only the standard library and reads no clock,
database, settings or network: equal states always give equal decisions.
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


OWN_USE = (Capability.DECOMPOSE, Capability.SAVE_AND_REVIEW, Capability.PUBLISH, Capability.RETRACT_OWN)
"""What the current terms and the application decide."""

ADMINISTRATION = (
    Capability.REVIEW_APPLICATIONS,
    Capability.MANAGE_USERS,
    Capability.VIEW_ACTIVITY,
    Capability.RETRACT_ANY,
    Capability.EXPORT_RESEARCH,
)
"""What the admin role grants; ``MANAGE_ADMINS`` is the owner's alone."""

MESSAGES: Dict[str, str] = {
    "sign_in_required": "Authentication required.",
    "blocked": "This account is blocked.",
    "email_not_verified": "Verify your email address to use this account.",
    "terms_not_accepted": "Accept the current terms to continue.",
    "password_ended": "Password sign-in has ended for this account. Ask an admin how to continue.",
    "admin_required": "Admin access required.",
    "owner_required": "Only the owner can do this.",
    "daily_limit": "No decompositions left today.",
}
"""The message of each reason that has one message whatever the capability (section 6)."""

_APPROVAL_REQUIRED: Dict[Capability, str] = {
    Capability.PUBLISH: "Publishing needs an approved account.",
    Capability.RETRACT_OWN: "Ask an admin to retract this publication.",
}

_APPROVAL_FIRST = "An admin must approve your account first."
"""What an account reads whose whole own use waits for an approval; only an admin can retract for it meanwhile."""

_RECORDS_DISABLED = "Records need accounts, and authentication is disabled."
_ACCOUNTS_DISABLED: Dict[Capability, str] = {
    Capability.ACCOUNT_DATA: "Saved ORCID iDs need accounts, and authentication is disabled.",
    Capability.SAVE_AND_REVIEW: _RECORDS_DISABLED,
    Capability.EXPORT_RESEARCH: _RECORDS_DISABLED,
    Capability.MANAGE_ADMINS: "The owner needs accounts, and authentication is disabled.",
}


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
        uses_left_today: Decompositions left today, at least 0; ``None`` when no daily cap applies.
        resets_at: When the daily count resets, or ``None`` when unknown.
        approval_first: Whether an account may use the tools only once it is approved, instead of
            decomposing under a daily cap and saving its work before.
        password_ended: Whether nobody can sign in to the account any more, and so nobody can approve
            it: its password was purged and no identity leads to it. A session may still be open.
    """

    account_id: Optional[int]
    account_backed: bool = True
    role: str = "user"
    blocked: bool = False
    email_verified: Optional[bool] = None
    terms_accepted: bool = False
    application: str = "none"
    uses_left_today: Optional[int] = 0
    resets_at: Optional[datetime] = None
    approval_first: bool = False
    password_ended: bool = False


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


ALLOWED = Decision(allowed=True)


def decide(state: AccessState) -> Dict[Capability, Decision]:
    """Decide every capability for an access state by the first matching rule of section 4.

    Args:
        state: The request's access state.
    """
    if state.account_id is None:
        return _only_public_tools("sign_in_required")
    if state.blocked:
        return _only_public_tools("blocked")
    if state.email_verified is False:
        return _only_public_tools("email_not_verified")
    if not state.account_backed:
        return _development_user()
    return {
        Capability.PUBLIC_TOOLS: ALLOWED,
        Capability.SESSION: ALLOWED,
        Capability.ACCOUNT_DATA: ALLOWED,
        **_own_use(state),
        **_administration(state.role),
    }


def _refused(reason: str, message: Optional[str] = None) -> Decision:
    """A refusal with its reason's message, or with a message of its own."""
    return Decision(allowed=False, reason=reason, message=message or MESSAGES[reason])


def _only_public_tools(reason: str) -> Dict[Capability, Decision]:
    """A gate: everything but the public tools is refused with the gate's reason."""
    decisions = {capability: _refused(reason) for capability in Capability}
    decisions[Capability.PUBLIC_TOOLS] = ALLOWED
    return decisions


def _own_use(state: AccessState) -> Dict[Capability, Decision]:
    """The account's own use of the tools, by its terms and application."""
    if not state.terms_accepted:
        return {capability: _refused("terms_not_accepted") for capability in OWN_USE}
    if state.application == "approved" or state.role in ("admin", "owner"):
        return {capability: ALLOWED for capability in OWN_USE}
    if state.password_ended:
        return {capability: _refused("password_ended") for capability in OWN_USE}
    retract_own = _refused("approval_required", _APPROVAL_REQUIRED[Capability.RETRACT_OWN])
    if state.approval_first:
        waiting = {capability: _refused("approval_required", _APPROVAL_FIRST) for capability in OWN_USE}
        return {**waiting, Capability.RETRACT_OWN: retract_own}
    return {
        Capability.DECOMPOSE: _decompose(state),
        Capability.SAVE_AND_REVIEW: ALLOWED,
        Capability.PUBLISH: _refused("approval_required", _APPROVAL_REQUIRED[Capability.PUBLISH]),
        Capability.RETRACT_OWN: retract_own,
    }


def _decompose(state: AccessState) -> Decision:
    """Decomposing for an account that is not approved: by its uses left today, where a daily cap applies."""
    if state.uses_left_today is None:
        return ALLOWED
    has_uses = state.uses_left_today > 0
    return Decision(
        allowed=has_uses,
        reason=None if has_uses else "daily_limit",
        message=None if has_uses else MESSAGES["daily_limit"],
        uses_left_today=state.uses_left_today,
        resets_at=state.resets_at,
    )


def _administration(role: str) -> Dict[Capability, Decision]:
    """The admin capabilities, by role; the current terms do not matter here (D-018)."""
    admin = ALLOWED if role in ("admin", "owner") else _refused("admin_required")
    return {
        **{capability: admin for capability in ADMINISTRATION},
        Capability.MANAGE_ADMINS: ALLOWED if role == "owner" else _refused("owner_required"),
    }


def _development_user() -> Dict[Capability, Decision]:
    """The user of a deployment with authentication disabled: an approved admin without account features."""
    decisions = {capability: ALLOWED for capability in Capability}
    for capability, message in _ACCOUNTS_DISABLED.items():
        decisions[capability] = _refused("accounts_disabled", message)
    return decisions
