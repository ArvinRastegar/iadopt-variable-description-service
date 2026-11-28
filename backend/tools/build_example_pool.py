#!/usr/bin/env python3
"""Build the measured prompt's example pool from the lab: its 102 corpus variables and PSNC's vectors (D-055).

The pool, ``backend/data/measured/example-pool.json``, holds for each of the lab's 102 corpus variables its lab
id, its definition exactly as the lab stores it, its decomposition (the lab's gold), its fold in the lab's six
folds, and the vector PSNC's ``Qwen3-Embedding-0.6B`` gave its definition in the lab's PSNC retrieval run. The
web app chooses the measured prompt's examples among them (``app/services/example_retrieval.py``). Run this with
the lab's Python, from the application root; it sends no request::

    ../i-adopt-llm-based-service/iadopt-lab/.venv/bin/python backend/tools/build_example_pool.py

The variables, their gold and their folds come through the lab's own loader (``few-shot-selection/guided/
knowledge.py``); the vectors from the run's ``few-shot-selection/output/retrieval/psnc-similarities.json``, which the
lab's git ignores, so it exists only on the machine that ran it. Nothing is written unless the definitions, the
folds and the vectors are those of the lab's run (digests ``c5a9f585fbcb9ffb``, ``80f37c48d2cf004c`` and
``3480e2bb4f059272``) and the service's 25 fixed examples are among the variables with the same answers. ``--check``
compares the pool in the repository with what this would write.

The pool in the repository was written by this on 2026-11-27 from the lab's repository at commit ``23c1c40`` and the
run's vectors file, with the lab's Python; its SHA-256 is
``7bcf98558e802cc71c706865d975f02978794ccc41a3ad2693ebd5543be51847``. The web app checks the same digests, and that
the fixed 25 are in it, each time it starts; ``backend/tests/test_example_retrieval.py`` checks them and rebuilds the
lab's prompts from it. As the vectors file stays on the machine that ran the lab, the pool is the artifact: no test
rebuilds it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any, Dict, Optional, Sequence

REPO = pathlib.Path(__file__).resolve().parents[2]
POOL = REPO / "backend" / "data" / "measured" / "example-pool.json"
DEMONSTRATIONS = REPO / "backend" / "data" / "measured" / "demonstrations-top25.json"
DEFAULT_LAB = REPO.parent / "i-adopt-llm-based-service" / "iadopt-lab"
VECTORS = pathlib.Path("few-shot-selection") / "output" / "retrieval" / "psnc-similarities.json"
EMBEDDING_MODEL = "Qwen3-Embedding-0.6B"
CORPUS = {"name": "I-ADOPT Corpus", "release": "v2.0.1", "doi": "10.5281/zenodo.18101358", "license": "CC BY 4.0"}
# The digests of the lab's PSNC retrieval run (paper/tracking/PSNC_RETRIEVAL_TEST_20261127.md), first 16 characters.
EXPECTED = {"definitions_sha256": "c5a9f585fbcb9ffb", "folds_sha256": "80f37c48d2cf004c",
            "vectors_sha256": "3480e2bb4f059272"}
VARIABLES = 102


class Refused(RuntimeError):
    """The lab's data are not those of the run, so no pool is written."""


