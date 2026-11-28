"""accounts-roles: accounts, identities, the owner, admins and blocks (increment 1, tests gate G-018).

Behavioural tests of ``components/accounts-roles.md`` (approved under G-017) through its public
seams: the admin routes, ``python -m app.cli owner|legacy``, ``AccountStore.ensure_account`` and
``AccountStore.account_for_identity``, startup with the bootstrap settings, and the previous image's
statements captured from ``de1a7af``. Identity-provider accounts use a synthetic issuer; nothing
talks to a real provider, a model or a registry. Admins that exist before an owner does are
arranged in the database as section 3 stores them (``admin`` in ``users.roles``), as legacy data
holds them.
"""

from __future__ import annotations

import csv
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple

import previous_image
from increment1_support import BACKEND, ISSUER, TempDatabaseTestCase, run_cli, sign_in_as, started_app
from test_records import CLIENT, DRAFT

from app import storage
from app.accounts import AccountStore

ORCID = "https://orcid.org/0000-0002-1825-0097"
OTHER_ISSUER = "https://login.example.org/auth/realms/other"
REFUSAL = {
    "owner_protected": "The owner cannot be blocked or lose admin.",
    "own_account": "You cannot block or unblock your own account.",
    "owner_required": "Only the owner can do this.",
    "provider_account": "This account's email and sign-in belong to its identity provider.",
    "target_blocked": "Unblock the account before granting admin.",
}
AUDIT_ACTION = {
    "grant-admin": "admin.admin_granted",
    "revoke-admin": "admin.admin_revoked",
    "block": "admin.user_blocked",
    "unblock": "admin.user_unblocked",
}
HISTORY_ACTION = {"grant-admin": "admin_granted", "revoke-admin": "admin_revoked", "block": "blocked", "unblock": "unblocked"}
LEGACY_FIELDS = ("id", "username", "display_name", "email", "roles", "is_active", "auth_provider", "external_subject")


def refusal(reason: str) -> Dict[str, str]:
    return {"detail": REFUSAL[reason], "reason": reason}


