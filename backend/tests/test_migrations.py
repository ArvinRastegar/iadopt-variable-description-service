"""Each schema migration only adds, keeps every row, and leaves the database usable by the previous image.

The previous image is the application before these migrations; its statements are captured in
``fixtures/previous_image``. A rollback to it must keep working on a migrated file. A migration that has been released
must never change its text: a database that ran it then refuses to start (test_app_storage). The migration framework
itself, backups and the whole chain on a fresh and on a filled file are tested in test_app_storage.

A new migration needs a row in ``RELEASED`` and in ``ADDS``; the first test fails until it has both.
"""

from __future__ import annotations

import hashlib
import pathlib
import sqlite3
from typing import Any, Dict, Iterator, List, Set, Tuple

import previous_image
import pytest

from app import storage

RELEASED = {
    "0001_baseline": ("sql", "0392838b9e0433e2a00287ad751644ea2dba3eed257256c924e62b55f10c9501"),
    "0002_incremental_vacuum": ("maintenance", "d20d5e9b5700f31c4c833b027dd435647d5c4d6a9e402aac1370a2a3073aa9fd"),
    "0003_accounts_roles": ("sql", "ce8629018ccd616d2478fca7833b1f895695791201964fecb6e6d25f68544fcc"),
    "0004_oidc_identity": ("sql", "6f3f3f92888aa809feba7bd1888d8943ef34b13cff6ff77f6fece83f7c018519"),
    "0005_daily_decompositions": ("sql", "95143ca9746c13820b668ec9788222996b4c98054c62bb6062b3d2c9d72aa9b6"),
    "0006_account_approval": ("sql", "55ab56b749f8de3758825ac838770fed01e8e36a6f5c627516e7b281da89ef90"),
    "0007_record_choices": ("sql", "201486b65e33d297ad3a74423850643bdad7083090f952d84288af4cc2fad647"),
    "0008_published_variable": ("sql", "bda6096176de5733a0338c53d4873b420f6ff0dceb27153729a405db572f3bcb"),
}
"""Each migration in order, with its kind and the SHA-256 of the text it was released with."""

ADDS: Dict[str, Tuple[Set[str], Dict[str, List[str]]]] = {
    "0004_oidc_identity": ({"oidc_transactions", "oidc_sessions", "idx_oidc_transactions_expires", "idx_oidc_sessions_user",
                            "idx_oidc_sessions_subject", "idx_oidc_sessions_sid", "idx_oidc_sessions_expires"}, {}),
    "0005_daily_decompositions": ({"decomposition_uses", "idx_decomposition_uses_account_day"}, {}),
    "0006_account_approval": (set(), {"users": ["approved_at", "approved_by"]}),
    "0007_record_choices": (set(), {"record_runs": ["choices"]}),
    "0008_published_variable": (set(), {"record_events": ["variable_uri"]}),
}
"""What each migration after the accounts' adds: new tables and indexes, and empty columns at the end of a table."""

BEFORE_THE_TABLE = {"0001_baseline", "0002_incremental_vacuum", "0003_accounts_roles"}
"""The migrations the previous image itself has; 0003's tables, keys and rules are tested in test_accounts_roles."""


def rows(db: pathlib.Path, sql: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def schema(db: pathlib.Path) -> Dict[str, str]:
    return {row["name"]: row["sql"] for row in rows(db, "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def contents(db: pathlib.Path) -> Dict[str, List[Dict[str, Any]]]:
    tables = [row["name"] for row in rows(db, "SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {table: rows(db, f"SELECT rowid, * FROM {table}") for table in tables if table not in ("schema_migrations", "sqlite_sequence")}


@pytest.fixture
def migrations() -> Iterator[List[Any]]:
    """All migrations; the test may cut the list, and it is put back afterwards."""
    saved = list(storage.MIGRATIONS)
    yield saved
    storage.MIGRATIONS[:] = saved


def test_migrations_run_in_order_keep_the_text_they_were_released_with_and_each_is_checked():
    assert [migration.id for migration in storage.MIGRATIONS] == list(RELEASED)
    for migration in storage.MIGRATIONS:
        kind, checksum = RELEASED[migration.id]
        found = (migration.kind, migration.checksum, hashlib.sha256(migration.sql.encode("utf-8")).hexdigest())
        assert found == (kind, checksum, checksum), migration.id
    assert set(RELEASED) - set(ADDS) == BEFORE_THE_TABLE


@pytest.mark.parametrize("migration_id", list(ADDS))
def test_a_migration_only_adds_and_the_previous_image_still_runs(migration_id, migrations, tmp_path):
    index = [migration.id for migration in migrations].index(migration_id)
    new_objects, new_columns = ADDS[migration_id]
    old = tmp_path / "previous.sqlite3"
    previous_image.make_de1a7af_file(old)
    storage.MIGRATIONS[:] = migrations[:index]
    storage.migrate(old)
    schema_before, rows_before = schema(old), contents(old)
    columns_before = {table: rows(old, f"PRAGMA table_info({table})") for table in new_columns}
    for table in new_columns:
        assert rows_before[table], f"{table} holds rows whose new columns can be checked"

    storage.MIGRATIONS[:] = migrations[: index + 1]
    assert storage.migrate(old) == [migration_id]

    schema_after = schema(old)
    assert set(schema_after) - set(schema_before) == new_objects
    assert {name: schema_after[name] for name in schema_before if name not in new_columns} == {
        name: sql for name, sql in schema_before.items() if name not in new_columns}
    rows_after = contents(old)
    for table, columns in new_columns.items():
        info = rows(old, f"PRAGMA table_info({table})")
        assert info[: len(columns_before[table])] == columns_before[table], f"{table}'s existing columns are unchanged"
        added = info[len(columns_before[table]):]
        assert [(column["name"], column["notnull"], column["dflt_value"]) for column in added] == [(name, 0, None) for name in columns]
        for row in rows_after[table]:
            assert [row.pop(column) for column in columns] == [None] * len(columns)
    assert {table: rows_after[table] for table in rows_before} == rows_before
    assert previous_image.start(old) == []
    assert previous_image.replay(old, include=previous_image.is_read) == []
