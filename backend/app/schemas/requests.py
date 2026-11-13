"""Request body models for the I-ADOPT API.

These are moved verbatim (field names, types, defaults, and validation bounds)
from the inline definitions in ``app.main`` so that request parsing and the
generated OpenAPI request schemas are unchanged. The ``DecomposeRequest`` default
for ``model_provider`` is injected at import time from the runtime configuration,
exactly as before.

No normalizing validators are attached to the original requests. The audit log no
longer echoes every request value: whole Turtle, definitions and results are left
out, because the decomposition record keeps them (D-024, D-036). The record requests
check that their fields fit the step type, and browser-reported device and screen
data that does not fit its bounds is dropped instead of failing the request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, List, Literal, Optional

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
    model_validator,
)

from .common import ActionName, NonEmptyStr, Password, Username
from ..core.config import settings

MAX_RECORD_TURTLE_CHARS = 500_000
"""Longest Turtle a record step may carry."""

MAX_RECORD_NOTE_CHARS = 500
"""Longest note on a "Mark as best"."""

RecordId = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$", description="A record id: 16 lowercase hex characters.")]
"""The public id of a decomposition record."""

RecordEventType = Literal["edit", "metadata", "check", "uncheck", "mark_best"]
"""The steps a browser can add to a record."""

RecordOperation = Literal[
    "add",
    "remove",
    "relabel",
    "relink",
    "change_shape",
    "add_constraint",
    "move_constraint",
    "confirm",
    "unconfirm",
    "other",
]
"""The kinds of change a record step can list."""

RecordRole = Literal[
    "",
    "variable",
    "hasProperty",
    "hasObjectOfInterest",
    "hasMatrix",
    "hasContextObject",
    "hasStatisticalModifier",
    "hasConstraint",
]
"""The I-ADOPT role of the component a change touches; empty when it has none."""


class ClientScreen(BaseModel):
    """The screen the browser reports (``window.screen``).

    Attributes:
        width: Screen width in CSS pixels.
        height: Screen height in CSS pixels.
        pixel_ratio: Device pixels per CSS pixel.
    """

    width: Optional[int] = Field(default=None, ge=0, le=100_000)
    height: Optional[int] = Field(default=None, ge=0, le=100_000)
    pixel_ratio: Optional[float] = Field(default=None, ge=0, le=100)


class ClientDevice(BaseModel):
    """The device the browser reports (``navigator.userAgentData`` and touch support).

    Attributes:
        platform: Operating system name, such as ``macOS`` or ``Android``.
        mobile: Whether the browser says it runs on a phone, when it says so.
        touch_points: How many touch points the screen supports (0 without touch).
    """

    platform: str = Field(default="", max_length=100)
    mobile: Optional[bool] = None
    touch_points: Optional[int] = Field(default=None, ge=0, le=1_000)


class ClientInfo(BaseModel):
    """Device and screen of the browser behind a recorded step; the server adds IP and browser.

    Attributes:
        screen: The reported screen, if any.
        device: The reported device, if any.
    """

    screen: Optional[ClientScreen] = None
    device: Optional[ClientDevice] = None


def _drop_invalid_client(value: Any, handler: ValidatorFunctionWrapHandler) -> Optional[ClientInfo]:
    """Validate browser-reported client info, dropping it instead of failing the request."""
    try:
        client: Optional[ClientInfo] = handler(value)
    except ValidationError:
        return None
    return client


LenientClientInfo = Annotated[Optional[ClientInfo], WrapValidator(_drop_invalid_client)]
"""Optional client info that is dropped, not refused, when it does not fit its bounds."""


class DecomposeRequest(BaseModel):
    """Input for the decomposition endpoints (``/api/decompose`` and ``/stream``).

    Attributes:
        definition: Variable definition in plain text (required, non-empty).
        model_name: One of the backend-configured model names; ``None`` uses the
            provider default.
        model_provider: Model provider to use; defaults to the configured default.
        disable_thinking: When true, request the model without reasoning effort.
        client: Device and screen for the decomposition record (D-036), if the browser sends them.
    """

    definition: str = Field(..., min_length=1, description="Variable definition in plain text")
    model_name: Optional[str] = Field(default=None, description="One of the backend-configured model names to use.")
    model_provider: str = Field(
        default=settings.default_model_provider,
        description=(
            "Model provider to use for decomposition. Enabled values: "
            f"{', '.join(settings.enabled_model_providers)}."
        ),
    )
    disable_thinking: bool = Field(
        default=True,
        description="When true, request the model without reasoning effort by sending `reasoning.effort = none`.",
    )
    client: LenientClientInfo = Field(
        default=None,
        description="Device and screen of the browser, kept with the decomposition record of a signed-in account.",
    )


class PublishNanopubRequest(BaseModel):
    """Input for ``POST /api/nanopub/publish``.

    Attributes:
        ttl: TTL assertion payload currently shown in the frontend (required).
        creator_orcid_id: The publishing person's ORCID (required; no server default).
        record_id: The decomposition record the TTL is a version of. Publishing needs it,
            and the TTL must be the version most recently marked as best, plus at most
            nanopublication metadata (D-036).
        client: Device and screen of the browser, kept with the publication in its record.
    """

    ttl: str = Field(..., min_length=1, description="TTL assertion payload currently shown in the frontend")
    creator_orcid_id: NonEmptyStr = Field(
        ..., description="ORCID of the person publishing (bare id or https://orcid.org/ URI); required."
    )
    record_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="The record whose version is published; that version must be marked as best.",
    )
    client: LenientClientInfo = Field(
        default=None, description="Device and screen of the browser, kept with the publication in its record."
    )


class RetractNanopubRequest(BaseModel):
    """Input for ``POST /api/nanopub/retract``.

    Attributes:
        nanopub_uri: The published nanopub URI or Nanodash explore URL to retract.
        creator_orcid_id: The retracting person's ORCID (required; no server default).
    """

    nanopub_uri: str = Field(
        ..., min_length=1, description="The published nanopub URI or Nanodash explore URL to retract"
    )
    creator_orcid_id: NonEmptyStr = Field(
        ..., description="ORCID of the person retracting (bare id or https://orcid.org/ URI); required."
    )


class LoginRequest(BaseModel):
    """Credentials for ``POST /api/auth/login``."""

    username: NonEmptyStr
    password: NonEmptyStr


class SaveOrcidRequest(BaseModel):
    """Input for ``POST /api/auth/me/orcids``.

    Attributes:
        orcid: The ORCID iD to save (bare id or https://orcid.org/ URI); the route checks
            its form and check digit.
    """

    orcid: NonEmptyStr = Field(..., description="ORCID iD to save (bare id or https://orcid.org/ URI)")


class FrontendEventRequest(BaseModel):
    """A frontend analytics/audit event for ``POST /api/events``.

    Attributes:
        action: Short action name (recorded as ``frontend.<action>``).
        payload: Optional free-form event payload.
        metadata: Optional free-form metadata.
    """

    action: ActionName
    payload: Optional[dict] = None
    metadata: Optional[dict] = None


class AdminCreateUserRequest(BaseModel):
    """Input for ``POST /api/admin/users``."""

    username: Username
    password: Password
    display_name: str = ""
    email: str = ""
    roles: List[str] = Field(default_factory=lambda: ["user"])
    is_active: bool = True


class AdminUpdateUserRequest(BaseModel):
    """Input for ``PATCH /api/admin/users/{user_id}`` (all fields optional/partial)."""

    username: Optional[str] = Field(default=None, min_length=1, max_length=120)
    password: Optional[str] = Field(default=None, min_length=8)
    display_name: Optional[str] = None
    email: Optional[str] = None
    roles: Optional[List[str]] = None
    is_active: Optional[bool] = None


class RecordChange(BaseModel):
    """One component change inside a recorded step, such as "water" relabelled "sea water".

    Attributes:
        operation: The kind of change.
        role: The I-ADOPT role of the component it touches, empty when it has none.
        component: The component's IRI, or its label when it has no IRI.
        before: The value before the change, when there was one.
        after: The value after the change, when there is one.
    """

    operation: RecordOperation
    role: RecordRole = ""
    component: str = Field(default="", max_length=500)
    before: Optional[str] = Field(default=None, max_length=2_000)
    after: Optional[str] = Field(default=None, max_length=2_000)


class StartRecordRequest(BaseModel):
    """Input for ``POST /api/records``: start a record from an opened or pasted Turtle.

    Attributes:
        turtle: The Turtle as opened; it becomes version 0.
        occurred_at: When the browser opened it.
        client: Device and screen of the browser, if any.
    """

    turtle: str = Field(..., min_length=1, max_length=MAX_RECORD_TURTLE_CHARS)
    occurred_at: Optional[datetime] = None
    client: LenientClientInfo = None


class RecordEventRequest(BaseModel):
    """Input for ``POST /api/records/{record_id}/events``: one step of a record.

    ``edit`` and ``metadata`` carry the Turtle after the change, which was made on
    ``version``; the server adds a version when the graph changed, and a ``metadata``
    step may change only nanopublication metadata. ``check`` and ``uncheck`` record
    "Looks right" on components of ``version``: they list each component with the
    operation ``confirm`` or ``unconfirm`` and its role or IRI. ``mark_best`` marks
    ``version`` as best, with an optional note and the required count of components that
    lacked "Looks right" at that moment, and lists no changes. Only ``check`` and
    ``uncheck`` may confirm or unconfirm.

    Attributes:
        type: The step type.
        version: The version the step was taken on.
        occurred_at: When the step happened in the browser.
        turtle: The Turtle after an ``edit`` or ``metadata`` step; absent otherwise.
        changes: The component changes in the step.
        note: The optional note of a ``mark_best``.
        unchecked_components: Components without "Looks right" at ``mark_best``.
        client: Device and screen of the browser, if any.
    """

    type: RecordEventType
    version: int = Field(..., ge=0, le=1_000_000)
    occurred_at: Optional[datetime] = None
    turtle: Optional[str] = Field(default=None, max_length=MAX_RECORD_TURTLE_CHARS)
    changes: List[RecordChange] = Field(default_factory=list, max_length=200)
    note: Optional[str] = Field(default=None, max_length=MAX_RECORD_NOTE_CHARS)
    unchecked_components: Optional[int] = Field(default=None, ge=0, le=10_000)
    client: LenientClientInfo = None

    @model_validator(mode="after")
    def _fields_fit_the_type(self) -> RecordEventRequest:
        """Check that each step type carries exactly the fields it needs (see the class docs).

        Raises:
            ValueError: If a step lacks a field its type needs, or carries one it may not.
        """
        changes_content = self.type in ("edit", "metadata")
        if changes_content and self.turtle is None:
            raise ValueError(f"A {self.type} step needs the Turtle after the change.")
        if not changes_content and self.turtle is not None:
            raise ValueError(f"A {self.type} step carries no Turtle.")
        if self.type == "mark_best":
            if self.unchecked_components is None:
                raise ValueError('A mark_best step needs the number of components without "Looks right".')
            if self.changes:
                raise ValueError("A mark_best step lists no changes.")
        elif self.note is not None or self.unchecked_components is not None:
            raise ValueError("Only a mark_best step carries a note or a count of unchecked components.")
        operations = {change.operation for change in self.changes}
        if self.type in ("check", "uncheck"):
            expected = "confirm" if self.type == "check" else "unconfirm"
            if operations != {expected}:
                raise ValueError(f"A {self.type} step lists its components, each with the operation {expected}.")
            if any(not (change.role or change.component) for change in self.changes):
                raise ValueError(f"Each component of a {self.type} step needs its role or IRI.")
        elif operations & {"confirm", "unconfirm"}:
            raise ValueError("Only check and uncheck steps confirm or unconfirm components.")
        return self
