# `label-comment`

## 1. Responsibility

Produce the `label` and `comment` fields that the measured prompt explicitly
forbids the model from generating, using a second, separate LLM call.

The measured template says verbatim: *"Do not regenerate the definition, label, or
comment."* The service needs both — `rdf_ttl.py:277` falls back to the string
`"generated variable"` for a missing label, and `:280` to `""` for a missing
comment. Publishing nanopublications labelled "generated variable" is not
acceptable, and adding the fields back into the measured prompt would change its
bytes and void the measurement. A second call is the only option that keeps both
intact. See `docs/decisions.md` D-002.

Module: `backend/app/services/label_comment.py`.

It does not decompose, does not validate against the schema, and does not decide
when it runs. It is **not** part of the measured configuration and carries none of
its evidence — this prompt has never been scored against anything.

## 2. Inputs

- `definition: str` — the variable definition, from the caller. Non-empty after
  stripping.
- `model_provider: str`, `model: str`, `temperature: float` — the same resolved
  provider, model and temperature the decomposition used, so labelling does not
  silently reach a different backend.

## 3. Outputs

`generate_label_and_comment(...)` returns a `tuple[str, str]` of `(label, comment)`.

Both are always non-empty strings. There is no `None` return and no partial result:
when the model cannot supply a field, the deterministic fallback does.

- `label` — at most 80 characters.
- `comment` — the definition verbatim on the fallback path.

## 4. Processing and invariants

1. Strip-check the definition; reject if empty.
2. Render the labelling prompt (section 8).
3. Call the provider once, through the existing `call_psnc_model` /
   `call_model` helpers. **One attempt from this module's perspective** — those
   helpers already retry up to 3 times internally, and a second retry layer would
   turn a labelling hiccup into a 9-attempt stall on a user-facing request.
4. Extract JSON with the existing `parse_llm_json` extraction behaviour.
5. Take `label` and `comment` from the parsed object; substitute the fallback for
   each field independently when missing, non-string, or blank.
6. Truncate the label to 80 characters at a whitespace boundary.

Invariants:

- **This function never raises for a model or transport problem.** Any exception
  from the call, the parse, or the extraction is caught and becomes the fallback.
  A decomposition must never fail because labelling failed — that would make an
  unmeasured auxiliary call able to break the measured path.
- Both returned strings are always non-empty.
- The fallback is **fully deterministic** for a given definition.
- Per-field fallback: a response supplying a good `comment` but no `label` keeps
  the comment and falls back only the label.

The success path is **not deterministic** — it is an LLM call at the same
temperature as the decomposition.

## 5. The fallback

Deterministic, computed from the definition alone:

- `label`: the definition with internal whitespace collapsed to single spaces and
  ends stripped; if longer than 80 characters, cut at the last whitespace at or
  before 80 and strip trailing punctuation and whitespace. No ellipsis.
- `comment`: the definition verbatim, unmodified.

Both are strictly better than the `rdf_ttl` defaults they replace
(`"generated variable"` and `""`), so even total failure of this module leaves the
service better off than today.

## 6. State and side effects

One outbound LLM call per invocation. No caching, no persistence, no mutation of
the caller's data. Failures are reported via `print`, matching the existing
convention in `services/llm.py` — this module introduces no new logging mechanism.

## 7. Failures

- Definition empty or whitespace-only → **raised**, `ValueError`. Caller bug.
- Model returns empty, HTML, malformed JSON, or valid JSON without the keys →
  **returned as data**: the fallback, per field.
- Transport error, timeout, or any unexpected exception → **returned as data**: the
  full fallback pair. Never propagated.

## 8. The labelling prompt

Fixed text, defined here so it is not left to the implementer. It is short
deliberately: it is unmeasured, and every token spent on it is latency added to a
user-facing request.

```
Write a short label and a short comment for this scientific variable definition.

label = a concise human-readable name, at most 80 characters. Not a sentence.
comment = a one-sentence summary of the definition. Do not add new concepts.

Definition:
{definition}

Return exactly one JSON object with the keys "label" and "comment", without prose,
Markdown fences, or explanations.
```

One user message, no system message, matching how the rest of the service calls
these providers.

## 9. Public interface

### `build_label_comment_prompt(definition: str) -> str`
- **Action:** substitute the definition into the template above, in one pass.
- **Output:** the prompt text.
- **Raises:** `ValueError` if the definition is empty after stripping.
- **Determinism:** fully deterministic.

### `fallback_label_and_comment(definition: str) -> tuple[str, str]`
- **Action:** apply the section 5 rules.
- **Output:** `(label, comment)`, both non-empty; label at most 80 characters.
- **Raises:** `ValueError` if the definition is empty after stripping.
- **Side effects:** none.
- **Determinism:** fully deterministic.

### `generate_label_and_comment(definition: str, *, model_provider: str, model: str, temperature: float) -> tuple[str, str]`
- **Input:** as section 2.
- **Action:** steps 1–6 of section 4.
- **Output:** `(label, comment)`, both non-empty; label at most 80 characters.
- **Raises:** `ValueError` on an empty definition — and nothing else, ever.
- **Side effects:** one LLM call; may `print` on failure.
- **Determinism:** not deterministic on the success path; the fallback path is.

## 10. Acceptance tests

1. A response of `{"label": "Soil moisture", "comment": "Water in soil."}` returns
   exactly that pair.
2. A response wrapped in ```` ```json ```` fences is parsed to the same pair.
3. An empty response returns the fallback pair.
4. A malformed-JSON response returns the fallback pair.
5. A response missing `label` keeps the returned `comment` and falls back only the
   label — per-field, not all-or-nothing.
6. A response whose `label` is `""` or `"   "` falls back the label.
7. A response whose `label` is a non-string (a number, a dict) falls back the label.
8. The LLM call raising an arbitrary `Exception` returns the fallback pair and
   **does not raise**.
9. `generate_label_and_comment("")` and `("   ")` raise `ValueError`.
10. `fallback_label_and_comment` returns identical output across two calls.
11. For a 300-character definition, the fallback label is at most 80 characters,
    ends on a word boundary, and has no trailing punctuation or whitespace.
12. For a short definition, the fallback label is the definition itself and the
    comment is byte-identical to the input.
13. A label longer than 80 characters returned by the model is truncated to at most
    80 characters.
14. The provider helper is called exactly **once** — this module adds no retry
    layer of its own.
15. `build_label_comment_prompt` inserts the definition verbatim and performs one
    substitution pass: a definition containing `{definition}` appears literally.