def flattened(value: Any) -> List[Any]:
    """Every scalar inside a JSON value."""
    if isinstance(value, dict):
        return [item for nested in value.values() for item in flattened(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in flattened(nested)]
    return [value]


class AccountsTestCase(TempDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.store.init()  # the bootstrap admin "admin" is account 1
        self.admin = self.account("admin")
        self.bob = self.store.create_user(username="bob", password="password-123", display_name="Bob Example", email="bob@example.org")
        self.carol = self.store.create_user(username="carol", password="password-123", display_name="Carol Example", email="carol@example.org")
        self.accounts = AccountStore(self.store)

    # ----------------------------------------------------------------- arrangement

    def account(self, username: str) -> Dict[str, Any]:
        [row] = self.rows("SELECT * FROM users WHERE username = ?", username)
        row["roles"] = json.loads(row["roles"])
        return row

    def make_admin_in_database(self, username: str) -> Dict[str, Any]:
        """An admin as legacy data holds one (section 3), made before any owner exists."""
        self.execute("UPDATE users SET roles = ? WHERE username = ?", json.dumps(["admin", "user"]), username)
        return self.account(username)

    def create(self, username: str, **fields: Any) -> Dict[str, Any]:
        return self.store.create_user(username=username, password="password-123", **fields)

    def provider_account(self, subject: str, issuer: str = ISSUER, **claims: Any) -> Dict[str, Any]:
        values = {"email": f"{subject}@example.org", "email_verified": True, "preferred_username": subject, **claims}
        account, created = self.accounts.ensure_account(issuer, subject, values)
        self.assertTrue(created)
        return account

    def bind_owner(self, *target: str) -> None:
        code, out = run_cli("owner", "bind", *(target or ("--username", "admin")))
        self.assertEqual(code, 0, out)

    def signed_in(self, username: str, password: str = "password-123", **kwargs: Any) -> Any:
        client = self.client(**kwargs)
        self.login(client, username, password)
        return client

    def owner_client(self) -> Any:
        self.bind_owner("--username", "admin")
        return self.signed_in("admin", "admin-password")

    # ----------------------------------------------------------------- observation

    def history(self, user_id: int) -> List[Dict[str, Any]]:
        return self.rows("SELECT * FROM account_history WHERE user_id = ? ORDER BY id", user_id)

    def all_history(self) -> List[Dict[str, Any]]:
        return self.rows("SELECT * FROM account_history ORDER BY id")

    def audit_actions(self) -> List[str]:
        return [row["action"] for row in self.rows("SELECT action FROM audit_events ORDER BY id")]

    def users_without(self, *columns: str) -> List[Dict[str, Any]]:
        rows = self.rows("SELECT * FROM users ORDER BY id")
        return [{key: value for key, value in row.items() if key not in columns} for row in rows]

    def identities(self) -> List[Tuple[str, str, int]]:
        return [(row["issuer"], row["subject"], row["user_id"]) for row in self.rows("SELECT * FROM account_identities ORDER BY issuer, subject")]

    def act(self, client: Any, action: str, user_id: int, reason: Optional[str] = None) -> Any:
        return client.post(f"/api/admin/users/{user_id}/{action}", json={} if reason is None else {"reason": reason})


# ------------------------------------------------------------------------------------------------
# Section 3: data
# ------------------------------------------------------------------------------------------------


class DataTests(AccountsTestCase):
    def test_the_migrations_add_the_account_tables_and_change_no_existing_row(self):
        """Covers: AC-accounts-roles-001."""
        old = self.dir / "de1a7af.sqlite3"
        previous_image.make_de1a7af_file(old)
        conn = sqlite3.connect(old)
        users_before = [tuple(row) for row in conn.execute("SELECT * FROM users ORDER BY id")]
        conn.close()

        storage.migrate(old)

        conn = sqlite3.connect(old)
        conn.row_factory = sqlite3.Row
        try:
            info = {row["name"]: dict(row) for row in conn.execute("PRAGMA table_info(users)")}
            self.assertIn("email_verified", info)
            self.assertEqual((info["email_verified"]["notnull"], info["email_verified"]["dflt_value"]), (0, None))
            width = len(users_before[0])
            self.assertEqual([tuple(row)[:width] for row in conn.execute("SELECT * FROM users ORDER BY id")], users_before)
            self.assertEqual({row[0] for row in conn.execute("SELECT email_verified FROM users")}, {None})

            def table(name: str) -> Dict[str, Dict[str, Any]]:
                return {row["name"]: dict(row) for row in conn.execute(f"PRAGMA table_info({name})")}

            identities = table("account_identities")
            self.assertLessEqual({"issuer", "subject", "user_id", "created_at", "last_login_at"}, set(identities))
            self.assertEqual((identities["issuer"]["pk"], identities["subject"]["pk"]), (1, 2))
            self.assertLessEqual({"issuer", "subject", "bound_at"}, set(table("owner_binding")))
            history_columns = {"id", "user_id", "action", "actor_user_id", "actor_kind", "reason", "details", "created_at"}
            self.assertLessEqual(history_columns, set(table("account_history")))
            for name in ("account_identities", "account_history"):
                references = {(row["table"], row["from"]) for row in conn.execute(f"PRAGMA foreign_key_list({name})")}
                self.assertIn(("users", "user_id"), references, name)

            conn.execute(
                "INSERT INTO account_history (user_id, action, actor_user_id, actor_kind, reason, details, created_at)"
                " VALUES (1, 'blocked', NULL, 'offline', 'A test row.', '{}', '2026-11-23T10:00:00+00:00')"
            )
            conn.commit()
            stored = [tuple(row) for row in conn.execute("SELECT * FROM account_history")]
            for statement in ("UPDATE account_history SET reason = 'changed'", "DELETE FROM account_history"):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.DatabaseError):
                        conn.execute(statement)
            conn.rollback()
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM account_history")], stored)
        finally:
            conn.close()


# ------------------------------------------------------------------------------------------------
# Section 4: who may change whom
# ------------------------------------------------------------------------------------------------


class GrantAndRevokeTests(AccountsTestCase):
    def test_only_the_owner_grants_and_revokes_admin(self):
        """Covers: AC-accounts-roles-002."""
        self.make_admin_in_database("carol")
        owner = self.owner_client()
        carol = self.signed_in("carol")
        history, audit = self.all_history(), self.audit_actions()

        for action, target in (("grant-admin", self.bob["id"]), ("revoke-admin", self.carol["id"]), ("revoke-admin", self.admin["id"])):
            with self.subTest(actor="admin", action=action, target=target):
                response = self.act(carol, action, target)
                self.assertEqual((response.status_code, response.json()), (403, refusal("owner_required")))
        self.assertEqual(self.account("bob")["roles"], ["user"])
        self.assertIn("admin", self.account("carol")["roles"])
        self.assertEqual((self.all_history(), self.audit_actions()[len(audit):]), (history, []))

        self.assertEqual(self.act(owner, "grant-admin", self.bob["id"]).status_code, 200)
        self.assertIn("admin", self.account("bob")["roles"])
        self.assertEqual(self.act(owner, "revoke-admin", self.bob["id"]).status_code, 200)
        self.assertNotIn("admin", self.account("bob")["roles"])

        response = self.act(owner, "revoke-admin", self.admin["id"])
        self.assertEqual((response.status_code, response.json()), (403, refusal("owner_protected")))
        self.assertIn("admin", self.account("admin")["roles"])


class BlockTests(AccountsTestCase):
    def test_who_may_block_and_unblock_whom(self):
        """Covers: AC-accounts-roles-003 (the rules of section 4; a refusal changes nothing)."""
        self.make_admin_in_database("carol")
        self.create("dave")
        self.make_admin_in_database("dave")
        owner = self.owner_client()
        carol = self.signed_in("carol")
        dave = self.account("dave")

        self.assertEqual(self.act(carol, "block", self.bob["id"], "Posted spam.").status_code, 200)
        self.assertFalse(self.account("bob")["is_active"])
        self.assertEqual(self.act(carol, "unblock", self.bob["id"]).status_code, 200)
        self.assertTrue(self.account("bob")["is_active"])

        for client, action, target, reason in (
            (carol, "block", dave["id"], "owner_required"),
            (carol, "block", self.admin["id"], "owner_protected"),
            (carol, "block", self.carol["id"], "own_account"),
            (owner, "block", self.admin["id"], "owner_protected"),
        ):
            with self.subTest(action=action, target=target):
                response = self.act(client, action, target, "A reason.")
                self.assertEqual((response.status_code, response.json()), (403, refusal(reason)))
        self.assertTrue(all(self.account(name)["is_active"] for name in ("admin", "carol", "dave")))

        self.assertEqual(self.act(owner, "block", dave["id"], "Left the project.").status_code, 200)
        self.assertFalse(self.account("dave")["is_active"])
        response = self.act(carol, "unblock", dave["id"])  # only the owner unblocks an admin
        self.assertEqual((response.status_code, response.json()), (403, refusal("owner_required")))
        self.assertFalse(self.account("dave")["is_active"])
        self.assertEqual(self.act(owner, "unblock", dave["id"]).status_code, 200)
        self.assertTrue(self.account("dave")["is_active"])

    def test_a_block_ends_sessions_and_logins_and_an_unblock_brings_back_the_data(self):
        """Covers: AC-accounts-roles-003 (sessions, login and data)."""
        bob = self.signed_in("bob")
        self.assertEqual(bob.post("/api/auth/me/orcids", json={"orcid": ORCID}).status_code, 200)
        started = bob.post("/api/records", json={"turtle": DRAFT, "client": CLIENT})
        self.assertEqual(started.status_code, 201, started.text)
        record_id = started.json()["record_id"]
        admin = self.signed_in("admin", "admin-password")

        self.assertEqual(self.act(admin, "block", self.bob["id"], "Shared the account.").status_code, 200)
        self.assertEqual(bob.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.rows("SELECT * FROM sessions WHERE user_id = ?", self.bob["id"]), [])
        self.assertEqual(self.client().post("/api/auth/login", json={"username": "bob", "password": "password-123"}).status_code, 401)

        self.assertEqual(self.act(admin, "unblock", self.bob["id"]).status_code, 200)
        bob = self.signed_in("bob")
        self.assertEqual([item["orcid"] for item in bob.get("/api/auth/me/orcids").json()["orcids"]], [ORCID])
        self.assertEqual(bob.get(f"/api/records/{record_id}").status_code, 200)


class HistoryAndAuditTests(AccountsTestCase):
    def test_each_change_writes_one_history_row_and_one_audit_event(self):
        """Covers: AC-accounts-roles-004."""
        owner = self.owner_client()
        owner_id = self.admin["id"]
        for action, reason in (("grant-admin", "Runs the workshop."), ("revoke-admin", None), ("block", "Posted spam."), ("unblock", "Appealed.")):
            with self.subTest(action=action):
                history, audit = self.history(self.bob["id"]), self.audit_actions()
                response = self.act(owner, action, self.bob["id"], reason)
                self.assertEqual(response.status_code, 200, response.text)
                [row] = self.history(self.bob["id"])[len(history):]
                self.assertEqual((row["action"], row["actor_user_id"], row["actor_kind"]), (HISTORY_ACTION[action], owner_id, "account"))
                self.assertEqual(row["reason"] or None, reason)
                self.assertTrue(row["created_at"])
                self.assertEqual(self.audit_actions()[len(audit):], [AUDIT_ACTION[action]])

    def test_a_refused_or_unchanged_action_writes_nothing(self):
        """Covers: AC-accounts-roles-004 (refused and no-op actions)."""
        self.make_admin_in_database("carol")
        owner = self.owner_client()
        carol = self.signed_in("carol")
        self.assertEqual(self.act(owner, "block", self.bob["id"], "Posted spam.").status_code, 200)
        history, audit = self.all_history(), self.audit_actions()
        for client, action, target, status in (
            (owner, "block", self.bob["id"], 200),          # already blocked
            (owner, "unblock", self.carol["id"], 200),      # not blocked
            (owner, "grant-admin", self.carol["id"], 200),  # already an admin
            (owner, "revoke-admin", self.bob["id"], 200),   # not an admin
            (carol, "block", self.admin["id"], 403),
            (carol, "grant-admin", self.bob["id"], 403),
        ):
            with self.subTest(action=action, target=target):
                self.assertEqual(self.act(client, action, target, "A reason.").status_code, status)
        self.assertEqual(self.all_history(), history)
        self.assertEqual(self.audit_actions(), audit)

    def test_the_history_row_and_the_change_commit_together(self):
        """Covers: AC-accounts-roles-004 (in the same transaction as the change)."""
        self.bind_owner("--username", "admin")
        failing = self.client(raise_server_exceptions=False)
        self.login(failing, "admin", "admin-password")
        self.signed_in("bob")
        sessions = self.rows("SELECT * FROM sessions WHERE user_id = ?", self.bob["id"])
        self.execute(
            "CREATE TRIGGER history_unavailable BEFORE INSERT ON account_history"
            " BEGIN SELECT RAISE(ABORT, 'history unavailable'); END"
        )
        audit = self.audit_actions()
        for action, reason in (("block", "Posted spam."), ("grant-admin", None)):
            with self.subTest(action=action):
                self.assertGreaterEqual(self.act(failing, action, self.bob["id"], reason).status_code, 500)
        bob = self.account("bob")
        self.assertEqual((bob["is_active"], bob["roles"]), (1, ["user"]))
        self.assertEqual(self.rows("SELECT * FROM sessions WHERE user_id = ?", self.bob["id"]), sessions)
        self.assertEqual(self.audit_actions(), audit)


class RoleChangeSessionTests(AccountsTestCase):
    def test_a_revoked_admin_continues_as_a_user_and_a_new_admin_is_one_at_once(self):
        """Covers: AC-accounts-roles-005."""
        owner = self.owner_client()
        bob = self.signed_in("bob")
        self.assertEqual(self.act(owner, "grant-admin", self.bob["id"]).status_code, 200)
        self.assertEqual(bob.get("/api/admin/users").status_code, 200)
        self.assertEqual(self.act(owner, "revoke-admin", self.bob["id"]).status_code, 200)
        response = bob.get("/api/admin/users")
        self.assertEqual((response.status_code, response.json()),
                         (403, {"detail": "Admin access required.", "reason": "admin_required"}))
        me = bob.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)  # the session continues
        self.assertEqual(me.json()["user"]["role"], "user")


