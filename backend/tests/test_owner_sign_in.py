"""owner-sign-in: the owner's own password sign-in (contract approved under gate G-025, D-053 and D-054).

AC-owner-sign-in-001 to -011, -014 and -015 through the routes, the operator commands and the operator texts;
-012 and -013 are the pages' (``frontend/test/owner.signIn.browser.test.tsx``). Every test runs on a temporary
database with the fake provider of increment 2, so nothing reaches a real provider.

Seams the implementation keeps: the password checks are observed as calls of ``hashlib.pbkdf2_hmac``, which the auth
store makes; ``owner password`` reads the terminal through ``getpass.getpass`` and tells a terminal by
``sys.stdin.isatty()``. The owner's hash is stored with fewer iterations than a new hash gets, so the stand-in check
can be told to take the owner's count.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import pathlib
import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

from fastapi.testclient import TestClient
from increment1_support import run_cli, sign_in_as
from increment2_support import PASSWORD_COOKIE, ROHUB_COOKIE, Clock, OidcTestCase

from app import guards

ROOT = pathlib.Path(__file__).resolve().parents[2]
OWNER_LOGIN = "/api/auth/owner-login"
REFUSAL = {"detail": "Invalid username or password."}
OWNER_ITERATIONS = 200_000
"""The owner's hash in these tests: fewer iterations than a new hash gets."""


class Pbkdf2Spy:
    """Counts the PBKDF2 checks and keeps their salts and iteration counts; ``real`` computes them too."""

    def __init__(self, real: bool = True) -> None:
        self.calls: List[Tuple[bytes, int]] = []
        self._real = hashlib.pbkdf2_hmac
        self._compute = real

    def __call__(self, hash_name: str, password: bytes, salt: bytes, iterations: int, dklen: Optional[int] = None) -> bytes:
        self.calls.append((bytes(salt), int(iterations)))
        if self._compute:
            return self._real(hash_name, password, salt, iterations, dklen)
        return b"\x00" * (dklen or 32)


def salt_of(password_hash: str) -> Optional[bytes]:
    """The salt of a stored ``pbkdf2_sha256$iterations$salt$digest`` hash, or ``None`` for the unusable marker."""
    parts = password_hash.split("$")
    return base64.b64decode(parts[2]) if len(parts) == 4 else None


