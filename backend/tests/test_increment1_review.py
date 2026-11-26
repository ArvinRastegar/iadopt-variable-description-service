"""Regression tests for what the review of the increment-1 implementation found (gate G-019).

They add cases to criteria of the approved contracts (G-017) that the approved tests (G-018) do not
reach, and change none of those tests. Temporary files only; nothing reaches a model or a registry.
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List

import previous_image
from increment1_support import BACKEND, ISSUER, MiB, TempDatabaseTestCase, free_space, run_cli

from app import storage
from app.accounts import AccountStore


def run_together(script: str, count: int, *args: str) -> List[str]:
    """Run ``script`` in several processes at once and return what each printed."""
    processes = [
        subprocess.Popen([sys.executable, "-c", script, *args], cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(count)
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        if process.returncode:
            raise AssertionError(f"a process failed: {stderr}")
        outputs.append(stdout.strip())
    return outputs


class StorageReviewTests(TempDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        previous_image.make_de1a7af_file(self.db)

    def files(self) -> Dict[str, str]:
        """Every file beside the database, by content; the shared-memory index is SQLite's scratch space."""
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.dir.iterdir() if path.is_file() and not path.name.endswith("-shm")
        }

    def backups(self) -> List[str]:
        folder = self.dir / "backups"
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    def older_backups(self, *names: str) -> None:
        (self.dir / "backups").mkdir(exist_ok=True)
        for name in names:
            shutil.copyfile(self.db, self.dir / "backups" / name)

    def test_reclaim_beside_a_long_reader_holds_writers_up_only_briefly(self):
        """Covers: AC-app-storage-016 (section 6: safe while the backend runs)."""
        storage.migrate(self.db)
        done = threading.Event()
        opened = threading.Event()

        def read() -> None:
            with storage.snapshot(path=self.db) as conn:
                conn.execute("SELECT COUNT(*) FROM users").fetchone()
                opened.set()
                done.wait(60)

        reader = threading.Thread(target=read)
        reader.start()
        opened.wait(10)
        with storage.transaction(immediate=True, path=self.db) as conn:  # the reader's snapshot is old now
            conn.execute("INSERT INTO audit_events (created_at, action) VALUES ('t', 'newer')")
        reclaiming = threading.Thread(target=storage.reclaim_space, args=(self.db,))
        reclaiming.start()
        time.sleep(0.2)
        started = time.monotonic()
        try:
            with storage.transaction(immediate=True, path=self.db) as conn:
                conn.execute("INSERT INTO audit_events (created_at, action) VALUES ('t', 'live')")
            self.assertLess(time.monotonic() - started, 10)
        finally:
            done.set()
            reader.join()
            reclaiming.join()

    def test_refused_starts_keep_the_good_backups(self):
        """Covers: AC-app-storage-007, AC-app-storage-014 (a refused file never replaces good backups)."""
        kept = ["iadopt-20260101T000000Z-pre-migration.sqlite3", "iadopt-20260201T000000Z-pre-migration.sqlite3"]
        self.older_backups(*kept)
        self.execute("DROP TRIGGER record_events_no_delete")
        for _ in range(3):
            with self.assertRaises(storage.SchemaMismatch):
                storage.migrate(self.db)
        self.assertEqual(self.backups(), kept)

    def test_the_leftover_of_an_interrupted_backup_does_not_stop_a_start(self):
        """Covers: AC-app-storage-013 (its space counts and it goes before the copy)."""
        (self.dir / "backups").mkdir()
        (self.dir / "backups" / ".0123abcd.partial").write_bytes(b"x" * 8 * MiB)
        with free_space(2 * self.db.stat().st_size + storage.SPACE_RESERVE_BYTES - 4 * MiB):
            storage.migrate(self.db)
        [made] = self.backups()
        self.assertRegex(made, r"^iadopt-\d{8}T\d{6}Z-pre-migration\.sqlite3$")

    def test_a_wal_left_behind_is_untouched_by_reads_and_refusals(self):
        """Covers: AC-app-storage-008, AC-app-storage-013, AC-app-storage-015 (nothing is written)."""
        storage.migrate(self.db)
        crash = (
            "import os, sqlite3, sys\n"
            "conn = sqlite3.connect(sys.argv[1])\n"
            "conn.execute(\"INSERT INTO audit_events (created_at, action) VALUES ('t', 'crash')\")\n"
            "conn.commit()\n"
            "os._exit(0)\n"
        )
        subprocess.run([sys.executable, "-c", crash, str(self.db)], check=True)
        files = self.files()
        self.assertIn("iadopt.sqlite3-wal", files)

        storage.status(self.db)
        self.assertEqual(self.files(), files)
        self.assertEqual(storage.migrate(self.db), [])
        self.assertEqual(self.files(), files)
        storage.MIGRATIONS.append(storage.Migration(id="0900_later", kind="sql", sql="CREATE TABLE later (x INTEGER)"))
        with free_space(1), self.assertRaises(storage.InsufficientSpace):
            storage.migrate(self.db)
        self.assertEqual(self.files(), files)
        storage.MIGRATIONS[0] = storage.Migration(id="0001_baseline", kind="sql", sql="another text")
        with self.assertRaises(storage.MigrationChanged):
            storage.migrate(self.db)
        self.assertEqual(self.files(), files)

    def test_backups_of_several_processes_never_replace_each_other(self):
        """Covers: AC-app-storage-014 (manual backups are never removed, also within one second)."""
        script = "import pathlib, sys\nfrom app import storage\nprint(storage.backup('manual', path=pathlib.Path(sys.argv[1])).name)\n"
        made = run_together(script, 3, str(self.db))
        self.assertEqual(sorted(made), self.backups())

    def test_several_processes_migrating_at_once_make_one_backup(self):
        """Covers: AC-app-storage-006, AC-app-storage-014."""
        script = "import pathlib, sys\nfrom app import storage\nprint(len(storage.migrate(pathlib.Path(sys.argv[1]))))\n"
        applied = run_together(script, 3, str(self.db))
        self.assertEqual(sorted(applied), ["0", "0", str(len(storage.MIGRATIONS))])
        self.assertEqual(len(self.backups()), 1, self.backups())


