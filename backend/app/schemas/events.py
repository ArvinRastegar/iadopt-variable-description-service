"""Discriminated union for the ``/api/decompose/stream`` NDJSON event protocol.

The stream endpoint emits newline-delimited JSON objects, each with a ``type``
discriminator. These models document that wire protocol exactly as produced by
``app.main._stream_event`` and consumed by the frontend:

* ``raw_delta`` — an incremental chunk of raw model output (``{type, delta}``).
* ``final`` — exactly one terminal success event carrying the full
  ``DecomposeResponse`` payload (``{type, data}``).
* ``error`` — a terminal failure event (``{type, detail}``).

These are a *documentation* contract for the streaming response (FastAPI cannot
attach a ``response_model`` to a raw ``StreamingResponse``); they are not yet used
to serialize the stream. Phase 2 may route ``_stream_event`` through these models.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from .responses import DecomposeResponse


class RawDeltaEvent(BaseModel):
    """An incremental chunk of raw LLM output during streaming."""

    type: Literal["raw_delta"] = "raw_delta"
    delta: str


class FinalEvent(BaseModel):
    """The terminal success event carrying the complete decomposition payload."""

    type: Literal["final"] = "final"
    data: DecomposeResponse


class ErrorEvent(BaseModel):
    """The terminal failure event carrying a human-readable error detail."""

    type: Literal["error"] = "error"
    detail: str


StreamEvent = Annotated[
    Union[RawDeltaEvent, FinalEvent, ErrorEvent],
    Field(discriminator="type"),
]
"""Any event that can appear on the decompose NDJSON stream, keyed by ``type``."""
