"""Decomposition routes: model options, non-streaming and streaming decompose."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..core.config import OPENROUTER_MODEL_PROVIDER, PSNC_MODEL_PROVIDER, settings
from ..core.dependencies import API_PREFIX, auth_store, require_current_user
from ..pipeline import run_pipeline, stream_pipeline_events
from ..schemas import DecomposeResponse, ModelOptionsResponse
from ..schemas.responses import ProviderConfig
from ..schemas.requests import DecomposeRequest

router = APIRouter()


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
    """Stream raw LLM output chunks first, then emit the final structured decompose payload."""
    start = time.perf_counter()
    request_payload = req.model_dump()

    def audited_events() -> Iterator[str]:
        """Yield pipeline NDJSON lines, then audit the stream outcome in a finally block."""
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
    """Run the full decomposition pipeline and return one final payload; audit the outcome.

    Raises:
        HTTPException: 400 on a ValueError (bad model/provider/empty definition),
            500 on a RuntimeError or unexpected backend error.
    """
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
