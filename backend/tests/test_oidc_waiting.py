"""oidc-identity: a provider that hangs holds up nothing else (increment 2).

Runs the application on a real server on a loopback port, since a test client may give every request a
thread limit of its own.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import List, Tuple

import requests
import uvicorn
from increment2_support import PASSWORD_COOKIE, OidcTestCase

from app import main
from app.core.logging import SignInQueryFilter
from app.oidc import provider as provider_module


class WaitingTests(OidcTestCase):
    def serve(self) -> str:
        config = uvicorn.Config(main.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(0.02)
        self.addCleanup(thread.join, 10)
        self.addCleanup(setattr, server, "should_exit", True)
        return f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"

    def test_while_the_provider_hangs_the_other_routes_answer(self):
        self.replace(provider_module, "REQUEST_SECONDS", 4.0)
        self.provider.answer("metadata", body=self.provider.metadata(), wait=6.0)
        cookie = {PASSWORD_COOKIE: self.password_browser().cookies[PASSWORD_COOKIE]}
        base = self.serve()
        results: List[Tuple[float, str]] = []

        def start() -> None:
            began = time.monotonic()
            answer = requests.get(f"{base}/api/auth/login", allow_redirects=False, timeout=30)
            results.append((time.monotonic() - began, answer.headers.get("location", "")))

        threads = [threading.Thread(target=start) for _ in range(60)]
        for thread in threads:
            thread.start()
        time.sleep(1.0)
        at_once = list(results)
        for path in ("/api/livez", "/api/model-options", "/api/nanopub/preparation-options", "/api/auth/me", "/api/auth/me/orcids", "/api/admin/stats"):
            began = time.monotonic()
            answer = requests.get(f"{base}{path}", cookies=cookie, timeout=30)
            self.assertEqual(answer.status_code, 200, path)
            self.assertLess(time.monotonic() - began, 1.0, path)
        for thread in threads:
            thread.join()
        self.assertEqual(len(at_once), 44, "the starts beyond 16 end at once")
        self.assertEqual({location for _, location in results}, {"/login.html?error=provider_unavailable&redirect=%2F"})
        self.assertEqual(sorted(seconds > 3.0 for seconds, _ in results).count(True), 16)
        self.assertLess(max(seconds for seconds, _ in results), 7.0)
        self.assertEqual((self.provider.count("metadata"), self.transactions()), (1, []))
        provider_module.provider.reset()
        self.provider.overrides.clear()
        answer = requests.get(f"{base}/api/auth/login", allow_redirects=False, timeout=30)
        self.assertTrue(answer.headers["location"].startswith(self.provider.issuer))

    def test_access_log_lines_of_the_trip_hold_no_query(self):
        lines: List[str] = []

        class Keep(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                lines.append(record.getMessage())

        access = logging.getLogger("uvicorn.access")
        handler, level = Keep(), access.level
        access.addHandler(handler)
        self.addCleanup(access.removeHandler, handler)
        self.addCleanup(access.setLevel, level)
        self.assertTrue(any(isinstance(item, SignInQueryFilter) for item in access.filters))
        config = uvicorn.Config(main.app, host="127.0.0.1", port=0, log_level="info", lifespan="off", log_config=None)
        server = uvicorn.Server(config)
        access.setLevel(logging.INFO)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(0.02)
        base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
        try:
            for path in ("/api/auth/login?redirect=/secret-1", "/api/auth/callback?code=secret-2&state=secret-3", "/api/auth/callback/?code=secret-4",
                         "/api/auth/end-session?x=secret-5", "/api/admin/audit?limit=5"):
                requests.get(f"{base}{path}", allow_redirects=False, timeout=30)
        finally:
            server.should_exit = True
            thread.join(10)
        text = "\n".join(lines)
        self.assertNotIn("secret", text)
        self.assertEqual(sum("/api/auth/" in line for line in lines), 4, text)
        self.assertIn("/api/admin/audit?limit=5", text)
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/x/api/auth/callback?code=secret-6", "1.1", 303)
        self.assertIn('"GET /x/api/auth/callback HTTP/1.1"', lines[-1], "also below a root path")