def hash_with(password: str, iterations: int) -> str:
    """A stored hash of ``password`` in the auth store's format, with ``iterations``."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


class OwnerSignInTestCase(OidcTestCase):
    """`oidc` on a development machine; `admin` (bootstrap) bound as the owner, `bob` an ordinary local account."""

    mode = "oidc"
    bind_admin = True

    def setUp(self) -> None:
        super().setUp()
        self.bob = self.store.create_user(username="bob", password="password-123", display_name="Bob", email="bob@example.org")
        self.admin_id = self.account_id("admin")
        if self.bind_admin:
            self.cli_ok("owner", "bind", "--username", "admin")
            self.execute("UPDATE users SET password_hash = ? WHERE id = ?", hash_with("admin-password", OWNER_ITERATIONS), self.admin_id)
        self.new_iterations = int(self.store.hash_password("x").split("$")[1])
        self.assertNotEqual(self.new_iterations, OWNER_ITERATIONS)

    # ----------------------------------------------------------------- arrangement and observation

    def cli_ok(self, *argv: str) -> str:
        code, out = run_cli(*argv)
        self.assertEqual(code, 0, out)
        return out

    def account_id(self, username: str) -> int:
        return int(self.rows("SELECT id FROM users WHERE username = ?", username)[0]["id"])

    def password_hash(self, username: str) -> str:
        return str(self.rows("SELECT password_hash FROM users WHERE username = ?", username)[0]["password_hash"])

    def owner_login(self, browser: Optional[TestClient] = None, username: str = "admin", password: str = "admin-password",
                    **kwargs: Any) -> Tuple[TestClient, Any]:
        browser = browser or self.browser()
        return browser, browser.post(OWNER_LOGIN, json={"username": username, "password": password}, **kwargs)

    def owner_browser(self, username: str = "admin", password: str = "admin-password") -> TestClient:
        browser, response = self.owner_login(username=username, password=password)
        self.assertEqual(response.status_code, 200, response.text)
        return browser

    def counts(self) -> Tuple[int, int, int]:
        """Password sessions, ROHub sessions and audit rows."""
        return tuple(self.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] for table in ("sessions", "oidc_sessions", "audit_events"))  # type: ignore[return-value]

    def history_actions(self, account_id: int) -> List[str]:
        return [row["action"] for row in self.rows("SELECT action FROM account_history WHERE user_id = ? ORDER BY id", account_id)]

    @contextmanager
    def spying(self, real: bool = True) -> Iterator[Pbkdf2Spy]:
        spy = Pbkdf2Spy(real)
        with patch("hashlib.pbkdf2_hmac", spy):
            yield spy

    def use_limiter_clock(self) -> Clock:
        clock = Clock()
        self.replace(guards, "limiter", guards.RateLimiter(clock=clock))
        return clock


class NoOwnerTestCase(OwnerSignInTestCase):
    bind_admin = False


# ------------------------------------------------------------------------------------------------
# Section 3: the route
# ------------------------------------------------------------------------------------------------


class RouteTests(OwnerSignInTestCase):
    def test_the_owner_signs_in_in_every_mode_with_the_name_matched_as_the_password_route_does(self):
        """Covers: AC-owner-sign-in-001."""
        for mode in ("local", "oidc", "both"):
            with self.subTest(mode=mode):
                self.settings(auth_mode_raw=mode)
                browser, other = self.browser(), None
                rohub_before = None
                if mode != "local":
                    self.signed_in(browser)
                    other = self.signed_in(sub=f"subject-ben-{mode}", email=f"ben-{mode}@example.org", sid=f"session-ben-{mode}")
                    rohub_before = len(self.rohub_sessions())
                browser, response = self.owner_login(browser, username="  ADMIN ")
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(set(body), {"user", "auth_enabled"})
                self.assertEqual((body["user"]["username"], body["auth_enabled"]), ("admin", True))
                self.assertIn(PASSWORD_COOKIE, self.set_cookies(response))
                me = self.me(browser)
                self.assertEqual((me["user"]["username"], me["sign_in"]), ("admin", "password"))
                if rohub_before is not None and other is not None:
                    self.assertEqual(len(self.rohub_sessions()), rohub_before - 1)
                    self.assertNotIn(ROHUB_COOKIE, browser.cookies)
                    self.assertEqual(self.me(other)["sign_in"], "rohub")  # another browser's session stays

    def test_every_other_case_gets_one_refusal_and_the_same_work(self):
        """Covers: AC-owner-sign-in-002 (the owner bound)."""
        ada_browser = self.signed_in()  # a ROHub account, without a usable password
        self.assertEqual(self.me(ada_browser)["user"]["username"], "ada")
        owner_salt = salt_of(self.password_hash("admin"))
        others = {salt_of(self.password_hash(name)) for name in ("bob", "ada")} - {None}
        cases = {
            "unknown name": ("nobody", "admin-password"),
            "wrong password": ("admin", "wrong-password"),
            "another account's right password": ("bob", "password-123"),
            "an account without a usable hash": ("ada", "anything"),
        }
        answers = {}
        for case, (username, password) in cases.items():
            with self.subTest(case=case):
                before = self.counts()
                with self.spying() as spy:  # sent by a browser with a ROHub session, which must stay
                    _, response = self.owner_login(ada_browser, username=username, password=password)
                self.assertEqual((response.status_code, response.json()), (401, REFUSAL))
                self.assertNotIn("set-cookie", response.headers)
                self.assertEqual(self.counts()[:2], before[:2])
                self.assertEqual(self.me(ada_browser)["sign_in"], "rohub")
                self.assertEqual(len(spy.calls), 1, spy.calls)
                salt, iterations = spy.calls[0]
                self.assertEqual(iterations, OWNER_ITERATIONS)
                self.assertNotIn(salt, others)
                if username != "admin":
                    self.assertNotEqual(salt, owner_salt)
                answers[case] = sorted(response.headers.items())
        self.assertEqual(len(answers), len(cases))
        self.assertEqual(len({json.dumps(headers) for headers in answers.values()}), 1, answers)

    def test_an_owner_that_is_not_active_or_has_no_usable_hash_is_refused_the_same_way(self):
        """Covers: AC-owner-sign-in-002 (the owner's account itself)."""
        owner_salt = salt_of(self.password_hash("admin"))
        self.execute("UPDATE users SET is_active = 0 WHERE id = ?", self.admin_id)
        with self.spying() as spy:
            _, inactive = self.owner_login()
        self.assertEqual((inactive.status_code, inactive.json()), (401, REFUSAL))
        self.assertEqual(len(spy.calls), 1)
        self.assertNotEqual(spy.calls[0][0], owner_salt)
        self.assertEqual(spy.calls[0][1], OWNER_ITERATIONS)
        self.execute("UPDATE users SET is_active = 1 WHERE id = ?", self.admin_id)

        self.signed_in()  # ada, a ROHub account
        self.cli_ok("owner", "rebind", "--issuer", self.provider.issuer, "--subject", "subject-ada", "--reason", "a test")
        with self.spying() as spy:
            _, no_hash = self.owner_login(username="ada", password="anything")
        self.assertEqual((no_hash.status_code, no_hash.json()), (401, REFUSAL))
        self.assertEqual([iterations for _, iterations in spy.calls], [self.new_iterations])
        self.assertEqual(sorted(inactive.headers.items()), sorted(no_hash.headers.items()))

    def test_a_body_that_is_not_valid_answers_422_takes_no_try_and_writes_no_audit(self):
        """Covers: AC-owner-sign-in-003."""
        self.use_limiter_clock()
        address = {"X-Forwarded-For": "198.51.100.7"}
        bodies = [
            {"content": b"username=admin&password=admin-password", "headers": {"Content-Type": "application/x-www-form-urlencoded"}},
            {"content": b'{"username": "admin", "password": "admin-password"}', "headers": {"Content-Type": "text/plain"}},
            {"content": b"{", "headers": {"Content-Type": "application/json"}},
            {"json": {}}, {"json": {"username": "admin"}}, {"json": {"password": "admin-password"}},
            {"json": {"username": "", "password": "admin-password"}}, {"json": {"username": "admin", "password": ""}},
            {"json": {"username": "a" * 121, "password": "admin-password"}}, {"json": {"username": "admin", "password": "p" * 1025}},
        ]
        audit_before = self.counts()[2]
        browser = self.browser(headers=address)
        with self.spying() as spy:
            for body in bodies:
                with self.subTest(body=str(body)[:60]):
                    self.assertEqual(browser.post(OWNER_LOGIN, **body).status_code, 422)
        self.assertEqual(spy.calls, [])
        self.assertEqual(self.counts()[2], audit_before)
        with self.spying(real=False):
            for _ in range(10):
                self.assertEqual(self.owner_login(browser, password="wrong")[1].status_code, 401)
        # The bounds themselves are valid.
        self.use_limiter_clock()
        self.assertEqual(self.owner_login(username="a" * 120, password="p" * 1024)[1].status_code, 401)

    def test_the_password_route_stays_switched_off_for_the_owner(self):
        """Covers: AC-owner-sign-in-004."""
        for username, password in (("admin", "admin-password"), ("bob", "password-123"), ("nobody", "x")):
            response = self.browser().post("/api/auth/login", json={"username": username, "password": password})
            self.assertEqual((response.status_code, response.json()["reason"]), (403, "password_sign_in_disabled"))
            self.assertNotIn("set-cookie", response.headers)


class NoOwnerRouteTests(NoOwnerTestCase):
    def test_with_no_owner_bound_everybody_is_refused_after_the_same_work(self):
        """Covers: AC-owner-sign-in-002 (no owner bound)."""
        for username, password in (("admin", "admin-password"), ("bob", "password-123")):
            with self.subTest(username=username), self.spying() as spy:
                _, response = self.owner_login(username=username, password=password)
                self.assertEqual((response.status_code, response.json()), (401, REFUSAL))
                self.assertEqual([iterations for _, iterations in spy.calls], [self.new_iterations])
                self.assertNotIn(spy.calls[0][0], {salt_of(self.password_hash(name)) for name in ("admin", "bob")})


# ------------------------------------------------------------------------------------------------
# Section 5: the limit
# ------------------------------------------------------------------------------------------------


class LimitTests(OwnerSignInTestCase):
    def test_ten_tries_per_address_then_one_a_minute_and_other_addresses_unaffected(self):
        """Covers: AC-owner-sign-in-005."""
        clock = self.use_limiter_clock()
        one, other = self.browser(headers={"X-Forwarded-For": "198.51.100.7"}), self.browser(headers={"X-Forwarded-For": "203.0.113.9"})
        with self.spying(real=False) as spy:
            for _ in range(10):
                self.assertEqual(self.owner_login(one, password="wrong")[1].status_code, 401)
            self.assertEqual(len(spy.calls), 10)
            _, refused = self.owner_login(one)
            self.assertEqual(refused.status_code, 429, refused.text)
            wait = int(refused.headers["retry-after"])
            self.assertGreaterEqual(wait, 1)
            self.assertEqual(refused.json(), {"detail": f"Too many requests. Try again in {wait} seconds.", "reason": "rate_limited"})
            self.assertEqual(len(spy.calls), 10, "the password is not checked beyond the limit")
            self.assertEqual(self.owner_login(other, password="wrong")[1].status_code, 401)
            self.assertEqual(one.post(OWNER_LOGIN, json={}).status_code, 422)  # an invalid body is refused before the limit
            clock.advance(60)
            self.assertEqual(self.owner_login(one, password="wrong")[1].status_code, 401)
            self.assertEqual(self.owner_login(one, password="wrong")[1].status_code, 429)
        self.assertEqual(len(spy.calls), 12)


# ------------------------------------------------------------------------------------------------
# Section 4: the owner's session
# ------------------------------------------------------------------------------------------------


class SessionTests(OwnerSignInTestCase):
    def test_in_oidc_the_owners_password_session_counts_and_carries_a_token(self):
        """Covers: AC-owner-sign-in-006."""
        owner = self.owner_browser()
        me = self.me(owner)
        self.assertEqual((me["user"]["username"], me["sign_in"]), ("admin", "password"))
        self.assertIsInstance(me["csrf_token"], str)
        self.assertGreaterEqual(len(me["csrf_token"]), 43)
        self.assertNotIn(me["csrf_token"], " ".join(owner.cookies.values()))
        bob = self.browser()
        sign_in_as(bob, self.bob["id"])
        self.assertEqual(bob.get("/api/auth/me").status_code, 401)

    def test_in_oidc_every_change_of_the_owners_session_needs_the_token_and_the_origin(self):
        """Covers: AC-owner-sign-in-007 (oidc)."""
        owner = self.owner_browser()
        good = self.csrf_headers(owner)
        path = f"/api/admin/users/{self.bob['id']}"
        refused = {
            "no token": {"Origin": self.public_url},
            "a wrong token": {"X-CSRF-Token": "x" * 43, "Origin": self.public_url},
            "no origin": {"X-CSRF-Token": good["X-CSRF-Token"]},
            "another origin": {"X-CSRF-Token": good["X-CSRF-Token"], "Origin": "https://evil.example"},
        }
        for case, headers in refused.items():
            with self.subTest(case=case):
                response = owner.patch(path, json={"display_name": "Changed"}, headers=headers)
                self.assertEqual((response.status_code, response.json()["reason"]), (403, "csrf_failed"))
        self.assertEqual(self.rows("SELECT display_name FROM users WHERE id = ?", self.bob["id"]), [{"display_name": "Bob"}])
        self.assertEqual(owner.get("/api/admin/users").status_code, 200)
        self.assertEqual(owner.patch(path, json={"display_name": "Changed"}, headers=good).status_code, 200)
        self.assertEqual(self.rows("SELECT display_name FROM users WHERE id = ?", self.bob["id"]), [{"display_name": "Changed"}])

    def test_in_local_and_both_the_owners_password_session_needs_no_token(self):
        """Covers: AC-owner-sign-in-007 (local and both)."""
        for mode in ("local", "both"):
            with self.subTest(mode=mode):
                self.settings(auth_mode_raw=mode)
                owner = self.owner_browser()
                self.assertIsNone(self.me(owner)["csrf_token"])
                response = owner.patch(f"/api/admin/users/{self.bob['id']}", json={"display_name": f"Bob {mode}"})
                self.assertEqual(response.status_code, 200, response.text)


class BindingTests(NoOwnerTestCase):
    def test_owner_bind_ends_the_sessions_of_the_account_it_binds(self):
        """Covers: AC-owner-sign-in-008 (bind)."""
        self.settings(auth_mode_raw="local")
        admin, bob = self.password_browser(), self.password_browser("bob", "password-123")
        self.cli_ok("owner", "bind", "--username", "admin")
        self.assertEqual(admin.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.me(bob)["user"]["username"], "bob")

    def test_owner_bind_ends_a_rohub_session_of_the_account_it_binds(self):
        """Covers: AC-owner-sign-in-008 (bind, a ROHub session)."""
        ada = self.signed_in()
        self.cli_ok("owner", "bind", "--issuer", self.provider.issuer, "--subject", "subject-ada")
        self.assertEqual(ada.get("/api/auth/me").status_code, 401)

    def test_owner_rebind_ends_the_sessions_of_both_accounts_and_moves_the_route(self):
        """Covers: AC-owner-sign-in-008 (rebind)."""
        self.cli_ok("owner", "bind", "--username", "admin")
        self.settings(auth_mode_raw="local")
        admin, bob = self.password_browser(), self.password_browser("bob", "password-123")
        self.cli_ok("owner", "rebind", "--username", "bob", "--reason", "a test")
        self.assertEqual((admin.get("/api/auth/me").status_code, bob.get("/api/auth/me").status_code), (401, 401))
        self.settings(auth_mode_raw="oidc")
        self.assertEqual(self.owner_login(username="admin", password="admin-password")[1].status_code, 401)
        self.assertEqual(self.owner_login(username="bob", password="password-123")[1].status_code, 200)

    def test_owner_rebind_ends_the_rohub_sessions_of_the_accounts_it_binds_and_unbinds(self):
        """Covers: AC-owner-sign-in-008 (both kinds, both accounts)."""
        self.cli_ok("owner", "bind", "--username", "admin")
        self.settings(auth_mode_raw="oidc")
        ada = self.signed_in()
        self.cli_ok("owner", "rebind", "--issuer", self.provider.issuer, "--subject", "subject-ada", "--reason", "a test")
        self.assertEqual(ada.get("/api/auth/me").status_code, 401)
        ada = self.signed_in()  # the owner now
        self.cli_ok("owner", "rebind", "--username", "admin", "--reason", "back")
        self.assertEqual(ada.get("/api/auth/me").status_code, 401)


# ------------------------------------------------------------------------------------------------
# Section 6: the owner's password
# ------------------------------------------------------------------------------------------------


class PurgeTests(OwnerSignInTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.create_user(username="carol", password="password-123", display_name="Carol", email="carol@example.org")

    def test_the_purge_keeps_the_owners_password_and_only_it(self):
        """Covers: AC-owner-sign-in-009 (with the flag)."""
        self.signed_in()  # ada: no usable hash to purge
        admin_hash = self.password_hash("admin")
        code, out = run_cli("legacy", "purge-passwords", "--confirm-cutover")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"\b2\b")  # bob and carol; the owner's account is 1
        self.assertIn("owner", out.lower())
        self.assertNotIn("admin-password", out)
        self.assertEqual(self.password_hash("admin"), admin_hash)
        for name in ("bob", "carol"):
            self.assertEqual(self.password_hash(name), "!")
            self.assertIn("password_purged", self.history_actions(self.account_id(name)))
        self.assertNotIn("password_purged", self.history_actions(self.admin_id))
        self.settings(auth_mode_raw="local")
        self.assertEqual(self.browser().post("/api/auth/login", json={"username": "admin", "password": "admin-password"}).status_code, 200)
        for name in ("bob", "carol"):
            self.assertEqual(self.browser().post("/api/auth/login", json={"username": name, "password": "password-123"}).status_code, 401)
        self.settings(auth_mode_raw="oidc")
        self.assertEqual(self.owner_login()[1].status_code, 200)

    def test_without_the_flag_the_purge_counts_without_the_owner_and_changes_nothing(self):
        """Covers: AC-owner-sign-in-009 (without the flag)."""
        before = self.accounts()
        code, out = run_cli("legacy", "purge-passwords")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"\b2\b")  # bob and carol; the owner's password is not counted
        self.assertEqual(self.accounts(), before)


