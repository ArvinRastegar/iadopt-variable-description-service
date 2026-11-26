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
        enriched_json: The prediction plus dynamic entity-link ``*URI`` keys: Wikidata, QUDT
            or OM IRIs (free-form).
        ttl: The generated I-ADOPT Turtle serialization.
        record_id: The decomposition record this run started, whose version 0 is ``ttl``;
            ``None`` when nothing is recorded (D-036).
    """

    raw_llm_output: str
    parsed_json: Dict[str, Any]
    schema_valid: bool
    validation_errors: List[str]
    enriched_json: Dict[str, Any]
    ttl: str
    record_id: Optional[str] = None


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
        conforms_to_uri: The I-ADOPT ``dct:conformsTo`` target URI.
        created_with_label: The ``pav:createdWith`` label.
    """

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
        auth_provider: Authentication provider (``"local"`` or ``"oidc"``).
        external_subject: External subject identifier, if any.
        role: ``user``, ``admin`` or ``owner``, derived for every request.
    """

    id: int
    username: str
    display_name: str
    email: str
    roles: List[str]
    is_active: bool
    auth_provider: str
    external_subject: Optional[str]
    role: str


class AuthUserResponse(BaseModel):
    """Body of ``/api/auth/login``.

    Attributes:
        user: The authenticated user's public projection.
        auth_enabled: Whether authentication is enabled on this deployment.
    """

    user: PublicUser
    auth_enabled: bool


class CapabilityDecision(BaseModel):
    """What the session may do with one capability.

    Attributes:
        allowed: Whether the capability is granted.
        reason: The reason code of a refusal, else ``None``.
        message: The message of a refusal, else ``None``.
        uses_left_today: For ``decompose`` only: the uses left under a daily cap, else ``None``.
        resets_at: For ``decompose`` only: when the daily count resets (ISO-8601), else ``None``.
    """

    allowed: bool
    reason: Optional[str]
    message: Optional[str]
    uses_left_today: Optional[int] = None
    resets_at: Optional[str] = None


class CurrentUserResponse(AuthUserResponse):
    """Body of ``/api/auth/me``.

    Attributes:
        capabilities: What the session may do, keyed by capability name.
        sign_in: What started the session, ``password`` or ``rohub``; ``None`` for the development user.
        csrf_token: What an unsafe request with a ROHub session must carry in ``X-CSRF-Token``;
            ``None`` otherwise.
    """

    capabilities: Dict[str, CapabilityDecision]
    sign_in: Optional[str]
    csrf_token: Optional[str]


class SavedOrcid(BaseModel):
    """One ORCID iD saved to the signed-in account.

    Attributes:
        orcid: Canonical ``https://orcid.org/<iD>`` URI.
        name: Public name from the ORCID record, when it could be resolved.
        created_at: When the iD was saved (ISO-8601 UTC).
        last_used_at: When it was last saved or used to publish or retract (ISO-8601 UTC).
    """

    orcid: str
    name: Optional[str]
    created_at: str
    last_used_at: str


class SavedOrcidsResponse(BaseModel):
    """Body of the ``/api/auth/me/orcids`` routes: the account's iDs, most recently used first."""

    orcids: List[SavedOrcid]


# --------------------------------------------------------------------------- #
# Decomposition records (D-036)
# --------------------------------------------------------------------------- #


class RecordStartedResponse(BaseModel):
    """Body of ``POST /api/records``.

    Attributes:
        record_id: The new record's id.
        version: Its first version number (always 0).
    """

    record_id: str
    version: int


class RecordEventResponse(BaseModel):
    """Body of ``POST /api/records/{record_id}/events``.

    Attributes:
        record_id: The record's id.
        event: The step's number in the record.
        version: The version the record is on after the step.
    """

    record_id: str
    event: int
    version: int


class RecordVersion(BaseModel):
    """One version of a record.

    Attributes:
        number: The version number (0, 1, 2, ...).
        kind: ``model_draft``, ``imported``, ``edited`` or ``metadata``.
        content_sha256: SHA-256 of the version's canonical graph.
        parent: The number of the version it was made from, or ``None`` for version 0.
        created_at: When it was stored (ISO-8601 UTC).
    """

    number: int
    kind: str
    content_sha256: str
    parent: Optional[int]
    created_at: str


