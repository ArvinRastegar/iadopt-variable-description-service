# iAdopt Variable Description Service — Contracts (Phase 0 Baseline)

> **Status:** Phase 0 baseline captured 2026-06-24; **Phase 1 (Pydantic contracts)
> applied** on branch `refactor/phase-1-pydantic-contracts`. This document is the
> frozen behavioral contract the refactor is checked against. Every endpoint and
> major internal boundary lists its **input shape, output shape, and side effects**.
> Regression fixtures live under [`backend/tests/contract/`](../backend/tests/contract/)
> and must stay green at every later gate.

> **Phase 2 changes (no runtime behavior change — Gate 2 verified):** the
> 2,913-line `main.py` monolith is decomposed into the layered structure below.
> `main.py` is now an ~80-line app factory (`create_app()`). Layers, all behavior-
> preserving (move, not rewrite), each verified against the contract suite:
> - `core/`: `config.py` (typed `Settings`), `text.py`, `state.py`, `dependencies.py`
>   (auth_store + deps + middleware), `logging.py`.
> - `clients/`: `http.py`, `openai_client.py`, `psnc_client.py`.
> - `services/`: `orcid.py`, `prompts.py`, `validation.py`, `rdf_ttl.py`, `llm.py`,
>   `reranker.py`, `enrichment.py`, `nanopub_service.py`.
> - `pipeline.py`: orchestration + startup warmup.
> - `routers/`: `auth.py`, `system.py`, `admin.py`, `decompose.py`, `nanopub.py`
>   (one `APIRouter` each, mounted by the factory in original order).
> Dependency direction is acyclic: `routers → services → clients → core/schemas`.
> `mypy` is configured strict on `core`/`clients`/`services`/`routers`/`pipeline`/
> `schemas` (`backend/mypy.ini`) and passes on all 34 app modules. New dep:
> `pydantic-settings==2.14.2` (pinned). **Gate 2:** 45 backend tests pass including
> 25 live contract tests (real LLM decompose + NDJSON stream); RDF/TTL goldens
> byte-identical; OpenAPI unchanged (18 paths / 25 schemas).

> **Phase 1 changes (no runtime behavior change to existing clients):**
> - All request/response models moved from inline `main.py` definitions into the
>   [`app/schemas/`](../backend/app/schemas/) package (single source of truth);
>   `main.py` shrank ~100 lines. Domain models (`Prediction`, `EnrichedPrediction`,
>   constraint/system parts) and the SSE `StreamEvent` discriminated union are now
>   defined there as the typed contract for Phase 2/3.
> - Provider config moved to the [`app/core/config.py`](../backend/app/core/config.py)
>   leaf (seed for the Phase-2 `Settings`); `main.py` imports it (no divergence).
> - Every functional route now declares `response_model=` and is visible in OpenAPI.
> - **Intended change:** `/api/openapi.json` now documents **18 functional paths /
>   25 component schemas** (was 6 paths / 10 schemas). `docs`/`redoc`/`openapi.json`
>   remain `include_in_schema=False` by design. Response *bodies* are byte-identical
>   (verified: key order preserved on every dict-returning route; all contract
>   fixtures green, including live LLM decompose + stream).
> - Type safety: `mypy --strict` passes on `app.schemas` + `app.core` (`backend/mypy.ini`).
>
> **Golden rule:** behavior is frozen. Same routes, request/response JSON, RDF/TTL
> output, auth flow, and rendered UI. Any intended behavior change is called out
> explicitly, never bundled into the refactor.

---

## 0. How the baseline was captured

