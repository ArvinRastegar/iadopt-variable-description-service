# Decisions

## D-001 — Adopt the measured PSNC configuration as the service default

**Status:** Accepted
**Date:** 2026-09-14

The service runs `Qwen3.8-27B` on PSNC with the `matrix-decomposition-v1` prompt,
25 fixed few-shot examples, reasoning disabled, `T=0.5`, `top_p=1.0`,
`max_tokens=16000` — the configuration measured in the `iadopt-lab`
example-selection study, held-out Close F1 **0.4890 ± 0.0252** (24 variables,
15 repetitions, run 2026-09-12).

Chosen over the previous default — `Qwen3.5-397B-A17B`, the `constraint_tree`
template and five examples — which was never scored by anything. The prompt
template was not even selectable: `warmup_assets` took `list_prompt_versions()[0]`,
so the service ran whichever template sorted first alphabetically.

**What this does not claim.** The measured set beats a deliberately-bad selection
(+0.091, Holm `p < 0.0001`) and a domain-stratified one (+0.094, `p < 0.0001`), but
it is **not** significantly better than the best of three random 25-subsets
(+0.0133, `p = 0.156`). Absolute accuracy is modest: 3 of 24 held-out items exactly
right, 7 of 24 scoring zero. This is a reliably good selection, not a proven
optimum, and the service should present decompositions as drafts for review.

Going forward: the prompt bytes, the 25 examples and the request body are frozen
together. Changing any one of them without re-measuring makes the 0.4890 figure
inapplicable, and the figure must not be quoted for a configuration that differs.

## D-002 — Generate `label` and `comment` with a second LLM call

**Status:** Accepted
**Date:** 2026-09-14

The measured prompt returns six fields and says verbatim *"Do not regenerate the
definition, label, or comment."* The TTL serializer needs both:
`rdf_ttl.py:277` falls back to the literal string `"generated variable"` and
`:280` to `""`. A separate, small LLM call supplies them.

Chosen over two alternatives:

- **Adding the fields back into the measured prompt.** Rejected: it changes the
  prompt bytes, so nothing in the study describes what the service would then run.
  This was the cheapest option in code and the most expensive in evidence.
- **Deriving them in code with no LLM call.** Viable and free, and it remains the
  fallback path when the call fails. Rejected as the primary route because label
  quality visibly degrades and the label is what a reader sees first on a published
  nanopublication.

Costs a second round trip (~1–2s) per decomposition. The labelling call carries
**none** of the study's evidence and must never be described as measured.

Going forward: `label-comment` must never raise for a model or transport problem.
An unmeasured auxiliary call must not be able to break the measured path.

## D-003 — Send the measured request body exactly, via a separate builder

**Status:** Accepted
**Date:** 2026-09-14

`build_measured_psnc_payload` emits exactly the seven keys of the measured body.
`build_psnc_chat_payload` is left untouched for the legacy path.

The existing builder sends **both** reasoning switches — a top-level
`enable_thinking: false` and `chat_template_kwargs` — and sends neither `top_p` nor
`max_tokens`. The top-level key was added for Qwen3.5 compatibility across
providers and is worth keeping there; it appeared in no measured call, so it has no
place in the measured body.

Chosen over threading a flag through the existing builder, which would have put the
legacy path one boolean away from a silent regression. Two functions, no shared
branches, and a regression test pinning the old one.

## D-004 — Keep the legacy prompts and five-shot examples selectable

**Status:** Accepted
**Date:** 2026-09-14

`constraint_tree`, `matrix_tree`, `strict_minimum` and the five-shot example set
stay in the repo and remain reachable. `Qwen3.5-397B-A17B` stays in
`PSNC_MODEL_NAMES`. `USE_MEASURED_CONFIGURATION=0` returns the service to its
previous behaviour.

Chosen over deleting them. The measured configuration is better-evidenced but not
dramatically better in absolute terms, and a single environment variable is a
cheaper rollback than a revert.

## D-005 — Correct `entityOrSystem`, and accept the golden flip

**Status:** Accepted
**Date:** 2026-09-14

`Json_schema.json` required an asymmetric system to carry all five of
`AsymmetricSystem`, `hasSource`, `hasTarget`, `hasNumerator`, `hasDenominator`
at once. The lab schema and the measured prompt define two **mutually exclusive**
three-key variants and forbid mixing them. The service schema now matches.

This corrects a pre-existing defect, not one this work introduced. Measured:
**10 of the 25 gold decompositions fail the old schema** (8 numerator/denominator,
2 source/target). The service's own five-shot examples — `SoilMoist.json`,
`SurfRunoff.json`, `DetritalNitrogenConc.json` — use the three-key source/target
shape, and `backend/tests/contract/golden/asymmetric_soil_moisture.validation.json`
had `"schema_valid": false` frozen in as expected behaviour. The service was
marking its own example data invalid.

It went unnoticed because only validation was affected. Enrichment
(`if val.get(kk)`) and TTL (`if not role_label: continue`) both handle three-key
shapes correctly, so output was right while being labelled wrong.

**The golden changes** from `schema_valid: false` to `true`, with an empty
`validation_errors`. That fixture is part of the Phase-0 contract baseline the
refactor has been checked against at every gate, so the change is deliberate and
was approved explicitly before any code was written.

Verified safe rather than assumed: the only golden containing a system is
`asymmetric_soil_moisture` (three-key); the only other validation golden is
string-only. No five-key object exists anywhere in the repo, so nothing that
validated before now fails.

`hasPart.minItems` stays at **1**, not the lab's 2 — raising it would newly reject
existing single-part data for no benefit, and measured output always has ≥2 parts.