class NoOwnerTests(AccountsTestCase):
    def test_without_an_owner_admins_keep_their_rights_and_the_set_of_admins_cannot_shrink(self):
        """Covers: AC-accounts-roles-006."""
        self.make_admin_in_database("carol")
        admin, carol = self.signed_in("admin", "admin-password"), self.signed_in("carol")
        self.assertEqual(admin.get("/api/admin/users").status_code, 200)
        self.assertEqual(admin.post("/api/admin/users", json={"username": "erin", "password": "password-123"}).status_code, 200)
        self.assertEqual(self.act(admin, "block", self.bob["id"], "Posted spam.").status_code, 200)

        for client, action, target in (
            (admin, "grant-admin", self.bob["id"]),
            (admin, "revoke-admin", self.carol["id"]),
            (carol, "revoke-admin", self.admin["id"]),
            (admin, "block", self.carol["id"]),
            (carol, "block", self.admin["id"]),
        ):
            with self.subTest(action=action, target=target):
                response = self.act(client, action, target, "A reason.")
                self.assertEqual((response.status_code, response.json()), (403, refusal("owner_required")))
        self.assertTrue(all("admin" in self.account(name)["roles"] and self.account(name)["is_active"] for name in ("admin", "carol")))
        self.assertEqual(self.rows("SELECT * FROM owner_binding"), [])


# ------------------------------------------------------------------------------------------------
# Section 5: the owner
# ------------------------------------------------------------------------------------------------


