"""access-policy: what a request may do (increment 1, tests gate G-018).

Behavioural tests of ``components/access-policy.md`` (approved under G-017) through its public
seams: the pure ``decide``, ``require`` with the access state replaced through FastAPI's
dependency overrides, ``ROUTE_CAPABILITIES``, ``GET /api/auth/me`` and the routes over real
sessions. Identity-provider accounts come from ``AccountStore.ensure_account`` with a synthetic
issuer and get their session from the auth store, as ``oidc-identity`` will give them one from
increment 2; nothing talks to a real provider, a model or a registry.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import socket
import sqlite3
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from increment1_support import BACKEND, ISSUER, TempDatabaseTestCase, run_cli, sign_in_as
from test_records import CLIENT, DRAFT, MATRIX, PIPELINE_RESULT

from app import main
from app.access.dependencies import ROUTE_CAPABILITIES, require
from app.access.policy import AccessState, Capability, decide
from app.access.state import access_state
from app.accounts import AccountStore
from app.routers import decompose as decompose_router
from app.routers import nanopub as nanopub_router

C = Capability
OWN_USE = (C.DECOMPOSE, C.SAVE_AND_REVIEW, C.PUBLISH, C.RETRACT_OWN)
ADMIN = (C.REVIEW_APPLICATIONS, C.MANAGE_USERS, C.VIEW_ACTIVITY, C.RETRACT_ANY, C.EXPORT_RESEARCH)
KEYS = {
    "public_tools", "session", "account_data", "decompose", "save_and_review", "publish", "retract_own",
    "review_applications", "manage_users", "view_activity", "retract_any", "export_research", "manage_admins",
}
MESSAGE = {
    "sign_in_required": "Authentication required.",
    "blocked": "This account is blocked.",
    "email_not_verified": "Verify your email address to use this account.",
    "terms_not_accepted": "Accept the current terms to continue.",
    "admin_required": "Admin access required.",
    "owner_required": "Only the owner can do this.",
    "daily_limit": "No decompositions left today.",
}
APPROVAL_MESSAGE = {C.PUBLISH: "Publishing needs an approved account.", C.RETRACT_OWN: "Ask an admin to retract this publication."}
APPROVAL_FIRST = {
    **{capability: ("approval_required", "An admin must approve your account first.") for capability in OWN_USE},
    C.RETRACT_OWN: ("approval_required", APPROVAL_MESSAGE[C.RETRACT_OWN]),
}
RECORDS_DISABLED = "Records need accounts, and authentication is disabled."
DISABLED_MESSAGE = {
    C.ACCOUNT_DATA: "Saved ORCID iDs need accounts, and authentication is disabled.",
    C.SAVE_AND_REVIEW: RECORDS_DISABLED,
    C.EXPORT_RESEARCH: RECORDS_DISABLED,
    C.MANAGE_ADMINS: "The owner needs accounts, and authentication is disabled.",
}
RESETS = datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc)
ORCID = "https://orcid.org/0000-0002-1825-0097"
NANOPUB = "https://w3id.org/np/RAexampleAccessPolicyNanopub"
NOT_ACCOUNT_BACKED = dict(account_id=0, account_backed=False)

# Section 5's table: every route of the application with its capability; None is public.
EXPECTED_ROUTES: Dict[Tuple[str, str], Optional[Capability]] = {
    ("GET", "/api/livez"): None,
    ("GET", "/api/readyz"): None,
    ("GET", "/api/health"): None,
    ("POST", "/api/auth/login"): None,
    # oidc-identity, section 10 (increment 2).
    ("GET", "/api/auth/login"): None,
    ("GET", "/api/auth/callback"): None,
    ("GET", "/api/auth/end-session"): None,
    ("POST", "/api/auth/backchannel-logout"): None,
    ("GET", "/api/auth/verify/admin"): C.VIEW_ACTIVITY,
    ("GET", "/api/model-options"): C.PUBLIC_TOOLS,
    ("GET", "/api/nanopub/preparation-options"): C.PUBLIC_TOOLS,
    ("GET", "/api/auth/verify"): C.SESSION,
    ("GET", "/api/auth/me"): C.SESSION,
    ("POST", "/api/auth/logout"): C.SESSION,
    ("POST", "/api/events"): C.SESSION,
    ("GET", "/api/docs"): C.SESSION,
    ("GET", "/api/redoc"): C.SESSION,
    ("GET", "/api/openapi.json"): C.SESSION,
    ("GET", "/api/auth/me/orcids"): C.ACCOUNT_DATA,
    ("POST", "/api/auth/me/orcids"): C.ACCOUNT_DATA,
    ("DELETE", "/api/auth/me/orcids/{orcid_id}"): C.ACCOUNT_DATA,
    ("POST", "/api/decompose"): C.DECOMPOSE,
    ("POST", "/api/decompose/stream"): C.DECOMPOSE,
    ("POST", "/api/records"): C.SAVE_AND_REVIEW,
    ("POST", "/api/records/{record_id}/events"): C.SAVE_AND_REVIEW,
    ("GET", "/api/records/{record_id}"): C.SAVE_AND_REVIEW,
    ("GET", "/api/admin/stats"): C.VIEW_ACTIVITY,
    ("GET", "/api/admin/audit"): C.VIEW_ACTIVITY,
    ("POST", "/api/nanopub/publish"): C.PUBLISH,
    ("POST", "/api/nanopub/retract"): C.RETRACT_OWN,
    ("GET", "/api/admin/records/export"): C.EXPORT_RESEARCH,
    ("GET", "/api/admin/users"): C.MANAGE_USERS,
    ("POST", "/api/admin/users"): C.MANAGE_USERS,
    ("PATCH", "/api/admin/users/{user_id}"): C.MANAGE_USERS,
    ("GET", "/api/admin/users/{user_id}/history"): C.MANAGE_USERS,
    ("POST", "/api/admin/users/{user_id}/block"): C.MANAGE_USERS,
    ("POST", "/api/admin/users/{user_id}/unblock"): C.MANAGE_USERS,
    # The approval of accounts (the owner, 2026-11-25).
    ("POST", "/api/admin/users/{user_id}/approve"): C.REVIEW_APPLICATIONS,
    ("POST", "/api/admin/users/{user_id}/withdraw-approval"): C.REVIEW_APPLICATIONS,
    ("POST", "/api/admin/users/{user_id}/grant-admin"): C.MANAGE_ADMINS,
    ("POST", "/api/admin/users/{user_id}/revoke-admin"): C.MANAGE_ADMINS,
}
PATH_VALUES = {"{user_id}": "1", "{record_id}": "0123456789abcdef", "{orcid_id}": "0000-0002-1825-0097"}


def state(**changes: Any) -> AccessState:
    """A signed-in, verified, approved ordinary account with current terms; ``changes`` narrow it."""
    values: Dict[str, Any] = dict(
        account_id=7, account_backed=True, role="user", blocked=False, email_verified=True,
        terms_accepted=True, application="approved", uses_left_today=3, resets_at=RESETS,
    )
    values.update(changes)
    return AccessState(**values)


def fill(path: str) -> str:
    for placeholder, value in PATH_VALUES.items():
        path = path.replace(placeholder, value)
    return path


def declared(route: APIRoute) -> List[Capability]:
    """The capabilities a route's dependencies check (``require`` marks its dependency with it)."""
    found: List[Capability] = []

    def walk(dependant: Any) -> None:
        for dependency in dependant.dependencies:
            capability = getattr(dependency.call, "capability", None)
            if capability is not None:
                found.append(capability)
            walk(dependency)

    walk(route.dependant)
    return found


