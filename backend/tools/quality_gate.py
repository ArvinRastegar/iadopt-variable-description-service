#!/usr/bin/env python3
"""Release quality gate: the running app's Close F1 on the lab's 102-variable corpus (D-043).

Run it before merging ``dev`` into ``prod``, from the application repository root, with the
lab's Python, while the local stack runs the code to release (``docker compose up -d
--build``)::

    ../i-adopt-llm-based-service/iadopt-lab/.venv/bin/python backend/tools/quality_gate.py

First it asks the backend container what it runs, which calls no model, and stops when the
container's code and data differ from this working tree or, unless ``--any-backend``, when the
backend is not the measured configuration ``prod`` runs by default. Then it decomposes all 102
corpus definitions with the app's own pipeline inside the container
(``quality_gate_container.py``: the app's default provider, model and prompt, and no records
written) and scores every answer with the lab's extraction, validation and Close F1 scorer, as
the lab's example-layout side test (D-042) scores its answers. An answer the app failed to
produce, or one the lab cannot use, counts as an empty prediction. The gate passes when the
micro Close F1 of this one run is at least the threshold (0.60).

Definitions the provider never answered, or whose answer was lost, count as empty too, which
makes the score a lower bound: it may still pass, but below the threshold the gate cannot tell
the code from a provider outage and does not decide.

The backend runs the lab's best pipeline: the 25 corpus variables most similar to each definition
as examples (by PSNC's embedding model), the matrix finder's statement, and the most typical of
three answers. The gate leaves each scored variable out of its own examples and of the finder's,
so no prompt shows the answer being scored. When PSNC fails the embedding request, that prompt
shows the 25 fixed examples instead, and when it fails the finder's request, it states no matrix:
below the threshold with such failures the gate does not decide either. It prints how many fell
back or got no statement, for which cause, which matrix decisions the finder stated, and which of
the three answers was kept.

Read the result with D-043's caveats: the owner chose all 102 variables and a single run, and runs
vary (a standard deviation of 0.019 over repeated runs on the lab's 24 held-out variables, not
measured on all 102). The gate also prints, for information, the score of the 25 fixed examples and
of the other 77. Before the best pipeline the 25 were the prompt's examples and saw their own
answers: the first run (2026-11-18) passed at 0.695 on all 102, with 0.571 on the 77 and 0.976 on
the 25. The lab measured 0.694 for the best pipeline (ten runs on PSNC, each variable's fold of 17
left out of its examples rather than only the variable).

Exit codes: 0 at or above the threshold, 1 below it, 2 when the gate could not run or decide.
Every run that got answers writes a JSON report with every raw answer to
``quality-gate-reports/``.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import runpy
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

REPO = pathlib.Path(__file__).resolve().parents[2]
CONTAINER_PROGRAM = pathlib.Path(__file__).with_name("quality_gate_container.py")
DEFAULT_LAB = REPO.parent / "i-adopt-llm-based-service" / "iadopt-lab"
DEFAULT_THRESHOLD = 0.60
DEFAULT_CONCURRENCY = 8
DEFAULT_REPORT_DIR = REPO / "quality-gate-reports"
EXPECTED_VARIABLES = 102
# The measured configuration `prod` runs by default (D-001, with D-042's prompt layout). The
# gate refuses another backend unless --any-backend is given; change this with that default.
RELEASE_BACKEND = {"provider": "psnc", "model": "Qwen3.8-27B", "prompt_setup": "measured-best-pipeline",
                   "temperature": 0.5, "top_p": 1.0, "max_tokens": 16000}
CHOICE_FIELDS = ("prompt_tokens", "examples", "examples_error", "matrix", "matrix_status", "matrix_error",
                 "answers_kept")
UNANSWERED = ("no_answer", "missing")
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


def now() -> str:
    """The current time, ISO 8601 in UTC, to the second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_lab(lab: pathlib.Path) -> Tuple[List[Dict[str, Any]], Scorer]:
    """Load the corpus and the scorer from a lab checkout, exactly as the lab's side tests do.

    The similarity model is loaded here, before any model call, so a broken lab install stops
    the gate before it spends anything.

    Args:
        lab: The ``iadopt-lab`` directory.

    Returns:
        The 102 canonical records (``variable_id``, ``definition``, ``gold``) and the scorer.

    Raises:
        GateError: When this Python is not the lab's, or the corpus or the scorer cannot be loaded.
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
        similarity("air temperature", "temperature of air")  # loads the model now, not after the calls
    except Exception as exc:  # the lab raises its own error types; none may end as a verdict
        raise GateError(f"could not load the corpus or the scorer from {lab}: {type(exc).__name__}: {exc}") from exc

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


def check_corpus(records: Sequence[Mapping[str, Any]]) -> None:
    """Refuse a corpus that is not the lab's 102 distinct variables, so the threshold keeps its meaning.

    Args:
        records: The loaded corpus records.

    Raises:
        GateError: When the corpus holds another number of variables or repeats a variable id,
            which would make two answers share one id.
    """
    if len(records) != EXPECTED_VARIABLES:
        raise GateError(f"the corpus holds {len(records)} variables, not {EXPECTED_VARIABLES}")
    if len({str(record["variable_id"]) for record in records}) != len(records):
        raise GateError("the corpus repeats a variable id")


def local_code(repo: pathlib.Path = REPO) -> Dict[str, Any]:
    """Describe the code to release.

    The digest covers ``backend/app`` and ``backend/data`` and is computed by the container
    program's own function, so it equals the container's when the image holds this tree.

    Args:
        repo: The application repository.

    Returns:
        ``sha256``, the ``commit`` the working tree is on (``None`` outside git) and whether it
        has ``uncommitted_changes``.
    """
    digest = runpy.run_path(str(CONTAINER_PROGRAM))["code_digest"](repo / "backend")

    def git(*arguments: str) -> Optional[str]:
        try:
            completed = subprocess.run(["git", *arguments], cwd=repo, capture_output=True, text=True)
        except OSError:
            return None
        return completed.stdout.strip() if completed.returncode == 0 else None

    status = git("status", "--porcelain")
    return {"sha256": digest, "commit": git("rev-parse", "HEAD"),
            "uncommitted_changes": None if status is None else bool(status)}


def check_backend(run: Mapping[str, Any], code: Mapping[str, Any], any_backend: bool) -> None:
    """Refuse to judge a backend that is not the code and configuration to release.

    Args:
        run: The container's run line.
        code: :func:`local_code` of this working tree.
        any_backend: Measure whatever provider, model, prompt and sampling the backend runs.

    Raises:
        GateError: When the container's code and data differ from this working tree or, unless
            ``any_backend``, its configuration differs from :data:`RELEASE_BACKEND`.
    """
    if run.get("code_sha256") != code.get("sha256"):
        raise GateError("the backend container does not run this working tree's backend/app and backend/data; "
                        "rebuild it with docker compose up -d --build")
    differences = [f"{key} is {run.get(key)!r}, not {value!r}" for key, value in RELEASE_BACKEND.items()
                   if run.get(key) != value]
    if differences and not any_backend:
        raise GateError("the backend does not run the measured configuration prod runs by default ("
                        + "; ".join(differences) + "); pass --any-backend to measure it anyway")


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
            continue
        if not isinstance(message, dict):
            continue
        if message.get("kind") == "run":
            run = message
        elif message.get("kind") == "prediction" and "variable_id" in message:
            predictions[str(message["variable_id"])] = message
    if run is None:
        raise GateError("the backend container did not report what it runs")
    return run, predictions


def container_command(argument: str, service: str = "backend") -> List[str]:
    """The command that runs the container program in the backend service with ``argument``."""
    return ["docker", "compose", "exec", "-T", service, "python", "-c",
            CONTAINER_PROGRAM.read_text(encoding="utf-8"), argument]


def describe_backend(*, run_command: Callable[..., Any] = subprocess.run, cwd: pathlib.Path = REPO,
                     service: str = "backend") -> Dict[str, Any]:
    """Ask the backend container what it runs; no model is called.

    Args:
        run_command: Runs the command; replaced in tests.
        cwd: Where ``docker compose`` finds the stack (the repository root).
        service: The compose service of the backend.

    Returns:
        The container's run line.

    Raises:
        GateError: When the container command fails or reports nothing.
    """
    try:
        completed = run_command(container_command("describe", service), cwd=cwd, input="", capture_output=True,
                                text=True, encoding="utf-8")
    except OSError as exc:
        raise GateError(f"could not start docker compose: {exc}") from exc
    if completed.returncode != 0:
        raise GateError(f"the backend container exited with {completed.returncode}; is the stack running? "
                        f"{completed.stderr.strip()[-500:]}")
    run, _ = parse_container_output(completed.stdout.splitlines())
    return run


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
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        try:
            process = popen(container_command(str(concurrency), service), cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8")
        except OSError as exc:
            raise GateError(f"could not start docker compose: {exc}") from exc
        try:
            process.stdin.write(json.dumps(items, ensure_ascii=False))
            process.stdin.close()
        except OSError:
            pass  # the process ended before reading everything; its exit code and errors say why
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
    """Score every record's answer; an answer that is not scored counts as an empty prediction.

    Each record ends as ``scored``, ``unusable`` (the lab cannot use the answer), ``failed`` (the
    app gave no answer although the provider answered), ``no_answer`` (the provider failed every
    decomposition request) or ``missing`` (no answer came back from the container).

    Args:
        records: The corpus records, with ``variable_id`` and ``gold``.
        predictions: The container's prediction lines by variable id.
        scorer: The lab's scoring steps.

    Returns:
        ``official`` (every record counted), ``as_run`` (usable answers only), the ``counts`` of
        each outcome, and one ``rows`` entry per record.
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
        elif prediction.get("provider_failed"):
            outcome = "no_answer"
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
            **{field: (prediction or {}).get(field) for field in CHOICE_FIELDS},
            "raw_output": (prediction or {}).get("raw_output"),
        })
    return {
        "official": scorer.aggregate(all_evidence, all_ids),
        "as_run": scorer.aggregate(usable_evidence, usable_ids),
        "counts": dict(Counter(row["outcome"] for row in rows)),
        "rows": rows,
    }