class NoOwnerPurgeTests(NoOwnerTestCase):
    def test_with_the_flag_and_no_owner_the_purge_refuses(self):
        """Covers: AC-owner-sign-in-009 (no owner bound)."""
        before = self.accounts()
        code, out = run_cli("legacy", "purge-passwords", "--confirm-cutover")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.accounts(), before)


class TerminalStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


class OwnerPasswordCommandTests(OwnerSignInTestCase):
    NEW = "a-long-new-password-1"

    def owner_password(self, stdin: str = "", terminal: Optional[List[str]] = None, *argv: str) -> Tuple[int, str]:
        source = TerminalStdin("") if terminal is not None else io.StringIO(stdin)
        with patch("sys.stdin", source), patch("getpass.getpass", side_effect=list(terminal or [])):
            return run_cli("owner", "password", *argv)

    def test_it_sets_the_password_from_standard_input_and_ends_every_session(self):
        """Covers: AC-owner-sign-in-010 (standard input, sessions, records)."""
        owner = self.owner_browser()
        code, out = self.owner_password(f"{self.NEW}\n")
        self.assertEqual(code, 0, out)
        self.assertNotIn(self.NEW, out)
        self.assertEqual(owner.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.owner_login(password="admin-password")[1].status_code, 401)
        self.assertEqual(self.owner_login(password=self.NEW)[1].status_code, 200)
        self.assertEqual(self.history_actions(self.admin_id)[-1], "owner_password_changed")
        self.assertEqual(self.rows("SELECT actor_kind FROM account_history WHERE user_id = ? AND action = ?", self.admin_id,
                                   "owner_password_changed"), [{"actor_kind": "offline"}])
        self.assertEqual(len(self.audit("owner.password_changed")), 1)

    def test_it_strips_only_the_line_ending(self):
        """Covers: AC-owner-sign-in-010 (one line without its line ending)."""
        spaced = "  a long spaced password  "
        self.assertEqual(self.owner_password(f"{spaced}\n")[0], 0)
        self.assertEqual(self.owner_login(password=spaced.strip())[1].status_code, 401)
        self.assertEqual(self.owner_login(password=spaced)[1].status_code, 200)

    def test_on_a_terminal_it_asks_twice(self):
        """Covers: AC-owner-sign-in-010 (the terminal)."""
        self.assertEqual(self.owner_password(terminal=[self.NEW, self.NEW])[0], 0)
        self.assertEqual(self.owner_login(password=self.NEW)[1].status_code, 200)

    def test_it_refuses_and_changes_nothing(self):
        """Covers: AC-owner-sign-in-010 (refusals)."""
        owner = self.owner_browser()
        before = self.password_hash("admin")
        cases = {
            "entries differ": {"terminal": [self.NEW, self.NEW + "x"]},
            "14 characters": {"stdin": "fourteen-chars\n"},
            "1,025 characters": {"stdin": "p" * 1025 + "\n"},
            "empty": {"stdin": "\n"},
            "nothing": {"stdin": ""},
        }
        for case, kwargs in cases.items():
            with self.subTest(case=case):
                code, out = self.owner_password(**kwargs)
                self.assertEqual(code, 1, out)
                self.assertEqual(self.password_hash("admin"), before)
                self.assertEqual(self.me(owner)["user"]["username"], "admin")
        self.assertEqual(self.owner_password("", None, self.NEW)[0], 2)  # never as an argument
        self.assertEqual(self.password_hash("admin"), before)
        self.assertEqual(self.history_actions(self.admin_id).count("owner_password_changed"), 0)
        self.assertEqual(self.audit("owner.password_changed"), [])

    def test_it_takes_15_and_1024_characters(self):
        """Covers: AC-owner-sign-in-010 (the bounds)."""
        for password in ("p" * 15, "q" * 1024):
            with self.subTest(length=len(password)):
                code, out = self.owner_password(f"{password}\n")
                self.assertEqual(code, 0, out)
                self.assertEqual(self.owner_login(password=password)[1].status_code, 200)

    def test_it_also_sets_the_password_of_an_owner_that_holds_an_identity(self):
        """Covers: AC-owner-sign-in-010 (an owner's account with an identity)."""
        ada = self.signed_in()
        self.cli_ok("owner", "rebind", "--issuer", self.provider.issuer, "--subject", "subject-ada", "--reason", "a test")
        ada = self.signed_in()  # rebinding ended the session
        self.assertEqual(self.owner_password(f"{self.NEW}\n")[0], 0)
        self.assertEqual(ada.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.owner_login(username="ada", password=self.NEW)[1].status_code, 200)


