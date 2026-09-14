# `entity-linking`

> **This contract documents existing behaviour. It does not propose changes.**
>
> The experiments never covered entity linking, so nothing measured governs it.
> Wikidata search, the `bge-reranker-v2-m3` reranker and `RERANK_THRESHOLD` keep
> their current steps exactly. This contract was reverse-engineered from the code
> so that the measured decomposition shapes can be tested against it, not so the
> module can be redesigned. Any change here must be forced by integration, and
> must be recorded as a decision before it is made.

## 1. Responsibility

For each label in a prediction, find the best-matching Wikidata entity and attach
a `*URI` field next to it.

Module: `backend/app/services/enrichment.py` (existing, unchanged). Depends on
`clients.http` for Wikidata search, `services.reranker` for scoring, and
`core.config` for the threshold.

It does not decide whether linking runs — `pipeline._finalize_pipeline_output`
gates that on `settings.enable_wikidata_linking` and swallows any exception,
falling back to the unenriched prediction. It does not serialize to TTL, and it
does not validate.

## 2. Inputs

- `pred: Dict[str, Any]` — the prediction, from the pipeline. Read-only.
- `threshold: Optional[float]` — minimum rerank score; `settings.rerank_threshold`
  (default `0.10`) when `None`.

`pred["definition"]` is used as disambiguation context for every lookup. When
absent, the context is `""` and lookups still proceed.

## 3. Outputs

A **deep copy** of `pred` with `*URI` / `*URIs` keys added where a match cleared the
threshold. The input is never mutated — the copy is made with
`json.loads(json.dumps(pred))`, which also means the prediction must be
JSON-serializable.

Keys added, and only these:

| Where | Key added |
|---|---|
| `hasProperty`, `hasMatrix`, `hasObjectOfInterest`, `hasContextObject`, `hasStatisticalModifier` — **when the value is a string** | `<field>URI` |
| asymmetric system on `hasMatrix`, `hasObjectOfInterest`, `hasContextObject` | `AsymmetricSystemURI`, and `hasSourceURI` / `hasTargetURI` / `hasNumeratorURI` / `hasDenominatorURI` for each role key **present and non-empty** |
| symmetric system on those same three fields | `SymmetricSystemURI`, and `hasPartURIs` |

`hasPartURIs` is a list positionally aligned with `hasPart`, holding `None` for
parts that did not link. It is set **only if at least one part linked**; when no
part links, the key is absent entirely.

`hasProperty` and `hasStatisticalModifier` are linked **only as strings**. A dict
there is left alone. This is current behaviour and is correct for the measured
configuration, where both fields are always strings.

## 4. Processing and invariants

Per label: URL-encode the term → `GET wbsearchentities` (20s timeout) → build one
document string per hit from its `label` and `description` → score them all with
`call_psnc_reranker` against the query `Definition of "<term>" in context:
"<definition>"` → sort descending → accept the top hit if its score is
`>= threshold`, else return `None`.

Invariants:

- The input `pred` is never mutated.
- A label that is empty, whitespace-only, or not a string is skipped — no key added.
- A term whose best score is below the threshold adds no key.
- A non-200 Wikidata response, or an empty `search` array, adds no key.
- **Role keys absent from an entity are skipped, not defaulted.** The loop is
  `for kk in [...]: if val.get(kk):` — which is exactly why a three-key asymmetric
  system works.
- Output ordering within `hasPartURIs` matches `hasPart` positionally.

Not deterministic: it depends on live Wikidata results and reranker scores.

## 5. Compatibility with the measured shapes

This is the reason the contract exists. Verified against the code, to be pinned by
the tests below:

| Measured shape | Count in the 25 | Handling |
|---|---|---|
| `{AsymmetricSystem, hasNumerator, hasDenominator}` | 8 | Links all 3 present keys; `hasSource`/`hasTarget` skipped |
| `{AsymmetricSystem, hasSource, hasTarget}` | 2 | Links all 3 present keys; numerator/denominator skipped |
| `{SymmetricSystem, hasPart}` | 1 | Links the container and each part |
| plain string | rest | Links the string |

**No change is required for integration.** The five-key loop already degrades
correctly to three-key inputs.

## 6. State and side effects

Two outbound calls per linkable label: one Wikidata `GET`, one PSNC rerank `POST`.
A prediction with an asymmetric system can therefore issue up to 8 label lookups
(5 top-level string fields + 3 system keys), each costing two requests. No caching,
no persistence, no retry.

## 7. Failures

- Wikidata returns non-200, or `search` is empty → **returned as data**: no URI key.
- `call_psnc_reranker` raises (mismatched score count, invalid index) → **raised**,
  and caught one level up by `_finalize_pipeline_output`, which logs
  `"Wikidata enrichment failed: ..."` and returns the unenriched prediction. The
  decomposition still succeeds. This module does not catch it itself.
- `pred` not JSON-serializable → **raised**, `TypeError`, from the deep copy.

## 8. Configuration consumed

- `RERANK_THRESHOLD` — float, default `0.10`. The minimum accepted rerank score.
- `PSNC_RERANK_MODEL` — str, default `bge-reranker-v2-m3`, read by `services.reranker`.
- `ENABLE_WIKIDATA_LINKING` — read by the **pipeline**, not here. Note its
  non-standard truthiness: only the literal string `"true"` is true, so `"1"` and
  `"yes"` disable linking. Preserved deliberately; see `core/config.py`.

## 9. Secrets

`psnc_chat_headers`-equivalent bearer auth is used by the reranker. The Wikidata
search URL carries only the search term and must never carry anything else — terms
come from model output, not from user credentials.

## 10. Public interface

Unchanged. `qid_from_uri_or_text(s)`, `to_wiki_url(uri)`,
`get_wikidata_entity_reranker(term, context="", threshold=None)`,
`enrich_with_uris_reranker(pred, threshold=None)` — signatures, names and
behaviour all exactly as they are today.

## 11. Acceptance tests

All tests stub the Wikidata `GET` and the reranker. None makes a network call, and
none asserts anything that would change if the linking algorithm were tuned — they
pin **shape handling**, which is what integration depends on.

1. `{AsymmetricSystem, hasNumerator, hasDenominator}` on `hasObjectOfInterest`
   yields `AsymmetricSystemURI`, `hasNumeratorURI`, `hasDenominatorURI` — and
   **no** `hasSourceURI` or `hasTargetURI`.
2. `{AsymmetricSystem, hasSource, hasTarget}` yields the mirror of (1).
3. `{SymmetricSystem, hasPart: [a, b]}` yields `SymmetricSystemURI` and
   `hasPartURIs` of length 2, positionally aligned.
4. When no part links, `hasPartURIs` is **absent**, not `[None, None]`.
5. When one of two parts links, `hasPartURIs == [uri, None]`.
6. The input `pred` is byte-identical before and after the call.
7. A string entity below the threshold adds no key.
8. An empty-string entity adds no key and issues no lookup.
9. A dict on `hasProperty` is left untouched — documented current behaviour.
10. **All 25 measured decompositions** pass through `enrich_with_uris_reranker`
    without raising, with the stub linking everything.
11. `to_wiki_url("Q42")` → `https://www.wikidata.org/wiki/Q42`;
    `to_wiki_url(None)` → `None`; a non-QID URI has `http` upgraded to `https`.
12. `qid_from_uri_or_text` finds `Q42` inside a full URI and returns `None` for
    text without a QID.
13. A reranker exception propagates out of `enrich_with_uris_reranker` — it is not
    swallowed here — and `_finalize_pipeline_output` returns the unenriched
    prediction with the decomposition intact.
