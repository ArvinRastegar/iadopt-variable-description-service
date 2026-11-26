"""request-guards: the client's address, rate limits and size bounds (increment 2).

Behavioural tests of ``components/request-guards.md`` (approved under G-020). Nothing reaches a model,
a registry or an identity provider.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from fake_provider import REMOVED  # noqa: F401  (the tests' own module must be importable)
from increment1_support import TempDatabaseTestCase, run_cli, started_app
from increment2_support import Clock, OidcTestCase
from starlette.requests import Request
from test_records import CLIENT, DRAFT

from app import guards, main
from app.core import config as core_config
from app.guards import Limit, RateLimiter, account_key, address_key, client_ip
from app.oidc.settings import SettingsError

MiB = 1024 * 1024
TOO_LARGE = {"detail": "The request is too large.", "reason": "request_too_large"}


def request(forwarded: Any = None, peer: Optional[str] = "10.0.0.9") -> Request:
    """A request with ``X-Forwarded-For`` lines from a socket peer."""
    lines = [forwarded] if isinstance(forwarded, str) else (forwarded or [])
    headers = [(b"x-forwarded-for", line.encode("latin-1")) for line in lines]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1) if peer is not None else None, "query_string": b""})


def hops(number: str) -> Any:
    return patch.object(core_config.settings, "trusted_proxy_hops_raw", number)


class AddressTests(TempDatabaseTestCase):
    def test_the_entry_n_places_before_the_peer(self):
        """Covers: AC-request-guards-001."""
        cases: List[Tuple[str, Any, str]] = [
            ("0", None, "10.0.0.9"), ("0", "203.0.113.7, 10.0.0.8", "10.0.0.9"),
            ("1", "203.0.113.7", "203.0.113.7"), ("1", "198.51.100.1, 203.0.113.7", "203.0.113.7"), ("1", None, "10.0.0.9"),
            ("2", "203.0.113.7, 10.0.0.8", "203.0.113.7"), ("2", "198.51.100.1, 203.0.113.7, 10.0.0.8", "203.0.113.7"),
            ("2", ["198.51.100.1, 203.0.113.7", "10.0.0.8"], "203.0.113.7"), ("2", ["198.51.100.1", "203.0.113.7", "10.0.0.8"], "203.0.113.7"),
            ("2", "203.0.113.7", "203.0.113.7"), ("10", "203.0.113.7, 10.0.0.8", "203.0.113.7"), ("1", "203.0.113.7,", "203.0.113.7"),
        ]
        for number, forwarded, expected in cases:
            with self.subTest(hops=number, forwarded=forwarded), hops(number):
                self.assertEqual(client_ip(request(forwarded)), expected)
        with hops("2"):
            for lie in ("1.1.1.1, ", "1.1.1.1, 2.2.2.2, 3.3.3.3, ", "garbage, ", ", , "):
                self.assertEqual(client_ip(request(f"{lie}203.0.113.7, 10.0.0.8")), "203.0.113.7", lie)
            self.assertEqual(client_ip(request("203.0.113.7, 10.0.0.8", peer="unix-socket")), "203.0.113.7")
        with hops("0"):
            self.assertEqual(client_ip(request(peer="unix-socket")), "unix-socket")
            self.assertEqual(client_ip(request(peer=None)), "")

    def test_without_the_number_it_is_as_before(self):
        """Covers: AC-request-guards-002."""
        for forwarded, peer, expected in (
            ("203.0.113.7, 10.0.0.8", "10.0.0.9", "203.0.113.7"), (" spaced , x", "10.0.0.9", "spaced"), ("[2001:DB8::1]:443", "10.0.0.9", "[2001:DB8::1]:443"),
            (None, "10.0.0.9", "10.0.0.9"), ("", "10.0.0.9", "10.0.0.9"), (None, None, ""), (["first", "second"], "10.0.0.9", "first"), (", x", "10.0.0.9", ""),
        ):
            with self.subTest(forwarded=forwarded, peer=peer):
                self.assertEqual(client_ip(request(forwarded, peer)), expected)
        self.assertEqual(client_ip(None), "")

    def test_addresses_are_normal_and_others_give_the_peer(self):
        """Covers: AC-request-guards-003."""
        for entry, expected in (
            ("203.0.113.7:8443", "203.0.113.7"), ("[2001:DB8:0:0::1]:443", "2001:db8::1"), ("[2001:db8::1]", "2001:db8::1"), ("2001:0DB8::0001", "2001:db8::1"),
            ("::ffff:203.0.113.7", "203.0.113.7"), ("[::FFFF:203.0.113.7]:80", "203.0.113.7"), ("unknown", "10.0.0.9"), ("_hidden", "10.0.0.9"),
            ("203.0.113.7:port", "10.0.0.9"), ("203.0.113.256", "10.0.0.9"), ("010.0.0.1", "10.0.0.9"), ("[203.0.113.7]", "10.0.0.9"), ("fe80::1%eth0", "10.0.0.9"),
            ("[2001:db8::1", "10.0.0.9"), ("<script>", "10.0.0.9"),
        ):
            with self.subTest(entry=entry), hops("1"):
                self.assertEqual(client_ip(request(entry)), expected)
        with hops("0"):
            self.assertEqual(client_ip(request(peer="::ffff:10.0.0.9")), "10.0.0.9")


class StoredAddressTests(OidcTestCase):
    def test_sessions_audit_rows_and_record_contexts_store_it(self):
        """Covers: AC-request-guards-004."""
        forwarded = "198.51.100.1, 203.0.113.7, 10.0.0.8"
        for number, expected in (("", "198.51.100.1"), ("2", "203.0.113.7")):
            with self.subTest(hops=number), hops(number):
                for table in ("sessions", "oidc_sessions", "audit_events"):
                    self.execute(f"DELETE FROM {table}")
                browser = self.browser(headers={"X-Forwarded-For": forwarded})
                self.login(browser, "admin", "admin-password")
                self.assertEqual(browser.post("/api/records", json={"turtle": DRAFT, "client": CLIENT}).status_code, 201)
                self.signed_in(self.browser(headers={"X-Forwarded-For": forwarded}))
                for table in ("sessions", "oidc_sessions", "audit_events"):
                    self.assertEqual({row["ip_address"] for row in self.rows(f"SELECT ip_address FROM {table}")}, {expected}, table)
                self.assertEqual(self.rows("SELECT ip_address FROM client_contexts ORDER BY id DESC LIMIT 1"), [{"ip_address": expected}])


class RoutePathTests(TempDatabaseTestCase):
    def test_it_is_the_path_the_routes_are_matched_by(self):
        """Covers: AC-request-guards-010 (for a server with a root path)."""
        from starlette._utils import get_route_path

        for path in ("/api/events", "/x/api/events", "/xy/api/events", "/x/x/api", "//x/api", "/x//api", "/api"):
            for root in ("", "/x", "/x/", "/x/x", "/"):
                with self.subTest(path=path, root=root):
                    scope = {"type": "http", "path": path, "root_path": root}
                    self.assertEqual(guards.route_path(scope), get_route_path(scope))


class LimiterTests(TempDatabaseTestCase):
    def limiter(self, burst: int = 3, seconds: int = 9) -> Tuple[RateLimiter, Clock]:
        clock = Clock()
        return RateLimiter(lambda scope: Limit(burst, seconds), clock=clock), clock

    def test_a_bucket_empties_refuses_and_refills(self):
        """Covers: AC-request-guards-005."""
        limiter, clock = self.limiter()
        key = address_key("203.0.113.7")
        self.assertEqual([limiter.check("a", key) for _ in range(4)], [None, None, None, 3])
        clock.advance(1)
        self.assertEqual(limiter.check("a", key), 2)
        clock.advance(2)
        self.assertEqual([limiter.check("a", key), limiter.check("a", key)], [None, 3])
        clock.advance(1000)
        self.assertEqual([limiter.check("a", key) for _ in range(4)], [None, None, None, 3])
        for burst, seconds in ((1, 1), (7, 300), (120, 300), (600, 300), (100_000, 1), (3, 86_400)):
            with self.subTest(burst=burst, seconds=seconds):
                limiter, clock = self.limiter(burst, seconds)
                for _ in range(burst):
                    self.assertIsNone(limiter.check("a", key))
                for _ in range(3):
                    wait = limiter.check("a", key)
                    self.assertGreaterEqual(wait, 1)
                    clock.advance(wait)
                    self.assertIsNone(limiter.check("a", key))
                    while limiter.check("a", key) is None:
                        pass

    def test_keys_and_scopes_do_not_share_a_bucket(self):
        """Covers: AC-request-guards-006."""
        limiter, _ = self.limiter(1, 60)
        self.assertIsNone(limiter.check("a", address_key("203.0.113.7")))
        self.assertIsNotNone(limiter.check("a", address_key("203.0.113.7")))
        for scope, key in (("a", address_key("203.0.113.8")), ("b", address_key("203.0.113.7")), ("a", account_key(7)), ("a", address_key("7"))):
            self.assertIsNone(limiter.check(scope, key), (scope, key))
        self.assertEqual(address_key("2001:db8:1:2::1"), address_key("2001:db8:1:2:ffff::9"))
        self.assertNotEqual(address_key("2001:db8:1:2::1"), address_key("2001:db8:1:3::1"))
        self.assertEqual(address_key("::ffff:203.0.113.7"), address_key("203.0.113.7"))
        self.assertEqual(len({address_key(text) for text in ("", "unknown", "testclient", "x" * 5000)}), 1)

    def test_over_http(self):
        """Covers: AC-request-guards-007."""
        ran: List[int] = []
        probe = FastAPI()
        probe.add_api_route("/probe", lambda: ran.append(1) or {"ok": True}, dependencies=[Depends(guards.limit(guards.SIGN_IN))])
        with patch.object(guards, "limiter", RateLimiter(lambda scope: Limit(2, 60), clock=Clock())):
            client = TestClient(probe)
            answers = [client.get("/probe") for _ in range(3)]
        self.assertEqual([answer.status_code for answer in answers], [200, 200, 429])
        self.assertEqual(answers[2].headers["retry-after"], "30")
        self.assertEqual(answers[2].json(), {"detail": "Too many requests. Try again in 30 seconds.", "reason": "rate_limited"})
        self.assertEqual(len(ran), 2)

    def test_threads_at_once_and_the_bound_of_buckets(self):
        """Covers: AC-request-guards-008."""
        limiter, _ = self.limiter(50, 3600)
        passed: List[Optional[int]] = []
        barrier = threading.Barrier(16)

        def go() -> None:
            barrier.wait()
            for _ in range(10):
                passed.append(limiter.check("a", address_key("203.0.113.7")))

        threads = [threading.Thread(target=go) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(passed.count(None), 50)

        limiter, _ = self.limiter(1, 3600)
        with patch.object(guards, "MAX_BUCKETS", 3):
            for number in (1, 2, 3):
                limiter.check("a", account_key(number))
            self.assertIsNotNone(limiter.check("a", account_key(1)))  # used again, so it is not the oldest
            limiter.check("a", account_key(4))
            self.assertIsNone(limiter.check("a", account_key(2)), "the least recently used was dropped")
            self.assertIsNotNone(limiter.check("a", account_key(1)))
        self.assertEqual(guards.MAX_BUCKETS, 10_000)


class BoundTests(OidcTestCase):
    """Bodies of every kind of length, before the session check."""

    def ask(self, path: str, size: int, *, declared: Any = True, cookie: str = "", method: str = "POST", root_path: str = "") -> Tuple[int, bytes]:
        """Send a body through the application's ASGI interface, with a true, a false or no length."""
        body = b"x" * size
        headers = [(b"host", b"localhost:5173"), (b"content-type", b"application/json"), (b"origin", b"http://localhost:5173")]
        if declared is True:
            headers.append((b"content-length", str(size).encode()))
        elif declared is not None:
            headers.append((b"content-length", str(declared).encode()))
        if cookie:
            headers.append((b"cookie", cookie.encode()))
        chunks: Iterator[bytes] = iter([body[start:start + 100_000] for start in range(0, size, 100_000)] or [b""])
        sent: List[Dict[str, Any]] = []
        remaining = -(-size // 100_000) or 1

        async def receive() -> Dict[str, Any]:
            nonlocal remaining
            remaining -= 1
            return {"type": "http.request", "body": next(chunks, b""), "more_body": remaining > 0}

        async def send(message: Dict[str, Any]) -> None:
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "root_path": root_path, "headers": headers, "client": ("10.0.0.9", 1), "server": ("localhost", 5173), "state": {}}
        asyncio.run(main.app(scope, receive, send))
        status = next(message["status"] for message in sent if message["type"] == "http.response.start")
        return status, b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")

    def test_a_body_above_the_bound_is_refused_before_anything_else(self):
        """Covers: AC-request-guards-009."""
        import json

        browser = self.password_browser()
        cookies = {"anonymous": "", "signed in": f"iadopt_session={browser.cookies['iadopt_session']}"}
        before = self.stored_text()
        for who, cookie in cookies.items():
            for declared in (True, 10, "x", None):
                with self.subTest(who=who, declared=declared):
                    status, body = self.ask("/api/events", MiB + 1, declared=declared, cookie=cookie)
                    self.assertEqual((status, json.loads(body)), (413, TOO_LARGE))
        self.assertEqual(self.stored_text(), before)
        for who, status in (("anonymous", 401), ("signed in", 422)):
            for declared in (True, None):
                with self.subTest(who=who, declared=declared, size="exactly the bound"):
                    self.assertEqual(self.ask("/api/events", MiB, declared=declared, cookie=cookies[who])[0], status)

    def test_the_bound_of_every_route(self):
        """Covers: AC-request-guards-010."""
        from app.access.dependencies import ROUTE_CAPABILITIES
        from test_access_policy import fill

        for method, path in ROUTE_CAPABILITIES:
            bound = 64 * 1024 if path == "/api/auth/backchannel-logout" else MiB
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(self.ask(fill(path), bound + 1, declared=True, method=method)[0], 413)
                self.assertEqual(self.ask(fill(path), bound + 1, declared=None, method=method)[0], 413)
                if method != "GET":
                    self.assertNotEqual(self.ask(fill(path), bound, declared=True, method=method)[0], 413)
        self.assertNotEqual(self.ask("/not-the-api", MiB + 1, declared=True)[0], 413)
        for path, bound in (("/x/api/events", MiB), ("/x/api/auth/backchannel-logout", 64 * 1024)):
            with self.subTest(path=path, root_path="/x"):
                self.assertEqual(self.ask(path, bound + 1, root_path="/x")[0], 413)
                self.assertNotEqual(self.ask(path, bound, root_path="/x")[0], 413)

    def test_the_refusal_is_not_kept(self):
        """Covers: AC-request-guards-009 (with oidc-identity section 9: every back-channel answer has no-store)."""
        for path in ("/api/events", "/api/auth/backchannel-logout"):
            response = self.browser().post(path, content=b"x" * (MiB + 1))
            self.assertEqual((response.status_code, response.headers["cache-control"]), (413, "no-store"), path)

    def test_a_body_that_passes_arrives_as_it_was_sent(self):
        """Covers: AC-request-guards-009 (a body of at most the bound passes)."""
        browser = self.password_browser()
        turtle = DRAFT + "#" * 700_000
        response = browser.post("/api/records", json={"turtle": turtle, "client": CLIENT})
        self.assertIn(response.status_code, (201, 400, 422), response.text)
        stream = browser.post("/api/events", content=iter([b'{"action": ', b'"visualize"', b"}"]), headers={"Content-Type": "application/json"})
        self.assertEqual(stream.status_code, 200)


