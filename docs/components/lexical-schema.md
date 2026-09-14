# `lexical-schema`

## 1. Responsibility

Own `backend/data/Json_schema.json` — the schema that model output is **validated
against** — and make its `entityOrSystem` definition agree with the shapes the
measured prompt actually instructs the model to produce.

Not to be confused with `backend/data/measured/lexical-decomposition.schema.json`,
which is a frozen artifact embedded *into* the prompt text and is owned by
`measured-config`. One schema goes in, the other checks what comes out.

This module does not change how validation is invoked, what it does with errors,
or the `schema_valid` / `validation_errors` response fields. It changes one
`$defs` entry.

## 2. The defect being corrected

The current `entityOrSystem` asymmetric branch requires **all five** of
`AsymmetricSystem`, `hasSource`, `hasTarget`, `hasNumerator`, `hasDenominator`,
with `additionalProperties: false`.

The lab schema — and the measured prompt, which says *"do not mix the role pairs"* —
defines two **mutually exclusive** three-key variants: source/target, or
numerator/denominator.

Measured consequence: **10 of the 25 measured gold decompositions fail the current
service schema** (8 numerator/denominator, 2 source/target).

This predates the measured configuration:

- `backend/data/Json_preferred/five_shot/SoilMoist.json`, `SurfRunoff.json` and
  `DetritalNitrogenConc.json` all use the three-key source/target shape.
- `backend/tests/contract/golden/asymmetric_soil_moisture.validation.json` has
  `"schema_valid": false` frozen in as expected behaviour.

The service has been marking its own example data invalid.

## 3. Scope: un-enriched predictions only

This schema validates the prediction **before** Wikidata enrichment. It carries
`additionalProperties: false` at the top level, so a document holding `*URI` /
`*URIs` keys does not validate against it — by design.

That matches how the pipeline uses it: `_finalize_pipeline_output` validates
`pred` and never validates `enriched`. The stored five-shot example files are
enriched documents and therefore do **not** validate as stored; `strip_all_uri_fields`
removes those keys before the examples reach a prompt, and they validate after that.
See D-009.

## 4. Inputs and outputs

Input: the existing `Json_schema.json`. Output: the same file with
`$defs.entityOrSystem.oneOf` replaced by four branches:

1. `{"type": "string"}` — **unchanged**
2. `{AsymmetricSystem, hasSource, hasTarget}`, all required, `additionalProperties: false`
3. `{AsymmetricSystem, hasNumerator, hasDenominator}`, all required, `additionalProperties: false`
4. `{SymmetricSystem, hasPart}` — **unchanged**, including `hasPart.minItems: 1`

No code changes. `load_schema` and `patch_schema_for_pipeline` are untouched.

## 5. Processing and invariants

No runtime processing — this module is a data file. The invariants it must satisfy:

- The three shapes the measured configuration emits all validate.
- A **mixed** object (`AsymmetricSystem` + `hasSource` + `hasNumerator`) fails, as
  the prompt forbids it.
- `hasPart.minItems` stays at **1**, not the lab's 2. Raising it would newly reject
  existing single-part data for no benefit; lab output always has ≥2 parts, so the
  looser bound accepts everything the measured configuration produces.
- The top-level required list (`label`, `definition`, `comment`, `hasProperty`,
  `hasObjectOfInterest`) is **unchanged**. Those three fields are supplied by
  `label-comment` and `pipeline-wiring`, not by the model.

## 6. Deliberate behaviour change

A five-key asymmetric object — which the *old* schema required — now fails both
new branches.

This is safe and was verified, not assumed: the only golden containing a system is
`asymmetric_soil_moisture`, which is three-key; the only other validation golden,
`simple_air_temperature`, is string-only. No five-key object exists in any fixture,
example, or test in the repo.

`asymmetric_soil_moisture.validation.json` changes from
`{"schema_valid": false, "validation_errors": [6 lines]}` to
`{"schema_valid": true, "validation_errors": []}`. That fixture is part of the
Phase-0 contract baseline, so the change is deliberate and approved — see
`docs/decisions.md` D-005.

`asymmetric_soil_moisture.expected.ttl` must **not** change. TTL serialization never
consulted the schema, so correcting validation cannot move a byte of it. A diff
there means something else broke.

## 7. Failures

- Malformed JSON in the file → **fatal**: `load_schema` raises at warmup and the
  app does not start. Existing behaviour, unchanged.

## 8. Acceptance tests

1. All 25 measured decompositions, each with `label`/`definition`/`comment` added,
   validate against the pipeline-patched schema. (Currently 10 fail.)
2. Each of the five service five-shot example files validates **after
   `strip_all_uri_fields`** — which is how the prompt builder uses them. As stored
   they carry `*URI` keys and correctly fail against `additionalProperties: false`;
   asserting otherwise would be asserting that enrichment output must validate
   against the pre-enrichment schema, which the pipeline never asks of it (D-009).
3. `{AsymmetricSystem, hasSource, hasTarget}` validates.
4. `{AsymmetricSystem, hasNumerator, hasDenominator}` validates.
5. `{AsymmetricSystem, hasSource, hasNumerator}` — mixed pairs — **fails**.
6. `{AsymmetricSystem, hasSource, hasTarget, hasNumerator, hasDenominator}` — the
   old five-key shape — **fails**.
7. `{AsymmetricSystem, hasSource}` — incomplete pair — **fails**.
8. `{SymmetricSystem, hasPart: ["a"]}` validates — `minItems` is still 1.
9. `{SymmetricSystem, hasPart: ["a","b"]}` validates.
10. A plain string entity validates on each of `hasObjectOfInterest`, `hasMatrix`,
    `hasContextObject`.
11. An entity object with an unexpected extra key fails
    (`additionalProperties: false` still holds).
12. The `asymmetric_soil_moisture` golden round-trip reports
    `schema_valid: true` and an empty `validation_errors`.
13. `asymmetric_soil_moisture.expected.ttl` is unchanged, byte-for-byte.
14. `simple_air_temperature` still reports `schema_valid: true` — the fix did not
    loosen anything that was already correct.
