# Measured-configuration fixture provenance

Everything in `backend/data/measured/` and the target definition beside this file is
**mechanically extracted**, never hand-transcribed. The golden prompt is **mechanically
rendered** from those artifacts in the layout of D-042 and accepted only because its
checksum equals the prompt the 2026-11-18 side test sent (see *Layout B* below). This
document records how, and the checksums that let anyone re-verify it.

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
self-checks pass. It writes the three artifacts and the target definition, not the
golden prompt; to reproduce the golden, render it from `backend/` and accept the result
only if its SHA-256 is the one recorded below, never to make a test pass:

```bash
../.venv/bin/python -c "import pathlib; from app.services import measured_prompt as mp; f = pathlib.Path('tests/fixtures'); f.joinpath('measured_prompt_golden.txt').write_text(mp.render_measured_prompt(f.joinpath('measured_target_definition.txt').read_text(encoding='utf-8').rstrip('\n')), encoding='utf-8')"
```

```bash
shasum -a 256 tests/fixtures/measured_prompt_golden.txt
```

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

SHA-256 over UTF-8 bytes. The layout-A rows were produced on 2026-09-14, the layout-B rows
on 2026-11-18:

| Artifact | Chars | SHA-256 |
|---|---:|---|
| Rendered prompt, layout B (`measured_prompt_golden.txt`) | 17,393 | `6b6f4823ca76178b027d1b07436140b9915eb66229a7f6369a07c310beff8da4` |
| Examples block, layout B | 13,121 | `691caf47264f071ba5c7dca1ade48c66d8a928e8fa1821f73df727d20708eef3` |
| Rendered prompt, layout A, rebuilt from the artifacts (the golden until D-042) | 15,791 | `4cf16c956a267ea1a063ec01b7c8f12db572f2996b305045ad0b4ebc45b2a79b` |
| Demonstrations, encoded as in layout A | 11,519 | `019b953878d776140edc36862512ef2b7342c402a9303b5d14fe6faf46da8e2d` |
| Lexical schema (`lexical-decomposition.schema.json`) | 1,906 | `a3b5ec8ae6bcde4bd79c5ba29b5f8947ad5537f7b9517b499d053926c34c7a8b` |
| Template (`matrix-decomposition-v1.txt`) | 2,267 | `689447d848fc86c94c7c1d32e314a876d288fff2580b8ae49d34cf6a387425f5` |

`backend/tests/test_measured_provenance.py` asserts these on every run, and rebuilds
both prompts from the artifacts: layout A with the document's encoding, layout B with its
own copy of the layout rules. The comparison of the artifacts against the lab document runs
when a lab checkout is reachable via `IADOPT_LAB_PATH` or at the default sibling path, and
**skips** rather than fails when it is not, so CI without the lab checkout stays green
without losing the check for anyone who has it.

## Layout B (D-042, 2026-11-18)

Since D-042 the prompt shows each demonstration as `Example n`, `Definition:` with its
definition, and `Output:` with its decomposition as `json.dumps(indent=2,
ensure_ascii=False)` in answer order (the six fields as the schema's `required` list
orders them, keys in systems and constraints as theirs do), examples separated by one
blank line. Only the examples block differs from layout A; template, schema, target
section and demonstrations are the same files.

The layout was measured in an exploratory side test of the lab,
`iadopt-lab/few-shot-selection/example_selection/format_test.py`, on the same 24 held-out
variables as the layout-A figure: Close F1 0.6117 ± 0.0190 over 15 repetitions with
`Qwen3.8-27B`, against 0.4924 ± 0.0321 for layout A in the same test. The calls were made
with runner versions SHA-256 `ecffeffb…` (`Qwen3.8-27B`) and `a918b9f3…` (`GLM-5.2`); lab
commit `3a1a038` holds the runner as `09d8d2eb…`, after post-run changes to its docstrings
and currency that leave every prompt unchanged.

The golden here was rendered by `backend/app/services/measured_prompt.py` and accepted
because its SHA-256 equals the prompt that test sent for this target. This was verified on
2026-11-18 against the test's stored call records: 30 of its calls, 15 per model, sent
exactly these bytes. The lab keeps those records out of git
(`example_selection/output/format-test-calls.jsonl`), so no test repeats the check; where
the file exists, this repeats it from the repository root:

```bash
python3 -c "import json, hashlib, pathlib; g = hashlib.sha256(pathlib.Path('backend/tests/fixtures/measured_prompt_golden.txt').read_bytes()).hexdigest(); c = pathlib.Path.home() / 'Documents/GitHub/i-adopt-llm-based-service/iadopt-lab/few-shot-selection/example_selection/output/format-test-calls.jsonl'; print(sum(json.loads(line).get('prompt_sha256') == g for line in c.read_text().splitlines()), 'calls sent the golden')"
```

## Files

