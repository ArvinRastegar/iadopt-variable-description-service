#!/usr/bin/env python3
"""Release quality gate: the running app's Close F1 on the lab's 102-variable corpus (D-043).

Run it before merging ``dev`` into ``prod``, from the application repository root, with the
lab's Python, while the local stack runs the code to release (``docker compose up -d
--build``)::

    ../i-adopt-llm-based-service/iadopt-lab/.venv/bin/python backend/tools/quality_gate.py

It decomposes all 102 corpus definitions with the app's own pipeline inside the backend
container (``quality_gate_container.py``: the app's default provider, model and prompt, and
no records written), scores every answer with the lab's extraction, validation and Close F1
scorer, and passes when the micro Close F1 of this one run is at least the threshold (0.60).
An answer that fails or cannot be used counts as an empty prediction, as in the lab's
official figures.

Read the result with D-043's caveats: the owner chose all 102 variables and a single run. 25
of the 102 are the prompt's own examples, whose answers the model sees, which raises the
score; and one run varies by about 0.02 from the next.

Exit codes: 0 at or above the threshold, 1 below it, 2 when the gate could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

REPO = pathlib.Path(__file__).resolve().parents[2]
CONTAINER_PROGRAM = pathlib.Path(__file__).with_name("quality_gate_container.py")
DEFAULT_LAB = pathlib.Path.home() / "Documents/GitHub/i-adopt-llm-based-service/iadopt-lab"
DEFAULT_THRESHOLD = 0.60
DEFAULT_CONCURRENCY = 8
DEFAULT_REPORT_DIR = REPO / "quality-gate-reports"
EXPECTED_VARIABLES = 102
EXIT_PASS, EXIT_FAIL, EXIT_ERROR = 0, 1, 2


class GateError(RuntimeError):
    """The gate could not run, so it decided nothing."""


@dataclass(frozen=True)
class Scorer:
    """The lab's scoring steps, passed in so the gate's own logic can be tested without the lab.

    Attributes:
        extract: Raw answer text to an extraction result with ``success`` and ``candidate``.
        validate: A candidate to a validation result with ``valid``.
        evaluate: ``(gold, prediction, variable_id)`` to one item's evaluation record.
        empty: A new empty prediction.
        aggregate: ``(records, variable_ids)`` to micro ``f1``, ``precision`` and ``recall``.
    """

    extract: Callable[[str], Any]
    validate: Callable[[Any], Any]
    evaluate: Callable[[Mapping[str, Any], Any, str], Dict[str, Any]]
    empty: Callable[[], Any]
    aggregate: Callable[[List[Dict[str, Any]], List[str]], Dict[str, float]]


def load_lab(lab: pathlib.Path) -> Tuple[List[Dict[str, Any]], Scorer]:
    """Load the corpus and the scorer from a lab checkout, exactly as the lab's side tests do.

    Args:
        lab: The ``iadopt-lab`` directory.

    Returns:
        The 102 canonical records (``variable_id``, ``definition``, ``gold``) and the scorer.

    Raises:
        GateError: When this Python is not the lab's, or the lab cannot be read.
    """
    try:
        from iadopt_eval import evaluate_item
        from iadopt_eval.core import aggregate_items
        from iadopt_lab.cli import _configuration, _similarity_backend
        from iadopt_lab.corpus.ingestion import load_canonical_records
        from iadopt_lab.generation.extractor import extract_json
        from iadopt_lab.validation import load_schema_bytes, validate_prediction
        from iadopt_lab.validation.lexical import empty_prediction
    except ImportError as exc:
        raise GateError(f"run this with the lab's Python, iadopt-lab/.venv/bin/python ({exc})") from exc
    try:
        records = [dict(record) for record in load_canonical_records(lab)]
        schema = load_schema_bytes(lab)
        similarity = _similarity_backend(lab, _configuration(lab).data["evaluation"])
    except (OSError, ValueError, KeyError) as exc:
        raise GateError(f"could not load the corpus or the scorer from {lab}: {exc}") from exc

    def evaluate(gold: Mapping[str, Any], prediction: Any, variable_id: str) -> Dict[str, Any]:
        return evaluate_item(gold, prediction, similarity, metadata={"variable_id": variable_id},
                             similarity_identity=similarity.identity)

    def aggregate(evidence: List[Dict[str, Any]], ids: List[str]) -> Dict[str, float]:
        if not evidence:
            return {"f1": 0.0, "precision": 0.0, "recall": 0.0}
        result = aggregate_items(list(evidence), expected_variable_ids=list(ids))
        return {name: result["close"]["metrics"][name]["value"] for name in ("f1", "precision", "recall")}

    return records, Scorer(
        extract=extract_json,
        validate=lambda candidate: validate_prediction(candidate, schema),
        evaluate=evaluate,
        empty=empty_prediction,
        aggregate=aggregate,
    )


def parse_container_output(lines: Sequence[str]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Read the container program's JSON lines; anything else it printed is ignored.

    Args:
        lines: The container's standard output, line by line.

    Returns:
        The run line, and the prediction lines by variable id.

    Raises:
        GateError: When no run line came.
    """
    run: Optional[Dict[str, Any]] = None
    predictions: Dict[str, Dict[str, Any]] = {}
    for line in lines:
        try:
            message = json.loads(line)
        except ValueError:
            continue  # the app's own log lines are not part of the protocol
        if not isinstance(message, dict):
            continue
        if message.get("kind") == "run":
            run = message
        elif message.get("kind") == "prediction" and "variable_id" in message:
            predictions[str(message["variable_id"])] = message
    if run is None:
        raise GateError("the backend container did not report what it runs")
    return run, predictions


