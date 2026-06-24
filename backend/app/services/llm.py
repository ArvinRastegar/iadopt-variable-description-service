"""LLM invocation, provider/model resolution, and response parsing.

Wraps both providers (OpenRouter via the OpenAI client, and PSNC via HTTP),
normalizes streamed/non-streamed responses to text, parses the JSON object out of
raw model output, and resolves the requested provider/model against the enabled
set. Depends on ``clients`` and ``core.config``.

The raw parsed LLM JSON deliberately stays ``Dict[str, Any]`` — it is free-form
until ``coerce_prediction`` normalizes the known onto keys.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx
import requests
from openai import APIStatusError, OpenAIError

from ..clients.openai_client import get_openai_client
from ..clients.http import get_http_session
from ..clients.psnc_client import (
    build_psnc_chat_payload,
    psnc_chat_completions_url,
    psnc_chat_headers,
)
from ..core.config import PSNC_MODEL_PROVIDER, settings

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


def build_chat_completion_request_kwargs(
    model: str,
    prompt: str,
    temperature: float,
    *,
    disable_thinking: bool = True,
    stream: bool = False,
) -> Dict[str, Any]:
    """Build kwargs for the OpenRouter chat-completions call.

    Args:
        model: Model name.
        prompt: User prompt.
        temperature: Sampling temperature.
        disable_thinking: When true, send ``reasoning.effort = none`` via extra_body.
        stream: Whether to request streaming.

    Returns:
        The kwargs dict for ``client.chat.completions.create``.
    """
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


def call_model(model: str, prompt: str, temperature: float, disable_thinking: bool = True) -> str:
    """Call the OpenRouter model with up to 3 attempts; return raw text or "".

    Args:
        model: Model name.
        prompt: User prompt.
        temperature: Sampling temperature.
        disable_thinking: Forwarded to the request builder.

    Returns:
        The raw model text, or ``""`` if all attempts fail or return HTML/empty.

    Side effects:
        Performs OpenRouter HTTP calls.
    """
    client = get_openai_client()

    for attempt in range(1, 4):
        try:
            resp = client.chat.completions.create(
                **build_chat_completion_request_kwargs(
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


def _extract_chat_completion_text(data: Dict[str, Any]) -> str:
    """Extract assistant text from a (non-streamed) chat-completions JSON body."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ""

    message = first_choice.get("message")
    if isinstance(message, dict):
        return flatten_text_fragments(message.get("content"))

    return flatten_text_fragments(first_choice.get("text"))