class SettingsTests(TempDatabaseTestCase):
    def test_bad_values_stop_the_start_only(self):
        """Covers: AC-request-guards-011."""
        fresh = self.dir / "fresh.sqlite3"
        self.use_database(fresh)
        cases = [("trusted_proxy_hops_raw", "IADOPT_TRUSTED_PROXY_HOPS", value) for value in ("-1", "11", "1.5", "two", " 1", "0x1", "١")]
        cases += [("sign_in_limit_raw", "IADOPT_SIGN_IN_LIMIT", value) for value in ("10", "10/", "/10", "0/10", "10/0", "-1/10", "1.5/10", "100001/10", "10/86401", "10/10/10", "a/b", " 1/1")]
        for field, name, value in cases:
            with self.subTest(setting=name, value=value), patch.object(core_config.settings, field, value):
                with self.assertRaises(SettingsError) as caught:
                    with started_app():
                        self.fail("the application started")
                self.assertIn(name, str(caught.exception))
                self.assertFalse(fresh.exists())
                self.assertEqual(run_cli("db", "status")[0], 0)
        for field, value in (("trusted_proxy_hops_raw", "0"), ("trusted_proxy_hops_raw", "10"), ("sign_in_limit_raw", "1/1"), ("sign_in_limit_raw", "100000/86400")):
            with self.subTest(accepted=value), patch.object(core_config.settings, field, value):
                with started_app() as client:
                    self.assertEqual(client.get("/api/livez").status_code, 200)

    def test_the_defaults_and_the_limit_set(self):
        """Covers: AC-request-guards-011 (the limits)."""
        scopes = (guards.SIGN_IN, guards.SIGN_IN_RETURN, guards.BACKCHANNEL_LOGOUT)
        self.assertEqual([guards.limit_of(scope) for scope in scopes], [Limit(120, 300), Limit(120, 300), Limit(600, 300)])
        with patch.object(core_config.settings, "sign_in_limit_raw", "5/60"):
            self.assertEqual([guards.limit_of(scope) for scope in scopes], [Limit(5, 60), Limit(5, 60), Limit(600, 300)])

    def test_password_sign_ins_have_no_limit(self):
        """Covers: AC-request-guards-012."""
        self.store.init()
        client = self.client()
        with patch.object(self.store, "verify_password", return_value=False):
            for _ in range(200):
                self.assertEqual(client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code, 401)
