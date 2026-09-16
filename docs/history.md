# History

## 2026-09-14 — Adopt the measured PSNC configuration: plan, contracts, failing tests

**Done:** Phases 1–3 of the contract-first workflow for replacing the service's
unmeasured LLM configuration with the one measured in `iadopt-lab`. Plan in
`docs/state.md`, seven contracts in `docs/components/`, decisions D-001…D-008,
144 tests across seven files, and the measured artifacts extracted into
`backend/data/measured/`. Implementation not started — every new module is a
skeleton raising `NotImplementedError`.

**Decided:** The measured configuration becomes the default but stays reversible
via `USE_MEASURED_CONFIGURATION=0` (D-001, D-004). `label`/`comment`, which the
measured prompt forbids, come from a second LLM call rather than from an extended
prompt (D-002). The measured request body gets its own builder so the legacy one
cannot regress (D-003). Entity linking is documented and pinned, never modified
(D-006).

**Rejected:** Adding `label`/`comment` back into the measured prompt — it changes
the prompt bytes, so nothing in the study would describe what the service runs.
Deriving them in code with no LLM call — kept as the fallback path, but label
quality is what a reader sees first on a published nanopublication. Threading a
flag through `build_psnc_chat_payload` — it would leave the legacy path one boolean
from a silent regression. Raising `hasPart.minItems` to the lab's 2 — it would
newly reject existing single-part data for no benefit.

**Learned:** Three findings that changed the work.

1. The report originally cited as the source does not contain the configuration.
   It ranks candidate example-sets; the model, prompt, parameters and 25 examples
   live in its sibling `best-configuration-session.md`.
2. **A pre-existing schema defect.** `Json_schema.json` required an asymmetric
   system to carry all five role keys at once, while the lab schema and prompt
   require exactly one pair. 10 of the 25 measured gold decompositions fail the
   service schema, and so do three of the service's own five-shot examples. The
   golden `asymmetric_soil_moisture.validation.json` had `schema_valid: false`
   frozen in as expected behaviour — the service was marking its own example data
   invalid. Only validation was affected; enrichment and TTL both handle three-key
   shapes correctly, which is why it went unnoticed (D-005).
3. **A contract error, caught before implementation.** The `measured-prompt`
   contract said to strip the schema's trailing newline. The measured prompt
   contains it: `}\n\n\nORDERED DEMONSTRATIONS`, three newlines, where the
   stripped rebuild had two. Rebuilding the documented prompt from the lab template
   came out at 15,790 characters against the recorded 15,791, diverging at offset
   3957. Every prompt would have been one byte short of measured, and every test
   would have passed, because the golden fixture would have carried the same wrong
   assumption. Only visible because the fixture is rebuilt from independent sources
   rather than extracted once (D-008).

**Executed:** `backend/tools/extract_measured_artifacts.py` against the lab
checkout. All four self-checks pass: demonstrations re-encode to the documented
bytes, the prompt's schema block matches the lab schema file, the lab template
rebuilds the document's prompt exactly, and the result is 15,791 characters.
Checksums recorded in `backend/tests/fixtures/PROVENANCE.md`.

Test suite at the Phase 3 gate: 65 failed, 79 passed, 16 skipped, 6 errors. No
test fails on import. `entity-linking`'s 12 tests passed on first run, which is the
evidence that the measured entity shapes need no change there.

**Next:** Phase 4 — implement one test at a time in the delivery order
`measured-config → measured-prompt → lexical-schema → psnc-payload →
label-comment → pipeline-wiring`.

---

## 2026-09-14 — Implementation complete

**Done:** All seven modules implemented in the delivery order. Final suite: **167
collected, 151 passed, 16 skipped, 0 failures**; ruff and mypy clean on 36 modules.
The approved golden flip landed — `asymmetric_soil_moisture.validation.json` is now
`schema_valid: true` with no errors, and `asymmetric_soil_moisture.expected.ttl` is
byte-identical, confirming validation and serialization stayed independent.
`entity-linking` was not modified.

