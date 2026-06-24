# Phase 0 contract regression suite

This is the behavioral baseline captured **before** the refactor. It must stay
green at every later gate. See [`docs/CONTRACTS.md`](../../../docs/CONTRACTS.md)
for the full contract map.

## Layout

```
contract/
  conftest.py             # fixtures + shape-comparison helper
  test_golden_ttl.py      # deterministic TTL + validation (no network)
  test_api_contract.py    # live shape replay against a running stack (gated)
  api/                    # captured request/response fixtures (PII redacted)
  golden/                 # <case>.input.json / .expected.ttl / .validation.json
```

## Tier 1 — golden transform tests (always runnable, no network)

Pin byte-for-byte TTL output and validation results for representative inputs.
The clock, RNG, and ORCID lookup are frozen (see the determinism note in
`docs/CONTRACTS.md`).

```bash
# From repo root, using the local venv (has the app deps):
cd backend
PYTHONPATH=. ../.venv/bin/python -m pytest tests/contract/test_golden_ttl.py -q

# Or inside the backend container:
docker compose exec -T -w /app -e PYTHONPATH=/app backend \
    python -m pip install -q pytest >/dev/null && \
docker compose exec -T -w /app -e PYTHONPATH=/app backend \
    python -m pytest tests/contract/test_golden_ttl.py -q
```

Expected: **6 passed**.

## Tier 2 — live contract tests (gated by env vars)

Replay captured shapes against a running deployment. Skipped unless
`IADOPT_CONTRACT_BASE_URL` is set.

```bash
cd backend
U=$(grep -E '^IADOPT_BOOTSTRAP_ADMIN_USERNAME=' ../.env | cut -d= -f2-)
P=$(grep -E '^IADOPT_BOOTSTRAP_ADMIN_PASSWORD=' ../.env | cut -d= -f2-)

PYTHONPATH=. \
IADOPT_CONTRACT_BASE_URL=http://localhost:5173 \
IADOPT_CONTRACT_USERNAME="$U" IADOPT_CONTRACT_PASSWORD="$P" \
../.venv/bin/python -m pytest tests/contract/test_api_contract.py -q
```

Expected: **20 passed, 2 skipped** (the 2 skipped make real LLM calls).

To also exercise the real decompose + NDJSON-stream shapes, add
`IADOPT_CONTRACT_RUN_LLM=1` (costs LLM tokens):

```bash
IADOPT_CONTRACT_RUN_LLM=1 ... -m pytest tests/contract/test_api_contract.py -q
```

## Regenerating the golden TTL/validation files

If a TTL change is **intended**, regenerate the goldens (clock/RNG frozen) and
review the diff before committing — an unexpected diff means a behavior change.

```bash
# the generator script lives in the PR notes; it freezes datetime.now + random
# and stubs the ORCID name lookup, then writes golden/*.expected.ttl etc.
```

## Safety notes

- **Nanopub publish/retract success paths are never run here** — they write
  irreversibly to the public nanopub registry. Only `401`/`422` error shapes are
  asserted live; success shapes are documented from code in `CONTRACTS.md`.
- Fixtures are **PII-redacted** (real emails → `REDACTED@example.org`).
- No secrets are stored in fixtures; credentials are read from `.env` at runtime only.
