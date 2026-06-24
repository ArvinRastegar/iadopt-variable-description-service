from __future__ import annotations

import copy
import json
import os
import pathlib
import random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from dotenv import load_dotenv
import httpx
from nanopub import Nanopub, NanopubConf, Profile
from nanopub.namespaces import NPX, NTEMPLATE, PAV
import requests
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse, StreamingResponse
from jsonschema import Draft202012Validator
from openai import APIStatusError, OpenAI, OpenAIError
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, FOAF, PROV, RDF, RDFS, SKOS, XSD
from contextlib import asynccontextmanager

from .auth import AuthStore, env_bool
from .core import config
from .schemas import (
    AdminCreateUserRequest,
    AdminStatsResponse,
    AdminUpdateUserRequest,
    AuditResponse,
    AuthUserResponse,
    DecomposeRequest,
    DecomposeResponse,
    FrontendEventRequest,
    LoginRequest,
    ModelOptionsResponse,
    NanopubPreparationOptionsResponse,
    PublishNanopubRequest,
    PublishNanopubResponse,
    ReadyzResponse,
    RetractNanopubRequest,
    RetractNanopubResponse,
    StatusOkResponse,
    UserResponse,
    UsersResponse,
)


# ======================================================================================
# App setup
# ======================================================================================
def warmup_assets() -> None:
    global _schema_cache, _validator_cache, _prompt_version_cache, _examples_5_cache

    # OpenRouter is only initialized when it is enabled for this deployment.
    if OPENROUTER_MODEL_PROVIDER in ENABLED_MODEL_PROVIDERS and OPENROUTER_API_KEY:
        get_openai_client()

    # Cache schema validator
    _schema_cache = _patch_schema_for_pipeline(load_schema(SCHEMA_PATH))
    _validator_cache = Draft202012Validator(_schema_cache)

    # Cache prompt version + examples
    versions = list_prompt_versions(PROMPT_DIR)
    if not versions:
        raise RuntimeError(f"No prompt files found in: {PROMPT_DIR}")
    _prompt_version_cache = versions[0]
    _examples_5_cache = load_examples(FIVE_SHOT_DIR, 5)

    # Prime HTTP session
    get_http_session()


@asynccontextmanager
async def lifespan(_: FastAPI):
    auth_store.init()
    warmup_assets()
    yield


API_DESCRIPTION = """
I-ADOPT variable decomposition, Turtle generation, visualization support, and nanopublication publishing.

There are two decomposition endpoints on purpose:

- `POST /api/decompose/stream` is the endpoint used by the frontend. It streams raw LLM output while the model is
  responding, then emits the final parsed JSON, validation result, enriched JSON, and Turtle payload.
- `POST /api/decompose` runs the same backend pipeline but returns only one final JSON response. It is kept for API
  clients, scripts, tests, and debugging tools that do not want to consume an NDJSON stream.
"""

API_PREFIX = "/api"

app = FastAPI(
    title="I-ADOPT Variable Decomposition API",
    version="0.1.0",
    description=API_DESCRIPTION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ======================================================================================
# Paths & configuration
# ======================================================================================

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent.parent

load_dotenv(ROOT_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
SCHEMA_PATH = DATA_DIR / "Json_schema.json"
PROMPT_DIR = DATA_DIR / "prompts"
FIVE_SHOT_DIR = DATA_DIR / "Json_preferred" / "five_shot"

# Provider configuration is owned by the core.config leaf so that the schemas
# package can read it without importing this module (avoids a circular import).
# These module-level names are kept as thin aliases for the rest of main.py.
OPENROUTER_MODEL_PROVIDER = config.OPENROUTER_MODEL_PROVIDER
PSNC_MODEL_PROVIDER = config.PSNC_MODEL_PROVIDER
SUPPORTED_MODEL_PROVIDERS = config.SUPPORTED_MODEL_PROVIDERS
ENABLED_MODEL_PROVIDERS = config.settings.enabled_model_providers
DEFAULT_MODEL_PROVIDER = config.settings.default_model_provider

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

# MODEL_NAME = os.getenv("MODEL_NAME", "qwen/qwen3.5-397b-a17b")
MODEL_NAME = os.getenv("MODEL_NAME", DEFAULT_MODEL_NAME)
# MODEL_NAME = os.getenv("MODEL_NAME", "qwen/qwen3-32b")
# MODEL_NAME = os.getenv("MODEL_NAME", "google/gemini-3-flash-preview")

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.5"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
PSNC_API_KEY = os.getenv("PSNC_API_KEY")
PSNC_API_BASE_URL = os.getenv("PSNC_API_BASE_URL", "https://llm.hpc.psnc.pl")
PSNC_RERANK_MODEL = os.getenv("PSNC_RERANK_MODEL", "bge-reranker-v2-m3")
NANOPUB_PRIVATE_KEY = os.getenv("NANOPUB_PRIVATE_KEY")
NANOPUB_PUBLIC_KEY = os.getenv("NANOPUB_PUBLIC_KEY")
NANOPUB_ORCID_ID = os.getenv("NANOPUB_ORCID_ID")
NANOPUB_AGENT_INTRO_URI = os.getenv("NANOPUB_AGENT_INTRO_URI")
NANOPUB_PUBLISH_SERVER = os.getenv("NANOPUB_PUBLISH_SERVER", "https://registry.petapico.org/np/")
NANOPUB_LICENSE_URI = os.getenv("NANOPUB_LICENSE_URI", "https://creativecommons.org/licenses/by/4.0/")
NANOPUB_WAS_CREATED_AT = os.getenv("NANOPUB_WAS_CREATED_AT", "https://nanodash.petapico.org/")
NANOPUB_TEMPLATE_URI = os.getenv(
    "NANOPUB_TEMPLATE_URI", "https://w3id.org/np/RAkcfj9W_lJjlq26paIFmTY4mZoaY27BnZCjcsL34EPIA"
)
NANOPUB_PROVENANCE_TEMPLATE_URI = os.getenv(
    "NANOPUB_PROVENANCE_TEMPLATE_URI", "https://w3id.org/np/RANwQa4ICWS5SOjw7gp99nBpXBasapwtZF1fIM3H2gYTM"
)
NANOPUB_PUBINFO_TEMPLATE_URIS = [
    uri.strip()
    for uri in os.getenv(
        "NANOPUB_PUBINFO_TEMPLATE_URIS",
        "https://w3id.org/np/RAA2MfqdBCzmz9yVWjKLXNbyfBNcwsMmOqcNUxkk1maIM,"
        "https://w3id.org/np/RA0J4vUn_dekg-U1kK3AOEt02p9mT2WO03uGxLDec1jLw,"
        "https://w3id.org/np/RAukAcWHRDlkqxk7H2XNSegc1WnHI569INvNr-xdptDGI",
    ).split(",")
    if uri.strip()
]
IADOPT_VARIABLE_CONFORMS_TO = os.getenv(
    "IADOPT_VARIABLE_CONFORMS_TO",
    "https://w3id.org/np/RA5MTl9GFH-QuuBHYEA2hOtxOMOV4-jrhtdx5lOy9CAQE",
)
NANOPUB_RETRACT_TEMPLATE_URI = os.getenv(
    "NANOPUB_RETRACT_TEMPLATE_URI",
    "https://w3id.org/np/RAQP3NJvnLA2Z-2DrYAN0nTC-RFp67td1t4-pQqQ_ZKmo",
)
NANOPUB_RETRACT_PROVENANCE_TEMPLATE_URI = os.getenv(
    "NANOPUB_RETRACT_PROVENANCE_TEMPLATE_URI",
    "https://w3id.org/np/RA7lSq6MuK_TIC6JMSHvLtee3lpLoZDOqLJCLXevnrPoU",
)
NANOPUB_RETRACT_PUBINFO_TEMPLATE_URIS = [
    uri.strip()
    for uri in os.getenv(
        "NANOPUB_RETRACT_PUBINFO_TEMPLATE_URIS",
        "https://w3id.org/np/RA0J4vUn_dekg-U1kK3AOEt02p9mT2WO03uGxLDec1jLw,"
        "https://w3id.org/np/RAukAcWHRDlkqxk7H2XNSegc1WnHI569INvNr-xdptDGI",
    ).split(",")
    if uri.strip()
]
IADOPT_CREATED_WITH_LABEL = os.getenv(
    "IADOPT_CREATED_WITH_LABEL",
    "LLM-assisted I-ADOPT variable generation",
)

RERANK_THRESHOLD = float(os.getenv("RERANK_THRESHOLD", "0.10"))
ENABLE_WIKIDATA_LINKING = os.getenv("ENABLE_WIKIDATA_LINKING", "true").lower() == "true"

AUTH_ENABLED = env_bool("IADOPT_AUTH_ENABLED", False)
AUTH_STATE_DIR = pathlib.Path(os.getenv("IADOPT_STATE_DIR", str(BASE_DIR / "state")))
AUTH_DB_PATH = pathlib.Path(os.getenv("IADOPT_DB_PATH", str(AUTH_STATE_DIR / "iadopt.sqlite3")))
AUTH_SESSION_SECRET = os.getenv("IADOPT_SESSION_SECRET", "")
AUTH_COOKIE_SECURE = env_bool("IADOPT_COOKIE_SECURE", False)
AUTH_SESSION_TTL_HOURS = int(os.getenv("IADOPT_SESSION_TTL_HOURS", "12"))
AUDIT_RETENTION_DAYS = int(os.getenv("IADOPT_AUDIT_RETENTION_DAYS", "30"))
AUDIT_MAX_PAYLOAD_BYTES = int(os.getenv("IADOPT_AUDIT_MAX_PAYLOAD_BYTES", "1000000"))

auth_store = AuthStore(
    db_path=AUTH_DB_PATH,
    enabled=AUTH_ENABLED,
    session_secret=AUTH_SESSION_SECRET,
    cookie_secure=AUTH_COOKIE_SECURE,
    session_ttl_hours=AUTH_SESSION_TTL_HOURS,
    audit_retention_days=AUDIT_RETENTION_DAYS,
    audit_max_payload_bytes=AUDIT_MAX_PAYLOAD_BYTES,
)

PUBLIC_API_PATHS = {
    f"{API_PREFIX}/auth/login",
    f"{API_PREFIX}/auth/verify",
    f"{API_PREFIX}/livez",
    f"{API_PREFIX}/readyz",
    f"{API_PREFIX}/health",
}


def _is_public_api_path(path: str) -> bool:
    return path in PUBLIC_API_PATHS


def _public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "email": user.get("email") or "",
        "roles": user.get("roles", []),
        "is_active": user.get("is_active", True),
        "auth_provider": user.get("auth_provider", "local"),
        "external_subject": user.get("external_subject"),
    }


def require_current_user(request: Request) -> Dict[str, Any]:
    return auth_store.require_user(request)


def require_admin_user(request: Request) -> Dict[str, Any]:
    return auth_store.require_admin(request)


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    if auth_store.enabled and path.startswith(API_PREFIX) and not _is_public_api_path(path):
        user = auth_store.user_from_request(request)
        if not user:
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        request.state.current_user = user
    elif path.startswith(API_PREFIX):
        user = auth_store.user_from_request(request)
        if user:
            request.state.current_user = user

    return await call_next(request)

_JSON_FENCE_RE = re.compile(r"```(?:json)?", re.MULTILINE)
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

ONTO_KEYS = [
    "hasStatisticalModifier",
    "hasProperty",
    "hasObjectOfInterest",
    "hasMatrix",
    "hasContextObject",
    "hasConstraint",
]


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []

    for value in values:
        clean_value = value.strip()
        if not clean_value or clean_value in seen:
            continue
        seen.add(clean_value)
        ordered.append(clean_value)

    return ordered


def _load_model_names() -> List[str]:
    configured = os.getenv("MODEL_NAMES", "")
    configured_models = [value.strip() for value in configured.split(",") if value.strip()]

    # When MODEL_NAMES is not provided, keep the small built-in fallback list available in the UI.
    base_models = configured_models or DEFAULT_MODEL_NAMES
    return _dedupe_preserve_order([MODEL_NAME, *base_models])


MODEL_NAMES = _load_model_names()
PSNC_MODEL_NAME = os.getenv("PSNC_MODEL_NAME", DEFAULT_PSNC_MODEL_NAME)


def _load_psnc_model_names() -> List[str]:
    configured = os.getenv("PSNC_MODEL_NAMES", "")
    configured_models = [value.strip() for value in configured.split(",") if value.strip()]
    base_models = configured_models or DEFAULT_PSNC_MODEL_NAMES
    return _dedupe_preserve_order([PSNC_MODEL_NAME, *base_models])


PSNC_MODEL_NAMES = _load_psnc_model_names()


# Request/response models now live in app.schemas (imported at the top of this
# module). They are the single source of truth for the API contract and feed the
# OpenAPI schema; see app/schemas/ and docs/CONTRACTS.md.


# ======================================================================================
# Lazy-loaded clients/models
# ======================================================================================

_openai_client: Optional[OpenAI] = None
_nanopub_profile: Optional[Profile] = None
_nanopub_agent_uri_cache: Optional[str] = None
_nanopub_agent_label_cache: Optional[str] = None
_orcid_name_cache: Dict[str, Optional[str]] = {}


def get_openai_client() -> OpenAI:
    global _openai_client

    if _openai_client is None:
        if not OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not set.")
        _openai_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )
    return _openai_client