**Decided:** Tracked deployment config (`.env.example`,
`docker-compose.portainer.yml`) now names the measured model and parameters; the
untracked `.env` was deliberately left to the operator (D-010).

**Learned:** Two things surfaced only by running the code.

1. **The test was wrong, not the code.** After the schema correction, 35 of 36
   schema tests went green and one did not: the five stored five-shot examples do
   not validate as stored. They are *enriched* documents carrying `*URI` keys, and
   the schema is scoped to pre-enrichment predictions with
   `additionalProperties: false`. The pipeline validates `pred` and never
   `enriched`, and `strip_all_uri_fields` exists precisely for this. The assertion
   was amended to test the examples as the prompt actually uses them, and a
   vacuity guard was added so it cannot pass if the URIs ever disappear (D-009).
2. **Changing the code default was not enough.** End-to-end verification showed the
   pipeline still sending `Qwen3.5-397B-A17B`, because `load_dotenv` puts the
   repo-root `.env` ahead of every field default. The old `PSNC_MODEL_NAMES` also
   omitted `Qwen3.8-27B` entirely, so `resolve_model_name` would have rejected the
   measured model even by explicit request (D-010).

**Executed:** End-to-end run with the provider stubbed at the HTTP boundary, using
the target definition from the lab document's section 5. The body sent to PSNC
carried exactly the seven measured keys, named `Qwen3.8-27B`, and its prompt was
byte-identical to the golden fixture. The merged prediction validated and the TTL
carried a real label instead of the `"generated variable"` placeholder.

**Next:** Update the deployment `.env` (two lines, recorded in `docs/state.md`),
then commit. Open question left for the frontend: whether the UI should state that
decompositions are drafts, given 7 of 24 held-out items scored zero.

---

## 2026-09-14 — CI gate, documentation reconciliation

**Done:** Closed the two blockers the commit-ready audit raised, then ran a
docs-drift pass.

Backend CI ran `python -m unittest discover -s tests`, which collects only
`unittest.TestCase` subclasses — **12 of 167 tests**. It now runs
`python -m pytest tests`. `pytest==9.1.1` was already in
`backend/requirements.txt`, so CI had been installing it without using it since
the suite was added.

`README.md` and `docs/CONTRACTS.md` were updated to describe the measured
configuration, the new model and sampling parameters, the `entityOrSystem`
correction and its approved golden flip, and the current test baseline. The README
also gained an explicit statement that decompositions are drafts, with the honest
reading of the 0.4890 figure beside it.

**Learned:** The CI gap was broader than this change. `test_settings_parity.py` and
the entire `tests/contract/` suite are pytest-style and were *already* invisible to
`unittest discover`, so the "45 backend tests pass" gate recorded in
`docs/CONTRACTS.md` had not been enforced by CI for some time. This change did not
create the gap; it made it large enough to notice.

**docs-drift pass:** One genuine finding, pre-existing and unrelated to this work.
`docs/CONTRACTS.md` named `_make_variable_identity()` at `main.py:1589`; Phase 2
renamed it to `make_variable_identity()` and moved it to
`backend/app/services/rdf_ttl.py:84`. The behavioural claim around it — that it
reads `datetime.now()` and `random.randint(0, 99)`, which is why golden tests
freeze the clock and RNG — was verified still true, so only the name and location
were corrected. Four other script findings were false positives: a third-party
`load_dotenv` import, an HTTP route read as a filesystem path, and two markdown
link texts that resolve correctly.

Three contract claims were checked directly against the diff rather than assumed:
`build_psnc_chat_payload` has zero deleted lines (legacy builder byte-identical),
`enrichment.py` and `reranker.py` have zero changes (D-006 holds), and
`pipeline.py` shows only added definitions (existing signatures intact).

**Next:** Commit.
