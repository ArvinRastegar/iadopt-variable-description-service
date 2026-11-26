"""Decomposition routes: model options, non-streaming and streaming decompose.

For a signed-in account every run starts a decomposition record (D-036): the run, every
request it sent to a model (D-037, stage 2), and its draft as version 0. The audit log keeps
the model choice, timing and errors of a run, and no longer the definition or the result,
which the record holds (D-024).

A run of an account under the daily limit, which is switched off unless a setting is set, reserves a
use before any model is called; how it ends decides whether the use is kept or given back (D-021).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Dict, Generator, Optional, Sequence

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from ..access.admission import NOT_COUNTED, Use, admit
from ..access.dependencies import require
from ..access.policy import Capability
from ..auth import utc_now
from ..core.config import OPENROUTER_MODEL_PROVIDER, PSNC_MODEL_PROVIDER, settings
from ..core.dependencies import API_PREFIX, auth_store, record_store, require_current_user
from ..core.logging import get_logger
from ..core.provider_keys import without_keys
from ..pipeline import prompt_setup, run_pipeline, stream_pipeline_events
from ..schemas import DecomposeResponse, ModelOptionsResponse
from ..schemas.responses import ProviderConfig
from ..schemas.requests import DecomposeRequest
from ..services.call_log import CallLog, ModelCall
from ..services.llm import resolve_model_name, resolve_model_provider, stream_event
from ..usage import Outcome, daily_limit

router = APIRouter()
logger = get_logger(__name__)

NDJSON = "application/x-ndjson"

_CLIENT_ERROR = re.compile(r"HTTP (4\d\d)")
"""How the call log names a provider's answer with a client error (``services.llm.call_error``)."""

_TRANSIENT = {"408", "409", "429"}
"""The client errors that pass, which are worth another try (D-020)."""

_NEVER_SENT = re.compile(
    r"(MissingSchema|InvalidSchema|InvalidURL|InvalidHeader|UnicodeEncodeError|ValueError: Invalid header value)\b"
)
"""How the call log names a request that could not be built: for an address with a mistake in it, or for a key
with one. Such a key is refused before there is a call (``core.provider_keys``); its texts stay here for one the
HTTP libraries refuse nevertheless, and they start with these words also with the key taken out."""


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


def _reserve(user: Dict[str, Any], req: DecomposeRequest) -> Use:
    """Reserve a use for a run of an account under the daily limit, or refuse it.

    It comes before any model is called. A request the pipeline refuses anyway takes no use.

    Returns:
        What to finish when the run ends.
    """
    if daily_limit() is None or _is_bad_request(req):
        return NOT_COUNTED
    return admit(user["id"])


def _failure(calls: Sequence[ModelCall]) -> Outcome:
    """Say how a run ended that gave no result (D-021): it counts when a model may have worked on it.

    Args:
        calls: The requests the run sent to a model.
    """
    tries = [call for call in calls if call.purpose == "decomposition"]
    if all(_was_never_sent(call) for call in tries):  # also when there is none
        return Outcome.NOT_SENT
    if all(_was_never_sent(call) or _was_refused(call) for call in tries):
        return Outcome.REFUSED
    return Outcome.FAILED


def _was_never_sent(call: ModelCall) -> bool:
    """Return whether a request could not be built, so that nothing reached the provider."""
    return call.status == "error" and _NEVER_SENT.match(call.error or "") is not None


def _was_refused(call: ModelCall) -> bool:
    """Return whether the provider refused a request for good: its credentials, its configuration or its input."""
    client_error = _CLIENT_ERROR.fullmatch(call.error or "") if call.status == "error" else None
    return client_error is not None and client_error.group(1) not in _TRANSIENT