_openai_client: Optional[OpenAI] = None
_http_session: Optional[requests.Session] = None

_schema_cache: Optional[Dict[str, Any]] = None
_validator_cache: Optional[Draft202012Validator] = None
_prompt_version_cache: Optional[str] = None
_examples_5_cache: Optional[List[Dict[str, Any]]] = None


def _normalize_env_multiline(value: Optional[str]) -> Optional[str]:
    """Turn `\\n` escapes in `.env` values back into literal newlines before key normalization."""
    if value is None:
        return None
    return value.strip().replace("\\n", "\n")


def _normalize_nanopub_key(value: Optional[str]) -> Optional[str]:
    """Accept PEM blocks or base64 key bodies and normalize them to the base64 form expected by `nanopub-py`."""
    normalized = _normalize_env_multiline(value)
    if not normalized:
        return None

    # python-dotenv strips surrounding quotes from `.env` values, but Portainer/Swarm pass environment
    # values verbatim. Drop any quotes that were copied along with the key so the PEM armor below is
    # still recognized (otherwise the markers stay glued to the base64 body and decoding fails).
    normalized = normalized.strip()
    while len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in ("'", '"'):
        normalized = normalized[1:-1].strip()

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines:
        return None

    # Strip PEM armor wherever it appears, tolerating stray characters left around the markers.
    lines = [line for line in lines if "-----BEGIN " not in line and "-----END " not in line]

    return "".join(lines)


def _normalize_orcid(orcid_id: Optional[str]) -> Optional[str]:
    if not orcid_id:
        return None
    if orcid_id.startswith("http://") or orcid_id.startswith("https://"):
        return orcid_id
    return f"https://orcid.org/{orcid_id}"


def _orcid_suffix(orcid_id: Optional[str]) -> Optional[str]:
    """Keep the prefix form stable in TTL by extracting the bare ORCID identifier from a full URI."""
    normalized = _normalize_orcid(orcid_id)
    if not normalized:
        return None
    return normalized.rstrip("/").rsplit("/", 1)[-1]


def _extract_orcid_display_name(payload: Any) -> Optional[str]:
    """Pull a human-readable name from either ORCID's public JSON-LD or record-style JSON payloads."""
    if not isinstance(payload, dict):
        return None

    direct_name = _normalize_text(payload.get("name") if isinstance(payload.get("name"), str) else "")
    if direct_name:
        return direct_name

    name_node = payload.get("name")
    if isinstance(name_node, dict):
        credit_name = name_node.get("credit-name")
        if isinstance(credit_name, dict):
            value = _normalize_text(credit_name.get("value") or "")
            if value:
                return value
        elif isinstance(credit_name, str):
            value = _normalize_text(credit_name)
            if value:
                return value

        given_names = name_node.get("given-names")
        family_name = name_node.get("family-name")
        given_value = _normalize_text(given_names.get("value") if isinstance(given_names, dict) else given_names or "")
        family_value = _normalize_text(family_name.get("value") if isinstance(family_name, dict) else family_name or "")
        combined = _normalize_text(f"{given_value} {family_value}")
        if combined:
            return combined

    given_name = payload.get("givenName")
    family_name = payload.get("familyName")
    given_value = _normalize_text(given_name.get("name") if isinstance(given_name, dict) else given_name or "")
    family_value = _normalize_text(family_name.get("name") if isinstance(family_name, dict) else family_name or "")
    combined = _normalize_text(f"{given_value} {family_value}")
    if combined:
        return combined

    return None


def _lookup_orcid_display_name(orcid_id: Optional[str]) -> Optional[str]:
    """Resolve the public display name for an ORCID by using ORCID's content-negotiated public record."""
    normalized_orcid = _normalize_orcid(orcid_id)
    if not normalized_orcid:
        return None

    if normalized_orcid in _orcid_name_cache:
        return _orcid_name_cache[normalized_orcid]

    try:
        response = get_http_session().get(
            normalized_orcid,
            headers={
                # ORCID documents content negotiation on the registry URL itself, so prefer machine-readable forms.
                "Accept": "application/ld+json, application/json;q=0.9, text/html;q=0.8",
            },
            timeout=20,
        )
        response.raise_for_status()
    except Exception:
        _orcid_name_cache[normalized_orcid] = None
        return None

    resolved_name: Optional[str] = None
    body = response.text
    content_type = response.headers.get("Content-Type", "").lower()

    if "json" in content_type or body.lstrip().startswith("{"):
        try:
            resolved_name = _extract_orcid_display_name(response.json())
        except Exception:
            resolved_name = None

    if not resolved_name and "<script" in body:
        for match in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            body,
            re.IGNORECASE | re.DOTALL,
        ):
            try:
                resolved_name = _extract_orcid_display_name(json.loads(match.group(1).strip()))
            except Exception:
                resolved_name = None
            if resolved_name:
                break

    if not resolved_name:
        meta_match = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            body,
            re.IGNORECASE,
        )
        if meta_match:
            resolved_name = _normalize_text(meta_match.group(1))

    _orcid_name_cache[normalized_orcid] = resolved_name
    return resolved_name


def _resolve_creator_metadata(creator_orcid_id: Optional[str] = None) -> Tuple[str, str]:
    resolved_orcid = _normalize_orcid(creator_orcid_id) or _normalize_orcid(NANOPUB_ORCID_ID)
    resolved_profile_name = _lookup_orcid_display_name(resolved_orcid)

    if not resolved_orcid:
        raise RuntimeError("No creator ORCID is configured. Provide it in the request or set NANOPUB_ORCID_ID.")

    if not resolved_profile_name:
        raise RuntimeError(
            "No public creator name could be resolved from the selected ORCID. Use an ORCID with a public name."
        )

    return resolved_orcid, resolved_profile_name


def get_nanopub_profile() -> Profile:
    """Load the signing profile from `.env` so backend publication never depends on frontend secrets."""
    global _nanopub_profile

    if _nanopub_profile is None:
        missing = []
        if not NANOPUB_PRIVATE_KEY:
            missing.append("NANOPUB_PRIVATE_KEY")
        if not NANOPUB_ORCID_ID:
            missing.append("NANOPUB_ORCID_ID")
        if missing:
            raise RuntimeError(f"Missing nanopub publishing configuration: {', '.join(missing)}")

        agent_uri = get_nanopub_agent_uri()
        signing_uri = agent_uri or _normalize_orcid(NANOPUB_ORCID_ID)
        signing_name = get_nanopub_agent_label() if agent_uri else _lookup_orcid_display_name(signing_uri)
        if not signing_name:
            raise RuntimeError(
                "No signing profile name is available from the configured software-agent intro or ORCID."
            )

        _nanopub_profile = Profile(
            # `nanopub-py` names this argument `orcid_id`, but it writes it directly to `npx:signedBy`.
            # When the private/public key pair belongs to the service, the signer URI must be the service URI.
            orcid_id=signing_uri,
            name=signing_name,
            private_key=_normalize_nanopub_key(NANOPUB_PRIVATE_KEY),
            public_key=_normalize_nanopub_key(NANOPUB_PUBLIC_KEY),
        )

    return _nanopub_profile


def get_nanopub_agent_uri() -> Optional[str]:
    """Resolve the software-agent concept URI from its introduction nanopub once and cache it for reuse."""
    global _nanopub_agent_uri_cache, _nanopub_agent_label_cache

    if _nanopub_agent_uri_cache:
        return _nanopub_agent_uri_cache

    if not NANOPUB_AGENT_INTRO_URI:
        return None

    intro_nanopub = Nanopub(source_uri=NANOPUB_AGENT_INTRO_URI)
    introduced_concept = intro_nanopub.introduces_concept
    if introduced_concept is None:
        raise RuntimeError(
            "Configured NANOPUB_AGENT_INTRO_URI does not introduce a concept. "
            "Provide a valid introduction nanopub for the software agent."
        )

    _nanopub_agent_uri_cache = str(introduced_concept)
    concept_ref = URIRef(_nanopub_agent_uri_cache)
    for graph in (intro_nanopub.assertion, intro_nanopub.pubinfo, intro_nanopub.rdf):
        for predicate in (RDFS.label, SKOS.prefLabel, FOAF.name):
            label = graph.value(concept_ref, predicate)
            if label and str(label).strip():
                _nanopub_agent_label_cache = str(label).strip()
                return _nanopub_agent_uri_cache
    return _nanopub_agent_uri_cache


def get_nanopub_agent_label() -> Optional[str]:
    """Return the software-agent label resolved from the introduction nanopub, falling back to its URI slug."""
    agent_uri = get_nanopub_agent_uri()
    if not agent_uri:
        return None
    if _nanopub_agent_label_cache:
        return _nanopub_agent_label_cache
    slug = urllib.parse.unquote(agent_uri.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1])
    return _normalize_text(slug.replace("_", " ").replace("-", " "))


def get_http_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        _http_session.headers.update({"User-Agent": "IADOPT-Linker/1.0 (+fastapi)"})
    return _http_session


# ======================================================================================
# Prompt building
# ======================================================================================

_EXAMPLE_HDR = "\n\n### Examples (valid against the same schema)\n"
_USER_HDR = "\n\n### Variable's definition to decompose\n"
_EXPECTED_HDR = "\n\n### Expected output\n*(only the JSON object)*"


def list_prompt_versions(prompt_dir: pathlib.Path) -> List[str]:
    if not prompt_dir.exists():
        return []
    return sorted(p.stem for p in prompt_dir.glob("*.txt"))


def load_prompt_instructions(prompt_dir: pathlib.Path, prompt_version: str) -> str:
    versions = list_prompt_versions(prompt_dir)
    if not versions:
        raise RuntimeError(f"No prompt templates found in {prompt_dir}")

    if not prompt_version or prompt_version not in versions:
        prompt_version = versions[0]

    return (prompt_dir / f"{prompt_version}.txt").read_text(encoding="utf-8").strip()


def strip_all_uri_fields(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if "URI" in k:
                continue
            if k.startswith("__"):
                continue
            out[k] = strip_all_uri_fields(v)
        return out
    if isinstance(obj, list):
        return [strip_all_uri_fields(x) for x in obj]
    return obj


def format_example_block(ex: Dict[str, Any], idx: int) -> str:
    definition = ex.get("definition") or ex.get("comment") or ""
    ex_no_uris = strip_all_uri_fields(ex)
    return (
        f"\n\n#### Example {idx}\n"
        f"Variable's definition to decompose: {definition}\n\n"
        f"Expected output:\n{json.dumps(ex_no_uris, indent=2, ensure_ascii=False)}"
    )


def load_examples(folder: pathlib.Path, n: int) -> List[Dict[str, Any]]:
    if n <= 0 or not folder.exists():
        return []
    paths = sorted(folder.glob("*.json"))
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths[:n]]