def sha256_text(text: str) -> str:
    """SHA-256 of a text's UTF-8 bytes, as the lab's ``ft.sha256``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lab_variables(lab: pathlib.Path) -> Dict[str, Dict[str, Any]]:
    """The lab's 102 variables, through its own loader: definition, gold decomposition and fold.

    Args:
        lab: The ``iadopt-lab`` directory.

    Returns:
        ``{lab id: {"definition", "decomposition", "fold"}}``.

    Raises:
        Refused: When this Python cannot load the lab.
    """
    guided = lab / "few-shot-selection" / "guided"
    for path in (lab / "src", guided.parent, guided.parent / "example_selection", guided):
        sys.path.insert(0, str(path))
    try:
        import knowledge  # type: ignore[import-not-found]  # the lab's guided/knowledge.py
    except ImportError as error:
        raise Refused(f"run this with the lab's Python, iadopt-lab/.venv/bin/python ({error})") from error
    ctx = knowledge.load()
    return {identifier: {"definition": ctx["records"][identifier]["definition"],
                         "decomposition": ctx["records"][identifier]["gold"],
                         "fold": ctx["fold_of"][identifier]}
            for identifier in ctx["ids"]}


def build(variables: Dict[str, Dict[str, Any]], stored: Dict[str, Any],
          demonstrations: Sequence[Dict[str, Any]]) -> str:
    """The pool's text, after checking that everything is the lab run's.

    Args:
        variables: From :func:`lab_variables`.
        stored: The run's ``psnc-similarities.json``.
        demonstrations: The service's 25 fixed examples.

    Returns:
        The pool as JSON: its header, then one variable per line in id order.

    Raises:
        Refused: When a count, a digest, the model or a fixed example differs.
    """
    ids = sorted(variables)
    vectors = stored.get("vectors") or {}
    model = (stored.get("model") or {}).get("model")
    if len(ids) != VARIABLES:
        raise Refused(f"the lab holds {len(ids)} variables, not {VARIABLES}")
    if model != EMBEDDING_MODEL:
        raise Refused(f"the vectors are {model!r}'s, not {EMBEDDING_MODEL}'s")
    if sorted(vectors) != ids:
        raise Refused("the vectors are not those of the lab's 102 variables")
    digests = {
        "definitions_sha256": sha256_text("\n".join(f"{v}\t{variables[v]['definition']}" for v in ids)),
        "folds_sha256": sha256_text("\n".join(f"{v} {variables[v]['fold']}" for v in ids)),
        "vectors_sha256": sha256_text(json.dumps({v: vectors[v] for v in ids}, sort_keys=True)),
    }
    for name, value in digests.items():
        if not value.startswith(EXPECTED[name]):
            raise Refused(f"{name} is {value[:16]}, not the run's {EXPECTED[name]}")
    by_definition = {variables[v]["definition"]: v for v in ids}
    for number, demonstration in enumerate(demonstrations, 1):
        identifier = by_definition.get(demonstration["definition"])
        if identifier is None or variables[identifier]["decomposition"] != demonstration["decomposition"]:
            raise Refused(f"fixed example {number} is not among the lab's variables with the same answer")
    header = {"corpus": CORPUS, "embedding_model": EMBEDDING_MODEL, **digests}
    lines = [json.dumps({"id": v, "definition": variables[v]["definition"],
                         "decomposition": variables[v]["decomposition"], "fold": variables[v]["fold"],
                         "vector": vectors[v]}, ensure_ascii=False) for v in ids]
    text = json.dumps(header, ensure_ascii=False)[:-1] + ', "variables": [\n' + ",\n".join(lines) + "\n]}\n"
    json.loads(text)
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Write the pool, or with ``--check`` compare it; returns the exit code."""
    parser = argparse.ArgumentParser(description="Build the example pool from the lab (D-055); no request is sent.")
    parser.add_argument("--lab", type=pathlib.Path, default=DEFAULT_LAB, help="the iadopt-lab directory")
    parser.add_argument("--vectors", type=pathlib.Path, help=f"the run's vectors (default: <lab>/{VECTORS})")
    parser.add_argument("--check", action="store_true", help="compare the pool with what would be written")
    args = parser.parse_args(argv)
    vectors_path = args.vectors or args.lab / VECTORS
    try:
        if not vectors_path.exists():
            raise Refused(f"{vectors_path} is missing: it is written by the lab's PSNC retrieval run")
        text = build(lab_variables(args.lab), json.loads(vectors_path.read_text(encoding="utf-8")),
                     json.loads(DEMONSTRATIONS.read_text(encoding="utf-8")))
    except Refused as refusal:
        print(f"NOT WRITTEN: {refusal}", file=sys.stderr)
        return 2
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if args.check:
        same = POOL.exists() and POOL.read_text(encoding="utf-8") == text
        print(f"{POOL.relative_to(REPO)} {'matches' if same else 'DIFFERS from'} the lab's data (sha256 {digest})")
        return 0 if same else 1
    POOL.write_text(text, encoding="utf-8")
    print(f"wrote {POOL.relative_to(REPO)} (sha256 {digest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