| File | What |
|---|---|
| `backend/data/measured/matrix-decomposition-v1.txt` | Template, verbatim from the lab |
| `backend/data/measured/lexical-decomposition.schema.json` | Schema, verbatim from the lab |
| `backend/data/measured/demonstrations-top25.json` | The 25 demonstrations, indented for review; shown in layout B at render time |
| `backend/tests/fixtures/measured_prompt_golden.txt` | The rendered prompt in layout B (D-042), one trailing newline |
| `backend/tests/fixtures/measured_target_definition.txt` | The §5 target definition the golden was rendered for |

# Entity-linking fixtures (D-032)

Added 2026-10-16. They pin the entity-linking question and the vocabulary searches to
what the sibling `iadopt-linking` project measured (its DECISIONS L-006 and L-016 to
L-018, prompt version `psnc-chat-entity-link-v1`, prompt fingerprint
`20d69e9a52e2dfe38c19250c0ce7fea2761b5352440b1b38bae4b63735ff6f24`). As above they are
produced by that project's own code, never typed.

| File | What it pins | Produced by |
|---|---|---|
| `entity_link_request_golden.json` | The chat request (system prompt, user-message layout, labels, decoding parameters) for three invented cases, and the constants | `chat_choice.chat_request` with an identity candidate order and the 300-character cap, then `logprobs` and `top_logprobs` removed |
| `entity_choice_helpers_golden.json` | Term normalisation and description truncation | `population.normalise_term`, `rerank.truncate_description` |
| `vocabulary_requests_golden.json` | The Wikidata, QUDT and OM search requests for one key, and the key that QUDT cannot search | `sources.WikidataSource/QudtSource/OmSource().request` |
| `vocabularies/depth.{wikidata,qudt,om}.json` | Three real responses, byte for byte (retrieved 2026-10-14, 10 + 2 + 1 candidates) | `results/baseline-20261014/candidates.jsonl`, field `response_text` |
| `vocabularies/depth.expected.json` | The candidates the sibling parsed from those responses | the same records, field `candidates` |

## How they were made

From the sibling repository root, with its lab interpreter and without writing anything
there (`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=iadopt-linking/src
iadopt-lab/.venv/bin/python script.py OUT`), a short script called
`chat_request(term, definition, pool, list(range(len(pool))), max_description_chars=300)`
for each case, `normalise_term` and `truncate_description` for each input, and
`SOURCES[name].request("soil moisture of the topsoil")` for each source, and dumped the
results. The three cases use invented candidates. Nothing was sent to any service.

Sources at that moment (sibling HEAD `6c00370`; the chat modules were uncommitted):

| Module | SHA-256 (first 16) | Modified |
|---|---|---|
| `chat_choice.py` | `9161b0056d781cb7` | 2026-10-15 14:59 |
| `jev.py` | `62d5cf0ba1436f9a` | 2026-10-15 12:29 |
| `rerank.py` | `c7b09ae0df076ffe` | 2026-10-14 15:12 |
| `population.py` | `3d795d2439cde960` | 2026-10-13 15:37 |
| `sources.py` | `02915bf09ac91dbb` | 2026-10-14 15:12 |

## Checked when they were made

- All 459 stored real responses (153 per source, 1,622 candidates) parse with
  `vocabularies.parse_response` to exactly the `iri`, `label` and `description` the
  sibling stored, in the same order. No mismatch.
- The three search-request builders equal the sibling's for the sample key.
- `chat_choice.prompt_fingerprint()` printed `20d69e9a52e2`, the value recorded in the
  sibling's plan and in D-032.

## Recorded checksums

SHA-256 over the file bytes, as produced on 2026-10-16; `test_entity_link_provenance.py`
fails if a file no longer matches.

| File | SHA-256 |
|---|---|
| `entity_link_request_golden.json` | `f0207c9df125df550e8a36f173b8d2648926aac6a823813711f94b92e85a6f2c` |
| `entity_choice_helpers_golden.json` | `92a7f5641605cde4e0ac5e9371859171683986c88996467f6e22e0ab6c222d88` |
| `vocabulary_requests_golden.json` | `36bcbe32cbc325cfadf1a5128a568be286c188b4c221ea02d9f9bc8b003b59fc` |
| `vocabularies/depth.expected.json` | `a0058b7e3ad11e92d0e6cff097c7d18a6d99f18ee87732420fcef44adcb38833` |
| `vocabularies/depth.wikidata.json` | `56590959945f1bb5c71ac0bf33202032a59abe7c4c54666c4929a8fd17af66f7` |
| `vocabularies/depth.qudt.json` | `04a2731983e23a265a5a82e6f40d04349f3404d57ee6572193fc1014db49319a` |
| `vocabularies/depth.om.json` | `c018fe9031bd5f17366e4796880293fe70c432be4246dbe002bf0e38620d36a1` |
