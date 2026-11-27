"""The SQL of the migrations, as literal text (app-storage, section 4).

A migration's checksum is the SHA-256 of its text, so an applied migration's text never changes:
not its layout, not a comment. A schema change is a new migration at the end of
``migrations.MIGRATIONS``, under the additive rule: the previous image (``de1a7af``) must still run
on a migrated file until the owner purges legacy passwords (D-017).
"""

from __future__ import annotations

BASELINE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    roles TEXT NOT NULL DEFAULT '["user"]',
    is_active INTEGER NOT NULL DEFAULT 1,
    auth_provider TEXT NOT NULL DEFAULT 'local',
    external_subject TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip_address TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    status_code INTEGER,
    latency_ms INTEGER,
    ip_address TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    request_payload TEXT,
    response_payload TEXT,
    metadata TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS saved_orcids (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    orcid TEXT NOT NULL,
    name TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (user_id, orcid)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_events(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_events(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action);

CREATE TABLE IF NOT EXISTS record_schema (
    version INTEGER NOT NULL
);

-- One piece of work: who did it, how it started, and the definition they typed.
CREATE TABLE IF NOT EXISTS record_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    origin TEXT NOT NULL CHECK (origin IN ('decompose', 'import')),
    definition TEXT,
    created_at TEXT NOT NULL
);

-- The model run behind a decomposition record: one per record.
CREATE TABLE IF NOT EXISTS record_runs (
    item_id INTEGER PRIMARY KEY REFERENCES record_items(id),
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    disable_thinking INTEGER NOT NULL DEFAULT 1,
    prompt_setup TEXT NOT NULL DEFAULT '',
    prompt_sha256 TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    error TEXT,
    raw_output TEXT,
    parsed_json TEXT,
    schema_valid INTEGER,
    validation_errors TEXT
);

-- Each call to a model inside a run, attempt by attempt.
CREATE TABLE IF NOT EXISTS record_llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES record_runs(item_id),
    purpose TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    status TEXT NOT NULL,
    error TEXT
);

-- Each distinct graph once, as the Turtle it first arrived in, with its core fingerprint.
CREATE TABLE IF NOT EXISTS record_contents (
    sha256 TEXT PRIMARY KEY,
    core_sha256 TEXT NOT NULL,
    turtle TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- Version 0, 1, 2 ... of a record, each pointing at its content and the version before it.
CREATE TABLE IF NOT EXISTS record_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES record_items(id),
    number INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('model_draft', 'imported', 'edited', 'metadata')),
    content_sha256 TEXT NOT NULL REFERENCES record_contents(sha256),
    parent_id INTEGER REFERENCES record_versions(id),
    created_at TEXT NOT NULL,
    UNIQUE (item_id, number)
);

-- Where a step came from: one row per distinct IP address, browser, device and screen.
CREATE TABLE IF NOT EXISTS client_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    ip_address TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    screen TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (user_id, ip_address, user_agent, device, screen)
);

-- The timeline: every step and milestone of a record, in order.
CREATE TABLE IF NOT EXISTS record_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES record_items(id),
    seq INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (
        type IN ('draft', 'import', 'edit', 'metadata', 'check', 'uncheck', 'mark_best', 'publish')
    ),
    version_id INTEGER REFERENCES record_versions(id),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    context_id INTEGER REFERENCES client_contexts(id),
    note TEXT,
    unchecked_components INTEGER,
    nanopub_uri TEXT,
    orcid TEXT,
    UNIQUE (item_id, seq)
);

-- What changed inside one step, component by component.
CREATE TABLE IF NOT EXISTS record_changes (
    event_id INTEGER NOT NULL REFERENCES record_events(id),
    position INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (
        operation IN (
            'add', 'remove', 'relabel', 'relink', 'change_shape',
            'add_constraint', 'move_constraint', 'confirm', 'unconfirm', 'other'
        )
    ),
    role TEXT NOT NULL DEFAULT '',
    component TEXT NOT NULL DEFAULT '',
    before_value TEXT,
    after_value TEXT,
    PRIMARY KEY (event_id, position)
);

