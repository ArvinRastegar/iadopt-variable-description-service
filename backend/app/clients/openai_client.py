"""OpenRouter (OpenAI-compatible) client construction and raw-request plumbing.

Builds and caches a single ``openai.OpenAI`` client pointed at the OpenRouter
endpoint, using the API key from :data:`app.core.config.settings`. Entity linking
posts its own requests with the shared HTTP session instead of the SDK, so that one
retry layer covers both providers (``services.entity_linking``); the URL, headers and
request body it needs are built here, as ``clients.psnc_client`` builds PSNC's.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from openai import OpenAI

from ..core.config import settings
from ..core.provider_keys import SENDABLE_BY_SDK, usable_key

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_openai_client: Optional[OpenAI] = None


def get_openai_client() -> OpenAI:
    """Return the cached OpenRouter client, creating it on first use.

    Returns:
        The shared ``OpenAI`` client configured for the OpenRouter base URL. Without a key that
        can be sent there is none (:func:`_openrouter_key` raises).
    """
    global _openai_client

    if _openai_client is None:
        _openai_client = OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=_openrouter_key(),
        )
    return _openai_client


def _openrouter_key() -> str:
    """Return OpenRouter's key; one that is missing or cannot be sent is refused (:func:`usable_key` raises)."""
    return usable_key("OPENROUTER_API_KEY", settings.openrouter_api_key, SENDABLE_BY_SDK)


def openrouter_chat_completions_url() -> str:
    """Return the OpenRouter chat-completions endpoint URL."""
    return f"{OPENROUTER_BASE_URL}/chat/completions"


def openrouter_chat_headers() -> Dict[str, str]:
    """Return the auth and content-type headers for a raw OpenRouter request.

    Returns:
        The header dict with the bearer token; without a key that can be sent there are none
        (:func:`_openrouter_key` raises).
    """
    return {
        "Authorization": f"Bearer {_openrouter_key()}",
        "Content-Type": "application/json",
    }


def build_openrouter_entity_link_payload(model: str, system_prompt: str, user_message: str) -> Dict[str, Any]:
    """Build the OpenRouter request body of the entity-linking question.

    The same question as PSNC's measured body (``clients.psnc_client.build_entity_link_payload``):
    the same messages, one generated token, temperature 0 and reasoning off. Reasoning is
    switched off the way the decomposition switches it off on OpenRouter
    (``reasoning.effort = none``), because ``chat_template_kwargs`` is a PSNC (vLLM) switch.
    No OpenRouter model was measured on this question (``decisions.md`` D-038).

    Args:
        model: OpenRouter model name, the one the decomposition used.
        system_prompt: The frozen instructions.
        user_message: The term, the variable definition and the labelled candidates.

    Returns:
        The request payload dict, with exactly the keys ``model``, ``messages``, ``stream``,
        ``temperature``, ``max_tokens`` and ``reasoning``.
    """
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": 1,
        "reasoning": {"effort": "none"},
    }
