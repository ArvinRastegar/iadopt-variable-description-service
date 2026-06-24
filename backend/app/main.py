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

from .auth import AuthStore
from .clients.http import get_http_session
from .clients.openai_client import get_openai_client
from .clients.psnc_client import (
    build_psnc_chat_payload,
    psnc_chat_completions_url,
    psnc_chat_headers,
    psnc_rerank_url,
)
from .core import config
from .core.config import settings
from .core.state import app_state
from .core.text import lookup_key, normalize_text, ttl_quote
from .services.orcid import (
    extract_orcid_display_name,
    lookup_orcid_display_name,
    normalize_orcid,
    orcid_suffix,
    resolve_creator_metadata,
)
from .services.prompts import (
    build_prompt,
    format_example_block,
    list_prompt_versions,
    load_examples,
    load_prompt_instructions,
    strip_all_uri_fields,
)
from .services.rdf_ttl import (
    build_alt_label,
    format_main_label,
    json_to_ttl_repo_style,
    literal_join,
    make_comment,
    make_variable_identity,
    normalize_constraint_phrase_for_alt_label,
    phrase_for_role,
    wiki_to_entity,
)
from .services.llm import (
    build_chat_completion_request_kwargs,
    call_llm_loose,
    call_model,
    call_psnc_model,
    coerce_prediction,
    extract_stream_text_deltas,
    extract_stream_text_deltas_from_dict,
    flatten_text_fragments,
    parse_llm_json,
    resolve_model_name,
    resolve_model_provider,
    stream_event,
    stream_psnc_model,
)
from .services.reranker import call_psnc_reranker
from .services.enrichment import (
    enrich_with_uris_reranker,
    get_wikidata_entity_reranker,
    qid_from_uri_or_text,
    to_wiki_url,
)
from .services.nanopub_service import (
    add_nanopub_metadata,
    assert_retraction_allowed,
    build_retraction_nanopub,
    extract_assertion_label,
    extract_variable_identifier,
    extract_variable_uri,
    get_nanopub_agent_label,
    get_nanopub_agent_uri,
    get_nanopub_profile,
    nanopub_created_literal,
    normalize_env_multiline,
    normalize_nanopub_key,
    normalize_target_nanopub_uri,
)
from .services.validation import (
    collect_constraint_target_keys,
    format_path,
    get_constraint_semantic_validation_errors,
    get_schema_validation_errors,
    load_schema,
    patch_schema_for_pipeline,
    safe_preview,
)
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

# Private-name aliases for client helpers moved to app.clients, kept so existing
# call sites in this module read unchanged during the incremental Phase-2 split.
_build_psnc_chat_payload = build_psnc_chat_payload
_psnc_chat_headers = psnc_chat_headers
_psnc_chat_completions_url = psnc_chat_completions_url
_psnc_rerank_url = psnc_rerank_url

# Aliases for ORCID + validation helpers moved to app.services.{orcid,validation}.
_normalize_orcid = normalize_orcid
_orcid_suffix = orcid_suffix
_extract_orcid_display_name = extract_orcid_display_name
_lookup_orcid_display_name = lookup_orcid_display_name
_resolve_creator_metadata = resolve_creator_metadata
_format_path = format_path
_safe_preview = safe_preview
_patch_schema_for_pipeline = patch_schema_for_pipeline
_collect_constraint_target_keys = collect_constraint_target_keys
_get_constraint_semantic_validation_errors = get_constraint_semantic_validation_errors
_build_chat_completion_request_kwargs = build_chat_completion_request_kwargs
_flatten_text_fragments = flatten_text_fragments
_extract_stream_text_deltas = extract_stream_text_deltas
_extract_stream_text_deltas_from_dict = extract_stream_text_deltas_from_dict
_stream_psnc_model = stream_psnc_model
_stream_event = stream_event
_resolve_model_provider = resolve_model_provider
_resolve_model_name = resolve_model_name
_qid_from_uri_or_text = qid_from_uri_or_text
_to_wiki_url = to_wiki_url
_normalize_env_multiline = normalize_env_multiline
_normalize_nanopub_key = normalize_nanopub_key
_nanopub_created_literal = nanopub_created_literal
_extract_variable_uri = extract_variable_uri
_extract_assertion_label = extract_assertion_label
_extract_variable_identifier = extract_variable_identifier
_normalize_target_nanopub_uri = normalize_target_nanopub_uri
_assert_retraction_allowed = assert_retraction_allowed
_build_retraction_nanopub = build_retraction_nanopub
_add_nanopub_metadata = add_nanopub_metadata


# ======================================================================================
# App setup
# ======================================================================================
def warmup_assets() -> None:
    # OpenRouter is only initialized when it is enabled for this deployment.
    if OPENROUTER_MODEL_PROVIDER in ENABLED_MODEL_PROVIDERS and OPENROUTER_API_KEY:
        get_openai_client()

    # Cache schema validator (shared via app.core.state so services can read it
    # without importing this module).
    app_state.schema_cache = _patch_schema_for_pipeline(load_schema(SCHEMA_PATH))
    app_state.validator_cache = Draft202012Validator(app_state.schema_cache)

    # Cache prompt version + examples
    versions = list_prompt_versions(PROMPT_DIR)
    if not versions:
        raise RuntimeError(f"No prompt files found in: {PROMPT_DIR}")
    app_state.prompt_version_cache = versions[0]
    app_state.examples_5_cache = load_examples(FIVE_SHOT_DIR, 5)

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

# Paths come from Settings (app.core.config also runs load_dotenv on import).
BASE_DIR = settings.base_dir
DATA_DIR = settings.data_dir
SCHEMA_PATH = settings.schema_path
PROMPT_DIR = settings.prompt_dir
FIVE_SHOT_DIR = settings.five_shot_dir