## D-006 — Entity linking is documented and pinned, not changed

**Status:** Accepted
**Date:** 2026-09-14

The experiments never covered entity linking, so nothing measured governs it.
Wikidata search, the `bge-reranker-v2-m3` reranker and `RERANK_THRESHOLD=0.10` keep
their current steps exactly. `docs/components/entity-linking.md` is a
reverse-engineered contract written so the measured decomposition shapes can be
tested against the existing behaviour, not so the module can be redesigned.

Verified during contract-writing: **no change is required for integration.** The
five-role loop in `enrich_with_uris_reranker` already degrades correctly to
three-key inputs, because it tests `if val.get(kk)` per role rather than assuming
all five are present.

Going forward: any change to this module must be forced by an integration failure
and recorded as a decision before it is made. Tuning it is out of scope.

## D-007 — Store the measured artifacts outside `data/prompts/`

**Status:** Accepted
**Date:** 2026-09-14

The measured template, lexical schema and 25 demonstrations live in
`backend/data/measured/`, not in `backend/data/prompts/`.

`list_prompt_versions` globs `data/prompts/*.txt`, and `build_prompt` performs no
placeholder substitution. A template containing `{{schema}}` placed there would be
selectable by the legacy path and rendered with its placeholders intact — a prompt
that looks plausible and is silently broken. Separate directories make that
impossible rather than merely unlikely.

## D-008 — `{{schema}}` is substituted verbatim, trailing newline included

**Status:** Accepted
**Date:** 2026-09-14

`load_lexical_schema_text()` returns the schema artifact's bytes exactly as stored,
including its single trailing newline. It does not strip.

**This corrects an error in the `measured-prompt` contract**, which said to "strip
trailing newline(s)". That was written from the document's prose — section 4 calls
`{{schema}}` "verbatim UTF-8" — without checking what verbatim meant at the byte
level.

Caught by the extraction script's self-check before any test or line of code was
written. Rebuilding the document's prompt from the lab template produced 15,790
characters against the recorded 15,791, diverging at offset 3957: the measured
prompt has `}\n\n\nORDERED DEMONSTRATIONS`, three newlines, where the stripped
rebuild had two. The template line is `{{schema}}\n\nORDERED DEMONSTRATIONS`, so
the third newline is the schema file's own.

Had the contract been implemented as written, every prompt the service sent would
have been one byte short of the measured one — and every test would have passed,
because the golden fixture would have been extracted with the same wrong
assumption. The defect was only visible because the fixture is rebuilt from the
lab template and schema independently rather than trusted as a single extraction.

Going forward: the fixture's provenance checks are not optional scaffolding. They
are the only thing standing between a plausible prompt and the measured one.

## D-009 — `Json_schema.json` validates pre-enrichment predictions only

**Status:** Accepted
**Date:** 2026-09-14

The validation schema is scoped to the prediction as parsed from the model, before
Wikidata enrichment. Documents carrying `*URI` / `*URIs` keys are not expected to
validate against it, and its top-level `additionalProperties: false` stays.

**This corrects an error in the `lexical-schema` contract**, whose acceptance test
2 asserted that the five stored five-shot example files validate as they are. They
do not, and should not: they are *enriched* documents, and all five fail with
"Additional properties are not allowed ('hasPropertyURI', ... were unexpected)".

Caught while implementing D-005, when 35 of 36 schema tests went green and that one
did not. The failure was real; the assertion behind it was wrong.

The behaviour is correct as it stands and no code changed:

* `pipeline._finalize_pipeline_output` validates `pred` and never validates
  `enriched`, so the pre-enrichment scope is what the pipeline actually needs.
* `prompts.strip_all_uri_fields` exists precisely because the stored examples carry
  URI keys; it removes them before an example reaches a prompt.
* After stripping, all five examples validate.

The test now asserts the property that is actually true and actually matters: the
examples validate **as the prompt uses them**.

Going forward: if enriched output ever needs validating, that wants a second,
wider schema — not a loosening of this one. Relaxing `additionalProperties` here
would stop the pre-enrichment schema catching stray keys in raw model output, which
is the one job it has.

## D-010 — Update tracked deployment config; leave the local `.env` to the operator

**Status:** Accepted
**Date:** 2026-09-14

`.env.example` and `docker-compose.portainer.yml` now name `Qwen3.8-27B` and carry
`TEMPERATURE`, `TOP_P`, `MAX_TOKENS` and `USE_MEASURED_CONFIGURATION`. The
untracked repo-root `.env` is **not** modified.

Found during end-to-end verification: the pipeline sent `Qwen3.5-397B-A17B` even
though the code default had been changed, because `core/config.py` calls
`load_dotenv(ROOT_DIR / ".env")` and an environment value beats a field default.
Three tracked-or-local places pinned the old model:

| Location | Tracked | Action |
|---|---|---|
| `backend/app/core/config.py` defaults | yes | changed (D-001) |
| `.env.example` | yes | changed |
| `docker-compose.portainer.yml` defaults | yes | changed |
| repo-root `.env` | no | **left alone** |

The old `PSNC_MODEL_NAMES` compounded it: it omitted `Qwen3.8-27B` entirely, so
`resolve_model_name` would have rejected the measured model even if a request
asked for it by name. All three tracked lists now carry it first, with the
previous models retained (D-004).

`.env` is left alone because it holds live API keys and is the operator's own
file; editing it silently would change a running deployment's behaviour without
a review step. **Consequence: an existing deployment keeps running the old,
unmeasured model until its `.env` is updated.** The two lines required are
recorded in `docs/state.md` under "Deploying this change".