class NoOwnerPasswordCommandTests(NoOwnerTestCase):
    def test_with_no_owner_bound_it_refuses(self):
        """Covers: AC-owner-sign-in-010 (no owner bound)."""
        before = self.accounts()
        with patch("sys.stdin", io.StringIO("a-long-new-password-1\n")):
            code, out = run_cli("owner", "password")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.accounts(), before)


class AdminPasswordTests(OwnerSignInTestCase):
    def test_in_oidc_the_admin_page_resets_no_password(self):
        """Covers: AC-owner-sign-in-011 (oidc)."""
        owner = self.owner_browser()
        headers = self.csrf_headers(owner)
        refusal = {"detail": "Passwords are changed with the owner password command.", "reason": "password_by_command"}
        hashes = {name: self.password_hash(name) for name in ("admin", "bob")}
        for target in (self.admin_id, self.bob["id"], 999_999):
            for body in ({"password": "new-password-123"}, {"password": None}, {"display_name": "Changed", "password": "new-password-123"}):
                with self.subTest(target=target, body=body):
                    response = owner.patch(f"/api/admin/users/{target}", json=body, headers=headers)
                    self.assertEqual((response.status_code, response.json()), (400, refusal))
        short = owner.patch(f"/api/admin/users/{self.bob['id']}", json={"password": "short"}, headers=headers)
        self.assertEqual(short.status_code, 422)
        self.assertEqual({name: self.password_hash(name) for name in ("admin", "bob")}, hashes)
        self.assertEqual(self.rows("SELECT display_name FROM users WHERE id = ?", self.bob["id"]), [{"display_name": "Bob"}])
        self.assertEqual(owner.patch(f"/api/admin/users/{self.bob['id']}", json={"display_name": "Bobby"}, headers=headers).status_code, 200)
        created = owner.post("/api/admin/users", json={"username": "dora", "password": "password-123"}, headers=headers)
        self.assertEqual(created.status_code, 200, created.text)

    def test_in_oidc_an_admin_who_signed_in_with_rohub_resets_no_password_either(self):
        """Covers: AC-owner-sign-in-011 (an admin of ROHub)."""
        owner = self.owner_browser()
        headers = self.csrf_headers(owner)
        ada = self.signed_in()
        ada_id = self.account_id("ada")
        for action in ("approve", "grant-admin"):
            self.assertEqual(owner.post(f"/api/admin/users/{ada_id}/{action}", json={}, headers=headers).status_code, 200)
        bob_hash = self.password_hash("bob")
        response = ada.patch(f"/api/admin/users/{self.bob['id']}", json={"password": "new-password-123"}, headers=self.csrf_headers(ada))
        self.assertEqual((response.status_code, response.json()["reason"]), (400, "password_by_command"))
        self.assertEqual(self.password_hash("bob"), bob_hash)

    def test_in_local_and_both_a_password_is_reset_as_before(self):
        """Covers: AC-owner-sign-in-011 (local and both)."""
        for mode, password in (("local", "new-password-123"), ("both", "new-password-456")):
            with self.subTest(mode=mode):
                self.settings(auth_mode_raw=mode)
                owner = self.password_browser()
                response = owner.patch(f"/api/admin/users/{self.bob['id']}", json={"password": password})
                self.assertEqual(response.status_code, 200, response.text)
                self.login(self.browser(), "bob", password)