def build_prompt(definition: str, prompt_version: str, examples: Optional[List[Dict[str, Any]]] = None) -> str:
    examples = examples or []
    instructions = load_prompt_instructions(PROMPT_DIR, prompt_version)
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8").strip() if SCHEMA_PATH.exists() else "{SCHEMA_PLACEHOLDER}"

    ex_block = ""
    if examples:
        blocks = [format_example_block(ex, i + 1) for i, ex in enumerate(examples)]
        ex_block = _EXAMPLE_HDR + "".join(blocks)

    return (
        f"{instructions}\n\n"
        f"### JSON-Schema\n{schema_text}\n"
        f"{ex_block}"
        f"{_USER_HDR}{definition}"
        f"{_EXPECTED_HDR}"
    )


# ======================================================================================
# LLM call + JSON extraction
# ======================================================================================


def call_model(model: str, prompt: str, temperature: float, disable_thinking: bool = True) -> str:
    client = get_openai_client()

    for attempt in range(1, 4):
        try:
            resp = client.chat.completions.create(
                **_build_chat_completion_request_kwargs(
                    model,
                    prompt,
                    temperature,
                    disable_thinking=disable_thinking,
                )
            )
            text = resp.choices[0].message.content or ""
            stripped = text.strip()

            if stripped.startswith("<!DOCTYPE html") or stripped.startswith("<html"):
                continue
            if not stripped:
                continue

            return text

        except APIStatusError as e:
            print(f"APIStatusError attempt {attempt}: {e}")
        except (OpenAIError, httpx.HTTPError) as e:
            print(f"Transport error attempt {attempt}: {e}")
        except Exception as e:
            print(f"Unexpected error attempt {attempt}: {e}")

    return ""


def _build_psnc_chat_payload(
    model: str,
    prompt: str,
    temperature: float,
    *,
    disable_thinking: bool = True,
    stream: bool = False,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
    }

    if disable_thinking:
        # PSNC is LiteLLM-compatible, but Qwen3.5 thinking is controlled by the model chat template.
        # Send both known request-level switches used by Qwen-compatible providers:
        # DashScope-style `enable_thinking` and vLLM-style `chat_template_kwargs`.
        payload["enable_thinking"] = False
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    return payload


def _psnc_chat_headers() -> Dict[str, str]:
    if not PSNC_API_KEY:
        raise RuntimeError("PSNC_API_KEY is not set.")

    return {
        "Authorization": f"Bearer {PSNC_API_KEY}",
        "Content-Type": "application/json",
    }


def _psnc_chat_completions_url() -> str:
    return f"{PSNC_API_BASE_URL.rstrip('/')}/v1/chat/completions"


def _psnc_rerank_url() -> str:
    return f"{PSNC_API_BASE_URL.rstrip('/')}/v1/rerank"