| Aspect | Value |
| --- | --- |
| Runtime | `docker compose up` — backend (uvicorn) internal `8000`, frontend nginx published on host **`5173`** → container `8080` |
| Capture path | Through nginx at `http://localhost:5173/api/...` (the real path the frontend uses) |
| Auth | Enabled (`IADOPT_AUTH_ENABLED=true`); bootstrap admin login (local username/password) |
| LLM provider exercised | `.env` default → **`psnc`** (PSNC/PCSS `Qwen3.5-397B-A17B`) |
| Nanopub publish/retract | **NOT exercised live** (irreversible registry writes). Success shapes documented from code; only 401/422 error paths captured live. |
| PII | Real emails in `/auth/me`, `/admin/users`, `/admin/audit` redacted to `REDACTED@example.org` in fixtures |

### Ground-truth corrections (verified against source — differ from the original brief)

These were stated in the refactor brief but are **wrong** about the current code.
Flagging per the brief's "confirm before editing" rule:

1. **Auth is NOT JWT and NOT ORCID-based.** [`auth.py`](../backend/app/auth.py) uses
   **local username/password** (PBKDF2-SHA256, 390k iterations) with **opaque random
   session tokens** (`secrets.token_urlsafe(32)`) **HMAC-SHA256-signed** with
   `IADOPT_SESSION_SECRET` and stored hashed in SQLite. No `jwt`/`jose`/`PyJWT`
   anywhere. ORCID appears **only** in `main.py` for nanopub/TTL provenance, never in auth.
2. **`private.pem` / `public.pem` are not referenced by any code.** Nanopub signing
   uses the `NANOPUB_PRIVATE_KEY` / `NANOPUB_PUBLIC_KEY` **env vars**. The PEM files at
   repo root appear legacy/unused (both are correctly gitignored).
3. **Counts:** `main.py` has **22** `@app` route decorators (brief said 21) and **117**
   `def` (brief said 108 — the extra are nested closures, esp. inside `json_to_ttl_repo_style`).
   `main.py` reads **37** env vars directly; the rest are in `auth.py`/derived. 12 `BaseModel`
   classes and `response_model=` on **6** routes (the 5 non-`include_in_schema=False` ones plus `/decompose`).
4. **A stray local `.env` entry `Barbara'sPassword=...`** (invalid env-var name, apostrophe)
   broke `docker compose` parsing. It is unreferenced in code; commented out locally so the
   stack can boot. `.env` is gitignored — not committed.

### Determinism note (critical for golden tests)

