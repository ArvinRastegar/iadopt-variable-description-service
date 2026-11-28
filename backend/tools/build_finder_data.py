#!/usr/bin/env python3
"""Export the lab's matrix finder and its reference cases for the web app's best pipeline.

The lab's best pipeline (``iadopt-lab`` README, "best pipeline", 2026-11-27) asks the model first whether the variable
has a matrix and of which kind (the finder, ``few-shot-selection/guided/answer_form_check.py``, arm ``named-first``),
states that matrix in the fill prompt, shows the 25 most similar corpus variables as examples, sends the fill prompt
three times and keeps the most typical answer (``fill_levers_test.py``). Run this with the lab's Python, from the
application root; it sends no request::

    ../i-adopt-llm-based-service/iadopt-lab/.venv/bin/python backend/tools/build_finder_data.py

It writes ``backend/data/measured/matrix-finder.json``, which the web app reads: the finder prompt's head (the
instruction and the pattern lists), the 102 corpus variables as the finder shows them (definition, decomposition and
patterns on one line each), the property pattern numbers an answer must name, and the guidance block that states each
matrix decision. With ``--fixtures`` it also writes ``backend/tests/fixtures/best_pipeline/lab_cases.json``, the lab's
own outputs the tests compare the web app with: the finder prompt of every corpus variable (its fold left out), the
finder's stored answers (run 1 of the answer-form check) with the matrix each states, the fill prompt of every corpus
variable with PSNC's examples and that statement, and the stored PSNC answers of the stand-in test's ``best`` arm in
groups of three runs (1 to 3, 4 to 6 and 7 to 9: 306 groups) with the agreement of each pair by the frozen scorer's
Exact F1 and the answer the most-typical rule keeps. ``--check`` compares the files with what would be written.

The data it read on 2026-11-27 came from the lab's repository at commit ``8ca2f36``; the stored answers are in the
lab's ignored output folder, so they exist only on the machine that ran the lab.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional, Sequence

REPO = pathlib.Path(__file__).resolve().parents[2]
DATA = REPO / "backend" / "data" / "measured" / "matrix-finder.json"
FIXTURE = REPO / "backend" / "tests" / "fixtures" / "best_pipeline" / "lab_cases.json"
DEFAULT_LAB = REPO.parent / "i-adopt-llm-based-service" / "iadopt-lab"
MATRIX_PATTERNS = (40, 41, 42, 43, 44, 45)
VARIABLES = 102


class Refused(RuntimeError):
    """The lab cannot be read as expected, so nothing is written."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_of(prompts: Sequence[str]) -> str:
    """The lab's digest of a set of prompts: SHA-256 over the lines of their SHA-256, first 16 characters."""
    return sha256_text("\n".join(sha256_text(prompt) for prompt in prompts))[:16]


def lab_modules(lab: pathlib.Path) -> Dict[str, Any]:
    """Import the lab's modules the export needs, by path."""
    guided = lab / "few-shot-selection" / "guided"
    for path in (lab / "src", guided.parent, guided.parent / "example_selection", guided):
        sys.path.insert(0, str(path))
    try:
        import answer_form_check as afc  # type: ignore[import-not-found]
        import conventions_test as ct  # type: ignore[import-not-found]
        import draft_rag as dr  # type: ignore[import-not-found]
        import engine  # type: ignore[import-not-found]
        import rag_tuning as rt  # type: ignore[import-not-found]
        import typical_answer_check as ta  # type: ignore[import-not-found]
        from iadopt_eval import evaluate_item  # type: ignore[import-not-found]
    except ImportError as error:
        raise Refused(f"run this with the lab's Python, iadopt-lab/.venv/bin/python ({error})") from error
    return {"afc": afc, "ct": ct, "dr": dr, "engine": engine, "rt": rt, "ta": ta, "evaluate_item": evaluate_item}


def statements(m: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, str]:
    """The guidance block of each matrix decision: ``none`` and every matrix pattern number."""
    property_number = int(next(n for n, p in ctx["patterns"].items() if p["kind"] == "property"))
    out = {}
    for matrix in (None, *MATRIX_PATTERNS):
        facts = {"property": [property_number], "object": [], "matrix": [] if matrix is None else [matrix],
                 "context": [], "constraint": [], "statistical_modifier": False, "names": {}}
        out["none" if matrix is None else str(matrix)] = m["dr"].render(ctx, m["dr"].statement(facts, {"matrix"}))
    return out