class OwnerCommandTests(AccountsTestCase):
    def assert_owner(self, account_id: int, issuer: str, subject: str) -> None:
        [binding] = self.rows("SELECT * FROM owner_binding")
        self.assertEqual((binding["issuer"], binding["subject"]), (issuer, subject))
        self.assertTrue(binding["bound_at"])
        account = self.rows("SELECT * FROM users WHERE id = ?", account_id)[0]
        self.assertEqual(account["is_active"], 1)
        self.assertIn("admin", json.loads(account["roles"]))
        client = self.client()
        sign_in_as(client, account_id)
        self.assertEqual(client.get("/api/auth/me").json()["user"]["role"], "owner")

    def test_owner_bind_by_username_account_id_or_identity(self):
        """Covers: AC-accounts-roles-007 (bind; it makes the account an active admin and records it)."""
        self.execute("UPDATE users SET is_active = 0 WHERE username = 'carol'")  # bind also unblocks
        for target in ("username", "account", "identity"):
            with self.subTest(target=target):
                self.use_database(self.dir / f"bind-{target}.sqlite3")
                self.store.init()
                if target == "identity":
                    person = self.provider_account("owner-subject")
                    arguments, issuer, subject = ("--issuer", ISSUER, "--subject", "owner-subject"), ISSUER, "owner-subject"
                else:
                    person = self.create("pat")
                    issuer, subject = "local", str(person["id"])
                    arguments = ("--username", "pat") if target == "username" else ("--account", str(person["id"]))
                audit = self.audit_actions()
                code, out = run_cli("owner", "bind", *arguments)
                self.assertEqual(code, 0, out)
                self.assert_owner(person["id"], issuer, subject)
                [row] = self.history(person["id"])[-1:]
                self.assertEqual((row["action"], row["actor_kind"], row["actor_user_id"]), ("owner_bound", "offline", None))
                self.assertEqual(len(self.audit_actions()) - len(audit), 1)

        # The first database: binding a blocked account makes it active.
        self.use_database(self.db)
        self.bind_owner("--username", "carol")
        self.assert_owner(self.carol["id"], "local", str(self.carol["id"]))

    def test_a_second_bind_is_refused(self):
        """Covers: AC-accounts-roles-007 (at most one owner; refusals change nothing)."""
        self.bind_owner("--username", "admin")
        history, binding = self.all_history(), self.rows("SELECT * FROM owner_binding")
        for arguments in (("--username", "bob"), ("--username", "admin"), ("--account", str(self.bob["id"]))):
            with self.subTest(arguments=arguments):
                code, out = run_cli("owner", "bind", *arguments)
                self.assertEqual(code, 1, out)
        self.assertEqual((self.all_history(), self.rows("SELECT * FROM owner_binding")), (history, binding))
        self.assertNotIn("admin", self.account("bob")["roles"])

    def test_owner_rebind_moves_the_binding_and_records_the_previous_owner(self):
        """Covers: AC-accounts-roles-007 (rebind)."""
        self.bind_owner("--username", "admin")
        binding = self.rows("SELECT * FROM owner_binding")
        code, out = run_cli("owner", "rebind", "--username", "bob")  # --reason is required
        self.assertEqual(code, 2, out)
        self.assertEqual(self.rows("SELECT * FROM owner_binding"), binding)

        code, out = run_cli("owner", "rebind", "--username", "bob", "--reason", "Handing over the project.")
        self.assertEqual(code, 0, out)
        self.assert_owner(self.bob["id"], "local", str(self.bob["id"]))
        self.assertIn("admin", self.account("admin")["roles"])  # the previous owner stays an admin
        [row] = [row for row in self.history(self.bob["id"]) if row["action"] == "owner_rebound"]
        self.assertEqual((row["actor_kind"], row["reason"]), ("offline", "Handing over the project."))
        previous_owner = {self.admin["id"], str(self.admin["id"]), "admin"}
        self.assertTrue(previous_owner & set(flattened(json.loads(row["details"] or "{}"))), row["details"])

    def test_rebind_recovers_an_owner_account_the_previous_image_blocked_or_demoted(self):
        """Covers: AC-accounts-roles-007 (section 5: rebind is the recovery path)."""
        self.bind_owner("--username", "admin")
        self.execute("UPDATE users SET is_active = 0, roles = ? WHERE username = 'admin'", json.dumps(["user"]))
        code, out = run_cli("owner", "rebind", "--username", "admin", "--reason", "Restored after a rollback.")
        self.assertEqual(code, 0, out)
        self.assert_owner(self.admin["id"], "local", str(self.admin["id"]))

    def test_two_binds_at_once_leave_exactly_one_owner(self):
        """Covers: AC-accounts-roles-007 (two commands at once, in two processes)."""
        environment = {**os.environ, "IADOPT_DB_PATH": str(self.db)}
        # Hold the write lock while both start, so that both reach their binding before either can
        # finish it; a check and a write in separate transactions would then both succeed.
        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            processes = [
                subprocess.Popen([sys.executable, "-m", "app.cli", "owner", "bind", "--username", name], cwd=BACKEND,
                                 env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for name in ("bob", "carol")
            ]
            time.sleep(3)
        finally:
            holder.rollback()
            holder.close()
        results = [(process.wait(timeout=120), process.stdout.read() if process.stdout else "") for process in processes]
        self.assertEqual(sorted(code for code, _ in results), [0, 1], results)
        self.assertEqual(len(self.rows("SELECT * FROM owner_binding")), 1)
        self.assertEqual(len([row for row in self.all_history() if row["action"] == "owner_bound"]), 1)

    def test_the_owner_cannot_be_blocked_demoted_or_replaced_through_the_api(self):
        """Covers: AC-accounts-roles-007 (section 5: only offline commands change the owner)."""
        owner = self.owner_client()
        for action in ("block", "revoke-admin"):
            with self.subTest(action=action):
                response = self.act(owner, action, self.admin["id"], "A reason.")
                self.assertEqual((response.status_code, response.json()), (403, refusal("owner_protected")))
        self.assertTrue(self.account("admin")["is_active"])
        self.assertIn("admin", self.account("admin")["roles"])


# ------------------------------------------------------------------------------------------------
# Section 6: the bootstrap admin
# ------------------------------------------------------------------------------------------------


class BootstrapTests(TempDatabaseTestCase):
    def test_an_empty_database_gets_the_bootstrap_admin_with_a_history_row(self):
        """Covers: AC-accounts-roles-008 (empty database)."""
        with started_app() as client:
            self.login(client, "admin", "admin-password")
        [admin] = self.rows("SELECT * FROM users")
        self.assertEqual((admin["username"], json.loads(admin["roles"]), admin["is_active"], admin["auth_provider"]),
                         ("admin", ["admin", "user"], 1, "local"))
        self.assertEqual((admin["display_name"], admin["email"]), ("Admin User", "admin@example.org"))
        [row] = self.rows("SELECT * FROM account_history WHERE user_id = ?", admin["id"])
        self.assertEqual((row["action"], row["actor_kind"], row["actor_user_id"]), ("admin_granted", "bootstrap", None))

    def test_with_accounts_present_the_settings_change_nothing(self):
        """Covers: AC-accounts-roles-008 (a blocked or demoted bootstrap account stays so)."""
        with started_app():
            pass
        self.store.create_user(username="carol", password="password-123")
        code, out = run_cli("owner", "bind", "--username", "carol")
        self.assertEqual(code, 0, out)
        owner = self.client()
        self.login(owner, "carol")
        self.assertEqual(owner.post("/api/admin/users/1/revoke-admin", json={}).status_code, 200)
        self.assertEqual(owner.post("/api/admin/users/1/block", json={"reason": "Bootstrap account retired."}).status_code, 200)
        before = self.rows("SELECT * FROM users WHERE id = 1")

        os.environ["IADOPT_BOOTSTRAP_ADMIN_DISPLAY_NAME"] = "Changed Name"
        os.environ["IADOPT_BOOTSTRAP_ADMIN_EMAIL"] = "changed@example.org"
        with self.assertLogs(level="INFO") as logged:
            with started_app():
                pass
        self.assertEqual(self.rows("SELECT * FROM users WHERE id = 1"), before)
        self.assertEqual(json.loads(before[0]["roles"]), ["user"])
        self.assertEqual(before[0]["is_active"], 0)
        self.assertTrue(any("bootstrap" in line.lower() for line in logged.output), logged.output)


class BootstrapWithoutSettingsTests(TempDatabaseTestCase):
    bootstrap_env = None

    def test_without_accounts_and_settings_startup_fails(self):
        """Covers: AC-accounts-roles-008 (no accounts, no settings)."""
        with self.assertRaises(RuntimeError):
            with started_app():
                self.fail("the application started")


# ------------------------------------------------------------------------------------------------
# Local accounts until the cutover (D-045)
# ------------------------------------------------------------------------------------------------


class LocalAccountTests(AccountsTestCase):
    def test_admins_create_only_ordinary_local_accounts(self):
        """Covers: AC-accounts-roles-009 (creation)."""
        admin = self.signed_in("admin", "admin-password")
        created = admin.post("/api/admin/users", json={"username": "dave", "password": "password-123",
                                                      "display_name": "Dave", "email": "dave@example.org"})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual((created.json()["user"]["role"], created.json()["user"]["roles"]), ("user", ["user"]))
        self.assertEqual(admin.post("/api/admin/users", json={"username": "erin", "password": "password-123",
                                                             "roles": ["user"], "is_active": True}).status_code, 200)
        count = len(self.rows("SELECT * FROM users"))
        for body in ({"roles": ["admin", "user"]}, {"roles": ["admin"]}, {"is_active": False}):
            with self.subTest(body=body):
                response = admin.post("/api/admin/users", json={"username": "frank", "password": "password-123", **body})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.rows("SELECT * FROM users")), count)

    def test_who_may_edit_and_reset_local_accounts(self):
        """Covers: AC-accounts-roles-009 (edits and resets; D-045)."""
        self.make_admin_in_database("carol")
        self.create("dave")
        self.make_admin_in_database("dave")
        owner = self.owner_client()
        carol = self.signed_in("carol")
        dave = self.account("dave")

        edited = carol.patch(f"/api/admin/users/{self.bob['id']}",
                             json={"username": "robert", "display_name": "Robert Example", "email": "robert@example.org"})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual({key: edited.json()["user"][key] for key in ("username", "display_name", "email")},
                         {"username": "robert", "display_name": "Robert Example", "email": "robert@example.org"})
        self.assertEqual(carol.patch(f"/api/admin/users/{self.bob['id']}", json={"password": "new-password-123"}).status_code, 200)
        self.signed_in("robert", "new-password-123")
        self.assertEqual(carol.patch(f"/api/admin/users/{self.carol['id']}", json={"display_name": "Carol C."}).status_code, 200)
        self.assertEqual(carol.patch(f"/api/admin/users/{self.carol['id']}", json={"password": "own-password-123"}).status_code, 200)

        for target in (dave["id"], self.admin["id"]):
            with self.subTest(actor="admin", target=target):
                response = carol.patch(f"/api/admin/users/{target}", json={"email": "changed@example.org"})
                self.assertEqual((response.status_code, response.json()), (403, refusal("owner_required")))
        self.assertEqual(self.account("dave")["email"], "")

        self.assertEqual(owner.patch(f"/api/admin/users/{dave['id']}", json={"email": "dave@example.org"}).status_code, 200)
        self.assertEqual(owner.patch(f"/api/admin/users/{self.carol['id']}", json={"password": "reset-by-owner-1"}).status_code, 200)

    def test_roles_and_is_active_cannot_be_changed_through_an_edit(self):
        """Covers: AC-accounts-roles-009 (roles and is_active in an update)."""
        admin = self.signed_in("admin", "admin-password")
        before = self.account("bob")
        for body, detail in (({"roles": ["admin", "user"]}, "Use grant-admin or revoke-admin."),
                             ({"is_active": False}, "Use block or unblock."),
                             ({"display_name": "Changed", "is_active": False}, "Use block or unblock.")):
            with self.subTest(body=body):
                response = admin.patch(f"/api/admin/users/{self.bob['id']}", json=body)
                self.assertEqual((response.status_code, response.json()["detail"]), (400, detail))
        self.assertEqual(self.account("bob"), before)

    def test_identity_provider_accounts_are_edited_only_by_their_provider(self):
        """Covers: AC-accounts-roles-009, AC-accounts-roles-019 (provider_account)."""
        account = self.provider_account("subject-edit")
        owner = self.owner_client()
        before = self.rows("SELECT * FROM users WHERE id = ?", account["id"])
        for body in ({"email": "elsewhere@example.org"}, {"password": "new-password-123"}, {"display_name": "Changed"},
                     {"username": "changed"}):
            with self.subTest(body=body):
                response = owner.patch(f"/api/admin/users/{account['id']}", json=body)
                self.assertEqual((response.status_code, response.json()), (400, refusal("provider_account")))
        self.assertEqual(self.rows("SELECT * FROM users WHERE id = ?", account["id"]), before)


