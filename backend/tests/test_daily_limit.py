"""The daily limit on decompositions, switched off unless one setting is set (D-049).

The owner, 2026-11-25: "do not apply the decomposition limit for now implement it but switched off so maybe later we
switch it on", and for whom: "No, unapproved only". Switched off, an account that is not approved waits for an approval
(test_account_approval); switched on, it may decompose so many times a day. What counts is D-021's and the day is
Europe/Berlin's (D-033). People sign in through the fake provider. Models are faked: nothing here reaches a provider.
"""

from __future__ import annotations

import gc
import json
import os
import pathlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

import requests
import uvicorn
from fastapi.testclient import TestClient
from increment1_support import ISSUER, TempDatabaseTestCase, run_cli, started_app
from increment2_support import ROHUB_COOKIE, OidcTestCase
from test_records import CLIENT, PIPELINE_RESULT

from app import main, storage, usage
from app.access import admission
from app.access import state as access_state_module
from app.access.policy import AccessState, Capability, decide
from app.access.state import access_state
from app.accounts import AccountStore
from app.core import config as core_config
from app.oidc.settings import SettingsError
from app.routers import decompose as decompose_router
from app.services import llm
from app.services.call_log import CallLog
from app.services.llm import stream_event

ROOT = pathlib.Path(__file__).resolve().parents[2]
JSON_ROUTE, STREAM_ROUTE = ROUTES = ("/api/decompose", "/api/decompose/stream")
DEFINITION = {"definition": "air temperature", "client": CLIENT}
REFUSED = "No decompositions left today."


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


# ------------------------------------------------------------------------------------------------
# The switch and the day
# ------------------------------------------------------------------------------------------------


