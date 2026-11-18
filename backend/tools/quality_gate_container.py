"""Decompose definitions with the app's own pipeline, inside the backend container (D-043).

``backend/tools/quality_gate.py`` runs this file's source with ``docker compose exec -T backend
python -c <source> <concurrency>``, so the decompositions come from exactly the code, settings
and provider key of the running backend. It reads a JSON list of ``{"variable_id",
"definition"}`` from stdin and writes JSON lines to stdout: first one ``{"kind": "run"}`` line
naming the provider, model and prompt fingerprint, then one ``{"kind": "prediction"}`` line
per definition, in input order, with the raw answer the pipeline ended with, or its error.

It calls ``app.pipeline.run_pipeline`` with the app's default provider and model, as the
deployed service does: the decomposition with its retries, the label call and the linking
questions. It writes no record and no audit entry; those are written by the HTTP routes.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

MAX_ERROR_CHARS = 300


def decompose_one(item: Dict[str, str]) -> Dict[str, Any]:
    """Run the app's pipeline for one definition and report how it ended.

    Args:
        item: ``{"variable_id", "definition"}``.

    Returns:
        The prediction line's fields: the raw answer, or the error, with timing and tokens.
    """
    from app.pipeline import run_pipeline
    from app.services.call_log import CallLog

    calls = CallLog()
    started = time.perf_counter()
    raw = None
    error = None
    try:
        raw = run_pipeline(item["definition"], calls=calls).get("raw_llm_output")
    except Exception as exc:  # one failed definition must not stop the run; it scores as empty
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
    decomposition = [call for call in calls.calls() if call.purpose == "decomposition"]
    return {
        "variable_id": item["variable_id"],
        "ok": error is None and bool(raw),
        "raw_output": raw,
        "error": error if error is not None else (None if raw else "The pipeline returned no answer."),
        "seconds": round(time.perf_counter() - started, 3),
        "decomposition_tries": len(decomposition),
        "prompt_tokens": decomposition[-1].input_tokens if decomposition else None,
    }


def run_description() -> Dict[str, Any]:
    """Name what the backend runs: the default provider and model and the prompt fingerprint."""
    from app.core.config import settings
    from app.pipeline import prompt_setup

    provider = settings.default_model_provider
    setup, fingerprint = prompt_setup(provider)
    model = settings.psnc_model_name if provider == "psnc" else settings.model_name
    return {"kind": "run", "provider": provider, "model": model, "prompt_setup": setup, "prompt_sha256": fingerprint}


def main(concurrency: int) -> None:
    """Read the definitions from stdin and write the run line and one prediction line each."""
    items: List[Dict[str, str]] = json.load(sys.stdin)
    print(json.dumps(run_description()), flush=True)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for record in pool.map(decompose_one, items):
            print(json.dumps({"kind": "prediction", **record}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8)
