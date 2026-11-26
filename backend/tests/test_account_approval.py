"""Approving accounts that an identity provider signs in (the owner, 2026-11-25).

Asked "On the live site, should new ROHub accounts wait for your approval?", the owner answered "Yes,
approval": people accept the terms by email, and an admin's approval on the admin page stands for that. An
account that is not approved signs in, sees its account and saves ORCID iDs. An approved one may do what a
local account may. While the daily limit is switched on an account that is not approved may decompose
before, so many times a day, and is recorded, but does not publish (plan v2's permission table).

Models, the registry and identity providers are faked: nothing here reaches any of them.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import threading
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from unittest.mock import patch

import previous_image
from fastapi.testclient import TestClient
from increment1_support import ISSUER, run_cli, sign_in_as
from increment2_support import OidcTestCase
from test_access_policy import ADMIN, NANOPUB, ORCID, OWN_USE, AccessHttpTestCase
from test_accounts_roles import AccountsTestCase
from test_records import CLIENT, DRAFT, MATRIX, PIPELINE_RESULT

from app import storage, usage
from app.access.policy import AccessState, Capability, decide
from app.access.state import account_state
from app.account_model import approval_of
from app.accounts import AccountStore
from app.core import config as core_config
from app.routers import decompose as decompose_router
from app.routers import nanopub as nanopub_router
from app.services.llm import stream_event

C = Capability
APPROVE, WITHDRAW = "approve", "withdraw-approval"
FIRST = "An admin must approve your account first."
NOTE = "accepted the terms by email on 25 November"
ASK = "Ask an admin to retract this publication."
ENDED = "Password sign-in has ended for this account. Ask an admin how to continue."
WAITING = {**{capability: ("approval_required", FIRST) for capability in OWN_USE}, C.RETRACT_OWN: ("approval_required", ASK)}
NOT_ADMIN = {**{capability: ("admin_required", "Admin access required.") for capability in ADMIN},
             C.MANAGE_ADMINS: ("owner_required", "Only the owner can do this.")}
REFUSED = {
    "owner_protected": (403, "The owner needs no approval."),
    "own_account": (403, "You cannot approve your own account or withdraw its approval."),
    "owner_required": (403, "Only the owner can do this."),
    "admin_required": (403, "Admin access required."),
    "approval_not_needed": (400, "A local account needs no approval."),
}
UNCHANGED = {
    APPROVE: "This account is already approved; see its history.",
    WITHDRAW: "This account is not approved; see its history.",
}
MIGRATION = "0006_account_approval"
MIGRATION_SHA256 = "55ab56b749f8de3758825ac838770fed01e8e36a6f5c627516e7b281da89ef90"


# ------------------------------------------------------------------------------------------------
# The decision
# ------------------------------------------------------------------------------------------------


class DecisionTests(unittest.TestCase):
    def state(self, **changes: Any) -> AccessState:
        values: Dict[str, Any] = dict(account_id=7, email_verified=True, terms_accepted=True, application="none", uses_left_today=3)
        return AccessState(**{**values, **changes})

    def test_approval_first_refuses_all_own_use_and_nothing_else(self):
        """Covers: AC-account-approval-001."""
        for application in ("none", "pending", "rejected", "withdrawn"):
            for uses in (None, 0, 3):
                with self.subTest(application=application, uses=uses):
                    decisions = decide(self.state(application=application, uses_left_today=uses, approval_first=True))
                    for capability, entry in decisions.items():
                        expected = {**WAITING, **NOT_ADMIN}.get(capability)
                        self.assertEqual((entry.allowed, entry.reason, entry.message), (False, *expected) if expected else (True, None, None), capability)
                        self.assertEqual((entry.uses_left_today, entry.resets_at), (None, None))

    def test_it_changes_nothing_for_whom_is_approved_and_comes_after_the_gates_and_the_terms(self):
        """Covers: AC-account-approval-001."""
        for changes in (dict(application="approved"), dict(role="admin"), dict(role="owner"), dict(account_id=None), dict(blocked=True),
                        dict(email_verified=False), dict(terms_accepted=False), dict(account_id=0, account_backed=False)):
            with self.subTest(changes):
                self.assertEqual(decide(self.state(**changes, approval_first=True)), decide(self.state(**changes)))

    def test_an_account_whose_password_ended_waits_for_nothing_and_has_no_own_use(self):
        """Covers: AC-account-approval-009."""
        ended = {capability: ("password_ended", ENDED) for capability in OWN_USE}
        for first, uses, application in itertools.product((True, False), (None, 0, 3), ("none", "pending", "rejected", "withdrawn")):
            with self.subTest(approval_first=first, uses=uses, application=application):
                decisions = decide(self.state(password_ended=True, approval_first=first, uses_left_today=uses, application=application))
                for capability, entry in decisions.items():
                    expected = {**ended, **NOT_ADMIN}.get(capability)
                    self.assertEqual((entry.allowed, entry.reason, entry.message), (False, *expected) if expected else (True, None, None), capability)
                    self.assertEqual((entry.uses_left_today, entry.resets_at), (None, None))
        for changes in (dict(application="approved"), dict(role="admin"), dict(role="owner"), dict(account_id=None), dict(blocked=True),
                        dict(email_verified=False), dict(terms_accepted=False), dict(account_id=0, account_backed=False)):
            with self.subTest(changes):
                self.assertEqual(decide(self.state(**changes, password_ended=True)), decide(self.state(**changes)))
        self.assertEqual(decide(self.state()), decide(self.state(password_ended=False)))

    def test_it_is_off_by_default(self):
        """Covers: AC-account-approval-001."""
        self.assertEqual(decide(self.state()), decide(self.state(approval_first=False)))
        self.assertTrue(decide(self.state())[C.DECOMPOSE].allowed)


# ------------------------------------------------------------------------------------------------
# What an approval changes, the same on a development machine and in production
# ------------------------------------------------------------------------------------------------


class Rules:
    """For a browser in which somebody of an identity provider is signed in."""

    def waiting(self, name: str = "ada") -> Tuple[TestClient, int]:
        raise NotImplementedError

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.admin = self.client()  # type: ignore[attr-defined]
        self.login(self.admin, "admin", "admin-password")  # type: ignore[attr-defined]

    def act(self, action: str, account_id: int, note: Optional[str] = None) -> Dict[str, Any]:
        response = self.admin.post(f"/api/admin/users/{account_id}/{action}", json={} if note is None else {"reason": note})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def stream(self, client: TestClient) -> Any:
        events = [stream_event("raw_delta", delta="{"), stream_event("final", data=dict(PIPELINE_RESULT))]
        with patch.object(decompose_router, "stream_pipeline_events", side_effect=lambda *_, **__: iter(events)):
            return client.post("/api/decompose/stream", json={"definition": "dissolved oxygen in water", "client": CLIENT})

    def assert_waits(self, client: TestClient) -> None:
        """Every own use is refused, with the reason and the message, and nothing of it is done."""
        self.assert_capabilities(self.capabilities(client), [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA], {**WAITING, **NOT_ADMIN})
        decompose = self.capabilities(client)["decompose"]
        self.assertEqual((decompose["uses_left_today"], decompose["resets_at"]), (None, None))
        tables = ("record_items", "record_events", "audit_events", "decomposition_uses")
        before = [self.rows(f"SELECT * FROM {table}") for table in tables]
        unexpected = AssertionError("the refused operation ran")
        with (
            patch.object(decompose_router, "run_pipeline", side_effect=unexpected) as pipeline,
            patch.object(decompose_router, "stream_pipeline_events", side_effect=unexpected) as stream,
            patch.object(nanopub_router, "get_nanopub_profile", side_effect=unexpected) as profile,
            patch.object(nanopub_router, "Nanopub", side_effect=unexpected) as nanopub,
            patch.object(nanopub_router, "build_retraction_nanopub", side_effect=unexpected) as retraction,
        ):
            for path, body in (
                ("/api/decompose", {"definition": "air temperature"}),
                ("/api/decompose/stream", {"definition": "air temperature"}),
                ("/api/records", {"turtle": DRAFT}),
                ("/api/records/0123456789abcdef/events", {"type": "mark_best", "version": 0, "unchecked_components": 0}),
                ("/api/nanopub/publish", {"ttl": DRAFT, "creator_orcid_id": ORCID, "record_id": "0123456789abcdef"}),
                ("/api/nanopub/retract", {"nanopub_uri": NANOPUB, "creator_orcid_id": ORCID}),
            ):
                with self.subTest(path):
                    response = client.post(path, json=body)
                    message = ASK if path.endswith("/retract") else FIRST
                    self.assertEqual((response.status_code, response.json()), (403, {"detail": message, "reason": "approval_required"}))
                    self.assertNotIn("retry-after", response.headers)
            for mock in (pipeline, stream, profile, nanopub, retraction):
                mock.assert_not_called()
        self.assertEqual([self.rows(f"SELECT * FROM {table}") for table in tables], before)

    def test_an_account_waits_and_still_has_its_session_and_its_orcid_ids(self):
        """Covers: AC-account-approval-002."""
        ada, _ = self.waiting()
        self.assert_waits(ada)
        self.assertEqual(ada.post("/api/auth/me/orcids", json={"orcid": ORCID}).status_code, 200)
        self.assertEqual([item["orcid"] for item in ada.get("/api/auth/me/orcids").json()["orcids"]], [ORCID])
        self.assertEqual(ada.get("/api/auth/verify").status_code, 204)
        self.assertEqual(ada.post("/api/events", json={"action": "visualize"}).status_code, 200)
        self.assertEqual(ada.get("/api/auth/verify/admin").status_code, 403)

    def test_an_approval_reaches_its_session_at_once_and_lets_it_do_what_a_local_account_does(self):
        """Covers: AC-account-approval-003."""
        ada, account_id = self.waiting()
        self.assert_waits(ada)
        self.act(APPROVE, account_id, NOTE)

        capabilities = self.capabilities(ada)
        self.assert_capabilities(capabilities, [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA, *OWN_USE], NOT_ADMIN)
        self.assertEqual((capabilities["decompose"]["uses_left_today"], capabilities["decompose"]["resets_at"]), (None, None))
        for _ in range(3):  # together more than the daily allowance of D-033
            self.assertEqual(self.decompose(ada).status_code, 200)
            self.assertIn('"final"', self.stream(ada).text)
        record_id = self.start_record(ada)
        for body in (
            {"type": "check", "version": 0, "changes": [{"operation": "confirm", "role": "hasMatrix", "component": MATRIX}]},
            {"type": "mark_best", "version": 0, "unchecked_components": 0},
        ):
            self.assertEqual(ada.post(f"/api/records/{record_id}/events", json={**body, "client": CLIENT}).status_code, 201)
        published = self.publish(ada, record_id)
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(self.retract(ada).status_code, 200)
        self.assertEqual(len(self.rows("SELECT * FROM record_items WHERE user_id = ?", account_id)), 7)
        self.assertEqual(self.rows("SELECT * FROM decomposition_uses"), [])

    def test_a_withdrawal_reaches_its_session_at_once_and_keeps_its_data(self):
        """Covers: AC-account-approval-003."""
        ada, account_id = self.waiting()
        self.act(APPROVE, account_id)
        record_id = self.start_record(ada)
        self.assertEqual(ada.get(f"/api/records/{record_id}").status_code, 200)
        self.act(WITHDRAW, account_id, "The terms changed.")
        self.assert_waits(ada)
        self.assertEqual(ada.get(f"/api/records/{record_id}").json()["reason"], "approval_required")
        self.act(APPROVE, account_id)
        self.assertEqual(ada.get(f"/api/records/{record_id}").status_code, 200)

    def test_each_account_is_approved_for_itself(self):
        """Covers: AC-account-approval-004."""
        ada, account_id = self.waiting("ada")
        ben, _ = self.waiting("ben")
        self.act(APPROVE, account_id)
        self.assertEqual(self.decompose(ada).status_code, 200)
        self.assert_waits(ben)

    def test_an_approved_account_that_is_blocked_is_blocked(self):
        """Covers: AC-account-approval-004."""
        ada, account_id = self.waiting()
        self.act(APPROVE, account_id)
        self.act("block", account_id, "A test.")
        self.assertEqual(ada.get("/api/auth/me").status_code, 401)
        self.assertEqual(ada.post("/api/decompose", json={"definition": "air temperature"}).status_code, 401)
        self.assertEqual(self.act("unblock", account_id)["approval"], "approved")

    def test_an_admin_and_the_owner_count_as_approved_by_what_they_are(self):
        """Covers: AC-account-approval-005."""
        ada, account_id = self.waiting("ada")
        code, out = run_cli("owner", "bind", "--username", "admin")
        self.assertEqual(code, 0, out)
        self.act("grant-admin", account_id)
        self.assertEqual(self.decompose(ada).status_code, 200)
        self.assert_capabilities(self.capabilities(ada), [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA, *OWN_USE, *ADMIN],
                                 {C.MANAGE_ADMINS: NOT_ADMIN[C.MANAGE_ADMINS]})
        self.act("revoke-admin", account_id)
        self.assert_waits(ada)  # nobody approved it
        self.act("grant-admin", account_id)
        self.act(APPROVE, account_id)  # the owner may, also an admin's account
        self.act("revoke-admin", account_id)
        self.assertEqual(self.decompose(ada).status_code, 200)

        ben, ben_id = self.waiting("ben")
        self.assert_waits(ben)
        code, out = run_cli("owner", "rebind", "--account", str(ben_id), "--reason", "A test.")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.decompose(ben).status_code, 200)

    def test_a_local_account_counts_as_approved_until_it_is_linked_and_is_then_approved_by_a_click(self):
        """Covers: AC-account-approval-006."""
        bob, account = self.client(), self.bob
        self.login(bob, "bob")
        self.assertEqual(self.decompose(bob).status_code, 200)
        code, out = run_cli("owner", "bind", "--username", "admin")
        self.assertEqual(code, 0, out)
        code, out = run_cli("legacy", "link", "--account", str(account["id"]), "--issuer", ISSUER, "--subject", "bob-subject", "--reason", "A test.")
        self.assertEqual(code, 0, out)
        self.assert_waits(bob)
        self.act(APPROVE, account["id"])
        self.assertEqual(self.decompose(bob).status_code, 200)

    def test_with_the_daily_limit_an_account_decomposes_and_is_recorded_before_it_is_approved(self):
        """Covers: AC-account-approval-003, AC-daily-limit-008, AC-daily-limit-009.

        Plan v2's table for a verified account; the refusal beyond it is the limit's, and it does not publish.
        """
        ada, account_id = self.waiting()
        with patch.object(core_config.settings, "daily_decompositions_raw", "2"), \
                patch.object(usage, "now", return_value=datetime(2026, 11, 25, 11, 0, tzinfo=timezone.utc)):
            capabilities = self.capabilities(ada)
            unapproved = {C.PUBLISH: ("approval_required", "Publishing needs an approved account."),
                          C.RETRACT_OWN: ("approval_required", "Ask an admin to retract this publication.")}
            self.assert_capabilities(capabilities, [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA, C.DECOMPOSE, C.SAVE_AND_REVIEW], {**unapproved, **NOT_ADMIN})
            self.assertEqual((capabilities["decompose"]["uses_left_today"], capabilities["decompose"]["resets_at"]), (2, "2026-11-26T00:00:00+01:00"))
            self.assertEqual(self.decompose(ada).status_code, 200)
            self.assertIn('"final"', self.stream(ada).text)
            for response in (self.decompose(ada), self.stream(ada)):
                self.assertEqual((response.status_code, response.json()), (429, {
                    "detail": "No decompositions left today.", "reason": "daily_limit", "resets_at": "2026-11-26T00:00:00+01:00"}))
            record_id = self.start_record(ada)
            self.assertEqual(len(self.rows("SELECT * FROM record_items WHERE user_id = ?", account_id)), 3)
            self.assertEqual(ada.post(f"/api/records/{record_id}/events", json={"type": "mark_best", "version": 0, "unchecked_components": 0, "client": CLIENT}).status_code, 201)
            refused = self.publish(ada, record_id)
            self.assertEqual((refused.status_code, refused.json()), (403, {"detail": "Publishing needs an approved account.", "reason": "approval_required"}))
            self.assertEqual(self.retract(ada).json()["reason"], "approval_required")

            self.act(APPROVE, account_id)  # an approved account is never capped (the owner: "No, unapproved only")
            for _ in range(3):
                self.assertEqual(self.decompose(ada).status_code, 200)
            self.assertEqual(self.publish(ada, record_id).status_code, 200)
            self.assertEqual(self.capabilities(ada)["decompose"]["uses_left_today"], None)
            self.act(WITHDRAW, account_id)
            self.assertEqual(self.decompose(ada).status_code, 429)
            self.assertEqual(len(self.rows("SELECT * FROM decomposition_uses")), 2)
        self.assert_waits(ada)  # switched off again


class ProductionTests(Rules, AccessHttpTestCase):
    """The default environment; accounts have their session from the auth store, as no fake provider signs in there."""

    def waiting(self, name: str = "ada") -> Tuple[TestClient, int]:
        account, client = self.provider_account(f"subject-{name}", preferred_username=name), self.client()
        sign_in_as(client, account["id"])
        return client, account["id"]


class DevelopmentTests(Rules, OidcTestCase, AccessHttpTestCase):
    """Mode ``both`` on a development machine; people sign in with ROHub."""

    def waiting(self, name: str = "ada") -> Tuple[TestClient, int]:
        browser = self.signed_in(sub=f"subject-{name}", email=f"{name}@example.org", preferred_username=name, sid=f"session-{name}")
        browser.headers.update(self.csrf_headers(browser))
        return browser, self.rows("SELECT id FROM users WHERE username = ?", name)[0]["id"]


class EnvironmentTests(AccessHttpTestCase):
    def test_an_account_s_state_does_not_depend_on_the_environment(self):
        """Covers: AC-account-approval-007."""
        account = self.store.get_user(self.provider_account("subject-ada")["id"])
        states = []
        for environment in ("", "production", "development"):
            with patch.object(core_config.settings, "environment_raw", environment):
                states.append(account_state(account, None))
        self.assertEqual(states, [states[0]] * 3)
        self.assertEqual((states[0].terms_accepted, states[0].application, states[0].approval_first, states[0].uses_left_today), (True, "none", True, None))

    def test_who_waits_is_whom_the_list_says_waits(self):
        """Covers: AC-account-approval-007.

        A usable password without an identity counts as approved, and so does a role; a password is not enough, nor is no identity.

        An account that nobody can sign in to has no approval and waits for none.
        """
        for local, has_identity, approved_at, role in itertools.product((True, False), (True, False), (None, "2026-11-25T12:00:00+00:00"), ("user", "admin", "owner")):
            account = {"id": 7, "role": role, "is_active": True, "email_verified": None, "local": local, "has_identity": has_identity, "approved_at": approved_at}
            with self.subTest(local=local, has_identity=has_identity, approved_at=approved_at, role=role):
                legacy = local and not has_identity
                self.assertEqual(account_state(account, None).application, "approved" if legacy or approved_at else "none")
                self.assertEqual(approval_of(account), "approved" if approved_at else "waiting" if has_identity and role == "user" else "not_needed")
                decision = decide(account_state(account, None))[C.DECOMPOSE]
                nobody_s = not (local or has_identity or role != "user" or approved_at is not None)
                self.assertEqual(decision.allowed, approval_of(account) != "waiting" and not nobody_s)
                self.assertEqual(decision.reason, "password_ended" if nobody_s else "approval_required" if approval_of(account) == "waiting" else None)


# ------------------------------------------------------------------------------------------------
# Who may approve whom, and what is kept of it
# ------------------------------------------------------------------------------------------------


class ApprovalTestCase(AccountsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ada = self.provider_account("subject-ada", preferred_username="ada", name="Ada Example")

    def signed_in_as(self, account_id: int) -> TestClient:
        client = self.client()
        sign_in_as(client, account_id)
        return client

    def approval(self, account_id: int) -> Tuple[Optional[str], Optional[int]]:
        [row] = self.rows("SELECT approved_at, approved_by FROM users WHERE id = ?", account_id)
        return row["approved_at"], row["approved_by"]

    def assert_refused(self, client: TestClient, account_id: int, reason: str) -> None:
        """Both actions are refused for that reason and change nothing."""
        status, message = REFUSED[reason]
        before = [self.rows(f"SELECT * FROM {table} ORDER BY rowid") for table in ("users", "account_history", "audit_events")]
        for action in (APPROVE, WITHDRAW):
            response = self.act(client, action, account_id, NOTE)
            self.assertEqual((response.status_code, response.json()), (status, {"detail": message, "reason": reason}), action)
        self.assertEqual([self.rows(f"SELECT * FROM {table} ORDER BY rowid") for table in ("users", "account_history", "audit_events")], before)


class WhoTests(ApprovalTestCase):
    def test_an_admin_and_the_owner_approve_ordinary_accounts(self):
        """Covers: AC-account-approval-008."""
        self.make_admin_in_database("carol")
        owner, carol = self.owner_client(), self.signed_in("carol")
        for client, actor in ((carol, self.carol["id"]), (owner, self.admin["id"])):
            approved = self.act(client, APPROVE, self.ada["id"], NOTE)
            self.assertEqual((approved.status_code, approved.json()["user"]["approval"]), (200, "approved"), approved.text)
            at, by = self.approval(self.ada["id"])
            self.assertEqual(by, actor)
            self.assertLess(abs(datetime.fromisoformat(at).timestamp() - datetime.now(timezone.utc).timestamp()), 60)
            withdrawn = self.act(client, WITHDRAW, self.ada["id"])
            self.assertEqual((withdrawn.status_code, withdrawn.json()["user"]["approval"]), (200, "waiting"))
            self.assertEqual(self.approval(self.ada["id"]), (None, None))

    def test_nobody_else_does(self):
        """Covers: AC-account-approval-008."""
        self.assert_refused(self.signed_in("bob"), self.ada["id"], "admin_required")
        self.assert_refused(self.signed_in_as(self.ada["id"]), self.ada["id"], "admin_required")
        for action in (APPROVE, WITHDRAW):
            response = self.client().post(f"/api/admin/users/{self.ada['id']}/{action}", json={})
            self.assertEqual((response.status_code, response.json()), (401, {"detail": "Authentication required."}))
        self.assertEqual(self.approval(self.ada["id"]), (None, None))

    def test_nobody_approves_themselves_or_the_owner_and_only_the_owner_acts_on_admins(self):
        """Covers: AC-account-approval-008."""
        ben = self.provider_account("subject-ben", preferred_username="ben")
        for name in ("ada", "ben"):
            self.make_admin_in_database(name)
        self.bind_owner("--issuer", ISSUER, "--subject", "subject-ben")
        ada, owner = self.signed_in_as(self.ada["id"]), self.signed_in_as(ben["id"])
        self.assert_refused(ada, self.ada["id"], "own_account")
        self.assert_refused(ada, ben["id"], "owner_protected")
        self.assert_refused(owner, ben["id"], "owner_protected")
        dora = self.provider_account("subject-dora", preferred_username="dora")
        self.make_admin_in_database("dora")
        self.assert_refused(ada, dora["id"], "owner_required")
        self.assertEqual(self.act(owner, APPROVE, dora["id"]).json()["user"]["approval"], "approved")
        self.assertEqual(self.act(owner, WITHDRAW, dora["id"]).json()["user"]["approval"], "not_needed")  # an admin

    def test_a_local_account_needs_none(self):
        """Covers: AC-account-approval-009."""
        admin = self.signed_in("admin", "admin-password")
        self.assert_refused(admin, self.bob["id"], "approval_not_needed")
        self.assert_refused(admin, self.admin["id"], "own_account")

    def test_an_account_nobody_can_sign_in_to_any_more_neither_waits_nor_is_approved(self):
        """Covers: AC-account-approval-009.

        After the cutover's purge, an unlinked local account has neither a password nor an identity.
        """
        self.bind_owner()
        admin = self.signed_in("admin", "admin-password")
        code, out = run_cli("legacy", "purge-passwords", "--confirm-cutover")
        self.assertEqual(code, 0, out)
        status = {user["username"]: user["approval"] for user in admin.get("/api/admin/users").json()["users"]}
        self.assertEqual((status["bob"], status["carol"], status["ada"]), ("not_needed", "not_needed", "waiting"))
        self.assert_refused(admin, self.bob["id"], "approval_not_needed")

    def test_somebody_still_signed_in_after_the_purge_is_told_that_the_password_ended(self):
        """Covers: AC-account-approval-009."""
        self.bind_owner()
        admin, bob, ada = self.signed_in("admin", "admin-password"), self.signed_in("bob"), self.signed_in_as(self.ada["id"])
        self.assertEqual(run_cli("legacy", "purge-passwords", "--confirm-cutover")[0], 0)
        for limit in ("", "3"):
            with self.subTest(limit=limit), patch.object(core_config.settings, "daily_decompositions_raw", limit):
                capabilities = bob.get("/api/auth/me").json()["capabilities"]
                for capability in OWN_USE:
                    entry = capabilities[capability.value]
                    self.assertEqual((entry["allowed"], entry["reason"], entry["message"]), (False, "password_ended", ENDED), capability)
                self.assertTrue(capabilities["session"]["allowed"] and capabilities["account_data"]["allowed"])
                refused = bob.post("/api/decompose", json={"definition": "dissolved oxygen in water"})
                self.assertEqual((refused.status_code, refused.json()), (403, {"detail": ENDED, "reason": "password_ended"}))
                # The owner's own session goes on, and an account with an identity is as it was.
                self.assertTrue(admin.get("/api/auth/me").json()["capabilities"]["decompose"]["allowed"])
                self.assertEqual(ada.get("/api/auth/me").json()["capabilities"]["publish"]["reason"], "approval_required")
        self.assertEqual(self.rows("SELECT * FROM decomposition_uses"), [])

    def test_an_unknown_account_is_unknown(self):
        """Covers: AC-account-approval-008."""
        admin = self.signed_in("admin", "admin-password")
        for action in (APPROVE, WITHDRAW):
            for account_id in (9999, 2**63):
                self.assertEqual(self.act(admin, action, account_id, NOTE).status_code, 404)

    def test_an_actor_who_lost_admin_or_was_blocked_meanwhile_approves_nobody(self):
        """Covers: AC-account-approval-010."""
        actor = self.store.get_user(self.admin["id"])
        for change, reason in (("roles = '[\"user\"]'", "admin_required"), ("is_active = 0", "blocked")):
            self.execute("UPDATE users SET roles = '[\"admin\", \"user\"]', is_active = 1 WHERE id = ?", self.admin["id"])
            self.execute(f"UPDATE users SET {change} WHERE id = ?", self.admin["id"])
            for action in (self.accounts.approve, self.accounts.withdraw_approval):
                with self.subTest(reason), self.assertRaises(Exception) as caught:
                    action(actor=actor, user_id=self.ada["id"])
                self.assertEqual(caught.exception.reason, reason)
        self.assertEqual((self.approval(self.ada["id"]), self.history(self.ada["id"])), ((None, None), []))

    def test_the_development_user_may_and_is_named(self):
        """Covers: AC-account-approval-010."""
        self.store.enabled = False
        client = self.client()
        user = self.act(client, APPROVE, self.ada["id"]).json()["user"]
        self.assertEqual((user["approval"], user["approved_by"]), ("approved", "development"))
        self.assertEqual(client.get(f"/api/admin/users/{self.ada['id']}/history").json()["history"][0]["actor"], "development")

    def test_a_blocked_account_can_be_approved_and_stays_blocked(self):
        """Covers: AC-account-approval-010."""
        admin = self.signed_in("admin", "admin-password")
        self.assertEqual(self.act(admin, "block", self.ada["id"], "A test.").status_code, 200)
        user = self.act(admin, APPROVE, self.ada["id"]).json()["user"]
        self.assertEqual((user["approval"], user["blocked"]), ("approved", True))


class KeptTests(ApprovalTestCase):
    def test_who_when_and_the_note_are_in_the_history_and_the_audit_log(self):
        """Covers: AC-account-approval-011."""
        admin = self.signed_in("admin", "admin-password")
        for action, stored, audited, note in ((APPROVE, "approved", "admin.user_approved", NOTE), (WITHDRAW, "approval_withdrawn", "admin.approval_withdrawn", None),
                                              (APPROVE, "approved", "admin.user_approved", None), (WITHDRAW, "approval_withdrawn", "admin.approval_withdrawn", "Asked to leave.")):
            with self.subTest(action=action, note=note):
                history, audit = self.history(self.ada["id"]), self.rows("SELECT * FROM audit_events ORDER BY id")
                self.assertEqual(self.act(admin, action, self.ada["id"], note).status_code, 200)
                [row] = self.history(self.ada["id"])[len(history):]
                self.assertEqual((row["action"], row["actor_user_id"], row["actor_kind"], row["reason"]), (stored, self.admin["id"], "account", note))
                at, _ = self.approval(self.ada["id"])
                if action == APPROVE:
                    self.assertEqual(row["created_at"], at)
                [event] = self.rows("SELECT * FROM audit_events ORDER BY id")[len(audit):]
                self.assertEqual((event["action"], event["username"], event["status_code"]), (audited, "admin", 200))
                self.assertEqual(json.loads(event["request_payload"]), {"user_id": self.ada["id"], "reason": note})
        history = admin.get(f"/api/admin/users/{self.ada['id']}/history").json()["history"]
        self.assertEqual([(item["action"], item["actor"], item["reason"]) for item in history], [
            ("approval_withdrawn", "admin", "Asked to leave."), ("approved", "admin", None), ("approval_withdrawn", "admin", None), ("approved", "admin", NOTE)])

    def test_the_history_is_append_only(self):
        """Covers: AC-account-approval-011."""
        admin = self.signed_in("admin", "admin-password")
        self.act(admin, APPROVE, self.ada["id"], NOTE)
        for statement in ("UPDATE account_history SET reason = 'changed'", "DELETE FROM account_history"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.execute(statement)

    def test_a_note_is_trimmed_optional_and_has_at_most_500_characters(self):
        """Covers: AC-account-approval-012."""
        admin = self.signed_in("admin", "admin-password")
        for note, status, kept in (("  " + NOTE + "\n", 200, NOTE), ("   ", 200, None), ("x" * 500, 200, "x" * 500), ("x" * 501, 400, None)):
            with self.subTest(length=len(note)):
                before = (self.approval(self.ada["id"]), self.history(self.ada["id"]))
                response = self.act(admin, APPROVE, self.ada["id"], note)
                self.assertEqual(response.status_code, status, response.text)
                if status == 400:
                    self.assertEqual((self.approval(self.ada["id"]), self.history(self.ada["id"])), before)
                    self.assertNotIn("reason", response.json())
                else:
                    self.assertEqual(self.history(self.ada["id"])[-1]["reason"], kept)
                    self.assertEqual(self.act(admin, WITHDRAW, self.ada["id"]).status_code, 200)
        self.assertEqual(admin.post(f"/api/admin/users/{self.ada['id']}/{APPROVE}").status_code, 200)  # no body at all

    def test_what_changes_nothing_writes_nothing_and_loses_no_note(self):
        """Covers: AC-account-approval-012."""
        admin = self.signed_in("admin", "admin-password")
        self.make_admin_in_database("carol")
        carol = self.signed_in("carol")
        for action, client, approval, approver in ((WITHDRAW, admin, "waiting", None), (APPROVE, carol, "approved", "admin")):
            if action == APPROVE:
                self.act(admin, APPROVE, self.ada["id"], NOTE)
            before = [self.rows(f"SELECT * FROM {table} ORDER BY rowid") for table in ("users", "account_history", "audit_events")]
            for note in (None, "  "):
                user = self.act(client, action, self.ada["id"], note).json()["user"]
                self.assertEqual((user["approval"], user["approved_by"]), (approval, approver))  # the first approval stays
            refused = self.act(client, action, self.ada["id"], "Another note.")
            self.assertEqual((refused.status_code, refused.json()), (400, {"detail": UNCHANGED[action], "reason": "approval_unchanged"}))
            self.assertEqual([self.rows(f"SELECT * FROM {table} ORDER BY rowid") for table in ("users", "account_history", "audit_events")], before)
        self.assertEqual([row["reason"] for row in self.history(self.ada["id"])], [NOTE])

    def test_the_approval_and_its_history_commit_together(self):
        """Covers: AC-account-approval-011."""
        failing = self.client(raise_server_exceptions=False)
        self.login(failing, "admin", "admin-password")
        self.execute("CREATE TRIGGER history_unavailable BEFORE INSERT ON account_history BEGIN SELECT RAISE(ABORT, 'history unavailable'); END")
        audit = self.audit_actions()
        self.assertGreaterEqual(self.act(failing, APPROVE, self.ada["id"]).status_code, 500)
        self.assertEqual((self.approval(self.ada["id"]), self.audit_actions()), ((None, None), audit))

    def test_approvals_at_the_same_moment_leave_one_with_its_one_row(self):
        """Covers: AC-account-approval-013."""
        actor, barrier, results = self.store.get_user(self.admin["id"]), threading.Barrier(8), []

        def approve() -> None:
            barrier.wait()
            results.append(self.accounts.approve(actor=actor, user_id=self.ada["id"])[1])

        threads = [threading.Thread(target=approve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(sorted(results), [False] * 7 + [True])
        self.assertEqual([row["action"] for row in self.history(self.ada["id"])], ["approved"])

    def test_signing_in_again_keeps_the_approval_and_gives_none(self):
        """Covers: AC-account-approval-014."""
        claims = {"email": "new@example.org", "email_verified": True, "name": "Ada New", "approved_at": "x", "approval": "approved"}
        account, created = self.accounts.ensure_account(ISSUER, "subject-ada", claims)
        self.assertEqual((created, account["approved_at"]), (False, None))
        self.act(self.signed_in("admin", "admin-password"), APPROVE, self.ada["id"])
        approved = self.approval(self.ada["id"])
        account, _ = self.accounts.ensure_account(ISSUER, "subject-ada", claims)
        self.assertEqual((account["approved_by"], self.approval(self.ada["id"])), ("admin", approved))
        other, created = self.accounts.ensure_account(ISSUER, "subject-eve", claims)  # the same email is another account
        self.assertEqual((created, other["approved_at"]), (True, None))


class ListTests(ApprovalTestCase):
    def test_the_list_says_where_each_account_stands(self):
        """Covers: AC-account-approval-015."""
        owner = self.owner_client()
        ben = self.provider_account("subject-ben", preferred_username="ben")
        eve = self.provider_account("subject-eve", preferred_username="eve")
        self.act(owner, APPROVE, ben["id"], NOTE)
        self.act(owner, "grant-admin", eve["id"])
        code, out = run_cli("legacy", "link", "--account", str(self.bob["id"]), "--issuer", ISSUER, "--subject", "bob-subject", "--reason", "A test.")
        self.assertEqual(code, 0, out)
        users = owner.get("/api/admin/users").json()["users"]
        status = {user["username"]: (user["approval"], user["approved_by"], bool(user["approved_at"])) for user in users}
        self.assertEqual(status, {
            "admin": ("not_needed", None, False), "bob": ("waiting", None, False), "carol": ("not_needed", None, False),
            "ada": ("waiting", None, False), "ben": ("approved", "admin", True), "eve": ("not_needed", None, False),
        })
        by_name = {user["username"]: user for user in users}
        self.assertEqual(by_name["ben"]["approved_at"], self.approval(ben["id"])[0])
        # Only admins see it: the shape an account's own page gets has none of it.
        me = self.signed_in_as(ben["id"]).get("/api/auth/me").json()["user"]
        self.assertFalse({"approval", "approved_at", "approved_by"} & set(me))

    def test_a_renamed_approver_is_shown_by_the_name_it_has(self):
        """Covers: AC-account-approval-015."""
        self.make_admin_in_database("carol")
        self.act(self.signed_in("carol"), APPROVE, self.ada["id"])
        self.execute("UPDATE users SET username = 'caroline' WHERE id = ?", self.carol["id"])
        users = self.signed_in("admin", "admin-password").get("/api/admin/users").json()["users"]
        self.assertEqual(next(user for user in users if user["id"] == self.ada["id"])["approved_by"], "caroline")

    def test_an_approved_account_cannot_be_folded_any_more_and_a_link_gives_no_approval(self):
        """Covers: AC-account-approval-016.

        The rules of accounts-roles section 8 as they are: an approval is history, which keeps an account from being emptied.
        """
        self.bind_owner()
        self.act(self.signed_in("admin", "admin-password"), APPROVE, self.ada["id"])
        code, out = run_cli("legacy", "fold", "--from", str(self.ada["id"]), "--into", str(self.bob["id"]), "--reason", "A test.")
        self.assertEqual(code, 1)
        self.assertIn("history rows", out)
        code, out = run_cli("legacy", "link", "--account", str(self.bob["id"]), "--issuer", ISSUER, "--subject", "bob-subject", "--reason", "A test.")
        self.assertEqual((code, self.approval(self.bob["id"])), (0, (None, None)), out)


# ------------------------------------------------------------------------------------------------
# The migration
# ------------------------------------------------------------------------------------------------


class MigrationTests(AccountsTestCase):
    def test_its_text_is_settled(self):
        """Covers: AC-account-approval-017."""
        migration = next(item for item in storage.MIGRATIONS if item.id == MIGRATION)
        self.assertEqual((migration.kind, migration.checksum), ("sql", MIGRATION_SHA256))
        self.assertEqual(hashlib.sha256(migration.sql.encode("utf-8")).hexdigest(), MIGRATION_SHA256)
        self.assertEqual([item.id for item in storage.MIGRATIONS][4:6], ["0005_daily_decompositions", MIGRATION])

    def test_it_adds_two_empty_columns_and_the_previous_image_still_runs(self):
        """Covers: AC-account-approval-017."""
        old = self.dir / "de1a7af.sqlite3"
        previous_image.make_de1a7af_file(old)
        position = [item.id for item in storage.MIGRATIONS].index(MIGRATION)
        migration = storage.MIGRATIONS[position]
        del storage.MIGRATIONS[position:]
        storage.migrate(old)
        names = "SELECT name, sql FROM sqlite_master WHERE name <> 'users'"
        schema = self.rows(names, db=old)
        columns = self.rows("PRAGMA table_info(users)", db=old)
        tables = [row["name"] for row in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'", db=old)]
        before = {table: self.rows(f"SELECT rowid, * FROM {table}", db=old) for table in tables if table not in ("schema_migrations", "sqlite_sequence")}
        self.assertTrue(before["users"])
        storage.MIGRATIONS.append(migration)
        self.assertEqual(storage.migrate(old), [MIGRATION])
        self.assertEqual(self.rows(names, db=old), schema)
        added = self.rows("PRAGMA table_info(users)", db=old)
        self.assertEqual(added[: len(columns)], columns)
        self.assertEqual([(row["name"], row["type"], row["notnull"], row["dflt_value"]) for row in added[len(columns):]],
                         [("approved_at", "TEXT", 0, None), ("approved_by", "INTEGER", 0, None)])
        after = {table: self.rows(f"SELECT rowid, * FROM {table}", db=old) for table in before}
        self.assertEqual([(row.pop("approved_at"), row.pop("approved_by")) for row in after["users"]], [(None, None)] * len(before["users"]))
        self.assertEqual(after, before)
        self.assertEqual(previous_image.start(old), [])
        self.assertEqual(previous_image.replay(old, include=previous_image.is_read), [])
        fresh = self.dir / "fresh.sqlite3"
        storage.migrate(fresh)
        self.assertEqual(previous_image.replay(fresh), [], "everything the previous image writes and reads")

    def test_the_previous_image_keeps_an_approval_it_does_not_know(self):
        """Covers: AC-account-approval-017."""
        ada = self.provider_account("subject-ada", preferred_username="ada")
        self.assertEqual(self.act(self.signed_in("admin", "admin-password"), APPROVE, ada["id"]).status_code, 200)
        before = self.rows("SELECT approved_at, approved_by FROM users WHERE id = ?", ada["id"])
        self.assertEqual(previous_image.start(self.store.db_path), [])
        conn = previous_image.connect(self.store.db_path)
        try:
            self.assertFalse(previous_image.password_login(conn, "ada", "!"))
        finally:
            conn.close()
        self.assertEqual(self.rows("SELECT approved_at, approved_by FROM users WHERE id = ?", ada["id"]), before)
        self.assertEqual(AccountStore(self.store).list_accounts()[0]["approved_by"], "admin")


if __name__ == "__main__":
    unittest.main()
