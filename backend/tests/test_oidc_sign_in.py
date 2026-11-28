"""oidc-identity: signing in with ROHub (increment 2; the security core, written with the code).

Behavioural tests of ``components/oidc-identity.md`` (approved under G-020) against a fake provider
with its own keys on a loopback port: the switch, the start, every refusal of the return, sessions,
CSRF and signing out. Nothing reaches a real provider.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
from datetime import timedelta
from typing import Any, Dict, List
from unittest.mock import patch
from urllib.parse import urlencode

from fake_provider import LOGOUT_EVENT, REMOVED, FakeProvider, b64, raw_jws, s256, shared_key_jws
from increment1_support import ISSUER, run_cli, started_app
from increment2_support import (
    BROWSER_COOKIE,
    PASSWORD_COOKIE,
    PUBLIC_URL,
    ROHUB_COOKIE,
    OidcTestCase,
    query_of,
)
from joserfc.jwe import encrypt_compact
from joserfc.jwk import ECKey, RSAKey

from app import storage
from app.auth import utc_iso, utc_now
from app.core import dependencies as core_deps
from app.oidc import flow
from app.oidc import provider as provider_module
from app.oidc import settings as oidc_settings
from app.oidc import store as oidc_store
from app.oidc import tokens as oidc_tokens

BACKEND = pathlib.Path(__file__).resolve().parents[1]
DISABLED = {"detail": "ROHub sign-in is not switched on.", "reason": "oidc_disabled"}
CSRF_FAILED = {"detail": "This page is out of date. Reload it and try again.", "reason": "csrf_failed"}
BAD_TARGETS = (
    "https://evil.example/", "javascript:alert(1)", "evil.example", "//evil.example", "/\\evil.example", "/.//evil.example",
    "/..//evil.example", "/%2e//evil.example", "/x/..//evil.example", "/x/y", "/api", "/api/x", "/%61pi/x", "/login.html",
    "/login.html?redirect=/", " /admin.html", "/ad min.html", "/admin.html ", "/admin\\html", "/admin.html?x=a\\b", "/\tadmin.html",
    "/admin.html\n", "/ädmin.html", "/.", "/..", "/-", "admin.html", "?x=y", "", "/admin.html?" + "x" * 2048,
)
GOOD_TARGETS = ("/", "/#/workspace", "/index.html?x=1#/workspace", "/admin.html", "/admin.html?x=y", "/remote.html", "/a_b-C.9", "/api.html")


# ------------------------------------------------------------------------------------------------
# Section 3: settings and the switch
# ------------------------------------------------------------------------------------------------


class LocalModeTests(OidcTestCase):
    mode = ""
    environment = ""

    def test_without_a_new_setting_passwords_work_as_before_and_the_rohub_routes_do_not_exist(self):
        self.settings(oidc_issuer_raw="not judged", oidc_client_id_raw="a b", public_url_raw="nonsense", sign_in_limit_raw="1/300")
        with started_app() as client:
            for method, path in (("GET", "/api/auth/login"), ("GET", "/api/auth/callback"), ("GET", "/api/auth/end-session"),
                                 ("POST", "/api/auth/backchannel-logout")):
                for _ in range(3):
                    response = client.request(method, path)
                    self.assertEqual((response.status_code, response.json()), (404, DISABLED), path)
            response = client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(list(self.set_cookies(response)), [PASSWORD_COOKIE])
            me = client.get("/api/auth/me")
            self.assertEqual((me.json()["sign_in"], me.json()["csrf_token"], me.headers["x-iadopt-sign-in"]), ("password", None, "password"))
            self.assertEqual(client.post("/api/events", json={"action": "visualize"}).status_code, 200)
            response = client.post("/api/auth/logout")
            self.assertEqual(response.json(), {"status": "ok", "end_session_url": None})
            self.assertEqual(client.get("/api/auth/me").status_code, 401)
        self.assertEqual((self.provider.received, self.transactions(), self.rohub_sessions()), ([], [], []))


class StartSettingsTests(OidcTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fresh = self.dir / "fresh.sqlite3"
        self.use_database(self.fresh)

    def refusal(self) -> str:
        """Start the application, which must stop before it opens the database; return its message."""
        with self.assertRaises(oidc_settings.SettingsError) as caught:
            with started_app():
                self.fail("the application started")
        self.assertFalse(self.fresh.exists(), "the database was opened")
        self.assertEqual(self.provider.received, [])
        self.assertNotIn("test-secret", str(caught.exception))
        return str(caught.exception)

    def test_each_missing_or_malformed_rohub_setting_stops_the_start_and_is_named(self):
        cases = {
            "IADOPT_OIDC_ISSUER": ("oidc_issuer_raw", ("", "ftp://localhost/x", "http://localhost/x?y", "http://localhost/x#y",
                                                       "http://local host/x", "localhost", "http://user@localhost/x", "http:///x",
                                                       "http://[v1.x]/x", "http://evil.example[::1]/x", "http://[::1]evil.example/x")),
            "IADOPT_OIDC_CLIENT_ID": ("oidc_client_id_raw", ("", "a b", "x" * 256, "a\x00b")),
            "IADOPT_PUBLIC_URL": ("public_url_raw", ("", "ftp://localhost", "http://localhost/app", "http://localhost?x", "http://localhost#x",
                                                     "http://local host", "localhost:5173", "http://localhost:99999", "http://localhost:",
                                                     "http://[v1.x]:5173", "http://evil.example[::1]:5173", "http://[::1]evil.example")),
            "IADOPT_OIDC_POST_LOGOUT": ("oidc_post_logout_raw", ("yes", "True", "1")),
        }
        for mode in ("oidc", "both"):
            for name, (field, values) in cases.items():
                for value in values:
                    with self.subTest(mode=mode, setting=name, value=value), patch.object(core_deps_settings(), field, value), \
                            patch.object(core_deps_settings(), "auth_mode_raw", mode):
                        message = self.refusal()
                        self.assertIn(name, message)
                        for other in set(cases) - {name}:
                            self.assertNotIn(other, message)
        self.settings(oidc_issuer_raw="", oidc_client_id_raw="", public_url_raw="", oidc_post_logout_raw="maybe")
        message = self.refusal()
        for name in cases:
            self.assertIn(name, message)
        code, out = run_cli("db", "status")  # no operator command fails with it
        self.assertEqual(code, 0, out)

    def test_bad_values_do_not_fail_the_import_of_the_application(self):
        bad = {name: "no such value" for name in (
            "IADOPT_ENVIRONMENT", "IADOPT_AUTH_MODE", "IADOPT_OIDC_ISSUER", "IADOPT_OIDC_CLIENT_ID", "IADOPT_PUBLIC_URL",
            "IADOPT_OIDC_POST_LOGOUT", "IADOPT_TRUSTED_PROXY_HOPS", "IADOPT_SIGN_IN_LIMIT", "IADOPT_DAILY_DECOMPOSITIONS")}
        code = "import app.main, app.cli; print('imported')"
        done = subprocess.run([sys.executable, "-W", "ignore", "-c", code], env={**os.environ, **bad}, cwd=BACKEND, capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stdout.splitlines()[-1:]), (0, ["imported"]), done.stderr)
        self.assertFalse(self.fresh.exists(), "the database was opened")

    def test_an_unknown_mode_or_environment_and_both_outside_development_stop_the_start(self):
        for field, value, name in (("auth_mode_raw", "ldap", "IADOPT_AUTH_MODE"), ("auth_mode_raw", "OIDC", "IADOPT_AUTH_MODE"),
                                   ("environment_raw", "staging", "IADOPT_ENVIRONMENT"), ("environment_raw", "", "IADOPT_AUTH_MODE"),
                                   ("environment_raw", "production", "IADOPT_AUTH_MODE")):
            with self.subTest(field=field, value=value), patch.object(core_deps_settings(), field, value):
                self.assertIn(name, self.refusal())

    def test_disabled_authentication_stops_the_start_outside_development_and_with_rohub(self):
        self.store.enabled = False
        for environment, mode, starts in (("", "", False), ("production", "local", False), ("development", "oidc", False),
                                          ("development", "both", False), ("development", "local", True), ("development", "", True)):
            with self.subTest(environment=environment, mode=mode), patch.object(core_deps_settings(), "environment_raw", environment), \
                    patch.object(core_deps_settings(), "auth_mode_raw", mode):
                if not starts:
                    self.assertIn("IADOPT_AUTH_ENABLED", self.refusal())
                    continue
                with started_app() as client:
                    me = client.get("/api/auth/me").json()
                    self.assertEqual((me["auth_enabled"], me["sign_in"], me["csrf_token"]), (False, None, None))
                self.fresh.unlink()

    def test_addresses_the_cookie_setting_and_the_number_of_proxies_are_judged(self):
        cases: List[tuple] = [
            (dict(oidc_issuer_raw="http://login.example.org/x"), False, "IADOPT_OIDC_ISSUER"),
            (dict(public_url_raw="http://iadopt.example.org"), False, "IADOPT_PUBLIC_URL"),
            (dict(environment_raw="production", auth_mode_raw="oidc", public_url_raw="http://localhost:5173"), False, "IADOPT_PUBLIC_URL"),
            (dict(public_url_raw="https://iadopt.example.org"), False, "IADOPT_COOKIE_SECURE"),
            (dict(), True, "IADOPT_COOKIE_SECURE"),
            (dict(environment_raw="production", auth_mode_raw="oidc", public_url_raw="https://iadopt.example.org",
                  oidc_issuer_raw="https://login.example.org/x"), True, "IADOPT_TRUSTED_PROXY_HOPS"),
        ]
        for raw, secure, name in cases:
            with self.subTest(raw=raw, secure=secure):
                self.store.cookie_secure = secure
                with patch.multiple(core_deps_settings(), **raw) if raw else patch.object(self, "fresh", self.fresh):
                    self.assertIn(name, self.refusal())
        self.store.cookie_secure = True
        with patch.multiple(core_deps_settings(), environment_raw="production", auth_mode_raw="oidc", public_url_raw="https://iadopt.example.org",
                            oidc_issuer_raw="https://login.example.org/x", trusted_proxy_hops_raw="2"):
            with started_app() as client:
                self.assertEqual(client.get("/api/livez").status_code, 200)
        self.assertEqual(self.provider.received, [])

    def test_the_origin_is_the_one_a_browser_writes(self):
        for text, origin in (("http://localhost:80/", "http://localhost"), ("http://LOCALHOST:5173", "http://localhost:5173"),
                             ("https://IADOPT.example.org:443/", "https://iadopt.example.org"), ("http://[0:0::1]:5173/", "http://[::1]:5173"),
                             ("http://app.localhost:5173", "http://app.localhost:5173"), ("http://127.0.0.1:5173", "http://127.0.0.1:5173")):
            with self.subTest(text=text), patch.object(core_deps_settings(), "public_url_raw", text):
                judged = oidc_settings.judge(auth_enabled=True, cookie_secure=text.lower().startswith("https"))
                self.assertEqual((judged.public_url, judged.callback_url), (origin, f"{origin}/api/auth/callback"))


def core_deps_settings() -> Any:
    from app.core import config

    return config.settings


class SwitchTests(OidcTestCase):
    def test_only_rohub_signs_people_in_where_passwords_are_off(self):
        browser = self.password_browser()
        self.settings(auth_mode_raw="oidc")
        me = browser.get("/api/auth/me")
        self.assertEqual((me.status_code, me.headers["x-iadopt-sign-in"]), (401, "rohub"))
        self.assertEqual(browser.get("/api/auth/verify").status_code, 401)
        for password in ("admin-password", "wrong"):
            response = self.browser().post("/api/auth/login", json={"username": "admin", "password": password})
            self.assertEqual((response.status_code, response.json()),
                             (403, {"detail": "Password sign-in is switched off. Sign in with ROHub.", "reason": "password_sign_in_disabled"}))
            self.assertNotIn("set-cookie", response.headers)
        for body in (b"", b"{", b'{"username": 1}'):  # nothing looks at it
            response = self.browser().post("/api/auth/login", content=body, headers={"Content-Type": "application/json"})
            self.assertEqual((response.status_code, response.json()["reason"]), (403, "password_sign_in_disabled"), body)
        self.assertEqual(self.audit("auth.login_failed"), [])
        me = self.signed_in().get("/api/auth/me")
        self.assertEqual((me.json()["sign_in"], me.headers["x-iadopt-sign-in"]), ("rohub", "rohub"))

    def test_both_kinds_count_and_the_rohub_one_is_used(self):
        browser = self.signed_in()
        self.assertEqual(self.me(browser)["user"]["username"], "ada")
        password = self.password_browser()
        self.assertEqual(self.me(password)["sign_in"], "password")
        # A browser that presents both, as after a change of the mode.
        browser.cookies.set(PASSWORD_COOKIE, password.cookies[PASSWORD_COOKIE])
        me = browser.get("/api/auth/me")
        self.assertEqual((me.json()["sign_in"], me.json()["user"]["username"], me.headers["x-iadopt-sign-in"]), ("rohub", "ada", "password, rohub"))
        self.settings(auth_mode_raw="local")
        me = browser.get("/api/auth/me")
        self.assertEqual((me.json()["sign_in"], me.json()["user"]["username"], me.headers["x-iadopt-sign-in"]), ("password", "admin", "password"))
        rohub_only = self.browser()
        rohub_only.cookies.set(ROHUB_COOKIE, browser.cookies[ROHUB_COOKIE])
        self.assertEqual(rohub_only.get("/api/auth/me").status_code, 401)

    def test_a_password_sign_in_ends_the_browsers_rohub_session(self):
        browser, other = self.signed_in(), self.signed_in(sub="subject-ben", email="ben@example.org", sid="session-ben")
        response = browser.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("x-iadopt-sign-in", response.headers)
        self.assertEqual(self.me(browser)["sign_in"], "rohub")
        self.login(browser, "admin", "admin-password")
        self.assertEqual(self.me(browser)["sign_in"], "password")
        self.assertEqual([row["subject"] for row in self.rohub_sessions()], ["subject-ben"])
        self.assertEqual(self.me(other)["sign_in"], "rohub")


# ------------------------------------------------------------------------------------------------
# Section 5: starting a sign-in
# ------------------------------------------------------------------------------------------------


class StartTests(OidcTestCase):
    def test_the_browser_is_sent_with_exactly_the_parameters_and_bound_by_a_cookie(self):
        browser = self.browser()
        seen = []
        for _ in range(2):
            response = browser.get("/api/auth/login")
            self.assertEqual((response.status_code, response.headers["cache-control"], response.headers["referrer-policy"]),
                             (303, "no-store", "no-referrer"))
            query = query_of(response.headers["location"])
            self.assertEqual(set(query), {"response_type", "client_id", "redirect_uri", "scope", "state", "nonce", "code_challenge", "code_challenge_method"})
            self.assertTrue(all(len(values) == 1 for values in query.values()))
            fixed = {name: query[name][0] for name in ("response_type", "client_id", "redirect_uri", "scope", "code_challenge_method")}
            self.assertEqual(fixed, {
                "response_type": "code", "client_id": self.provider.client_id, "redirect_uri": f"{PUBLIC_URL}/api/auth/callback",
                "scope": "openid profile email", "code_challenge_method": "S256",
            })
            row = self.transactions()[-1]
            self.assertEqual((query["code_challenge"][0], query["nonce"][0]), (s256(row["code_verifier"]), row["nonce"]))
            [cookie] = self.set_cookies(response).values()
            self.assertEqual({key: cookie[key] for key in cookie if key != "value"},
                             {"httponly": "", "samesite": "lax", "path": "/", "max-age": "600"})
            seen.append((query["state"][0], query["nonce"][0], row["code_verifier"], self.set_cookies(response)[BROWSER_COOKIE]["value"]))
        for first, second in list(zip(*seen))[:3]:
            self.assertNotEqual(first, second)
            self.assertGreaterEqual(min(len(first), len(second)), 43)
        self.assertEqual(seen[0][3], seen[1][3], "the key the browser has is set again")
        self.assertGreaterEqual(len(seen[0][3]), 43)

    def test_a_malformed_browser_key_is_replaced(self):
        for key in ("short", "x" * 43 + "!", "y" * 44):
            browser = self.browser()
            browser.cookies.set(BROWSER_COOKIE, key)
            self.assertNotEqual(self.set_cookies(browser.get("/api/auth/login"))[BROWSER_COOKIE]["value"], key)

    def test_only_pages_of_the_application_are_returned_to(self):
        for target in GOOD_TARGETS:
            with self.subTest(target=target):
                _, response = self.sign_in(redirect=target)
                self.assertEqual((response.status_code, response.headers["location"]), (303, target))
        for target in BAD_TARGETS:
            with self.subTest(target=target):
                browser = self.browser()
                location = self.begin(browser, target)
                self.assertEqual(self.transactions()[-1]["redirect"], "/")
                query = self.provider.authorize(location)
                refused = self.come_back(browser, {"error": "access_denied", "state": query["state"], "iss": self.provider.issuer})
                self.assert_refused(refused, "cancelled", "/")
        browser = self.browser()
        location = browser.get("/api/auth/login?redirect=/admin.html&redirect=//evil.example").headers["location"]
        self.assertEqual(self.come_back(browser, self.provider.authorize(location)).headers["location"], "/")

    def test_a_refusal_keeps_the_valid_page(self):
        browser = self.browser()
        query = self.provider.authorize(self.begin(browser, "/admin.html?a=1&b=2#x"))
        response = self.come_back(browser, {"error": "access_denied", "state": query["state"], "iss": self.provider.issuer})
        self.assert_refused(response, "cancelled", "/admin.html?a=1&b=2#x")
        self.provider.answer("metadata", 503)
        provider_module.provider.reset()
        self.assert_refused(self.browser().get("/api/auth/login", params={"redirect": "/admin.html?a=1&b=2"}), "provider_unavailable", "/admin.html?a=1&b=2")

    def test_transactions_hold_hashes_expire_and_are_bounded(self):
        browser = self.browser()
        response = browser.get("/api/auth/login")
        state = query_of(response.headers["location"])["state"][0]
        key = self.set_cookies(response)[BROWSER_COOKIE]["value"]
        text = self.stored_text()
        self.assertNotIn(state, text)
        self.assertNotIn(key, text)
        [row] = self.transactions()
        self.assertEqual(row["state_hash"], hashlib.sha256(state.encode()).hexdigest())

        self.execute("UPDATE oidc_transactions SET expires_at = ?", utc_iso(utc_now() - timedelta(seconds=1)))
        self.begin(self.browser())
        self.assertEqual(len(self.transactions()), 1, "expired ones are deleted when one is stored")

        with patch.object(oidc_store, "MAX_TRANSACTIONS", 3):
            locations = [self.begin(self.browser()) for _ in range(5)]
        kept = {row["state_hash"] for row in self.transactions()}
        newest = {hashlib.sha256(query_of(location)["state"][0].encode()).hexdigest() for location in locations[-3:]}
        self.assertEqual(kept, newest)
        self.assertEqual(oidc_store.MAX_TRANSACTIONS, 10_000)

    def test_a_sign_in_survives_a_restart_but_not_another_issuer(self):
        browser = self.browser()
        query = self.provider.authorize(self.begin(browser, "/admin.html"))
        provider_module.provider.reset()
        self.assertEqual(self.come_back(browser, query).headers["location"], "/admin.html")

        query = self.provider.authorize(self.begin(browser, "/admin.html"))
        with FakeProvider() as other:
            self.settings(oidc_issuer_raw=other.issuer)
            self.assert_refused(self.come_back(browser, query), "sign_in_expired")
            self.assertEqual(other.received, [])


class ProviderTests(OidcTestCase):
    def unavailable(self) -> None:
        self.assert_refused(self.browser().get("/api/auth/login"), "provider_unavailable")
        self.assertEqual(self.transactions(), [])

    def test_a_provider_that_answers_badly_starts_no_sign_in(self):
        self.assertEqual((provider_module.REQUEST_SECONDS, provider_module.MAX_ANSWER_BYTES), (10, 1024 * 1024))
        self.replace(provider_module, "REQUEST_SECONDS", 0.5)
        self.replace(provider_module, "MAX_ANSWER_BYTES", 20_000)
        good = self.provider.metadata()
        changes: Dict[str, Dict[str, Any]] = {
            "another issuer": {"issuer": self.provider.issuer + "/"},
            "no issuer": {"issuer": REMOVED},
            "an authorization address that is not allowed": {"authorization_endpoint": "http://login.example.org/auth"},
            "a token address that is not allowed": {"token_endpoint": "ftp://127.0.0.1/token"},
            "a key address that is not allowed": {"jwks_uri": "//127.0.0.1/certs"},
            "no authorization endpoint": {"authorization_endpoint": REMOVED},
            "no token endpoint": {"token_endpoint": REMOVED},
            "no key address": {"jwks_uri": REMOVED},
            "no S256": {"code_challenge_methods_supported": ["plain"]},
            "no PKCE": {"code_challenge_methods_supported": REMOVED},
            "S256 only as text": {"code_challenge_methods_supported": "S256"},
            "no allowed algorithm": {"id_token_signing_alg_values_supported": ["HS256", "none"]},
            "no algorithm": {"id_token_signing_alg_values_supported": REMOVED},
        }
        for name, change in changes.items():
            with self.subTest(name):
                provider_module.provider.reset()
                self.provider.metadata_changes = change
                self.unavailable()
        self.provider.metadata_changes = {}
        answers: Dict[str, Dict[str, Any]] = {
            "an error": dict(status=500, body=good), "not found": dict(status=404, body=good), "another 2xx": dict(status=203, body=good),
            "a redirect": dict(status=302, body=good, headers={"Location": self.provider.issuer + "/.well-known/openid-configuration?again"}),
            "no JSON": dict(body=b"<html>"), "no object": dict(body=[good]), "nothing": dict(body=b""),
            "too much": dict(body={**good, "padding": "x" * 20_000}), "too late": dict(body=good, wait=1.0),
            "deep JSON": dict(body=b"[" * 15_000),
        }
        for name, answer in answers.items():
            with self.subTest(name):
                provider_module.provider.reset()
                before = len(self.provider.received)
                self.provider.answer("metadata", **answer)
                self.unavailable()
                self.assertEqual(len(self.provider.received), before + 1, "no redirect is followed")
        with self.subTest("no answer at all"):
            provider_module.provider.reset()
            self.settings(oidc_issuer_raw="http://127.0.0.1:9/realms/none")
            self.unavailable()

    def test_a_drip_of_bytes_ends_in_time(self):
        self.replace(provider_module, "REQUEST_SECONDS", 0.6)

        def drip(handler: Any, _: Any) -> None:
            handler.send_response(200)
            handler.send_header("Content-Length", "1000")
            handler.end_headers()
            try:
                for _ in range(40):
                    handler.wfile.write(b" ")
                    handler.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass

        self.provider.overrides[self.provider.paths("metadata")] = drip
        started = time.monotonic()
        self.unavailable()
        self.assertLess(time.monotonic() - started, 2.0)
        time.sleep(0.4)
        self.assertEqual([thread for thread in threading.enumerate() if thread.name == "oidc-provider"], [], "nothing is left behind")

    def test_a_certificate_that_does_not_verify_starts_no_sign_in(self):
        with FakeProvider(tls=self_signed(self.dir)) as secure:
            self.settings(oidc_issuer_raw=secure.issuer)
            self.unavailable()
            self.assertEqual(secure.received, [])

    def test_over_tls_the_whole_trip_is_the_same(self):
        certificate = self_signed(self.dir)
        with FakeProvider(tls=certificate) as secure, patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": str(self.dir / "certificate.pem")}):
            self.assertTrue(secure.issuer.startswith("https://"))
            self.settings(oidc_issuer_raw=secure.issuer)
            self.provider = secure
            self.assertEqual(self.me(self.signed_in())["sign_in"], "rohub")
            self.assertEqual([secure.count(name) for name in ("metadata", "token", "certs")], [1, 1, 1])

    def test_good_metadata_is_read_once_an_hour_and_a_failure_is_remembered(self):
        clock = self.clock()
        for _ in range(3):
            self.begin(self.browser())
        clock.advance(3599)
        self.begin(self.browser())
        self.assertEqual(self.provider.count("metadata"), 1)
        clock.advance(2)
        self.begin(self.browser())
        self.assertEqual(self.provider.count("metadata"), 2)

        clock.advance(3601)
        self.provider.answer("metadata", 503)
        for _ in range(3):
            self.assert_refused(self.browser().get("/api/auth/login"), "provider_unavailable")
        self.assertEqual(self.provider.count("metadata"), 3)
        clock.advance(14)
        self.provider.overrides.clear()
        self.assert_refused(self.browser().get("/api/auth/login"), "provider_unavailable")
        clock.advance(2)
        self.begin(self.browser())
        self.assertEqual(self.provider.count("metadata"), 4)

    def test_one_read_serves_everyone_who_waits(self):
        self.provider.answer("metadata", body=self.provider.metadata(), wait=0.4)
        results: List[int] = []
        threads = [threading.Thread(target=lambda: results.append(self.browser().get("/api/auth/login").status_code)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((results, self.provider.count("metadata"), len(self.transactions())), ([303] * 8, 1, 8))


def self_signed(directory: Any) -> Any:
    """A TLS context with a certificate nobody vouches for."""
    import datetime
    import ipaddress
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    path = directory / "certificate.pem"
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(path)
    return context


# ------------------------------------------------------------------------------------------------
# Section 6: a sign-in that succeeds
# ------------------------------------------------------------------------------------------------


class SuccessTests(OidcTestCase):
    def test_a_first_sign_in_creates_an_account_and_a_second_finds_it(self):
        before = len(self.accounts())
        browser, response = self.sign_in(redirect="/admin.html?x=y")
        self.assertEqual((response.status_code, response.headers["location"], response.headers["cache-control"], response.headers["referrer-policy"]),
                         (303, "/admin.html?x=y", "no-store", "no-referrer"))
        [exchange] = [request for request in self.provider.received if request.path == self.provider.paths("token")]
        self.assertEqual(set(exchange.form), {"grant_type", "code", "redirect_uri", "client_id", "code_verifier"})
        self.assertEqual((exchange.form["grant_type"], exchange.form["client_id"], exchange.form["redirect_uri"]),
                         (["authorization_code"], [self.provider.client_id], [f"{PUBLIC_URL}/api/auth/callback"]))
        self.assertNotIn("authorization", {name.lower() for name in exchange.headers})
        me = self.me(browser)
        self.assertEqual((me["user"]["username"], me["user"]["display_name"], me["user"]["email"], me["user"]["auth_provider"], me["user"]["role"]),
                         ("ada", "Ada Example", "ada@example.org", "oidc", "user"))
        [account] = self.accounts()[before:]
        self.assertEqual((account["password_hash"], account["email_verified"], account["external_subject"]), ("!", 1, "subject-ada"))

        _, response = self.sign_in(email="ada.new@example.org")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(len(self.accounts()), before + 1)
        self.assertEqual(self.accounts()[-1]["email"], "ada.new@example.org")
        events = [(event["user_id"], event["status_code"], json.loads(event["metadata"])) for event in self.audit("auth.login")]
        self.assertEqual(events, [(account["id"], 303, {"method": "rohub", "account_created": True}),
                                  (account["id"], 303, {"method": "rohub", "account_created": False})])
        self.assertEqual(self.provider.count("userinfo"), 0)

    def test_the_session_cookie_has_its_attributes(self):
        _, response = self.sign_in()
        cookie = self.set_cookies(response)[ROHUB_COOKIE]
        self.assertEqual({key: cookie[key] for key in cookie if key != "value"}, {"httponly": "", "samesite": "lax", "path": "/", "max-age": "43200"})
        self.assertEqual(list(self.set_cookies(response)), [ROHUB_COOKIE])
        token = cookie["value"].rsplit(".", 1)[0]
        [row] = self.rohub_sessions()
        self.assertEqual((row["token_hash"], row["issuer"], row["subject"], row["sid"], row["user_agent"]),
                         (hashlib.sha256(token.encode()).hexdigest(), self.provider.issuer, "subject-ada", "session-ada", "Increment2Test/1.0"))
        self.assertNotIn(token, self.stored_text())

    def test_each_sign_in_ends_the_earlier_session_and_a_refused_one_does_not(self):
        browser = self.signed_in()
        first = browser.cookies[ROHUB_COOKIE]
        _, response = self.sign_in(browser, email_verified=False)
        self.assert_refused(response, "email_not_verified")
        self.assertEqual(self.me(browser)["sign_in"], "rohub")
        self.signed_in(browser)
        self.assertNotEqual(browser.cookies[ROHUB_COOKIE], first)
        self.assertEqual(len(self.rohub_sessions()), 1)
        old = self.browser()
        old.cookies.set(ROHUB_COOKIE, first)
        self.assertEqual(old.get("/api/auth/me").status_code, 401)

        browser = self.password_browser()
        _, response = self.sign_in(browser, sub="subject-ben", email="ben@example.org")
        self.assertIn(PASSWORD_COOKIE, self.set_cookies(response))
        self.assertEqual(self.rows("SELECT * FROM sessions"), [])
        self.assertEqual(self.me(browser)["sign_in"], "rohub")

    def test_text_no_database_can_hold_does_not_stop_a_sign_in(self):
        browser = self.signed_in(name="Ada \ud800 Example", sid="broken \udfff", preferred_username=7)
        self.assertEqual(self.me(browser)["user"]["display_name"], "Ada ? Example")
        self.assertIsNone(self.rohub_sessions()[-1]["sid"])

    def test_the_providers_session_id_is_kept_up_to_a_bound(self):
        """A session without a kept id ends with its subject."""
        for length, kept in ((255, True), (256, False), (10_000, False)):
            with self.subTest(length=length):
                self.execute("DELETE FROM oidc_sessions")
                sid = "s" * length
                self.signed_in(sid=sid)
                self.assertEqual([row["sid"] for row in self.rohub_sessions()], [sid if kept else None])
                token = self.provider.sign(self.provider.logout_claims(sid=REMOVED))
                self.assertEqual(self.browser().post("/api/auth/backchannel-logout", data={"logout_token": token}).status_code, 200)
                self.assertEqual(self.rohub_sessions(), [])


class SecureCookieTests(OidcTestCase):
    public_url = "https://iadopt.example.org"

    def test_secure_cookies_are_bound_to_the_host(self):
        browser = self.browser()
        start = browser.get("/api/auth/login")
        cookie = self.set_cookies(start)[f"__Host-{BROWSER_COOKIE}"]
        self.assertEqual({key: cookie[key] for key in cookie if key != "value"}, {"httponly": "", "samesite": "lax", "path": "/", "max-age": "600", "secure": ""})
        response = self.come_back(browser, self.provider.authorize(start.headers["location"]))
        cookie = self.set_cookies(response)[f"__Host-{ROHUB_COOKIE}"]
        self.assertEqual({key: cookie[key] for key in cookie if key != "value"}, {"httponly": "", "samesite": "lax", "path": "/", "max-age": "43200", "secure": ""})
        self.assertEqual(self.me(browser)["sign_in"], "rohub")
        response = browser.post("/api/auth/logout", headers=self.csrf_headers(browser))
        removed = self.set_cookies(response)[f"__Host-{ROHUB_COOKIE}"]
        self.assertEqual((removed["max-age"], removed["path"], "secure" in removed, "httponly" in removed, removed["samesite"], "domain" in removed),
                         ("0", "/", True, True, "lax", False))

    def test_a_cookie_that_another_host_could_set_is_not_taken_for_ours(self):
        attacker = self.browser()
        location = self.begin(attacker)
        key = attacker.cookies[f"__Host-{BROWSER_COOKIE}"]
        session = self.signed_in().cookies[f"__Host-{ROHUB_COOKIE}"]
        mine, ours = f"__Host-{BROWSER_COOKIE}={key}".encode("ascii"), f"__Host-{ROHUB_COOKIE}={session}".encode("ascii")
        before = len(self.rohub_sessions())
        for prefix in (b"\xa0", b"\x85", b"\x1f", b"\x0b", b"x", b"=", b"\xc2\xa0"):
            with self.subTest(prefix=prefix):
                query = self.provider.authorize(location)
                victim = self.browser()
                response = victim.get(f"/api/auth/callback?{urlencode(query)}", headers=[(b"Cookie", prefix + mine)])
                self.assert_refused(response, "sign_in_expired")
                self.assertEqual(victim.get("/api/auth/me", headers=[(b"Cookie", prefix + ours)]).status_code, 401)
        for name, line in {"twice": ours + b"; " + ours, "its plain name": ours.removeprefix(b"__Host-"), "another case": ours.replace(b"__Host-", b"__host-"),
                           "after its name": ours.replace(b"=", b"\xa0=", 1)}.items():
            with self.subTest(name):
                self.assertEqual(self.browser().get("/api/auth/me", headers=[(b"Cookie", line)]).status_code, 401)
        self.assertEqual(len(self.rohub_sessions()), before)
        for name, lines in {"among others": [b"a=b; " + ours + b";c=d"], "in a line of its own": [b"a=b", ours]}.items():
            with self.subTest(name):
                self.assertEqual(self.browser().get("/api/auth/me", headers=[(b"Cookie", line) for line in lines]).status_code, 200)


# ------------------------------------------------------------------------------------------------
# Section 6 and 7: a sign-in that is refused
# ------------------------------------------------------------------------------------------------


class RefusalTestCase(OidcTestCase):
    """Each case starts and ends no session and creates or changes no account."""

    def setUp(self) -> None:
        super().setUp()
        self.bystander = self.password_browser()
        self.before = (self.accounts(), self.rows("SELECT * FROM account_identities"))

    def assert_nothing_happened(self, response: Any, code: str, *, audited: bool = False, rule: Any = None, user: Any = None) -> None:
        self.assert_refused(response, code)
        self.assertNotIn(ROHUB_COOKIE, self.set_cookies(response))
        self.assertEqual(self.rohub_sessions(), [])
        self.assertEqual((self.accounts(), self.rows("SELECT * FROM account_identities")), self.before)
        self.assertEqual(self.me(self.bystander)["sign_in"], "password")
        events = self.audit("auth.login_failed")
        self.execute("DELETE FROM audit_events WHERE action = 'auth.login_failed'")
        if not audited:
            self.assertEqual(events, [])
            return
        [event] = events
        metadata = json.loads(event["metadata"])
        self.assertEqual((metadata["code"], metadata.get("rule"), event["user_id"], event["username"]),
                         (code, rule, user["id"] if user else None, user["username"] if user else ""))
        self.assertEqual((event["request_payload"], event["response_payload"], event["path"]), (None, None, "/api/auth/callback"))
        for value in ("subject-ada", "ada@example.org", "Ada Example", "provider text", self.provider.issuer):
            self.assertNotIn(value, json.dumps(event))


class ReturnRefusalTests(RefusalTestCase):
    def test_an_unknown_or_expired_state_and_another_browser_are_refused(self):
        browser = self.browser()
        query = self.provider.authorize(self.begin(browser))
        for name, change in {"unknown": {"state": "unknown"}, "empty": {"state": ""}, "longer": {"state": query["state"] + "x"}}.items():
            with self.subTest(name):
                self.assert_nothing_happened(self.come_back(browser, {**query, **change}), "sign_in_expired")
        with self.subTest("no state"):
            self.assert_nothing_happened(self.come_back(browser, {"code": query["code"], "iss": query["iss"]}), "sign_in_expired")
        with self.subTest("two states"):
            response = browser.get(f"/api/auth/callback?state={query['state']}&state=x&code={query['code']}")
            self.assert_nothing_happened(response, "sign_in_expired")
        for name, key in {"no key": None, "another key": "k" * 43, "a malformed key": "k"}.items():
            with self.subTest(name):
                other = self.browser()
                if key:
                    other.cookies.set(BROWSER_COOKIE, key)
                self.assert_nothing_happened(self.come_back(other, query), "sign_in_expired")
        self.assertEqual(len(self.transactions()), 1, "another browser's request deletes nothing")
        self.assertEqual(self.provider.count("token"), 0)
        self.assertEqual(self.come_back(browser, query).headers["location"], "/")

    def test_an_expired_transaction_is_refused_and_deleted(self):
        browser = self.browser()
        query = self.provider.authorize(self.begin(browser, "/admin.html"))
        self.execute("UPDATE oidc_transactions SET expires_at = ?", utc_iso(utc_now() - timedelta(seconds=1)))
        response = self.come_back(browser, query)
        self.assert_nothing_happened(response, "sign_in_expired")
        self.assert_refused(response, "sign_in_expired", "/admin.html")
        self.assertEqual((self.transactions(), self.provider.count("token")), ([], 0))

    def test_a_transaction_serves_once(self):
        browser = self.browser()
        query = self.provider.authorize(self.begin(browser))
        self.come_back(browser, query)
        tokens = self.provider.count("token")
        self.assert_refused(self.come_back(browser, query), "sign_in_expired")
        self.assertEqual((self.provider.count("token"), len(self.rohub_sessions())), (tokens, 1))

    def test_of_two_returns_at_once_exactly_one_signs_in(self):
        for _ in range(3):
            browser = self.browser()
            query = self.provider.authorize(self.begin(browser))
            key, locations, barrier = browser.cookies[BROWSER_COOKIE], [], threading.Barrier(2)

            def go() -> None:
                twin = self.browser()
                twin.cookies.set(BROWSER_COOKIE, key)
                barrier.wait()
                locations.append(self.come_back(twin, query).headers["location"])

            threads = [threading.Thread(target=go) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sorted(locations), ["/", "/login.html?error=sign_in_expired"])

    def test_the_providers_refusal_is_not_shown(self):
        for error, code in (("access_denied", "cancelled"), ("server_error", "provider_refused"), ("", "provider_refused"),
                            ("invalid_request", "provider_refused")):
            with self.subTest(error=error):
                browser = self.browser()
                query = self.provider.authorize(self.begin(browser))
                before = len(self.provider.received)
                response = self.come_back(browser, {"error": error, "error_description": "provider text", "state": query["state"],
                                                    "iss": self.provider.issuer, "code": query["code"]})
                self.assert_nothing_happened(response, code)
                self.assertNotIn("provider", response.headers["location"].replace("provider_refused", ""))
                self.assertEqual(len(self.provider.received), before)
                self.assertNotIn("provider text", self.stored_text())

    def test_the_issuer_of_the_return_must_be_the_configured_one(self):
        for name, iss in {"another": "https://login.example.org/x", "empty": "", "with a slash": self.provider.issuer + "/", "none": None}.items():
            with self.subTest(name):
                browser = self.browser()
                query = self.provider.authorize(self.begin(browser))
                query = {**{key: value for key, value in query.items() if key != "iss"}, **({} if iss is None else {"iss": iss})}
                self.assert_nothing_happened(self.come_back(browser, query), "provider_refused")
        self.assertEqual(self.provider.count("token"), 0)
        provider_module.provider.reset()
        self.provider.metadata_changes = {"authorization_response_iss_parameter_supported": REMOVED}
        browser = self.browser()
        query = self.provider.authorize(self.begin(browser))
        del query["iss"]
        self.assertEqual(self.come_back(browser, query).headers["location"], "/", "a provider that does not declare it need not give it")

    def test_a_code_that_is_missing_or_too_long_is_refused_without_a_request(self):
        for name, code in {"missing": None, "empty": "", "too long": "x" * 4097}.items():
            with self.subTest(name):
                browser = self.browser()
                query = self.provider.authorize(self.begin(browser))
                query = {**{key: value for key, value in query.items() if key != "code"}, **({} if code is None else {"code": code})}
                self.assert_nothing_happened(self.come_back(browser, query), "provider_refused")
        browser = self.browser()
        query = self.provider.authorize(self.begin(browser))
        response = browser.get(f"/api/auth/callback?state={query['state']}&iss={query['iss']}&code={query['code']}&code=x")
        self.assert_nothing_happened(response, "provider_refused")
        self.assertEqual(self.provider.count("token"), 0)

    def test_what_the_token_endpoint_answers(self):
        self.replace(provider_module, "REQUEST_SECONDS", 0.5)
        self.replace(provider_module, "MAX_ANSWER_BYTES", 20_000)
        good = {"access_token": "access", "token_type": "Bearer", "expires_in": 300}
        answers: Dict[str, tuple] = {
            "400": (dict(status=400, body={"error": "invalid_grant", "error_description": "provider text"}), "provider_refused"),
            "401": (dict(status=401), "provider_refused"), "429": (dict(status=429, body=b"provider text"), "provider_refused"),
            "500": (dict(status=500), "provider_unavailable"), "503": (dict(status=503, body=good), "provider_unavailable"),
            "a redirect": (dict(status=307, headers={"Location": self.provider.url("token") + "?again"}), "provider_unavailable"),
            "another 2xx": (dict(status=201, body={**good, "id_token": "x"}), "provider_unavailable"),
            "no JSON": (dict(body=b"provider text"), "provider_unavailable"), "no object": (dict(body=[good]), "provider_unavailable"),
            "too much": (dict(body={**good, "padding": "x" * 20_000}), "provider_unavailable"),
            "too late": (dict(body=good, wait=1.0), "provider_unavailable"),
            "no ID token": (dict(body=good), "provider_unavailable"), "no text": (dict(body={**good, "id_token": 7}), "provider_unavailable"),
            "an empty one": (dict(body={**good, "id_token": ""}), "provider_unavailable"),
            "an error with 200": (dict(body={"error": "x", "error_description": "provider text"}), "provider_unavailable"),
            "a lifetime that is no number": (dict(body={**good, "expires_in": "soon"}), "provider_unavailable"),
        }
        for name, (answer, code) in answers.items():
            with self.subTest(name):
                browser = self.browser()
                query = self.provider.authorize(self.begin(browser))
                before = self.provider.count("token")
                self.provider.answer("token", **answer)
                self.assert_nothing_happened(self.come_back(browser, query), code)
                self.assertEqual(self.provider.count("token"), before + 1)
                self.assertNotIn("provider text", self.stored_text())

    def test_keys_that_cannot_be_read(self):
        for name, answer in {"an error": dict(status=500, body=self.provider.jwks()), "no JSON": dict(body=b"x"), "no key set": dict(body={"keys": "x"})}.items():
            with self.subTest(name):
                provider_module.provider.reset()
                browser = self.browser()
                query = self.provider.authorize(self.begin(browser))
                self.provider.answer("certs", **answer)
                self.assert_nothing_happened(self.come_back(browser, query), "provider_unavailable")

    def test_a_wrong_verifier_is_the_providers_refusal(self):
        browser = self.browser()
        query = self.provider.authorize(self.begin(browser))
        self.execute("UPDATE oidc_transactions SET code_verifier = ?", "v" * 43)
        self.assert_nothing_happened(self.come_back(browser, query), "provider_refused")

    def test_beyond_the_limit_nothing_is_stored_or_sent(self):
        browser = self.browser()
        query = self.provider.authorize(self.begin(browser))
        sent = len(self.provider.received)
        self.settings(sign_in_limit_raw="1/300")
        from app import guards

        self.replace(guards, "limiter", guards.RateLimiter())
        self.begin(browser)
        stored = len(self.transactions())
        self.assert_nothing_happened(browser.get("/api/auth/login?redirect=/admin.html"), "rate_limited")
        self.assertEqual(len(self.transactions()), stored)
        self.assertEqual(self.come_back(browser, {"state": "x"}).headers["location"], "/login.html?error=sign_in_expired")
        self.assert_nothing_happened(self.come_back(browser, query), "rate_limited")
        self.assertEqual(len(self.provider.received), sent)


class IdTokenTests(RefusalTestCase):
    def refuse(self, rule: str, **kwargs: Any) -> None:
        _, response = self.sign_in(**kwargs)
        self.assert_nothing_happened(response, "token_invalid", audited=True, rule=rule)

    def accept(self, **kwargs: Any) -> None:
        _, response = self.sign_in(**kwargs)
        self.assertEqual(response.headers["location"], "/", self.audit("auth.login_failed"))
        for table in ("oidc_sessions", "account_identities"):
            self.execute(f"DELETE FROM {table}")
        self.execute("DELETE FROM users WHERE auth_provider = 'oidc'")

    def test_the_issuer(self):
        for iss in ("https://login.example.org/x", self.provider.issuer + "/", "", REMOVED, 7, [self.provider.issuer]):
            with self.subTest(iss=str(iss)):
                self.refuse("iss", iss=iss)

    def test_the_audience_and_the_authorized_party(self):
        me, other = self.provider.client_id, "another-client"
        for aud, azp, rule in ((REMOVED, me, "aud"), (other, me, "aud"), ([], me, "aud"), ("", me, "aud"), (7, me, "aud"), ([me, 7], me, "aud"),
                               ([me, other], REMOVED, "aud"), ([other], me, "aud"), (me, other, "azp"), ([me, other], other, "azp"),
                               (me, None, "azp"), (me, [me], "azp"), (me, "", "azp")):
            with self.subTest(aud=str(aud), azp=str(azp)):
                self.refuse(rule, aud=aud, azp=azp)
        for aud, azp in (([me, other], me), ([me], REMOVED), (me, REMOVED), (me, me)):
            with self.subTest(accepted=str(aud), azp=str(azp)):
                self.accept(aud=aud, azp=azp)

    def test_the_nonce(self):
        for nonce in (REMOVED, "another", "", None, 7):
            with self.subTest(nonce=str(nonce)):
                self.refuse("nonce", nonce=nonce)
        browser = self.browser()
        location = self.begin(browser)
        nonce = query_of(location)["nonce"][0]
        self.assert_nothing_happened(self.come_back(browser, self.provider.authorize(location, nonce=[nonce])), "token_invalid", audited=True, rule="nonce")

    def test_the_times(self):
        now = int(time.time())
        for name, rule, changes in (
            ("expired", "exp", {"exp": now - 90}), ("no exp", "exp", {"exp": REMOVED}), ("exp as text", "exp", {"exp": str(now + 300)}),
            ("exp true", "exp", {"exp": True}), ("exp null", "exp", {"exp": None}), ("exp infinite", "exp", {"exp": float("inf")}),
            ("exp NaN", "exp", {"exp": float("nan")}),
            ("iat in the future", "iat", {"iat": now + 120}), ("iat before the sign-in", "iat", {"iat": now - 120}),
            ("no iat", "iat", {"iat": REMOVED}), ("iat as text", "iat", {"iat": str(now)}), ("iat false", "iat", {"iat": False}),
            ("nbf in the future", "nbf", {"nbf": now + 120}), ("nbf as text", "nbf", {"nbf": "0"}), ("nbf true", "nbf", {"nbf": True}),
            ("nbf null", "nbf", {"nbf": None}),
        ):
            with self.subTest(name):
                self.refuse(rule, **changes)
        for name, changes in (
            ("expired within the leeway", {"exp": now - 30}), ("iat slightly ahead", {"iat": now + 30}), ("iat slightly before", {"iat": now - 30}),
            ("nbf slightly ahead", {"nbf": now + 30}), ("nbf in the past", {"nbf": now - 300}), ("no nbf", {}), ("exp with a fraction", {"exp": now + 300.5}),
        ):
            with self.subTest(accepted=name):
                self.accept(**changes)

    def test_the_leeway_is_a_minute_to_the_second(self):
        now = 1_900_000_000

        def rule_of(**changes: Any) -> Any:
            claims = {**self.provider.claims("nonce"), "iat": now, "exp": now + 300, **changes}
            try:
                oidc_tokens.id_token_claims(
                    self.provider.sign(claims), algorithms=("RS256",), find_key=lambda *_: self.provider.key, issuer=self.provider.issuer,
                    client_id=self.provider.client_id, nonce="nonce", began_at=now, now=now,
                )
            except oidc_tokens.TokenInvalid as e:
                return e.rule
            return None

        # A token is no longer good at the second of its `exp` (RFC 7519), so the leeway's last second is the 59th.
        for claim, inside, outside in (("exp", now - 59, now - 60), ("iat", now + 60, now + 61), ("iat", now - 60, now - 61), ("nbf", now + 60, now + 61)):
            with self.subTest(claim=claim, seconds=inside - now):
                self.assertEqual((rule_of(**{claim: inside}), rule_of(**{claim: outside})), (None, claim))

    def test_the_signature_and_the_algorithm(self):
        mine, public = RSAKey.generate_key(2048, {"kid": "key-1"}), self.provider.key.as_pem(private=False)
        signed = self.provider.sign
        encryption = RSAKey.generate_key(2048, {"kid": "key-1"})
        attacker_curve = ECKey.generate_key("P-256", {"kid": "key-1"})
        attacker = {"jwk": attacker_curve.as_dict(private=False)}  # the library refuses the header of a large key as too long
        small = RSAKey.generate_key(1024, {"kid": "small", "use": "sig"})
        wrong_use = RSAKey.generate_key(2048, {"kid": "enc-key", "use": "enc"})
        other_alg = RSAKey.generate_key(2048, {"kid": "ps-key", "alg": "PS256"})
        self.provider.published += [small, wrong_use, other_alg]
        cases: Dict[str, tuple] = {
            "a changed payload": ("signature", lambda c: ".".join([signed(c).split(".")[0], b64(json.dumps({**c, "sub": "subject-ben"}).encode()), signed(c).split(".")[2]])),
            "another key": ("signature", lambda c: signed(c, key=mine)),
            "no signature": ("signature", lambda c: ".".join(signed(c).split(".")[:2]) + "."),
            "alg none": ("algorithm", lambda c: signed(c, alg="none")),
            "alg None": ("algorithm", lambda c: signed(c, alg="none", header={"alg": "None"})),
            "alg nOnE without kid": ("algorithm", lambda c: signed(c, alg="none", header={"alg": "nOnE", "kid": REMOVED})),
            "a shared key over the public key": ("algorithm", lambda c: shared_key_jws({"alg": "HS256", "kid": "key-1"}, json.dumps(c).encode(), public)),
            "RS384, not accepted": ("algorithm", lambda c: signed(c, alg="RS384")),
            "no alg": ("algorithm", lambda c: signed(c, header={"alg": REMOVED})),
            "alg as a list": ("form", lambda c: signed(c, header={"alg": ["RS256"]})),
            "a key in the header": ("signature", lambda c: signed(c, key=attacker_curve, alg="ES256", header=attacker)),
            "a key in the header, signed by the provider": ("signature", lambda c: signed(c, header=attacker)),
            "a large key in the header": ("form", lambda c: signed(c, key=mine, header={"jwk": mine.as_dict(private=False)})),
            "a key address": ("signature", lambda c: signed(c, key=mine, header={"jku": "https://evil.example/keys"})),
            "a certificate": ("signature", lambda c: signed(c, header={"x5c": ["MIIB"]})),
            "a certificate address": ("signature", lambda c: signed(c, header={"x5u": "https://evil.example/cert"})),
            "an unknown critical header": ("form", lambda c: signed(c, header={"crit": ["x-new"], "x-new": 1})),
            "a critical header that is missing": ("form", lambda c: signed(c, header={"crit": ["exp"]})),
            "a payload that is not encoded": ("form", lambda c: signed(c, header={"crit": ["b64"], "b64": False})),
            "a critical header that is no list": ("form", lambda c: signed(c, header={"crit": 5})),
            "an encrypted token": ("form", lambda c: encrypt_compact({"alg": "RSA-OAEP", "enc": "A128GCM", "kid": "key-1"}, json.dumps(c), encryption)),
            "no token": ("form", lambda c: "not a token"),
            "JSON": ("form", lambda c: json.dumps(c)),
            "four parts": ("form", lambda c: signed(c) + ".x"),
            "no object": ("form", lambda c: raw_jws({"alg": "RS256", "kid": "key-1"}, b"[]", self.provider.key, "RS256")),
            "no JSON payload": ("form", lambda c: raw_jws({"alg": "RS256", "kid": "key-1"}, b"{", self.provider.key, "RS256")),
            "a key for encryption": ("signature", lambda c: signed(c, key=wrong_use)),
            "a key of another algorithm": ("signature", lambda c: signed(c, key=other_alg)),
            "a key that is too small": ("signature", lambda c: signed(c, key=small)),
            "an unknown key": ("signature", lambda c: signed(c, key=mine, header={"kid": "unknown"})),
            "a kid that is no text": ("form", lambda c: signed(c, header={"kid": 7})),
        }
        for name, (rule, make) in cases.items():
            with self.subTest(name):
                self.refuse(rule, id_token=make)
        self.accept(id_token=lambda c: signed(c, header={"x-unknown": "ignored", "typ": REMOVED}))
        self.accept(id_token=lambda c: signed(c, key=other_alg, alg="PS256"))

    def test_an_algorithm_the_metadata_does_not_list(self):
        self.provider.metadata_changes = {"id_token_signing_alg_values_supported": ["PS256"]}
        self.refuse("algorithm")

    def test_the_accepted_algorithms(self):
        curve = ECKey.generate_key("P-256", {"kid": "ec-1", "use": "sig"})
        self.provider.published = [RSAKey.import_key(self.provider.key.as_dict(private=True) | {"alg": "PS256"}), curve]
        self.accept(id_token=lambda c: self.provider.sign(c, alg="PS256"))
        self.accept(id_token=lambda c: self.provider.sign(c, key=curve, alg="ES256"))
        self.refuse("signature", id_token=lambda c: self.provider.sign(c, key=curve, alg="ES256", header={"kid": "key-1"}))

    def test_a_token_without_a_key_id(self):
        encryption = RSAKey.generate_key(2048, {"kid": "enc", "use": "enc"})
        self.provider.published = [encryption, self.provider.key]
        self.accept(id_token=lambda c: self.provider.sign(c, header={"kid": REMOVED}))
        second = RSAKey.generate_key(2048, {"kid": "key-2", "use": "sig"})
        self.provider.published.append(second)
        provider_module.provider.reset()
        self.refuse("signature", id_token=lambda c: self.provider.sign(c, header={"kid": REMOVED}))
        self.accept(id_token=lambda c: self.provider.sign(c, key=second))

    def test_an_unknown_key_id_reads_the_key_set_once_more_at_most_once_a_minute(self):
        clock = self.clock()
        self.accept()
        self.accept()
        self.assertEqual(self.provider.count("certs"), 1)
        for kid in ("key-2", "key-3"):
            fresh = RSAKey.generate_key(2048, {"kid": kid, "use": "sig"})
            if kid == "key-2":
                self.provider.published = [fresh]
                self.accept(id_token=lambda c: self.provider.sign(c, key=fresh))
                self.assertEqual(self.provider.count("certs"), 2)
                continue
            self.provider.published = [fresh]
            self.refuse("signature", id_token=lambda c: self.provider.sign(c, key=fresh))
            clock.advance(59)
            self.refuse("signature", id_token=lambda c: self.provider.sign(c, key=fresh))
            self.assertEqual(self.provider.count("certs"), 2)
            clock.advance(2)
            self.accept(id_token=lambda c: self.provider.sign(c, key=fresh))
            self.assertEqual(self.provider.count("certs"), 3)

    def test_keys_the_library_cannot_use_do_not_spoil_the_set(self):
        self.provider.answer("certs", body={"keys": [None, 7, "x", {}, {"kty": "RSA"}, {"kty": "new"}, {"kty": "oct", "k": "AAAA", "kid": "key-1"},
                                                     {"kty": "EC", "crv": "P-256", "x": "x", "y": "y"}, *self.provider.jwks()["keys"]]})
        self.accept()

    def test_the_email_must_be_verified(self):
        for changes in ({"email_verified": REMOVED}, {"email_verified": False}, {"email_verified": "true"}, {"email_verified": 1},
                        {"email_verified": None}, {"email": REMOVED}, {"email": ""}, {"email": "  "}, {"email": 7}, {"email": None}):
            with self.subTest(changes=str(changes)):
                _, response = self.sign_in(**changes)
                self.assert_nothing_happened(response, "email_not_verified", audited=True)
        self.signed_in()
        self.execute("DELETE FROM oidc_sessions")
        self.before = (self.accounts(), self.rows("SELECT * FROM account_identities"))
        _, response = self.sign_in(email="new@example.org", email_verified=False, name="Another")
        self.assert_nothing_happened(response, "email_not_verified", audited=True)

    def test_the_subject(self):
        for sub in (REMOVED, "", "x" * 256, 7, None, ["x"], "broken \ud800"):
            with self.subTest(sub=str(sub)[:20]):
                self.refuse("sub", sub=sub)
        self.accept(sub="x" * 255)

    def test_a_blocked_account(self):
        self.signed_in()
        self.execute("DELETE FROM oidc_sessions")
        account = self.accounts()[-1]
        self.execute("UPDATE users SET is_active = 0 WHERE id = ?", account["id"])
        self.before = (self.accounts(), self.rows("SELECT * FROM account_identities"))
        _, response = self.sign_in(self.bystander, email="new@example.org", name="Another")
        self.assert_nothing_happened(response, "blocked", audited=True, user=account)

    def test_nobody_is_named_when_the_browser_has_anothers_session(self):
        _, response = self.sign_in(self.bystander, iss="https://login.example.org/x")
        self.assert_nothing_happened(response, "token_invalid", audited=True, rule="iss")

    def test_an_account_blocked_meanwhile_gets_no_session(self):
        self.signed_in()
        self.execute("DELETE FROM oidc_sessions")
        account = self.accounts()[-1]
        real = core_deps.account_store.ensure_account

        def blocked_meanwhile(*args: Any) -> Any:
            found = real(*args)
            self.execute("UPDATE users SET is_active = 0 WHERE id = ?", account["id"])
            return found

        with patch.object(core_deps.account_store, "ensure_account", blocked_meanwhile):
            _, response = self.sign_in()
        self.assert_refused(response, "blocked")
        self.assertEqual(self.rohub_sessions(), [])


class LeakTests(OidcTestCase):
    def test_nothing_of_the_trip_is_kept_or_logged(self):
        answers: List[Dict[str, Any]] = []
        real = self.provider.exchange

        def remember(request: Any) -> Any:
            status, body = real(request)
            answers.append({**body, "code": request.form["code"][0], "verifier": request.form["code_verifier"][0]})
            return status, body

        self.replace(self.provider, "exchange", remember)
        secrets_seen: List[str] = []
        own_cookies: List[str] = []
        responses: List[Any] = []
        with self.assertLogs(level=logging.DEBUG) as logged:
            for changes in ({}, {"email_verified": False}, {"nonce": "another"}, {"sub": ""}):
                browser = self.browser()
                location = self.begin(browser)
                query = self.provider.authorize(location, **changes)
                trip = query_of(location)
                secrets_seen += [trip["state"][0], trip["nonce"][0], trip["code_challenge"][0], query["code"]]
                own_cookies.append(browser.cookies[BROWSER_COOKIE])
                responses.append(self.come_back(browser, query))
                if not changes:
                    responses += [browser.get("/api/auth/me"), browser.post("/api/auth/logout", headers=self.csrf_headers(browser))]
                    own_cookies.append(responses[-3].headers["set-cookie"].split("=", 1)[1].split(";")[0])
            browser = self.browser()
            query = self.provider.authorize(self.begin(browser))
            responses.append(self.come_back(browser, {"error": "server_error", "error_description": "provider text", "state": query["state"], "iss": query["iss"]}))
            secrets_seen += [query["state"], query["code"], "provider text"]
            self.signed_in()
            token = self.provider.sign(self.provider.logout_claims())
            responses.append(self.browser().post("/api/auth/backchannel-logout", data={"logout_token": token}))
            secrets_seen.append(token)
        for answer in answers:
            secrets_seen += [str(answer[name]) for name in ("access_token", "refresh_token", "id_token", "code", "verifier")]
        # The test client logs the addresses it asks for; the application's own log is everything else.
        ours = [record.getMessage() for record in logged.records if record.name.split(".")[0] not in ("httpx", "httpcore", "asyncio")]
        text, log = self.stored_text(), "\n".join(ours)
        for secret in secrets_seen + own_cookies:
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, log)
        for response in responses:
            shown = response.text + "".join(value for name, value in response.headers.multi_items() if name != "set-cookie")
            cookies = "".join(value for name, value in response.headers.multi_items() if name == "set-cookie")
            for secret in secrets_seen:
                self.assertNotIn(secret, shown)
                self.assertNotIn(secret, cookies)
            for value in own_cookies:
                self.assertNotIn(value, shown)
        self.assertEqual(self.transactions(), [])

    def test_malformed_headers_of_the_provider_are_not_logged(self):

        def malformed(handler: Any, _: Any) -> None:
            handler.wfile.write(b"HTTP/1.1 400 Bad Request\r\nbroken provider-secret\r\nContent-Length: 2\r\n\r\n{}")
            handler.close_connection = True

        for name, code in (("token", "provider_refused"), ("certs", "provider_unavailable")):
            with self.subTest(name), self.assertLogs(level=logging.DEBUG) as logged:
                provider_module.provider.reset()
                self.provider.overrides.clear()
                browser = self.browser()
                query = self.provider.authorize(self.begin(browser))
                self.provider.overrides[self.provider.paths(name)] = malformed
                self.assert_refused(self.come_back(browser, query), code)
            self.assertTrue(any(record.name == "urllib3.connectionpool" for record in logged.records), "the library logs")
            self.assertNotIn("provider-secret", "\n".join(logging.Formatter().format(record) for record in logged.records))


# ------------------------------------------------------------------------------------------------
# Section 8: sessions
# ------------------------------------------------------------------------------------------------


class SessionTests(OidcTestCase):
    def test_a_session_ends_at_its_lifetime_however_much_it_is_used(self):
        browser = self.signed_in()
        [row] = self.rohub_sessions()
        for _ in range(3):
            self.me(browser)
        self.assertEqual(self.rohub_sessions(), [row], "use does not extend it")
        self.execute("UPDATE oidc_sessions SET expires_at = ?", utc_iso(utc_now() - timedelta(seconds=1)))
        self.assertEqual(browser.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.rohub_sessions(), [])

    def test_a_block_ends_both_kinds_and_changes_are_in_force_at_once(self):
        browser = self.signed_in()
        account = self.me(browser)["user"]
        with storage.transaction(path=self.store.db_path) as conn:
            conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES ('x', ?, 't', '9')", (account["id"],))
        code, out = run_cli("owner", "bind", "--username", "admin")
        self.assertEqual(code, 0, out)
        owner = self.password_browser()
        self.assertEqual(browser.get("/api/admin/stats").status_code, 403)
        self.assertEqual(owner.post(f"/api/admin/users/{account['id']}/grant-admin", json={}).status_code, 200)
        self.assertEqual(browser.get("/api/admin/stats").status_code, 200)
        self.assertEqual(browser.get("/api/auth/verify/admin").status_code, 204)
        self.assertEqual(owner.post(f"/api/admin/users/{account['id']}/revoke-admin", json={}).status_code, 200)
        self.assertEqual(browser.get("/api/admin/stats").status_code, 403)
        self.assertEqual(owner.post(f"/api/admin/users/{account['id']}/block", json={"reason": "A test."}).status_code, 200)
        self.assertEqual((self.rohub_sessions(), self.rows("SELECT * FROM sessions WHERE token_hash = 'x'")), ([], []))
        self.assertEqual(browser.get("/api/auth/me").status_code, 401)

    def test_a_session_counts_only_while_its_account_is_not_blocked(self):
        """Also when the account was blocked without the block route, for example by the previous image."""
        browser = self.signed_in()
        self.execute("UPDATE users SET is_active = 0 WHERE auth_provider = 'oidc'")
        self.assertEqual(browser.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.rohub_sessions(), [])

    def test_a_session_of_another_issuer_does_not_count(self):
        browser = self.signed_in()
        self.settings(oidc_issuer_raw=ISSUER)
        self.assertEqual(browser.get("/api/auth/me").status_code, 401)
        self.settings(oidc_issuer_raw=self.provider.issuer)
        self.assertEqual(self.me(browser)["sign_in"], "rohub")

    def test_a_forged_or_moved_cookie_opens_nothing(self):
        browser = self.signed_in()
        value = browser.cookies[ROHUB_COOKIE]
        token, signature = value.rsplit(".", 1)
        for name, cookie in ((ROHUB_COOKIE, f"{token}.{'0' * 64}"), (ROHUB_COOKIE, token), (ROHUB_COOKIE, f"x{value}"), (PASSWORD_COOKIE, value),
                             (ROHUB_COOKIE, f"csrf:{token}.{self.me(browser)['csrf_token']}"), (ROHUB_COOKIE, f"{token}.{signature}é")):
            with self.subTest(name=name, cookie=cookie[:20]):
                other = self.browser()
                response = other.get("/api/auth/me", headers={"Cookie": f"{name}={cookie}".encode("utf-8")})
                self.assertEqual(response.status_code, 401)

    def test_expired_sessions_are_deleted(self):
        self.signed_in()
        self.execute("UPDATE oidc_sessions SET expires_at = ?", utc_iso(utc_now() - timedelta(seconds=1)))
        self.signed_in(sub="subject-ben", email="ben@example.org")
        self.assertEqual([row["subject"] for row in self.rohub_sessions()], ["subject-ben"])
        self.execute("UPDATE oidc_sessions SET expires_at = ?", utc_iso(utc_now() - timedelta(seconds=1)))
        self.store.cleanup_old_audit(force=True)
        self.assertEqual(self.rohub_sessions(), [])

    def test_the_previous_image_finds_no_rohub_session(self):
        import previous_image

        browser = self.signed_in()
        token = browser.cookies[ROHUB_COOKIE].rsplit(".", 1)[0]
        conn = previous_image.connect(self.store.db_path)
        try:
            names = previous_image.data()["named"]
            lookups = [name for name in names if "FROM sessions" in names[name]["sql"]]
            self.assertTrue(lookups, list(names))
            for name in lookups:
                statement = names[name]["sql"]
                if statement.lstrip().upper().startswith("SELECT"):
                    row = conn.execute(statement, (hashlib.sha256(token.encode()).hexdigest(), utc_iso())[: statement.count("?")]).fetchone()
                    self.assertIsNone(row)
            self.assertFalse(previous_image.password_login(conn, "ada", "!"))
        finally:
            conn.close()
        self.assertEqual(previous_image.start(self.store.db_path), [])

        fresh = self.dir / "fresh.sqlite3"
        storage.migrate(fresh)
        self.use_database(fresh)
        self.signed_in()
        self.assertEqual(previous_image.replay(fresh), [], "everything the previous image writes and reads")
        self.assertEqual(len(self.rohub_sessions()), 1)


def tables(path: Any) -> List[str]:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------------
# Section 9: CSRF and signing out
# ------------------------------------------------------------------------------------------------


class CsrfTests(OidcTestCase):
    def test_every_unsafe_route_needs_the_token_and_the_origin(self):
        from test_access_policy import fill

        from app.access.dependencies import ROUTE_CAPABILITIES
        from app.routers import decompose, nanopub

        browser, other = self.signed_in(), self.signed_in(sub="subject-ben", email="ben@example.org", sid="session-ben")
        code, out = run_cli("owner", "bind", "--issuer", self.provider.issuer, "--subject", "subject-ada")
        self.assertEqual(code, 0, out)
        browser = self.signed_in()  # owner bind ended the session (owner-sign-in, section 4)
        good, foreign = self.csrf_headers(browser), self.csrf_headers(other)
        self.assertNotEqual(good["X-CSRF-Token"], foreign["X-CSRF-Token"])
        bad: Dict[str, Dict[str, str]] = {
            "no token": {"Origin": good["Origin"]}, "an empty token": {**good, "X-CSRF-Token": ""},
            "another session's token": {**good, "X-CSRF-Token": foreign["X-CSRF-Token"]},
            "no origin": {"X-CSRF-Token": good["X-CSRF-Token"]}, "another origin": {**good, "Origin": "http://evil.example"},
            "another port": {**good, "Origin": "http://localhost:5174"}, "a null origin": {**good, "Origin": "null"},
            "the origin with a slash": {**good, "Origin": good["Origin"] + "/"}, "a referrer only": {"X-CSRF-Token": good["X-CSRF-Token"], "Referer": good["Origin"] + "/"},
        }
        routes = [(method, path) for (method, path), capability in ROUTE_CAPABILITIES.items() if method not in ("GET", "HEAD", "OPTIONS") and capability is not None]
        self.assertGreater(len(routes), 12)
        before = self.stored_text()
        unexpected = AssertionError("the operation ran")
        with (
            patch.object(decompose, "run_pipeline", side_effect=unexpected), patch.object(decompose, "stream_pipeline_events", side_effect=unexpected),
            patch.object(nanopub, "get_nanopub_profile", side_effect=unexpected),
        ):
            for method, path in routes:
                for name, headers in bad.items():
                    with self.subTest(route=f"{method} {path}", case=name):
                        response = browser.request(method, fill(path), json={}, headers=headers)
                        self.assertEqual((response.status_code, response.json()), (403, CSRF_FAILED))
                with self.subTest(route=f"{method} {path}", case="no session"):
                    response = self.browser().request(method, fill(path), json={}, headers=good)
                    self.assertEqual((response.status_code, response.json()), (401, {"detail": "Authentication required."}))
        self.assertEqual(self.stored_text(), before)
        for path in ("/api/auth/me", "/api/auth/me/orcids", "/api/admin/users", "/api/auth/verify"):
            self.assertIn(browser.get(path).status_code, (200, 204), path)
        self.assertEqual(browser.post("/api/events", json={"action": "visualize"}, headers=good).status_code, 200)
        self.assertEqual(browser.post("/api/auth/me/orcids", json={"orcid": "0000-0002-1825-0097"}, headers=good).status_code, 200)
        self.assertEqual(browser.delete("/api/auth/me/orcids/0000-0002-1825-0097", headers=good).status_code, 200)

    def test_only_me_returns_the_token_and_a_password_session_needs_none(self):
        browser = self.signed_in()
        token = self.me(browser)["csrf_token"]
        self.assertGreaterEqual(len(token), 43)
        for path in ("/api/auth/me/orcids", "/api/auth/verify", "/api/openapi.json", "/api/model-options"):
            self.assertNotIn(token, browser.get(path).text)
        self.assertNotIn(token, " ".join(browser.cookies.values()))
        response = browser.get("/api/auth/me")
        self.assertEqual((response.headers["cache-control"], response.headers["x-iadopt-sign-in"]), ("no-store", "password, rohub"))
        self.assertEqual(set(response.json()), {"user", "auth_enabled", "capabilities", "sign_in", "csrf_token"})
        self.signed_in(browser)
        self.assertNotEqual(self.me(browser)["csrf_token"], token)

        password = self.password_browser()
        me = self.me(password)
        self.assertEqual((me["sign_in"], me["csrf_token"]), ("password", None))
        self.assertEqual(password.post("/api/events", json={"action": "visualize"}).status_code, 200)

        anonymous = self.browser()
        for method, path in (("GET", "/api/auth/me"), ("GET", "/api/auth/verify"), ("POST", "/api/decompose"), ("GET", "/api/admin/stats")):
            response = anonymous.request(method, path)
            self.assertEqual((response.status_code, response.json(), response.headers["x-iadopt-sign-in"]),
                             (401, {"detail": "Authentication required."}, "password, rohub"), path)
        self.assertEqual(anonymous.get("/api/auth/me").headers["cache-control"], "no-store")

    def test_a_server_with_a_root_path_asks_for_the_same(self):
        browser, rooted = self.signed_in(), self.browser(root_path="/x")
        anonymous = self.browser(root_path="/x")
        rooted.cookies.set(ROHUB_COOKIE, browser.cookies[ROHUB_COOKIE])
        good = self.csrf_headers(browser)
        for path in ("/x/api/events", "/api/events"):
            with self.subTest(path=path):
                response = rooted.post(path, json={"action": "visualize"})
                self.assertEqual((response.status_code, response.json()), (403, CSRF_FAILED))
                response = anonymous.post(path, json={"action": "visualize"}, headers=good)
                self.assertEqual((response.status_code, response.headers["x-iadopt-sign-in"]), (401, "password, rohub"))
                self.assertEqual(rooted.post(path, json={"action": "visualize"}, headers=good).status_code, 200)
        self.assertEqual(rooted.get("/x/api/auth/me").headers["cache-control"], "no-store")
        self.settings(auth_mode_raw="oidc")
        self.assertEqual(anonymous.post("/x/api/auth/login", json={"username": "admin", "password": "admin-password"}).status_code, 403)

    def test_the_public_posts_need_no_token_and_other_methods_on_public_paths_do(self):
        browser = self.signed_in()
        self.assertEqual(browser.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code, 401)
        self.assertEqual(browser.post("/api/auth/backchannel-logout").status_code, 400)
        self.assertEqual(browser.post("/api/livez").status_code, 403)
        self.assertEqual(browser.post("/api/model-options").status_code, 403)


class SignOutTests(OidcTestCase):
    def test_sign_out_ends_the_session_and_asks_the_provider_nothing(self):
        browser = self.signed_in()
        sent = len(self.provider.received)
        response = browser.post("/api/auth/logout", headers=self.csrf_headers(browser))
        self.assertEqual((response.status_code, response.json()), (200, {"status": "ok", "end_session_url": "/api/auth/end-session"}))
        removed = self.set_cookies(response)
        self.assertEqual(list(removed), [ROHUB_COOKIE])
        self.assertEqual({key: removed[ROHUB_COOKIE][key] for key in ("max-age", "path", "samesite", "httponly")},
                         {"max-age": "0", "path": "/", "samesite": "lax", "httponly": ""})
        self.assertEqual((self.rohub_sessions(), len(self.provider.received)), ([], sent))
        self.assertEqual(len(self.audit("auth.logout")), 1)

        password = self.password_browser()
        response = password.post("/api/auth/logout")
        self.assertEqual((response.json(), list(self.set_cookies(response))), ({"status": "ok", "end_session_url": None}, [PASSWORD_COOKIE]))

        both, password = self.signed_in(), self.password_browser()
        both.cookies.set(PASSWORD_COOKIE, password.cookies[PASSWORD_COOKIE])
        response = both.post("/api/auth/logout", headers=self.csrf_headers(both))
        self.assertEqual(response.json()["end_session_url"], "/api/auth/end-session")
        self.assertEqual(set(self.set_cookies(response)), {ROHUB_COOKIE, PASSWORD_COOKIE})
        self.assertEqual((self.rohub_sessions(), self.rows("SELECT * FROM sessions")), ([], []))

    def test_the_providers_sign_out_page(self):
        for registered, expected in (("", {"client_id"}), ("false", {"client_id"}), ("true", {"client_id", "post_logout_redirect_uri"})):
            with self.subTest(registered=registered), patch.object(core_deps_settings(), "oidc_post_logout_raw", registered):
                response = self.browser().get("/api/auth/end-session")
                self.assertEqual((response.status_code, response.headers["cache-control"], response.headers["referrer-policy"]), (303, "no-store", "no-referrer"))
                location = response.headers["location"]
                self.assertTrue(location.startswith(self.provider.url("logout") + "?"))
                query = query_of(location)
                self.assertEqual((set(query), query["client_id"]), (expected, [self.provider.client_id]))
                if len(expected) == 2:
                    self.assertEqual(query["post_logout_redirect_uri"], [f"{PUBLIC_URL}/"])
        for name, change in {"no endpoint": REMOVED, "an address that is not allowed": "http://login.example.org/logout", "no text": 7}.items():
            with self.subTest(name):
                provider_module.provider.reset()
                self.provider.metadata_changes = {"end_session_endpoint": change}
                self.assert_refused(self.browser().get("/api/auth/end-session"), "sign_out_incomplete")
        provider_module.provider.reset()
        self.provider.answer("metadata", 503)
        self.assert_refused(self.browser().get("/api/auth/end-session"), "sign_out_incomplete")
        self.provider.overrides.clear()
        provider_module.provider.reset()
        self.settings(sign_in_limit_raw="1/300")
        from app import guards

        self.replace(guards, "limiter", guards.RateLimiter())
        self.browser().get("/api/auth/end-session")
        self.assert_refused(self.browser().get("/api/auth/end-session"), "sign_out_incomplete")


class BackchannelTests(OidcTestCase):
    def send(self, token: Any) -> Any:
        return self.browser().post("/api/auth/backchannel-logout", data={"logout_token": token} if token is not None else {})

    def sessions(self) -> List[tuple]:
        return [(row["subject"], row["sid"]) for row in self.rohub_sessions()]

    def arrange(self) -> None:
        self.execute("DELETE FROM oidc_sessions")
        for sub, sid in (("subject-ada", "s1"), ("subject-ada", "s2"), ("subject-ben", "s3")):
            self.signed_in(sub=sub, sid=sid, email=f"{sub}@example.org", preferred_username=sub)

    def test_a_valid_token_ends_the_sessions_it_names(self):
        for name, changes, left in (
            ("sid and sub", {"sub": "subject-ada", "sid": "s1"}, [("subject-ada", "s2"), ("subject-ben", "s3")]),
            ("sid only", {"sub": REMOVED, "sid": "s2"}, [("subject-ada", "s1"), ("subject-ben", "s3")]),
            ("sub only", {"sub": "subject-ada", "sid": REMOVED}, [("subject-ben", "s3")]),
            ("the sid of someone else", {"sub": "subject-ben", "sid": "s1"}, [("subject-ada", "s1"), ("subject-ada", "s2"), ("subject-ben", "s3")]),
            ("none to end", {"sub": "subject-eve", "sid": "s9"}, [("subject-ada", "s1"), ("subject-ada", "s2"), ("subject-ben", "s3")]),
            ("without exp", {"sub": "subject-ada", "sid": "s1", "exp": REMOVED}, [("subject-ada", "s2"), ("subject-ben", "s3")]),
        ):
            with self.subTest(name):
                self.arrange()
                events = len(self.audit("auth.backchannel_logout"))
                response = self.send(self.provider.sign(self.provider.logout_claims(**changes)))
                self.assertEqual((response.status_code, response.headers["cache-control"]), (200, "no-store"))
                self.assertEqual(self.sessions(), left)
                new = self.audit("auth.backchannel_logout")[events:]
                ended = 3 - len(left)
                self.assertEqual([json.loads(event["metadata"])["sessions_ended"] for event in new], [ended] if ended else [])
                self.assertTrue(all(event["user_id"] is None for event in new))

    def test_the_sessions_of_another_issuer_stay(self):
        self.arrange()
        self.execute("UPDATE oidc_sessions SET issuer = ?", ISSUER)
        self.assertEqual(self.send(self.provider.sign(self.provider.logout_claims(sub="subject-ada", sid="s1"))).status_code, 200)
        self.assertEqual(len(self.sessions()), 3)

    def test_a_token_that_fails_ends_nothing(self):
        self.arrange()
        now, mine, signed = int(time.time()), RSAKey.generate_key(2048, {"kid": "key-1"}), self.provider.sign
        claims = self.provider.logout_claims
        cases: Dict[str, Any] = {
            "another key": signed(claims(), key=mine), "a changed token": signed(claims())[:-6] + "AAAAAA", "alg none": signed(claims(), alg="none"),
            "a shared key": shared_key_jws({"alg": "HS256", "kid": "key-1"}, json.dumps(claims()).encode(), self.provider.key.as_pem(private=False)),
            "an algorithm that is not accepted": signed(claims(), alg="RS512"), "a key in the header": signed(claims(), key=mine, header={"jwk": mine.as_dict(private=False)}),
            "a key address in the header": signed(claims(), header={"jku": "https://evil.example"}), "a critical header": signed(claims(), header={"crit": ["x"], "x": 1}),
            "encrypted": encrypt_compact({"alg": "RSA-OAEP", "enc": "A128GCM"}, json.dumps(claims()), mine),
            "another issuer": signed(claims(iss="https://login.example.org")), "no issuer": signed(claims(iss=REMOVED)),
            "another audience": signed(claims(aud="another")), "no audience": signed(claims(aud=REMOVED)),
            "no iat": signed(claims(iat=REMOVED)), "iat as text": signed(claims(iat=str(now))), "too old": signed(claims(iat=now - 700, exp=REMOVED)),
            "from the future": signed(claims(iat=now + 120)), "expired": signed(claims(exp=now - 90)), "exp as text": signed(claims(exp="soon")),
            "no jti": signed(claims(jti=REMOVED)), "jti empty": signed(claims(jti="")), "jti no text": signed(claims(jti=7)),
            "no events": signed(claims(events=REMOVED)), "events as a list": signed(claims(events=[LOGOUT_EVENT])), "another event": signed(claims(events={"x": {}})),
            "the event is no object": signed(claims(events={LOGOUT_EVENT: True})), "a nonce": signed(claims(nonce="x")), "a null nonce": signed(claims(nonce=None)),
            "an ID token": signed(self.provider.claims("nonce")), "neither sub nor sid": signed(claims(sub=REMOVED, sid=REMOVED)),
            "sid no text": signed(claims(sid=7)), "sub no text": signed(claims(sub=None, sid=REMOVED)), "no token": "x", "empty": "",
        }
        for name, token in cases.items():
            with self.subTest(name):
                response = self.send(token)
                self.assertEqual((response.status_code, response.json(), response.headers["cache-control"]), (400, {"error": "invalid_request"}, "no-store"))
        for name, kwargs in {"no field": {}, "two fields": {"content": b"logout_token=a&logout_token=b"}, "no form": {"content": b"\xff\xfe"},
                             "JSON": {"json": {"logout_token": signed(claims())}}}.items():
            with self.subTest(name):
                response = self.browser().post("/api/auth/backchannel-logout", **kwargs)
                self.assertEqual((response.status_code, response.json()), (400, {"error": "invalid_request"}))
        self.assertEqual(len(self.sessions()), 3)

        token = signed(claims(sub="subject-ada", sid="s1"))
        self.assertEqual(self.send(token).status_code, 200)
        self.signed_in(sub="subject-ada", sid="s1", email="a@example.org")
        self.assertEqual(self.send(token).status_code, 400, "a used token")
        self.assertIn(("subject-ada", "s1"), self.sessions())
        self.assertEqual(self.audit("auth.backchannel_logout")[1:], [])

    def test_the_times_of_a_logout_token_to_the_second(self):
        now = 1_900_000_000

        def rule_of(**changes: Any) -> Any:
            claims = self.provider.logout_claims(**{"iat": now, "exp": now + 120, **changes})
            try:
                oidc_tokens.logout_token_claims(
                    self.provider.sign(claims), algorithms=("RS256",), find_key=lambda *_: self.provider.key, issuer=self.provider.issuer,
                    client_id=self.provider.client_id, now=now,
                )
            except oidc_tokens.TokenInvalid as e:
                return e.rule
            return None

        for claim, inside, outside in (("iat", now - 660, now - 661), ("iat", now + 60, now + 61), ("exp", now - 59, now - 60)):
            with self.subTest(claim=claim, seconds=inside - now):
                self.assertEqual((rule_of(**{claim: inside}), rule_of(**{claim: outside})), (None, claim))

    def test_a_used_token_is_remembered_for_as_long_as_it_would_pass(self):
        used = flow._UsedTokens()
        self.assertTrue(used.use("first", 660.0, 0.0))
        for now in (0.0, 659.0, 660.0):
            self.assertFalse(used.use("first", 660.0, now), now)
        self.assertTrue(used.use("second", 700.0, 660.5))
        self.assertEqual(set(used._until), {"second"}, "what would pass no more is forgotten")

        claims = self.provider.logout_claims(exp=REMOVED)
        self.assertEqual(self.send(self.provider.sign(claims)).status_code, 200)
        self.assertEqual(flow.used_tokens._until, {claims["jti"]: claims["iat"] + 660})

    def test_without_keys_nothing_changes(self):
        self.arrange()
        for name in ("certs", "metadata"):
            with self.subTest(name):
                provider_module.provider.reset()
                self.provider.overrides.clear()
                self.provider.answer(name, 500)
                response = self.send(self.provider.sign(self.provider.logout_claims(sub="subject-ada", sid="s1")))
                self.assertEqual((response.status_code, response.json(), response.headers["cache-control"]), (400, {"error": "logout_failed"}, "no-store"))
        self.assertEqual(len(self.sessions()), 3)

    def test_a_used_token_is_forgotten_when_it_could_not_be_acted_on(self):
        self.arrange()
        token = self.provider.sign(self.provider.logout_claims(sub="subject-ada", sid="s1"))
        with patch.object(oidc_store, "end_sessions", side_effect=storage.StorageBusy("busy")):
            with self.assertRaises(storage.StorageBusy):
                self.send(token)
        self.assertEqual(self.send(token).status_code, 200)
        self.assertEqual(len(self.sessions()), 2)


# ------------------------------------------------------------------------------------------------
# Section 10: what a ROHub account may do
# ------------------------------------------------------------------------------------------------


class AccountTests(OidcTestCase):
    def test_a_rohub_account_has_the_interim_capabilities_and_roles_come_from_here_only(self):
        roles = {"roles": ["admin", "owner"], "realm_access": {"roles": ["admin"]}, "resource_access": {"x": {"roles": ["admin"]}},
                 "groups": ["/admins"], "role": "owner", "is_active": True}
        ada, ben = self.signed_in(**roles), self.signed_in(sub="subject-ben", email="ben@example.org", preferred_username="ben", **roles)
        for browser in (ada, ben):
            me = self.me(browser)
            self.assertEqual(me["user"]["role"], "user")
            allowed = {name for name, entry in me["capabilities"].items() if entry["allowed"]}
            self.assertEqual(allowed, {"public_tools", "session", "account_data"})
            self.assertEqual(browser.get("/api/auth/verify/admin").status_code, 403)
            self.assertEqual(browser.get("/api/auth/verify").status_code, 204)
        response = ada.post("/api/decompose", json={"definition": "air temperature"}, headers=self.csrf_headers(ada))
        self.assertEqual((response.status_code, response.json()["reason"]), (403, "approval_required"))

        code, out = run_cli("owner", "bind", "--issuer", self.provider.issuer, "--subject", "subject-ada")
        self.assertEqual(code, 0, out)
        ada = self.signed_in(**roles)  # owner bind ended the session (owner-sign-in, section 4)
        self.assertEqual((self.me(ada)["user"]["role"], self.me(ben)["user"]["role"]), ("owner", "user"))
        self.assertEqual(ada.get("/api/auth/verify/admin").status_code, 204)
        self.assertEqual(self.browser().get("/api/auth/verify/admin").status_code, 401)
        self.assertEqual(self.password_browser().get("/api/auth/verify/admin").status_code, 204)