def finder_data(m: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """What the web app needs to ask the finder and to read its answer."""
    ids = ctx["ids"]
    if len(ids) != VARIABLES:
        raise Refused(f"the lab holds {len(ids)} variables, not {VARIABLES}")
    examples = [{"id": v, "definition": ctx["demos"][v]["definition"], "decomposition": ctx["demos"][v]["gold"],
                 "patterns": ctx["demos"][v]["patterns"]} for v in ids]
    head = m["ct"].head(ctx)
    property_patterns = sorted(int(n) for n, p in ctx["patterns"].items() if p["kind"] == "property")
    table = statements(m, ctx)
    # The block of a decision depends on its matrix only: every stored finder answer's block is in the table.
    found = m["afc"].finder_facts(ctx, m["engine"].latest(m["afc"].CALLS_A))
    for v, facts in found.items():
        if facts is None:
            continue
        block = m["dr"].render(ctx, m["dr"].statement(facts, {"matrix"}))
        key = str(facts["matrix"][0]) if facts["matrix"] else "none"
        if table[key] != block:
            raise Refused(f"the statement of {v} is not the table's {key}")
    request = dict(m["ct"].REQUEST)
    return {"source": "iadopt-lab answer_form_check.py, arm named-first (the conventions test's plain prompt)",
            "head": head, "examples": examples, "property_patterns": property_patterns, "statements": table,
            "request": {key: request[key] for key in ("temperature", "top_p", "max_tokens")}}


def lab_cases(m: Dict[str, Any], ctx: Dict[str, Any], psnc_table: Dict[str, Any]) -> Dict[str, Any]:
    """The lab's own outputs the tests compare the web app with."""
    afc, dr, engine, rt, ta = m["afc"], m["dr"], m["engine"], m["rt"], m["ta"]
    ids = ctx["ids"]
    finder_prompts = {v: sha256_text(afc.build(ctx, "named-first", ctx["records"][v]["definition"], ctx["fold_of"][v]))
                      for v in ids}
    calls_a = engine.latest(afc.CALLS_A)
    found = afc.finder_facts(ctx, calls_a)
    answers = {}
    for v in ids:
        record = calls_a.get(("named-first", 1, v), {})
        facts = found.get(v)
        answers[v] = {"answer": record.get("answer") if record.get("ok") else None,
                      "matrix": None if facts is None else (facts["matrix"][0] if facts["matrix"] else "none")}
    world = rt.corpus_world(rt.load(table=psnc_table), None)
    fill = {}
    for v in ids:
        chosen = rt.examples(world, "rag-25", v)
        facts = found.get(v)
        if facts is None:
            prompt = rt.prompt_of(world, v, chosen)
        else:
            inputs = dict(world["base_inputs"](v), examples=[world["record"](u) for u in chosen])
            prompt = dr.fill_prompt(inputs, world["definition"](v), dr.render(ctx, dr.statement(facts, {"matrix"})))
        fill[v] = sha256_text(prompt)
    calls, scores_path, arm = ta.SOURCES["psnc"]
    scored = engine.score(calls, scores_path, ctx["records"], ctx["inputs"]["schema"])
    raw = engine.latest(calls)
    evaluate_item = m["evaluate_item"]

    def exact(first: Optional[Dict[str, Any]], second: Optional[Dict[str, Any]], v: str) -> float:
        if first is None or second is None:
            return 0.0
        both = [ta.f1(evaluate_item(g, p, lambda a, b: 0.0, metadata={"variable_id": v})["exact"]["totals"])
                for g, p in ((first, second), (second, first))]
        return sum(both) / 2

    votes = []
    for v in ids:
        for group in ta.GROUPS[3]:
            predictions = {run: scored[(arm, run, v)]["prediction"] for run in group}
            agreement = {(a, b): exact(predictions[a], predictions[b], v) for a in group for b in group if a < b}
            kept = ta.pick(group, agreement)
            votes.append({"variable_id": v, "answers": [raw[(arm, run, v)].get("answer") for run in group],
                          "usable": [predictions[run] is not None for run in group],
                          "agreements": [agreement[(group[0], group[1])], agreement[(group[0], group[2])],
                                         agreement[(group[1], group[2])]],
                          "kept": group.index(kept)})
    return {"source": "built by backend/tools/build_finder_data.py --fixtures from the lab's code and stored runs",
            "finder_prompts": {"digest": digest_of([afc.build(ctx, "named-first", ctx["records"][v]["definition"],
                                                              ctx["fold_of"][v]) for v in ids]),
                               "by_variable": finder_prompts},
            "finder_answers": answers,
            "fill_prompts": {"digest": sha256_text("\n".join(fill[v] for v in ids))[:16], "by_variable": fill},
            "votes": votes}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Write the finder's data, and with ``--fixtures`` the reference cases; ``--check`` compares."""
    parser = argparse.ArgumentParser(description="Export the lab's matrix finder (no request is sent).")
    parser.add_argument("--lab", type=pathlib.Path, default=DEFAULT_LAB, help="the iadopt-lab directory")
    parser.add_argument("--fixtures", action="store_true", help="also write the tests' reference cases")
    parser.add_argument("--check", action="store_true", help="compare with what would be written")
    args = parser.parse_args(argv)
    try:
        m = lab_modules(args.lab)
        ctx = m["afc"].load()
        outputs: List[tuple] = [(DATA, finder_data(m, ctx))]
        if args.fixtures:
            similarities = args.lab / "few-shot-selection" / "output" / "retrieval" / "psnc-similarities.json"
            if not similarities.exists():
                raise Refused(f"{similarities} is missing: it is written by the lab's PSNC retrieval run")
            psnc_table = json.loads(similarities.read_text(encoding="utf-8"))["table"]
            outputs.append((FIXTURE, lab_cases(m, ctx, psnc_table)))
    except Refused as refusal:
        print(f"NOT WRITTEN: {refusal}", file=sys.stderr)
        return 2
    status = 0
    for path, data in outputs:
        text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
        digest = sha256_text(text)
        if args.check:
            same = path.exists() and path.read_text(encoding="utf-8") == text
            print(f"{path.relative_to(REPO)} {'matches' if same else 'DIFFERS from'} the lab (sha256 {digest})")
            status = status or (0 if same else 1)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO)} (sha256 {digest})")
    return status


if __name__ == "__main__":
    sys.exit(main())
