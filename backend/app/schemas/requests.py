"""Request body models for the I-ADOPT API.

These are moved verbatim (field names, types, defaults, and validation bounds)
from the inline definitions in ``app.main`` so that request parsing and the
generated OpenAPI request schemas are unchanged. The ``DecomposeRequest`` default
for ``model_provider`` is injected at import time from the runtime configuration,
exactly as before.

No normalizing validators are attached: request values must be echoed back into
the audit log unchanged, so rewriting them here would be a behavior change.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from .common import ActionName, NonEmptyStr, Password, Username
from ..core.config import settings


class DecomposeRequest(BaseModel):
    """Input for the decomposition endpoints (``/api/decompose`` and ``/stream``).

    Attributes:
        definition: Variable definition in plain text (required, non-empty).
        model_name: One of the backend-configured model names; ``None`` uses the
            provider default.
        model_provider: Model provider to use; defaults to the configured default.
        creator_orcid_id: Optional ORCID override for TTL/provenance metadata;
            falls back to ``NANOPUB_ORCID_ID`` when omitted.
        disable_thinking: When true, request the model without reasoning effort.
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
    creator_orcid_id: Optional[str] = Field(
        default=None,
        description="Optional ORCID override for TTL/provenance metadata; falls back to NANOPUB_ORCID_ID when omitted.",
    )
    disable_thinking: bool = Field(
        default=True,
        description="When true, request the model without reasoning effort by sending `reasoning.effort = none`.",
    )


class PublishNanopubRequest(BaseModel):
    """Input for ``POST /api/nanopub/publish``.

    Attributes:
        ttl: TTL assertion payload currently shown in the frontend (required).
        creator_orcid_id: Optional ORCID override for provenance/pubinfo metadata.
    """

    ttl: str = Field(..., min_length=1, description="TTL assertion payload currently shown in the frontend")
    creator_orcid_id: Optional[str] = Field(
        default=None,
        description="Optional ORCID override for provenance/pubinfo metadata; falls back to NANOPUB_ORCID_ID when omitted.",
    )


class RetractNanopubRequest(BaseModel):
    """Input for ``POST /api/nanopub/retract``.

    Attributes:
        nanopub_uri: The published nanopub URI or Nanodash explore URL to retract.
        creator_orcid_id: Optional ORCID override for retraction provenance/pubinfo.
    """

    nanopub_uri: str = Field(
        ..., min_length=1, description="The published nanopub URI or Nanodash explore URL to retract"
    )
    creator_orcid_id: Optional[str] = Field(
        default=None,
        description="Optional ORCID override for retraction provenance/pubinfo metadata; falls back to NANOPUB_ORCID_ID when omitted.",
    )


class LoginRequest(BaseModel):
    """Credentials for ``POST /api/auth/login``."""

    username: NonEmptyStr
    password: NonEmptyStr


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
