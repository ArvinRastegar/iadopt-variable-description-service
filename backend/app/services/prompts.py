"""Prompt assembly: load instruction templates and few-shot examples, build prompts.

Reads the prompt-version templates from ``data/prompts`` and the preferred
few-shot examples from ``data/Json_preferred``, and assembles the final prompt
string (instructions + JSON-Schema + examples + the variable definition) sent to
the LLM. Leaf service: depends only on ``core.config``.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Optional

from ..core.config import settings

_EXAMPLE_HDR = "\n\n### Examples (valid against the same schema)\n"
_USER_HDR = "\n\n### Variable's definition to decompose\n"
_EXPECTED_HDR = "\n\n### Expected output\n*(only the JSON object)*"


def list_prompt_versions(prompt_dir: pathlib.Path) -> List[str]:
    """List available prompt-version names (``*.txt`` stems), sorted.

    Args:
        prompt_dir: Directory holding prompt templates.

    Returns:
        Sorted version names, or an empty list if the directory is absent.
    """
    if not prompt_dir.exists():
        return []
    return sorted(p.stem for p in prompt_dir.glob("*.txt"))


def load_prompt_instructions(prompt_dir: pathlib.Path, prompt_version: str) -> str:
    """Load the instruction text for a prompt version (falling back to the first).

    Args:
        prompt_dir: Directory holding prompt templates.
        prompt_version: Requested version; the first available is used if missing.

    Returns:
        The stripped instruction text.

    Raises:
        RuntimeError: If no templates exist in ``prompt_dir``.
    """
    versions = list_prompt_versions(prompt_dir)
    if not versions:
        raise RuntimeError(f"No prompt templates found in {prompt_dir}")

    if not prompt_version or prompt_version not in versions:
        prompt_version = versions[0]

    return (prompt_dir / f"{prompt_version}.txt").read_text(encoding="utf-8").strip()


def strip_all_uri_fields(obj: Any) -> Any:
    """Recursively drop ``*URI`` and dunder keys so examples show only labels.

    Args:
        obj: Any JSON-ish value.

    Returns:
        A copy with URI/private keys removed.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if "URI" in k:
                continue
            if k.startswith("__"):
                continue
            out[k] = strip_all_uri_fields(v)
        return out
    if isinstance(obj, list):
        return [strip_all_uri_fields(x) for x in obj]
    return obj


def format_example_block(ex: Dict[str, Any], idx: int) -> str:
    """Render one few-shot example as a definition + expected-JSON block.

    Args:
        ex: The example object.
        idx: 1-based example number for the heading.

    Returns:
        The formatted markdown block.
    """
    definition = ex.get("definition") or ex.get("comment") or ""
    ex_no_uris = strip_all_uri_fields(ex)
    return (
        f"\n\n#### Example {idx}\n"
        f"Variable's definition to decompose: {definition}\n\n"
        f"Expected output:\n{json.dumps(ex_no_uris, indent=2, ensure_ascii=False)}"
    )


def load_examples(folder: pathlib.Path, n: int) -> List[Dict[str, Any]]:
    """Load up to ``n`` few-shot example JSON files from ``folder``, sorted by name.

    Args:
        folder: Directory of example ``*.json`` files.
        n: Maximum number of examples to load (``<= 0`` yields none).

    Returns:
        The parsed example dicts.
    """
    if n <= 0 or not folder.exists():
        return []
    paths = sorted(folder.glob("*.json"))
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths[:n]]


def build_prompt(definition: str, prompt_version: str, examples: Optional[List[Dict[str, Any]]] = None) -> str:
    """Assemble the full LLM prompt for a variable definition.

    Args:
        definition: The variable definition to decompose.
        prompt_version: The instruction template version to use.
        examples: Optional few-shot examples to include.

    Returns:
        The complete prompt string (instructions + schema + examples + definition).
    """
    examples = examples or []
    instructions = load_prompt_instructions(settings.prompt_dir, prompt_version)
    schema_path = settings.schema_path
    schema_text = schema_path.read_text(encoding="utf-8").strip() if schema_path.exists() else "{SCHEMA_PLACEHOLDER}"

    ex_block = ""
    if examples:
        blocks = [format_example_block(ex, i + 1) for i, ex in enumerate(examples)]
        ex_block = _EXAMPLE_HDR + "".join(blocks)

    return (
        f"{instructions}\n\n"
        f"### JSON-Schema\n{schema_text}\n"
        f"{ex_block}"
        f"{_USER_HDR}{definition}"
        f"{_EXPECTED_HDR}"
    )