# ------------------------------------------------------------------------------------------------
# Section 7: identity-provider accounts
# ------------------------------------------------------------------------------------------------


class EnsureAccountTests(AccountsTestCase):
    def test_a_new_identity_gets_one_ordinary_account_from_its_claims(self):
        """Covers: AC-accounts-roles-010 (creation; role claims ignored)."""
        claims = {
            "email": " ada@example.org ", "email_verified": True, "name": " Ada Lovelace ", "preferred_username": "ada",
            "roles": ["admin"], "groups": ["/admins"], "realm_access": {"roles": ["admin", "owner"]},
        }
        account, created = self.accounts.ensure_account(ISSUER, "subject-ada", claims)
        self.assertTrue(created)
        self.assertEqual((account["username"], account["email"], account["display_name"], account["email_verified"]),
                         ("ada", "ada@example.org", "Ada Lovelace", True))
        self.assertEqual((account["roles"], account["is_active"]), (["user"], True))
        [row] = self.rows("SELECT * FROM users WHERE id = ?", account["id"])
        self.assertEqual((row["auth_provider"], row["external_subject"], row["password_hash"], row["email_verified"]),
                         ("oidc", "subject-ada", "!", 1))
        [identity] = self.rows("SELECT * FROM account_identities")
        self.assertEqual((identity["issuer"], identity["subject"], identity["user_id"]), (ISSUER, "subject-ada", account["id"]))
        self.assertTrue(identity["created_at"] and identity["last_login_at"])

        again, created = self.accounts.ensure_account(ISSUER, "subject-ada", claims)
        self.assertEqual((again["id"], created), (account["id"], False))
        self.assertEqual(len(self.rows("SELECT * FROM users")), 4)
        self.assertEqual(len(self.rows("SELECT * FROM account_identities")), 1)
        self.assertEqual(self.client().post("/api/auth/login", json={"username": "ada", "password": "!"}).status_code, 401)

    def test_a_later_login_refreshes_the_claims_but_never_the_role_or_the_block(self):
        """Covers: AC-accounts-roles-010 (refresh), section 7 (a blocked account is returned blocked)."""
        account = self.provider_account("subject-refresh", name="Ada Lovelace")
        self.execute("UPDATE users SET roles = ?, is_active = 0 WHERE id = ?", json.dumps(["admin", "user"]), account["id"])
        # An old last login on both, so the refresh shows whatever the stored precision.
        old = "2020-01-01T00:00:00+00:00"
        self.execute("UPDATE users SET last_login_at = ? WHERE id = ?", old, account["id"])
        self.execute("UPDATE account_identities SET last_login_at = ?", old)

        refreshed, created = self.accounts.ensure_account(ISSUER, "subject-refresh", {
            "email": "new@example.org", "email_verified": "true", "given_name": "Ada", "family_name": "King",
            "preferred_username": "someone-else", "roles": ["user"],
        })
        self.assertFalse(created)
        self.assertEqual((refreshed["email"], refreshed["email_verified"], refreshed["display_name"]), ("new@example.org", False, "Ada King"))
        self.assertEqual((refreshed["roles"], refreshed["is_active"]), (["admin", "user"], False))
        self.assertEqual(refreshed["username"], account["username"])
        [identity] = self.rows("SELECT last_login_at FROM account_identities")
        [user] = self.rows("SELECT last_login_at FROM users WHERE id = ?", account["id"])
        self.assertGreater(identity["last_login_at"] or "", old)
        self.assertGreater(user["last_login_at"] or "", old)

    def test_names_and_usernames_fall_back_in_order_and_stay_within_their_lengths(self):
        """Covers: AC-accounts-roles-010 (section 7's fallbacks and limits)."""
        cases = (
            ("only-preferred", {"preferred_username": "plain"}, "plain", "plain"),
            ("only-email", {"email": "mail.only@example.org"}, "mail.only", ""),
            ("nothing", {}, "user", ""),
            ("given-and-family", {"given_name": "Grace", "family_name": "Hopper", "email": "gh@example.org"}, "gh", "Grace Hopper"),
        )
        for subject, claims, username, display_name in cases:
            with self.subTest(subject=subject):
                account, _ = self.accounts.ensure_account(ISSUER, subject, claims)
                self.assertEqual((account["username"], account["display_name"]), (username, display_name))
                self.assertEqual(account["email"], claims.get("email", ""))
                self.assertFalse(account["email_verified"])
        long, _ = self.accounts.ensure_account(ISSUER, "subject-long", {
            "preferred_username": "u" * 300, "name": "n" * 300, "email": ("e" * 250) + "@example.org",
        })
        self.assertLessEqual(len(long["username"]), 120)
        self.assertLessEqual(len(long["display_name"]), 200)
        self.assertLessEqual(len(long["email"]), 254)

    def test_usernames_are_made_unique_ignoring_case_and_never_reuse_an_account(self):
        """Covers: AC-accounts-roles-010 (suffixes; two subjects with one email; no match by name)."""
        self.create("Ada")
        first, _ = self.accounts.ensure_account(ISSUER, "subject-ada-1", {"preferred_username": "ada", "email": "same@example.org"})
        second, _ = self.accounts.ensure_account(ISSUER, "subject-ada-2", {"preferred_username": "ADA", "email": "same@example.org"})
        third, _ = self.accounts.ensure_account(OTHER_ISSUER, "subject-ada-1", {"preferred_username": "bob", "email": "bob@example.org"})
        self.assertEqual((first["username"], second["username"], third["username"]), ("ada-2", "ADA-3", "bob-2"))
        self.assertEqual(len({first["id"], second["id"], third["id"], self.bob["id"]}), 4)

    def test_an_identity_needs_an_absolute_issuer_and_a_subject(self):
        """Covers: AC-accounts-roles-010 (issuer local, an empty subject and other refusals)."""
        before = self.rows("SELECT * FROM users")
        for issuer, subject in (("local", "1"), ("", "subject"), ("login.example.org/realms/x", "subject"),
                                ("ftp://login.example.org/realms/x", "subject"), (ISSUER, ""), (ISSUER, "s" * 256)):
            with self.subTest(issuer=issuer, subject=subject[:10]):
                with self.assertRaises(ValueError):
                    self.accounts.ensure_account(issuer, subject, {"email": "x@example.org"})
        self.assertEqual(self.rows("SELECT * FROM users"), before)
        account, created = self.accounts.ensure_account(ISSUER, "s" * 255, {})
        self.assertTrue(created)

    def test_account_for_identity_finds_the_account_and_never_creates_one(self):
        """Covers: AC-accounts-roles-011."""
        users = self.rows("SELECT * FROM users")
        self.assertIsNone(self.accounts.account_for_identity(ISSUER, "nobody"))
        self.assertEqual(self.rows("SELECT * FROM users"), users)
        self.assertEqual(self.rows("SELECT * FROM account_identities"), [])
        account = self.provider_account("somebody")
        self.assertEqual(self.accounts.account_for_identity(ISSUER, "somebody")["id"], account["id"])
        self.assertIsNone(self.accounts.account_for_identity(OTHER_ISSUER, "somebody"))