class SwitchTests(TempDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fresh = self.dir / "fresh.sqlite3"
        self.use_database(self.fresh)

    def refusal(self, value: str, **others: str) -> str:
        """Start with a bad value, which must stop before the database is opened; return the message."""
        with patch.multiple(core_config.settings, daily_decompositions_raw=value, **others):
            with self.assertRaises(SettingsError) as caught:
                with started_app():
                    self.fail("the application started")
            self.assertFalse(self.fresh.exists(), "the database was opened")
        return str(caught.exception)

    def test_anything_but_empty_or_a_whole_number_up_to_1000_stops_the_start_and_no_value_is_shown(self):
        for value in ("0", "1001", "1.5", " 3", "three"):
            with self.subTest(value=value):
                message = self.refusal(value)
                self.assertIn("IADOPT_DAILY_DECOMPOSITIONS must be empty", message)
                self.assertIn("from 1 to 1000", message)
        message = self.refusal("a-secret-by-mistake", trusted_proxy_hops_raw="another-secret")
        self.assertIn("IADOPT_TRUSTED_PROXY_HOPS", message)
        self.assertNotIn("secret", message)

    def test_empty_or_unset_is_off_and_whole_numbers_are_on(self):
        for value, limit in (("", None), ("1", 1), ("1000", 1000)):
            with self.subTest(value=value), patch.object(core_config.settings, "daily_decompositions_raw", value):
                self.assertEqual(usage.daily_limit(), limit)
                with started_app() as client:
                    self.assertEqual(client.get("/api/livez").status_code, 200)
        with patch.dict(os.environ):
            del os.environ["IADOPT_DAILY_DECOMPOSITIONS"]
            self.assertEqual(core_config.Settings().daily_decompositions_raw, "")

    def test_the_example_settings_keep_the_limit_off(self):
        """A number would let accounts nobody approved decompose."""
        self.assertIn("\nIADOPT_DAILY_DECOMPOSITIONS=\n", (ROOT / ".env.example").read_text())


def test_a_day_runs_from_midnight_to_midnight_in_berlin_also_when_the_clocks_change():
    cases = (
        ("2026-11-25T23:59:59.999999+01:00", "2026-11-25", "2026-11-26T00:00:00+01:00"),
        ("2026-03-29T23:59:59+02:00", "2026-03-29", "2026-03-30T00:00:00+02:00"),  # 23 hours
        ("2026-10-25T02:30:00+01:00", "2026-10-25", "2026-10-26T00:00:00+01:00"),  # 25 hours, 02:30 twice
    )
    for moment, date, end in cases:
        for seen_from in (timezone.utc, timezone(timedelta(hours=13))):
            day = usage.day_of(datetime.fromisoformat(moment).astimezone(seen_from))
            assert (day.date, day.end.isoformat()) == (date, end)
    for start, hours in (("2026-03-29T00:00:00+01:00", 23), ("2026-10-25T00:00:00+02:00", 25)):
        assert usage.day_of(utc(start)).end.astimezone(timezone.utc) - utc(start) == timedelta(hours=hours)


# ------------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------------


class LimitTestCase(OidcTestCase):
    """A development machine with the fake provider, and a server clock the test sets."""

    def setUp(self) -> None:
        super().setUp()
        self.moment = utc("2026-11-25T12:00:00+01:00")
        self.replace(usage, "now", lambda: self.moment)
        self.replace(admission, "_waiting", {})
        self.replace(admission, "_retried", set())
        self.replace(admission, "RETRY_SECONDS", 0.2)

    def switch(self, value: str) -> None:
        self.settings(daily_decompositions_raw=value)

    def at(self, text: str) -> None:
        self.moment = utc(text)

    def person(self, name: str = "ada") -> TestClient:
        """A browser in which somebody is signed in with ROHub."""
        return self.signed_in(sub=f"subject-{name}", email=f"{name}@example.org", preferred_username=name, sid=f"session-{name}")

    def account(self, name: str = "ada") -> Dict[str, Any]:
        return next(row for row in self.accounts() if row["username"] == name)

    def approve(self, name: str = "ada", *, change: str = "approve") -> None:
        response = self.password_browser().post(f"/api/admin/users/{self.account(name)['id']}/{change}", json={})
        self.assertEqual(response.status_code, 200, response.text)

    def own_account(self, name: str) -> Dict[str, Any]:
        """An account with an identity, made without a sign-in."""
        claims = {"email": f"{name}@example.org", "email_verified": True, "preferred_username": name}
        return AccountStore(self.store).ensure_account(ISSUER, f"subject-{name}", claims)[0]

    def post(self, browser: TestClient, route: str = JSON_ROUTE, body: Optional[Dict[str, Any]] = None) -> Any:
        body = DEFINITION if body is None else body
        return browser.post(route, json=body, headers=self.csrf_headers(browser) if browser.cookies.get(ROHUB_COOKIE) else {})

    def allowance(self, browser: TestClient) -> Tuple[Optional[int], Optional[str]]:
        entry = self.me(browser)["capabilities"]["decompose"]
        return entry["uses_left_today"], entry["resets_at"]

    def uses(self) -> List[Dict[str, Any]]:
        return self.rows("SELECT * FROM decomposition_uses ORDER BY id")

    def ends(self) -> List[Tuple[Optional[str], int]]:
        return [(row["outcome"], row["released"]) for row in self.uses()]

    def await_ends(self, expected: List[Tuple[Optional[str], int]]) -> None:
        deadline = time.monotonic() + 20
        while self.ends() != expected and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.ends(), expected)

    @contextmanager
    def models(self, run: Optional[Callable[[CallLog], Dict[str, Any]]] = None) -> Iterator[List[str]]:
        """Replace the pipeline of both routes; yields the routes that reached a model, in order."""
        ran: List[str] = []

        def answer(calls: CallLog) -> Dict[str, Any]:
            return run(calls) if run else dict(PIPELINE_RESULT)

        def whole(definition: str, calls: Optional[CallLog] = None, **_: Any) -> Dict[str, Any]:
            ran.append(JSON_ROUTE)
            return answer(calls or CallLog())

        def streamed(definition: str, calls: Optional[CallLog] = None, **_: Any) -> Iterator[str]:
            ran.append(STREAM_ROUTE)
            yield stream_event("raw_delta", delta="{")
            yield stream_event("final", data=answer(calls or CallLog()))

        with patch.object(decompose_router, "run_pipeline", side_effect=whole), \
                patch.object(decompose_router, "stream_pipeline_events", side_effect=streamed):
            yield ran

    def assert_done(self, response: Any) -> None:
        self.assertEqual(response.status_code, 200, response.text)
        last = json.loads(response.text.splitlines()[-1])
        self.assertTrue(last.get("type", "final") == "final", response.text)

    def assert_limited(self, response: Any, resets_at: str, seconds: int) -> None:
        self.assertEqual((response.status_code, response.json()),
                         (429, {"detail": REFUSED, "reason": "daily_limit", "resets_at": resets_at}))
        self.assertEqual(response.headers["retry-after"], str(seconds))


