"""A ROHub account decomposes once it is approved, on a development machine as anywhere (the owner, 2026-11-25).

People accept the terms by email for now and an admin's approval stands for that, so the application asks
nobody for them, and there is no daily cap unless one is switched on. What an approval is and who gives it
is in test_account_approval.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from increment1_support import run_cli, sign_in_as
from increment2_support import OidcTestCase
from test_records import PIPELINE_RESULT

from app.access.policy import AccessState, Capability, decide
from app.routers import decompose as decompose_router

DEFINITION = {"definition": "air temperature"}


class UncappedDecisionTests(unittest.TestCase):
    def test_no_daily_cap_allows_decomposing_and_says_no_number(self):
        """Covers: AC-account-approval-001."""
        decisions = decide(AccessState(account_id=1, email_verified=True, terms_accepted=True, uses_left_today=None))
        decompose = decisions[Capability.DECOMPOSE]
        self.assertEqual((decompose.allowed, decompose.reason, decompose.uses_left_today, decompose.resets_at), (True, None, None, None))
        self.assertTrue(decisions[Capability.SAVE_AND_REVIEW].allowed)
        for capability in (Capability.PUBLISH, Capability.RETRACT_OWN):
            self.assertEqual((decisions[capability].allowed, decisions[capability].reason), (False, "approval_required"))

    def test_a_cap_of_none_left_still_refuses(self):
        """Covers: AC-account-approval-001."""
        decompose = decide(AccessState(account_id=1, email_verified=True, terms_accepted=True, uses_left_today=0))[Capability.DECOMPOSE]
        self.assertEqual((decompose.allowed, decompose.reason), (False, "daily_limit"))

    def test_terms_a_block_and_an_unverified_email_come_first(self):
        """Covers: AC-account-approval-001."""
        for change, reason in (
            ({"terms_accepted": False}, "terms_not_accepted"), ({"blocked": True}, "blocked"), ({"email_verified": False}, "email_not_verified"),
        ):
            with self.subTest(reason):
                values = {"account_id": 1, "email_verified": True, "terms_accepted": True, "uses_left_today": None, **change}
                self.assertEqual(decide(AccessState(**values))[Capability.DECOMPOSE].reason, reason)


class DevelopmentMachineTests(OidcTestCase):
    def setUp(self):
        super().setUp()
        self.ada = self.signed_in(sub="subject-ada", email="ada@example.org", preferred_username="ada")
        self.account = next(row for row in self.accounts() if row["email"] == "ada@example.org")

    def decompose(self, browser, headers=None):
        headers = self.csrf_headers(browser) if headers is None else headers
        with patch.object(decompose_router, "run_pipeline", return_value=dict(PIPELINE_RESULT)) as pipeline:
            return browser.post("/api/decompose", json=DEFINITION, headers=headers), pipeline

    def approve(self):
        response = self.password_browser().post(f"/api/admin/users/{self.account['id']}/approve", json={})
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_rohub_account_waits_and_once_approved_decomposes_as_often_as_it_likes_and_may_publish(self):
        """Covers: AC-account-approval-002, AC-account-approval-003."""
        capabilities = self.me(self.ada)["capabilities"]
        allowed = {name for name, entry in capabilities.items() if entry["allowed"]}
        self.assertEqual(allowed, {"public_tools", "session", "account_data"})
        self.assertEqual(capabilities["publish"]["reason"], "approval_required")
        response, pipeline = self.decompose(self.ada)
        self.assertEqual((response.status_code, response.json()["reason"]), (403, "approval_required"))
        pipeline.assert_not_called()

        self.approve()
        capabilities = self.me(self.ada)["capabilities"]
        allowed = {name for name, entry in capabilities.items() if entry["allowed"]}
        self.assertEqual(allowed, {"public_tools", "session", "account_data", "decompose", "save_and_review", "publish", "retract_own"})
        self.assertEqual((capabilities["decompose"]["uses_left_today"], capabilities["decompose"]["resets_at"]), (None, None))
        for _ in range(4):  # more than the three a day of D-033
            response, pipeline = self.decompose(self.ada)
            self.assertEqual(response.status_code, 200, response.text)
            pipeline.assert_called_once()

    def test_the_owner_with_a_rohub_account_decomposes_and_may_publish(self):
        """Covers: AC-account-approval-005."""
        code, out = run_cli("owner", "bind", "--issuer", self.provider.issuer, "--subject", "subject-ada")
        self.assertEqual(code, 0, out)
        self.ada = self.signed_in(sub="subject-ada", email="ada@example.org", preferred_username="ada")  # owner bind ended the session (owner-sign-in, section 4)
        capabilities = self.me(self.ada)["capabilities"]
        self.assertTrue(capabilities["decompose"]["allowed"] and capabilities["publish"]["allowed"])
        self.assertEqual(self.decompose(self.ada)[0].status_code, 200)

    def test_a_blocked_account_does_not(self):
        """Covers: AC-account-approval-004."""
        self.approve()
        self.assertEqual(self.decompose(self.ada)[0].status_code, 200)
        admin = self.password_browser()
        blocked = admin.post(f"/api/admin/users/{self.account['id']}/block", json={"reason": "A test."})
        self.assertEqual(blocked.status_code, 200, blocked.text)
        self.assertIn(self.ada.post("/api/decompose", json=DEFINITION).status_code, (401, 403))

    def test_a_production_deployment_has_the_same_rule(self):
        """Covers: AC-account-approval-007."""
        self.settings(environment_raw="production", auth_mode_raw="local")
        browser = self.browser()
        sign_in_as(browser, self.account["id"])  # a session of its own, as no fake provider signs in in production
        response, pipeline = self.decompose(browser, headers={})
        self.assertEqual((response.status_code, response.json()["reason"]), (403, "approval_required"))
        pipeline.assert_not_called()
        self.approve()
        for _ in range(4):
            self.assertEqual(self.decompose(browser, headers={})[0].status_code, 200)


if __name__ == "__main__":
    unittest.main()
