"""Byte-exact rendering of the measured decomposition prompt.

This module is the only place prompt byte-exactness is enforced. The measured
Close F1 of 0.4890 describes a specific sequence of bytes; a prompt differing by
one space in the demonstrations encoding is a different prompt that nothing has
measured. See ``docs/components/measured-prompt.md``.

Leaf service: depends only on ``core.config``. It does not call the model, does not
decide whether the measured configuration is in use, and does not validate output.
``services/prompts.py`` continues to serve the legacy path, untouched.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Dict, List, Optional

from ..core.config import settings

# The documented encoding. Every part matters: ensure_ascii=False keeps `→` and `Ω`
# as themselves, sort_keys=True fixes key order, and the separators remove the
# spaces json.dumps adds by default. See best-configuration-session.md section 4.
DEMONSTRATION_ENCODING = {
    "ensure_ascii": False,
    "sort_keys": True,
    "allow_nan": False,
    "separators": (",", ":"),
}

EXPECTED_DEMONSTRATIONS = 25

PLACEHOLDERS = ("{{schema}}", "{{demonstrations}}", "{{target_definition}}")

_PLACEHOLDER_RE = re.compile(r"\{\{(schema|demonstrations|target_definition)\}\}")


def _read_artifact(path: pathlib.Path, what: str) -> str:
    """Read a measured artifact verbatim, or raise naming the path.

    Args:
        path: The artifact path.
        what: Human-readable artifact name for the error message.

    Returns:
        The file's text content.

    Raises:
        RuntimeError: If the file is absent or unreadable.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Measured {what} is missing or unreadable: {path}") from exc


def load_measured_template() -> str:
    """Read the measured prompt template verbatim.

    Returns:
        The template text, not stripped — trailing whitespace is part of the
        measured bytes.

    Raises:
        RuntimeError: If the template file is absent or unreadable.
    """
    path = settings.measured_template_path
    if not path.exists():
        raise RuntimeError(f"Measured prompt template is missing: {path}")
    return _read_artifact(path, "prompt template")


def load_lexical_schema_text() -> str:
    r"""Read the lexical schema artifact verbatim, trailing newline included.

    The template line is ``{{schema}}\\n\\nORDERED DEMONSTRATIONS`` and the measured
    prompt has *three* newlines before ``ORDERED DEMONSTRATIONS``. The third comes
    from this file. Stripping it yields a prompt one byte short of the measured
    one — see D-008.

    Returns:
        The schema file's exact bytes as text, including its single trailing
        newline.

    Raises:
        RuntimeError: If the schema file is absent or unreadable.
    """
    path = settings.measured_lexical_schema_path
    if not path.exists():
        raise RuntimeError(f"Measured lexical schema is missing: {path}")
    return _read_artifact(path, "lexical schema")


def load_demonstrations() -> List[Dict[str, Any]]:
    """Read and parse the 25 measured demonstrations.

    Returns:
        The demonstration objects in stored order.

    Raises:
        RuntimeError: If the file is absent, unparseable, or is not exactly 25
            objects.
    """
    path = settings.measured_demonstrations_path
    if not path.exists():
        raise RuntimeError(f"Measured demonstrations file is missing: {path}")
    raw = _read_artifact(path, "demonstrations")

    try:
        demonstrations = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Measured demonstrations are not valid JSON: {settings.measured_demonstrations_path}"
        ) from exc

    if not isinstance(demonstrations, list) or len(demonstrations) != EXPECTED_DEMONSTRATIONS:
        # Rendering 24 or 26 demonstrations produces an unmeasured prompt that looks correct.
        found = len(demonstrations) if isinstance(demonstrations, list) else "a non-list"
        raise RuntimeError(f"Expected {EXPECTED_DEMONSTRATIONS} measured demonstrations, found {found}.")

    return demonstrations


def encode_demonstrations(demonstrations: List[Dict[str, Any]]) -> str:
    """Encode demonstrations with the measured JSON encoding.

    Args:
        demonstrations: The demonstration objects to encode.

    Returns:
        A single-line JSON array; 11,519 characters for the stored 25.

    Raises:
        ValueError: If a non-finite float is present (``allow_nan=False``).
    """
    try:
        return json.dumps(demonstrations, **DEMONSTRATION_ENCODING)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ValueError(f"Measured demonstrations are not JSON-encodable: {exc}") from exc


def render_measured_prompt(
    definition: str,
    *,
    template: Optional[str] = None,
    schema_text: Optional[str] = None,
    demonstrations: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Render the complete measured prompt for one target definition.

    All three placeholders are substituted in a single pass, so text introduced by
    one substitution is never scanned for another. The definition is inserted
    verbatim — not stripped, normalized, or escaped.

    Args:
        definition: The variable definition to decompose; non-empty after
            stripping.
        template: Pre-loaded template, or None to read from disk.
        schema_text: Pre-loaded schema text, or None to read from disk.
        demonstrations: Pre-loaded demonstrations, or None to read from disk.

    Returns:
        The complete prompt, ending in exactly one newline.

    Raises:
        ValueError: If the definition is empty or whitespace-only.
        RuntimeError: If any artifact is missing, malformed, or the template is
            missing a placeholder.
    """
    if not definition.strip():
        raise ValueError("Definition must not be empty.")

    template = load_measured_template() if template is None else template
    schema_text = load_lexical_schema_text() if schema_text is None else schema_text
    demonstrations = load_demonstrations() if demonstrations is None else demonstrations

    missing = [placeholder for placeholder in PLACEHOLDERS if placeholder not in template]
    if missing:
        raise RuntimeError(f"Measured template is missing placeholder(s): {', '.join(missing)}")

    replacements = {
        "schema": schema_text,
        "demonstrations": encode_demonstrations(demonstrations),
        "target_definition": definition,
    }

    # One pass: text introduced by a substitution is never rescanned, so a definition
    # containing "{{schema}}" stays literal rather than expanding into the schema.
    return _PLACEHOLDER_RE.sub(lambda match: replacements[match.group(1)], template)
