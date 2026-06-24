"""Minimal configuration leaf for the model-provider settings shared with schemas.

Phase 1 scope: this module exists so ``schemas`` can read the provider
configuration without importing ``app.main`` (which would be circular —
``main → schemas → main``). It deliberately covers only the provider selection
needed at schema-definition time.

The full typed ``Settings`` consolidation of all ~37 environment variables is a
Phase-2 task; this leaf is the seed for it. To avoid two sources of truth in the
meantime, ``app.main`` imports ``ENABLED_MODEL_PROVIDERS`` / ``DEFAULT_MODEL_PROVIDER``
from here rather than re-deriving them.

No new dependency is introduced: parsing uses ``os.getenv`` exactly as ``app.main``
did before, so the resolved values are unchanged.
"""

from __future__ import annotations

import os
import pathlib
from typing import List, Optional

from dotenv import load_dotenv

# Load the repo-root .env before reading any variable, so importing this leaf
# resolves the same values whether or not app.main has run its own load_dotenv
# yet. In containers the variables already live in the process environment
# (docker-compose env_file/environment), so this is a no-op there; load_dotenv
# never overrides an existing environment variable.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
load_dotenv(_REPO_ROOT / ".env")

OPENROUTER_MODEL_PROVIDER = "openrouter"
PSNC_MODEL_PROVIDER = "psnc"
SUPPORTED_MODEL_PROVIDERS = (OPENROUTER_MODEL_PROVIDER, PSNC_MODEL_PROVIDER)


def _parse_enabled_model_providers(configured: Optional[str]) -> List[str]:
    """Parse the ``ENABLED_MODEL_PROVIDERS`` CSV into a validated provider list.

    Behavior-identical to the original ``app.main._parse_enabled_model_providers``.

    Args:
        configured: Raw CSV value of ``ENABLED_MODEL_PROVIDERS`` or ``None``.

    Returns:
        The ordered, de-duplicated list of enabled provider keys. Defaults to all
        supported providers when unset/blank.

    Raises:
        RuntimeError: If an unsupported provider is named, or the list is empty.
    """
    if configured is None or not configured.strip():
        return list(SUPPORTED_MODEL_PROVIDERS)

    providers: List[str] = []
    for value in configured.split(","):
        provider = value.strip().lower()
        if provider and provider not in providers:
            providers.append(provider)

    unsupported = [provider for provider in providers if provider not in SUPPORTED_MODEL_PROVIDERS]
    if unsupported:
        raise RuntimeError(
            "Unsupported ENABLED_MODEL_PROVIDERS value(s): "
            f"{', '.join(unsupported)}. Supported providers: {', '.join(SUPPORTED_MODEL_PROVIDERS)}"
        )
    if not providers:
        raise RuntimeError("ENABLED_MODEL_PROVIDERS must enable at least one provider.")
    return providers


class _ProviderSettings:
    """Resolved model-provider configuration, read once from the environment.

    Attributes:
        enabled_model_providers: Ordered list of enabled provider keys.
        default_model_provider: The default provider; the configured value when it
            is enabled, otherwise the first enabled provider.
    """

    def __init__(self) -> None:
        self.enabled_model_providers: List[str] = _parse_enabled_model_providers(
            os.getenv("ENABLED_MODEL_PROVIDERS")
        )
        configured_default = os.getenv("DEFAULT_MODEL_PROVIDER", "").strip().lower()
        self.default_model_provider: str = (
            configured_default
            if configured_default in self.enabled_model_providers
            else self.enabled_model_providers[0]
        )


settings = _ProviderSettings()
"""Process-wide provider settings, resolved at import time."""