def run_predictions(
    items: Sequence[Mapping[str, str]],
    concurrency: int,
    *,
    popen: Callable[..., Any] = subprocess.Popen,
    cwd: pathlib.Path = REPO,
    service: str = "backend",
    progress: Optional[Callable[[int], None]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Decompose the definitions in the running backend container.

    Args:
        items: ``{"variable_id", "definition"}`` for each variable.
        concurrency: How many definitions the container decomposes at once.
        popen: Starts the process; replaced in tests.
        cwd: Where ``docker compose`` finds the stack (the repository root).
        service: The compose service of the backend.
        progress: Called with the number of predictions received so far.

    Returns:
        What :func:`parse_container_output` returns.

    Raises:
        GateError: When the container command fails or reports nothing.
    """
    command = ["docker", "compose", "exec", "-T", service, "python", "-c",
               CONTAINER_PROGRAM.read_text(encoding="utf-8"), str(concurrency)]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        try:
            process = popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                            text=True, encoding="utf-8")
        except OSError as exc:
            raise GateError(f"could not start docker compose: {exc}") from exc
        process.stdin.write(json.dumps(items, ensure_ascii=False))
        process.stdin.close()
        lines: List[str] = []
        done = 0
        for line in process.stdout:
            lines.append(line)
            if progress is not None and '"kind": "prediction"' in line:
                done += 1
                progress(done)
        code = process.wait()
        errors.seek(0)
        stderr = errors.read()
    if code != 0:
        raise GateError(f"the backend container exited with {code}; is the stack running? {stderr.strip()[-500:]}")
    return parse_container_output(lines)


def score(records: Sequence[Mapping[str, Any]], predictions: Mapping[str, Mapping[str, Any]],
          scorer: Scorer) -> Dict[str, Any]:
    """Score every record's answer; one that failed, is missing or is unusable scores as empty.

    Args:
        records: The corpus records, with ``variable_id`` and ``gold``.
        predictions: The container's prediction lines by variable id.
        scorer: The lab's scoring steps.

    Returns:
        ``official`` (every record counted, as the lab reports), ``as_run`` (usable answers only),
        the ``counts`` of each outcome, and one ``rows`` entry per record.
    """
    all_evidence: List[Dict[str, Any]] = []
    all_ids: List[str] = []
    usable_evidence: List[Dict[str, Any]] = []
    usable_ids: List[str] = []
    rows: List[Dict[str, Any]] = []
    for record in records:
        variable_id = str(record["variable_id"])
        prediction = predictions.get(variable_id)
        evidence: Optional[Dict[str, Any]] = None
        if prediction is None:
            outcome = "missing"
        elif not prediction.get("ok") or not prediction.get("raw_output"):
            outcome = "failed"
        else:
            extracted = scorer.extract(str(prediction["raw_output"]))
            if extracted.success and scorer.validate(extracted.candidate).valid:
                evidence = scorer.evaluate(record["gold"], extracted.candidate, variable_id)
                outcome = "scored"
                usable_evidence.append(evidence)
                usable_ids.append(variable_id)
            else:
                outcome = "unusable"
        if evidence is None:
            evidence = scorer.evaluate(record["gold"], scorer.empty(), variable_id)
        all_evidence.append(evidence)
        all_ids.append(variable_id)
        rows.append({
            "variable_id": variable_id,
            "outcome": outcome,
            "error": (prediction or {}).get("error"),
            "seconds": (prediction or {}).get("seconds"),
            "decomposition_tries": (prediction or {}).get("decomposition_tries"),
            "raw_output": (prediction or {}).get("raw_output"),
        })
    return {
        "official": scorer.aggregate(all_evidence, all_ids),
        "as_run": scorer.aggregate(usable_evidence, usable_ids),
        "counts": dict(Counter(row["outcome"] for row in rows)),
        "rows": rows,
    }


def write_report(directory: pathlib.Path, report: Mapping[str, Any]) -> pathlib.Path:
    """Write the run's report as JSON and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"quality-gate-{report['finished_at'].replace(':', '').replace('-', '')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    """Read the command line; the lab path defaults to ``IADOPT_LAB_PATH`` or the sibling checkout."""
    parser = argparse.ArgumentParser(description="Release quality gate on the lab's 102-variable corpus (D-043).")
    configured = os.environ.get("IADOPT_LAB_PATH")
    parser.add_argument("--lab", type=pathlib.Path,
                        default=pathlib.Path(configured).expanduser() if configured else DEFAULT_LAB,
                        help="the iadopt-lab directory (default: IADOPT_LAB_PATH or the sibling checkout)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="minimum micro Close F1 (0.60)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help="definitions decomposed at once in the container (8)")
    parser.add_argument("--report-dir", type=pathlib.Path, default=DEFAULT_REPORT_DIR,
                        help="where the JSON report goes (quality-gate-reports/)")
    return parser.parse_args(argv)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    load: Callable[[pathlib.Path], Tuple[List[Dict[str, Any]], Scorer]] = load_lab,
    predict: Callable[..., Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]] = run_predictions,
) -> int:
    """Run the gate and print its verdict.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.
        load: Loads the corpus and the scorer; replaced in tests.
        predict: Runs the decompositions; replaced in tests.

    Returns:
        The exit code: 0 pass, 1 below the threshold, 2 could not run.
    """
    args = parse_args(argv)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        records, scorer = load(args.lab)
        if len(records) != EXPECTED_VARIABLES:
            raise GateError(f"the corpus holds {len(records)} variables, not {EXPECTED_VARIABLES}")
        items = [{"variable_id": str(r["variable_id"]), "definition": str(r["definition"])} for r in records]
        print(f"Decomposing {len(items)} corpus definitions in the backend container...", file=sys.stderr)
        run, predictions = predict(
            items, args.concurrency,
            progress=lambda done: print(f"\r  {done}/{len(items)}", end="", file=sys.stderr, flush=True),
        )
        print(file=sys.stderr)
        result = score(records, predictions, scorer)
    except GateError as exc:
        print(f"QUALITY GATE COULD NOT RUN: {exc}", file=sys.stderr)
        return EXIT_ERROR

    f1 = float(result["official"]["f1"])
    passed = f1 >= args.threshold
    report = {
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "threshold": args.threshold,
        "verdict": "pass" if passed else "fail",
        "run": run,
        "variables": len(records),
        **result,
    }
    path = write_report(args.report_dir, report)
    counts = ", ".join(f"{count} {outcome}" for outcome, count in sorted(result["counts"].items()))
    print(f"Backend: {run.get('provider')} {run.get('model')}, prompt {run.get('prompt_setup')} "
          f"{str(run.get('prompt_sha256'))[:16]}…")
    print(f"Variables: {len(records)} ({counts})")
    print(f"Micro Close F1: {f1:.4f} (precision {result['official']['precision']:.4f}, "
          f"recall {result['official']['recall']:.4f}); usable answers only: {result['as_run']['f1']:.4f}")
    print(f"Threshold {args.threshold:.2f}: {'PASS' if passed else 'FAIL'}")
    print(f"Report: {path}")
    return EXIT_PASS if passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