# ------------------------------------------------------------------------------------------------
# Section 8: cutover commands
# ------------------------------------------------------------------------------------------------


class LegacyCommandTests(AccountsTestCase):
    def setUp(self):
        super().setUp()
        # The cutover commands run after the operator bound the owner (D-045), so these tests hold
        # whether or not a command insists on a bound owner.
        self.bind_owner("--username", "admin")

    def test_a_linked_identity_lands_in_the_legacy_account_with_its_data(self):
        """Covers: AC-accounts-roles-012 (link)."""
        self.store.save_orcid(self.bob["id"], ORCID)
        record_id, _ = self.records.start_import(user=self.bob, request=None, turtle=DRAFT, occurred_at=None, client=CLIENT)
        audit = self.audit_actions()
        code, out = run_cli("legacy", "link", "--account", str(self.bob["id"]), "--issuer", ISSUER,
                            "--subject", "bob-subject", "--reason", "Bob's ROHub login.")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.audit_actions()) - len(audit), 1)
        account, created = self.accounts.ensure_account(ISSUER, "bob-subject", {"email": "bob@rohub.example.org", "email_verified": True})
        self.assertEqual((account["id"], created), (self.bob["id"], False))
        self.assertEqual([item["orcid"] for item in self.store.list_orcids(account["id"])], [ORCID])
        self.assertEqual(self.records.summary(user=account, record_id=record_id)["record_id"], record_id)
        [row] = [row for row in self.history(self.bob["id"]) if row["action"] == "identity_linked"]
        self.assertEqual((row["actor_kind"], row["reason"]), ("offline", "Bob's ROHub login."))

    def test_linking_an_identity_that_is_already_held_is_refused(self):
        """Covers: AC-accounts-roles-012 (refusal)."""
        self.provider_account("held-subject")
        code, out = run_cli("legacy", "link", "--account", str(self.bob["id"]), "--issuer", ISSUER, "--subject", "bob-subject", "--reason", "x")
        self.assertEqual(code, 0, out)
        identities, history = self.identities(), self.all_history()
        for subject in ("held-subject", "bob-subject"):
            with self.subTest(subject=subject):
                code, out = run_cli("legacy", "link", "--account", str(self.carol["id"]), "--issuer", ISSUER,
                                    "--subject", subject, "--reason", "x")
                self.assertEqual(code, 1, out)
        self.assertEqual((self.identities(), self.all_history()), (identities, history))

    def test_admin_survives_a_link_only_with_keep_admin_or_as_the_owner(self):
        """Covers: AC-accounts-roles-012 (--keep-admin)."""
        self.make_admin_in_database("carol")
        self.create("dave")
        self.make_admin_in_database("dave")
        for username, extra, keeps in (("carol", (), False), ("dave", ("--keep-admin",), True), ("admin", (), True)):
            with self.subTest(account=username):
                code, out = run_cli("legacy", "link", "--account", str(self.account(username)["id"]), "--issuer", ISSUER,
                                    "--subject", f"{username}-subject", "--reason", "Cutover.", *extra)
                self.assertEqual(code, 0, out)
                self.assertEqual("admin" in self.account(username)["roles"], keeps)

    def test_fold_moves_the_identities_of_an_empty_provider_account_and_blocks_it(self):
        """Covers: AC-accounts-roles-013 (fold)."""
        self.make_admin_in_database("carol")
        provider = self.provider_account("new-carol")
        elsewhere, _ = self.accounts.ensure_account(OTHER_ISSUER, "new-carol-elsewhere", {})  # another account's identity
        code, out = run_cli("legacy", "fold", "--from", str(provider["id"]), "--into", str(self.carol["id"]), "--reason", "Same person.")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.accounts.account_for_identity(ISSUER, "new-carol")["id"], self.carol["id"])
        self.assertEqual(self.accounts.account_for_identity(OTHER_ISSUER, "new-carol-elsewhere")["id"], elsewhere["id"])
        emptied = self.rows("SELECT * FROM users WHERE id = ?", provider["id"])
        self.assertEqual([row["is_active"] for row in emptied], [0])
        self.assertNotIn("admin", self.account("carol")["roles"])  # no --keep-admin
        for account_id in (provider["id"], self.carol["id"]):
            self.assertIn("account_folded", [row["action"] for row in self.history(account_id)])

    def test_fold_refuses_an_account_that_holds_anything_or_is_the_owner(self):
        """Covers: AC-accounts-roles-013 (refusals)."""
        # Each refusal names what stands in the way. The owner case moves the binding in the database,
        # so that account holds nothing else, history included.
        cases = {"record items": "record", "client contexts": "context", "saved ORCID iDs": "orcid",
                 "history": "history", "owner": "owner"}
        for case, named in cases.items():
            with self.subTest(case=case):
                provider = self.provider_account(f"busy-{case.replace(' ', '-')}")
                if case == "record items":
                    self.records.start_import(user=provider, request=None, turtle=DRAFT, occurred_at=None, client=CLIENT)
                elif case == "client contexts":
                    self.execute("INSERT INTO client_contexts (user_id, created_at) VALUES (?, '2026-11-23T10:00:00+00:00')", provider["id"])
                elif case == "saved ORCID iDs":
                    self.store.save_orcid(provider["id"], ORCID)
                elif case == "history":
                    admin = self.signed_in("admin", "admin-password")
                    self.assertEqual(self.act(admin, "block", provider["id"], "Checking.").status_code, 200)
                    self.assertEqual(self.act(admin, "unblock", provider["id"]).status_code, 200)
                else:
                    self.execute("UPDATE owner_binding SET issuer = ?, subject = ?", ISSUER, provider["external_subject"])
                identities, history = self.identities(), self.all_history()
                code, out = run_cli("legacy", "fold", "--from", str(provider["id"]), "--into", str(self.bob["id"]), "--reason", "x")
                self.assertEqual(code, 1, out)
                self.assertIn(named, out.lower())
                self.assertEqual((self.identities(), self.all_history()), (identities, history))
                self.assertEqual(self.rows("SELECT is_active FROM users WHERE id = ?", provider["id"]), [{"is_active": 1}])

    def test_purge_passwords_without_the_flag_only_counts(self):
        """Covers: AC-accounts-roles-014 (without --confirm-cutover), as owner-sign-in section 10 amends it."""
        self.provider_account("no-password")
        before = self.rows("SELECT * FROM users ORDER BY id")
        code, out = run_cli("legacy", "purge-passwords")
        self.assertRegex(out, r"\b2\b")  # bob and carol; the owner's (admin's) password is kept, so not counted
        self.assertEqual(self.rows("SELECT * FROM users ORDER BY id"), before)
        self.assertEqual(self.all_history(), [row for row in self.all_history() if row["action"] != "password_purged"])
        self.signed_in("bob")

    def test_purge_passwords_ends_every_password_login_but_the_owners_and_keeps_every_row(self):
        """Covers: AC-accounts-roles-014 (with --confirm-cutover), as owner-sign-in section 10 amends it: the owner's
        (admin's) password is kept."""
        provider = self.provider_account("no-password")
        before = self.users_without("password_hash", "updated_at")
        owner_hash = self.rows("SELECT password_hash FROM users WHERE id = ?", self.admin["id"])[0]["password_hash"]
        code, out = run_cli("legacy", "purge-passwords", "--confirm-cutover")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.users_without("password_hash", "updated_at"), before)
        self.assertEqual({row["password_hash"] for row in self.rows("SELECT password_hash FROM users WHERE id <> ?", self.admin["id"])}, {"!"})
        self.assertEqual(self.rows("SELECT password_hash FROM users WHERE id = ?", self.admin["id"]), [{"password_hash": owner_hash}])
        conn = previous_image.connect(self.db)
        try:
            for username, password, works in (("admin", "admin-password", True), ("bob", "password-123", False), ("carol", "password-123", False)):
                with self.subTest(username=username):
                    response = self.client().post("/api/auth/login", json={"username": username, "password": password})
                    self.assertEqual(response.status_code, 200 if works else 401)
                    self.assertEqual(bool(previous_image.password_login(conn, username, password)), works)
        finally:
            conn.close()
        for account_id in (self.bob["id"], self.carol["id"]):
            rows = [row for row in self.history(account_id) if row["action"] == "password_purged"]
            self.assertEqual([(row["actor_kind"]) for row in rows], ["offline"])
        for account_id in (self.admin["id"], provider["id"]):
            self.assertEqual([row for row in self.history(account_id) if row["action"] == "password_purged"], [])

    def test_legacy_show_describes_an_account(self):
        """Covers: AC-accounts-roles-012 (section 8's legacy show: identities and history among what it prints)."""
        self.store.save_orcid(self.bob["id"], ORCID)
        code, out = run_cli("legacy", "link", "--account", str(self.bob["id"]), "--issuer", ISSUER,
                            "--subject", "bob-subject", "--reason", "Cutover.")
        self.assertEqual(code, 0, out)
        for arguments in (("--account", str(self.bob["id"])), ("--username", "bob")):
            with self.subTest(arguments=arguments):
                code, out = run_cli("legacy", "show", *arguments)
                self.assertEqual(code, 0, out)
                for shown in ("bob", ISSUER, "bob-subject", "identity_linked"):
                    self.assertIn(shown, out)
        self.assertEqual(run_cli("legacy", "show", "--username", "nobody")[0], 1)
        self.assertEqual(run_cli("legacy", "show")[0], 2)


