<!-- Repo-specific rules only; every line must prevent a mistake. Keep it short. Plans, decisions and approvals never go here: they live in the private docs checkout. -->
# Agent instructions

## Git ownership
- Never create commits or push in this project, including amend, merge commits or equivalent automated actions. The user performs all commits and pushes.
- Provide proposed commit messages only; stop before any commit or push, even if a workflow or skill suggests it.

## Verify
Run the commands that cover your change, all of them when unsure. Backend from `backend/` with the project venv (`../.venv/bin/`), frontend from `frontend/`.
- `python -m compileall -q app && python -m pytest tests -q`
- `ruff check --config ruff.toml app` and `python -m mypy --config-file mypy.ini app` (they cover `app/` only)
- `pnpm test:unit --run && pnpm build` (frontend changes)

CI runs compileall, pytest, `pnpm test:unit`, `pnpm build` and the docker builds, but not ruff or mypy, so green CI is not enough for changes under `backend/app/`. Expected skips: the live contract tests, and `test_golden_matches_the_lab_source_document` when no `iadopt-lab` checkout is reachable (`IADOPT_LAB_PATH`); any other skip or failure is new. A golden diff (`backend/tests/contract/golden`) is a behavior change: never edit or regenerate goldens just to turn a test green.

## Never without explicit approval
- Read `.env` or `*.pem` (real LLM keys, the nanopub signing key, the admin password). If the user asks you to inspect one, read only the entries they name and report presence or format, never any part of a value. If a tool blocks the read, do not work around it (scripts, `grep -r`).
- Publish or retract nanopublications (`/api/nanopub/publish`, `/api/nanopub/retract`): they write irreversibly to the public registry, which is the default `NANOPUB_PUBLISH_SERVER`. The README's `NANOPUB_USE_TEST_SERVER` is not read by any code. Verify with tests and mocks.
- Push, merge or dispatch anything that reaches `prod`: it publishes images and triggers the Portainer redeploy, and `pcss-production.yml` deploys on manual dispatch.
