"""Typed application settings (the single source of truth for configuration).

Phase 2 consolidates the ~24 ``os.getenv(...)`` reads that were scattered at the
top of ``app.main`` into one ``pydantic-settings`` ``Settings`` object. Everything
else imports ``settings`` from here, which kills the biggest source of hidden
coupling in the monolith.

This module is a leaf: it imports only stdlib, dotenv, and pydantic — never any
app internals — so ``schemas``, ``clients``, and ``services`` can all depend on it
without forming an import cycle.

Behavior is preserved exactly. In particular two boolean flags use *different*
parsing rules in the original code, and that difference is kept:

* ``IADOPT_AUTH_ENABLED`` / ``IADOPT_COOKIE_SECURE`` use ``env_bool`` semantics —
  truthy values are ``{"1", "true", "yes", "on"}`` (case-insensitive).
* ``ENABLE_WIKIDATA_LINKING`` is truthy only when the value lower-cases to exactly
  ``"true"`` (so ``"1"``/``"yes"`` are *false* for this flag).

The resolved values are verified against the original logic by
``tests/test_settings_parity.py``.
"""

from __future__ import annotations

import pathlib
from typing import Annotated, List, Optional

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Load the repo-root .env before Settings reads the environment, so importing this
# leaf resolves the same values whether or not app.main has run load_dotenv yet.
# In containers the variables already live in the process environment, and
# load_dotenv never overrides an existing variable — so this is a safe no-op there.
_THIS_FILE = pathlib.Path(__file__).resolve()
BASE_DIR = _THIS_FILE.parents[2]  # .../backend
ROOT_DIR = _THIS_FILE.parents[3]  # repo root
load_dotenv(ROOT_DIR / ".env")

# --------------------------------------------------------------------------- #
# Provider constants and defaults (moved verbatim from app.main)
# --------------------------------------------------------------------------- #
OPENROUTER_MODEL_PROVIDER = "openrouter"
PSNC_MODEL_PROVIDER = "psnc"
SUPPORTED_MODEL_PROVIDERS = (OPENROUTER_MODEL_PROVIDER, PSNC_MODEL_PROVIDER)

DEFAULT_MODEL_NAME = "qwen/qwen3.5-flash-02-23"
DEFAULT_MODEL_NAMES = [
    "qwen/qwen3.5-flash-02-23",
    "qwen/qwen3-32b",
    "qwen/qwen3.5-397b-a17b",
    "google/gemini-3-flash-preview",
]
DEFAULT_PSNC_MODEL_NAME = "Qwen3.5-397B-A17B"
DEFAULT_PSNC_MODEL_NAMES = [
    "Qwen3.5-397B-A17B",
    "Qwen3-VL-235B-A22B-Instruct-FP8",
]

_DEFAULT_PUBINFO_TEMPLATE_URIS = [
    "https://w3id.org/np/RAA2MfqdBCzmz9yVWjKLXNbyfBNcwsMmOqcNUxkk1maIM",
    "https://w3id.org/np/RA0J4vUn_dekg-U1kK3AOEt02p9mT2WO03uGxLDec1jLw",
    "https://w3id.org/np/RAukAcWHRDlkqxk7H2XNSegc1WnHI569INvNr-xdptDGI",
]
_DEFAULT_RETRACT_PUBINFO_TEMPLATE_URIS = [
    "https://w3id.org/np/RA0J4vUn_dekg-U1kK3AOEt02p9mT2WO03uGxLDec1jLw",
    "https://w3id.org/np/RAukAcWHRDlkqxk7H2XNSegc1WnHI569INvNr-xdptDGI",
]

_TRUTHY = {"1", "true", "yes", "on"}


def _parse_enabled_model_providers(configured: Optional[str]) -> List[str]:
    """Parse the ``ENABLED_MODEL_PROVIDERS`` CSV into a validated provider list.

    Behavior-identical to the original ``app.main._parse_enabled_model_providers``.

    Args:
        configured: Raw CSV value of ``ENABLED_MODEL_PROVIDERS`` or ``None``.

    Returns:
        The ordered, de-duplicated list of enabled provider keys; all supported
        providers when unset/blank.

    Raises:
        RuntimeError: If an unsupported provider is named, or the result is empty.
    """
    if configured is None or not configured.strip():
        return list(SUPPORTED_MODEL_PROVIDERS)

    providers: List[str] = []
    for value in configured.split(","):
        provider = value.strip().lower()
        if provider and provider not in providers:
            providers.append(provider)

    unsupported = [provider for provider in providers if provider not in SUPPORTED_MODEL_PROVIDERS]
    if unsupported:
        raise RuntimeError(
            "Unsupported ENABLED_MODEL_PROVIDERS value(s): "
            f"{', '.join(unsupported)}. Supported providers: {', '.join(SUPPORTED_MODEL_PROVIDERS)}"
        )
    if not providers:
        raise RuntimeError("ENABLED_MODEL_PROVIDERS must enable at least one provider.")
    return providers


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    """Drop blanks/duplicates while keeping first-seen order (matches app.main)."""
    seen = set()
    ordered: List[str] = []
    for value in values:
        clean_value = value.strip()
        if not clean_value or clean_value in seen:
            continue
        seen.add(clean_value)
        ordered.append(clean_value)
    return ordered


