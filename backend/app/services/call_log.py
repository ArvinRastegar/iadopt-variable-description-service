"""The model calls of one decomposition run, one by one (D-037, stage 2).

A run asks models several things: the decomposition itself (streamed, with retries and a
non-streamed fallback), on the measured path the examples' embedding, the matrix finder and three
fill answers of which the most typical is kept (the lab's best pipeline), a label and comment, and
one linking question per distinct label (D-038). Each request sent to a model is one :class:`ModelCall`: what it
was for, which try it was, the provider and model, when it started and how long it took,
the tokens sent and received when the provider reports them, and whether its answer was used.

A :class:`CallLog` collects the calls of one run in memory, and what the run chose on the measured
path (its examples, its matrix statement, the answer it kept), which the record keeps. The decompose routes create it,
the pipeline and the services it calls add to it, and the record store writes it with the run
(``records.RecordStore.record_decomposition``), so a failed run keeps its calls too. Nothing
here does I/O, and no prompt, definition, label or answer text is kept, only a short reason
when a call failed or its answer could not be used.

Pure standard library; no dependency on the rest of the application.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, Hashable, List, Optional, Tuple

PURPOSES = frozenset({"decomposition", "label", "linking", "examples", "matrix"})
# ok: the answer was used; unusable: an answer came back but could not be used; error: none came back.
STATUSES = frozenset({"ok", "unusable", "error"})
MAX_ERROR_CHARS = 300


@dataclass(frozen=True)
class ModelCall:
    """One request sent to a model.

    Attributes:
        purpose: ``decomposition``, ``label``, ``linking``, ``examples`` (the embedding request that
            chose the measured prompt's examples) or ``matrix`` (the matrix finder).
        attempt: Which try of that purpose this was in the run, from 1; for linking, which try
            of the same label.
        provider: ``psnc`` or ``openrouter``.
        model: The model name (for ``examples``, the embedding model).
        started_at: When the request was sent, ISO 8601 in UTC.
        latency_ms: How long until the answer was complete or the request failed.
        input_tokens: Tokens sent, when the provider reported them.
        output_tokens: Tokens received, when the provider reported them.
        status: ``ok``, ``unusable`` or ``error``.
        error: A short reason when the status is not ``ok``.
    """

    purpose: str
    attempt: int
    provider: str
    model: str
    started_at: str
    latency_ms: int
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    status: str
    error: Optional[str]


def usage_tokens(usage: Any) -> Tuple[Optional[int], Optional[int]]:
    """Read the token counts from an OpenAI-style ``usage`` dict or object.

    Args:
        usage: The ``usage`` member of a response or of a stream's last chunk, or ``None``.

    Returns:
        ``(prompt_tokens, completion_tokens)``; each ``None`` unless it is a whole number of
        at least zero.
    """
    if usage is None:
        return None, None

    def read(name: str) -> Optional[int]:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return read("prompt_tokens"), read("completion_tokens")


def short_reason(reason: Any) -> Optional[str]:
    """Turn an exception or message into a short, single-line reason.

    Args:
        reason: An exception, a message, or ``None``.

    Returns:
        The reason with whitespace collapsed and at most ``MAX_ERROR_CHARS`` characters, or
        ``None`` when there is nothing to say. An exception without a message gives its class name.
    """
    if reason is None:
        return None
    text = str(reason) or (type(reason).__name__ if isinstance(reason, BaseException) else "")
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


class PendingCall:
    """A request that has been sent and not yet finished; :meth:`finish` records it once."""

    def __init__(
        self, log: Optional["CallLog"], purpose: str, provider: str, model: str, key: Optional[Hashable]
    ) -> None:
        """Note the start of a request.

        Args:
            log: Where the call is recorded, or ``None`` to record nothing.
            purpose: One of :data:`PURPOSES`.
            provider: The provider the request goes to.
            model: The model the request names.
            key: Calls with the same purpose and key are numbered together; ``None`` for one
                count per purpose.

        Raises:
            ValueError: If the purpose is not one of :data:`PURPOSES`.
        """
        if purpose not in PURPOSES:
            raise ValueError(f"Unknown model call purpose {purpose!r}")
        self._log = log
        self._purpose = purpose
        self._provider = provider or ""
        self._model = model or ""
        self._key = key
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._started = time.perf_counter()
        self._ended: Optional[float] = None
        self._finished = False

    def answered(self) -> None:
        """Note that the answer is complete: the latency ends now, also when the call is finished later.

        The best pipeline finishes its three fill calls only once it has chosen among their answers.
        """
        if self._ended is None:
            self._ended = time.perf_counter()

    def finish(self, status: str, *, error: Any = None, usage: Any = None) -> None:
        """Record how the request ended; only the first call counts.

        Args:
            status: One of :data:`STATUSES`.
            error: Why the answer was not used or did not come, if it was not ``ok``.
            usage: The provider's ``usage`` data, if any.

        Raises:
            ValueError: If the status is not one of :data:`STATUSES`.
        """
        if status not in STATUSES:
            raise ValueError(f"Unknown model call status {status!r}")
        if self._finished:
            return
        self._finished = True
        if self._log is None:
            return
        input_tokens, output_tokens = usage_tokens(usage)
        self._log._add(
            self._key,
            ModelCall(
                purpose=self._purpose,
                attempt=0,
                provider=self._provider,
                model=self._model,
                started_at=self._started_at,
                latency_ms=max(0, round(((self._ended or time.perf_counter()) - self._started) * 1000)),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                status=status,
                error=None if status == "ok" else short_reason(error),
            ),
        )


class CallLog:
    """The model calls of one run, in the order they finished."""

    def __init__(self) -> None:
        """Start an empty log."""
        self._calls: List[ModelCall] = []
        self._counts: Dict[Tuple[str, Optional[Hashable]], int] = {}
        self._lock = threading.Lock()
        self._choices: Dict[str, Any] = {}

    def _add(self, key: Optional[Hashable], call: ModelCall) -> None:
        """Number a finished call within its purpose and key, and keep it."""
        with self._lock:
            count = self._counts.get((call.purpose, key), 0) + 1
            self._counts[(call.purpose, key)] = count
            self._calls.append(replace(call, attempt=count))

    def mark_unusable(self, purpose: str, reason: Any, *, attempt: Optional[int] = None) -> None:
        """Mark the last call of a purpose as unusable when its answer turned out unusable later.

        A call that returned text is recorded as ``ok`` where it is made; the caller may only
        find out afterwards, when parsing, that the text was of no use.

        Args:
            purpose: The purpose of the call to mark.
            reason: Why its answer could not be used.
            attempt: Mark the call with this try number instead of the last one: the kept one of
                several answers.
        """
        with self._lock:
            for index in range(len(self._calls) - 1, -1, -1):
                call = self._calls[index]
                if call.purpose == purpose and (attempt is None or call.attempt == attempt):
                    if call.status == "ok":
                        self._calls[index] = replace(call, status="unusable", error=short_reason(reason))
                    return

    def calls(self) -> List[ModelCall]:
        """Return a copy of the calls recorded so far, in order."""
        with self._lock:
            return list(self._calls)

    def adopt(self, other: "CallLog") -> None:
        """Append another log's calls, numbered within this one: requests sent side by side keep a fixed order.

        Args:
            other: A log no request writes to any more.
        """
        for call in other.calls():
            self._add(None, call)

    def update_choices(self, **choices: Any) -> None:
        """Keep what the run chose on the measured path: ``examples``, ``matrix`` and ``answers``."""
        with self._lock:
            self._choices.update(copy.deepcopy(choices))

    def choices(self) -> Optional[Dict[str, Any]]:
        """What the run chose on the measured path, or ``None`` when it chose nothing (the legacy path)."""
        with self._lock:
            return copy.deepcopy(self._choices) if self._choices else None


def start_call(
    log: Optional[CallLog], purpose: str, provider: str, model: str, *, key: Optional[Hashable] = None
) -> PendingCall:
    """Note that a request is about to be sent; with no log, nothing will be recorded.

    Args:
        log: The run's log, or ``None``.
        purpose: One of :data:`PURPOSES`.
        provider: The provider the request goes to.
        model: The model the request names.
        key: Numbering key within the purpose (the label, for linking), or ``None``.

    Returns:
        The pending call, to :meth:`PendingCall.finish` when the request has ended.
    """
    return PendingCall(log, purpose, provider, model, key)


def mark_unusable(log: Optional[CallLog], purpose: str, reason: Any, *, attempt: Optional[int] = None) -> None:
    """Mark the last call of a purpose as unusable, if there is a log.

    Args:
        log: The run's log, or ``None``.
        purpose: The purpose of the call to mark.
        reason: Why its answer could not be used.
        attempt: Mark the call with this try number instead of the last one.
    """
    if log is not None:
        log.mark_unusable(purpose, reason, attempt=attempt)