class DecisionAssertions:
    def assert_decisions(self, decisions: Dict[Capability, Any], allowed: Iterable[Capability],
                         refused: Dict[Capability, Tuple[str, str]]) -> None:
        """Every capability is decided; those in ``allowed`` are granted, the others refused as given."""
        allowed = tuple(allowed)
        self.assertEqual(set(decisions), set(Capability))  # type: ignore[attr-defined]
        self.assertEqual(set(allowed) | set(refused), set(Capability), "the test states every capability")  # type: ignore[attr-defined]
        for capability in allowed:
            decision = decisions[capability]
            self.assertEqual((decision.allowed, decision.reason, decision.message), (True, None, None), capability)  # type: ignore[attr-defined]
        for capability, (reason, message) in refused.items():
            decision = decisions[capability]
            self.assertEqual((decision.allowed, decision.reason, decision.message), (False, reason, message), capability)  # type: ignore[attr-defined]


def every_but(allowed: Iterable[Capability], reason: str, message: Optional[str] = None) -> Dict[Capability, Tuple[str, str]]:
    allowed = set(allowed)
    return {capability: (reason, message or MESSAGE[reason]) for capability in Capability if capability not in allowed}


# ------------------------------------------------------------------------------------------------
# Sections 2 to 4: the pure decision
# ------------------------------------------------------------------------------------------------