# All configuration now lives in the typed app.core.config.Settings singleton.
# These module-level names are thin aliases kept so the rest of main.py (and the
# incremental Phase-2 extraction) reads unchanged. Defaults/coercion are verified
# identical to the original os.getenv logic by tests/test_settings_parity.py.
OPENROUTER_MODEL_PROVIDER = config.OPENROUTER_MODEL_PROVIDER
PSNC_MODEL_PROVIDER = config.PSNC_MODEL_PROVIDER
SUPPORTED_MODEL_PROVIDERS = config.SUPPORTED_MODEL_PROVIDERS
ENABLED_MODEL_PROVIDERS = settings.enabled_model_providers
DEFAULT_MODEL_PROVIDER = settings.default_model_provider

DEFAULT_MODEL_NAME = config.DEFAULT_MODEL_NAME
DEFAULT_MODEL_NAMES = config.DEFAULT_MODEL_NAMES
DEFAULT_PSNC_MODEL_NAME = config.DEFAULT_PSNC_MODEL_NAME
DEFAULT_PSNC_MODEL_NAMES = config.DEFAULT_PSNC_MODEL_NAMES

MODEL_NAME = settings.model_name
TEMPERATURE = settings.temperature
OPENROUTER_API_KEY = settings.openrouter_api_key
PSNC_API_KEY = settings.psnc_api_key
PSNC_API_BASE_URL = settings.psnc_api_base_url
PSNC_RERANK_MODEL = settings.psnc_rerank_model
NANOPUB_PRIVATE_KEY = settings.nanopub_private_key
NANOPUB_PUBLIC_KEY = settings.nanopub_public_key
NANOPUB_ORCID_ID = settings.nanopub_orcid_id
NANOPUB_AGENT_INTRO_URI = settings.nanopub_agent_intro_uri
NANOPUB_PUBLISH_SERVER = settings.nanopub_publish_server
NANOPUB_LICENSE_URI = settings.nanopub_license_uri
NANOPUB_WAS_CREATED_AT = settings.nanopub_was_created_at
NANOPUB_TEMPLATE_URI = settings.nanopub_template_uri
NANOPUB_PROVENANCE_TEMPLATE_URI = settings.nanopub_provenance_template_uri
NANOPUB_PUBINFO_TEMPLATE_URIS = settings.nanopub_pubinfo_template_uris
IADOPT_VARIABLE_CONFORMS_TO = settings.iadopt_variable_conforms_to
NANOPUB_RETRACT_TEMPLATE_URI = settings.nanopub_retract_template_uri
NANOPUB_RETRACT_PROVENANCE_TEMPLATE_URI = settings.nanopub_retract_provenance_template_uri
NANOPUB_RETRACT_PUBINFO_TEMPLATE_URIS = settings.nanopub_retract_pubinfo_template_uris
IADOPT_CREATED_WITH_LABEL = settings.iadopt_created_with_label

RERANK_THRESHOLD = settings.rerank_threshold
ENABLE_WIKIDATA_LINKING = settings.enable_wikidata_linking

AUTH_ENABLED = settings.auth_enabled
AUTH_STATE_DIR = settings.auth_state_dir
AUTH_DB_PATH = settings.auth_db_path
AUTH_SESSION_SECRET = settings.session_secret
AUTH_COOKIE_SECURE = settings.cookie_secure
AUTH_SESSION_TTL_HOURS = settings.session_ttl_hours
AUDIT_RETENTION_DAYS = settings.audit_retention_days
AUDIT_MAX_PAYLOAD_BYTES = settings.audit_max_payload_bytes

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


# Allowed model-name lists are computed by Settings (see app.core.config).
MODEL_NAMES = settings.model_names
PSNC_MODEL_NAME = settings.psnc_model_name
PSNC_MODEL_NAMES = settings.psnc_model_names


# Request/response models now live in app.schemas (imported at the top of this
# module). They are the single source of truth for the API contract and feed the
# OpenAPI schema; see app/schemas/ and docs/CONTRACTS.md.


# Clients (http/openai/psnc), warmup caches (core.state), and all service-level
# caches now live in their own modules; main.py holds no lazy-client state.


# Nanopub profile/agent helpers moved to app.services.nanopub_service.
# Prompt building moved to app.services.prompts.
# LLM call + JSON extraction moved to app.services.llm.
# PSNC reranker moved to app.services.reranker.
# (all imported above)


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


# Schema validation moved to app.services.validation (imported above).


# Wikidata enrichment moved to app.services.enrichment (imported above).
# ======================================================================================
# JSON -> TTL
# ======================================================================================

# Text helpers moved to app.core.text (a dependency-free leaf shared by the orcid,
# nanopub, rdf_ttl, and validation services). Aliased here so existing call sites
# in this module keep working during the incremental Phase-2 split.
_ttl_quote = ttl_quote
_normalize_text = normalize_text
_lookup_key = lookup_key


# JSON -> TTL generation moved to app.services.rdf_ttl (imported above).
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

    prompt_version = app_state.prompt_version_cache
    if not prompt_version:
        prompt_versions = list_prompt_versions(PROMPT_DIR)
        if not prompt_versions:
            raise RuntimeError(f"No prompt files found in: {PROMPT_DIR}")
        prompt_version = prompt_versions[0]

    examples_5 = (
        app_state.examples_5_cache if app_state.examples_5_cache is not None else load_examples(FIVE_SHOT_DIR, 5)
    )
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

# Nanopub publish/retract helpers moved to app.services.nanopub_service (imported above).


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