def call_psnc_reranker(query: str, documents: List[str]) -> List[float]:
    if not documents:
        return []

    response = get_http_session().post(
        _psnc_rerank_url(),
        headers=_psnc_chat_headers(),
        json={
            "model": PSNC_RERANK_MODEL,
            "query": query,
            "documents": documents,
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    raw_results = payload.get("results") if isinstance(payload, dict) else None

    if not isinstance(raw_results, list) or len(raw_results) != len(documents):
        raise RuntimeError("PSNC reranker response did not contain one score per document.")

    scores = [0.0] * len(documents)
    for result in raw_results:
        if not isinstance(result, dict):
            raise RuntimeError("PSNC reranker response contained an invalid result.")
        index = int(result.get("index", -1))
        if index < 0 or index >= len(documents):
            raise RuntimeError("PSNC reranker response contained an invalid document index.")
        scores[index] = float(result.get("relevance_score"))

    return scores


def _extract_chat_completion_text(data: Dict[str, Any]) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ""

    message = first_choice.get("message")
    if isinstance(message, dict):
        return _flatten_text_fragments(message.get("content"))

    return _flatten_text_fragments(first_choice.get("text"))


def call_psnc_model(model: str, prompt: str, temperature: float, disable_thinking: bool = True) -> str:
    url = _psnc_chat_completions_url()
    headers = _psnc_chat_headers()

    for attempt in range(1, 4):
        try:
            response = get_http_session().post(
                url,
                headers=headers,
                json=_build_psnc_chat_payload(
                    model,
                    prompt,
                    temperature,
                    disable_thinking=disable_thinking,
                ),
                timeout=120,
            )
            response.raise_for_status()
            text = _extract_chat_completion_text(response.json())
            stripped = text.strip()

            if stripped.startswith("<!DOCTYPE html") or stripped.startswith("<html"):
                continue
            if not stripped:
                continue

            return text
        except requests.HTTPError as e:
            response_text = getattr(e.response, "text", "")
            print(f"PSNC HTTP error attempt {attempt}: {e} {response_text[:500]}")
        except requests.RequestException as e:
            print(f"PSNC transport error attempt {attempt}: {e}")
        except Exception as e:
            print(f"Unexpected PSNC error attempt {attempt}: {e}")

    return ""


def coerce_prediction(pred: Dict[str, Any]) -> Dict[str, Any]:
    pred = dict(pred or {})

    for k in ONTO_KEYS:
        if k not in pred or pred[k] is None:
            pred[k] = [] if k == "hasConstraint" else ""
        elif k == "hasConstraint" and not isinstance(pred[k], list):
            pred[k] = []

    if isinstance(pred.get("hasProperty"), dict):
        pred["hasProperty"] = pred["hasProperty"].get("label", "") or ""

    return pred


def parse_llm_json(raw: str, definition: str) -> Dict[str, Any]:
    cleaned = _JSON_FENCE_RE.sub("", raw).strip()
    match = _JSON_BLOCK_RE.search(cleaned)

    if not match:
        raise ValueError("No JSON object found in model output.")

    try:
        data = json.loads(match.group(0))
    except Exception as e:
        raise ValueError(f"JSON decode failure: {e}") from e

    data["definition"] = definition
    return coerce_prediction(data)


def _resolve_model_provider(requested_provider: Optional[str]) -> str:
    provider = (requested_provider or DEFAULT_MODEL_PROVIDER).strip().lower()

    if not provider:
        provider = DEFAULT_MODEL_PROVIDER

    if provider not in ENABLED_MODEL_PROVIDERS:
        raise ValueError(
            f"Model provider '{provider}' is not enabled. " f"Enabled providers: {', '.join(ENABLED_MODEL_PROVIDERS)}"
        )

    return provider


def _resolve_model_name(requested_model_name: Optional[str], model_provider: str = DEFAULT_MODEL_PROVIDER) -> str:
    default_model = PSNC_MODEL_NAME if model_provider == PSNC_MODEL_PROVIDER else MODEL_NAME
    allowed_models = PSNC_MODEL_NAMES if model_provider == PSNC_MODEL_PROVIDER else MODEL_NAMES
    selected_model = (requested_model_name or default_model).strip()

    if not selected_model:
        selected_model = default_model

    if selected_model not in allowed_models:
        raise ValueError(f"Unsupported model '{selected_model}'. Allowed models: {', '.join(allowed_models)}")

    return selected_model


def _build_chat_completion_request_kwargs(
    model: str,
    prompt: str,
    temperature: float,
    *,
    disable_thinking: bool = True,
    stream: bool = False,
) -> Dict[str, Any]:
    request_kwargs: Dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
        "timeout": 60,
    }

    if stream:
        request_kwargs["stream"] = True

    # The default mode intentionally sends no reasoning override so the provider keeps its normal thinking behavior.
    if disable_thinking:
        request_kwargs["extra_body"] = {
            "reasoning": {
                "effort": "none",
            }
        }

    return request_kwargs


def _as_plain_data(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(exclude_none=True)
        except Exception:
            return value
    if isinstance(value, list):
        return [_as_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_as_plain_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _as_plain_data(item) for key, item in value.items()}
    return value


def _flatten_text_fragments(value: Any) -> str:
    plain_value = _as_plain_data(value)

    if plain_value is None:
        return ""
    if isinstance(plain_value, str):
        return plain_value
    if isinstance(plain_value, list):
        return "".join(_flatten_text_fragments(item) for item in plain_value)
    if isinstance(plain_value, dict):
        fragments: List[str] = []

        for key in ("text", "summary", "reasoning"):
            if key in plain_value:
                fragments.append(_flatten_text_fragments(plain_value[key]))

        if fragments:
            return "".join(fragments)

    return ""


def _extract_stream_text_deltas(chunk: Any) -> Tuple[str, str]:
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return "", ""

    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return "", ""

    content_delta = _flatten_text_fragments(getattr(delta, "content", None))

    reasoning_fragments: List[str] = []
    for attr_name in ("reasoning", "reasoning_content", "reasoning_text"):
        reasoning_fragments.append(_flatten_text_fragments(getattr(delta, attr_name, None)))

    reasoning_fragments.append(_flatten_text_fragments(getattr(delta, "reasoning_details", None)))

    reasoning_delta = "".join(fragment for fragment in reasoning_fragments if fragment)
    return reasoning_delta, content_delta


def _extract_stream_text_deltas_from_dict(data: Dict[str, Any]) -> Tuple[str, str]:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return "", ""

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return "", ""

    delta = first_choice.get("delta")
    if isinstance(delta, dict):
        content_delta = _flatten_text_fragments(delta.get("content"))
        reasoning_delta = "".join(
            _flatten_text_fragments(delta.get(key))
            for key in ("reasoning", "reasoning_content", "reasoning_text", "reasoning_details")
        )
        return reasoning_delta, content_delta

    message = first_choice.get("message")
    if isinstance(message, dict):
        content_delta = _flatten_text_fragments(message.get("content"))
        reasoning_delta = "".join(
            _flatten_text_fragments(message.get(key))
            for key in ("reasoning", "reasoning_content", "reasoning_text", "reasoning_details")
        )
        return reasoning_delta, content_delta

    text_delta = _flatten_text_fragments(first_choice.get("text"))
    return "", text_delta


def _stream_psnc_model(
    model: str,
    prompt: str,
    temperature: float,
    *,
    disable_thinking: bool = True,
) -> Iterator[Tuple[str, str]]:
    response = get_http_session().post(
        _psnc_chat_completions_url(),
        headers=_psnc_chat_headers(),
        json=_build_psnc_chat_payload(
            model,
            prompt,
            temperature,
            disable_thinking=disable_thinking,
            stream=True,
        ),
        stream=True,
        timeout=120,
    )
    response.raise_for_status()

    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue

        line = raw_line.strip()
        if line.startswith("data:"):
            line = line.removeprefix("data:").strip()

        if not line or line == "[DONE]":
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        yield _extract_stream_text_deltas_from_dict(event)


def _stream_event(event_type: str, **payload: Any) -> str:
    return json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "\n"


def call_llm_loose(
    model_provider: str,
    model: str,
    prompt: str,
    definition: str,
    temperature: float,
    disable_thinking: bool = True,
) -> Tuple[str, Dict[str, Any]]:
    last_raw = ""

    for attempt in range(1, 4):
        raw = (
            call_psnc_model(model, prompt, temperature, disable_thinking=disable_thinking)
            if model_provider == PSNC_MODEL_PROVIDER
            else call_model(model, prompt, temperature, disable_thinking=disable_thinking)
        )
        last_raw = raw

        if not raw.strip():
            continue

        try:
            data = parse_llm_json(raw, definition)
            return raw, data
        except Exception as e:
            print(f"LLM parse attempt {attempt} failed: {e}")

    return last_raw, {}


def _finalize_pipeline_output(
    raw_llm_output: str,
    pred: Dict[str, Any],
    *,
    creator_orcid_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not pred:
        raise RuntimeError("Could not extract valid JSON from the model output.")

    validation_errors = get_schema_validation_errors(pred, label_for_logs=pred.get("label"))
    validation_errors.extend(_get_constraint_semantic_validation_errors(pred))
    schema_valid = len(validation_errors) == 0

    if ENABLE_WIKIDATA_LINKING:
        try:
            enriched = enrich_with_uris_reranker(pred, threshold=RERANK_THRESHOLD)
        except Exception as e:
            print(f"Wikidata enrichment failed: {e}")
            enriched = pred
    else:
        enriched = pred

    ttl = json_to_ttl_repo_style(
        enriched,
        creator_orcid_id=creator_orcid_id,
    )

    return {
        "raw_llm_output": raw_llm_output,
        "parsed_json": pred,
        "schema_valid": schema_valid,
        "validation_errors": validation_errors,
        "enriched_json": enriched,
        "ttl": ttl,
    }


# ======================================================================================
# JSON Schema validation
# ======================================================================================


def _format_path(err) -> str:
    if not err.path:
        return "$"
    out = "$"
    for p in err.path:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += f".{p}"
    return out


def _safe_preview(value: Any, limit: int = 200) -> str:
    try:
        s = json.dumps(value, ensure_ascii=False)
    except Exception:
        s = repr(value)
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def _patch_schema_for_pipeline(schema: Dict[str, Any]) -> Dict[str, Any]:
    patched = copy.deepcopy(schema)

    try:
        hc = patched["properties"]["hasConstraint"]
        if isinstance(hc, dict) and hc.get("minItems", None) == 1:
            hc["minItems"] = 0
    except Exception:
        pass

    return patched


def load_schema(schema_path: pathlib.Path) -> Dict[str, Any]:
    if not schema_path.exists():
        raise RuntimeError(f"Schema file not found: {schema_path}")
    return json.loads(schema_path.read_text(encoding="utf-8"))


def get_schema_validation_errors(
    instance: Dict[str, Any],
    *,
    schema_path: pathlib.Path = SCHEMA_PATH,
    schema: Optional[Dict[str, Any]] = None,
    label_for_logs: Optional[str] = None,
) -> List[str]:
    if schema is not None:
        validator = Draft202012Validator(_patch_schema_for_pipeline(schema))
    elif _validator_cache is not None:
        validator = _validator_cache
    else:
        schema = _patch_schema_for_pipeline(load_schema(schema_path))
        validator = Draft202012Validator(schema)

    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))

    if not errors:
        return []

    header = "Schema validation failed"
    if label_for_logs:
        header += f" for variable: {label_for_logs}"

    lines: List[str] = [header, "-" * len(header)]

    max_errs = 30
    for i, err in enumerate(errors[:max_errs], start=1):
        path = _format_path(err)
        offending_value = _safe_preview(err.instance)

        lines.append(f"{i:02d}) Path: {path}")
        lines.append(f"    Error: {err.message}")
        lines.append(f"    Offending value: {offending_value}")

        if (
            path.startswith("$.hasObjectOfInterest")
            or path.startswith("$.hasMatrix")
            or path.startswith("$.hasContextObject")
        ):
            lines.append(
                "    Hint: This error is inside an entityOrSystem field "
                "(string vs AsymmetricSystem vs SymmetricSystem)."
            )

    if len(errors) > max_errs:
        lines.append(f"... plus {len(errors) - max_errs} more errors.")

    return lines


# ======================================================================================
# Wikidata linking
# ======================================================================================


def _qid_from_uri_or_text(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = re.search(r"(Q\d+)", s)
    return m.group(1) if m else None


def _to_wiki_url(uri: Optional[str]) -> Optional[str]:
    if not uri:
        return None
    q = _qid_from_uri_or_text(uri)
    return f"https://www.wikidata.org/wiki/{q}" if q else uri.strip().replace("http://", "https://")


def get_wikidata_entity_reranker(
    term: str,
    context: str = "",
    threshold: float = RERANK_THRESHOLD,
) -> Optional[str]:
    if not term:
        return None

    encoded = urllib.parse.quote_plus(term)
    # headers = {"User-Agent": "IADOPT-Linker/1.0 (+fastapi)"}
    url = "https://www.wikidata.org/w/api.php" f"?action=wbsearchentities&search={encoded}&language=en&format=json"

    response = get_http_session().get(url, timeout=20)

    if response.status_code != 200:
        return None

    search = response.json().get("search", [])
    if not search:
        return None

    query = f'Definition of "{term}" in context: "{context}"'
    documents = [f'label: "{s.get("label", "")}", description: "{s.get("description", "")}"' for s in search]

    scores = call_psnc_reranker(query, documents)

    ranked = sorted(zip(search, scores), key=lambda x: float(x[1]), reverse=True)
    best_s, best_score = ranked[0]

    return _to_wiki_url(best_s["id"]) if float(best_score) >= float(threshold) else None


def enrich_with_uris_reranker(pred: Dict[str, Any], threshold: float = RERANK_THRESHOLD) -> Dict[str, Any]:
    out = json.loads(json.dumps(pred))

    def add_uri_field(container: Dict[str, Any], key: str, label_value: Any):
        if isinstance(label_value, str) and label_value.strip():
            uri = get_wikidata_entity_reranker(
                label_value,
                context=pred.get("definition", ""),
                threshold=threshold,
            )
            if uri:
                container[f"{key}URI"] = _to_wiki_url(uri)

    for p in ["hasProperty", "hasMatrix", "hasObjectOfInterest", "hasContextObject", "hasStatisticalModifier"]:
        if p in out and isinstance(out[p], str):
            add_uri_field(out, p, out[p])

    for p in ["hasMatrix", "hasObjectOfInterest", "hasContextObject"]:
        val = out.get(p)
        if isinstance(val, dict):
            if "AsymmetricSystem" in val:
                # Link both system-level and component-level asymmetric system labels so the serializer
                # can emit readable labels and URIs for all formula variants.
                for kk in ["AsymmetricSystem", "hasSource", "hasTarget", "hasNumerator", "hasDenominator"]:
                    if val.get(kk):
                        uri = get_wikidata_entity_reranker(
                            val[kk],
                            context=pred.get("definition", ""),
                            threshold=threshold,
                        )
                        if uri:
                            val[f"{kk}URI"] = _to_wiki_url(uri)

            if "SymmetricSystem" in val:
                if val.get("SymmetricSystem"):
                    uri = get_wikidata_entity_reranker(
                        val["SymmetricSystem"],
                        context=pred.get("definition", ""),
                        threshold=threshold,
                    )
                    if uri:
                        val["SymmetricSystemURI"] = _to_wiki_url(uri)

                parts = val.get("hasPart", [])
                if isinstance(parts, list) and parts:
                    part_uris = []
                    for part in parts:
                        if isinstance(part, str) and part.strip():
                            uri = get_wikidata_entity_reranker(
                                part,
                                context=pred.get("definition", ""),
                                threshold=threshold,
                            )
                            part_uris.append(_to_wiki_url(uri) if uri else None)
                        else:
                            part_uris.append(None)

                    if any(part_uris):
                        val["hasPartURIs"] = part_uris

    return out


# ======================================================================================
# JSON -> TTL
# ======================================================================================

TTL_PREFIXES = """@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix pav: <http://purl.org/pav/> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix orcid: <https://orcid.org/> .
@prefix fdof: <https://w3id.org/fdof/ontology#> .

"""

WIKIDATA_ENTITY = "https://www.wikidata.org/entity/"
IADOPT_VARIABLE_BASE = "https://w3id.org/iadopt/variable/"


def wiki_to_entity(uri: Optional[str]) -> Optional[str]:
    """Normalize Wikidata page URLs into entity URLs so the TTL always points at the canonical resource."""
    if not uri:
        return None
    m = re.search(r"(Q\d+)", uri)
    if not m:
        return None
    return WIKIDATA_ENTITY + m.group(1)


def _ttl_quote(text: str) -> str:
    """Escape arbitrary text once so labels, comments, and definitions stay valid Turtle literals."""
    return json.dumps((text or "").strip(), ensure_ascii=False)


def _normalize_text(text: str) -> str:
    """Collapse repeated whitespace so generated labels read naturally and consistently."""
    return re.sub(r"\s+", " ", (text or "").strip())


def _lookup_key(text: str) -> str:
    """Normalize label lookups so constraints can resolve targets by human-readable names."""
    return _normalize_text(text).lower()


def _normalize_constraint_phrase_for_alt_label(label: str) -> str:
    """Convert extracted constraint labels into natural phrases for alt-label assembly without changing the TTL label."""
    clean_label = _normalize_text(label)

    if re.match(r"^location\s*:\s*", clean_label, re.IGNORECASE):
        clean_label = re.sub(r"^location\s*:\s*", "", clean_label, flags=re.IGNORECASE)
        if clean_label and not re.match(
            r"^(at|in|on|near|above|below|under|over|within|outside|around)\b", clean_label, re.IGNORECASE
        ):
            clean_label = f"at {clean_label}"

    return clean_label


def _collect_constraint_target_keys(pred: Dict[str, Any]) -> List[str]:
    """Return the normalized labels of the actual property/entity targets that constraints are allowed to point at."""
    keys: List[str] = []

    def add_value(value: Any) -> None:
        if isinstance(value, str):
            clean_value = _lookup_key(value)
            if clean_value:
                keys.append(clean_value)
            return

        if not isinstance(value, dict):
            return

        for field_name in (
            "AsymmetricSystem",
            "SymmetricSystem",
            "hasSource",
            "hasTarget",
            "hasNumerator",
            "hasDenominator",
        ):
            add_value(value.get(field_name))

        for part_label in value.get("hasPart") or []:
            add_value(part_label)

    for field_name in (
        "hasProperty",
        "hasObjectOfInterest",
        "hasStatisticalModifier",
        "hasMatrix",
        "hasContextObject",
    ):
        add_value(pred.get(field_name))

    seen = set()
    ordered: List[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)

    return ordered


def _get_constraint_semantic_validation_errors(pred: Dict[str, Any]) -> List[str]:
    """Flag constraints whose `on` target does not match any real property/entity label in the prediction."""
    allowed_targets = _collect_constraint_target_keys(pred)
    if not allowed_targets:
        return []

    errors: List[str] = []
    for idx, constraint in enumerate(pred.get("hasConstraint") or [], start=1):
        if not isinstance(constraint, dict):
            continue

        constraint_on = _lookup_key(constraint.get("on") or "")
        if not constraint_on or constraint_on in allowed_targets:
            continue

        errors.append(
            f"Constraint target error at $.hasConstraint[{idx - 1}].on: "
            f"'{constraint.get('on')}' does not match any extracted property/entity label. "
            f"Allowed targets: {', '.join(allowed_targets)}"
        )

    return errors


def _make_variable_identity() -> Tuple[str, str, str]:
    """Create the new variable URI, its textual identifier, and the UTC timestamp literal from one clock read."""
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    identifier_suffix = f"{created_at.strftime('%Y%m%dT%H%M%S')}-{random.randint(0, 99):02d}"
    variable_uri = f"{IADOPT_VARIABLE_BASE}{identifier_suffix}"
    variable_identifier = f"iadopt-variable-{identifier_suffix}"
    created_literal = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return variable_uri, variable_identifier, created_literal


def _format_main_label(pref_label: str) -> str:
    """Promote the LLM label into a human-readable main label while preserving the original wording."""
    pref_label = _normalize_text(pref_label)
    if not pref_label:
        return "Generated variable"
    return pref_label[:1].upper() + pref_label[1:]


def _make_comment(formula_name: str) -> str:
    """Explain directly in the TTL how the preferred and alternative labels were produced."""
    return (
        "LLM-proposed preferred label is stored in skos:prefLabel. "
        f"The alternative label is generated from the {formula_name} formula."
    )


def _literal_join(parts: List[str]) -> str:
    """Join only the non-empty text fragments and normalize the result for use as a label phrase."""
    return _normalize_text(" ".join(part for part in parts if part))


def _phrase_for_role(role: str, label: str, constraints_by_role: Dict[str, List[str]]) -> str:
    """Place qualifier text before properties/modifiers and after entities so labels stay readable."""
    clean_label = _normalize_text(label)
    if not clean_label:
        return ""

    clean_constraints = [_normalize_text(item) for item in constraints_by_role.get(role, []) if _normalize_text(item)]
    if not clean_constraints:
        return clean_label

    constraint_text = " ".join(clean_constraints)
    if role in {"property", "statistical_modifier"}:
        return _literal_join([constraint_text, clean_label])
    return _literal_join([clean_label, constraint_text])


def _build_alt_label(formula_context: Dict[str, str], constraints_by_role: Dict[str, List[str]]) -> Tuple[str, str]:
    """Select the matching label formula and assemble the final `skos:altLabel` text."""
    uses_ooi_asymmetric = formula_context.get("ooi_kind") == "asymmetric"
    uses_matrix_asymmetric = formula_context.get("matrix_kind") == "asymmetric"

    if uses_ooi_asymmetric and formula_context.get("numerator") and formula_context.get("denominator"):
        formula_name = "asymmetric-numerator-denominator"
        phrase_plan = [
            (
                _phrase_for_role(
                    "statistical_modifier", formula_context.get("statistical_modifier", ""), constraints_by_role
                ),
                None,
            ),
            (_phrase_for_role("property", formula_context.get("property", ""), constraints_by_role), None),
            (_phrase_for_role("numerator", formula_context.get("numerator", ""), constraints_by_role), "of"),
            (_phrase_for_role("denominator", formula_context.get("denominator", ""), constraints_by_role), "in"),
            (_phrase_for_role("matrix", formula_context.get("matrix", ""), constraints_by_role), "in"),
            (_phrase_for_role("context", formula_context.get("context", ""), constraints_by_role), "in"),
        ]
    elif uses_ooi_asymmetric and formula_context.get("source") and formula_context.get("target"):
        formula_name = "asymmetric-source-target-object"
        phrase_plan = [
            (
                _phrase_for_role(
                    "statistical_modifier", formula_context.get("statistical_modifier", ""), constraints_by_role
                ),
                None,
            ),
            (_phrase_for_role("property", formula_context.get("property", ""), constraints_by_role), None),
            (_phrase_for_role("source", formula_context.get("source", ""), constraints_by_role), "from"),
            (_phrase_for_role("target", formula_context.get("target", ""), constraints_by_role), "to"),
            (_phrase_for_role("matrix", formula_context.get("matrix", ""), constraints_by_role), "in"),
            (_phrase_for_role("context", formula_context.get("context", ""), constraints_by_role), "in"),
        ]
    elif uses_matrix_asymmetric and formula_context.get("source") and formula_context.get("target"):
        formula_name = "asymmetric-source-target-matrix"
        phrase_plan = [
            (
                _phrase_for_role(
                    "statistical_modifier", formula_context.get("statistical_modifier", ""), constraints_by_role
                ),
                None,
            ),
            (_phrase_for_role("property", formula_context.get("property", ""), constraints_by_role), None),
            (_phrase_for_role("object", formula_context.get("object", ""), constraints_by_role), "of"),
            (_phrase_for_role("source", formula_context.get("source", ""), constraints_by_role), "from"),
            (_phrase_for_role("target", formula_context.get("target", ""), constraints_by_role), "to"),
            (_phrase_for_role("context", formula_context.get("context", ""), constraints_by_role), "in"),
        ]
    else:
        formula_name = "simple-entity"
        phrase_plan = [
            (
                _phrase_for_role(
                    "statistical_modifier", formula_context.get("statistical_modifier", ""), constraints_by_role
                ),
                None,
            ),
            (_phrase_for_role("property", formula_context.get("property", ""), constraints_by_role), None),
            (_phrase_for_role("object", formula_context.get("object", ""), constraints_by_role), "of"),
            (_phrase_for_role("matrix", formula_context.get("matrix", ""), constraints_by_role), "in"),
            (_phrase_for_role("context", formula_context.get("context", ""), constraints_by_role), "in"),
        ]

    assembled: List[str] = []
    for phrase, connector in phrase_plan:
        if not phrase:
            continue
        if connector and assembled:
            assembled.append(connector)
        assembled.append(phrase)

    alt_label = _literal_join(assembled)
    return alt_label or _normalize_text(formula_context.get("pref_label", "")), formula_name


def json_to_ttl_repo_style(
    pred: Dict[str, Any],
    *,
    creator_orcid_id: Optional[str] = None,
) -> str:
    """Serialize the enriched JSON prediction into the new simple I-ADOPT TTL shape required by the frontend."""
    pref_label = _normalize_text(pred.get("label") or "generated variable")
    main_label = _format_main_label(pref_label)
    definition = _normalize_text(pred.get("definition") or "")
    comment = _normalize_text(pred.get("comment") or "")
    resolved_orcid, resolved_profile_name = _resolve_creator_metadata(creator_orcid_id)
    variable_uri, variable_identifier, created_literal = _make_variable_identity()
    orcid_suffix = _orcid_suffix(resolved_orcid) or "0000-0000-0000-0000"

    blocks: List[str] = []
    variable_lines: List[str] = []
    constraint_targets: Dict[str, Tuple[str, str]] = {}
    constraints_by_role: Dict[str, List[str]] = {}
    formula_context: Dict[str, str] = {
        "pref_label": pref_label,
        "ooi_kind": "simple",
        "matrix_kind": "simple",
    }

    def local_resource_ref(suffix: str) -> str:
        return f"<{variable_uri}#{suffix}>"

    def register_target(ref: str, role: str, *aliases: Optional[str]) -> None:
        # This lookup table lets constraint `on` values resolve against either field names or human-readable labels.
        for alias in aliases:
            if alias:
                constraint_targets[_lookup_key(alias)] = (ref, role)

    def add_block(
        ref: str, rdf_types: List[str], label: Optional[str], extra_lines: Optional[List[str]] = None
    ) -> None:
        # Every linked resource gets its own readable TTL block so the frontend receives a self-contained graph.
        lines = [f"{ref}", "    a " + " ,\n      ".join(rdf_types) + " ;"]
        if label:
            lines.append(f"    rdfs:label {_ttl_quote(label)} ;")
        for extra_line in extra_lines or []:
            lines.append(extra_line)
        # Close the block by replacing the last semicolon with a final period.
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        blocks.append("\n".join(lines))

    def build_simple_component(field: str, label: str, rdf_type: str, uri_override: Optional[str]) -> Tuple[str, str]:
        clean_label = _normalize_text(label)
        ref = f"<{uri_override}>" if uri_override else local_resource_ref(field)
        add_block(ref, [rdf_type], clean_label)
        return ref, clean_label

    def build_system_component(field: str, value: Dict[str, Any], role_name: str) -> Tuple[str, str]:
        system_key = "AsymmetricSystem" if "AsymmetricSystem" in value else "SymmetricSystem"
        system_uri = wiki_to_entity(value.get(f"{system_key}URI"))
        system_ref = f"<{system_uri}>" if system_uri else local_resource_ref(field)
        component_lines: List[str] = []
        # The system label is derived from its components in canonical order (first part + second
        # part), so every system is labelled consistently rather than from a per-variable phrase.
        component_labels: List[str] = []
        kind_key = "ooi_kind" if role_name == "object" else "matrix_kind" if role_name == "matrix" else f"{field}_kind"

        if system_key == "AsymmetricSystem":
            # Source/target and numerator/denominator resources are emitted explicitly so constraints
            # and alt-label formulas can target them individually.
            formula_context[kind_key] = "asymmetric"
            asym_roles = [
                ("hasSource", "source", f"{field}-source"),
                ("hasTarget", "target", f"{field}-target"),
                ("hasNumerator", "numerator", f"{field}-numerator"),
                ("hasDenominator", "denominator", f"{field}-denominator"),
            ]
            for key, role_name, suffix in asym_roles:
                role_label = _normalize_text(value.get(key) or "")
                if not role_label:
                    continue
                role_uri = wiki_to_entity(value.get(f"{key}URI"))
                role_ref, clean_role_label = build_simple_component(suffix, role_label, "iop:Entity", role_uri)
                component_lines.append(f"    iop:{key} {role_ref} ;")
                component_labels.append(clean_role_label)
                formula_context[role_name] = clean_role_label
                register_target(role_ref, role_name, key, role_name, clean_role_label)

            system_label = " ".join(component_labels) or _normalize_text(value.get(system_key) or field)
            add_block(system_ref, ["iop:Entity", "iop:AsymmetricSystem"], system_label, component_lines)
        else:
            formula_context[kind_key] = "symmetric"
            part_refs: List[str] = []
            part_uris = value.get("hasPartURIs") if isinstance(value.get("hasPartURIs"), list) else []
            for idx, part_label in enumerate(value.get("hasPart") or [], start=1):
                clean_part_label = _normalize_text(part_label)
                if not clean_part_label:
                    continue
                part_uri = wiki_to_entity(part_uris[idx - 1]) if idx - 1 < len(part_uris) else None
                part_ref, _ = build_simple_component(f"{field}-part-{idx}", clean_part_label, "iop:Entity", part_uri)
                part_refs.append(part_ref)
                component_labels.append(clean_part_label)
                register_target(part_ref, f"{field}_part", clean_part_label)

            if part_refs:
                component_lines.append(f"    iop:hasPart {', '.join(part_refs)} ;")
            system_label = " ".join(component_labels) or _normalize_text(value.get(system_key) or field)
            add_block(system_ref, ["iop:Entity", "iop:SymmetricSystem"], system_label, component_lines)

        return system_ref, system_label

    def build_component(field: str, rdf_type: str, role_name: str) -> Tuple[Optional[str], str]:
        # This one function keeps the simple-entity and system cases aligned so later label
        # generation and constraint resolution work from the same canonical context.
        value = pred.get(field)
        if isinstance(value, str) and _normalize_text(value):
            uri = wiki_to_entity(pred.get(f"{field}URI"))
            ref, label = build_simple_component(field, value, rdf_type, uri)
            formula_context[role_name] = label
            register_target(ref, role_name, field, role_name, label)
            return ref, label

        if isinstance(value, dict):
            ref, label = build_system_component(field, value, role_name)
            formula_context[role_name] = label
            register_target(
                ref, role_name, field, role_name, label, value.get("AsymmetricSystem"), value.get("SymmetricSystem")
            )
            return ref, label

        return None, ""

    property_ref, _ = build_component("hasProperty", "iop:Property", "property")
    stat_ref, _ = build_component("hasStatisticalModifier", "iop:StatisticalModifier", "statistical_modifier")
    ooi_ref, _ = build_component("hasObjectOfInterest", "iop:Entity", "object")
    matrix_ref, _ = build_component("hasMatrix", "iop:Entity", "matrix")
    context_ref, _ = build_component("hasContextObject", "iop:Entity", "context")

    constraint_refs: List[str] = []
    for idx, constraint in enumerate(pred.get("hasConstraint") or [], start=1):
        if not isinstance(constraint, dict):
            continue

        constraint_on_raw = constraint.get("on") or ""
        constraint_label = _normalize_text(constraint.get("label") or "")
        alt_constraint_label = _normalize_constraint_phrase_for_alt_label(constraint_label)
        constraint_on = _lookup_key(constraint.get("on") or "")
        if not constraint_label or not constraint_on:
            continue

        target_ref, target_role = constraint_targets.get(constraint_on, (None, None))
        if not target_ref or not target_role:
            continue

        constraints_by_role.setdefault(target_role, []).append(alt_constraint_label)
        constraint_ref = f"_:c{idx}"
        constraint_refs.append(constraint_ref)
        blocks.append(
            "\n".join(
                [
                    f"{constraint_ref}",
                    "    a iop:Constraint ;",
                    f"    rdfs:label {_ttl_quote(constraint_label)} ;",
                    f"    iop:constrains {target_ref} .",
                ]
            )
        )

    alt_label, formula_name = _build_alt_label(formula_context, constraints_by_role)
    ttl_comment = comment or _make_comment(formula_name)

    variable_lines.extend(
        [
            f"<{variable_uri}>",
            "    a fdof:FAIRDigitalObject ,",
            "      iop:Variable ;",
            f"    dct:conformsTo <{IADOPT_VARIABLE_CONFORMS_TO}> ;",
            f"    rdfs:label {_ttl_quote(main_label)} ;",
            f"    skos:prefLabel {_ttl_quote(pref_label)} ;",
            f"    skos:altLabel {_ttl_quote(alt_label)} ;",
            f"    skos:definition {_ttl_quote(definition)} ;",
            f"    rdfs:comment {_ttl_quote(ttl_comment)} ;",
            # The identifier is the resolvable variable IRI itself so the published id always resolves.
            f"    dct:identifier <{variable_uri}> ;",
            f'    dct:created "{created_literal}"^^xsd:dateTime ;',
            f"    dct:creator orcid:{orcid_suffix} ;",
            f"    pav:createdWith {_ttl_quote(IADOPT_CREATED_WITH_LABEL)} ;",
            f"    prov:wasAttributedTo orcid:{orcid_suffix} ;",
        ]
    )

    if ooi_ref:
        variable_lines.append(f"    iop:hasObjectOfInterest {ooi_ref} ;")
    if property_ref:
        variable_lines.append(f"    iop:hasProperty {property_ref} ;")
    if matrix_ref:
        variable_lines.append(f"    iop:hasMatrix {matrix_ref} ;")
    if context_ref:
        variable_lines.append(f"    iop:hasContextObject {context_ref} ;")
    if stat_ref:
        variable_lines.append(f"    iop:hasStatisticalModifier {stat_ref} ;")
    if constraint_refs:
        variable_lines.append(f"    iop:hasConstraint {', '.join(constraint_refs)} ;")

    variable_lines[-1] = variable_lines[-1].rstrip(" ;") + " ."

    creator_block = "\n".join(
        [
            f"orcid:{orcid_suffix}",
            f"    rdfs:label {_ttl_quote(resolved_profile_name)} .",
        ]
    )

    return "\n".join([TTL_PREFIXES, "\n".join(variable_lines), "", *blocks, creator_block, ""])


# ======================================================================================
# Main pipeline
# ======================================================================================


def _prepare_pipeline_inputs(
    definition: str,
    model_name: Optional[str] = None,
    model_provider: Optional[str] = None,
) -> Tuple[str, str, str]:
    definition = definition.strip()
    if not definition:
        raise ValueError("Definition must not be empty.")

    prompt_version = _prompt_version_cache
    if not prompt_version:
        prompt_versions = list_prompt_versions(PROMPT_DIR)
        if not prompt_versions:
            raise RuntimeError(f"No prompt files found in: {PROMPT_DIR}")
        prompt_version = prompt_versions[0]

    examples_5 = _examples_5_cache if _examples_5_cache is not None else load_examples(FIVE_SHOT_DIR, 5)
    prompt = build_prompt(definition, prompt_version=prompt_version, examples=examples_5)
    selected_model_provider = _resolve_model_provider(model_provider)
    selected_model_name = _resolve_model_name(model_name, model_provider=selected_model_provider)

    return prompt, selected_model_provider, selected_model_name


def stream_pipeline_events(
    definition: str,
    *,
    disable_thinking: bool = True,
    model_provider: Optional[str] = None,
    model_name: Optional[str] = None,
    creator_orcid_id: Optional[str] = None,
) -> Iterator[str]:
    try:
        definition = definition.strip()
        prompt, selected_model_provider, selected_model_name = _prepare_pipeline_inputs(
            definition,
            model_name=model_name,
            model_provider=model_provider,
        )
        all_display_parts: List[str] = []
        last_error_message = "Could not extract valid JSON from the model output."

        for attempt in range(1, 4):
            attempt_display_parts: List[str] = []
            attempt_content_parts: List[str] = []
            saw_reasoning = False
            started_content = False
            streamed_any_chunk = False

            if attempt > 1:
                retry_note = "\n\n[Retrying after the previous streamed response did not yield valid JSON.]\n\n"
                all_display_parts.append(retry_note)
                yield _stream_event("raw_delta", delta=retry_note)

            try:
                if selected_model_provider == PSNC_MODEL_PROVIDER:
                    stream = _stream_psnc_model(
                        selected_model_name,
                        prompt,
                        TEMPERATURE,
                        disable_thinking=disable_thinking,
                    )
                else:
                    client = get_openai_client()
                    stream = (
                        _extract_stream_text_deltas(chunk)
                        for chunk in client.chat.completions.create(
                            **_build_chat_completion_request_kwargs(
                                selected_model_name,
                                prompt,
                                TEMPERATURE,
                                disable_thinking=disable_thinking,
                                stream=True,
                            )
                        )
                    )

                for reasoning_delta, content_delta in stream:
                    if reasoning_delta:
                        streamed_any_chunk = True
                        saw_reasoning = True
                        attempt_display_parts.append(reasoning_delta)
                        yield _stream_event("raw_delta", delta=reasoning_delta)

                    if content_delta:
                        streamed_any_chunk = True
                        if saw_reasoning and not started_content:
                            separator = "\n\n"
                            attempt_display_parts.append(separator)
                            yield _stream_event("raw_delta", delta=separator)
                        started_content = True
                        attempt_display_parts.append(content_delta)
                        attempt_content_parts.append(content_delta)
                        yield _stream_event("raw_delta", delta=content_delta)

                attempt_display = "".join(attempt_display_parts)
                attempt_content = "".join(attempt_content_parts)

                if attempt_display:
                    all_display_parts.append(attempt_display)

                stripped_content = attempt_content.strip()
                if stripped_content.startswith("<!DOCTYPE html") or stripped_content.startswith("<html"):
                    last_error_message = "The model returned HTML instead of JSON."
                    continue
                if not stripped_content:
                    last_error_message = "The streamed model response was empty."
                    continue

                try:
                    pred = parse_llm_json(attempt_content, definition)
                except Exception as e:
                    last_error_message = str(e)
                    print(f"LLM parse attempt {attempt} failed: {e}")
                    continue

                final_payload = _finalize_pipeline_output(
                    "".join(all_display_parts),
                    pred,
                    creator_orcid_id=creator_orcid_id,
                )
                yield _stream_event("final", data=final_payload)
                return

            except APIStatusError as e:
                last_error_message = str(e)
                print(f"APIStatusError attempt {attempt}: {e}")
            except (OpenAIError, httpx.HTTPError) as e:
                last_error_message = str(e)
                print(f"Transport error attempt {attempt}: {e}")
            except Exception as e:
                last_error_message = str(e)
                print(f"Unexpected streaming error attempt {attempt}: {e}")

            if not streamed_any_chunk:
                fallback_raw = (
                    call_psnc_model(
                        selected_model_name,
                        prompt,
                        TEMPERATURE,
                        disable_thinking=disable_thinking,
                    )
                    if selected_model_provider == PSNC_MODEL_PROVIDER
                    else call_model(
                        selected_model_name,
                        prompt,
                        TEMPERATURE,
                        disable_thinking=disable_thinking,
                    )
                )
                fallback_display = fallback_raw or ""
                if fallback_display:
                    all_display_parts.append(fallback_display)
                    yield _stream_event("raw_delta", delta=fallback_display)
                    try:
                        pred = parse_llm_json(fallback_raw, definition)
                        final_payload = _finalize_pipeline_output(
                            "".join(all_display_parts),
                            pred,
                            creator_orcid_id=creator_orcid_id,
                        )
                        yield _stream_event("final", data=final_payload)
                        return
                    except Exception as e:
                        last_error_message = str(e)
                        print(f"Fallback parse attempt {attempt} failed: {e}")

        yield _stream_event(
            "error", detail=f"Could not extract valid JSON from the model output. Last error: {last_error_message}"
        )
    except ValueError as e:
        yield _stream_event("error", detail=str(e))
    except RuntimeError as e:
        yield _stream_event("error", detail=str(e))
    except Exception as e:
        yield _stream_event("error", detail=f"Unexpected backend error: {e}")


def run_pipeline(
    definition: str,
    disable_thinking: bool = True,
    model_provider: Optional[str] = None,
    model_name: Optional[str] = None,
    creator_orcid_id: Optional[str] = None,
) -> Dict[str, Any]:
    definition = definition.strip()
    prompt, selected_model_provider, selected_model_name = _prepare_pipeline_inputs(
        definition,
        model_name=model_name,
        model_provider=model_provider,
    )

    raw_llm_output, pred = call_llm_loose(
        selected_model_provider,
        selected_model_name,
        prompt,
        definition=definition,
        temperature=TEMPERATURE,
        disable_thinking=disable_thinking,
    )
    return _finalize_pipeline_output(
        raw_llm_output,
        pred,
        creator_orcid_id=creator_orcid_id,
    )


# ======================================================================================
# Routes
# ======================================================================================

IADOPT_VARIABLE_CLASS = URIRef("https://w3id.org/iadopt/ont/Variable")


def _nanopub_created_literal() -> Literal:
    """Create the publication timestamp once so every pubinfo timestamp is internally consistent."""
    created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return Literal(created_at.replace("+00:00", "Z"), datatype=XSD.dateTime)


def _extract_variable_uri(assertion_graph: Graph) -> URIRef:
    """Find the variable resource in the assertion so pubinfo can point `npx:introduces` at it."""
    for subject in assertion_graph.subjects(RDF.type, IADOPT_VARIABLE_CLASS):
        if isinstance(subject, URIRef):
            return subject

    raise RuntimeError("The Turtle assertion does not contain an `iop:Variable` resource with a URI subject.")


def _extract_assertion_label(assertion_graph: Graph, variable_uri: URIRef) -> Optional[str]:
    """Reuse the variable label as the nanopub label when it exists in the assertion graph."""
    label = assertion_graph.value(variable_uri, RDFS.label)
    if label is None:
        return None
    label_text = str(label).strip()
    return label_text or None


def _extract_variable_identifier(assertion_graph: Graph, variable_uri: URIRef) -> str:
    """Return the variable identifier string that the frontend stores in the retract dropdown."""
    identifier = assertion_graph.value(variable_uri, DCTERMS.identifier)
    if identifier is not None and str(identifier).strip():
        return str(identifier).strip()
    return str(variable_uri).rstrip("/").rsplit("/", 1)[-1]


def _normalize_target_nanopub_uri(raw_value: str) -> str:
    """Accept saved nanopub URLs, raw RA identifiers, or Nanodash explore links and normalize them to the canonical URI."""
    candidate = (raw_value or "").strip()
    if not candidate:
        raise RuntimeError("No nanopub URI was provided for retraction.")

    # Support Nanodash explore URLs such as `.../explore?id=RA...` by extracting the underlying nanopub identifier.
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        query_id = urllib.parse.parse_qs(parsed.query).get("id", [])
        if query_id and query_id[0]:
            candidate = query_id[0].strip()
        else:
            trusty_match = re.search(r"(RA[A-Za-z0-9_-]+)", candidate)
            if trusty_match:
                candidate = trusty_match.group(1)

    if re.fullmatch(r"RA[A-Za-z0-9_-]+", candidate):
        return f"https://w3id.org/np/{candidate}"

    if candidate.startswith("https://w3id.org/np/"):
        return candidate

    raise RuntimeError(
        "Unsupported nanopub reference. Provide a `https://w3id.org/np/RA...` URI, "
        "a raw `RA...` identifier, or a Nanodash explore URL."
    )


def _public_key_prefix(public_key: Optional[str], prefix_length: int = 32) -> str:
    """Shorten public keys in error messages so users can compare them without dumping the full key."""
    clean_key = (public_key or "").strip()
    if not clean_key:
        return "missing"
    return clean_key[:prefix_length]


def _assert_retraction_allowed(target_nanopub_uri: str, profile: Profile) -> None:
    """Enforce the key-match rule ourselves because `nanopub-py`'s local retract check is unreliable."""
    try:
        target_nanopub = Nanopub(
            source_uri=target_nanopub_uri,
            conf=NanopubConf(use_server=NANOPUB_PUBLISH_SERVER),
        )
    except Exception as e:
        raise RuntimeError(f"Could not load the target nanopub for retraction: {e}") from e

    target_public_key = (target_nanopub.metadata.public_key or "").strip()
    profile_public_key = (profile.public_key or "").strip()

    if not target_public_key:
        raise RuntimeError(
            "The target nanopub does not expose a public key, so retraction ownership cannot be verified."
        )

    if not profile_public_key:
        raise RuntimeError("The configured nanopub profile does not expose a public key.")

    if target_public_key != profile_public_key:
        raise RuntimeError(
            "The target nanopub was not signed with the key currently configured in this backend, so it cannot be "
            "retracted here. "
            f"Target key prefix: {_public_key_prefix(target_public_key)} ; "
            f"current key prefix: {_public_key_prefix(profile_public_key)}."
        )


def _build_retraction_nanopub(
    target_nanopub_uri: str,
    profile: Profile,
    creator_orcid_id: Optional[str] = None,
) -> Nanopub:
    """Create the richer retraction nanopub shape that the production registries currently accept."""
    resolved_orcid, resolved_profile_name = _resolve_creator_metadata(creator_orcid_id)
    orcid_uri = URIRef(resolved_orcid)
    agent_uri = get_nanopub_agent_uri()
    pubinfo_creator_uri = URIRef(agent_uri) if agent_uri else orcid_uri
    target_identifier = target_nanopub_uri.rsplit("/", 1)[-1]
    retraction_label = f"Retraction of {target_identifier[:10]}"

    assertion_graph = Graph()
    assertion_graph.add((orcid_uri, NPX.retracts, URIRef(target_nanopub_uri)))

    nanopub = Nanopub(
        assertion=assertion_graph,
        conf=NanopubConf(
            profile=profile,
            use_server=NANOPUB_PUBLISH_SERVER,
            add_prov_generated_time=False,
            add_pubinfo_generated_time=False,
            attribute_assertion_to_profile=False,
            attribute_publication_to_profile=False,
        ),
    )

    # The registries accept retractions when they mirror the current Nanodash-style pubinfo shape.
    nanopub.provenance.add((nanopub.assertion.identifier, PROV.wasAttributedTo, orcid_uri))
    if agent_uri:
        nanopub.provenance.add((nanopub.assertion.identifier, PROV.wasGeneratedBy, URIRef(agent_uri)))
    nanopub.pubinfo.add((orcid_uri, FOAF.name, Literal(resolved_profile_name)))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], DCTERMS.created, _nanopub_created_literal()))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], DCTERMS.creator, pubinfo_creator_uri))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], DCTERMS.license, URIRef(NANOPUB_LICENSE_URI)))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], NPX.hasNanopubType, NPX.retracts))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], NPX["wasCreatedAt"], URIRef(NANOPUB_WAS_CREATED_AT)))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], RDFS.label, Literal(retraction_label)))

    if NANOPUB_RETRACT_PROVENANCE_TEMPLATE_URI:
        nanopub.pubinfo.add(
            (
                nanopub.metadata.namespace[""],
                NTEMPLATE["wasCreatedFromProvenanceTemplate"],
                URIRef(NANOPUB_RETRACT_PROVENANCE_TEMPLATE_URI),
            )
        )

    for template_uri in NANOPUB_RETRACT_PUBINFO_TEMPLATE_URIS:
        nanopub.pubinfo.add(
            (
                nanopub.metadata.namespace[""],
                NTEMPLATE["wasCreatedFromPubinfoTemplate"],
                URIRef(template_uri),
            )
        )

    if NANOPUB_RETRACT_TEMPLATE_URI:
        nanopub.pubinfo.add(
            (
                nanopub.metadata.namespace[""],
                NTEMPLATE["wasCreatedFromTemplate"],
                URIRef(NANOPUB_RETRACT_TEMPLATE_URI),
            )
        )

    return nanopub