CREATE INDEX IF NOT EXISTS idx_record_items_user ON record_items(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_record_versions_content ON record_versions(item_id, content_sha256);
CREATE INDEX IF NOT EXISTS idx_record_events_type ON record_events(item_id, type);
CREATE INDEX IF NOT EXISTS idx_record_events_nanopub ON record_events(nanopub_uri);
CREATE INDEX IF NOT EXISTS idx_record_llm_calls_item ON record_llm_calls(item_id);

CREATE TRIGGER IF NOT EXISTS record_items_no_update BEFORE UPDATE ON record_items
BEGIN SELECT RAISE(ABORT, 'record_items is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_items_no_delete BEFORE DELETE ON record_items
BEGIN SELECT RAISE(ABORT, 'record_items is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_items_no_overwrite BEFORE INSERT ON record_items
WHEN EXISTS (SELECT 1 FROM record_items WHERE id = NEW.id OR public_id = NEW.public_id)
BEGIN SELECT RAISE(ABORT, 'record_items is append-only'); END;

CREATE TRIGGER IF NOT EXISTS record_runs_no_update BEFORE UPDATE ON record_runs
BEGIN SELECT RAISE(ABORT, 'record_runs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_runs_no_delete BEFORE DELETE ON record_runs
BEGIN SELECT RAISE(ABORT, 'record_runs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_runs_no_overwrite BEFORE INSERT ON record_runs
WHEN EXISTS (SELECT 1 FROM record_runs WHERE item_id = NEW.item_id)
BEGIN SELECT RAISE(ABORT, 'record_runs is append-only'); END;

CREATE TRIGGER IF NOT EXISTS record_llm_calls_no_update BEFORE UPDATE ON record_llm_calls
BEGIN SELECT RAISE(ABORT, 'record_llm_calls is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_llm_calls_no_delete BEFORE DELETE ON record_llm_calls
BEGIN SELECT RAISE(ABORT, 'record_llm_calls is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_llm_calls_no_overwrite BEFORE INSERT ON record_llm_calls
WHEN EXISTS (SELECT 1 FROM record_llm_calls WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'record_llm_calls is append-only'); END;

CREATE TRIGGER IF NOT EXISTS record_contents_no_update BEFORE UPDATE ON record_contents
BEGIN SELECT RAISE(ABORT, 'record_contents is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_contents_no_delete BEFORE DELETE ON record_contents
BEGIN SELECT RAISE(ABORT, 'record_contents is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_contents_no_overwrite BEFORE INSERT ON record_contents
WHEN EXISTS (SELECT 1 FROM record_contents WHERE sha256 = NEW.sha256)
BEGIN SELECT RAISE(IGNORE); END;

CREATE TRIGGER IF NOT EXISTS record_versions_no_update BEFORE UPDATE ON record_versions
BEGIN SELECT RAISE(ABORT, 'record_versions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_versions_no_delete BEFORE DELETE ON record_versions
BEGIN SELECT RAISE(ABORT, 'record_versions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_versions_no_overwrite BEFORE INSERT ON record_versions
WHEN EXISTS (SELECT 1 FROM record_versions WHERE id = NEW.id OR (item_id = NEW.item_id AND number = NEW.number))
BEGIN SELECT RAISE(ABORT, 'record_versions is append-only'); END;

CREATE TRIGGER IF NOT EXISTS client_contexts_no_update BEFORE UPDATE ON client_contexts
BEGIN SELECT RAISE(ABORT, 'client_contexts is append-only'); END;
CREATE TRIGGER IF NOT EXISTS client_contexts_no_delete BEFORE DELETE ON client_contexts
BEGIN SELECT RAISE(ABORT, 'client_contexts is append-only'); END;
CREATE TRIGGER IF NOT EXISTS client_contexts_no_overwrite BEFORE INSERT ON client_contexts
WHEN EXISTS (SELECT 1 FROM client_contexts WHERE id = NEW.id OR (user_id = NEW.user_id AND ip_address = NEW.ip_address AND user_agent = NEW.user_agent AND device = NEW.device AND screen = NEW.screen))
BEGIN SELECT RAISE(IGNORE); END;

CREATE TRIGGER IF NOT EXISTS record_events_no_update BEFORE UPDATE ON record_events
BEGIN SELECT RAISE(ABORT, 'record_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_events_no_delete BEFORE DELETE ON record_events
BEGIN SELECT RAISE(ABORT, 'record_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_events_no_overwrite BEFORE INSERT ON record_events
WHEN EXISTS (SELECT 1 FROM record_events WHERE id = NEW.id OR (item_id = NEW.item_id AND seq = NEW.seq))
BEGIN SELECT RAISE(ABORT, 'record_events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS record_changes_no_update BEFORE UPDATE ON record_changes
BEGIN SELECT RAISE(ABORT, 'record_changes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_changes_no_delete BEFORE DELETE ON record_changes
BEGIN SELECT RAISE(ABORT, 'record_changes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS record_changes_no_overwrite BEFORE INSERT ON record_changes
WHEN EXISTS (SELECT 1 FROM record_changes WHERE event_id = NEW.event_id AND position = NEW.position)
BEGIN SELECT RAISE(ABORT, 'record_changes is append-only'); END;

INSERT INTO record_schema (version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM record_schema);
"""
"""``0001_baseline``: the schema the previous image's stores created at every start (section 2).

``users``, ``sessions``, ``audit_events`` and ``saved_orcids`` belong to accounts-roles and the auth
store, the record tables to research-records (D-037). The record tables are append-only: their
triggers refuse ``UPDATE`` and ``DELETE``, and an insert whose key is already stored is refused or,
for the deduplicated ``record_contents`` and ``client_contexts``, dropped.
"""

INCREMENTAL_VACUUM_SQL = """
PRAGMA auto_vacuum = INCREMENTAL;
VACUUM;
"""
"""``0002_incremental_vacuum``: let ``reclaim_space`` return free pages to the file system."""

ACCOUNTS_ROLES_SQL = """
ALTER TABLE users ADD COLUMN email_verified INTEGER;

-- Identity-provider identities: who an account is. Issuer 'local' never appears here.
CREATE TABLE account_identities (
    issuer TEXT NOT NULL CHECK (issuer <> 'local'),
    subject TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    PRIMARY KEY (issuer, subject)
);

-- At most one owner: an identity, or issuer 'local' with the account's decimal id as the subject.
CREATE TABLE owner_binding (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    bound_at TEXT NOT NULL
);

-- Role, block and identity changes of each account. Append-only and never pruned.
CREATE TABLE account_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    actor_user_id INTEGER,
    actor_kind TEXT NOT NULL,
    reason TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_account_identities_user ON account_identities(user_id);
CREATE INDEX idx_account_history_user ON account_history(user_id);

CREATE TRIGGER account_history_no_update BEFORE UPDATE ON account_history
BEGIN SELECT RAISE(ABORT, 'account_history is append-only'); END;
CREATE TRIGGER account_history_no_delete BEFORE DELETE ON account_history
BEGIN SELECT RAISE(ABORT, 'account_history is append-only'); END;
CREATE TRIGGER account_history_no_overwrite BEFORE INSERT ON account_history
WHEN EXISTS (SELECT 1 FROM account_history WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'account_history is append-only'); END;
"""
"""``0003_accounts_roles``: the data of accounts-roles, section 3.

``users.email_verified`` is NULL (unknown) for local accounts and 0 or 1 from the identity provider.
"""

OIDC_IDENTITY_SQL = """
-- Sign-ins on their way to the identity provider and back. Only hashes of the state and the browser's key are kept.
CREATE TABLE oidc_transactions (
    state_hash TEXT PRIMARY KEY,
    browser_key_hash TEXT NOT NULL,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    issuer TEXT NOT NULL,
    redirect TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Sessions an identity provider started. The previous image knows neither the table nor the cookie.
CREATE TABLE oidc_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    sid TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip_address TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_oidc_transactions_expires ON oidc_transactions(expires_at);
CREATE INDEX idx_oidc_sessions_user ON oidc_sessions(user_id);
CREATE INDEX idx_oidc_sessions_subject ON oidc_sessions(issuer, subject);
CREATE INDEX idx_oidc_sessions_sid ON oidc_sessions(issuer, sid);
CREATE INDEX idx_oidc_sessions_expires ON oidc_sessions(expires_at);
"""
"""``0004_oidc_identity``: the data of oidc-identity, section 11."""

DAILY_DECOMPOSITIONS_SQL = """
-- Decompositions counted against the daily limit, one row each, written before a model is called.
-- Without an outcome it is still running. A released one was given back and no longer counts.
CREATE TABLE decomposition_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    berlin_date TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    outcome TEXT,
    released INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_decomposition_uses_account_day ON decomposition_uses(user_id, berlin_date);
"""
"""``0005_daily_decompositions``: the data of the daily limit (a slice of usage-ledger).

``berlin_date`` is the date in Europe/Berlin that the use counts for.
"""

ACCOUNT_APPROVAL_SQL = """
-- When an admin approved an account, and the id of who did; both empty without an approval. The
-- note of each approval and withdrawal is in account_history.
ALTER TABLE users ADD COLUMN approved_at TEXT;
ALTER TABLE users ADD COLUMN approved_by INTEGER;
"""
"""``0006_account_approval``: the approval of accounts that an identity provider signs in."""

RECORD_CHOICES_SQL = """
-- What a run of the best pipeline chose, as JSON: {"examples": {"source": "retrieved" | "fallback", "ids": [...]},
-- "matrix": "none" | "40" ... "45" | null, "answers": {"asked": 3, "kept": 1 | 2 | 3 | null}}. Empty on the legacy
-- path and for runs recorded before.
ALTER TABLE record_runs ADD COLUMN choices TEXT;
"""
"""``0007_record_choices``: what a run chose on the measured path, under app-storage's additive rule."""