`json_to_ttl_repo_style` is **not** naturally byte-reproducible: `_make_variable_identity()`
([main.py:1589](../backend/app/main.py#L1589)) reads `datetime.now()` **and** `random.randint(0,99)`,
which flow into the variable URI, `dct:identifier`, and `dct:created`. Golden tests freeze the
clock + RNG and stub the ORCID name lookup; with those frozen, output is verified identical across runs.

---

## 1. HTTP API — all 22 routes (prefix `/api`)

Auth model: a global HTTP middleware ([main.py:264](../backend/app/main.py#L264)) requires a valid
session for any `/api/*` path except the public set `{auth/login, auth/verify, livez, readyz, health}`.
`require_current_user` / `require_admin` are the per-route dependencies. When `IADOPT_AUTH_ENABLED=false`
a synthetic development admin user is injected.

### 1.1 Decomposition

#### `POST /api/decompose/stream` — [main.py:2588](../backend/app/main.py#L2588)
- **Auth:** user. **In:** `DecomposeRequest`. **Out:** `200` NDJSON (`application/x-ndjson`).
- **Stream events** (one JSON object per line):
  - `{"type":"raw_delta","delta":"<str>"}` — incremental raw model output (N events).
  - `{"type":"final","data":<DecomposeResponse>}` — exactly one, terminal on success.
  - `{"type":"error","detail":"<str>"}` — terminal on failure.
  - Observed baseline: 151 `raw_delta` + 1 `final`.
- **Side effects:** LLM (PSNC or OpenRouter), PSNC reranker + Wikidata (enrichment), ORCID lookup (creator name), SQLite audit (`decompose.stream`).
- **Errors:** `401` unauth; `422` validation; in-stream `error` event (status still 200) for pipeline failure.

#### `POST /api/decompose` — [main.py:2665](../backend/app/main.py#L2665)
- **Auth:** user. **In:** `DecomposeRequest`. **Out:** `200` `DecomposeResponse` (one JSON object).
- **Side effects:** same as stream. **Errors:** `400` (ValueError), `500` (RuntimeError/unexpected), `401`, `422`.

`DecomposeRequest`: `definition: str(≥1)`, `model_name?: str`, `model_provider: str = "psnc"`, `creator_orcid_id?: str`, `disable_thinking: bool = true`.
`DecomposeResponse`: `raw_llm_output: str`, `parsed_json: dict`, `schema_valid: bool`, `validation_errors: str[]`, `enriched_json: dict`, `ttl: str`.

#### `GET /api/model-options` — [main.py:2548](../backend/app/main.py#L2548)
- **Auth:** user. **Out:** `ModelOptionsResponse` = `{default_model_provider, default_model_name, model_names[], providers{provider:{label,default_model_name,model_names[]}}}`. **Side effects:** none (reads config).

#### `POST /api/events` — [main.py:2441](../backend/app/main.py#L2441)
- **Auth:** user. **In:** `FrontendEventRequest` = `{action: str(1..120), payload?: dict, metadata?: dict}`. **Out:** `{"status":"ok"}`. **Side effects:** SQLite audit (`frontend.<action>`).

### 1.2 Nanopub

#### `GET /api/nanopub/preparation-options` — [main.py:2574](../backend/app/main.py#L2574)
- **Auth:** user. **Out:** `NanopubPreparationOptionsResponse` = `{default_creator_orcid_id?, conforms_to_uri, created_with_label}`. **Side effects:** none.

#### `POST /api/nanopub/publish` — [main.py:2744](../backend/app/main.py#L2744)  ⚠️ irreversible
- **Auth:** user. **In:** `PublishNanopubRequest` = `{ttl: str(≥1), creator_orcid_id?: str}`.
- **Out (from code):** `PublishNanopubResponse` = `{nanopub_url, published_to, variable_identifier, variable_uri}`.
- **Side effects:** parse TTL (rdflib), ORCID lookup, **publish to public nanopub registry** (`NANOPUB_PUBLISH_SERVER`, default `registry.petapico.org`), SQLite audit. Uses `NANOPUB_PRIVATE_KEY` to sign.
- **Errors:** `400` empty/unparseable TTL; `500` publish failure; `401`; `422` missing/empty `ttl`.

#### `POST /api/nanopub/retract` — [main.py:2856](../backend/app/main.py#L2856)  ⚠️ irreversible
- **Auth:** user. **In:** `RetractNanopubRequest` = `{nanopub_uri: str(≥1), creator_orcid_id?: str}`.
- **Out (from code):** `RetractNanopubResponse` = `{retraction_url, published_to, retracted_nanopub_url}`.
- **Side effects:** load target nanopub, **key-ownership check** (target public key must equal configured key), **publish retraction to registry**, SQLite audit.
- **Errors:** `400` bad URI / key mismatch / load failure; `500` publish failure; `401`; `422`.

### 1.3 Auth (all `include_in_schema=False`)

| Route | Line | Auth | In | Out | Side effects |
| --- | --- | --- | --- | --- | --- |
| `POST /api/auth/login` | [2374](../backend/app/main.py#L2374) | public | `LoginRequest{username,password}` | `{user, auth_enabled}` + sets `iadopt_session` cookie | SQLite (authenticate, create session, audit `auth.login`/`auth.login_failed`) |
| `GET /api/auth/verify` | [2404](../backend/app/main.py#L2404) | public | — | `204` if authed else `401` | SQLite (session lookup). Used by nginx `auth_request`. |
| `GET /api/auth/me` | [2412](../backend/app/main.py#L2412) | user | — | `{user, auth_enabled}` | SQLite |
| `POST /api/auth/logout` | [2417](../backend/app/main.py#L2417) | user | — | `{"status":"ok"}` + clears cookie | SQLite (delete session, audit `auth.logout`) |

`user` public shape: `{id, username, display_name, email, roles[], is_active, auth_provider, external_subject}`.

### 1.4 Admin (all `include_in_schema=False`, require `admin` role)

| Route | Line | In | Out | Side effects |
| --- | --- | --- | --- | --- |
| `GET /api/admin/stats` | [2458](../backend/app/main.py#L2458) | — | stats dict (users, 30d event aggregates, `recent_events[]`, `readiness`) | SQLite |
| `GET /api/admin/audit` | [2469](../backend/app/main.py#L2469) | `?limit=100&offset=0` | `{events: AuditEvent[]}` | SQLite |
| `GET /api/admin/users` | [2478](../backend/app/main.py#L2478) | — | `{users: User[]}` | SQLite |
| `POST /api/admin/users` | [2483](../backend/app/main.py#L2483) | `AdminCreateUserRequest` | `{user}` | SQLite (create + audit). `400` on duplicate/invalid. |
| `PATCH /api/admin/users/{user_id}` | [2504](../backend/app/main.py#L2504) | `AdminUpdateUserRequest` | `{user}` | SQLite (update + audit). `400` invalid / last-admin guard. |

`AdminCreateUserRequest`: `{username(1..120), password(≥8), display_name="", email="", roles=["user"], is_active=true}`.
`AdminUpdateUserRequest`: all of the above optional (partial update via `exclude_unset`).

### 1.5 System (all `include_in_schema=False`)

| Route | Line | Auth | Out |
| --- | --- | --- | --- |
| `GET /api/livez` | [2530](../backend/app/main.py#L2530) | public | `{"status":"ok"}` |
| `GET /api/readyz` | [2535](../backend/app/main.py#L2535) | public | `{"status":"ready","checks":{...}}` or `503` `{status:"not_ready",checks}` |
| `GET /api/health` | [2543](../backend/app/main.py#L2543) | public | `{"status":"ok"}` |
| `GET /api/docs` | [2426](../backend/app/main.py#L2426) | user | Swagger UI HTML |
| `GET /api/redoc` | [2431](../backend/app/main.py#L2431) | user | ReDoc HTML |
| `GET /api/openapi.json` | [2436](../backend/app/main.py#L2436) | user | OpenAPI schema (only 6 routes documented today) |

`readyz` checks (no external calls — local readiness only): `schema_exists, prompt_dir_exists, five_shot_dir_exists, enabled_provider_keys_set, wikidata_reranker_ready`.

### Common error shapes
- `401`: `{"detail":"Authentication required."}` (middleware) / `{"detail":"Invalid username or password."}` (login).
- `403`: `{"detail":"Admin access required."}`.
- `422`: FastAPI `{"detail":[{type, loc, msg, input, ctx?}]}`.
- `400`/`500`: `{"detail":"<message>"}`.

---

## 2. Internal boundaries (refactor targets → Phase 2 modules)

> `Dict[str, Any]` is pervasive across these boundaries; the "real shape" column is
> what Phase 1 Pydantic models must capture.

### Pipeline orchestration → `pipeline.py`
| Function | Line | In → Out | Real shape / notes | Side effects |
| --- | --- | --- | --- | --- |
| `run_pipeline` | [2107](../backend/app/main.py#L2107) | `(definition, …)` → `dict` | output = `DecomposeResponse` shape | LLM, reranker, Wikidata, ORCID |
| `stream_pipeline_events` | [1953](../backend/app/main.py#L1953) | `(definition, …)` → `Iterator[str]` | yields NDJSON event lines | same |
| `_prepare_pipeline_inputs` | [1929](../backend/app/main.py#L1929) | → `(prompt, provider, model)` | resolves provider/model + builds prompt | filesystem (prompt/examples cache) |
| `_finalize_pipeline_output` | [1211](../backend/app/main.py#L1211) | `(raw, pred)` → `dict` | validate → enrich → TTL | reranker, Wikidata, ORCID |

### LLM service → `services/llm.py` + `clients/`
| Function | Line | Notes | Side effects |
| --- | --- | --- | --- |
| `get_openai_client` | [449](../backend/app/main.py#L449) | OpenRouter client (base `openrouter.ai/api/v1`) | — |
| `call_model` | [794](../backend/app/main.py#L794) | OpenRouter chat, 3 retries | OpenRouter HTTP |
| `call_psnc_model` | [919](../backend/app/main.py#L919) | PSNC `/v1/chat/completions`, 3 retries | PSNC HTTP |
| `_stream_psnc_model` | [1136](../backend/app/main.py#L1136) | SSE → `(reasoning, content)` deltas | PSNC HTTP |
| `call_llm_loose` | [1181](../backend/app/main.py#L1181) | call + `parse_llm_json`, 3 retries → `(raw, dict)` | LLM HTTP |
| `parse_llm_json` | [972](../backend/app/main.py#L972) | strip fences, extract `{...}`, `coerce_prediction` | — (raw LLM JSON stays `Dict[str,Any]` — genuinely free-form) |
| `build_prompt` / `load_prompt_instructions` / `load_examples` / `format_example_block` | [770](../backend/app/main.py#L770) | prompt assembly from `data/prompts` + `data/Json_preferred` | filesystem |
| provider/model resolution `_resolve_model_provider` / `_resolve_model_name` | [988](../backend/app/main.py#L988) | validates against enabled providers/allowed models | — |

### Reranker + enrichment → `services/reranker.py`, `services/enrichment.py`
| Function | Line | Notes | Side effects |
| --- | --- | --- | --- |
| `call_psnc_reranker` | [870](../backend/app/main.py#L870) | `/v1/rerank`, returns one score per doc | PSNC HTTP |
| `get_wikidata_entity_reranker` | [1364](../backend/app/main.py#L1364) | Wikidata search → rerank → best ≥ threshold | Wikidata + PSNC HTTP |
| `enrich_with_uris_reranker` | [1396](../backend/app/main.py#L1396) | adds `*URI` fields to prediction (string + Asym/Sym system parts) | Wikidata + PSNC HTTP |

### Validation → `services/validation.py`
| Function | Line | Notes |
| --- | --- | --- |
| `load_schema` / `_patch_schema_for_pipeline` | [1288](../backend/app/main.py#L1288) | loads `data/Json_schema.json`; relaxes `hasConstraint.minItems` 1→0 |
| `get_schema_validation_errors` | [1294](../backend/app/main.py#L1294) | Draft2020-12; returns formatted error lines (≤30) |
| `_get_constraint_semantic_validation_errors` | [1565](../backend/app/main.py#L1565) | flags `constraint.on` not matching a real target label |

### RDF/TTL → `services/rdf_ttl.py` (deterministic except identity)
| Function | Line | Notes |
| --- | --- | --- |
| `json_to_ttl_repo_style` | [1713](../backend/app/main.py#L1713) | enriched JSON → Turtle string (frontend's exact shape) |
| `_make_variable_identity` | [1589](../backend/app/main.py#L1589) | ⚠️ uses `datetime.now()` + `random.randint` — only non-deterministic source |
| `_build_alt_label` / `_phrase_for_role` / `_normalize_constraint_phrase_for_alt_label` | [1505](../backend/app/main.py#L1505) | label-formula assembly (simple / asym-source-target / asym-num-denom) |
| `wiki_to_entity` / `_ttl_quote` / `_normalize_text` | [1480](../backend/app/main.py#L1480) | URI + literal normalization |

### Nanopub service → `services/nanopub_service.py` ⚠️ security + irreversible
| Function | Line | Notes | Side effects |
| --- | --- | --- | --- |
| `get_nanopub_profile` | [631](../backend/app/main.py#L631) | builds signing `Profile` from `NANOPUB_PRIVATE_KEY`/`PUBLIC_KEY` | **loads private key** |
| `get_nanopub_agent_uri` / `_label` | [664](../backend/app/main.py#L664) | resolves software-agent concept from intro nanopub | nanopub registry fetch |
| `_add_nanopub_metadata` / `_build_retraction_nanopub` | [2312](../backend/app/main.py#L2312) | provenance + pubinfo templates | ORCID lookup |
| `_assert_retraction_allowed` | [2212](../backend/app/main.py#L2212) | key-ownership guard before retract | nanopub registry fetch |
| `_normalize_target_nanopub_uri` / `_extract_variable_uri` / `_extract_variable_identifier` | [2149](../backend/app/main.py#L2149) | parse/normalize for publish + retract | rdflib |

### ORCID + config + clients → `services/orcid.py`, `core/config.py`, `clients/http.py`
| Function | Line | Notes | Side effects |
| --- | --- | --- | --- |
| `_normalize_orcid` / `_orcid_suffix` | [501](../backend/app/main.py#L501) | bare ID ↔ `https://orcid.org/<id>` | — (Phase-1 validator candidates) |
| `_lookup_orcid_display_name` / `_extract_orcid_display_name` | [557](../backend/app/main.py#L557) | content-negotiated ORCID record → display name (cached) | **ORCID HTTP** |
| `_resolve_creator_metadata` | [616](../backend/app/main.py#L616) | `(orcid, name)`; raises if no public name | ORCID HTTP |
| `get_http_session` | [704](../backend/app/main.py#L704) | shared `requests.Session` w/ UA | — |
| `_normalize_nanopub_key` / `_normalize_env_multiline` | [471](../backend/app/main.py#L471) | PEM/base64 key normalization | — (security-sensitive) |

### Auth store → `routers/auth.py` + `services/auth` (security-sensitive)
[`auth.py`](../backend/app/auth.py) `AuthStore` — SQLite at `${IADOPT_STATE_DIR}/iadopt.sqlite3` (compose volume `backend_state`).
- **Tables:** `users` (id, username UNIQUE NOCASE, password_hash, display_name, email, roles JSON, is_active, auth_provider, external_subject, created_at, updated_at, last_login_at); `sessions` (token_hash PK, user_id FK CASCADE, created_at, expires_at, ip_address, user_agent); `audit_events` (id, created_at, user_id, username, action, method, path, status_code, latency_ms, ip_address, user_agent, request_payload, response_payload, metadata, error).
- **Crypto:** `hash_password` PBKDF2-SHA256 390k iters + 16-byte salt → `pbkdf2_sha256$iters$salt$digest`; `verify_password` constant-time. Sessions: random token, HMAC-SHA256 signature (`IADOPT_SESSION_SECRET`), only the SHA-256 of the token stored. Cookie `iadopt_session`, `httponly`, `samesite=lax`, `secure=IADOPT_COOKIE_SECURE`, TTL `IADOPT_SESSION_TTL_HOURS` (12).
- **Methods:** `authenticate`, `create_session`, `user_from_request`, `require_user`, `require_admin`, `create_user`, `list_users`, `update_user` (last-admin guard), `audit_event` (payload truncation at `IADOPT_AUDIT_MAX_PAYLOAD_BYTES`), `get_audit_events`, `stats`, `cleanup_old_audit` (retention `IADOPT_AUDIT_RETENTION_DAYS`).
- **Audit redaction:** create/update user already redact `password` to `[redacted]` before storing.

---

## 3. Configuration surface (→ `core/config.py` typed `Settings`)

Module-level env reads in `main.py` (defaults in parentheses). These become a single
Pydantic `BaseSettings`. Secrets (`*_API_KEY`, `NANOPUB_PRIVATE_KEY`, `IADOPT_SESSION_SECRET`,
`IADOPT_BOOTSTRAP_ADMIN_PASSWORD`) stay env-only.

| Group | Vars |
| --- | --- |
| Providers | `ENABLED_MODEL_PROVIDERS`, `DEFAULT_MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_NAMES`, `PSNC_MODEL_NAME`, `PSNC_MODEL_NAMES`, `TEMPERATURE` (0.5) |
| OpenRouter | `OPENROUTER_API_KEY` |
| PSNC | `PSNC_API_KEY`, `PSNC_API_BASE_URL` (`llm.hpc.psnc.pl`), `PSNC_RERANK_MODEL` (`bge-reranker-v2-m3`) |
| Wikidata | `ENABLE_WIKIDATA_LINKING` (true), `RERANK_THRESHOLD` (0.10) |
| Nanopub | `NANOPUB_PRIVATE_KEY`, `NANOPUB_PUBLIC_KEY`, `NANOPUB_ORCID_ID`, `NANOPUB_AGENT_INTRO_URI`, `NANOPUB_PUBLISH_SERVER` (`registry.petapico.org/np/`), `NANOPUB_LICENSE_URI`, `NANOPUB_WAS_CREATED_AT`, `NANOPUB_TEMPLATE_URI`, `NANOPUB_PROVENANCE_TEMPLATE_URI`, `NANOPUB_PUBINFO_TEMPLATE_URIS`, `NANOPUB_RETRACT_TEMPLATE_URI`, `NANOPUB_RETRACT_PROVENANCE_TEMPLATE_URI`, `NANOPUB_RETRACT_PUBINFO_TEMPLATE_URIS`, `IADOPT_VARIABLE_CONFORMS_TO`, `IADOPT_CREATED_WITH_LABEL` |
| Auth | `IADOPT_AUTH_ENABLED` (false), `IADOPT_STATE_DIR`, `IADOPT_DB_PATH`, `IADOPT_SESSION_SECRET`, `IADOPT_COOKIE_SECURE` (false), `IADOPT_SESSION_TTL_HOURS` (12), `IADOPT_AUDIT_RETENTION_DAYS` (30), `IADOPT_AUDIT_MAX_PAYLOAD_BYTES` (1e6), `IADOPT_BOOTSTRAP_ADMIN_{USERNAME,PASSWORD,DISPLAY_NAME,EMAIL}` |

---

## 4. Frontend ↔ backend contract

- Frontend (`frontend/src/**`, ES modules, vanilla Bootstrap 5.3) calls `/api/...` via `fetch`.
  Dev: Vite proxy → `localhost:8000`. Prod: nginx (`frontend/nginx.conf`) proxies `/api/` → `backend:8000`.
- nginx also gates the SPA: `location /` runs `auth_request /api/auth/verify`, redirecting to `login.html` on 401.
- The frontend's own deterministic transforms (`toTurtle`, `toJSONLD`, `parseJSONLD`,
  `applyPreNanopubSettingsToTurtle`, `extract`, `mergeCurrentTurtle`) are covered by Vitest.
  **Baseline:** node unit project **16/16 pass**; browser (Playwright) project blocked locally only
  because chromium isn't installed (`pnpm exec playwright install` to enable) — not a code failure.
- Phase 4 will generate TS types from the backend OpenAPI so `fetch` calls type against the same models.
  Today OpenAPI documents only 6 of 22 routes (the rest are `include_in_schema=False`); Phase 1 fixes this.

---

## 5. Regression fixtures (run at every gate)

See [`backend/tests/contract/README.md`](../backend/tests/contract/README.md). Two tiers:

- **Golden** (`test_golden_ttl.py`): deterministic TTL + validation, no network. **6/6 pass.**
- **Live** (`test_api_contract.py`): replay shapes against a running stack; gated by env vars.
  **20 pass / 2 skipped** read-only; the 2 LLM tests pass when `IADOPT_CONTRACT_RUN_LLM=1`.
  Nanopub publish/retract **success** paths are never run (irreversible); only 401/422 asserted.
