"""JSON-Schema and semantic validation of LLM predictions.

Loads and pipeline-patches ``data/Json_schema.json`` (relaxing the
``hasConstraint`` minimum so partial predictions validate during generation), runs
the Draft 2020-12 validator, and formats human-readable error lines. Also provides
the semantic check that constraint ``on`` targets point at a real property/entity
label in the prediction.

Leaf service: depends only on ``core.text``, ``core.state``, and ``core.config``.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any, Dict, List, Optional

from jsonschema import Draft202012Validator

from ..core.config import settings
from ..core.state import app_state
from ..core.text import lookup_key


def format_path(err: Any) -> str:
    """Render a JSON-Schema error path as a ``$.a.b[0]`` style string.

    Args:
        err: A ``jsonschema`` validation error.

    Returns:
        The dotted/indexed path string, ``"$"`` for the root.
    """
    if not err.path:
        return "$"
    out = "$"
    for p in err.path:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += f".{p}"
    return out


def safe_preview(value: Any, limit: int = 200) -> str:
    """Render a JSON-ish preview of an offending value, truncated to ``limit``.

    Args:
        value: Any value to preview.
        limit: Maximum length before truncation with an ellipsis.

    Returns:
        A short string preview.
    """
    try:
        s = json.dumps(value, ensure_ascii=False)
    except Exception:
        s = repr(value)
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def patch_schema_for_pipeline(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the schema with ``hasConstraint.minItems`` relaxed 1 → 0.

    The pipeline validates partial predictions that may legitimately have no
    constraints; the on-disk schema requires at least one.

    Args:
        schema: The raw schema dict.

    Returns:
        A deep-copied, patched schema dict.
    """
    patched = copy.deepcopy(schema)

    try:
        hc = patched["properties"]["hasConstraint"]
        if isinstance(hc, dict) and hc.get("minItems", None) == 1:
            hc["minItems"] = 0
    except Exception:
        pass

    return patched


def load_schema(schema_path: pathlib.Path) -> Dict[str, Any]:
    """Load and parse the JSON schema from disk.

    Args:
        schema_path: Path to the schema file.

    Returns:
        The parsed schema dict.

    Raises:
        RuntimeError: If the file does not exist.
    """
    if not schema_path.exists():
        raise RuntimeError(f"Schema file not found: {schema_path}")
    return json.loads(schema_path.read_text(encoding="utf-8"))


def get_schema_validation_errors(
    instance: Dict[str, Any],
    *,
    schema_path: Optional[pathlib.Path] = None,
    schema: Optional[Dict[str, Any]] = None,
    label_for_logs: Optional[str] = None,
) -> List[str]:
    """Validate a prediction against the schema and return formatted error lines.

    Uses the warmup-cached validator when available; otherwise builds one from the
    provided ``schema`` or by loading ``schema_path`` (defaulting to the configured
    schema path).

    Args:
        instance: The prediction dict to validate.
        schema_path: Optional override schema path; defaults to ``settings.schema_path``.
        schema: Optional explicit schema dict (takes precedence over the cache/path).
        label_for_logs: Optional variable label for the error header.

    Returns:
        A list of human-readable error lines; empty when valid.
    """
    if schema_path is None:
        schema_path = settings.schema_path

    if schema is not None:
        validator = Draft202012Validator(patch_schema_for_pipeline(schema))
    elif app_state.validator_cache is not None:
        validator = app_state.validator_cache
    else:
        schema = patch_schema_for_pipeline(load_schema(schema_path))
        validator = Draft202012Validator(schema)

    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))

    if not errors:
        return []

    header = "Schema validation failed"
    if label_for_logs:
        header += f" for variable: {label_for_logs}"

    lines: List[str] = [header, "-" * len(header)]

    max_errs = 30
    for i, err in enumerate(errors[:max_errs], start=1):
        path = format_path(err)
        offending_value = safe_preview(err.instance)

        lines.append(f"{i:02d}) Path: {path}")
        lines.append(f"    Error: {err.message}")
        lines.append(f"    Offending value: {offending_value}")

        if (
            path.startswith("$.hasObjectOfInterest")
            or path.startswith("$.hasMatrix")
            or path.startswith("$.hasContextObject")
        ):
            lines.append(
                "    Hint: This error is inside an entityOrSystem field "
                "(string vs AsymmetricSystem vs SymmetricSystem)."
            )

    if len(errors) > max_errs:
        lines.append(f"... plus {len(errors) - max_errs} more errors.")

    return lines


def collect_constraint_target_keys(pred: Dict[str, Any]) -> List[str]:
    """Return the normalized labels constraints are allowed to point at.

    Walks the property/entity/system fields of the prediction and collects every
    human-readable label (normalized via :func:`lookup_key`), de-duplicated in order.

    Args:
        pred: The prediction dict.

    Returns:
        The ordered list of allowed constraint-target lookup keys.
    """
    keys: List[str] = []

    def add_value(value: Any) -> None:
        if isinstance(value, str):
            clean_value = lookup_key(value)
            if clean_value:
                keys.append(clean_value)
            return

        if not isinstance(value, dict):
            return

        for field_name in (
            "AsymmetricSystem",
            "SymmetricSystem",
            "hasSource",
            "hasTarget",
            "hasNumerator",
            "hasDenominator",
        ):
            add_value(value.get(field_name))

        for part_label in value.get("hasPart") or []:
            add_value(part_label)

    for field_name in (
        "hasProperty",
        "hasObjectOfInterest",
        "hasStatisticalModifier",
        "hasMatrix",
        "hasContextObject",
    ):
        add_value(pred.get(field_name))

    seen = set()
    ordered: List[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)

    return ordered


def get_constraint_semantic_validation_errors(pred: Dict[str, Any]) -> List[str]:
    """Flag constraints whose ``on`` target is not a real property/entity label.

    Args:
        pred: The prediction dict.

    Returns:
        A list of error strings, one per dangling constraint target; empty when all
        constraint targets resolve (or there are no targets to check against).
    """
    allowed_targets = collect_constraint_target_keys(pred)
    if not allowed_targets:
        return []

    errors: List[str] = []
    for idx, constraint in enumerate(pred.get("hasConstraint") or [], start=1):
        if not isinstance(constraint, dict):
            continue

        constraint_on = lookup_key(constraint.get("on") or "")
        if not constraint_on or constraint_on in allowed_targets:
            continue

        errors.append(
            f"Constraint target error at $.hasConstraint[{idx - 1}].on: "
            f"'{constraint.get('on')}' does not match any extracted property/entity label. "
            f"Allowed targets: {', '.join(allowed_targets)}"
        )

    return errors
