# `measured-prompt`

## 1. Responsibility

Render the measured prompt byte-exactly, for one target definition.

This is the **only** place byte-exactness is enforced. The measured Close F1 of
0.4890 describes a specific sequence of bytes; a prompt that differs by one space
in the demonstrations encoding is a different prompt that nothing has measured.
Everything in this module exists to make that failure impossible to introduce
silently.

Module: `backend/app/services/measured_prompt.py`. Leaf — depends only on
`core.config`.

It does not call the model, does not choose whether the measured configuration is
in use, and does not validate model output. It does not touch `services/prompts.py`,
which continues to serve the legacy path unchanged.

## 2. Inputs

- `definition: str` — the variable definition to decompose, from the caller
  (`pipeline`, originally the `DecomposeRequest.definition` field). Must be
  non-empty after stripping.
- The three artifact files named by `measured-config`, read from disk.

## 3. Outputs

`render_measured_prompt(definition)` returns the complete prompt as a `str`.

For the target definition recorded in `best-configuration-session.md` §5, the
output is **byte-identical** to the verbatim prompt in §4 of that document:
15,791 characters, ending in exactly one `\n`.

No files written, no network.

## 4. Processing and invariants

Ordered steps:

1. Strip-check the definition; reject if empty.
2. Read the template, the lexical schema text, and the demonstrations array.
3. Encode the demonstrations with the documented call (section 8).
4. Substitute all three placeholders in **one pass**.

Invariants:

- **One substitution pass.** Text introduced by one substitution is never scanned
  for another placeholder. A definition containing the literal `{{schema}}` appears
  in the output as `{{schema}}`, not as the schema. This is implemented with a
  single regex pass over the template using a replacement callback — not three
  sequential `str.replace` calls, which would violate it.
- The schema is inserted **verbatim UTF-8**, exactly as stored, with no
  re-serialization *and no stripping*. Re-encoding it through
  `json.loads`/`json.dumps` would change its whitespace; stripping its trailing
  newline removes a byte the measured prompt contains. Both change the prompt.
- The demonstrations encoding is `ensure_ascii=False, sort_keys=True,
  allow_nan=False, separators=(",", ":")`. Every part of that matters:
  `ensure_ascii=False` keeps `→` and `Ω` as themselves, `sort_keys=True` fixes key
  order, and the separators remove the spaces `json.dumps` adds by default.
- The rendered prompt ends with exactly one `\n`.
- Rendering is **fully deterministic**: same definition, same bytes, every time.

## 5. State and side effects

Reads three files per call unless the caller passes cached content. `pipeline-wiring`
caches them at warmup via `app_state`; this module does not own that cache and works
correctly without it. No mutation, no globals, no logging.

## 6. Failures

- Definition empty or whitespace-only → **raised**, `ValueError`. A caller sending
  an empty definition is a caller bug; the API layer already rejects it via
  `min_length=1`.
- Any artifact file missing or unreadable → **raised**, `RuntimeError` naming the
  path. This is a broken deployment, not a runtime condition, and must be loud.
- Demonstrations file not a list of exactly 25 objects → **raised**, `RuntimeError`.
  Silently rendering 24 or 26 demonstrations would produce an unmeasured prompt that
  looks correct.
- Template missing any of the three placeholders → **raised**, `RuntimeError`.

None of these are returned as data. A prompt that cannot be rendered exactly must
not be rendered approximately.

## 7. Configuration consumed

`settings.measured_template_path`, `settings.measured_lexical_schema_path`,
`settings.measured_demonstrations_path`. No defaults are applied here — the paths
come from `measured-config`.

## 8. Public interface

### `load_measured_template() -> str`
- **Action:** read the template file as UTF-8.
- **Output:** the template text, **not** stripped — trailing whitespace is part of
  the measured bytes.
- **Raises:** `RuntimeError` if absent or unreadable.
- **Side effects:** one file read.
- **Determinism:** deterministic for a given file.

