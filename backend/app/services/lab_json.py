"""The lab's strict JSON reading of a model's answer (``iadopt_lab.generation.extractor.extract_json``).

The best pipeline reads the matrix finder's answer and judges the three fill answers as the lab did, so it reads
them with the lab's own protocol, not the pipeline's looser parser: the whole answer when it is one JSON object (or one
JSON string holding one), else the one complete Markdown fence that parses, else the one balanced ``{...}`` span that
parses. Duplicate keys, ``NaN`` and infinite numbers are refused; more than one candidate is ambiguous. Nothing is
repaired.

Pure standard library; no dependency on the rest of the application.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

MAX_BYTES = 1_048_576
MAX_DEPTH = 64
MAX_FENCES = 64
MAX_CANDIDATES = 64
_FENCE = re.compile(r"(?m)^[ \t]*```(?:json)?[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*(?:\r?\n|$)", re.DOTALL | re.IGNORECASE)


def _unique_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """An object, refusing a key given twice.

    Raises:
        ValueError: If a key is given twice.
    """
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """Refuse the decoder's ``NaN`` and ``Infinity``.

    Raises:
        ValueError: Always.
    """
    raise ValueError("Non-finite JSON number is forbidden")


def _check_depth(text: str) -> None:
    """Bound the nesting, not counting brackets inside strings.

    Raises:
        ValueError: If the nesting is deeper than :data:`MAX_DEPTH`.
    """
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("JSON nesting safety limit exceeded")
        elif char in "]}":
            depth -= 1


def _strict_parse(text: str) -> Any:
    """One strict JSON value, or ``ValueError``."""
    _check_depth(text)
    result = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    # A very large exponent decodes to infinity without parse_constant.
    json.dumps(result, allow_nan=False, ensure_ascii=False).encode("utf-8")
    return result


def _parsed(text: str) -> Tuple[bool, Any]:
    """Whether a span parses strictly, and its value."""
    try:
        return True, _strict_parse(text)
    except (ValueError, RecursionError, UnicodeError):
        return False, None


def _balanced_spans(text: str) -> List[Tuple[int, int]]:
    """The complete outermost ``{...}`` spans, quote- and escape-aware.

    Raises:
        ValueError: If there are more than :data:`MAX_CANDIDATES`.
    """
    spans: List[Tuple[int, int]] = []
    start: Optional[int] = None
    depth, quoted, escaped = 0, False, False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append((start, index + 1))
                if len(spans) > MAX_CANDIDATES:
                    raise ValueError("Object candidate safety limit exceeded")
    return spans


def extract_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    """The one JSON object an answer holds, read by the lab's frozen protocol, or ``None``.

    Args:
        raw_text: The model's answer.

    Returns:
        The object, or ``None`` when the answer holds none, several, or one that is not strict JSON.
    """
    if not isinstance(raw_text, str) or len(raw_text.encode("utf-8", errors="surrogatepass")) > MAX_BYTES:
        return None
    try:
        raw_text.encode("utf-8")
    except UnicodeError:
        return None
    working, unwrapped = raw_text, False
    while True:
        parsed, value = _parsed(working.strip())
        if parsed and isinstance(value, dict):
            return value
        if parsed and isinstance(value, str):
            if unwrapped:
                return None
            unwrapped, working = True, value
            continue
        if parsed:
            return None
        break
    fences = list(_FENCE.finditer(working))
    if len(fences) > MAX_FENCES:
        return None
    found = [value for parsed, value in (_parsed(match.group(1)) for match in fences) if parsed]
    if found:
        return found[0] if len(found) == 1 and isinstance(found[0], dict) else None
    try:
        spans = _balanced_spans(working)
    except ValueError:
        return None
    objects = [value for parsed, value in (_parsed(working[start:end]) for start, end in spans)
               if parsed and isinstance(value, dict)]
    return objects[0] if len(objects) == 1 else None