# ------------------------------------------------------------------------------------------------
# Off, as it runs today
# ------------------------------------------------------------------------------------------------


class OffTests(LimitTestCase):
    def test_off_counts_stores_and_refuses_nothing(self):
        def forbidden(*_: Any, **__: Any) -> Any:
            raise AssertionError("the daily limit did something while it is off")

        ada, ben = self.person("ada"), self.person("ben")
        self.approve("ada")
        self.store.create_user(username="bob", password="password-123")
        opened: List[Any] = []

        def recorded(*args: Any, **_: Any) -> Any:
            opened.append(args)
            return forbidden()

        no_database = SimpleNamespace(transaction=recorded)
        with patch.multiple(usage, allowance=forbidden, add_use=forbidden, finish_use=forbidden, release_running=forbidden, day_of=forbidden, now=forbidden), \
                patch.object(admission, "storage", no_database), patch.object(access_state_module, "storage", no_database), self.models() as ran:
            for browser in (ada, self.password_browser("bob", "password-123"), self.password_browser()):
                for route in ROUTES * 2:  # more than the three a day of D-033
                    self.assert_done(self.post(browser, route))
                decompose = self.me(browser)["capabilities"]["decompose"]
                self.assertEqual(decompose, {"allowed": True, "reason": None, "message": None, "uses_left_today": None, "resets_at": None})
            for route in ROUTES:  # an account that is not approved waits, without a number
                response = self.post(ben, route)
                self.assertEqual((response.status_code, response.json()["reason"], "retry-after" in response.headers), (403, "approval_required", False))
            with started_app() as client:
                self.assertEqual(client.get("/api/livez").status_code, 200)
        self.assertEqual((len(ran), self.uses(), opened, admission._waiting), (12, [], [], {}))


# ------------------------------------------------------------------------------------------------
# On
# ------------------------------------------------------------------------------------------------