def group_scores(records: Sequence[Mapping[str, Any]], predictions: Mapping[str, Mapping[str, Any]],
                 scorer: Scorer, example_definitions: Optional[Sequence[str]]) -> Optional[Dict[str, Any]]:
    """Score the 25 fixed examples apart from the other variables, for information.

    The decision is made on all variables (D-043). With the leave-out neither group sees its own
    answer; the fixed examples are scored apart because they were the prompt's examples before.

    Args:
        records: The corpus records.
        predictions: The prediction lines by variable id.
        scorer: The lab's scoring steps.
        example_definitions: The definitions the prompt shows, or ``None`` when unknown.

    Returns:
        ``{"unseen": ..., "examples": ...}`` with each group's ``variables``, ``official`` and
        ``counts``, or ``None`` when the examples are unknown.
    """
    if example_definitions is None:
        return None
    shown = set(example_definitions)
    groups: Dict[str, Any] = {}
    for name, subset in (("unseen", [r for r in records if r["definition"] not in shown]),
                         ("examples", [r for r in records if r["definition"] in shown])):
        result = score(subset, predictions, scorer)
        groups[name] = {"variables": len(subset), "official": result["official"], "counts": result["counts"]}
    return groups


def decide(f1: float, threshold: float, counts: Mapping[str, int], degraded: int = 0) -> Tuple[str, int]:
    """Turn the score into a verdict and its exit code.

    Unanswered definitions count as empty, so the score is a lower bound: at or above the
    threshold it passes, but below it the gate cannot tell the code from a provider outage. The
    same holds for definitions whose prompt PSNC's failures degraded (examples that fell back, a
    finder that did not answer): their prompt is not the one to release.

    Args:
        f1: The micro Close F1 on all variables.
        threshold: The minimum to pass.
        counts: How many records ended with each outcome.
        degraded: How many definitions had a degraded prompt.

    Returns:
        ``("pass", 0)``, ``("fail", 1)`` or ``("undecided", 2)``.
    """
    if f1 >= threshold:
        return "pass", EXIT_PASS
    if degraded or any(counts.get(outcome) for outcome in UNANSWERED):
        return "undecided", EXIT_ERROR
    return "fail", EXIT_FAIL


