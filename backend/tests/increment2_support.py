"""Shared helpers of the increment-2 tests (request-guards, oidc-identity, keycloak-deployment).

Each test gets a fake provider with its own keys on a loopback port, the sign-in settings that point
at it, and a fresh limiter, provider cache and memory of used logout tokens. Nothing here reaches a
real provider.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

from fake_provider import FakeProvider
from fastapi.testclient import TestClient
from increment1_support import TempDatabaseTestCase

from app import guards, main
from app.core import config as core_config
from app.oidc import flow
from app.oidc import provider as provider_module

PUBLIC_URL = "http://localhost:5173"
AGENT = "Increment2Test/1.0"
ROHUB_COOKIE = "iadopt_rohub_session"
BROWSER_COOKIE = "iadopt_sign_in"
PASSWORD_COOKIE = "iadopt_session"


class OidcTestCase(TempDatabaseTestCase):
    """The mode ``both`` on a development machine, with a fake provider; subclasses change the settings."""

    mode = "both"
    environment = "development"
    public_url = PUBLIC_URL

    def setUp(self) -> None:
        super().setUp()
        self.provider = FakeProvider()
        self.provider.__enter__()
        self.addCleanup(self.provider.__exit__)
        self.settings(
            environment_raw=self.environment,
            auth_mode_raw=self.mode,
            oidc_issuer_raw=self.provider.issuer,
            oidc_client_id_raw=self.provider.client_id,
            public_url_raw=self.public_url,
            oidc_post_logout_raw="",
            trusted_proxy_hops_raw="",
            sign_in_limit_raw="",
        )
        provider_module.provider.reset()
        self.addCleanup(provider_module.provider.reset)
        for module, name, value in (
            (guards, "limiter", guards.RateLimiter()),
            (flow, "used_tokens", flow._UsedTokens()),
        ):
            self.replace(module, name, value)
        self.store.cookie_secure = self.public_url.startswith("https:")
        self.store.init()

    # ----------------------------------------------------------------- arrangement

    def replace(self, target: Any, name: str, value: Any) -> None:
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings(self, **raw: str) -> None:
        """Set settings for the rest of the test."""
        for name, value in raw.items():
            self.replace(core_config.settings, name, value)

    def clock(self) -> "Clock":
        """Replace the provider's clock with one the test moves."""
        clock = Clock()
        self.replace(provider_module, "clock", clock)
        return clock

    def browser(self, **kwargs: Any) -> TestClient:
        """A browser of its own, at the public URL, that follows no redirect by itself."""
        headers = {"User-Agent": AGENT, **kwargs.pop("headers", {})}
        return TestClient(main.app, base_url=self.public_url, follow_redirects=False, headers=headers, **kwargs)

    # ----------------------------------------------------------------- the trip

    def begin(self, browser: TestClient, redirect: Optional[str] = None) -> str:
        """Start a sign-in and return the address of the provider the browser is sent to."""
        response = browser.get("/api/auth/login", params={} if redirect is None else {"redirect": redirect})
        self.assertEqual(response.status_code, 303, response.text)
        location = response.headers["location"]
        self.assertTrue(location.startswith(self.provider.url("auth") + "?"), location)
        return location

    def come_back(self, browser: TestClient, query: Dict[str, str]) -> Any:
        return browser.get(f"/api/auth/callback?{urlencode(query)}")

    def sign_in(self, browser: Optional[TestClient] = None, *, redirect: Optional[str] = None,
                id_token: Optional[Callable[[Dict[str, Any]], str]] = None, **claims: Any) -> Tuple[TestClient, Any]:
        """Make the whole trip and return the browser and the answer to its return."""
        browser = browser or self.browser()
        location = self.begin(browser, redirect)
        return browser, self.come_back(browser, self.provider.authorize(location, id_token=id_token, **claims))

    def signed_in(self, browser: Optional[TestClient] = None, **claims: Any) -> TestClient:
        browser, response = self.sign_in(browser, **claims)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/"), response.headers)
        return browser

    def password_browser(self, username: str = "admin", password: str = "admin-password") -> TestClient:
        browser = self.browser()
        self.login(browser, username, password)
        return browser

    def me(self, browser: TestClient) -> Dict[str, Any]:
        response = browser.get("/api/auth/me")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def csrf_headers(self, browser: TestClient) -> Dict[str, str]:
        return {"X-CSRF-Token": self.me(browser)["csrf_token"], "Origin": self.public_url}

    # ----------------------------------------------------------------- observation

    def assert_refused(self, response: Any, code: str, redirect: Optional[str] = None) -> None:
        """The browser is sent to the sign-in page with the code, and with the page when it is known."""
        self.assertEqual(response.status_code, 303, response.text)
        parts = urlsplit(response.headers["location"])
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("", "", "/login.html"), response.headers["location"])
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        self.assertEqual(query.pop("error", None), code)
        if redirect is not None:
            self.assertEqual(query.pop("redirect", None), redirect)
        self.assertIn(set(query), (set(), {"redirect"}))
        self.assertEqual((response.headers["cache-control"], response.headers["referrer-policy"]), ("no-store", "no-referrer"))

    def audit(self, action: str) -> List[Dict[str, Any]]:
        return self.rows("SELECT * FROM audit_events WHERE action = ? ORDER BY id", action)

    def rohub_sessions(self) -> List[Dict[str, Any]]:
        return self.rows("SELECT * FROM oidc_sessions ORDER BY created_at")

    def transactions(self) -> List[Dict[str, Any]]:
        return self.rows("SELECT * FROM oidc_transactions ORDER BY created_at")

    def accounts(self) -> List[Dict[str, Any]]:
        return self.rows("SELECT * FROM users ORDER BY id")

    def set_cookies(self, response: Any) -> Dict[str, Dict[str, str]]:
        """The answer's cookies: name to value and lower-case attributes."""
        cookies: Dict[str, Dict[str, str]] = {}
        for line in response.headers.get_list("set-cookie"):
            first, *attributes = [part.strip() for part in line.split(";")]
            name, _, value = first.partition("=")
            cookies[name] = {"value": value}
            for attribute in attributes:
                key, _, text = attribute.partition("=")
                cookies[name][key.lower()] = text
        return cookies

    def stored_text(self) -> str:
        """Every value of every table, as text: what the database holds."""
        conn = sqlite3.connect(self.store.db_path)
        try:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            return "\n".join(str(row) for table in tables for row in conn.execute(f"SELECT * FROM {table}"))
        finally:
            conn.close()


class Clock:
    """A monotonic clock a test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def query_of(location: str) -> Dict[str, List[str]]:
    return parse_qs(urlsplit(location).query, keep_blank_values=True)