class OnTests(LimitTestCase):
    def test_the_last_use_of_the_day_is_the_last_on_both_routes(self):
        self.switch("3")
        ada, midnight = self.person(), "2026-11-26T00:00:00+01:00"
        with self.models() as ran:
            for left, route in ((3, JSON_ROUTE), (2, STREAM_ROUTE), (1, JSON_ROUTE)):
                self.assertEqual(self.allowance(ada), (left, midnight))
                self.assert_done(self.post(ada, route))
            decompose = self.me(ada)["capabilities"]["decompose"]
            self.assertEqual(decompose, {"allowed": False, "reason": "daily_limit", "message": REFUSED, "uses_left_today": 0, "resets_at": midnight})
            stored = [self.rows(f"SELECT * FROM {table}") for table in ("record_items", "record_events", "audit_events")]
            for route in ROUTES:
                self.assert_limited(self.post(ada, route), midnight, 12 * 3600)
            self.assertEqual([self.rows(f"SELECT * FROM {table}") for table in ("record_items", "record_events", "audit_events")], stored)
        self.assertEqual(ran, [JSON_ROUTE, STREAM_ROUTE, JSON_ROUTE])
        self.assertEqual(self.ends(), [("success", 0)] * 3)

    def test_each_account_has_its_own_uses_and_unused_ones_do_not_carry_over(self):
        self.switch("1")
        ada, ben = self.person("ada"), self.person("ben")
        with self.models():
            self.assert_done(self.post(ada))
            self.assertEqual((self.allowance(ada)[0], self.allowance(ben)[0]), (0, 1))
            self.assert_done(self.post(ben, STREAM_ROUTE))
            self.assertEqual([self.post(browser).status_code for browser in (ada, ben)], [429, 429])
            self.assertEqual(self.allowance(self.person("ada"))[0], 0)  # a new session adds none
            self.at("2026-11-27T08:00:00+01:00")  # ben left none, ada none: the new day has one each again
            self.assertEqual((self.allowance(ada), self.allowance(ben)[0]), ((1, "2026-11-28T00:00:00+01:00"), 1))

    def test_requests_at_the_same_moment_never_pass_the_limit(self):
        """Both routes at once: the model is reached as often as uses are left, and every other request is refused."""
        self.switch("3")
        ada, barrier, lock = self.person(), threading.Barrier(10), threading.Lock()
        headers, results = self.csrf_headers(ada), []

        def slow(_: CallLog) -> Dict[str, Any]:
            time.sleep(0.3)
            return dict(PIPELINE_RESULT)

        def request(route: str) -> None:
            browser = self.browser()
            browser.cookies.set(ROHUB_COOKIE, ada.cookies[ROHUB_COOKIE])
            barrier.wait()
            response = browser.post(route, json=DEFINITION, headers=headers)
            with lock:
                results.append((response.status_code, response.json()["reason"] if response.status_code == 429 else None))

        with self.models(slow) as ran:
            threads = [threading.Thread(target=request, args=(route,)) for route in ROUTES * 5]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        self.assertEqual(sorted(results, key=str), [(200, None)] * 3 + [(429, "daily_limit")] * 7)
        self.assertEqual((len(ran), self.ends()), (3, [("success", 0)] * 3))

    def test_the_admission_decides_again_after_the_quick_check(self):
        """The decision inside the transaction is the one that counts, not an outdated quick check."""
        self.switch("1")
        ada = self.person()
        account = self.account()
        with self.models():
            self.assert_done(self.post(ada))
        outdated = AccessState(account_id=account["id"], email_verified=True, terms_accepted=True, uses_left_today=1)
        self.assertTrue(decide(outdated)[Capability.DECOMPOSE].allowed)
        main.app.dependency_overrides[access_state] = lambda: outdated
        self.addCleanup(main.app.dependency_overrides.pop, access_state, None)
        with self.models() as ran:
            for route in ROUTES:
                self.assert_limited(self.post(ada, route), "2026-11-26T00:00:00+01:00", 12 * 3600)
        self.assertEqual(ran, [])


# ------------------------------------------------------------------------------------------------
# Whom it does not hold for
# ------------------------------------------------------------------------------------------------


class WhoTests(LimitTestCase):
    def assert_without_limit(self, browser: TestClient) -> None:
        with self.models() as ran:
            for route in ROUTES * 2:
                self.assert_done(self.post(browser, route))
        self.assertEqual((len(ran), self.allowance(browser)), (4, (None, None)))
        self.assertEqual(self.uses(), [])

    def test_a_local_account_the_admin_and_the_owner_have_no_limit(self):
        self.switch("1")
        self.store.create_user(username="bob", password="password-123")
        self.assert_without_limit(self.password_browser("bob", "password-123"))
        self.assert_without_limit(self.password_browser())  # the admin
        code, out = run_cli("owner", "bind", "--username", "admin")
        self.assertEqual(code, 0, out)
        self.assert_without_limit(self.password_browser())  # the owner

    def test_an_approved_account_has_no_limit_and_an_approval_or_its_end_reaches_a_session_at_once(self):
        self.switch("1")
        ada = self.person()
        with self.models():
            self.assert_done(self.post(ada))
            self.assertEqual(self.post(ada).status_code, 429)
            self.approve()
            for route in ROUTES * 2:
                self.assert_done(self.post(ada, route))
            self.assertEqual(self.allowance(ada), (None, None))
            self.approve(change="withdraw-approval")
            self.assertEqual(self.allowance(ada)[0], 0)
            self.assertEqual(self.post(ada, STREAM_ROUTE).status_code, 429)
        self.assertEqual(self.ends(), [("success", 0)])


