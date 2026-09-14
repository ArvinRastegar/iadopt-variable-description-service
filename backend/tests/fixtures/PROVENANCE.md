# Measured-configuration fixture provenance

Everything in `backend/data/measured/` and the golden fixtures beside this file is
**mechanically extracted**, never hand-transcribed. This document records how, and
the checksums that let anyone re-verify it.

## Source of truth

`iadopt-lab/few-shot-selection/example_selection/best-configuration-session.md`

That document is itself generated from the run logs by the lab's
`build_best_config_doc.py`, which refuses to write if the recomputed top-25 no
longer matches the set the run evaluated. Candidate hash `2d56af0e65d42748`,
run date 2026-09-12, corpus tag `v2.0.1`.

## Regenerating

```bash
python backend/tools/extract_measured_artifacts.py --lab /path/to/iadopt-lab
```

Add `--check` to verify without writing. The script writes nothing unless all four
self-checks pass.

## Why a single extraction is not enough

A golden fixture extracted once is a baseline nobody can check. If the extraction
is subtly wrong, every test passes against the wrong bytes and the error is
invisible — the tests agree with themselves.

So the prompt is not merely copied out of the document. It is **rebuilt** from the
lab's own template and schema files and compared against the document's copy. The
two derivations are independent: one comes from prose in a generated report, the
other from the three artifacts the harness actually rendered. They agree only if
both are right.

This is not hypothetical. The rebuild caught a real error before any test existed —
see D-008. The contract said to strip the schema's trailing newline; the measured
prompt contains it. The rebuild came out at 15,790 characters against the recorded
15,791, diverging at offset 3957. A single extraction would have baked the missing
byte into the fixture and passed every test forever.

## The four self-checks

| # | Check | Pins |
|---|---|---|
| 1 | Demonstrations re-encode to exactly the bytes in the document | The `separators=(",", ":")` / `sort_keys=True` encoding |
| 2 | The prompt's schema block is byte-identical to the lab schema file | That the schema was not re-serialized |
| 3 | The lab template rebuilds the document's prompt exactly | Placeholder substitution, and D-008's trailing newline |
| 4 | The prompt is exactly 15,791 characters | The figure the document records independently |

## Recorded checksums

SHA-256 over UTF-8 bytes, as produced on 2026-09-14:

| Artifact | Chars | SHA-256 |
|---|---:|---|
| Rendered prompt (`measured_prompt_golden.txt`) | 15,791 | `4cf16c956a267ea1a063ec01b7c8f12db572f2996b305045ad0b4ebc45b2a79b` |
| Demonstrations, encoded | 11,519 | `019b953878d776140edc36862512ef2b7342c402a9303b5d14fe6faf46da8e2d` |
| Lexical schema (`lexical-decomposition.schema.json`) | 1,906 | `a3b5ec8ae6bcde4bd79c5ba29b5f8947ad5537f7b9517b499d053926c34c7a8b` |
| Template (`matrix-decomposition-v1.txt`) | 2,267 | `689447d848fc86c94c7c1d32e314a876d288fff2580b8ae49d34cf6a387425f5` |

`backend/tests/test_measured_provenance.py` asserts these on every run. Three of the
four checks need only files committed to this repo and always run. The fourth —
comparison against the lab document itself — runs when a lab checkout is reachable
via `IADOPT_LAB_PATH` or at the default sibling path, and **skips** rather than
fails when it is not, so CI without the lab checkout stays green without losing the
check for anyone who has it.

## Files

| File | What |
|---|---|
| `backend/data/measured/matrix-decomposition-v1.txt` | Template, verbatim from the lab |
| `backend/data/measured/lexical-decomposition.schema.json` | Schema, verbatim from the lab |
| `backend/data/measured/demonstrations-top25.json` | The 25 demonstrations, indented for review; re-encoded compactly at render time |
| `backend/tests/fixtures/measured_prompt_golden.txt` | The rendered prompt, one trailing newline |
| `backend/tests/fixtures/measured_target_definition.txt` | The §5 target definition the golden was rendered for |
