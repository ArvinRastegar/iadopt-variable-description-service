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
    AccountActionRequest,
    AdminCreateUserRequest,
    AdminUpdateUserRequest,
    ClientDevice,
    ClientInfo,
    ClientScreen,
    DecomposeRequest,
    FrontendEventRequest,
    LoginRequest,
    PublishNanopubRequest,
    RecordChange,
    RecordEventRequest,
    RetractNanopubRequest,
    SaveOrcidRequest,
    StartRecordRequest,
)
from .responses import (
    AccountChange,
    AccountHistoryResponse,
    AccountIdentity,
    AdminStatsResponse,
    AdminUser,
    AuditEvent,
    AuditResponse,
    AuthUserResponse,
    CapabilityDecision,
    CurrentUserResponse,
    DecomposeResponse,
    ModelOptionsResponse,
    NanopubPreparationOptionsResponse,
    ProviderConfig,
    PublicUser,
    PublishNanopubResponse,
    ReadinessSummary,
    ReadyzResponse,
    RecordEventResponse,
    RecordPublication,
    RecordStartedResponse,
    RecordSummaryResponse,
    RecordVersion,
    RetractNanopubResponse,
    SavedOrcid,
    SavedOrcidsResponse,
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
    "AccountActionRequest",
    "AdminCreateUserRequest",
    "AdminUpdateUserRequest",
    "ClientDevice",
    "ClientInfo",
    "ClientScreen",
    "DecomposeRequest",
    "FrontendEventRequest",
    "LoginRequest",
    "PublishNanopubRequest",
    "RecordChange",
    "RecordEventRequest",
    "RetractNanopubRequest",
    "SaveOrcidRequest",
    "StartRecordRequest",
    # responses
    "AccountChange",
    "AccountHistoryResponse",
    "AccountIdentity",
    "AdminStatsResponse",
    "AdminUser",
    "AuditEvent",
    "AuditResponse",
    "AuthUserResponse",
    "CapabilityDecision",
    "CurrentUserResponse",
    "DecomposeResponse",
    "ModelOptionsResponse",
    "NanopubPreparationOptionsResponse",
    "ProviderConfig",
    "PublicUser",
    "PublishNanopubResponse",
    "ReadinessSummary",
    "ReadyzResponse",
    "RecordEventResponse",
    "RecordPublication",
    "RecordStartedResponse",
    "RecordSummaryResponse",
    "RecordVersion",
    "RetractNanopubResponse",
    "SavedOrcid",
    "SavedOrcidsResponse",
    "StatusOkResponse",
    "UserResponse",
    "UsersResponse",
]
