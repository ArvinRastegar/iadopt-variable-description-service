"""OpenRouter (OpenAI-compatible) client construction.

Builds and caches a single ``openai.OpenAI`` client pointed at the OpenRouter
endpoint, using the API key from :data:`app.core.config.settings`.
"""

from __future__ import annotations

from typing import Optional

from openai import OpenAI

from ..core.config import settings

_openai_client: Optional[OpenAI] = None


def get_openai_client() -> OpenAI:
    """Return the cached OpenRouter client, creating it on first use.

    Returns:
        The shared ``OpenAI`` client configured for the OpenRouter base URL.

    Raises:
        RuntimeError: If ``OPENROUTER_API_KEY`` is not configured.
    """
    global _openai_client

    if _openai_client is None:
        if not settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set.")
        _openai_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.openrouter_api_key,
        )
    return _openai_client