def _add_nanopub_metadata(
    nanopub: Nanopub,
    *,
    variable_uri: URIRef,
    created_at: Literal,
    agent_uri: Optional[str],
    creator_orcid_id: Optional[str] = None,
) -> None:
    """Mirror the requested provenance and template metadata into the nanopub before signing."""
    nanopub_uri = nanopub.metadata.namespace[""]
    resolved_orcid, resolved_profile_name = _resolve_creator_metadata(creator_orcid_id)
    orcid_uri = URIRef(resolved_orcid)
    pubinfo_creator_uri = URIRef(agent_uri) if agent_uri else orcid_uri

    # The provenance graph must describe who is responsible for the assertion and which software agent generated it.
    nanopub.provenance.add((nanopub.assertion.identifier, PROV.wasAttributedTo, orcid_uri))
    if agent_uri:
        nanopub.provenance.add((nanopub.assertion.identifier, PROV.wasGeneratedBy, URIRef(agent_uri)))

    # The publication info graph mirrors the creator, license, template, and software metadata requested by the user.
    nanopub.pubinfo.add((orcid_uri, FOAF.name, Literal(resolved_profile_name)))
    nanopub.pubinfo.add((nanopub_uri, DCTERMS.created, created_at))
    nanopub.pubinfo.add((nanopub_uri, DCTERMS.creator, pubinfo_creator_uri))
    nanopub.pubinfo.add((nanopub_uri, DCTERMS.license, URIRef(NANOPUB_LICENSE_URI)))
    nanopub.pubinfo.add((nanopub_uri, NPX.introduces, variable_uri))
    nanopub.pubinfo.add((nanopub_uri, NPX["wasCreatedAt"], URIRef(NANOPUB_WAS_CREATED_AT)))

    # if agent_uri:
    #     nanopub.pubinfo.add((nanopub_uri, PAV.createdWith, URIRef(agent_uri)))

    if NANOPUB_TEMPLATE_URI:
        nanopub.pubinfo.add((nanopub_uri, NTEMPLATE["wasCreatedFromTemplate"], URIRef(NANOPUB_TEMPLATE_URI)))

    if NANOPUB_PROVENANCE_TEMPLATE_URI:
        nanopub.pubinfo.add(
            (
                nanopub_uri,
                NTEMPLATE["wasCreatedFromProvenanceTemplate"],
                URIRef(NANOPUB_PROVENANCE_TEMPLATE_URI),
            )
        )

    for template_uri in NANOPUB_PUBINFO_TEMPLATE_URIS:
        nanopub.pubinfo.add((nanopub_uri, NTEMPLATE["wasCreatedFromPubinfoTemplate"], URIRef(template_uri)))


