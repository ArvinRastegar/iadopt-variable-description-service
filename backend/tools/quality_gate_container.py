"""Decompose definitions with the app's own pipeline, inside the backend container (D-043).

``backend/tools/quality_gate.py`` runs this file's source with ``docker compose exec -T backend
python -c <source> <argument>``, so the decompositions come from exactly the code, settings and
provider key of the running backend.

- With the argument ``describe`` it writes one ``{"kind": "run"}`` line naming what the backend
  runs (provider, model, sampling, prompt setup and fingerprint, the measured prompt's example
  definitions, and a digest of its code and data) and calls no model.
- With a number, it reads a JSON list of ``{"variable_id", "definition"}`` from stdin and writes
  the run line, then one ``{"kind": "prediction"}`` line per definition, in input order,
  decomposing that many definitions at once.

It calls ``app.pipeline.run_pipeline`` with the app's default provider and model, as the
deployed service does: the decomposition with its retries, the label call and the linking
questions. It writes no record and no audit entry; those are written by the HTTP routes. The
app prints diagnostics to standard output from its worker threads, so the protocol lines go to
a copy of the original standard output and everything else goes to standard error.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, TextIO

MAX_ERROR_CHARS = 300
CODE_DIRECTORIES = ("app", "data")
BYTECODE_SUFFIXES = (".pyc", ".pyo", ".pyd")


def code_digest(root: pathlib.Path) -> str:
    """Fingerprint the backend's code and data as the image holds them.

    The container computes it on ``/app`` and the gate on ``backend/``, so equal digests mean the
    container runs this working tree. Every file under ``app/`` and ``data/`` counts by its
    relative path and content; bytecode, ``__pycache__`` and dotfiles are skipped, because the
    image may hold them or not.

    Args:
        root: The directory that holds ``app/`` and ``data/``.

    Returns:
        A SHA-256 hex digest.
    """
    files = []
    for directory in CODE_DIRECTORIES:
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if (not path.is_file() or path.suffix in BYTECODE_SUFFIXES
                    or any(part == "__pycache__" or part.startswith(".") for part in relative.parts)):
                continue
            files.append((relative.as_posix(), path))
    digest = hashlib.sha256()
    for name, path in sorted(files):
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def decompose_one(item: Dict[str, str]) -> Dict[str, Any]:
    """Run the app's pipeline for one definition and report how it ended.

    Args:
        item: ``{"variable_id", "definition"}``.

    Returns:
        The prediction line's fields: the raw answer or the error, whether the provider failed
        every decomposition request (``provider_failed``), with timing and tokens.
    """
    from app.pipeline import run_pipeline
    from app.services.call_log import CallLog

    calls = CallLog()
    started = time.perf_counter()
    raw = None
    error = None
    try:
        raw = run_pipeline(item["definition"], calls=calls).get("raw_llm_output")
    except Exception as exc:  # one failed definition must not stop the run; the gate counts it
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
    if error is None and not raw:
        error = "The pipeline returned no answer."
    decomposition = [call for call in calls.calls() if call.purpose == "decomposition"]
    provider_failed = bool(decomposition) and all(call.status == "error" for call in decomposition)
    if provider_failed:
        error = f"The provider never answered: {decomposition[-1].error}"[:MAX_ERROR_CHARS]
    return {
        "variable_id": item["variable_id"],
        "ok": error is None,
        "provider_failed": provider_failed,
        "raw_output": raw,
        "error": error,
        "seconds": round(time.perf_counter() - started, 3),
        "decomposition_tries": len(decomposition),
        "prompt_tokens": decomposition[-1].input_tokens if decomposition else None,
    }


def run_description() -> Dict[str, Any]:
    """Name what the backend runs: provider, model, sampling, prompt, examples and code.

    ``example_definitions`` lists the definitions the measured prompt shows with their answers,
    so the gate can score the variables the model has not seen apart from those; it is ``None``
    when the backend runs another prompt, whose examples this program does not read.
    ``code_sha256`` is :func:`code_digest` of the code and data the backend runs.
    """
    from app.core.config import settings
    from app.pipeline import prompt_setup
    from app.services.measured_prompt import load_demonstrations

    provider = settings.default_model_provider
    setup, fingerprint = prompt_setup(provider)
    model = settings.psnc_model_name if provider == "psnc" else settings.model_name
    examples = [d["definition"] for d in load_demonstrations()] if setup == "measured" else None
    root = pathlib.Path(str(importlib.import_module("app.pipeline").__file__)).resolve().parents[1]
    return {"kind": "run", "provider": provider, "model": model, "temperature": settings.temperature,
            "top_p": settings.top_p, "max_tokens": settings.max_tokens, "prompt_setup": setup,
            "prompt_sha256": fingerprint, "example_definitions": examples, "code_sha256": code_digest(root)}


def protocol_stream() -> TextIO:
    """Give the protocol its own copy of standard output and send everything else to standard error.

    The app prints diagnostics from worker threads, and such a print can land between a protocol
    line and its newline and spoil it. File descriptor 1, which ``print``, logging and C code
    write to, is pointed at standard error, which the gate keeps apart.

    Returns:
        A line-buffered text stream on the original standard output.
    """
    sys.stdout.flush()
    stream = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    return stream


def main(argument: str, out: Optional[TextIO] = None) -> None:
    """Describe the backend, or decompose the definitions on stdin.

    Args:
        argument: ``describe``, or how many definitions to decompose at once.
        out: Where the protocol lines go; the original standard output when ``None``.
    """
    out = out if out is not None else protocol_stream()
    out.write(json.dumps(run_description()) + "\n")
    if argument != "describe":
        items: List[Dict[str, str]] = json.load(sys.stdin)
        with ThreadPoolExecutor(max_workers=max(1, int(argument))) as pool:
            for record in pool.map(decompose_one, items):
                out.write(json.dumps({"kind": "prediction", **record}, ensure_ascii=False) + "\n")
    out.flush()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "8")
