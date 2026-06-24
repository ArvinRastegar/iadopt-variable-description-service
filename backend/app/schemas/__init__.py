"""Pydantic v2 contracts for the I-ADOPT API.

This package is the single source of truth for the data shapes that cross the
service's boundaries: HTTP request bodies, HTTP response bodies, the internal
pipeline *domain* objects (prediction / enriched prediction / constraints), and
the decompose stream event protocol.

Layering: ``schemas`` is a leaf — it depends only on ``app.core.config`` (also a
leaf), never on routers/services. This keeps the Phase-2 dependency direction
(routers → services → clients → core/schemas) acyclic.
"""

from __future__ import annotations

from .common import (
    ActionName,
    NonEmptyStr,
    Password,
    Username,
    normalize_orcid,
    orcid_suffix,
)
from .domain import (
    AsymmetricSystem,
    Constraint,
    EnrichedPredictionDict,
    EntityOrSystem,
    Prediction,
    SymmetricSystem,
)
from .events import ErrorEvent, FinalEvent, RawDeltaEvent, StreamEvent
from .requests import (
    AdminCreateUserRequest,
    AdminUpdateUserRequest,
    DecomposeRequest,
    FrontendEventRequest,
    LoginRequest,
    PublishNanopubRequest,
    RetractNanopubRequest,
)
from .responses import (
    AdminStatsResponse,
    AuditEvent,
    AuditResponse,
    AuthUserResponse,
    DecomposeResponse,
    ModelOptionsResponse,
    NanopubPreparationOptionsResponse,
    ProviderConfig,
    PublicUser,
    PublishNanopubResponse,
    ReadinessSummary,
    ReadyzResponse,
    RetractNanopubResponse,
    StatusOkResponse,
    UserResponse,
    UsersResponse,
)

__all__ = [
    # common
    "ActionName",
    "NonEmptyStr",
    "Password",
    "Username",
    "normalize_orcid",
    "orcid_suffix",
    # domain
    "AsymmetricSystem",
    "Constraint",
    "EnrichedPredictionDict",
    "EntityOrSystem",
    "Prediction",
    "SymmetricSystem",
    # events
    "ErrorEvent",
    "FinalEvent",
    "RawDeltaEvent",
    "StreamEvent",
    # requests
    "AdminCreateUserRequest",
    "AdminUpdateUserRequest",
    "DecomposeRequest",
    "FrontendEventRequest",
    "LoginRequest",
    "PublishNanopubRequest",
    "RetractNanopubRequest",
    # responses
    "AdminStatsResponse",
    "AuditEvent",
    "AuditResponse",
    "AuthUserResponse",
    "DecomposeResponse",
    "ModelOptionsResponse",
    "NanopubPreparationOptionsResponse",
    "ProviderConfig",
    "PublicUser",
    "PublishNanopubResponse",
    "ReadinessSummary",
    "ReadyzResponse",
    "RetractNanopubResponse",
    "StatusOkResponse",
    "UserResponse",
    "UsersResponse",
]