def readiness_checks() -> Dict[str, bool]:
    return {
        "schema_exists": SCHEMA_PATH.exists(),
        "prompt_dir_exists": PROMPT_DIR.exists(),
        "five_shot_dir_exists": FIVE_SHOT_DIR.exists(),
        "enabled_provider_keys_set": all(
            {
                OPENROUTER_MODEL_PROVIDER: bool(OPENROUTER_API_KEY),
                PSNC_MODEL_PROVIDER: bool(PSNC_API_KEY),
            }[provider]
            for provider in ENABLED_MODEL_PROVIDERS
        ),
        "wikidata_reranker_ready": not ENABLE_WIKIDATA_LINKING or bool(PSNC_API_KEY),
    }


@app.post(f"{API_PREFIX}/auth/login", response_model=AuthUserResponse, tags=["Auth"])
def login(req: LoginRequest, request: Request) -> JSONResponse:
    start = time.perf_counter()
    user = auth_store.authenticate(req.username, req.password) if auth_store.enabled else auth_store.user_from_request(request)
    if not user:
        auth_store.audit_event(
            action="auth.login_failed",
            request=request,
            status_code=401,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload={"username": req.username},
            error="Invalid username or password.",
        )
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    response = JSONResponse({"user": _public_user(user), "auth_enabled": auth_store.enabled})
    if auth_store.enabled:
        auth_store.set_session_cookie(response, auth_store.create_session(user["id"], request))
    auth_store.audit_event(
        action="auth.login",
        user=user,
        request=request,
        status_code=200,
        latency_ms=round((time.perf_counter() - start) * 1000),
        request_payload={"username": req.username},
        response_payload={"user": _public_user(user)},
    )
    return response