# ------------------------------------------------------------------------------------------------
# What counts and what is given back (D-021)
# ------------------------------------------------------------------------------------------------

ANSWER = json.dumps({"hasProperty": "temperature", "hasObjectOfInterest": "air", "hasMatrix": "", "hasContextObject": "", "hasConstraint": [], "hasStatisticalModifier": ""})


class Reply:
    """PSNC's HTTP answer: a status, and for 200 the model's text, which is streamed when it is asked for."""

    text = "a provider's own words"

    def __init__(self, status: int, answer: str = "") -> None:
        self.status_code, self._answer = status, answer

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)  # type: ignore[arg-type]

    def json(self) -> Dict[str, Any]:
        return {"choices": [{"message": {"role": "assistant", "content": self._answer}}]}

    def iter_lines(self, decode_unicode: bool = False) -> Iterator[str]:
        yield "data: " + json.dumps({"choices": [{"delta": {"content": self._answer}}]})
        yield "data: [DONE]"


class FakeSession:
    """What the PSNC requests go through: the answers in turn, the last one from then on."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes, self.requests = outcomes, 0

    def post(self, url: str, headers: Any = None, json: Any = None, timeout: Any = None, stream: bool = False) -> Reply:
        self.requests += 1
        outcome = self.outcomes[min(self.requests, len(self.outcomes)) - 1]
        return Reply(200, outcome) if isinstance(outcome, str) else Reply(outcome)


class OutcomeTests(LimitTestCase):
    """The real pipeline on both routes, with PSNC's answers faked."""

    def setUp(self) -> None:
        super().setUp()
        self.switch("1000")
        for name, value in (
            ("enabled_model_providers_raw", "psnc,openrouter"), ("psnc_api_key", "test"),
            ("psnc_model_name", "Qwen3.8-27B"), ("psnc_model_names_raw", "Qwen3.8-27B"),
            ("enable_wikidata_linking", False), ("use_measured_configuration", True),
        ):
            self.replace(llm.settings, name, value)
        self.ada = self.person()

    def test_a_run_that_reached_a_model_counts_and_one_refused_or_never_sent_is_given_back(self):
        cases = (
            ("an answer", (ANSWER,), ("success", 0)),
            ("server errors, then a refusal", (503, 401), ("failed", 0)),
            ("a refused key", (401,), ("refused", 1)),
            ("no key", None, ("not_sent", 1)),
        )
        for route in ROUTES:
            for name, outcomes, end in cases:
                with self.subTest(name, route=route):
                    before = self.allowance(self.ada)[0]
                    session = FakeSession(*(outcomes or (ANSWER,)))
                    with patch.object(llm, "get_http_session", return_value=session), \
                            patch.object(llm.settings, "psnc_api_key", "test" if outcomes else None):
                        response = self.post(self.ada, route, {**DEFINITION, "model_provider": "psnc"})
                    self.assertEqual(self.ends()[-1], end)
                    self.assertEqual(before - self.allowance(self.ada)[0], 0 if end[1] else 1)
                    if end[0] == "success":
                        self.assert_done(response)
                    else:
                        self.assertNotIn('"final"', response.text)


# ------------------------------------------------------------------------------------------------
# A browser that leaves (D-021)
# ------------------------------------------------------------------------------------------------


