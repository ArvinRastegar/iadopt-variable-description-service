# `pipeline-wiring`

## 1. Responsibility

Select the measured path when it applies, and merge its six-field output plus
`label`/`comment` into the eight-field prediction the rest of the pipeline already
expects.

Module: `backend/app/pipeline.py` (existing), with a new cache field on
`backend/app/core/state.py`.

This is the only module that knows the measured configuration exists as a *choice*.
Everything downstream of the merge — validation, enrichment, TTL, nanopub — sees
the same eight-field prediction it sees today and is untouched.

It does not render the measured prompt (`measured-prompt`), build the payload
(`psnc-payload`), or generate the label (`label-comment`). It sequences them.

## 2. Inputs

From the caller (`routers/decompose.py`), unchanged:
`definition`, `model_name`, `model_provider`, `disable_thinking`, `creator_orcid_id`.

From configuration: `settings.use_measured_configuration`, `settings.temperature`,
`settings.top_p`, `settings.max_tokens`.

## 3. Outputs

Unchanged. `run_pipeline` returns the same `DecomposeResponse`-shaped dict —
`raw_llm_output`, `parsed_json`, `schema_valid`, `validation_errors`,
`enriched_json`, `ttl`. `stream_pipeline_events` yields the same
`raw_delta` / `final` / `error` NDJSON events.

**No endpoint request or response shape changes.** `docs/CONTRACTS.md` stays true
and the 25 live contract tests stay green.

## 4. Path selection

The measured path is taken when **both** hold:

1. `settings.use_measured_configuration` is true, **and**
2. the resolved provider is `psnc`.

Otherwise the legacy path runs exactly as today. The provider condition is not
optional: the measurement is PSNC-only, and rendering the measured prompt into an
OpenRouter request would produce an unmeasured configuration wearing a measured
prompt — the worst of both.

Selection happens **after** `resolve_model_provider`, so an explicit
`model_provider: "openrouter"` in the request still reaches OpenRouter with the
legacy prompt.

## 5. Processing

Measured path, in order:

1. Resolve provider and model (`resolve_model_provider`, `resolve_model_name`) —
   unchanged.
2. Render the prompt with `render_measured_prompt`, passing the warmup-cached
   artifacts.
3. Call PSNC with `build_measured_psnc_payload`, streamed or not per endpoint.
4. Parse the six-field object from the raw text using the existing extraction.
5. Call `generate_label_and_comment` with the **same** provider, model and
   temperature.
6. Merge into the eight-field prediction.
7. Hand to `_finalize_pipeline_output` — unchanged.

Step 5 runs **after** step 4 and only when step 4 produced a prediction. A failed
decomposition must not spend a second call on labelling something that will not be
returned.

For the streaming endpoint, steps 5–7 run after the stream completes, before the
`final` event is emitted. `raw_delta` events carry the decomposition stream only;
the labelling call is never streamed.

## 6. The merge

`merge_measured_prediction(six_field, *, label, comment, definition) -> Dict[str, Any]`

Produces a dict with exactly the keys `coerce_prediction` guarantees, plus the
three the measured prompt omits:

- the six onto keys, from the model, passed through the existing
  `coerce_prediction` so defaults and the `hasProperty`-dict flattening behave
  identically to the legacy path
- `definition` — the caller's definition **verbatim**, never the model's
- `label`, `comment` — from `label-comment`

Invariants:

- `definition` is the stripped input definition, byte-identical to what was sent in
  the prompt. The model is forbidden from regenerating it, and if it emits one
  anyway it is discarded.
- All eight keys are always present.
- Any key the model emitted outside the six plus `definition`/`label`/`comment` is
  preserved, matching `coerce_prediction`'s existing permissiveness — `strip_all_uri_fields`
  and the TTL serializer already ignore what they do not recognize.

## 7. State and side effects

`warmup_assets` gains one cache, populated only when
`settings.use_measured_configuration` is true:

`app_state.measured_assets_cache: Optional[Tuple[str, str, List[Dict[str, Any]]]]`
— `(template, schema_text, demonstrations)`.

A warmup failure to load the artifacts is **fatal**: the app must not start serving
a configuration it cannot render. This is stricter than the legacy caches, which
fall back to reading from disk per request, and deliberately so.

Network: two LLM calls per decomposition on the measured path instead of one, plus
the existing enrichment calls.

## 8. Failures

- Definition empty → **raised**, `ValueError`. Unchanged.
- Artifacts unloadable at warmup → **fatal**, `RuntimeError`, app does not start.
- Decomposition call fails or output never parses → unchanged behaviour: three
  attempts, then `_finalize_pipeline_output` raises `RuntimeError("Could not
  extract valid JSON from the model output.")`, surfaced as today.
- Labelling fails → **not a failure**: `label-comment` returns its fallback and the
  decomposition is returned normally.
- Enrichment fails → unchanged: caught, logged, unenriched prediction returned.

## 9. Configuration consumed

- `USE_MEASURED_CONFIGURATION` — bool, default true.
- `TEMPERATURE`, `TOP_P`, `MAX_TOKENS` — forwarded to the payload builder.
- `PSNC_MODEL_NAME` — the default model when the request names none.

## 10. Public interface

### `merge_measured_prediction(six_field: Dict[str, Any], *, label: str, comment: str, definition: str) -> Dict[str, Any]`
- **Input:** `six_field` as parsed from the model; `label` and `comment` non-empty;
  `definition` the stripped caller input.
- **Action:** `coerce_prediction`, then set `definition`, `label`, `comment`.
- **Output:** the eight-field prediction.
- **Raises:** nothing.
- **Side effects:** none. Does not mutate `six_field`.
- **Determinism:** fully deterministic.

### `use_measured_path(provider: str) -> bool`
- **Input:** the **resolved** provider.
- **Output:** `settings.use_measured_configuration and provider == PSNC_MODEL_PROVIDER`.
- **Determinism:** deterministic for a given settings state.

`warmup_assets`, `run_pipeline`, `stream_pipeline_events` and
`_finalize_pipeline_output` keep their existing signatures.

## 11. Acceptance tests

1. `use_measured_path("psnc")` is true by default;
   `use_measured_path("openrouter")` is false.
2. With `USE_MEASURED_CONFIGURATION=0`, `use_measured_path("psnc")` is false.
3. On the measured path with a stubbed provider, the prompt sent is byte-identical
   to `render_measured_prompt(definition)`.
4. On the measured path, the payload sent is the seven-key measured body.
5. With `model_provider="openrouter"`, the legacy prompt is sent and the measured
   prompt is never rendered.
6. The merged prediction has exactly the eight expected keys.
7. `definition` in the result is the caller's definition, even when the model
   emitted a different `definition` value.
8. `label` and `comment` come from `generate_label_and_comment`.
9. When the labelling call raises, the decomposition is still returned, with the
   fallback label and comment.
10. When the decomposition never parses, `generate_label_and_comment` is **not**
    called.
11. `merge_measured_prediction` does not mutate its input.
12. `run_pipeline` returns the same six top-level keys as before.
13. `stream_pipeline_events` emits the same event types in the same order, and no
    `raw_delta` contains labelling output.
14. `warmup_assets` populates `measured_assets_cache` when the flag is on and
    leaves it `None` when off.
15. `warmup_assets` raises when the flag is on and an artifact is missing.
16. **Regression:** the 25 existing live contract tests and the golden TTL tests
    pass unchanged.