class RecordPublication(BaseModel):
    """One publication of a record's version.

    Attributes:
        version: The published version number.
        nanopub_uri: The nanopublication's URI.
        orcid: The ORCID iD the nanopublication names as its creator.
        published_at: When it was published (ISO-8601 UTC).
    """

    version: int
    nanopub_uri: str
    orcid: str
    published_at: str


class RecordSummaryResponse(BaseModel):
    """Body of ``GET /api/records/{record_id}``.

    Attributes:
        record_id: The record's id.
        origin: ``decompose`` or ``import``.
        created_at: When the record started (ISO-8601 UTC).
        current_version: The latest version number, or ``None`` before the first version.
        best_version: The version most recently marked as best, or ``None``.
        versions: Every version, oldest first.
        publications: Every publication, oldest first.
        event_count: How many steps and milestones the record holds.
    """

    record_id: str
    origin: str
    created_at: str
    current_version: Optional[int]
    best_version: Optional[int]
    versions: List[RecordVersion]
    publications: List[RecordPublication]
    event_count: int


class StatusOkResponse(BaseModel):
    """A bare ``{"status": "ok"}`` acknowledgement (events, livez, health)."""

    status: str


class SignOutResponse(StatusOkResponse):
    """Body of ``/api/auth/logout``.

    Attributes:
        end_session_url: Where the page goes next to sign out at ROHub; ``None`` for a password session.
    """

    end_session_url: Optional[str]


class ReadyzResponse(BaseModel):
    """Body of ``/api/readyz`` on success.

    Attributes:
        status: Literal ``"ready"`` on success.
        checks: Per-check readiness booleans.
    """

    status: str
    checks: Dict[str, bool]


class AccountIdentity(BaseModel):
    """An identity-provider identity that leads to an account.

    Attributes:
        issuer: The provider's issuer URL.
        subject: The subject the provider gives the person.
        last_login_at: The last login through it (ISO-8601 UTC), or ``None``.
    """

    issuer: str
    subject: str
    last_login_at: Optional[str]


class AdminUser(PublicUser):
    """An account as admins see it.

    Attributes:
        blocked: Whether the account is blocked.
        email_verified: Whether the identity provider verified the email; ``None`` when unknown.
        local: Whether the account has a usable password.
        identities: The identity-provider identities that lead to it.
        created_at: When the account was created (ISO-8601 UTC).
        last_login_at: The account's last login (ISO-8601 UTC), or ``None``.
        approval: ``approved`` by an admin, ``waiting``, or ``not_needed`` for a local account, an
            admin and the owner.
        approved_at: When it was approved (ISO-8601 UTC), or ``None``.
        approved_by: The username of who approved it, or ``None``.
    """

    blocked: bool
    email_verified: Optional[bool]
    local: bool
    identities: List[AccountIdentity]
    created_at: str
    last_login_at: Optional[str]
    approval: str
    approved_at: Optional[str]
    approved_by: Optional[str]


class UserResponse(BaseModel):
    """Body of the admin routes that change one account: ``{"user": ...}``."""

    user: AdminUser


class UsersResponse(BaseModel):
    """Body of ``/api/admin/users``: ``{"users": [...]}``."""

    users: List[AdminUser]


class AccountChange(BaseModel):
    """One change in an account's history.

    Attributes:
        action: What changed, such as ``blocked`` or ``admin_granted``.
        actor: The username of the account that acted, or ``offline`` or ``bootstrap``.
        reason: The reason given, if any.
        details: What else belongs to the change.
        created_at: When it happened (ISO-8601 UTC).
    """

    action: str
    actor: str
    reason: Optional[str]
    details: Dict[str, Any]
    created_at: str


class AccountHistoryResponse(BaseModel):
    """Body of ``/api/admin/users/{user_id}/history``, newest change first."""

    history: List[AccountChange]


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
