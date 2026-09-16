# `measured-config`

## 1. Responsibility

Hold the three frozen artifacts of the measured configuration, and the settings
that name them and carry its sampling parameters.

Artifacts are stored under `backend/data/measured/` and are **copies, byte-for-byte,
of files owned by `iadopt-lab`**. This module is the boundary at which they stop
being someone else's files and become this repo's frozen inputs.

It does not render prompts, does not call anything, and contains no branching
logic. It does not decide *when* the measured configuration is used — that is
`pipeline-wiring`.

The artifacts deliberately live outside `backend/data/prompts/`. That directory is
globbed by `list_prompt_versions`, and a template containing `{{schema}}`
placeholders would be selectable by the legacy path and rendered with its
placeholders unsubstituted. Keeping them separate makes that impossible.

## 2. Inputs

Files on disk, read at warmup:

Sources in the middle column are paths inside the **external** `iadopt-lab`
repository, not this one. They record provenance; they do not resolve here.

| Path (this repo) | Source (external `iadopt-lab`) | Content |
|---|---|---|
| `backend/data/measured/matrix-decomposition-v1.txt` | `iadopt-lab/prompts/matrix-decomposition-v1.txt` | Template with `{{schema}}`, `{{demonstrations}}`, `{{target_definition}}` |
| `backend/data/measured/lexical-decomposition.schema.json` | `iadopt-lab/schemas/lexical-decomposition.schema.json` | The six-field schema embedded in the prompt |
| `backend/data/measured/demonstrations-top25.json` | derived from `best-configuration-session.md` §4 | JSON array of exactly 25 demonstration objects |

Environment variables, read by `Settings`:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `PSNC_MODEL_NAME` | str | `Qwen3.8-27B` | **changed** from `Qwen3.5-397B-A17B` |
| `TOP_P` | float | `1.0` | new |
| `MAX_TOKENS` | int | `16000` | new |
| `USE_MEASURED_CONFIGURATION` | bool | `True` | new; `env_bool` truthiness (`{1,true,yes,on}`) |
| `TEMPERATURE` | float | `0.5` | unchanged, already correct |

## 3. Outputs

Four new `Settings` members:

- `settings.top_p: float`
- `settings.max_tokens: int`
- `settings.use_measured_configuration: bool`
- `settings.measured_dir: pathlib.Path` → `<data_dir>/measured`

and three path properties derived from `measured_dir`:
`measured_template_path`, `measured_lexical_schema_path`, `measured_demonstrations_path`.

`DEFAULT_PSNC_MODEL_NAME` becomes `"Qwen3.8-27B"`, and `"Qwen3.8-27B"` is added to
`DEFAULT_PSNC_MODEL_NAMES` as its first entry. The existing
`Qwen3.5-397B-A17B` and `Qwen3-VL-235B-A22B-Instruct-FP8` entries remain, so the
previously-default model stays selectable.

No files written. No network.

## 4. Processing and invariants

There is no processing. The invariants are about the artifacts:

- The demonstrations file parses to a list of **exactly 25** objects.
- Each object has exactly the keys `demonstration`, `definition`, `decomposition`.
- The `demonstration` values are the integers 1…25, in ascending order, matching
  array position.
- The template contains each of the three placeholders **exactly once**.
- `settings.psnc_model_names` contains `Qwen3.8-27B`, and it is first.

These are asserted by tests, not enforced at runtime — a broken artifact is a
broken deployment, and the tests are what catch it before deploy.

## 5. State and side effects

None. `Settings` is constructed once at import (`settings` singleton, already
existing behaviour). Artifact files are read by `measured-prompt`, not here.

## 6. Failures

- A missing or unparseable artifact file → **raised** by whichever module reads it
  (`measured-prompt`). This module does not read them, so it cannot fail.
- `TOP_P` / `MAX_TOKENS` not coercible to float/int → **raised** by pydantic at
  import, as `ValidationError`. Consistent with every other `Settings` field.

## 7. Configuration consumed

**An environment value beats every default here.** `core/config.py` calls
`load_dotenv(ROOT_DIR / ".env")` at import, so a repo-root `.env` naming
`PSNC_MODEL_NAME` silently overrides the measured default, and a `PSNC_MODEL_NAMES`
that omits `Qwen3.8-27B` makes the measured model unreachable even by explicit
request. Changing the constant in this module is necessary but not sufficient;
`.env.example` and `docker-compose.portainer.yml` carry the same values and are
updated with it. See D-010.

Exactly the five keys in section 2. `USE_MEASURED_CONFIGURATION` uses the same
`_coerce_env_bool` validator as `IADOPT_AUTH_ENABLED` and `IADOPT_COOKIE_SECURE` —
**not** the stricter `enable_wikidata_linking` rule, which accepts only the literal
string `"true"`. New flags follow the common case.

## 8. Public interface

No functions. The interface is `Settings` fields and properties:

### `settings.measured_dir -> pathlib.Path`
- **Output:** `BASE_DIR / "data" / "measured"`. Existence is not checked.
- **Determinism:** fully deterministic.

### `settings.measured_template_path -> pathlib.Path`
### `settings.measured_lexical_schema_path -> pathlib.Path`
### `settings.measured_demonstrations_path -> pathlib.Path`
- **Output:** `measured_dir` joined with the filenames in section 2.
- **Side effects:** none.

## 9. Acceptance tests

1. `settings.top_p == 1.0`, `settings.max_tokens == 16000`,
   `settings.temperature == 0.5` with a clean environment.
2. `settings.use_measured_configuration` is `True` by default, and `"0"`, `"false"`,
   `"no"`, `"off"` each make it `False`.
3. `settings.psnc_model_name == "Qwen3.8-27B"`, and it is the first element of
   `settings.psnc_model_names`.
4. `Qwen3.5-397B-A17B` is still present in `settings.psnc_model_names` — the
   previous default did not become unreachable.
5. All three artifact paths exist on disk.
6. The demonstrations file parses to a list of exactly 25 objects, each with exactly
   the keys `{demonstration, definition, decomposition}`.
7. The `demonstration` values are `1..25` in order.
8. The template contains `{{schema}}`, `{{demonstrations}}` and
   `{{target_definition}}` exactly once each.
9. The lexical schema file is byte-identical to
   `iadopt-lab/schemas/lexical-decomposition.schema.json` when that path is
   available; the assertion is skipped, not failed, when it is not.
10. `list_prompt_versions(settings.prompt_dir)` does **not** contain
    `matrix-decomposition-v1` — the measured template is invisible to the legacy path.
