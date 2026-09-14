"""Generate the ``label`` and ``comment`` the measured prompt forbids.

The measured template says verbatim "Do not regenerate the definition, label, or
comment", but ``rdf_ttl`` needs both — it falls back to the literal string
"generated variable" and to "" respectively. A second, separate LLM call supplies
them. See ``docs/decisions.md`` D-002 and ``docs/components/label-comment.md``.

This module is **not** part of the measured configuration and carries none of its
evidence. Its prompt has never been scored against anything.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple

from ..core.config import PSNC_MODEL_PROVIDER
from .llm import call_model, call_psnc_model

_JSON_FENCE_RE = re.compile(r"```(?:json)?", re.MULTILINE)
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_TRAILING_PUNCTUATION = " \t\n,.;:-—–"

MAX_LABEL_CHARS = 80

LABEL_COMMENT_TEMPLATE = """Write a short label and a short comment for this scientific variable definition.

label = a concise human-readable name, at most 80 characters. Not a sentence.
comment = a one-sentence summary of the definition. Do not add new concepts.

Definition:
{definition}

Return exactly one JSON object with the keys "label" and "comment", without prose,
Markdown fences, or explanations.
"""


def build_label_comment_prompt(definition: str) -> str:
    """Build the labelling prompt for a definition.

    Args:
        definition: The variable definition; non-empty after stripping.

    Returns:
        The prompt text, with the definition substituted in one pass.

    Raises:
        ValueError: If the definition is empty or whitespace-only.
    """
    if not definition.strip():
        raise ValueError("Definition must not be empty.")

    # One substitution pass, so a definition containing "{definition}" stays literal.
    head, _, tail = LABEL_COMMENT_TEMPLATE.partition("{definition}")
    return head + definition + tail


def _shorten(text: str) -> str:
    """Collapse whitespace and cut to MAX_LABEL_CHARS on a word boundary.

    Args:
        text: The text to shorten.

    Returns:
        The shortened text, stripped of trailing punctuation; never longer than
        ``MAX_LABEL_CHARS``.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_LABEL_CHARS:
        return collapsed.strip(_TRAILING_PUNCTUATION)

    window = collapsed[: MAX_LABEL_CHARS + 1]
    cut = window.rfind(" ")
    shortened = window[:cut] if cut > 0 else collapsed[:MAX_LABEL_CHARS]
    return shortened.strip(_TRAILING_PUNCTUATION)


def _usable(value: Any) -> bool:
    """Report whether a parsed field is a usable non-blank string."""
    return isinstance(value, str) and bool(value.strip())


def _parse_label_comment(raw: str) -> Dict[str, Any]:
    """Extract the label/comment object from raw model text.

    Args:
        raw: The raw model output, possibly fenced or surrounded by prose.

    Returns:
        The parsed object, or an empty dict when nothing usable was found.
    """
    match = _JSON_BLOCK_RE.search(_JSON_FENCE_RE.sub("", raw or "").strip())
    if not match:
        return {}

    try:
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return {}

    return parsed if isinstance(parsed, dict) else {}


def fallback_label_and_comment(definition: str) -> Tuple[str, str]:
    """Derive a label and comment from the definition alone, deterministically.

    The label is the definition with whitespace collapsed, cut at the last
    whitespace at or before 80 characters and stripped of trailing punctuation.
    The comment is the definition verbatim. Both are strictly better than the
    ``rdf_ttl`` defaults they replace.

    Args:
        definition: The variable definition; non-empty after stripping.

    Returns:
        A ``(label, comment)`` tuple, both non-empty; label at most 80 characters.

    Raises:
        ValueError: If the definition is empty or whitespace-only.
    """
    if not definition.strip():
        raise ValueError("Definition must not be empty.")

    return _shorten(definition), definition


def generate_label_and_comment(
    definition: str,
    *,
    model_provider: str,
    model: str,
    temperature: float,
) -> Tuple[str, str]:
    """Generate a label and comment via one LLM call, falling back per field.

    Never raises for a model or transport problem: any failure of the call, the
    parse, or the extraction becomes the deterministic fallback. A decomposition
    must never fail because labelling failed.

    Args:
        definition: The variable definition; non-empty after stripping.
        model_provider: The resolved provider the decomposition used.
        model: The resolved model name the decomposition used.
        temperature: The sampling temperature the decomposition used.

    Returns:
        A ``(label, comment)`` tuple, both non-empty; label at most 80 characters.

    Raises:
        ValueError: If the definition is empty or whitespace-only.
    """
    if not definition.strip():
        raise ValueError("Definition must not be empty.")

    fallback_label, fallback_comment = fallback_label_and_comment(definition)

    try:
        prompt = build_label_comment_prompt(definition)
        # One attempt from here: call_psnc_model / call_model already retry 3 times
        # internally, and a second retry layer would stall a user-facing request.
        raw = (
            call_psnc_model(model, prompt, temperature)
            if model_provider == PSNC_MODEL_PROVIDER
            else call_model(model, prompt, temperature)
        )
        parsed = _parse_label_comment(raw)
    except Exception as exc:  # noqa: BLE001 - labelling must never break a decomposition
        print(f"Label/comment generation failed: {exc}")
        return fallback_label, fallback_comment

    # Per-field fallback: a good comment survives a missing label and vice versa.
    label = _shorten(parsed["label"]) if _usable(parsed.get("label")) else fallback_label
    comment = parsed["comment"].strip() if _usable(parsed.get("comment")) else fallback_comment

    return label or fallback_label, comment or fallback_comment
