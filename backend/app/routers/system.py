"""System routes: liveness/readiness/health, frontend events, and protected docs.

Includes the readiness probe logic and the auth-gated OpenAPI documentation
endpoints (Swagger UI, ReDoc, and the raw schema).
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse

from ..core.config import OPENROUTER_MODEL_PROVIDER, PSNC_MODEL_PROVIDER, settings
from ..core.dependencies import API_PREFIX, auth_store, require_current_user
from ..schemas import ReadyzResponse, StatusOkResponse
from ..schemas.requests import FrontendEventRequest

router = APIRouter()


def readiness_checks() -> Dict[str, bool]:
    """Return per-check readiness booleans (schema/prompt/example files + keys).

    Returns:
        A dict of named boolean checks; all true means the service is ready.
    """
    return {
        "schema_exists": settings.schema_path.exists(),
        "prompt_dir_exists": settings.prompt_dir.exists(),
        "five_shot_dir_exists": settings.five_shot_dir.exists(),
        "enabled_provider_keys_set": all(
            {
                OPENROUTER_MODEL_PROVIDER: bool(settings.openrouter_api_key),
                PSNC_MODEL_PROVIDER: bool(settings.psnc_api_key),
            }[provider]
            for provider in settings.enabled_model_providers
        ),
        "wikidata_reranker_ready": not settings.enable_wikidata_linking or bool(settings.psnc_api_key),
    }


@router.get(f"{API_PREFIX}/docs", include_in_schema=False)
def protected_docs(request: Request, _: Dict[str, Any] = Depends(require_current_user)) -> HTMLResponse:
    """Serve the auth-gated Swagger UI."""
    return get_swagger_ui_html(openapi_url=f"{API_PREFIX}/openapi.json", title=f"{request.app.title} - Swagger UI")


@router.get(f"{API_PREFIX}/redoc", include_in_schema=False)
def protected_redoc(request: Request, _: Dict[str, Any] = Depends(require_current_user)) -> HTMLResponse:
    """Serve the auth-gated ReDoc UI."""
    return get_redoc_html(openapi_url=f"{API_PREFIX}/openapi.json", title=f"{request.app.title} - ReDoc")


@router.get(f"{API_PREFIX}/openapi.json", include_in_schema=False)
def protected_openapi(request: Request, _: Dict[str, Any] = Depends(require_current_user)) -> Dict[str, Any]:
    """Serve the auth-gated raw OpenAPI schema."""
    return request.app.openapi()


@router.post(f"{API_PREFIX}/events", response_model=StatusOkResponse, tags=["System"])
def record_frontend_event(
    req: FrontendEventRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
) -> Dict[str, str]:
    """Record a frontend analytics/audit event."""
    auth_store.audit_event(
        action=f"frontend.{req.action}",
        user=user,
        request=request,
        status_code=200,
        request_payload=req.payload or {},
        metadata=req.metadata or {},
    )
    return {"status": "ok"}


@router.get(f"{API_PREFIX}/livez", response_model=StatusOkResponse, tags=["System"])
def liveness() -> Dict[str, str]:
    """Liveness probe: always returns ok if the process is up."""
    return {"status": "ok"}


@router.get(f"{API_PREFIX}/readyz", response_model=ReadyzResponse, tags=["System"])
def health() -> Dict[str, Any]:
    """Readiness probe: 200 with checks when ready, else 503."""
    checks = readiness_checks()
    if not all(checks.values()):
        raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}


@router.get(f"{API_PREFIX}/health", response_model=StatusOkResponse, tags=["System"])
def health_alias() -> Dict[str, str]:
    """Health alias: always returns ok."""
    return {"status": "ok"}