def call_psnc_model(model: str, prompt: str, temperature: float, disable_thinking: bool = True) -> str:
    """Call the PSNC model with up to 3 attempts; return raw text or "".

    Args:
        model: PSNC model name.
        prompt: User prompt.
        temperature: Sampling temperature.
        disable_thinking: Forwarded to the payload builder.

    Returns:
        The raw model text, or ``""`` if all attempts fail or return HTML/empty.

    Side effects:
        Performs PSNC HTTP calls.
    """
    url = psnc_chat_completions_url()
    headers = psnc_chat_headers()

    for attempt in range(1, 4):
        try:
            response = get_http_session().post(
                url,
                headers=headers,
                json=build_psnc_chat_payload(
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
    """Normalize a raw prediction: ensure onto keys exist with sane defaults.

    Args:
        pred: The raw parsed prediction (or ``None``).

    Returns:
        A new dict with all ``ONTO_KEYS`` present (``[]`` for ``hasConstraint``,
        ``""`` otherwise) and ``hasProperty`` flattened from dict to label.
    """
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
    """Extract and normalize the JSON object from raw model output.

    Args:
        raw: The raw model text (may include code fences/prose).
        definition: The original definition, written back into the result.

    Returns:
        The coerced prediction dict.

    Raises:
        ValueError: If no JSON object is found or it fails to decode.
    """
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


def resolve_model_provider(requested_provider: Optional[str]) -> str:
    """Resolve and validate the requested provider against the enabled set.

    Args:
        requested_provider: The requested provider, or ``None`` for the default.

    Returns:
        The resolved, lower-cased provider key.

    Raises:
        ValueError: If the provider is not enabled.
    """
    provider = (requested_provider or settings.default_model_provider).strip().lower()

    if not provider:
        provider = settings.default_model_provider

    if provider not in settings.enabled_model_providers:
        raise ValueError(
            f"Model provider '{provider}' is not enabled. "
            f"Enabled providers: {', '.join(settings.enabled_model_providers)}"
        )

    return provider


def resolve_model_name(requested_model_name: Optional[str], model_provider: Optional[str] = None) -> str:
    """Resolve and validate the requested model name for a provider.

    Args:
        requested_model_name: The requested model, or ``None`` for the provider default.
        model_provider: The provider; defaults to the configured default provider.

    Returns:
        The resolved model name.

    Raises:
        ValueError: If the model is not allowed for the provider.
    """
    if model_provider is None:
        model_provider = settings.default_model_provider

    default_model = settings.psnc_model_name if model_provider == PSNC_MODEL_PROVIDER else settings.model_name
    allowed_models = settings.psnc_model_names if model_provider == PSNC_MODEL_PROVIDER else settings.model_names
    selected_model = (requested_model_name or default_model).strip()

    if not selected_model:
        selected_model = default_model

    if selected_model not in allowed_models:
        raise ValueError(f"Unsupported model '{selected_model}'. Allowed models: {', '.join(allowed_models)}")

    return selected_model


def _as_plain_data(value: Any) -> Any:
    """Recursively convert pydantic models / tuples to plain JSON-ish data."""
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


def flatten_text_fragments(value: Any) -> str:
    """Flatten nested content/reasoning fragments into a single text string.

    Args:
        value: Any content value (str, list, dict, pydantic model, or None).

    Returns:
        The concatenated text.
    """
    plain_value = _as_plain_data(value)

    if plain_value is None:
        return ""
    if isinstance(plain_value, str):
        return plain_value
    if isinstance(plain_value, list):
        return "".join(flatten_text_fragments(item) for item in plain_value)
    if isinstance(plain_value, dict):
        fragments: List[str] = []

        for key in ("text", "summary", "reasoning"):
            if key in plain_value:
                fragments.append(flatten_text_fragments(plain_value[key]))

        if fragments:
            return "".join(fragments)

    return ""


def extract_stream_text_deltas(chunk: Any) -> Tuple[str, str]:
    """Extract ``(reasoning_delta, content_delta)`` from an OpenAI stream chunk."""
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return "", ""

    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return "", ""

    content_delta = flatten_text_fragments(getattr(delta, "content", None))

    reasoning_fragments: List[str] = []
    for attr_name in ("reasoning", "reasoning_content", "reasoning_text"):
        reasoning_fragments.append(flatten_text_fragments(getattr(delta, attr_name, None)))

    reasoning_fragments.append(flatten_text_fragments(getattr(delta, "reasoning_details", None)))

    reasoning_delta = "".join(fragment for fragment in reasoning_fragments if fragment)
    return reasoning_delta, content_delta


def extract_stream_text_deltas_from_dict(data: Dict[str, Any]) -> Tuple[str, str]:
    """Extract ``(reasoning_delta, content_delta)`` from a PSNC SSE JSON event."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return "", ""

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return "", ""

    delta = first_choice.get("delta")
    if isinstance(delta, dict):
        content_delta = flatten_text_fragments(delta.get("content"))
        reasoning_delta = "".join(
            flatten_text_fragments(delta.get(key))
            for key in ("reasoning", "reasoning_content", "reasoning_text", "reasoning_details")
        )
        return reasoning_delta, content_delta

    message = first_choice.get("message")
    if isinstance(message, dict):
        content_delta = flatten_text_fragments(message.get("content"))
        reasoning_delta = "".join(
            flatten_text_fragments(message.get(key))
            for key in ("reasoning", "reasoning_content", "reasoning_text", "reasoning_details")
        )
        return reasoning_delta, content_delta

    text_delta = flatten_text_fragments(first_choice.get("text"))
    return "", text_delta


def stream_psnc_model(
    model: str,
    prompt: str,
    temperature: float,
    *,
    disable_thinking: bool = True,
) -> Iterator[Tuple[str, str]]:
    """Stream a PSNC completion, yielding ``(reasoning_delta, content_delta)`` pairs.

    Args:
        model: PSNC model name.
        prompt: User prompt.
        temperature: Sampling temperature.
        disable_thinking: Forwarded to the payload builder.

    Yields:
        ``(reasoning_delta, content_delta)`` tuples per SSE event.

    Side effects:
        Opens a streaming HTTP POST to PSNC.
    """
    response = get_http_session().post(
        psnc_chat_completions_url(),
        headers=psnc_chat_headers(),
        json=build_psnc_chat_payload(
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

        yield extract_stream_text_deltas_from_dict(event)


def call_llm_loose(
    model_provider: str,
    model: str,
    prompt: str,
    definition: str,
    temperature: float,
    disable_thinking: bool = True,
) -> Tuple[str, Dict[str, Any]]:
    """Call the selected provider up to 3 times and parse JSON from the output.

    Args:
        model_provider: ``"psnc"`` or ``"openrouter"``.
        model: Model name.
        prompt: User prompt.
        definition: Original definition (written into the parsed result).
        temperature: Sampling temperature.
        disable_thinking: Forwarded to the provider call.

    Returns:
        A ``(raw_text, prediction)`` tuple; ``prediction`` is ``{}`` if parsing
        never succeeded.

    Side effects:
        Performs LLM HTTP calls.
    """
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


def stream_event(event_type: str, **payload: Any) -> str:
    """Serialize a stream event as a single NDJSON line.

    Args:
        event_type: The event ``type`` discriminator.
        **payload: Additional event fields.

    Returns:
        The JSON line (terminated by a newline).
    """
    return json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "\n"