### `load_lexical_schema_text() -> str`
- **Action:** read the schema file as UTF-8 and return it **verbatim** — no strip,
  no normalization, no re-serialization.
- **Output:** the file's exact bytes, **including its single trailing newline**.
  The template line is `{{schema}}\n\nORDERED DEMONSTRATIONS`, and the measured
  prompt has *three* newlines between the closing `}` and `ORDERED DEMONSTRATIONS`.
  The third comes from the schema file itself. Stripping it yields a prompt one
  byte short of the measured one — see D-008.
- **Raises:** `RuntimeError` if absent or unreadable.

### `load_demonstrations() -> list[dict]`
- **Action:** read and parse the demonstrations file.
- **Output:** the 25 demonstration objects in stored order.
- **Raises:** `RuntimeError` if absent, unparseable, or not exactly 25 objects.

### `encode_demonstrations(demonstrations: list[dict]) -> str`
- **Input:** the demonstrations list.
- **Action:** `json.dumps(demonstrations, ensure_ascii=False, sort_keys=True,
  allow_nan=False, separators=(",", ":"))`.
- **Output:** a single-line JSON array, 11,519 characters for the stored 25.
- **Raises:** `ValueError` from `json.dumps` on non-finite floats (`allow_nan=False`).
- **Determinism:** fully deterministic.

### `render_measured_prompt(definition: str, *, template: str | None = None, schema_text: str | None = None, demonstrations: list[dict] | None = None) -> str`
- **Input:** `definition`, non-empty after stripping. The three keyword arguments
  are the warmup cache injection point; when `None`, the loaders are called.
- **Action:** the four steps of section 4.
- **Output:** the complete prompt. Ends with exactly one `\n`.
- **Raises:** `ValueError` on empty definition; `RuntimeError` on any artifact
  problem.
- **Side effects:** file reads only when the cache arguments are omitted.
- **Determinism:** fully deterministic.

The definition is inserted **verbatim** — not stripped, not normalized, not escaped.
Whatever the caller passes is what the model sees.

## 9. Acceptance tests

The golden fixture is `backend/tests/fixtures/measured_prompt_golden.txt`: the
verbatim prompt from `best-configuration-session.md` §4, with exactly one trailing
newline (the document's fence adds a second — it is stripped when the fixture is
created).

1. **The byte-exactness test.** `render_measured_prompt` called with the §5 target
   definition equals the golden fixture byte-for-byte. This is the test the module
   exists for.
2. The rendered prompt is 15,791 characters.
3. `encode_demonstrations(load_demonstrations())` equals the exact demonstrations
   line inside the golden fixture.
4. Re-encoding with default `json.dumps` separators does **not** equal that line —
   proving the test in (3) would actually catch a regression.
5. **One substitution pass:** rendering a definition of `"{{schema}}"` produces a
   prompt whose `TARGET DEFINITION` section contains the literal `{{schema}}`, and
   the schema block appears exactly once in the whole prompt.
6. The prompt ends with exactly one `\n` (`prompt[-2] != "\n"`).
6a. `load_lexical_schema_text()` output ends with exactly one `\n`, and equals the
    artifact file's bytes exactly.
6b. The rendered prompt contains `}\n\n\nORDERED DEMONSTRATIONS` — three newlines,
    not two. This is the specific byte the D-008 contract error would have lost,
    and it is asserted directly so a future "tidy-up" strip cannot pass the suite.
7. The definition is inserted verbatim: leading and trailing spaces in the input
   survive into the output.
8. Two calls with the same definition return identical strings.
9. `""`, `"   "`, and `"\n"` each raise `ValueError`.
10. A missing template, schema, or demonstrations file each raise `RuntimeError`
    naming the path.
11. A demonstrations file with 24 objects raises `RuntimeError`.
12. A template missing `{{target_definition}}` raises `RuntimeError`.
13. Passing cached `template`/`schema_text`/`demonstrations` produces the same
    output as loading from disk, and performs no file reads.