@app.get(f"{API_PREFIX}/auth/verify", status_code=204, tags=["Auth"])
def verify_auth(request: Request) -> Response:
    user = auth_store.user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return Response(status_code=204)


@app.get(f"{API_PREFIX}/auth/me", response_model=AuthUserResponse, tags=["Auth"])
def current_user(user: Dict[str, Any] = Depends(require_current_user)) -> Dict[str, Any]:
    return {"user": _public_user(user), "auth_enabled": auth_store.enabled}


@app.post(f"{API_PREFIX}/auth/logout", response_model=StatusOkResponse, tags=["Auth"])
def logout(request: Request, user: Dict[str, Any] = Depends(require_current_user)) -> JSONResponse:
    auth_store.delete_session(request)
    response = JSONResponse({"status": "ok"})
    auth_store.clear_session_cookie(response)
    auth_store.audit_event(action="auth.logout", user=user, request=request, status_code=200)
    return response


@app.get(f"{API_PREFIX}/docs", include_in_schema=False)
def protected_docs(_: Dict[str, Any] = Depends(require_current_user)):
    return get_swagger_ui_html(openapi_url=f"{API_PREFIX}/openapi.json", title=f"{app.title} - Swagger UI")


@app.get(f"{API_PREFIX}/redoc", include_in_schema=False)
def protected_redoc(_: Dict[str, Any] = Depends(require_current_user)):
    return get_redoc_html(openapi_url=f"{API_PREFIX}/openapi.json", title=f"{app.title} - ReDoc")


@app.get(f"{API_PREFIX}/openapi.json", include_in_schema=False)
def protected_openapi(_: Dict[str, Any] = Depends(require_current_user)):
    return app.openapi()


@app.post(f"{API_PREFIX}/events", response_model=StatusOkResponse, tags=["System"])
def record_frontend_event(
    req: FrontendEventRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
) -> Dict[str, str]:
    auth_store.audit_event(
        action=f"frontend.{req.action}",
        user=user,
        request=request,
        status_code=200,
        request_payload=req.payload or {},
        metadata=req.metadata or {},
    )
    return {"status": "ok"}