def write_report(directory: pathlib.Path, report: Mapping[str, Any]) -> pathlib.Path:
    """Write the run's report as JSON and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(str(report["finished_at"])).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"quality-gate-{stamp}.json"
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
    parser.add_argument("--any-backend", action="store_true",
                        help="measure the backend even when it is not the measured configuration prod runs by default")
    return parser.parse_args(argv)


VERDICT_TEXT = {
    "pass": "PASS",
    "fail": "FAIL",
    "undecided": "CANNOT DECIDE (below it with the unanswered definitions counted as empty; run it again)",
}


def verdict_text(verdict: str, unanswered: int, fell_back: int, unstated: int) -> str:
    """The verdict as printed; an undecided one says why the score cannot decide."""
    if verdict != "undecided" or not (fell_back or unstated):
        return VERDICT_TEXT[verdict]
    reasons = ["with the unanswered definitions counted as empty"] if unanswered else []
    if fell_back:
        reasons.append(f"while {fell_back} definition{'' if fell_back == 1 else 's'} used the 25 fixed examples "
                       "because the embedding request failed")
    if unstated:
        reasons.append(f"while {unstated} definition{'' if unstated == 1 else 's'} got no matrix statement because "
                       "the finder's request failed")
    return f"CANNOT DECIDE (below it {' and '.join(reasons)}; run it again)"


def print_choices(rows: Sequence[Mapping[str, Any]]) -> None:
    """Print what the best pipeline chose: examples, the finder's matrix decisions, the kept answers."""
    retrieved = sum(1 for row in rows if row["examples"] == "retrieved")
    fell_back = [row for row in rows if row["examples"] == "fallback"]
    if retrieved or fell_back:
        print(f"Examples: {retrieved} retrieved, {len(fell_back)} fell back to the 25 fixed examples")
        causes = Counter(row["examples_error"] or "no reason was recorded" for row in fell_back)
        for cause, count in sorted(causes.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {count} {cause}")
    stated = [row for row in rows if row["matrix_status"] == "ok"]
    unusable = [row for row in rows if row["matrix_status"] == "unusable"]
    failed = [row for row in rows if row["matrix_status"] == "error"]
    if stated or unusable or failed:
        decisions = Counter(str(row["matrix"]) for row in stated)
        shown = ", ".join(f"{name} {decisions[name]}" for name in sorted(decisions, key=lambda n: (n != "none", n)))
        print(f"Matrix finder: {len(stated)} stated ({shown or 'none'}), {len(unusable)} not the requested JSON, "
              f"{len(failed)} without an answer")
        causes = Counter(row["matrix_error"] or "no reason was recorded" for row in failed)
        for cause, count in sorted(causes.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {count} {cause}")
    kept = Counter(row["answers_kept"] for row in rows if row["answers_kept"] is not None)
    if kept:
        names = {1: "the first", 2: "the second", 3: "the third"}
        print("Kept answer: " + ", ".join(f"{names.get(position, position)} {kept[position]}"
                                          for position in sorted(kept)))


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    load: Callable[[pathlib.Path], Tuple[List[Dict[str, Any]], Scorer]] = load_lab,
    code: Callable[[], Dict[str, Any]] = local_code,
    describe: Callable[[], Dict[str, Any]] = describe_backend,
    predict: Callable[..., Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]] = run_predictions,
) -> int:
    """Run the gate and print its verdict.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.
        load: Loads the corpus and the scorer; replaced in tests.
        code: Describes the code to release; replaced in tests.
        describe: Asks the backend container what it runs; replaced in tests.
        predict: Runs the decompositions; replaced in tests.

    Returns:
        The exit code: 0 pass, 1 below the threshold, 2 could not run or decide.
    """
    args = parse_args(argv)
    started_at = now()
    try:
        records, scorer = load(args.lab)
        check_corpus(records)
        local = code()
        check_backend(describe(), local, args.any_backend)
        items = [{"variable_id": str(r["variable_id"]), "definition": str(r["definition"])} for r in records]
        print(f"Decomposing {len(items)} corpus definitions in the backend container...", file=sys.stderr)
        run, predictions = predict(
            items, args.concurrency,
            progress=lambda done: print(f"\r  {done}/{len(items)}", end="", file=sys.stderr, flush=True),
        )
        print(file=sys.stderr)
    except GateError as exc:
        print(f"QUALITY GATE COULD NOT RUN: {exc}", file=sys.stderr)
        return EXIT_ERROR

    report: Dict[str, Any] = {"started_at": started_at, "threshold": args.threshold, "code": local, "run": run,
                              "variables": len(records)}
    try:
        check_backend(run, local, args.any_backend)
        result = score(records, predictions, scorer)
        groups = group_scores(records, predictions, scorer, run.get("example_definitions"))
    except Exception as exc:  # the answers cost provider calls, so they are kept before giving up
        report.update(finished_at=now(), verdict="error", error=f"{type(exc).__name__}: {exc}",
                      predictions=list(predictions.values()))
        path = write_report(args.report_dir, report)
        print(f"QUALITY GATE COULD NOT RUN: {exc}\nThe answers are kept in {path}", file=sys.stderr)
        return EXIT_ERROR

    f1 = float(result["official"]["f1"])
    fell_back = sum(1 for row in result["rows"] if row["examples"] == "fallback")
    unstated = sum(1 for row in result["rows"] if row["matrix_status"] == "error")
    verdict, exit_code = decide(f1, args.threshold, result["counts"], fell_back + unstated)
    report.update(finished_at=now(), verdict=verdict, **result, groups=groups)
    path = write_report(args.report_dir, report)
    counts = ", ".join(f"{count} {outcome.replace('_', ' ')}" for outcome, count in sorted(result["counts"].items()))
    print(f"Backend: {run.get('provider')} {run.get('model')} (temperature {run.get('temperature')}, top_p "
          f"{run.get('top_p')}, max_tokens {run.get('max_tokens')}), prompt {run.get('prompt_setup')} "
          f"{str(run.get('prompt_sha256'))[:16]}…")
    print(f"Code: {str(local.get('commit'))[:12]}"
          f"{' with uncommitted changes' if local.get('uncommitted_changes') else ''}")
    print(f"Variables: {len(records)} ({counts})")
    unanswered = [row for row in result["rows"] if row["outcome"] in UNANSWERED]
    if unanswered:
        reason = next((row["error"] for row in unanswered if row["error"]), "no answer came back")
        print(f"  {len(unanswered)} got no answer from the provider or were lost, e.g. {reason}")
    print_choices(result["rows"])
    print(f"Micro Close F1, all {len(records)}: {f1:.4f} (precision {result['official']['precision']:.4f}, "
          f"recall {result['official']['recall']:.4f}); usable answers only: {result['as_run']['f1']:.4f}")
    if groups is None:
        print("  (this prompt's examples are unknown, so the unseen variables are not scored apart)")
    else:
        print(f"  the {groups['examples']['variables']} fixed examples: {groups['examples']['official']['f1']:.4f}")
        print(f"  the other {groups['unseen']['variables']}: {groups['unseen']['official']['f1']:.4f}")
    print(f"Threshold {args.threshold:.2f} on all {len(records)}: "
          f"{verdict_text(verdict, len(unanswered), fell_back, unstated)}")
    print(f"Report: {path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