class LeavingTests(LimitTestCase):
    """On a real server, as a test client never leaves."""

    def serve(self) -> str:
        """Run the application on a real server; returns its address."""
        server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=0, log_level="critical", lifespan="off"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(0.02)

        def stop() -> None:
            server.should_exit = True
            thread.join(20)
            self.assertFalse(thread.is_alive(), "a request keeps the server from stopping")

        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"

    @staticmethod
    def endless(definition: str, calls: Optional[CallLog] = None, **_: Any) -> Iterator[str]:
        while True:
            yield stream_event("raw_delta", delta="{")
            time.sleep(0.02)

    def test_a_browser_that_leaves_a_stream_keeps_its_use_taken_and_the_run_is_recorded(self):
        """At once, so that no restart gives it back; and the run's record and audit row are there."""
        self.switch("2")
        ada = self.person()
        headers, cookies = self.csrf_headers(ada), {ROHUB_COOKIE: ada.cookies[ROHUB_COOKIE]}
        base = self.serve()
        self.addCleanup(gc.collect)  # nothing may depend on garbage collection: it can come at any time, in any thread
        gc.collect()
        gc.disable()
        self.addCleanup(gc.enable)
        for left in (1, 0):
            with patch.object(decompose_router, "stream_pipeline_events", side_effect=self.endless):
                response = requests.post(f"{base}{STREAM_ROUTE}", json=DEFINITION, headers=headers, cookies=cookies, stream=True, timeout=20)
                self.assertEqual(response.status_code, 200)
                lines = response.iter_lines()  # dropping it would close the connection
                self.assertIn(b"raw_delta", next(lines))
                self.assertEqual((self.ends()[-1], self.allowance(ada)[0]), ((None, 0), left))
                response.close()
                self.await_ends([("client_left", 0)] * (2 - left))
            deadline = time.monotonic() + 20
            while len(self.audit("decompose.stream")) < 2 - left and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(self.audit("decompose.stream")), 2 - left)
            self.assertEqual([row["error"] for row in self.rows("SELECT error FROM record_runs")],
                             ["The decomposition stopped before it finished."] * (2 - left))
            admission.release_interrupted()
            self.assertEqual((self.ends()[-1], self.allowance(ada)[0]), (("client_left", 0), left))
        answer = requests.post(f"{base}{JSON_ROUTE}", json=DEFINITION, headers=headers, cookies=cookies, timeout=20)
        self.assertEqual((answer.status_code, answer.json()["reason"], answer.headers["Retry-After"]), (429, "daily_limit", str(12 * 3600)))


# ------------------------------------------------------------------------------------------------
# A restart, and what an account's uses belong to
# ------------------------------------------------------------------------------------------------


class EndTests(LimitTestCase):
    def test_a_restart_gives_back_what_was_running_and_nothing_else(self):
        self.switch("9")
        ada = self.person()
        account = self.account()
        ended = [outcome for outcome in usage.Outcome if outcome is not usage.Outcome.INTERRUPTED]
        for outcome in ended:
            admission.admit(account["id"]).finish(outcome)
        self.moment -= timedelta(days=1)
        admission.admit(account["id"])  # yesterday's, in the middle of which the server stopped
        self.moment += timedelta(days=1)
        admission.admit(account["id"])
        before = self.uses()
        self.assertEqual(self.allowance(ada)[0], 9 - 3 - 1)
        with started_app():
            self.assertEqual(self.uses()[:-2], before[:-2])
            self.assertEqual(self.ends()[-2:], [("interrupted", 1)] * 2)
            self.assertEqual(self.allowance(ada)[0], 9 - 3)
        after = self.uses()
        with started_app():
            self.assertEqual(self.uses(), after)

    def test_a_use_belongs_to_an_account_and_goes_with_it(self):
        account = self.own_account("ada")
        day, moment = usage.day_of(utc("2026-11-25T12:00:00+01:00")), utc("2026-11-25T12:00:00+01:00")
        with storage.transaction(path=self.store.db_path) as conn:
            usage.add_use(conn, account["id"], day, moment)
        with self.assertRaises(sqlite3.IntegrityError), storage.transaction(path=self.store.db_path) as conn:
            usage.add_use(conn, 10_000, day, moment)
        self.assertEqual([row["user_id"] for row in self.uses()], [account["id"]])
        self.execute("DELETE FROM account_identities WHERE user_id = ?", account["id"])
        self.execute("DELETE FROM users WHERE id = ?", account["id"])
        self.assertEqual(self.uses(), [])
