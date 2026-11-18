"""Choose the vocabulary entry a label names: search, ask the run's model, ask again if needed.

One label goes through three steps. The vocabularies are searched (``vocabularies``), the
pooled candidates are shown with reasoning off in a seeded-hash order (``entity_choice``) to
the model that made the decomposition, and the model's one-character answer is mapped back to
a candidate. That model is a :class:`Linker`: ``Qwen3.8-27B`` on PSNC, the measured linker,
unless the run used another model on PSNC or OpenRouter (``decisions.md`` D-038). The model
cannot be restricted to answer in exactly that form, so an answer is checked strictly and, if
it is not valid, the identical request is sent again.

The attempt policy mirrors ``decisions.md`` D-020: the original request and up to three
retries, each sending the identical body. Retried: an invalid answer, a timeout or
connection error, and HTTP 408, 409, 429 and 5xx. Not retried: any other 4xx, which aborts
linking. There is one retry layer here and none below it, so an unreachable service cannot
multiply attempts.

When the attempts run out the outcome depends on what happened. If the model answered but
never validly, the label gets no link and the caller carries on with the others. If nothing
answered at all, :class:`RuntimeError` aborts linking, as a failing reranker did before
(``decisions.md`` D-032 replaced that reranker).

Depends on ``clients`` and the ``vocabularies`` and ``entity_choice`` services. Logs carry
source names, status codes and exception classes; never the label, a response body or a header.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence, Tuple

import requests

from ..clients.http import get_http_session
from ..clients.openai_client import openrouter_chat_completions_url, openrouter_chat_headers
from ..clients.psnc_client import psnc_chat_completions_url, psnc_chat_headers
from ..core.config import OPENROUTER_MODEL_PROVIDER, PSNC_MODEL_PROVIDER
from .call_log import CallLog, start_call
from .entity_choice import (
    MODEL,
    InvalidAnswer,
    build_request,
    normalise_term,
    presentation_order,
    query_key,
    read_choice,
)
from .vocabularies import Candidate, retrieve_pool

logger = logging.getLogger(__name__)

# The original request and three retries; D-020 counts attempts the same way.
MAX_ATTEMPTS = 4
# One token is generated, which took 0.3 s on average and 1.5 s at worst when measured.
CHAT_TIMEOUT_S = 30
# A longer label is not a concept name, and the QUDT search puts every word into a GET query.
MAX_TERM_CHARS = 200
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 4.0
RETRY_AFTER_CAP_S = 10.0
RETRYABLE_STATUSES = frozenset({408, 409, 429})

# Replaced in tests so that retries do not wait.
_sleep = time.sleep


@dataclass(frozen=True)
class Linker:
    """The model that answers the entity-linking question: the one that made the decomposition.

    Attributes:
        provider: ``psnc`` or ``openrouter``.
        model: The model name, as the decomposition request resolved it.
        calls: The run's call log, where each linking request is recorded (D-037, stage 2);
            not part of the linker's identity.
    """

    provider: str
    model: str
    calls: Optional[CallLog] = field(default=None, compare=False, repr=False)


# The configuration D-032 measured; it answers when no run names a model, as in direct calls.
MEASURED_LINKER = Linker(PSNC_MODEL_PROVIDER, MODEL)


def _endpoint(linker: Linker) -> Tuple[str, str, Dict[str, str]]:
    """Resolve where a linker's question is sent.

    Args:
        linker: The model that answers.

    Returns:
        ``(name, url, headers)``: the provider's name for messages, its chat-completions URL,
        and the request headers with its key.

    Raises:
        RuntimeError: If entity linking cannot ask that provider, or its key is not configured.
    """
    if linker.provider == PSNC_MODEL_PROVIDER:
        return "PSNC", psnc_chat_completions_url(), psnc_chat_headers()
    if linker.provider == OPENROUTER_MODEL_PROVIDER:
        return "OpenRouter", openrouter_chat_completions_url(), openrouter_chat_headers()
    raise RuntimeError(f"Entity linking cannot ask a model from provider {linker.provider!r}.")


def _pause_after(attempt: int, response: Optional[requests.Response]) -> float:
    """Decide how long to wait before the next attempt.

    Args:
        attempt: The 1-based number of the attempt that just failed.
        response: Its response, or ``None`` if there was none.

    Returns:
        Exponential backoff, or the response's ``Retry-After`` seconds when it gave a whole
        number; both are capped.
    """
    pause = min(BACKOFF_BASE_S * 2 ** (attempt - 1), BACKOFF_CAP_S)
    header = response.headers.get("Retry-After", "") if response is not None else ""
    if header.isascii() and header.isdigit():
        pause = min(float(header), RETRY_AFTER_CAP_S)
    return pause


def ask_for_choice(
    term: str,
    definition: str,
    shown: Sequence[Candidate],
    linker: Linker = MEASURED_LINKER,
) -> Optional[int]:
    """Ask the model which candidate names the term, asking again if the answer is not valid.

    Args:
        term: The normalised term.
        definition: The variable definition.
        shown: The candidates in presentation order.
        linker: The model that answers: the decomposition's, or the measured one by default.

    Returns:
        ``0`` when the model chose "none of these", the 1-based position of the candidate it
        chose, or ``None`` when it answered but never gave a valid answer.

    Raises:
        RuntimeError: If the linker's provider cannot be asked or is not configured, refuses
            the request with a status that is not retried, or never answers.

    Side effects:
        Up to ``MAX_ATTEMPTS`` HTTP POSTs to the linker's provider, all with the identical
        body, and sleeps between failed attempts. Each POST is recorded in the linker's call
        log, numbered per term.
    """
    name, url, headers = _endpoint(linker)
    payload = build_request(term, definition, shown, provider=linker.provider, model=linker.model)
    answered = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response: Optional[requests.Response] = None
        problem = ""
        pending = start_call(linker.calls, "linking", linker.provider, linker.model, key=term)
        try:
            response = get_http_session().post(url, headers=headers, json=payload, timeout=CHAT_TIMEOUT_S)
        except requests.RequestException as exc:
            problem = type(exc).__name__
            pending.finish("error", error=problem)
        else:
            if response.status_code == 200:
                answered = True
                usage = None
                try:
                    data = response.json()
                    usage = data.get("usage") if isinstance(data, dict) else None
                    choice = read_choice(data, len(shown))
                except ValueError as exc:
                    reason = str(exc) if isinstance(exc, InvalidAnswer) else "the body is not JSON"
                    pending.finish("unusable", error=reason, usage=usage)
                    logger.warning(
                        "entity linking: attempt %d of %d gave no valid answer (%s)", attempt, MAX_ATTEMPTS, reason
                    )
                    continue
                pending.finish("ok", usage=usage)
                return choice
            pending.finish("error", error=f"HTTP {response.status_code}")
            if response.status_code not in RETRYABLE_STATUSES and response.status_code < 500:
                raise RuntimeError(f"{name} refused the entity-linking request (HTTP {response.status_code}).")
            problem = f"HTTP {response.status_code}"
        logger.warning("entity linking: attempt %d of %d failed (%s)", attempt, MAX_ATTEMPTS, problem)
        if attempt < MAX_ATTEMPTS:
            _sleep(_pause_after(attempt, response))
    if answered:
        return None
    raise RuntimeError(f"{name} did not answer the entity-linking request.")


def choose_candidate(term: str, definition: str, linker: Linker = MEASURED_LINKER) -> Optional[Candidate]:
    """Find the vocabulary entry a label names.

    A :class:`RuntimeError` from :func:`ask_for_choice` (the linker's provider unusable) is
    not caught here.

    Args:
        term: The label as the prediction wrote it.
        definition: The variable definition, used only to tell which meaning of the term is
            intended.
        linker: The model that answers; see :func:`ask_for_choice`.

    Returns:
        The chosen candidate, or ``None`` when the label is empty or too long, no vocabulary
        offered a candidate, the model chose "none of these", or it never answered validly.

    Side effects:
        Up to three searches and one chat request, plus retries; see the module docstring.
    """
    text = normalise_term(term)
    if not text:
        return None
    if len(text) > MAX_TERM_CHARS:
        logger.warning("entity linking: a label of %d characters was not linked", len(text))
        return None
    key = query_key(text)
    pool, _failed = retrieve_pool(key)
    if not pool:
        return None
    shown = [pool[index] for index in presentation_order(pool, key)]
    position = ask_for_choice(text, definition, shown, linker)
    if not position:
        return None
    return shown[position - 1]