class KeptDataTests(AccountsTestCase):
    def test_blocked_folded_and_purged_accounts_keep_their_data_and_their_name_in_the_export(self):
        """Covers: AC-accounts-roles-015."""
        for account in (self.bob, self.carol):
            self.store.save_orcid(account["id"], ORCID)
            self.records.start_import(user=account, request=None, turtle=DRAFT, occurred_at=None, client=CLIENT)
        admin = self.owner_client()  # the cutover commands below run with an owner bound (D-045)
        self.assertEqual(self.act(admin, "block", self.bob["id"], "Left.").status_code, 200)
        provider = self.provider_account("carol-at-rohub")
        code, out = run_cli("legacy", "fold", "--from", str(provider["id"]), "--into", str(self.carol["id"]), "--reason", "Same person.")
        self.assertEqual(code, 0, out)
        code, out = run_cli("legacy", "purge-passwords", "--confirm-cutover")
        self.assertEqual(code, 0, out)

        self.assertEqual(len(self.rows("SELECT * FROM saved_orcids")), 2)
        self.assertEqual(len(self.rows("SELECT * FROM record_items")), 2)
        exporter = self.client()
        sign_in_as(exporter, self.admin["id"])  # passwords are gone; a session still works
        response = exporter.get("/api/admin/records/export")
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            summary = list(csv.DictReader(io.StringIO(archive.read("summary.csv").decode("utf-8"), newline="")))
        named = {(row["username"], row["name"], row["email"]) for row in summary}
        self.assertEqual(named, {("bob", "Bob Example", "bob@example.org"), ("carol", "Carol Example", "carol@example.org")})


class PreviousImageTests(AccountsTestCase):
    def test_the_previous_image_reads_this_module_s_accounts_as_section_3_says(self):
        """Covers: AC-accounts-roles-016."""
        self.bind_owner("--username", "carol")
        owner = self.signed_in("carol")
        self.assertEqual(self.act(owner, "block", self.bob["id"], "Left.").status_code, 200)
        dave = self.create("dave")
        self.assertEqual(self.act(owner, "grant-admin", dave["id"]).status_code, 200)
        provider = self.provider_account("provider-login")
        # The owner retires the bootstrap account (id 1): no longer an admin, and blocked.
        self.assertEqual(self.act(owner, "revoke-admin", self.admin["id"]).status_code, 200)
        self.assertEqual(self.act(owner, "block", self.admin["id"], "Retired.").status_code, 200)

        conn = previous_image.connect(self.db)
        try:
            self.assertFalse(previous_image.password_login(conn, "bob", "password-123"))  # blocked
            for password in ("", "!", "password-123"):
                self.assertFalse(previous_image.password_login(conn, provider["username"], password))  # provider account
            self.assertTrue(previous_image.password_login(conn, "carol", "password-123"))
            self.assertTrue(previous_image.is_admin(conn, "carol"))  # the owner is an admin
            self.assertTrue(previous_image.is_admin(conn, "dave"))
        finally:
            conn.close()

        # Its startup, with the bootstrap settings set, re-enables and re-promotes the bootstrap account.
        self.assertEqual(previous_image.start(self.db), [])
        [admin] = self.rows("SELECT * FROM users WHERE id = 1")
        self.assertEqual((admin["username"], admin["is_active"], json.loads(admin["roles"])), ("admin", 1, ["admin", "user"]))


