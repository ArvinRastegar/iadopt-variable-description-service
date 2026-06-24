"""Response body models for the I-ADOPT API.

Every model here is shaped to serialize **byte-identically** to the dict the
corresponding route returns today (field order preserved, nullable fields kept as
``Optional[...] = None`` so FastAPI emits explicit ``null``). Adding these as
``response_model=`` lets the OpenAPI schema describe every route without changing
any output.

Two fields are intentionally left as free-form dicts (documented, not closed):
``DecomposeResponse.parsed_json`` and ``DecomposeResponse.enriched_json`` — see
``app.schemas.domain`` for why. Audit ``request_payload``/``response_payload``/
``metadata`` are JSON-serialized **strings** (or null), not nested objects, per the
storage format in ``AuthStore._payload_to_text``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Decomposition
# --------------------------------------------------------------------------- #


class DecomposeResponse(BaseModel):
    """The complete result of a decomposition pipeline run.

    Attributes:
        raw_llm_output: The raw model output (reasoning + content) as displayed.
        parsed_json: The parsed/coerced LLM prediction (free-form; see domain docs).
        schema_valid: Whether the prediction passed schema + semantic validation.
        validation_errors: Human-readable validation error lines (empty if valid).
        enriched_json: The prediction plus dynamic Wikidata ``*URI`` keys (free-form).
        ttl: The generated I-ADOPT Turtle serialization.
    """

    raw_llm_output: str
    parsed_json: Dict[str, Any]
    schema_valid: bool
    validation_errors: List[str]
    enriched_json: Dict[str, Any]
    ttl: str


class ProviderConfig(BaseModel):
    """One provider's entry in the model-options ``providers`` map.

    Attributes:
        label: Human-readable provider label (e.g. ``"PSNC"``).
        default_model_name: The provider's default model.
        model_names: All model names the provider exposes to the UI.
    """

    label: str
    default_model_name: str
    model_names: List[str]


class ModelOptionsResponse(BaseModel):
    """Backend-managed model options for the frontend dropdown (``/api/model-options``).

    Attributes:
        default_model_provider: The default provider key.
        default_model_name: The default model for the default provider.
        model_names: Allowed model names for the default provider.
        providers: Per-provider configuration, keyed by provider name.
    """

    default_model_provider: str
    default_model_name: str
    model_names: List[str]
    providers: Dict[str, ProviderConfig]


# --------------------------------------------------------------------------- #
# Nanopub
# --------------------------------------------------------------------------- #


class NanopubPreparationOptionsResponse(BaseModel):
    """Metadata constants for preparing pasted Turtle (``/api/nanopub/preparation-options``).

    Attributes:
        default_creator_orcid_id: Configured default creator ORCID URI, if any.
        conforms_to_uri: The I-ADOPT ``dct:conformsTo`` target URI.
        created_with_label: The ``pav:createdWith`` label.
    """

    default_creator_orcid_id: Optional[str]
    conforms_to_uri: str
    created_with_label: str


class PublishNanopubResponse(BaseModel):
    """Result of publishing a nanopublication (``/api/nanopub/publish``).

    Attributes:
        nanopub_url: The URI of the newly published nanopub.
        published_to: The registry/server it was published to.
        variable_identifier: The variable identifier extracted from the assertion.
        variable_uri: The variable resource URI.
    """

    nanopub_url: str
    published_to: str
    variable_identifier: str
    variable_uri: str


class RetractNanopubResponse(BaseModel):
    """Result of retracting a nanopublication (``/api/nanopub/retract``).

    Attributes:
        retraction_url: The URI of the published retraction nanopub.
        published_to: The registry/server it was published to.
        retracted_nanopub_url: The canonical URI of the retracted nanopub.
    """

    retraction_url: str
    published_to: str
    retracted_nanopub_url: str


# --------------------------------------------------------------------------- #
# Auth / users
# --------------------------------------------------------------------------- #


class PublicUser(BaseModel):
    """The public projection of a user (``_public_user``), shared by auth/admin routes.

    Field order matches ``app.main._public_user`` exactly.

    Attributes:
        id: User id.
        username: Login name.
        display_name: Display name (falls back to username).
        email: Email address (empty string when unset).
        roles: Assigned roles (subset of ``{"admin", "user"}``).
        is_active: Whether the account is active.
        auth_provider: Authentication provider (``"local"``).
        external_subject: External subject identifier, if any.
    """

    id: int
    username: str
    display_name: str
    email: str
    roles: List[str]
    is_active: bool
    auth_provider: str
    external_subject: Optional[str]


class AuthUserResponse(BaseModel):
    """Body of ``/api/auth/login`` and ``/api/auth/me``.

    Attributes:
        user: The authenticated user's public projection.
        auth_enabled: Whether authentication is enabled on this deployment.
    """

    user: PublicUser
    auth_enabled: bool


class StatusOkResponse(BaseModel):
    """A bare ``{"status": "ok"}`` acknowledgement (logout, events, livez, health)."""

    status: str


class ReadyzResponse(BaseModel):
    """Body of ``/api/readyz`` on success.

    Attributes:
        status: Literal ``"ready"`` on success.
        checks: Per-check readiness booleans.
    """

    status: str
    checks: Dict[str, bool]


class UserResponse(BaseModel):
    """Body of the admin user create/update routes: ``{"user": ...}``."""

    user: PublicUser


class UsersResponse(BaseModel):
    """Body of ``/api/admin/users``: ``{"users": [...]}``."""

    users: List[PublicUser]


# --------------------------------------------------------------------------- #
# Admin: audit + stats
# --------------------------------------------------------------------------- #


class AuditEvent(BaseModel):
    """One audit log row (``AuthStore._event_from_row``).

    Field order matches the source dict exactly. Note that ``request_payload``,
    ``response_payload`` and ``metadata`` are JSON-serialized **strings** (or null),
    while ``metadata_json`` is the parsed object.

    Attributes:
        id: Event id.
        created_at: ISO-8601 creation timestamp.
        user_id: Acting user id, or ``None`` for unauthenticated events.
        username: Acting username (empty string when unknown).
        action: Action name (e.g. ``"auth.login"``, ``"decompose"``).
        method: HTTP method.
        path: Request path.
        status_code: HTTP status code, or ``None``.
        latency_ms: Server-side latency in ms, or ``None``.
        ip_address: Client IP (empty string when unknown).
        user_agent: Client user agent (empty string when unknown).
        request_payload: JSON string of the request payload, or ``None``.
        response_payload: JSON string of the response payload, or ``None``.
        metadata: JSON string of metadata, or ``None``.
        metadata_json: Parsed metadata object (``{}`` when absent/unparseable).
        error: Error message, or ``None``.
    """

    id: int
    created_at: str
    user_id: Optional[int]
    username: str
    action: str
    method: str
    path: str
    status_code: Optional[int]
    latency_ms: Optional[int]
    ip_address: str
    user_agent: str
    request_payload: Optional[str]
    response_payload: Optional[str]
    metadata: Optional[str]
    metadata_json: Dict[str, Any]
    error: Optional[str]


class AuditResponse(BaseModel):
    """Body of ``/api/admin/audit``: ``{"events": [...]}``."""

    events: List[AuditEvent]


class ReadinessSummary(BaseModel):
    """The ``readiness`` block appended to admin stats.

    Attributes:
        status: ``"ready"`` or ``"not_ready"``.
        checks: Per-check readiness booleans.
    """

    status: str
    checks: Dict[str, bool]


class AdminStatsResponse(BaseModel):
    """Body of ``/api/admin/stats``.

    Field order matches ``AuthStore.stats()`` with ``readiness`` appended last.

    Attributes:
        auth_enabled: Whether auth is enabled.
        total_users: Total user count.
        active_users: Active user count.
        active_usernames_30d: Sorted usernames seen in recent events.
        event_count_30d: Number of recent events considered.
        failures_30d: Count of failed/errored events.
        average_latency_ms_30d: Mean latency (integer ms), ``0`` when none.
        events_by_action_30d: Event counts keyed by action.
        model_usage_30d: Usage counts keyed by ``"provider / model"``.
        recent_events: Up to 25 most-recent audit events.
        readiness: Readiness summary (added by the route).
    """

    auth_enabled: bool
    total_users: int
    active_users: int
    active_usernames_30d: List[str]
    event_count_30d: int
    failures_30d: int
    average_latency_ms_30d: int
    events_by_action_30d: Dict[str, int]
    model_usage_30d: Dict[str, int]
    recent_events: List[AuditEvent]
    readiness: ReadinessSummary
