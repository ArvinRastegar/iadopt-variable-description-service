"""Process-wide warmup caches shared between startup and the services that read them.

These four caches are populated once at application startup (``warmup_assets``)
and read later by the validation service and the decompose pipeline. They live in
this neutral leaf so that neither the startup code nor the services need to import
each other — which would create an import cycle (see the Phase-2 dependency map).

Access them via :class:`AppState`'s module-level singleton ``app_state``. The
schema validator type is referenced lazily (``Any``) to keep this module free of
heavy imports.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class AppState:
    """Mutable holder for the assets cached at startup.

    Attributes:
        schema_cache: The pipeline-patched JSON schema dict, or ``None`` before warmup.
        validator_cache: The compiled ``Draft202012Validator``, or ``None``.
        prompt_version_cache: The selected prompt version name, or ``None``.
        examples_5_cache: The loaded five-shot examples, or ``None``.
    """

    def __init__(self) -> None:
        self.schema_cache: Optional[Dict[str, Any]] = None
        self.validator_cache: Optional[Any] = None
        self.prompt_version_cache: Optional[str] = None
        self.examples_5_cache: Optional[List[Dict[str, Any]]] = None


app_state = AppState()
"""The process-wide application-state singleton."""