class DecisionTests(DecisionAssertions, unittest.TestCase):
    def test_decide_imports_only_the_standard_library_and_no_clock_database_settings_or_network(self):
        """Covers: AC-access-policy-001 (in the source)."""
        tree = ast.parse((BACKEND / "app" / "access" / "policy.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add("<relative import>" if node.level else (node.module or "").split(".")[0])
        self.assertLessEqual(imported, set(sys.stdlib_module_names) | {"__future__"})
        io_and_clock = {
            "time", "os", "sys", "socket", "sqlite3", "urllib", "http", "ssl", "subprocess", "asyncio", "random",
            "secrets", "uuid", "pathlib", "shutil", "io", "zoneinfo", "threading", "multiprocessing", "tempfile",
            "select", "selectors", "socketserver", "dbm", "shelve",
        }
        self.assertEqual(imported & io_and_clock, set())
        called = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        called |= {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertEqual(called & {"now", "utcnow", "today", "monotonic", "perf_counter", "time_ns", "getenv", "open"}, set())

    def test_equal_states_give_equal_decisions_without_any_outside_input(self):
        """Covers: AC-access-policy-001 (at run time)."""
        states = [
            state(account_id=None), state(blocked=True, role="owner"), state(email_verified=False),
            state(terms_accepted=False, role="admin"), state(application="pending", uses_left_today=0),
            state(application="none", uses_left_today=1), state(role="admin"), state(role="owner"),
            state(**NOT_ACCOUNT_BACKED, role="admin"), state(email_verified=None),
        ]

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("decide read the clock, a database, the network or the environment")

        with (
            patch.object(time, "time", forbidden), patch.object(time, "monotonic", forbidden),
            patch.object(sqlite3, "connect", forbidden), patch.object(socket, "socket", forbidden),
            patch.object(os, "getenv", forbidden),
        ):
            first = [decide(item) for item in states]
            second = [decide(dataclasses.replace(item)) for item in states]
        self.assertEqual(first, second)

    def test_without_an_account_only_the_public_tools(self):
        """Covers: AC-access-policy-002."""
        for role in ("user", "admin", "owner"):
            with self.subTest(role=role):
                self.assert_decisions(decide(state(account_id=None, role=role)), [C.PUBLIC_TOOLS],
                                      every_but([C.PUBLIC_TOOLS], "sign_in_required"))

    def test_a_blocked_account_of_any_role_keeps_only_the_public_tools(self):
        """Covers: AC-access-policy-003."""
        for role in ("user", "admin", "owner"):
            with self.subTest(role=role):
                self.assert_decisions(decide(state(blocked=True, role=role)), [C.PUBLIC_TOOLS],
                                      every_but([C.PUBLIC_TOOLS], "blocked"))

    def test_an_unverified_email_keeps_only_the_public_tools(self):
        """Covers: AC-access-policy-004, AC-access-policy-018 (false is refused)."""
        for role in ("user", "admin", "owner"):
            with self.subTest(role=role):
                self.assert_decisions(decide(state(email_verified=False, role=role)), [C.PUBLIC_TOOLS],
                                      every_but([C.PUBLIC_TOOLS], "email_not_verified"))

    def test_an_unknown_email_counts_as_verified(self):
        """Covers: AC-access-policy-018."""
        for role in ("user", "admin", "owner"):
            with self.subTest(role=role):
                unknown, verified = decide(state(email_verified=None, role=role)), decide(state(email_verified=True, role=role))
                self.assertEqual(unknown, verified)
                self.assertTrue(unknown[C.DECOMPOSE].allowed)

    def test_without_current_terms_only_the_session_account_data_and_admin_rights_remain(self):
        """Covers: AC-access-policy-005."""
        for role, application in (("user", "approved"), ("user", "none"), ("admin", "none"), ("owner", "none")):
            with self.subTest(role=role, application=application):
                admin_side: Dict[Capability, Tuple[str, str]]
                if role == "user":
                    allowed = [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA]
                    admin_side = {capability: ("admin_required", MESSAGE["admin_required"]) for capability in ADMIN}
                    admin_side[C.MANAGE_ADMINS] = ("owner_required", MESSAGE["owner_required"])
                elif role == "admin":
                    allowed = [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA, *ADMIN]
                    admin_side = {C.MANAGE_ADMINS: ("owner_required", MESSAGE["owner_required"])}
                else:
                    allowed = [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA, *ADMIN, C.MANAGE_ADMINS]
                    admin_side = {}
                refused = {capability: ("terms_not_accepted", MESSAGE["terms_not_accepted"]) for capability in OWN_USE}
                self.assert_decisions(decide(state(terms_accepted=False, role=role, application=application)),
                                      allowed, {**refused, **admin_side})

    def test_approved_accounts_admins_and_the_owner_use_everything_without_a_cap(self):
        """Covers: AC-access-policy-006."""
        for role, application in (("user", "approved"), ("admin", "none"), ("admin", "rejected"), ("owner", "none")):
            for uses_left in (0, 1, 3):
                with self.subTest(role=role, application=application, uses_left=uses_left):
                    decisions = decide(state(role=role, application=application, uses_left_today=uses_left))
                    for capability in (C.SESSION, C.ACCOUNT_DATA, *OWN_USE):
                        self.assertTrue(decisions[capability].allowed, capability)
                    self.assertIsNone(decisions[C.DECOMPOSE].uses_left_today)
                    self.assertIsNone(decisions[C.DECOMPOSE].resets_at)

    def test_accounts_that_are_not_approved_have_a_daily_cap_and_cannot_publish(self):
        """Covers: AC-access-policy-007."""
        for application in ("none", "pending", "rejected", "withdrawn"):
            with self.subTest(application=application, uses_left=1):
                decisions = decide(state(application=application, uses_left_today=1))
                self.assertEqual((decisions[C.DECOMPOSE].allowed, decisions[C.DECOMPOSE].uses_left_today,
                                  decisions[C.DECOMPOSE].resets_at), (True, 1, RESETS))
                admin_side = {capability: ("admin_required", MESSAGE["admin_required"]) for capability in ADMIN}
                admin_side[C.MANAGE_ADMINS] = ("owner_required", MESSAGE["owner_required"])
                self.assert_decisions(
                    decisions,
                    [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA, C.DECOMPOSE, C.SAVE_AND_REVIEW],
                    {C.PUBLISH: ("approval_required", APPROVAL_MESSAGE[C.PUBLISH]),
                     C.RETRACT_OWN: ("approval_required", APPROVAL_MESSAGE[C.RETRACT_OWN]), **admin_side},
                )
            with self.subTest(application=application, uses_left=0):
                decision = decide(state(application=application, uses_left_today=0))[C.DECOMPOSE]
                self.assertEqual((decision.allowed, decision.reason, decision.message, decision.resets_at),
                                 (False, "daily_limit", MESSAGE["daily_limit"], RESETS))

    def test_administration_follows_the_role(self):
        """Covers: AC-access-policy-008."""
        for role, granted, owner_only in (("user", False, False), ("admin", True, False), ("owner", True, True)):
            with self.subTest(role=role):
                decisions = decide(state(role=role))
                for capability in ADMIN:
                    expected = (True, None, None) if granted else (False, "admin_required", MESSAGE["admin_required"])
                    self.assertEqual((decisions[capability].allowed, decisions[capability].reason, decisions[capability].message),
                                     expected, capability)
                expected = (True, None, None) if owner_only else (False, "owner_required", MESSAGE["owner_required"])
                manage_admins = decisions[C.MANAGE_ADMINS]
                self.assertEqual((manage_admins.allowed, manage_admins.reason, manage_admins.message), expected)

    def test_the_development_user_is_an_approved_admin_without_account_features(self):
        """Covers: AC-access-policy-009."""
        allowed = [C.PUBLIC_TOOLS, C.SESSION, C.DECOMPOSE, C.PUBLISH, C.RETRACT_OWN, C.REVIEW_APPLICATIONS,
                   C.MANAGE_USERS, C.VIEW_ACTIVITY, C.RETRACT_ANY]
        refused = {capability: ("accounts_disabled", message) for capability, message in DISABLED_MESSAGE.items()}
        for name, inputs in (
            ("permissive inputs", state(**NOT_ACCOUNT_BACKED, role="admin")),
            ("restrictive inputs", state(**NOT_ACCOUNT_BACKED, role="user", terms_accepted=False, application="none", uses_left_today=0)),
        ):
            with self.subTest(inputs=name):
                decisions = decide(inputs)
                self.assert_decisions(decisions, allowed, refused)
                self.assertIsNone(decisions[C.DECOMPOSE].uses_left_today)


# ------------------------------------------------------------------------------------------------
# Sections 5 and 6: routes, require() and refusals over HTTP
# ------------------------------------------------------------------------------------------------


class RouteTableTests(unittest.TestCase):
    def test_every_route_declares_exactly_its_one_capability(self):
        """Covers: AC-access-policy-010, AC-oidc-identity-043 (with the rows of its section 10)."""
        routes = {(method, route.path): route for route in main.app.routes if isinstance(route, APIRoute) for method in route.methods}
        self.assertEqual(sorted(set(routes) - set(EXPECTED_ROUTES)), [], "routes without a capability")
        self.assertEqual(sorted(set(EXPECTED_ROUTES) - set(routes)), [], "routes of the contract that are missing")
        self.assertEqual(dict(ROUTE_CAPABILITIES), EXPECTED_ROUTES)
        for key, capability in EXPECTED_ROUTES.items():
            with self.subTest(route=key):
                found = declared(routes[key])
                if capability is None:
                    self.assertEqual(found, [])
                elif capability is C.PUBLIC_TOOLS:
                    self.assertIn(found, ([], [C.PUBLIC_TOOLS]))
                else:
                    self.assertEqual(found, [capability])


class RequireTests(unittest.TestCase):
    """``require`` against a stubbed access state: also the reasons no account can reach in increment 1."""

    def probe(self, stub: AccessState) -> TestClient:
        app = FastAPI()
        for capability in Capability:
            app.add_api_route(f"/probe/{capability.value}", lambda: {"ok": True}, methods=["GET"],
                              dependencies=[Depends(require(capability))])
        app.dependency_overrides[access_state] = lambda: stub
        return TestClient(app)

    def refusal(self, stub: AccessState, capability: Capability) -> Any:
        return self.probe(stub).get(f"/probe/{capability.value}")

    def test_require_carries_its_capability(self):
        """Covers: AC-access-policy-010 (the route table's dependencies name their capability)."""
        for capability in Capability:
            with self.subTest(capability=capability.value):
                self.assertEqual(getattr(require(capability), "capability", None), capability)

    def test_every_reason_becomes_its_status_and_body(self):
        """Covers: AC-access-policy-011."""
        cases = [
            (state(account_id=None), C.SESSION, 401, {"detail": "Authentication required."}),
            (state(blocked=True), C.SESSION, 403, {"detail": MESSAGE["blocked"], "reason": "blocked"}),
            (state(email_verified=False), C.ACCOUNT_DATA, 403, {"detail": MESSAGE["email_not_verified"], "reason": "email_not_verified"}),
            (state(terms_accepted=False), C.DECOMPOSE, 403, {"detail": MESSAGE["terms_not_accepted"], "reason": "terms_not_accepted"}),
            (state(application="pending"), C.PUBLISH, 403, {"detail": APPROVAL_MESSAGE[C.PUBLISH], "reason": "approval_required"}),
            (state(application="withdrawn"), C.RETRACT_OWN, 403, {"detail": APPROVAL_MESSAGE[C.RETRACT_OWN], "reason": "approval_required"}),
            (state(), C.MANAGE_USERS, 403, {"detail": MESSAGE["admin_required"], "reason": "admin_required"}),
            (state(role="admin"), C.MANAGE_ADMINS, 403, {"detail": MESSAGE["owner_required"], "reason": "owner_required"}),
            (state(application="none", uses_left_today=0, resets_at=None), C.DECOMPOSE, 429,
             {"detail": MESSAGE["daily_limit"], "reason": "daily_limit", "resets_at": None}),
        ]
        cases += [
            (state(**NOT_ACCOUNT_BACKED, role="admin"), capability, 409, {"detail": message, "reason": "accounts_disabled"})
            for capability, message in DISABLED_MESSAGE.items()
        ]
        for stub, capability, status, body in cases:
            with self.subTest(reason=body.get("reason", "sign_in_required"), capability=capability.value):
                response = self.refusal(stub, capability)
                self.assertEqual((response.status_code, response.json()), (status, body))
                self.assertNotIn("retry-after", response.headers)

    def test_the_daily_limit_says_when_to_try_again(self):
        """Covers: AC-access-policy-011 (429 with Retry-After and resets_at)."""
        resets_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        response = self.refusal(state(application="none", uses_left_today=0, resets_at=resets_at), C.DECOMPOSE)
        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual((body["detail"], body["reason"]), (MESSAGE["daily_limit"], "daily_limit"))
        self.assertEqual(datetime.fromisoformat(body["resets_at"]), resets_at)
        self.assertTrue(3500 <= int(response.headers["retry-after"]) <= 3601, response.headers["retry-after"])

    def test_an_allowed_capability_reaches_the_route(self):
        """Covers: AC-access-policy-011 (no refusal when the decision allows)."""
        for stub, capability in ((state(account_id=None), C.PUBLIC_TOOLS), (state(), C.DECOMPOSE),
                                 (state(role="owner"), C.MANAGE_ADMINS), (state(**NOT_ACCOUNT_BACKED, role="admin"), C.VIEW_ACTIVITY)):
            with self.subTest(capability=capability.value):
                response = self.refusal(stub, capability)
                self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))


# ------------------------------------------------------------------------------------------------
# The adapter and the routes over real sessions
# ------------------------------------------------------------------------------------------------


class AccessHttpTestCase(TempDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.store.init()
        self.bob = self.store.create_user(username="bob", password="password-123", display_name="Bob Example", email="bob@example.org")
        self.carol = self.store.create_user(username="carol", password="password-123")
        self.accounts = AccountStore(self.store)
        self.addCleanup(main.app.dependency_overrides.pop, access_state, None)

    def stub(self, stubbed: AccessState) -> None:
        main.app.dependency_overrides[access_state] = lambda: stubbed

    def me(self, client: TestClient) -> Dict[str, Any]:
        response = client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def capabilities(self, client: TestClient) -> Dict[str, Dict[str, Any]]:
        return self.me(client)["capabilities"]

    def assert_capabilities(self, capabilities: Dict[str, Dict[str, Any]], allowed: Iterable[Capability],
                            refused: Dict[Capability, Tuple[str, str]]) -> None:
        self.assertEqual(set(capabilities), KEYS)
        for capability in allowed:
            entry = capabilities[capability.value]
            self.assertEqual((entry["allowed"], entry["reason"], entry["message"]), (True, None, None), capability)
        for capability, (reason, message) in refused.items():
            entry = capabilities[capability.value]
            self.assertEqual((entry["allowed"], entry["reason"], entry["message"]), (False, reason, message), capability)

    def bind_owner(self, username: str = "admin") -> None:
        code, out = run_cli("owner", "bind", "--username", username)
        self.assertEqual(code, 0, out)

    def provider_account(self, subject: str, **claims: Any) -> Dict[str, Any]:
        values = {"email": f"{subject}@example.org", "email_verified": True, "preferred_username": subject, **claims}
        account, created = self.accounts.ensure_account(ISSUER, subject, values)
        self.assertTrue(created)
        return account

    def decompose(self, client: TestClient) -> Any:
        with patch.object(decompose_router, "run_pipeline", return_value=dict(PIPELINE_RESULT)):
            return client.post("/api/decompose", json={"definition": "dissolved oxygen in water", "client": CLIENT})

    def publish(self, client: TestClient, record_id: str) -> Any:
        nanopub = MagicMock()
        nanopub.publish.return_value = (NANOPUB, "https://registry.example.org")
        with (
            patch.object(nanopub_router, "get_nanopub_profile", return_value=MagicMock()),
            patch.object(nanopub_router, "get_nanopub_agent_uri", return_value=None),
            patch.object(nanopub_router, "NanopubConf", return_value=MagicMock()),
            patch.object(nanopub_router, "Nanopub", return_value=nanopub),
            patch.object(nanopub_router, "add_nanopub_metadata"),
        ):
            return client.post("/api/nanopub/publish", json={
                "ttl": DRAFT, "creator_orcid_id": "0000-0002-1825-0097", "record_id": record_id, "client": CLIENT,
            })

    def retract(self, client: TestClient) -> Any:
        retraction = MagicMock()
        retraction.publish.return_value = ("https://w3id.org/np/RAexampleRetraction", "https://registry.example.org")
        with (
            patch.object(nanopub_router, "normalize_target_nanopub_uri", side_effect=lambda uri: uri),
            patch.object(nanopub_router, "get_nanopub_profile", return_value=MagicMock()),
            patch.object(nanopub_router, "assert_retraction_allowed"),
            patch.object(nanopub_router, "build_retraction_nanopub", return_value=retraction),
        ):
            return client.post("/api/nanopub/retract", json={"nanopub_uri": NANOPUB, "creator_orcid_id": "0000-0002-1825-0097"})

    def start_record(self, client: TestClient) -> str:
        response = client.post("/api/records", json={"turtle": DRAFT, "client": CLIENT})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["record_id"]


class LegacyAndProviderAccountTests(AccessHttpTestCase):
    def test_a_legacy_local_account_keeps_everything_it_does_today(self):
        """Covers: AC-access-policy-012 (D-045), AC-access-policy-018 (its unknown email counts as verified)."""
        client = self.client()
        self.login(client, "bob")
        capabilities = self.capabilities(client)
        admin_side = {capability: ("admin_required", MESSAGE["admin_required"]) for capability in ADMIN}
        admin_side[C.MANAGE_ADMINS] = ("owner_required", MESSAGE["owner_required"])
        self.assert_capabilities(capabilities, [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA, *OWN_USE], admin_side)
        self.assertEqual((capabilities["decompose"]["uses_left_today"], capabilities["decompose"]["resets_at"]), (None, None))

        for _ in range(4):  # more than the daily allowance of D-033: legacy accounts have no cap
            self.assertEqual(self.decompose(client).status_code, 200)
        record_id = self.start_record(client)
        steps = (
            {"type": "check", "version": 0, "changes": [{"operation": "confirm", "role": "hasMatrix", "component": MATRIX}]},
            {"type": "mark_best", "version": 0, "unchecked_components": 0},
        )
        for body in steps:
            self.assertEqual(client.post(f"/api/records/{record_id}/events", json={**body, "client": CLIENT}).status_code, 201)
        published = self.publish(client, record_id)
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(self.retract(client).status_code, 200)
        self.assertEqual(client.post("/api/auth/me/orcids", json={"orcid": ORCID}).status_code, 200)
        self.assertEqual(len(self.rows("SELECT * FROM record_items WHERE user_id = ?", self.bob["id"])), 5)

    def test_an_identity_provider_account_without_onboarding_data_has_only_its_session_and_data(self):
        """Covers: AC-access-policy-012 (the most restrictive interim values), AC-access-policy-005."""
        account = self.provider_account("subject-1")
        client = self.client()
        sign_in_as(client, account["id"])
        admin_side = {capability: ("admin_required", MESSAGE["admin_required"]) for capability in ADMIN}
        admin_side[C.MANAGE_ADMINS] = ("owner_required", MESSAGE["owner_required"])
        self.assert_capabilities(self.capabilities(client), [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA], {**APPROVAL_FIRST, **admin_side})

        refusal = {"detail": APPROVAL_FIRST[C.DECOMPOSE][1], "reason": "approval_required"}
        with patch.object(decompose_router, "run_pipeline", side_effect=AssertionError("Unexpected model call")) as pipeline:
            response = client.post("/api/decompose", json={"definition": "air temperature"})
        self.assertEqual((response.status_code, response.json()), (403, refusal))
        pipeline.assert_not_called()
        response = client.post("/api/records", json={"turtle": DRAFT})
        self.assertEqual((response.status_code, response.json()), (403, refusal))
        self.assertEqual(client.get("/api/auth/me/orcids").status_code, 200)
        self.assertEqual(self.rows("SELECT * FROM record_items"), [])

    def test_an_identity_provider_admin_keeps_the_admin_capabilities_of_its_role(self):
        """Covers: AC-access-policy-012 (such an admin keeps its role's capabilities), AC-access-policy-005.

        An admin counts as approved, and nobody is asked for the terms here (the owner, 2026-11-25).
        """
        account = self.provider_account("subject-2")
        self.bind_owner("admin")
        owner = self.client()
        self.login(owner, "admin", "admin-password")
        self.assertEqual(owner.post(f"/api/admin/users/{account['id']}/grant-admin", json={}).status_code, 200)

        client = self.client()
        sign_in_as(client, account["id"])
        self.assert_capabilities(self.capabilities(client), [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA, *OWN_USE, *ADMIN],
                                 {C.MANAGE_ADMINS: ("owner_required", MESSAGE["owner_required"])})
        self.assertEqual(client.get("/api/admin/users").status_code, 200)
        self.assertEqual(self.decompose(client).status_code, 200)

        self.assertEqual(owner.post(f"/api/admin/users/{account['id']}/revoke-admin", json={}).status_code, 200)
        self.assertEqual(client.post("/api/decompose", json={"definition": "air temperature"}).status_code, 403)

    def test_a_legacy_account_linked_before_the_cutover_keeps_its_session_and_ids_but_not_its_tools(self):
        """Covers: AC-access-policy-012 (section 2: a linked legacy account loses decomposing, recording, publishing)."""
        client = self.client()
        self.login(client, "bob")
        self.store.save_orcid(self.bob["id"], ORCID)
        self.bind_owner("admin")  # the cutover commands run with an owner bound (D-045)
        code, out = run_cli("legacy", "link", "--account", str(self.bob["id"]), "--issuer", ISSUER,
                            "--subject", "bob-subject", "--reason", "Linked for the cutover test.")
        self.assertEqual(code, 0, out)

        admin_side = {capability: ("admin_required", MESSAGE["admin_required"]) for capability in ADMIN}
        admin_side[C.MANAGE_ADMINS] = ("owner_required", MESSAGE["owner_required"])
        self.assert_capabilities(self.capabilities(client), [C.PUBLIC_TOOLS, C.SESSION, C.ACCOUNT_DATA], {**APPROVAL_FIRST, **admin_side})
        self.assertEqual([item["orcid"] for item in client.get("/api/auth/me/orcids").json()["orcids"]], [ORCID])

    def test_an_identity_provider_account_with_a_false_email_claim_keeps_only_the_public_tools(self):
        """Covers: AC-access-policy-004, AC-access-policy-018 (the adapter reads email_verified)."""
        account = self.provider_account("subject-3", email_verified=False)
        client = self.client()
        sign_in_as(client, account["id"])
        response = client.get("/api/auth/me")
        self.assertEqual((response.status_code, response.json()),
                         (403, {"detail": MESSAGE["email_not_verified"], "reason": "email_not_verified"}))
        self.assertEqual(client.get("/api/model-options").status_code, 200)


class SessionChangeTests(AccessHttpTestCase):
    def test_a_grant_a_revocation_and_a_block_reach_an_existing_session_at_once(self):
        """Covers: AC-access-policy-013."""
        self.bind_owner("admin")
        owner = self.client()
        self.login(owner, "admin", "admin-password")
        bob = self.client()
        self.login(bob, "bob")
        self.assertEqual(bob.get("/api/admin/users").status_code, 403)

        self.assertEqual(owner.post(f"/api/admin/users/{self.bob['id']}/grant-admin", json={}).status_code, 200)
        self.assertEqual(bob.get("/api/admin/users").status_code, 200)

        self.assertEqual(owner.post(f"/api/admin/users/{self.bob['id']}/revoke-admin", json={}).status_code, 200)
        response = bob.get("/api/admin/users")
        self.assertEqual((response.status_code, response.json()),
                         (403, {"detail": MESSAGE["admin_required"], "reason": "admin_required"}))

        self.assertEqual(owner.post(f"/api/admin/users/{self.bob['id']}/block", json={"reason": "Test block."}).status_code, 200)
        self.assertEqual(bob.get("/api/auth/me").status_code, 401)


class MeTests(AccessHttpTestCase):
    PUBLIC_FIELDS = ("id", "username", "display_name", "email", "roles", "is_active", "auth_provider", "external_subject")

    def assert_me(self, body: Dict[str, Any], username: str, role: str) -> None:
        self.assertIs(body["auth_enabled"], True)
        user = body["user"]
        self.assertTrue(set(self.PUBLIC_FIELDS) <= set(user), user)
        self.assertEqual((user["username"], user["role"]), (username, role))
        capabilities = body["capabilities"]
        self.assertEqual(set(capabilities), KEYS)
        for key, entry in capabilities.items():
            with self.subTest(username=username, capability=key):
                self.assertIsInstance(entry["allowed"], bool)
                if entry["allowed"]:
                    self.assertEqual((entry["reason"], entry["message"]), (None, None))
                else:
                    self.assertIsInstance(entry["reason"], str)
                    self.assertIsInstance(entry["message"], str)
        self.assertEqual((capabilities["decompose"]["uses_left_today"], capabilities["decompose"]["resets_at"]), (None, None))

    def test_me_reports_the_user_with_its_role_and_every_capability(self):
        """Covers: AC-access-policy-014."""
        bob, admin = self.client(), self.client()
        self.login(bob, "bob")
        self.login(admin, "admin", "admin-password")
        self.assert_me(self.me(bob), "bob", "user")
        self.assert_me(self.me(admin), "admin", "admin")
        self.bind_owner("admin")
        self.assert_me(self.me(admin), "admin", "owner")

    def test_me_reports_the_uses_left_under_a_daily_cap(self):
        """Covers: AC-access-policy-014 (uses_left_today and resets_at with a cap)."""
        client = self.client()
        self.login(client, "bob")
        self.stub(state(account_id=self.bob["id"], application="none", uses_left_today=2, resets_at=RESETS))
        decompose = self.capabilities(client)["decompose"]
        self.assertEqual((decompose["allowed"], decompose["reason"], decompose["message"], decompose["uses_left_today"]),
                         (True, None, None, 2))
        self.assertEqual(datetime.fromisoformat(decompose["resets_at"]), RESETS)

    def test_the_development_user_is_refused_account_features_with_409(self):
        """Covers: AC-access-policy-009 (over HTTP, authentication disabled)."""
        self.store.enabled = False
        client = self.client()
        self.assert_capabilities(
            self.capabilities(client),
            [C.PUBLIC_TOOLS, C.SESSION, C.DECOMPOSE, C.PUBLISH, C.RETRACT_OWN, C.REVIEW_APPLICATIONS,
             C.MANAGE_USERS, C.VIEW_ACTIVITY, C.RETRACT_ANY],
            {capability: ("accounts_disabled", message) for capability, message in DISABLED_MESSAGE.items()},
        )
        for method, path, body, capability in (
            ("GET", "/api/auth/me/orcids", None, C.ACCOUNT_DATA),
            ("POST", "/api/records", {"turtle": DRAFT}, C.SAVE_AND_REVIEW),
            ("GET", "/api/records/0123456789abcdef", None, C.SAVE_AND_REVIEW),
            ("GET", "/api/admin/records/export", None, C.EXPORT_RESEARCH),
            ("POST", f"/api/admin/users/{self.bob['id']}/grant-admin", {}, C.MANAGE_ADMINS),
        ):
            with self.subTest(path=path):
                response = client.request(method, path, json=body)
                self.assertEqual((response.status_code, response.json()),
                                 (409, {"detail": DISABLED_MESSAGE[capability], "reason": "accounts_disabled"}))


class RecordReadTests(AccessHttpTestCase):
    def test_a_record_answers_its_account_and_admins_and_stays_hidden_from_others(self):
        """Covers: AC-access-policy-015."""
        bob, admin, carol = self.client(), self.client(), self.client()
        self.login(bob, "bob")
        self.login(admin, "admin", "admin-password")
        self.login(carol, "carol")
        record_id = self.start_record(bob)

        self.assertEqual(bob.get(f"/api/records/{record_id}").status_code, 200)
        self.assertEqual(admin.get(f"/api/records/{record_id}").status_code, 200)
        hidden, missing = carol.get(f"/api/records/{record_id}"), carol.get("/api/records/fedcba9876543210")
        self.assertEqual((hidden.status_code, missing.status_code), (404, 404))
        self.assertEqual(hidden.json(), missing.json())

        self.store.enabled = False
        response = self.client().get(f"/api/records/{record_id}")
        self.assertEqual((response.status_code, response.json()),
                         (409, {"detail": RECORDS_DISABLED, "reason": "accounts_disabled"}))


class RefusedRequestTests(AccessHttpTestCase):
    def test_a_refused_request_does_nothing_for_its_operation(self):
        """Covers: AC-access-policy-011 (real routes, stubbed states: no model call, record row, registry request
        or audit row)."""
        client = self.client(raise_server_exceptions=False)
        self.login(client, "bob")  # a real session passes the authentication middleware
        record_id = self.start_record(client)
        records, events = self.rows("SELECT * FROM record_items"), self.rows("SELECT * FROM record_events")
        audit = self.rows("SELECT id FROM audit_events")
        bob = self.bob["id"]
        cases = (
            (state(account_id=bob, terms_accepted=False), "POST", "/api/decompose", {"definition": "air temperature"}, 403, "terms_not_accepted"),
            (state(account_id=bob, application="none", uses_left_today=0), "POST", "/api/decompose/stream",
             {"definition": "air temperature"}, 429, "daily_limit"),
            (state(account_id=bob, terms_accepted=False), "POST", "/api/records", {"turtle": DRAFT}, 403, "terms_not_accepted"),
            (state(account_id=bob, terms_accepted=False), "POST", f"/api/records/{record_id}/events",
             {"type": "mark_best", "version": 0, "unchecked_components": 0}, 403, "terms_not_accepted"),
            (state(account_id=bob, application="pending"), "POST", "/api/nanopub/publish",
             {"ttl": DRAFT, "creator_orcid_id": "0000-0002-1825-0097", "record_id": record_id}, 403, "approval_required"),
            (state(account_id=bob, application="rejected"), "POST", "/api/nanopub/retract",
             {"nanopub_uri": NANOPUB, "creator_orcid_id": "0000-0002-1825-0097"}, 403, "approval_required"),
        )
        unexpected = AssertionError("the refused operation ran")
        with (
            patch.object(decompose_router, "run_pipeline", side_effect=unexpected) as pipeline,
            patch.object(decompose_router, "stream_pipeline_events", side_effect=unexpected) as stream,
            patch.object(nanopub_router, "get_nanopub_profile", side_effect=unexpected) as profile,
            patch.object(nanopub_router, "Nanopub", side_effect=unexpected) as nanopub,
            patch.object(nanopub_router, "build_retraction_nanopub", side_effect=unexpected) as retraction,
        ):
            for stubbed, method, path, body, status, reason in cases:
                with self.subTest(path=path):
                    self.stub(stubbed)
                    response = client.request(method, path, json=body)
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(response.json()["reason"], reason)
            for mock in (pipeline, stream, profile, nanopub, retraction):
                mock.assert_not_called()
        self.assertEqual(self.rows("SELECT * FROM record_items"), records)
        self.assertEqual(self.rows("SELECT * FROM record_events"), events)
        self.assertEqual(self.rows("SELECT id FROM audit_events"), audit)


class AnonymousTests(AccessHttpTestCase):
    def test_without_a_session_only_the_public_routes_answer(self):
        """Covers: AC-access-policy-016 (D-034)."""
        client = self.client()
        for path in ("/api/model-options", "/api/nanopub/preparation-options"):
            with self.subTest(path=path, method="GET"):
                self.assertEqual(client.get(path).status_code, 200)
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(client.request(method, path).status_code, 401)
        unexpected = AssertionError("a protected operation ran without a session")
        with (
            patch.object(decompose_router, "run_pipeline", side_effect=unexpected),
            patch.object(decompose_router, "stream_pipeline_events", side_effect=unexpected),
            patch.object(nanopub_router, "get_nanopub_profile", side_effect=unexpected),
            patch.object(self.store, "audit_event") as audit,
        ):
            for (method, path), capability in EXPECTED_ROUTES.items():
                if capability is None or capability is C.PUBLIC_TOOLS:
                    continue
                with self.subTest(route=(method, path)):
                    response = client.request(method, fill(path), json=None if method == "GET" else {})
                    self.assertEqual((response.status_code, response.json()), (401, {"detail": "Authentication required."}))
            audit.assert_not_called()
