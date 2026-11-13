"""Decomposition routes: model options, non-streaming and streaming decompose.

For a signed-in account every run starts a decomposition record (D-036): the run and its
draft as version 0. The audit log keeps the model choice, timing and errors of a run, and
no longer the definition or the result, which the record holds (D-024).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..auth import utc_now
from ..core.config import OPENROUTER_MODEL_PROVIDER, PSNC_MODEL_PROVIDER, settings
from ..core.dependencies import API_PREFIX, auth_store, record_store, require_current_user
from ..core.logging import get_logger
from ..pipeline import prompt_setup, run_pipeline, stream_pipeline_events
from ..schemas import DecomposeResponse, ModelOptionsResponse
from ..schemas.responses import ProviderConfig
from ..schemas.requests import DecomposeRequest
from ..services.llm import resolve_model_name, resolve_model_provider, stream_event

router = APIRouter()
logger = get_logger(__name__)


def _audit_metadata(req: DecomposeRequest, record_id: Optional[str]) -> Dict[str, Any]:
    """Return what the audit log keeps about a run: the model choice and its record, not the content."""
    return {
        "model_provider": req.model_provider,
        "model_name": req.model_name,
        "disable_thinking": req.disable_thinking,
        "record_id": record_id,
    }


def _is_bad_request(req: DecomposeRequest) -> bool:
    """Return whether the pipeline refuses this request before any model runs.

    Such requests (a blank definition, an unknown provider or model) are answered with an
    error but not recorded, on the streaming route as on the JSON route.
    """
    if not req.definition.strip():
        return True
    try:
        resolve_model_name(req.model_name, resolve_model_provider(req.model_provider))
    except ValueError:
        return True
    return False


def _record_run(
    *,
    user: Dict[str, Any],
    request: Request,
    req: DecomposeRequest,
    started_at: datetime,
    result: Optional[Dict[str, Any]],
    error: Optional[str],
) -> Optional[str]:
    """Write a run to the account's decomposition record; a failure here never fails the run.

    Returns:
        The new record's id, or ``None`` when nothing was recorded.
    """
    try:
        provider = resolve_model_provider(req.model_provider)
        model = resolve_model_name(req.model_name, provider)
    except ValueError:
        provider, model = req.model_provider or "", req.model_name or ""
    try:
        setup, prompt_sha256 = prompt_setup(provider)
    except Exception:
        logger.exception("Could not fingerprint the prompt setup.")
        setup, prompt_sha256 = "", None
    try:
        return record_store.record_decomposition(
            user=user,
            request=request,
            definition=req.definition,
            provider=provider,
            model=model,
            disable_thinking=req.disable_thinking,
            prompt_setup=setup,
            prompt_sha256=prompt_sha256,
            started_at=started_at,
            ended_at=utc_now(),
            result=result,
            error=error,
            client=req.client.model_dump(exclude_none=True) if req.client else None,
        )
    except Exception:
        logger.exception("Could not write the decomposition record.")
        return None


@router.get(f"{API_PREFIX}/model-options", response_model=ModelOptionsResponse, tags=["Decomposition"])
def model_options() -> ModelOptionsResponse:
    """Expose the backend-managed list of allowed model names for the frontend dropdown."""
    provider_configs: Dict[str, ProviderConfig] = {}
    if OPENROUTER_MODEL_PROVIDER in settings.enabled_model_providers:
        provider_configs[OPENROUTER_MODEL_PROVIDER] = ProviderConfig(
            label="OpenRouter",
            default_model_name=settings.model_name,
            model_names=settings.model_names,
        )
    if PSNC_MODEL_PROVIDER in settings.enabled_model_providers:
        provider_configs[PSNC_MODEL_PROVIDER] = ProviderConfig(
            label="PSNC",
            default_model_name=settings.psnc_model_name,
            model_names=settings.psnc_model_names,
        )

    default_provider_config = provider_configs[settings.default_model_provider]
    return ModelOptionsResponse(
        default_model_provider=settings.default_model_provider,
        default_model_name=default_provider_config.default_model_name,
        model_names=default_provider_config.model_names,
        providers=provider_configs,
    )


@router.post(
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
    """Stream raw LLM output chunks first, then emit the final structured decompose payload.

    The final event's ``data`` has the ``DecomposeResponse`` shape, including the
    ``record_id`` of the decomposition record the run started (``null`` when nothing is
    recorded).
    """
    start = time.perf_counter()
    started_at = utc_now()
    bad_request = _is_bad_request(req)

    def audited_events() -> Iterator[str]:
        """Yield pipeline NDJSON lines, then record and audit the stream outcome in a finally block."""
        final_payload: Optional[Dict[str, Any]] = None
        error_detail: Optional[str] = None
        status_code = 200
        record_id: Optional[str] = None
        try:
            for line in stream_pipeline_events(
                req.definition,
                disable_thinking=req.disable_thinking,
                model_provider=req.model_provider,
                model_name=req.model_name,
            ):
                try:
                    event = json.loads(line)
                    if event.get("type") == "final":
                        final_payload = event.get("data")
                        record_id = _record_run(
                            user=user, request=request, req=req, started_at=started_at, result=final_payload, error=None
                        )
                        if isinstance(final_payload, dict):
                            line = stream_event("final", data={**final_payload, "record_id": record_id})
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
            if final_payload is None and not bad_request:
                record_id = _record_run(
                    user=user,
                    request=request,
                    req=req,
                    started_at=started_at,
                    result=None,
                    error=error_detail or "The decomposition stopped before it finished.",
                )
            auth_store.audit_event(
                action="decompose.stream",
                user=user,
                request=request,
                status_code=status_code,
                latency_ms=round((time.perf_counter() - start) * 1000),
                metadata=_audit_metadata(req, record_id),
                error=error_detail,
            )

    return StreamingResponse(
        audited_events(),
        media_type="application/x-ndjson",
    )


@router.post(
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
    """Run the full decomposition pipeline and return one final payload; record and audit the outcome.

    Raises:
        HTTPException: 400 on a ValueError (bad model/provider/empty definition),
            500 on a RuntimeError or unexpected backend error.
    """
    start = time.perf_counter()
    started_at = utc_now()
    try:
        result = run_pipeline(
            req.definition,
            disable_thinking=req.disable_thinking,
            model_provider=req.model_provider,
            model_name=req.model_name,
        )
        record_id = _record_run(user=user, request=request, req=req, started_at=started_at, result=result, error=None)
        auth_store.audit_event(
            action="decompose",
            user=user,
            request=request,
            status_code=200,
            latency_ms=round((time.perf_counter() - start) * 1000),
            metadata=_audit_metadata(req, record_id),
        )
        return DecomposeResponse(**result, record_id=record_id)
    except ValueError as e:
        auth_store.audit_event(
            action="decompose",
            user=user,
            request=request,
            status_code=400,
            latency_ms=round((time.perf_counter() - start) * 1000),
            metadata=_audit_metadata(req, None),
            error=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        record_id = _record_run(user=user, request=request, req=req, started_at=started_at, result=None, error=str(e))
        auth_store.audit_event(
            action="decompose",
            user=user,
            request=request,
            status_code=500,
            latency_ms=round((time.perf_counter() - start) * 1000),
            metadata=_audit_metadata(req, record_id),
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        detail = f"Unexpected backend error: {e}"
        record_id = _record_run(user=user, request=request, req=req, started_at=started_at, result=None, error=detail)
        auth_store.audit_event(
            action="decompose",
            user=user,
            request=request,
            status_code=500,
            latency_ms=round((time.perf_counter() - start) * 1000),
            metadata=_audit_metadata(req, record_id),
            error=detail,
        )
        raise HTTPException(status_code=500, detail=detail) from e