# ------------------------------------------------------------------------------------------------
# Section 9: audit and logs
# ------------------------------------------------------------------------------------------------


class AuditTests(OwnerSignInTestCase):
    def test_the_route_is_audited_with_its_name_and_nothing_secret_is_written(self):
        """Covers: AC-owner-sign-in-014."""
        root = logging.getLogger()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous)

        self.owner_login(username="nobody", password="a-wrong-password")
        owner = self.owner_browser(username="  ADMIN ")
        token = self.me(owner)["csrf_token"]
        new_password = "a-new-owner-password-1"
        with patch("sys.stdin", io.StringIO(f"{new_password}\n")):
            self.assertEqual(run_cli("owner", "password")[0], 0)
        new_hash = self.password_hash("admin")
        success, failure = self.audit("auth.login")[-1], self.audit("auth.login_failed")[-1]
        for row, username in ((success, "  ADMIN "), (failure, "nobody")):
            fields = {**json.loads(row["request_payload"] or "{}"), **json.loads(row["metadata"] or "{}")}
            self.assertEqual((fields.get("route"), fields.get("username")), ("owner", username), row)
        _, salt, digest = new_hash.split("$")[1:]
        secrets = ("admin-password", "a-wrong-password", new_password, new_hash, salt, digest, token, owner.cookies[PASSWORD_COOKIE])
        tables = [row["name"] for row in self.rows("SELECT name FROM sqlite_master WHERE type = 'table' AND name <> 'users'")]
        written = "".join(json.dumps(self.rows(f"SELECT * FROM {table}")) for table in tables) + stream.getvalue()
        for secret in secrets:
            self.assertNotIn(secret, written)