# ------------------------------------------------------------------------------------------------
# Section 9: routes
# ------------------------------------------------------------------------------------------------


class AdminRouteTests(AccountsTestCase):
    def test_the_public_user_shape_adds_the_role_everywhere(self):
        """Covers: AC-accounts-roles-017 (section 9: login, /api/auth/me and the admin routes share it)."""
        owner = self.owner_client()
        for username, password, role in (("bob", "password-123", "user"), ("admin", "admin-password", "owner")):
            with self.subTest(username=username):
                response = self.client().post("/api/auth/login", json={"username": username, "password": password})
                self.assertEqual(response.json()["user"]["role"], role)
        self.assertEqual(owner.get("/api/auth/me").json()["user"]["role"], "owner")
        created = owner.post("/api/admin/users", json={"username": "erin", "password": "password-123"})
        self.assertEqual(created.json()["user"]["role"], "user")
        edited = owner.patch(f"/api/admin/users/{self.carol['id']}", json={"display_name": "Carol C."})
        self.assertEqual(edited.json()["user"]["role"], "user")
        granted = self.act(owner, "grant-admin", self.carol["id"])
        self.assertEqual(granted.json()["user"]["role"], "admin")
        blocked = self.act(owner, "block", self.bob["id"], "Left.")
        self.assertEqual((blocked.json()["user"]["role"], blocked.json()["user"]["blocked"]), ("user", True))

    def test_the_account_list_adds_role_block_state_identities_and_times(self):
        """Covers: AC-accounts-roles-017 (GET /api/admin/users)."""
        owner = self.owner_client()
        self.assertEqual(self.act(owner, "grant-admin", self.carol["id"]).status_code, 200)
        self.assertEqual(self.act(owner, "block", self.bob["id"], "Left.").status_code, 200)
        verified = self.provider_account("listed-verified")
        unverified = self.provider_account("listed-unverified", email_verified=False)

        users = owner.get("/api/admin/users").json()["users"]
        stored = {row["id"]: row for row in self.rows("SELECT * FROM users")}
        expected_order = sorted(stored.values(), key=lambda row: (-row["is_active"], row["username"].lower()))
        self.assertEqual([user["id"] for user in users], [row["id"] for row in expected_order])
        for user in users:
            row = stored[user["id"]]
            with self.subTest(username=row["username"]):
                self.assertEqual({key: user[key] for key in LEGACY_FIELDS}, {
                    "id": row["id"], "username": row["username"], "display_name": row["display_name"] or row["username"],
                    "email": row["email"] or "", "roles": json.loads(row["roles"]), "is_active": bool(row["is_active"]),
                    "auth_provider": row["auth_provider"], "external_subject": row["external_subject"],
                })
                self.assertEqual((user["created_at"], user["last_login_at"]), (row["created_at"], row["last_login_at"]))
                self.assertEqual(user["blocked"], not row["is_active"])
        by_id = {user["id"]: user for user in users}
        self.assertEqual([by_id[account["id"]]["role"] for account in (self.admin, self.carol, self.bob)], ["owner", "admin", "user"])
        self.assertEqual([by_id[account["id"]]["local"] for account in (self.admin, verified)], [True, False])
        self.assertEqual([by_id[account["id"]]["email_verified"] for account in (self.bob, verified, unverified)], [None, True, False])
        self.assertEqual(by_id[self.bob["id"]]["identities"], [])
        [identity] = by_id[verified["id"]]["identities"]
        self.assertEqual((identity["issuer"], identity["subject"]), (ISSUER, "listed-verified"))
        self.assertTrue(identity["last_login_at"])

    def test_an_account_s_history_lists_its_changes_newest_first(self):
        """Covers: AC-accounts-roles-017 (GET /api/admin/users/{user_id}/history)."""
        owner = self.owner_client()
        for action, reason in (("grant-admin", "Workshop."), ("block", "Holiday."), ("unblock", None), ("revoke-admin", "Done.")):
            self.assertEqual(self.act(owner, action, self.carol["id"], reason).status_code, 200)
        history = owner.get(f"/api/admin/users/{self.carol['id']}/history").json()["history"]
        self.assertEqual([(item["action"], item["actor"], item["reason"] or None) for item in history], [
            ("admin_revoked", "admin", "Done."), ("unblocked", "admin", None), ("blocked", "admin", "Holiday."),
            ("admin_granted", "admin", "Workshop."),
        ])
        self.assertTrue(all(item["created_at"] and "details" in item for item in history))
        [bound] = owner.get(f"/api/admin/users/{self.admin['id']}/history").json()["history"][:1]
        self.assertEqual((bound["action"], bound["actor"]), ("owner_bound", "offline"))
        self.assertEqual(owner.get("/api/admin/users/9999/history").status_code, 404)
        for action in ("block", "unblock", "grant-admin", "revoke-admin"):
            with self.subTest(unknown=action):
                self.assertEqual(self.act(owner, action, 9999, "x").status_code, 404)

    def test_a_bootstrap_grant_names_its_actor(self):
        """Covers: AC-accounts-roles-017 (actor bootstrap)."""
        db = self.dir / "fresh.sqlite3"
        self.use_database(db)
        with started_app() as client:
            self.login(client, "admin", "admin-password")
            history = client.get("/api/admin/users/1/history").json()["history"]
        self.assertEqual([(item["action"], item["actor"]) for item in history], [("admin_granted", "bootstrap")])


class RequestValidationTests(AccountsTestCase):
    def test_a_block_needs_a_reason_of_1_to_500_characters(self):
        """Covers: AC-accounts-roles-019 (the block reason)."""
        admin = self.signed_in("admin", "admin-password")
        history = self.all_history()
        for body in ({}, {"reason": ""}, {"reason": "x" * 501}):
            with self.subTest(length=len(body.get("reason", "")) if body else None):
                response = admin.post(f"/api/admin/users/{self.bob['id']}/block", json=body)
                self.assertEqual(response.status_code, 400)
        self.assertTrue(self.account("bob")["is_active"])
        self.assertEqual(self.all_history(), history)
        self.assertEqual(admin.post(f"/api/admin/users/{self.bob['id']}/block", json={"reason": "x" * 500}).status_code, 200)

    def test_granting_admin_to_a_blocked_account_is_refused(self):
        """Covers: AC-accounts-roles-019 (target_blocked)."""
        owner = self.owner_client()
        self.assertEqual(self.act(owner, "block", self.bob["id"], "Left.").status_code, 200)
        history = self.all_history()
        response = self.act(owner, "grant-admin", self.bob["id"])
        self.assertEqual((response.status_code, response.json()), (400, refusal("target_blocked")))
        self.assertEqual((self.account("bob")["roles"], self.all_history()), (["user"], history))