@app.get(f"{API_PREFIX}/admin/stats", response_model=AdminStatsResponse, tags=["Admin"])
def admin_stats(_: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    checks = readiness_checks()
    stats = auth_store.stats()
    stats["readiness"] = {
        "status": "ready" if all(checks.values()) else "not_ready",
        "checks": checks,
    }
    return stats


@app.get(f"{API_PREFIX}/admin/audit", response_model=AuditResponse, tags=["Admin"])
def admin_audit(
    limit: int = 100,
    offset: int = 0,
    _: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    return {"events": auth_store.get_audit_events(limit=limit, offset=offset)}


@app.get(f"{API_PREFIX}/admin/users", response_model=UsersResponse, tags=["Admin"])
def admin_users(_: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    return {"users": [_public_user(user) for user in auth_store.list_users()]}


@app.post(f"{API_PREFIX}/admin/users", response_model=UserResponse, tags=["Admin"])
def admin_create_user(
    req: AdminCreateUserRequest,
    request: Request,
    admin: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    try:
        user = auth_store.create_user(**req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    auth_store.audit_event(
        action="admin.user_created",
        user=admin,
        request=request,
        status_code=200,
        request_payload={**req.model_dump(exclude={"password"}), "password": "[redacted]"},
        response_payload={"user": _public_user(user)},
    )
    return {"user": _public_user(user)}


@app.patch(f"{API_PREFIX}/admin/users/{{user_id}}", response_model=UserResponse, tags=["Admin"])
def admin_update_user(
    user_id: int,
    req: AdminUpdateUserRequest,
    request: Request,
    admin: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    updates = req.model_dump(exclude_unset=True)
    try:
        user = auth_store.update_user(user_id, updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    audit_payload = dict(updates)
    if "password" in audit_payload:
        audit_payload["password"] = "[redacted]"
    auth_store.audit_event(
        action="admin.user_updated",
        user=admin,
        request=request,
        status_code=200,
        request_payload={"user_id": user_id, "updates": audit_payload},
        response_payload={"user": _public_user(user)},
    )
    return {"user": _public_user(user)}


@app.get(f"{API_PREFIX}/livez", response_model=StatusOkResponse, tags=["System"])
def liveness() -> Dict[str, str]:
    return {"status": "ok"}


@app.get(f"{API_PREFIX}/readyz", response_model=ReadyzResponse, tags=["System"])
def health() -> Dict[str, Any]:
    checks = readiness_checks()
    if not all(checks.values()):
        raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}


@app.get(f"{API_PREFIX}/health", response_model=StatusOkResponse, tags=["System"])
def health_alias() -> Dict[str, str]:
    return {"status": "ok"}


@app.get(f"{API_PREFIX}/model-options", response_model=ModelOptionsResponse, tags=["Decomposition"])
def model_options() -> ModelOptionsResponse:
    """Expose the backend-managed list of allowed model names for the frontend dropdown."""
    provider_configs: Dict[str, Dict[str, Any]] = {}
    if OPENROUTER_MODEL_PROVIDER in ENABLED_MODEL_PROVIDERS:
        provider_configs[OPENROUTER_MODEL_PROVIDER] = {
            "label": "OpenRouter",
            "default_model_name": MODEL_NAME,
            "model_names": MODEL_NAMES,
        }
    if PSNC_MODEL_PROVIDER in ENABLED_MODEL_PROVIDERS:
        provider_configs[PSNC_MODEL_PROVIDER] = {
            "label": "PSNC",
            "default_model_name": PSNC_MODEL_NAME,
            "model_names": PSNC_MODEL_NAMES,
        }

    default_provider_config = provider_configs[DEFAULT_MODEL_PROVIDER]
    return ModelOptionsResponse(
        default_model_provider=DEFAULT_MODEL_PROVIDER,
        default_model_name=default_provider_config["default_model_name"],
        model_names=default_provider_config["model_names"],
        providers=provider_configs,
    )


@app.get(
    f"{API_PREFIX}/nanopub/preparation-options",
    response_model=NanopubPreparationOptionsResponse,
    tags=["Nanopub"],
)
def nanopub_preparation_options() -> NanopubPreparationOptionsResponse:
    """Expose the metadata constants the frontend needs to enrich pasted Turtle for nanopublication.

    These values are the single source of truth shared with the backend's own TTL generator, so
    pasted-Turtle preparation and generated Turtle stay byte-for-byte aligned.
    """
    return NanopubPreparationOptionsResponse(
        default_creator_orcid_id=_normalize_orcid(NANOPUB_ORCID_ID),
        conforms_to_uri=IADOPT_VARIABLE_CONFORMS_TO,
        created_with_label=IADOPT_CREATED_WITH_LABEL,
    )


@app.post(
    f"{API_PREFIX}/decompose/stream",
    summary="Decompose a variable with streamed raw LLM output",
    description=(
        "Frontend endpoint. Use this when the caller wants to show the raw LLM response while it is being "
        "generated. The response is newline-delimited JSON with `raw_delta` events during generation, followed "
        "by one `final` event containing the same final payload shape as `/decompose`. An `error` event is emitted "
        "if the streamed output cannot be parsed or the backend fails."
    ),
    tags=["Decomposition"],
    responses={
        200: {
            "description": (
                "NDJSON stream. Event types: `raw_delta`, `final`, and `error`. "
                "Use `/api/decompose` instead if you need a single JSON response."
            )
        }
    },
)
def decompose_stream(
    req: DecomposeRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
) -> StreamingResponse:
    """Stream raw LLM output chunks first, then emit the final structured decompose payload."""
    start = time.perf_counter()
    request_payload = req.model_dump()

    def audited_events() -> Iterator[str]:
        final_payload: Optional[Dict[str, Any]] = None
        error_detail: Optional[str] = None
        status_code = 200
        try:
            for line in stream_pipeline_events(
                req.definition,
                disable_thinking=req.disable_thinking,
                model_provider=req.model_provider,
                model_name=req.model_name,
                creator_orcid_id=req.creator_orcid_id,
            ):
                try:
                    event = json.loads(line)
                    if event.get("type") == "final":
                        final_payload = event.get("data")
                    elif event.get("type") == "error":
                        error_detail = event.get("detail") or "Streaming backend error."
                        status_code = 500
                except Exception:
                    pass
                yield line
        except Exception as e:
            error_detail = str(e)
            status_code = 500
            raise
        finally:
            auth_store.audit_event(
                action="decompose.stream",
                user=user,
                request=request,
                status_code=status_code,
                latency_ms=round((time.perf_counter() - start) * 1000),
                request_payload=request_payload,
                response_payload=final_payload,
                metadata={
                    "model_provider": req.model_provider,
                    "model_name": req.model_name,
                    "disable_thinking": req.disable_thinking,
                },
                error=error_detail,
            )

    return StreamingResponse(
        audited_events(),
        media_type="application/x-ndjson",
    )


@app.post(
    f"{API_PREFIX}/decompose",
    response_model=DecomposeResponse,
    summary="Decompose a variable with one final JSON response",
    description=(
        "Non-streaming endpoint. It runs the same decomposition, validation, enrichment, and Turtle-generation "
        "pipeline as `/decompose/stream`, but waits until the model response is complete and returns one JSON "
        "object. This is useful for scripts, API clients, tests, and debugging tools. The frontend normally uses "
        "`/decompose/stream` so users can see raw LLM output progressively."
    ),
    tags=["Decomposition"],
)
def decompose(
    req: DecomposeRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
) -> DecomposeResponse:
    start = time.perf_counter()
    try:
        result = run_pipeline(
            req.definition,
            disable_thinking=req.disable_thinking,
            model_provider=req.model_provider,
            model_name=req.model_name,
            creator_orcid_id=req.creator_orcid_id,
        )
        auth_store.audit_event(
            action="decompose",
            user=user,
            request=request,
            status_code=200,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            response_payload=result,
            metadata={
                "model_provider": req.model_provider,
                "model_name": req.model_name,
                "disable_thinking": req.disable_thinking,
            },
        )
        return DecomposeResponse(**result)
    except ValueError as e:
        auth_store.audit_event(
            action="decompose",
            user=user,
            request=request,
            status_code=400,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            metadata={"model_provider": req.model_provider, "model_name": req.model_name},
            error=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        auth_store.audit_event(
            action="decompose",
            user=user,
            request=request,
            status_code=500,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            metadata={"model_provider": req.model_provider, "model_name": req.model_name},
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        auth_store.audit_event(
            action="decompose",
            user=user,
            request=request,
            status_code=500,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            metadata={"model_provider": req.model_provider, "model_name": req.model_name},
            error=f"Unexpected backend error: {e}",
        )
        raise HTTPException(status_code=500, detail=f"Unexpected backend error: {e}") from e


@app.post(f"{API_PREFIX}/nanopub/publish", response_model=PublishNanopubResponse, tags=["Nanopub"])
def publish_nanopub(
    req: PublishNanopubRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
) -> PublishNanopubResponse:
    """Publish the exact TTL currently shown in the frontend as a signed nanopublication."""
    start = time.perf_counter()
    ttl = req.ttl.strip()
    if not ttl:
        auth_store.audit_event(
            action="nanopub.publish",
            user=user,
            request=request,
            status_code=400,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error="TTL payload is empty.",
        )
        raise HTTPException(status_code=400, detail="TTL payload is empty.")

    assertion_graph = Graph()
    try:
        assertion_graph.parse(data=ttl, format="turtle")
    except Exception as e:
        auth_store.audit_event(
            action="nanopub.publish",
            user=user,
            request=request,
            status_code=400,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error=f"Could not parse Turtle payload: {e}",
        )
        raise HTTPException(status_code=400, detail=f"Could not parse Turtle payload: {e}") from e

    try:
        profile = get_nanopub_profile()
        variable_uri = _extract_variable_uri(assertion_graph)
        variable_identifier = _extract_variable_identifier(assertion_graph, variable_uri)
        assertion_label = _extract_assertion_label(assertion_graph, variable_uri)
        created_at = _nanopub_created_literal()
        agent_uri = get_nanopub_agent_uri()

        nanopub_conf = NanopubConf(
            profile=profile,
            use_server=NANOPUB_PUBLISH_SERVER,
            add_prov_generated_time=False,
            add_pubinfo_generated_time=False,
            attribute_assertion_to_profile=False,
            attribute_publication_to_profile=False,
        )
        nanopub = Nanopub(assertion=assertion_graph, conf=nanopub_conf)
        _add_nanopub_metadata(
            nanopub,
            variable_uri=variable_uri,
            created_at=created_at,
            agent_uri=agent_uri,
            creator_orcid_id=req.creator_orcid_id,
        )

        if assertion_label:
            # Carry the assertion label into the nanopub pubinfo so the resulting publication is easier to inspect.
            nanopub.pubinfo.add((nanopub.metadata.namespace[""], RDFS.label, Literal(assertion_label)))

        publish_result = nanopub.publish()
        nanopub_url = str(publish_result[0])
        published_to = str(publish_result[1])

        response_payload = PublishNanopubResponse(
            nanopub_url=nanopub_url,
            published_to=published_to,
            variable_identifier=variable_identifier,
            variable_uri=str(variable_uri),
        )
        auth_store.audit_event(
            action="nanopub.publish",
            user=user,
            request=request,
            status_code=200,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            response_payload=response_payload.model_dump(),
            metadata={"variable_identifier": variable_identifier, "variable_uri": str(variable_uri)},
        )
        return response_payload
    except HTTPException:
        raise
    except RuntimeError as e:
        auth_store.audit_event(
            action="nanopub.publish",
            user=user,
            request=request,
            status_code=500,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        auth_store.audit_event(
            action="nanopub.publish",
            user=user,
            request=request,
            status_code=500,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error=f"Nanopub publish failed: {e}",
        )
        raise HTTPException(status_code=500, detail=f"Nanopub publish failed: {e}") from e


@app.post(f"{API_PREFIX}/nanopub/retract", response_model=RetractNanopubResponse, tags=["Nanopub"])
def retract_nanopub(
    req: RetractNanopubRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
) -> RetractNanopubResponse:
    """Publish a signed nanopub retraction for a previously published nanopublication."""
    start = time.perf_counter()
    try:
        target_nanopub_uri = _normalize_target_nanopub_uri(req.nanopub_uri)
        profile = get_nanopub_profile()
        _assert_retraction_allowed(target_nanopub_uri, profile)
        retraction = _build_retraction_nanopub(
            target_nanopub_uri,
            profile,
            creator_orcid_id=req.creator_orcid_id,
        )

        # Publishing the custom retraction nanopub creates a new nanopub whose assertion retracts the target URI.
        publish_result = retraction.publish()
        response_payload = RetractNanopubResponse(
            retraction_url=str(publish_result[0]),
            published_to=str(publish_result[1]),
            retracted_nanopub_url=target_nanopub_uri,
        )
        auth_store.audit_event(
            action="nanopub.retract",
            user=user,
            request=request,
            status_code=200,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            response_payload=response_payload.model_dump(),
            metadata={"target_nanopub_uri": target_nanopub_uri},
        )
        return response_payload
    except RuntimeError as e:
        auth_store.audit_event(
            action="nanopub.retract",
            user=user,
            request=request,
            status_code=400,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        auth_store.audit_event(
            action="nanopub.retract",
            user=user,
            request=request,
            status_code=500,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error=f"Nanopub retract failed: {e}",
        )
        raise HTTPException(status_code=500, detail=f"Nanopub retract failed: {e}") from e