class AccountsReviewTests(TempDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.init()
        self.bob = self.store.create_user(username="bob", password="password-123")
        self.carol = self.store.create_user(username="carol", password="password-123")
        self.accounts = AccountStore(self.store)

    def data(self) -> List[Any]:
        return [self.rows(f"SELECT * FROM {table}") for table in ("users", "account_identities", "account_history", "owner_binding")]

    def test_the_cutover_commands_need_a_bound_owner(self):
        """Covers: AC-accounts-roles-006, AC-accounts-roles-012 to -014 (no admin is lost before there is an owner)."""
        provider, _ = self.accounts.ensure_account(ISSUER, "subject", {})
        before = self.data()
        for argv in (
            ("legacy", "link", "--account", "1", "--issuer", ISSUER, "--subject", "admin-subject", "--reason", "x"),
            ("legacy", "fold", "--from", str(provider["id"]), "--into", "1", "--reason", "x"),
            ("legacy", "purge-passwords", "--confirm-cutover"),
        ):
            with self.subTest(command=argv[1]):
                self.assertEqual(run_cli(*argv)[0], 1)
        self.assertEqual(self.data(), before)

    def test_an_admin_who_loses_the_role_while_a_request_waits_changes_nothing(self):
        """Covers: AC-accounts-roles-003, AC-accounts-roles-009 (the actor is read in the transaction too)."""
        requests = {
            "block": ("POST", f"/api/admin/users/{self.bob['id']}/block", {"reason": "A reason."}),
            "reset": ("PATCH", f"/api/admin/users/{self.bob['id']}", {"password": "new-password-123"}),
            "create": ("POST", "/api/admin/users", {"username": "mallory", "password": "password-123"}),
        }
        for change, reason in (("roles = '[\"user\"]'", "admin_required"), ("is_active = 0", "blocked")):
            for name, (method, path, body) in requests.items():
                with self.subTest(request=name, reason=reason):
                    self.execute("UPDATE users SET roles = '[\"admin\", \"user\"]', is_active = 1 WHERE username = 'carol'")
                    carol = self.client()
                    self.login(carol, "carol")
                    before = self.rows("SELECT id, username, password_hash, is_active FROM users WHERE username <> 'carol'")
                    holder = sqlite3.connect(self.db, isolation_level=None)
                    holder.execute("BEGIN IMMEDIATE")
                    answers: List[Any] = []
                    waiting = threading.Thread(target=lambda: answers.append(carol.request(method, path, json=body)))
                    waiting.start()
                    time.sleep(0.8)  # the request passed its capability and now waits for the write lock
                    holder.execute(f"UPDATE users SET {change} WHERE username = 'carol'")
                    holder.execute("COMMIT")
                    holder.close()
                    waiting.join()
                    self.assertEqual((answers[0].status_code, answers[0].json()["reason"]), (403, reason))
                    self.assertEqual(
                        self.rows("SELECT id, username, password_hash, is_active FROM users WHERE username <> 'carol'"), before
                    )

    def test_an_issuer_with_spaces_is_no_issuer(self):
        """Covers: AC-accounts-roles-010, AC-accounts-roles-012 (a mistyped link would never be found again)."""
        for issuer in (f"{ISSUER} ", f" {ISSUER}", f"{ISSUER}\n", "https://login.example.org/ realm"):
            with self.subTest(issuer=issuer):
                with self.assertRaises(ValueError):
                    self.accounts.ensure_account(issuer, "subject", {})
        self.assertEqual(run_cli("owner", "bind", "--username", "admin")[0], 0)
        code, out = run_cli("legacy", "link", "--account", str(self.bob["id"]), "--issuer", f" {ISSUER} ",
                            "--subject", " bob-subject ", "--reason", "Pasted with spaces.")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.accounts.account_for_identity(ISSUER, "bob-subject")["id"], self.bob["id"])

    def test_an_id_sqlite_cannot_hold_is_an_unknown_account(self):
        """Covers: AC-accounts-roles-017 (an unknown account answers 404)."""
        admin = self.client()
        self.login(admin, "admin", "admin-password")
        user_id = "9" * 30
        self.assertEqual(admin.get(f"/api/admin/users/{user_id}/history").status_code, 404)
        self.assertEqual(admin.post(f"/api/admin/users/{user_id}/block", json={"reason": "x"}).status_code, 404)
        self.assertEqual(admin.patch(f"/api/admin/users/{user_id}", json={"email": "x@example.org"}).status_code, 404)

    def test_usernames_are_unique_ignoring_case_beyond_ascii(self):
        """Covers: AC-accounts-roles-010 (suffixes, ignoring case)."""
        names = [self.accounts.ensure_account(ISSUER, str(number), {"preferred_username": name})[0]["username"]
                 for number, name in enumerate(("Ünal", "ünal", "ÜNAL"))]
        self.assertEqual(names, ["Ünal", "ünal-2", "ÜNAL-3"])
