# Project state

**Updated:** 2026-09-14
**Docs last reconciled:** 2026-09-14 at commit `0d1a4cd` (docs-drift pass, clean)

## What this project is

A web service that decomposes a plain-text scientific-variable definition into
I-ADOPT lexical components using an LLM, enriches the components with Wikidata
URIs, serializes the result to Turtle, and publishes it as a nanopublication.
FastAPI backend (`backend/app`), vanilla-JS frontend (`frontend/src`).

## Where things stand

The service works end to end and is deployed. Three refactor phases are complete:
schemas extracted to `app/schemas` (Phase 1), the `main.py` monolith decomposed
into `core`/`clients`/`services`/`routers`/`pipeline` (Phase 2), in-code docs plus
ruff/pydoclint enforcement (Phase 3). 45 backend tests pass, including 25 live
contract tests.

The service now runs the **measured** PSNC configuration by default:
`Qwen3.8-27B`, the `matrix-decomposition-v1` prompt rendered byte-exactly with its
25 fixed few-shot examples, reasoning disabled, `T=0.5`, `top_p=1.0`,
`max_tokens=16000`. Held-out Close F1 **0.4890 ± 0.0252**. Because that prompt
returns six fields and forbids `label`/`comment`, a second LLM call supplies those
two; everything downstream — validation, Wikidata enrichment, TTL, nanopub — is
unchanged and still sees the same eight-field prediction.

Setting `USE_MEASURED_CONFIGURATION=0` restores the previous behaviour.
`151 tests pass, 16 skipped`; ruff and mypy are clean.

## Document conventions

This project predates the plan→contract→test→code workflow and already had its own
records. The mapping:

| Role | This project uses |
|---|---|
| API/behavioral contract (pre-existing) | `docs/CONTRACTS.md` — the frozen Phase 0 baseline the refactor is checked against. Still authoritative for endpoints. |
| Module contracts (new) | `docs/components/<module>.md` |
| Present state | `docs/state.md` (this file) |
| Why | `docs/decisions.md` |
| What happened | `docs/history.md` |

`docs/CONTRACTS.md` is not being renamed or replaced. New module contracts sit
beside it and cover only the modules this work touches.

## Read these first

| File | Why |
|---|---|
| `iadopt-lab/few-shot-selection/example_selection/best-configuration-session.md` | The measured configuration being adopted: model, prompt, 25 examples, request body, and the limits on what it proves. External to this repo. |
| `docs/decisions.md` D-001 … D-007 | Why the configuration is adopted the way it is: the label/comment split (D-002), the schema correction and the approved golden flip (D-005), and the hands-off rule for entity linking (D-006). |
| `docs/components/*.md` | The seven module contracts. `measured-prompt` and `lexical-schema` are the two that carry real risk. |
| `backend/tests/fixtures/PROVENANCE.md` | How the golden prompt was derived and how to re-verify it. Read before touching any measured artifact. |
| `docs/CONTRACTS.md` | The endpoint contract that must not change. |

## The goal

Make the service run the configuration that was actually measured, byte-for-byte,
instead of the unmeasured one it runs today — without changing any endpoint's
request or response shape.

**What was measured** (held-out Close F1 **0.4890 ± 0.0252**, 24 variables,
15 repetitions, run 2026-09-12):

| | Measured | Service today |
|---|---|---|
| Model | `Qwen3.8-27B` on PSNC | `Qwen3.5-397B-A17B` (or OpenRouter) |
| Prompt | `matrix-decomposition-v1`, six-field | `constraint_tree`, eight-field |
| Few-shot | 25 examples, compact JSON array | 5 examples, indented markdown blocks |
| Temperature | 0.5 | 0.5 ✓ |
| `top_p` | 1.0 | not sent |
| `max_tokens` | 16000 | not sent |
| Reasoning | off, `chat_template_kwargs` only | off, **both** switches sent |

## Module breakdown

| Module | Responsibility |
|---|---|
| `measured-config` | Hold the three frozen artifacts (template, lexical schema, 25 demonstrations) and the settings that name them. No logic. |
| `measured-prompt` | Render the measured prompt byte-exactly: one substitution pass, the documented JSON encoding. The only place byte-exactness is enforced. |
| `psnc-payload` | Build the PSNC request body. Sends exactly the measured body for the measured configuration. |
| `lexical-schema` | The schema output is *validated against*. Aligns `entityOrSystem` with the lab's two exclusive asymmetric role pairs. |
| `label-comment` | Second LLM call producing `label` and `comment`, which the measured prompt forbids. Must never fail a decomposition. |
| `entity-linking` | Wikidata search + PSNC rerank. **Behaviour unchanged** — contract and regression tests only, pinning that it consumes the measured entity shapes. |
| `pipeline-wiring` | Select the measured path, merge its six fields with label/comment into the eight-field prediction the rest of the pipeline already expects. |

Two schemas are in play and must not be confused. `lexical-decomposition.schema.json`
is a frozen prompt artifact owned by `measured-config` — it goes *into* the prompt
text. `Json_schema.json` is owned by `lexical-schema` and validates what comes
*out* of the model.

Dependency direction is unchanged and stays acyclic:
`routers → pipeline → services → clients → core/schemas`.

## Delivery order

1. `measured-config` — artifacts on disk and settings, nothing reads them yet
2. `measured-prompt` — rendering, provable against the verbatim prompt in the source doc
3. `lexical-schema` — stop rejecting correct output; unblocks everything downstream
4. `psnc-payload` — request body
5. `label-comment` — the second call
6. `entity-linking` — contract + regression tests, no code change
7. `pipeline-wiring` — flip the default