# ------------------------------------------------------------------------------------------------
# Section 8: the operator texts
# ------------------------------------------------------------------------------------------------


def runbook(number: int) -> str:
    text = (ROOT / "deploy" / "keycloak" / "README.md").read_text()
    parts = re.split(r"^### (\d)\. .+$", text, flags=re.MULTILINE)
    sections = dict(zip(parts[1::2], parts[2::2]))
    return sections[str(number)].split("\n## ")[0]


def manual_section(heading: str) -> str:
    text = (ROOT / "docs" / "manual.md").read_text()
    match = re.search(rf"^(#+) {re.escape(heading)}\n(.*?)(?=^\1 |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, heading
    return match.group(2)


class OperatorTextTests(OidcTestCase):
    def test_the_runbooks_say_how_the_owner_signs_in(self):
        """Covers: AC-owner-sign-in-015 (deploy/keycloak/README.md)."""
        two = runbook(2)
        opening = two.strip().split("\n\n")[0]
        self.assertRegex(opening, r"(?i)password sign-in[^.]*but the owner|only the owner[^.]*password")
        self.assertRegex(opening, r"(?i)terms[^.]*outside the application|outside the application[^.]*terms")
        self.assertIn("Owner sign-in", two)
        self.assertRegex(two, r"(?i)long password[^.]*owner password|owner password[^.]*long password")
        self.assertNotIn("--into OWNER_ID", two)
        self.assertRegex(two, r"(?i)not folded|do not fold")
        self.assertRegex(two, r"(?i)approves?[^.]*makes? it an admin")
        self.assertIn("Owner sign-in", runbook(3))
        four = runbook(4)
        self.assertRegex(four, r"(?i)block[^.]*owner|owner[^.]*block")
        self.assertIn("owner password", four)
        five = runbook(5)
        self.assertIn("Owner sign-in", five)
        self.assertIn("owner password", five)
        self.assertRegex(five, r"(?i)owner rebind[^.]*sessions")
        six = runbook(6)
        self.assertRegex(six, r"(?i)c103d4d[^.]*oidc|oidc[^.]*c103d4d")
        self.assertRegex(six, r"(?i)local[^.]*owner|owner[^.]*local")

    def test_the_manual_says_how_the_owner_signs_in_and_keeps_the_password(self):
        """Covers: AC-owner-sign-in-015 (docs/manual.md)."""
        text = manual_section("Signing in with ROHub") + manual_section("Accounts, admins and the owner")
        self.assertIn("Owner sign-in", text)
        self.assertRegex(text, r"(?i)owner[^.]*(per|each|one|every) (network )?address|(per|each|one|every) (network )?address[^.]*owner")
        self.assertIn("owner password", text)
        self.assertRegex(text, r"(?i)purge[^.]*(keeps|except)[^.]*owner|owner's password[^.]*purge")