class RunResponse(StreamingResponse):
    """Send a run's events, and end the run and its use with the response.

    Starlette does not close the events of a browser that left. Their end, which writes to the database,
    would wait for garbage collection and then happen in whatever thread that is, also one that holds the
    database's write lock; and until its use has ended, a restart would give it back.
    """

    def __init__(self, events: Generator[str, None, None], use: Use) -> None:
        """Stream a run's NDJSON lines.

        Args:
            events: The lines.
            use: What the run reserved.
        """
        super().__init__(events, media_type=NDJSON)
        self._events = events
        self._use = use

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the events, then end what a browser that left has broken off.

        A server that stops cancels its responses. Such a run is interrupted, so its use is left for the
        next start to give back.
        """
        stopped = False
        try:
            await super().__call__(scope, receive, send)
        except anyio.get_cancelled_exc_class():
            stopped = True
            raise
        finally:
            if not stopped:
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(self._end)

    def _end(self) -> None:
        """End the use and the run, unless they have ended."""
        self._use.finish(Outcome.CLIENT_LEFT)
        self._events.close()


def _record_run(
    *,
    user: Dict[str, Any],
    request: Request,
    req: DecomposeRequest,
    started_at: datetime,
    result: Optional[Dict[str, Any]],
    error: Optional[str],
    calls: Sequence[ModelCall] = (),
) -> Optional[str]:
    """Write a run to the account's decomposition record; a failure here never fails the run.

    ``calls`` are the requests the run sent to a model, recorded with it.

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
            calls=calls,
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
    dependencies=[Depends(require(Capability.DECOMPOSE))],
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
    use = _reserve(user, req)

    def audited_events() -> Generator[str, None, None]:
        """Yield pipeline NDJSON lines, then record and audit the stream outcome in a finally block."""
        final_payload: Optional[Dict[str, Any]] = None
        error_detail: Optional[str] = None
        status_code = 200
        record_id: Optional[str] = None
        calls = CallLog()
        events = stream_pipeline_events(
            req.definition,
            disable_thinking=req.disable_thinking,
            model_provider=req.model_provider,
            model_name=req.model_name,
            calls=calls,
        )
        try:
            for line in events:
                try:
                    event = json.loads(line)
                    if event.get("type") == "final":
                        use.finish(Outcome.SUCCESS)
                        final_payload = event.get("data")
                        record_id = _record_run(
                            user=user,
                            request=request,
                            req=req,
                            started_at=started_at,
                            result=final_payload,
                            error=None,
                            calls=calls.calls(),
                        )
                        if isinstance(final_payload, dict):
                            line = stream_event("final", data={**final_payload, "record_id": record_id})
                    elif event.get("type") == "error":
                        use.finish(_failure(calls.calls()))
                        error_detail = event.get("detail") or "Streaming backend error."
                        status_code = 500
                except Exception:
                    pass
                yield line
            use.finish(_failure(calls.calls()))
        except Exception as e:
            use.finish(_failure(calls.calls()))
            error_detail = without_keys(e)
            status_code = 500
            raise
        finally:
            # Closing the pipeline first records a try the client interrupted mid-answer.
            close = getattr(events, "close", None)
            if close is not None:
                close()
            if final_payload is None and not bad_request:
                record_id = _record_run(
                    user=user,
                    request=request,
                    req=req,
                    started_at=started_at,
                    result=None,
                    error=error_detail or "The decomposition stopped before it finished.",
                    calls=calls.calls(),
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

    if daily_limit() is None:
        return StreamingResponse(audited_events(), media_type=NDJSON)
    return RunResponse(audited_events(), use)


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
    dependencies=[Depends(require(Capability.DECOMPOSE))],
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
    calls = CallLog()
    use = _reserve(user, req)
    try:
        result = run_pipeline(
            req.definition,
            disable_thinking=req.disable_thinking,
            model_provider=req.model_provider,
            model_name=req.model_name,
            calls=calls,
        )
        use.finish(Outcome.SUCCESS)
        record_id = _record_run(
            user=user, request=request, req=req, started_at=started_at, result=result, error=None, calls=calls.calls()
        )
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
        use.finish(_failure(calls.calls()))
        detail = without_keys(e)
        auth_store.audit_event(
            action="decompose",
            user=user,
            request=request,
            status_code=400,
            latency_ms=round((time.perf_counter() - start) * 1000),
            metadata=_audit_metadata(req, None),
            error=detail,
        )
        raise HTTPException(status_code=400, detail=detail) from e
    except RuntimeError as e:
        use.finish(_failure(calls.calls()))
        detail = without_keys(e)
        record_id = _record_run(
            user=user, request=request, req=req, started_at=started_at, result=None, error=detail, calls=calls.calls()
        )
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
    except Exception as e:
        use.finish(_failure(calls.calls()))
        detail = f"Unexpected backend error: {without_keys(e)}"
        record_id = _record_run(
            user=user, request=request, req=req, started_at=started_at, result=None, error=detail, calls=calls.calls()
        )
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