def _split_csv(value: str) -> List[str]:
    """Split a CSV string into stripped, non-empty parts (matches app.main)."""
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    """All runtime configuration, read once from the environment.

    Field defaults and coercion mirror the original ``app.main`` module-level
    constants exactly. Raw env strings that feed derived values (model-name lists,
    enabled providers, state paths) are stored on ``*_raw`` fields and exposed
    through computed properties.
    """

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=(),
    )

    # --- LLM / providers ---------------------------------------------------- #
    temperature: float = Field(default=0.5, validation_alias="TEMPERATURE")
    openrouter_api_key: Optional[str] = Field(default=None, validation_alias="OPENROUTER_API_KEY")
    model_name: str = Field(default=DEFAULT_MODEL_NAME, validation_alias="MODEL_NAME")
    model_names_raw: str = Field(default="", validation_alias="MODEL_NAMES")
    psnc_model_name: str = Field(default=DEFAULT_PSNC_MODEL_NAME, validation_alias="PSNC_MODEL_NAME")
    psnc_model_names_raw: str = Field(default="", validation_alias="PSNC_MODEL_NAMES")
    enabled_model_providers_raw: Optional[str] = Field(default=None, validation_alias="ENABLED_MODEL_PROVIDERS")
    default_model_provider_raw: str = Field(default="", validation_alias="DEFAULT_MODEL_PROVIDER")

    # --- PSNC client -------------------------------------------------------- #
    psnc_api_key: Optional[str] = Field(default=None, validation_alias="PSNC_API_KEY")
    psnc_api_base_url: str = Field(default="https://llm.hpc.psnc.pl", validation_alias="PSNC_API_BASE_URL")
    psnc_rerank_model: str = Field(default="bge-reranker-v2-m3", validation_alias="PSNC_RERANK_MODEL")

    # --- Wikidata enrichment ------------------------------------------------ #
    rerank_threshold: float = Field(default=0.10, validation_alias="RERANK_THRESHOLD")
    enable_wikidata_linking: bool = Field(default=True, validation_alias="ENABLE_WIKIDATA_LINKING")

    # --- Nanopub ------------------------------------------------------------ #
    nanopub_private_key: Optional[str] = Field(default=None, validation_alias="NANOPUB_PRIVATE_KEY")
    nanopub_public_key: Optional[str] = Field(default=None, validation_alias="NANOPUB_PUBLIC_KEY")
    nanopub_orcid_id: Optional[str] = Field(default=None, validation_alias="NANOPUB_ORCID_ID")
    nanopub_agent_intro_uri: Optional[str] = Field(default=None, validation_alias="NANOPUB_AGENT_INTRO_URI")
    nanopub_publish_server: str = Field(
        default="https://registry.petapico.org/np/", validation_alias="NANOPUB_PUBLISH_SERVER"
    )
    nanopub_license_uri: str = Field(
        default="https://creativecommons.org/licenses/by/4.0/", validation_alias="NANOPUB_LICENSE_URI"
    )
    nanopub_was_created_at: str = Field(
        default="https://nanodash.petapico.org/", validation_alias="NANOPUB_WAS_CREATED_AT"
    )
    nanopub_template_uri: str = Field(
        default="https://w3id.org/np/RAkcfj9W_lJjlq26paIFmTY4mZoaY27BnZCjcsL34EPIA",
        validation_alias="NANOPUB_TEMPLATE_URI",
    )
    nanopub_provenance_template_uri: str = Field(
        default="https://w3id.org/np/RANwQa4ICWS5SOjw7gp99nBpXBasapwtZF1fIM3H2gYTM",
        validation_alias="NANOPUB_PROVENANCE_TEMPLATE_URI",
    )
    nanopub_pubinfo_template_uris: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: list(_DEFAULT_PUBINFO_TEMPLATE_URIS),
        validation_alias="NANOPUB_PUBINFO_TEMPLATE_URIS",
    )
    iadopt_variable_conforms_to: str = Field(
        default="https://w3id.org/np/RA5MTl9GFH-QuuBHYEA2hOtxOMOV4-jrhtdx5lOy9CAQE",
        validation_alias="IADOPT_VARIABLE_CONFORMS_TO",
    )
    nanopub_retract_template_uri: str = Field(
        default="https://w3id.org/np/RAQP3NJvnLA2Z-2DrYAN0nTC-RFp67td1t4-pQqQ_ZKmo",
        validation_alias="NANOPUB_RETRACT_TEMPLATE_URI",
    )
    nanopub_retract_provenance_template_uri: str = Field(
        default="https://w3id.org/np/RA7lSq6MuK_TIC6JMSHvLtee3lpLoZDOqLJCLXevnrPoU",
        validation_alias="NANOPUB_RETRACT_PROVENANCE_TEMPLATE_URI",
    )
    nanopub_retract_pubinfo_template_uris: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: list(_DEFAULT_RETRACT_PUBINFO_TEMPLATE_URIS),
        validation_alias="NANOPUB_RETRACT_PUBINFO_TEMPLATE_URIS",
    )
    iadopt_created_with_label: str = Field(
        default="LLM-assisted I-ADOPT variable generation", validation_alias="IADOPT_CREATED_WITH_LABEL"
    )

    # --- Auth / audit ------------------------------------------------------- #
    auth_enabled: bool = Field(default=False, validation_alias="IADOPT_AUTH_ENABLED")
    cookie_secure: bool = Field(default=False, validation_alias="IADOPT_COOKIE_SECURE")
    session_secret: str = Field(default="", validation_alias="IADOPT_SESSION_SECRET")
    session_ttl_hours: int = Field(default=12, validation_alias="IADOPT_SESSION_TTL_HOURS")
    audit_retention_days: int = Field(default=30, validation_alias="IADOPT_AUDIT_RETENTION_DAYS")
    audit_max_payload_bytes: int = Field(default=1_000_000, validation_alias="IADOPT_AUDIT_MAX_PAYLOAD_BYTES")
    state_dir_raw: Optional[str] = Field(default=None, validation_alias="IADOPT_STATE_DIR")
    db_path_raw: Optional[str] = Field(default=None, validation_alias="IADOPT_DB_PATH")

    # --- Validators (preserve exact original coercion) ---------------------- #
    @field_validator("auth_enabled", "cookie_secure", mode="before")
    @classmethod
    def _coerce_env_bool(cls, value: object) -> object:
        """Apply ``env_bool`` truthiness ({1,true,yes,on}); pass through real bools."""
        if isinstance(value, bool) or value is None:
            return value
        return str(value).strip().lower() in _TRUTHY

    @field_validator("enable_wikidata_linking", mode="before")
    @classmethod
    def _coerce_wikidata_flag(cls, value: object) -> object:
        """Truthy only when the value lower-cases to exactly ``"true"``."""
        if isinstance(value, bool) or value is None:
            return value
        return str(value).strip().lower() == "true"

    @field_validator("nanopub_pubinfo_template_uris", "nanopub_retract_pubinfo_template_uris", mode="before")
    @classmethod
    def _coerce_uri_csv(cls, value: object) -> object:
        """Split a CSV env string into stripped, non-empty URIs; pass through lists."""
        if isinstance(value, list) or value is None:
            return value
        return _split_csv(str(value))

    # --- Derived properties ------------------------------------------------- #
    @property
    def enabled_model_providers(self) -> List[str]:
        """The validated, ordered list of enabled model providers."""
        return _parse_enabled_model_providers(self.enabled_model_providers_raw)

    @property
    def default_model_provider(self) -> str:
        """The default provider: the configured value if enabled, else the first enabled."""
        enabled = self.enabled_model_providers
        configured = self.default_model_provider_raw.strip().lower()
        return configured if configured in enabled else enabled[0]

    @property
    def model_names(self) -> List[str]:
        """Allowed OpenRouter model names (configured or built-in), model_name first."""
        configured = _split_csv(self.model_names_raw)
        base_models = configured or DEFAULT_MODEL_NAMES
        return _dedupe_preserve_order([self.model_name, *base_models])

    @property
    def psnc_model_names(self) -> List[str]:
        """Allowed PSNC model names (configured or built-in), psnc_model_name first."""
        configured = _split_csv(self.psnc_model_names_raw)
        base_models = configured or DEFAULT_PSNC_MODEL_NAMES
        return _dedupe_preserve_order([self.psnc_model_name, *base_models])

    @property
    def base_dir(self) -> pathlib.Path:
        """The backend package directory (``.../backend``)."""
        return BASE_DIR

    @property
    def data_dir(self) -> pathlib.Path:
        """The ``data`` directory holding the schema, prompts, and examples."""
        return BASE_DIR / "data"

    @property
    def schema_path(self) -> pathlib.Path:
        """Path to ``Json_schema.json``."""
        return self.data_dir / "Json_schema.json"

    @property
    def prompt_dir(self) -> pathlib.Path:
        """Path to the prompt-templates directory."""
        return self.data_dir / "prompts"

    @property
    def five_shot_dir(self) -> pathlib.Path:
        """Path to the five-shot example directory."""
        return self.data_dir / "Json_preferred" / "five_shot"

    @property
    def auth_state_dir(self) -> pathlib.Path:
        """Directory for auth state; defaults to ``<base_dir>/state``."""
        return pathlib.Path(self.state_dir_raw or str(BASE_DIR / "state"))

    @property
    def auth_db_path(self) -> pathlib.Path:
        """SQLite path; defaults to ``<auth_state_dir>/iadopt.sqlite3``."""
        return pathlib.Path(self.db_path_raw or str(self.auth_state_dir / "iadopt.sqlite3"))


settings = Settings()
"""The process-wide settings singleton."""