Each is contract → tests → code before the next begins.

## Out of scope

- **The service has no evaluation loop.** Nothing in `backend/app` computes a score;
  "evaluation loop" here means the decomposition pipeline. No scoring, gold-set, or
  F1 machinery is being added.
- OpenRouter behaviour — untouched. The measurement is PSNC-only.
- **The entity-linking algorithm.** The experiments never covered linking, so
  nothing measured governs it. Wikidata search, the `bge-reranker-v2-m3` reranker
  and `RERANK_THRESHOLD=0.10` keep their current steps exactly. The `entity-linking`
  module documents and pins that behaviour; it does not change it.
- Deleting `constraint_tree` / `matrix_tree` / `strict_minimum` or the five-shot
  examples. They stay and stay reachable.
- Endpoint request/response shapes. `docs/CONTRACTS.md` stays true.
- Re-deriving or re-running the experiment. This repo consumes its output.

## Module status

| Module | Contract | Tests | Implementation |
|---|---|---|---|
| `measured-config` | done | done | done |
| `measured-prompt` | done | done | done |
| `lexical-schema` | done | done | done |
| `psnc-payload` | done | done | done |
| `label-comment` | done | done | done |
| `entity-linking` | done | done | **unchanged, by decision (D-006)** |
| `pipeline-wiring` | done | done | done |

`entity-linking` has a contract and 12 regression tests but no code change. They
passed on first run, which is the evidence that the measured entity shapes need
nothing there.
| everything else in `backend/app` | covered by `docs/CONTRACTS.md` | done | done |

## In flight

All seven modules are implemented and their tests pass. Nothing is mid-change.

## Test suite

`python -m pytest tests/` from `backend/`: **167 collected — 151 passed, 16 skipped,
0 failures.** The 16 skips are the live contract tests, which need a running stack
via `IADOPT_CONTRACT_BASE_URL`.

CI reports **150 passed, 17 skipped** — it has no `iadopt-lab` checkout, so the
fixture-provenance check against the lab source document skips there. CI runs
`python -m pytest tests`; it previously ran `unittest discover`, which collected
only the 12 `TestCase`-based tests and never saw the contract suite either.

122 of those tests are new (the seven files below); 45 pre-date this work and still
pass unchanged.

| File | Tests | Covers |
|---|---:|---|
| `test_measured_provenance.py` | 13 | The golden prompt is re-derivable from its sources |
| `test_measured_prompt.py` | 20 | Byte-exact rendering |
| `test_lexical_schema.py` | 37 | The four accepted entity shapes and the four rejected ones |
| `test_psnc_payload.py` | 9 | Exactly seven keys; legacy builder untouched |
| `test_label_comment.py` | 19 | Per-field fallback; never raises |
| `test_entity_linking.py` | 12 | Existing linking consumes the measured shapes |
| `test_pipeline_wiring.py` | 12 | Path selection and the six-to-eight merge |

`ruff check --config ruff.toml app` and `mypy --config-file mypy.ini app`
(36 modules) both clean.

## Verified end to end

With the provider stubbed at the HTTP boundary and the §5 target definition, the
body actually sent to PSNC carries exactly the seven measured keys, names
`Qwen3.8-27B`, and its prompt is **byte-identical to the golden fixture**. The
merged prediction validates (`schema_valid: true`), and the TTL carries a real
label rather than the `"generated variable"` placeholder.

## Deploying this change

**An existing deployment will keep running the old model until its `.env` is
updated.** `core/config.py` loads the repo-root `.env` at import and an environment
value beats a field default. Tracked config (`.env.example`,
`docker-compose.portainer.yml`) is updated; the untracked `.env` is deliberately
not (D-010). Two lines need changing there:

```
PSNC_MODEL_NAME=Qwen3.8-27B
PSNC_MODEL_NAMES=Qwen3.8-27B,Qwen3.5-397B-A17B,Qwen3-VL-235B-A22B-Instruct-FP8
```

The second matters as much as the first: the old list omits `Qwen3.8-27B`, so
`resolve_model_name` would reject the measured model even by explicit request.

## Blocked

Nothing.

## Known defect this work inherits

The service's `entityOrSystem` requires an asymmetric system to carry all five of
`AsymmetricSystem`, `hasSource`, `hasTarget`, `hasNumerator`, `hasDenominator`
simultaneously. The lab schema and the measured prompt require *exactly one* role
pair and forbid mixing. Measured: 10 of the 25 gold examples fail the service
schema (8 numerator/denominator, 2 source/target).

This predates the work. `backend/data/Json_preferred/five_shot/SoilMoist.json`,
`SurfRunoff.json` and `DetritalNitrogenConc.json` all use the three-key
source/target shape, and `backend/tests/contract/golden/asymmetric_soil_moisture.validation.json`
has `"schema_valid": false` frozen into it as expected behaviour — the service
marks its own example data invalid.

Only validation is affected. Enrichment (`if val.get(kk)`) and TTL
(`if not role_label: continue`) both handle the three-key shape correctly, which
is why it went unnoticed. Fixing it flips that golden to `"schema_valid": true`;
that is an intended contract change, recorded as a decision.

## Open questions

- **Are decompositions presented as drafts?** The lab doc (section 7) says a
  service on this configuration should present them "as drafts for review, not as
  answers" — 3 of 24 held-out items were exactly right and 7 of 24 scored zero.
  The frontend is an RDF editor, so a human does review before publishing, but no
  wording in the UI states the accuracy expectation. Backend work is complete
  either way; this is a frontend/copy question nobody has decided.

## Next action

Update the deployment `.env` as above, then commit. Nothing else is outstanding.
