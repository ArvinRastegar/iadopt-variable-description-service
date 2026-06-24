"""PSNC/PCSS HTTP plumbing: request payloads, auth headers, and endpoint URLs.

This module owns the wire-level details of talking to the PSNC LiteLLM-compatible
service (chat completions and reranking). Business logic (retries, parsing) lives
in the ``services`` layer; this module only builds requests and resolves URLs from
:data:`app.core.config.settings`.
"""

from __future__ import annotations

from typing import Any, Dict

from ..core.config import settings


def build_psnc_chat_payload(
    model: str,
    prompt: str,
    temperature: float,
    *,
    disable_thinking: bool = True,
    stream: bool = False,
) -> Dict[str, Any]:
    """Build the JSON body for a PSNC chat-completions request.

    Args:
        model: PSNC model name.
        prompt: The full user prompt.
        temperature: Sampling temperature.
        disable_thinking: When true, add the Qwen thinking-disable switches.
        stream: Whether to request a streamed response.

    Returns:
        The request payload dict.
    """
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


def psnc_chat_headers() -> Dict[str, str]:
    """Return the auth + content-type headers for PSNC requests.

    Returns:
        The header dict with the bearer token.

    Raises:
        RuntimeError: If ``PSNC_API_KEY`` is not configured.
    """
    if not settings.psnc_api_key:
        raise RuntimeError("PSNC_API_KEY is not set.")

    return {
        "Authorization": f"Bearer {settings.psnc_api_key}",
        "Content-Type": "application/json",
    }


def psnc_chat_completions_url() -> str:
    """Return the PSNC chat-completions endpoint URL."""
    return f"{settings.psnc_api_base_url.rstrip('/')}/v1/chat/completions"


def psnc_rerank_url() -> str:
    """Return the PSNC rerank endpoint URL."""
    return f"{settings.psnc_api_base_url.rstrip('/')}/v1/rerank"
