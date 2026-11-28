# Best-pipeline fixtures

`backend/tests/test_best_pipeline.py` compares the web app's best pipeline (examples by similarity, the matrix
finder, the most typical of three answers) with the lab's own outputs, so the tests need no lab checkout and send no
request. The three files were made on 2026-11-27 on the machine that ran the lab: the lab's stored runs are in its
ignored output folders and exist only there.

## Files

| File | What it holds | Made from |
|---|---|---|
| `lab_cases.json` | The matrix finder's prompts, its stored answers, the fill prompts with the finder's statement, and 306 groups of three stored answers with the lab's vote | `backend/tools/build_finder_data.py --fixtures`, lab commit `8ca2f36` |
| `lab_prompts.json` | The SHA-256 of each corpus variable's fill prompt in the lab's PSNC retrieval run: `base` (the fixed 25 examples, the variable itself left out) and `rag-25-psnc` (the 25 most similar by PSNC's embedding, the variable's fold left out), with the lab's digest of each set | `few-shot-selection/output/retrieval/psnc-calls.jsonl` of that run |
| `lexical_cases.json` | 13 answers with the problems the lab's check finds in them (none for an answer it accepts) | `iadopt_lab.validation.lexical.validate_prediction`, each problem written as `few-shot-selection/guided/pipeline.py` `check()` writes it, lab commit `763a675` |

## lab_cases.json

Made with the lab's Python from the application root; it reads the lab's code and stored runs and sends nothing:

```bash
../i-adopt-llm-based-service/iadopt-lab/.venv/bin/python backend/tools/build_finder_data.py --fixtures
```

- `finder_prompts`: the finder's prompt (`answer_form_check.py`, arm `named-first`) for each of the 102 corpus
  variables, its fold of 17 left out; digest `2c4a50e46b18415f`.
- `finder_answers`: the finder's stored answer for each variable (run 1 of the answer-form check) and the matrix the
  lab read from it (`null` when it read none).
- `fill_prompts`: each variable's fill prompt with PSNC's 25 most similar examples (its fold left out) and the
  statement of that matrix after the definition; digest `1ea4710d404120f7`.
- `votes`: the stored PSNC answers of the stand-in test's `best` arm in groups of three runs (1 to 3, 4 to 6, 7 to 9),
  three groups per variable: which answers the lab could read, the agreement of each pair by the frozen scorer's Exact
  F1 (averaged both ways) and the position of the answer the most-typical rule keeps.

`--check` compares the files with what would be written; it needs the same lab checkout and stored runs.

## Recorded checksums

SHA-256 of each file as the tests check it:

- `lab_cases.json`: `6b1a79f5170ac901890b3e5a0724e237189518dd3f1f28fa6e2706f9a05202d7`
- `lab_prompts.json`: `752675052e075120d7638dade9b85ba349b84e39c66c6033201738bd613be94b`
- `lexical_cases.json`: `3e553a35425fd8560178773c0b8d7597f9aa715bd7a924392314d2d92c5cb5db`
