"""The daily limit on decompositions, switched off unless one setting is set (a slice of usage-ledger).

The owner, 2026-11-25: "do not apply the decomposition limit for now implement it but switched off so
maybe later we switch it on", and for whom: "No, unapproved only". Switched off, an account that is not
approved waits for an approval (test_account_approval); switched on, it may decompose so many times a day.
What counts is D-021's, the day in Europe/Berlin D-033's, the refusal and ``GET /api/auth/me``
access-policy's (sections 4 to 7). People sign in through the fake provider. Models are faked: nothing
here reaches a provider.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import os
import pathlib
import socket
import sqlite3
import threading
import time
import unittest
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

import anyio
import anyio.lowlevel
import httpx
import openai
import previous_image
import requests
import uvicorn
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from increment1_support import ISSUER, TempDatabaseTestCase, run_cli, started_app
from increment2_support import PUBLIC_URL, ROHUB_COOKIE, OidcTestCase
from starlette.requests import ClientDisconnect
from test_records import CLIENT, PIPELINE_RESULT

from app import main, pipeline, storage, usage
from app.access import admission
from app.access import state as access_state_module
from app.access.dependencies import AccessRefused
from app.access.policy import AccessState, Capability, decide
from app.access.state import access_state
from app.accounts import AccountStore
from app.clients import openai_client, psnc_client
from app.core import config as core_config
from app.oidc.settings import SettingsError
from app.routers import decompose as decompose_router
from app.services import llm
from app.services.call_log import CallLog, ModelCall, start_call
from app.services.llm import stream_event

ROOT = pathlib.Path(__file__).resolve().parents[2]
JSON_ROUTE, STREAM_ROUTE = ROUTES = ("/api/decompose", "/api/decompose/stream")
DEFINITION = {"definition": "air temperature", "client": CLIENT}
REFUSED = "No decompositions left today."
MIGRATION = "0005_daily_decompositions"
MIGRATION_SHA256 = "95143ca9746c13820b668ec9788222996b4c98054c62bb6062b3d2c9d72aa9b6"


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


# ------------------------------------------------------------------------------------------------
# The switch
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
            self.assertEqual(run_cli("db", "status")[0], 0, "no operator command fails with it")
        return str(caught.exception)

    def test_anything_but_empty_or_a_whole_number_up_to_the_maximum_stops_the_start(self):
        """Covers: AC-daily-limit-001."""
        for value in ("0", "00", "-1", "+3", "03", "1.5", "1e2", "0x3", " ", " 3", "3 ", "3\n", "1001", "9999", "10000", "٣", "three", "true", "off", "none"):
            with self.subTest(value=value):
                message = self.refusal(value)
                self.assertIn("IADOPT_DAILY_DECOMPOSITIONS must be empty", message)
                self.assertIn("from 1 to 1000", message)

    def test_the_message_names_every_setting_at_fault_and_no_value(self):
        """Covers: AC-daily-limit-001."""
        message = self.refusal("a-secret-by-mistake", trusted_proxy_hops_raw="another-secret", auth_mode_raw="ldap-secret")
        for name in ("IADOPT_DAILY_DECOMPOSITIONS", "IADOPT_TRUSTED_PROXY_HOPS", "IADOPT_AUTH_MODE"):
            self.assertIn(name, message)
        self.assertNotIn("secret", message)

    def test_empty_is_off_and_whole_numbers_are_on(self):
        """Covers: AC-daily-limit-001."""
        for value, limit in (("", None), ("1", 1), ("3", 3), ("10", 10), ("999", 999), ("1000", 1000)):
            with self.subTest(value=value), patch.object(core_config.settings, "daily_decompositions_raw", value):
                self.assertEqual(usage.daily_limit(), limit)
                with started_app() as client:
                    self.assertEqual(client.get("/api/livez").status_code, 200)

    def test_a_system_without_the_time_zone_starts_only_with_the_limit_off(self):
        """Covers: AC-daily-limit-001."""
        with patch.object(usage, "TIME_ZONE", "Europe/Nowhere"):
            self.assertIn("IADOPT_DAILY_DECOMPOSITIONS needs the time zone Europe/Nowhere", self.refusal("3"))
            with patch.object(core_config.settings, "daily_decompositions_raw", ""), started_app() as client:
                self.assertEqual(client.get("/api/livez").status_code, 200)
        self.fresh.unlink()
        for error in (ValueError("Invalid TZif file"), PermissionError("not readable")):
            with self.subTest(repr(error)), patch.object(usage, "ZoneInfo", side_effect=error):
                self.assertIn("IADOPT_DAILY_DECOMPOSITIONS needs the time zone", self.refusal("3"))

    def test_it_is_read_from_its_variable_and_off_by_default(self):
        """Covers: AC-daily-limit-001."""
        for value, expected in (("7", "7"), ("", "")):
            with patch.dict(os.environ, {"IADOPT_DAILY_DECOMPOSITIONS": value}):
                self.assertEqual(core_config.Settings().daily_decompositions_raw, expected)
        with patch.dict(os.environ):
            del os.environ["IADOPT_DAILY_DECOMPOSITIONS"]
            self.assertEqual(core_config.Settings().daily_decompositions_raw, "")

    def test_it_is_documented_and_off_in_the_example(self):
        """Covers: AC-daily-limit-001."""
        self.assertIn("\nIADOPT_DAILY_DECOMPOSITIONS=\n", (ROOT / ".env.example").read_text())
        self.assertIn("`IADOPT_DAILY_DECOMPOSITIONS`", (ROOT / "docs" / "manual.md").read_text())


# ------------------------------------------------------------------------------------------------
# The day
# ------------------------------------------------------------------------------------------------


class DayTests(unittest.TestCase):
    CASES = (
        # An ordinary day in winter and in summer.
        ("2026-11-25T23:59:59.999999+01:00", "2026-11-25", "2026-11-26T00:00:00+01:00"),
        ("2026-11-26T00:00:00+01:00", "2026-11-26", "2026-11-27T00:00:00+01:00"),
        ("2026-07-01T23:59:59+02:00", "2026-07-01", "2026-07-02T00:00:00+02:00"),
        ("2026-12-31T23:59:59+01:00", "2026-12-31", "2027-01-01T00:00:00+01:00"),
        # 2026-03-29 has 23 hours and 2026-10-25 has 25; its 02:30 comes twice.
        ("2026-03-29T00:00:00+01:00", "2026-03-29", "2026-03-30T00:00:00+02:00"),
        ("2026-03-29T23:59:59+02:00", "2026-03-29", "2026-03-30T00:00:00+02:00"),
        ("2026-03-30T00:00:00+02:00", "2026-03-30", "2026-03-31T00:00:00+02:00"),
        ("2026-10-25T00:00:00+02:00", "2026-10-25", "2026-10-26T00:00:00+01:00"),
        ("2026-10-25T02:30:00+02:00", "2026-10-25", "2026-10-26T00:00:00+01:00"),
        ("2026-10-25T02:30:00+01:00", "2026-10-25", "2026-10-26T00:00:00+01:00"),
        ("2026-10-25T23:59:59+01:00", "2026-10-25", "2026-10-26T00:00:00+01:00"),
        ("2026-10-26T00:00:00+01:00", "2026-10-26", "2026-10-27T00:00:00+01:00"),
    )

    def test_a_day_begins_and_ends_at_midnight_in_berlin(self):
        """Covers: AC-daily-limit-004."""
        for moment, date, end in self.CASES:
            for seen_from in (timezone.utc, timezone(timedelta(hours=13)), timezone(timedelta(hours=-11))):
                with self.subTest(moment=moment, seen_from=seen_from):
                    day = usage.day_of(datetime.fromisoformat(moment).astimezone(seen_from))
                    self.assertEqual((day.date, day.end.isoformat()), (date, end))

    def test_the_days_of_a_clock_change_have_23_and_25_hours(self):
        """Covers: AC-daily-limit-004."""
        for start, hours in (("2026-03-29T00:00:00+01:00", 23), ("2026-10-25T00:00:00+02:00", 25), ("2026-11-25T00:00:00+01:00", 24)):
            with self.subTest(start):
                end = usage.day_of(utc(start)).end.astimezone(timezone.utc)
                self.assertEqual(end - utc(start), timedelta(hours=hours))

    def test_the_server_may_be_set_to_any_time_zone(self):
        """Covers: AC-daily-limit-004."""
        before = os.environ.get("TZ")
        self.addCleanup(time.tzset)
        self.addCleanup(lambda: os.environ.pop("TZ", None) if before is None else os.environ.__setitem__("TZ", before))
        for name in ("Pacific/Auckland", "America/Los_Angeles", "UTC"):
            os.environ["TZ"] = name
            time.tzset()
            with self.subTest(name):
                self.assertEqual(usage.now().utcoffset(), timedelta(0))
                self.assertLess(abs(usage.now().timestamp() - time.time()), 5)
                self.assertEqual(usage.day_of(utc("2026-11-25T23:59:59+01:00")).date, "2026-11-25")


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
# Off
# ------------------------------------------------------------------------------------------------


class OffTests(LimitTestCase):
    def test_off_counts_stores_and_refuses_nothing(self):
        """Covers: AC-daily-limit-002."""
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
            decompose = self.me(ben)["capabilities"]["decompose"]
            self.assertEqual((decompose["reason"], decompose["uses_left_today"], decompose["resets_at"]), ("approval_required", None, None))
            with started_app() as client:
                self.assertEqual(client.get("/api/livez").status_code, 200)
        self.assertEqual((len(ran), self.uses(), opened, admission._waiting), (12, [], [], {}))

    def test_off_streams_with_the_response_it_always_had(self):
        """Covers: AC-daily-limit-002."""
        account = self.store.get_user(self.own_account("ada")["id"])
        request = SimpleNamespace(method="POST", url=SimpleNamespace(path=STREAM_ROUTE), headers={}, client=None)
        for value, kind in (("", StreamingResponse), ("3", decompose_router.RunResponse)):
            self.switch(value)
            response = decompose_router.decompose_stream(decompose_router.DecomposeRequest(definition="air"), request, account)  # type: ignore[arg-type]
            self.assertIs(type(response), kind)
            self.assertEqual((response.status_code, response.headers["content-type"]), (200, "application/x-ndjson"))

    def test_off_opens_no_connection_of_its_own(self):
        """Covers: AC-daily-limit-002."""
        ada, opened = self.person(), []
        transaction = storage.transaction

        def counting(*args: Any, **kwargs: Any) -> Any:
            opened.append(args)
            return transaction(*args, **kwargs)

        def opened_by(*steps: Callable[[], Any]) -> List[int]:
            counts = []
            for step in steps:
                opened.clear()
                with patch.object(storage, "transaction", side_effect=counting):
                    step()
                counts.append(len(opened))
            return counts

        account = self.account()
        steps = (lambda: self.me(ada), lambda: admission.admit(account["id"]), admission.release_interrupted)
        me, admit, start = opened_by(*steps)
        self.assertEqual((admit, start), (0, 0))
        self.assertIs(admission.admit(account["id"]), admission.NOT_COUNTED)
        self.switch("3")
        self.assertEqual(opened_by(*steps), [me + 1, 1, 1])

    def test_off_leaves_what_an_earlier_limit_stored_as_it_is(self):
        """Covers: AC-daily-limit-002."""
        self.switch("2")
        ada = self.person()
        admission.admit(self.account()["id"])
        with self.models():
            self.assert_done(self.post(ada))
        before = self.uses()
        self.assertEqual(self.ends(), [(None, 0), ("success", 0)])
        self.switch("")
        with started_app(), self.models() as ran:
            for route in ROUTES:
                self.assertEqual(self.post(ada, route).json()["reason"], "approval_required")
            self.assertEqual((ran, self.allowance(ada)), ([], (None, None)))
            self.approve()
            for route in ROUTES:
                self.assert_done(self.post(ada, route))
        self.assertEqual(self.uses(), before)
        self.assertEqual(self.allowance(ada), (None, None))


# ------------------------------------------------------------------------------------------------
# On
# ------------------------------------------------------------------------------------------------


class OnTests(LimitTestCase):
    def test_the_last_use_of_the_day_is_the_last_on_both_routes(self):
        """Covers: AC-daily-limit-003."""
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
        account = self.account()
        self.assertEqual({(row["user_id"], row["berlin_date"], row["reserved_at"]) for row in self.uses()},
                         {(account["id"], "2026-11-25", "2026-11-25T11:00:00+00:00")})

    def test_each_account_has_its_own(self):
        """Covers: AC-daily-limit-005."""
        self.switch("1")
        ada, ben = self.person("ada"), self.person("ben")
        with self.models():
            self.assert_done(self.post(ada))
            self.assertEqual((self.allowance(ada)[0], self.allowance(ben)[0]), (0, 1))
            self.assert_done(self.post(ben, STREAM_ROUTE))
            self.assertEqual([self.post(browser).status_code for browser in (ada, ben)], [429, 429])

    def test_saving_and_the_public_tools_go_on_without_uses_left(self):
        """Covers: AC-daily-limit-009."""
        self.switch("1")
        ada = self.person()
        with self.models():
            self.assert_done(self.post(ada))
            self.assertEqual(self.post(ada).status_code, 429)
        capabilities = self.me(ada)["capabilities"]
        self.assertEqual({name for name, entry in capabilities.items() if entry["allowed"]}, {"public_tools", "session", "account_data", "save_and_review"})
        self.assertEqual({capabilities[name]["reason"] for name in ("publish", "retract_own")}, {"approval_required"})

    def test_unused_uses_do_not_carry_over_and_a_new_session_adds_none(self):
        """Covers: AC-daily-limit-005."""
        self.switch("2")
        ada = self.person()
        with self.models():
            self.assert_done(self.post(ada))
            self.assertEqual(self.allowance(self.person("ada"))[0], 1)
            self.at("2026-11-26T08:00:00+01:00")
            self.assertEqual(self.allowance(ada), (2, "2026-11-27T00:00:00+01:00"))

    def test_a_new_limit_holds_for_the_day_that_has_begun(self):
        """Covers: AC-daily-limit-005."""
        self.switch("4")
        ada = self.person()
        with self.models():
            for _ in range(3):
                self.assert_done(self.post(ada))
            for value, left in (("3", 0), ("1", 0), ("5", 2)):
                self.switch(value)
                self.assertEqual(self.allowance(ada)[0], left)
                self.assertEqual(self.post(ada).status_code, 429 if not left else 200)

    def test_a_decomposition_that_is_still_running_counts(self):
        """Covers: AC-daily-limit-005."""
        self.switch("1")
        ada, going, go_on = self.person(), threading.Event(), threading.Event()

        def wait(_: CallLog) -> Dict[str, Any]:
            going.set()
            go_on.wait(10)
            return dict(PIPELINE_RESULT)

        for route in ROUTES:
            with self.subTest(route), self.models(wait) as ran:
                going.clear()
                go_on.clear()
                results: List[Any] = []
                headers = self.csrf_headers(ada)
                other = self.browser()
                other.cookies.set(ROHUB_COOKIE, ada.cookies[ROHUB_COOKIE])
                thread = threading.Thread(target=lambda: results.append(other.post(route, json=DEFINITION, headers=headers)))
                thread.start()
                self.assertTrue(going.wait(10))
                try:
                    self.assertEqual((self.ends()[-1], self.allowance(ada)[0]), ((None, 0), 0))
                    self.assertEqual([self.post(ada, other_route).status_code for other_route in ROUTES], [429, 429])
                finally:
                    go_on.set()
                    thread.join(10)
                self.assert_done(results[0])
                self.assertEqual((ran, self.ends()[-1]), ([route], ("success", 0)))
            self.moment += timedelta(days=1)

    def test_the_hours_of_the_day_over_http(self):
        """Covers: AC-daily-limit-004.

        D-033 and plan v2: 23:59:59 and 00:00:00, and the days with 23 and 25 hours.
        """
        self.switch("1")
        ada = self.person()
        days = (
            # first second, resets at, hours, some moments of the same day, first second of the next
            ("2026-11-25T00:00:00+01:00", "2026-11-26T00:00:00+01:00", 24, ("2026-11-25T01:00:00+01:00",), "2026-11-26T00:00:00+01:00"),
            ("2026-03-29T00:00:00+01:00", "2026-03-30T00:00:00+02:00", 23, ("2026-03-29T01:59:59+01:00", "2026-03-29T03:00:00+02:00"), "2026-03-30T00:00:00+02:00"),
            ("2026-10-25T00:00:00+02:00", "2026-10-26T00:00:00+01:00", 25, ("2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00", "2026-10-25T23:00:00+01:00"), "2026-10-26T00:00:00+01:00"),
        )
        with self.models():
            for first, resets_at, hours, others, following in days:
                with self.subTest(first):
                    self.at(first)
                    self.moment -= timedelta(seconds=1)
                    self.assert_done(self.post(ada, STREAM_ROUTE))  # 23:59:59 of the day before
                    self.assert_limited(self.post(ada), first, 1)
                    self.at(first)  # 00:00:00
                    self.assertEqual(self.allowance(ada), (1, resets_at))
                    self.assert_done(self.post(ada))
                    self.assert_limited(self.post(ada, STREAM_ROUTE), resets_at, hours * 3600)
                    for moment in others:
                        self.at(moment)
                        self.assertEqual(self.post(ada).status_code, 429, moment)
                    self.at(following)
                    self.moment -= timedelta(seconds=1)  # 23:59:59
                    self.assert_limited(self.post(ada), resets_at, 1)
                    self.moment += timedelta(microseconds=500_000)
                    self.assert_limited(self.post(ada), resets_at, 1)
                    self.at(following)
                    self.assert_done(self.post(ada))
                self.execute("DELETE FROM decomposition_uses")

    def test_who_waits_for_the_lock_past_midnight_has_the_new_day(self):
        """Covers: AC-daily-limit-004."""
        self.switch("1")
        account = self.own_account("ada")
        self.at("2026-11-25T23:59:59+01:00")
        admission.admit(account["id"]).finish(usage.Outcome.SUCCESS)
        holder = sqlite3.connect(self.store.db_path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        results: List[Any] = []
        thread = threading.Thread(target=lambda: results.append(self.tried(account["id"])))
        thread.start()
        time.sleep(0.5)  # it is waiting for the lock
        self.assertEqual(results, [])
        self.at("2026-11-26T00:00:01+01:00")
        holder.execute("COMMIT")
        thread.join(10)
        self.assertEqual((results, [row["berlin_date"] for row in self.uses()]), (["admitted"], ["2026-11-25", "2026-11-26"]))

    def tried(self, account_id: int) -> str:
        """What became of one admission: ``admitted``, the reason of a refusal, or an error."""
        try:
            admission.admit(account_id)
        except AccessRefused as refused:
            return str(refused.body.get("reason"))
        except Exception as error:  # noqa: BLE001 - an error is a result here
            return repr(error)
        return "admitted"

    def test_reservations_at_the_same_moment_never_pass_the_limit(self):
        """Covers: AC-daily-limit-006.

        Real threads on a real file: exactly as many as are left, and no error.
        """
        self.switch("3")
        for number in range(6):
            account, barrier, results = self.own_account(f"person-{number}"), threading.Barrier(16), []

            def reserve() -> None:
                barrier.wait()
                results.append(self.tried(account["id"]))

            threads = [threading.Thread(target=reserve) for _ in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
            self.assertEqual(sorted(results), ["admitted"] * 3 + ["daily_limit"] * 13)
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM decomposition_uses WHERE user_id = ?", account["id"]), [{"n": 3}])

    def test_requests_at_the_same_moment_never_pass_the_limit(self):
        """Covers: AC-daily-limit-006.

        Both routes at once: the model is reached as often as uses are left, and every other request is refused.
        """
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
        """Covers: AC-daily-limit-007.

        access-policy, section 5: the decision inside the transaction is the one that counts.
        """
        self.switch("1")
        ada = self.person()
        account = self.account()
        with self.models():
            self.assert_done(self.post(ada))
        outdated = AccessState(account_id=account["id"], email_verified=True, terms_accepted=True, uses_left_today=1)
        self.assertTrue(decide(outdated)[Capability.DECOMPOSE].allowed)
        main.app.dependency_overrides[access_state] = lambda: outdated
        self.addCleanup(main.app.dependency_overrides.pop, access_state, None)
        stored = [self.rows(f"SELECT * FROM {table}") for table in ("record_items", "record_events", "audit_events", "decomposition_uses")]
        with self.models() as ran:
            for route in ROUTES:
                self.assert_limited(self.post(ada, route), "2026-11-26T00:00:00+01:00", 12 * 3600)
        self.assertEqual(ran, [])
        self.assertEqual([self.rows(f"SELECT * FROM {table}") for table in ("record_items", "record_events", "audit_events", "decomposition_uses")], stored)

    def test_an_approval_given_or_withdrawn_meanwhile_decides(self):
        """Covers: AC-daily-limit-007.

        The admission reads it again, after the quick check.
        """
        self.switch("1")
        ada = self.person()
        account = self.account()
        with self.models():
            self.assert_done(self.post(ada))
        for approved, status, counted in ((True, 200, 1), (False, 429, 1)):
            for route in ROUTES:
                stored = ("2026-11-25T12:00:00+00:00", 1) if approved else (None, None)
                self.execute("UPDATE users SET approved_at = ?, approved_by = ? WHERE id = ?", *((None, None) if approved else ("2026-11-25T12:00:00+00:00", 1)), account["id"])

                def changed_meanwhile(account_id: int, stored: Tuple[Any, Any] = stored) -> Any:
                    self.execute("UPDATE users SET approved_at = ?, approved_by = ? WHERE id = ?", *stored, account_id)
                    return admission.admit(account_id)

                # The quick check must pass, so it sees the account as it is not.
                main.app.dependency_overrides[access_state] = lambda: AccessState(account_id=account["id"], email_verified=True, terms_accepted=True, uses_left_today=1)
                self.addCleanup(main.app.dependency_overrides.pop, access_state, None)
                with self.subTest(approved=approved, route=route), self.models(), patch.object(decompose_router, "admit", side_effect=changed_meanwhile):
                    self.assertEqual(self.post(ada, route).status_code, status)
                    self.assertEqual(len(self.uses()), counted)

    def test_an_account_that_was_blocked_or_lost_its_email_meanwhile_is_not_admitted(self):
        """Covers: AC-daily-limit-007."""
        self.switch("3")
        ada = self.person()
        account = self.account()
        for change, status, reason in (("is_active = 0", 403, "blocked"), ("email_verified = 0", 403, "email_not_verified")):
            for route in ROUTES:
                def changed_meanwhile(account_id: int, change: str = change) -> Any:
                    self.execute(f"UPDATE users SET {change} WHERE id = ?", account_id)
                    return admission.admit(account_id)

                with self.subTest(change=change, route=route), self.models() as ran:
                    with patch.object(decompose_router, "admit", side_effect=changed_meanwhile):
                        response = self.post(ada, route)
                    self.assertEqual((response.status_code, response.json()["reason"]), (status, reason))
                    self.assertEqual((ran, self.uses()), ([], []))
                self.execute("UPDATE users SET is_active = 1, email_verified = 1 WHERE id = ?", account["id"])

    def test_an_account_that_is_gone_is_not_admitted(self):
        """Covers: AC-daily-limit-007."""
        self.switch("3")
        with self.assertRaises(AccessRefused) as caught:
            admission.admit(10_000)
        self.assertEqual((caught.exception.status_code, self.uses()), (401, []))


# ------------------------------------------------------------------------------------------------
# Whom it does not hold for, and changes that reach a session at once
# ------------------------------------------------------------------------------------------------


class WhoTests(LimitTestCase):
    def assert_without_limit(self, browser: TestClient) -> None:
        with self.models() as ran:
            for route in ROUTES * 2:
                self.assert_done(self.post(browser, route))
        self.assertEqual((len(ran), self.allowance(browser)), (4, (None, None)))
        self.assertEqual(self.uses(), [])

    def test_a_local_account_the_admin_and_the_owner_have_no_limit(self):
        """Covers: AC-daily-limit-008."""
        self.switch("1")
        self.store.create_user(username="bob", password="password-123")
        self.assert_without_limit(self.password_browser("bob", "password-123"))
        self.assert_without_limit(self.password_browser())  # the admin
        code, out = run_cli("owner", "bind", "--username", "admin")
        self.assertEqual(code, 0, out)
        self.assert_without_limit(self.password_browser())  # the owner

    def test_an_admin_and_the_owner_with_a_rohub_account_have_no_limit(self):
        """Covers: AC-daily-limit-008."""
        self.switch("1")
        ada, ben = self.person("ada"), self.person("ben")
        code, out = run_cli("owner", "bind", "--issuer", self.provider.issuer, "--subject", "subject-ada")
        self.assertEqual(code, 0, out)
        ada = self.person("ada")  # owner bind ended the session (owner-sign-in, section 4)
        self.assert_without_limit(ada)
        granted = ada.post(f"/api/admin/users/{self.account('ben')['id']}/grant-admin", json={}, headers=self.csrf_headers(ada))
        self.assertEqual(granted.status_code, 200, granted.text)
        self.assert_without_limit(ben)

    def test_the_development_user_has_no_limit(self):
        """Covers: AC-daily-limit-008."""
        self.switch("1")
        self.settings(auth_mode_raw="local")
        self.store.enabled = False
        self.assert_without_limit(self.browser())

    def test_an_approved_account_has_no_limit_and_an_approval_or_its_end_reaches_a_session_at_once(self):
        """Covers: AC-daily-limit-008.

        The owner, 2026-11-25: the limit covers only accounts that are not approved.
        """
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

    def test_a_grant_a_revocation_and_a_block_reach_a_session_at_once(self):
        """Covers: AC-daily-limit-008."""
        self.switch("1")
        ada = self.person()
        code, out = run_cli("owner", "bind", "--username", "admin")
        self.assertEqual(code, 0, out)
        owner = self.password_browser()  # after the bind, which ends the account's sessions (owner-sign-in, section 4)
        account = self.account()

        def change(what: str) -> None:
            response = owner.post(f"/api/admin/users/{account['id']}/{what}", json={"reason": "A test."})
            self.assertEqual(response.status_code, 200, response.text)

        with self.models() as ran:
            self.assert_done(self.post(ada))
            self.assertEqual(self.post(ada).status_code, 429)
            change("grant-admin")
            for route in ROUTES * 2:
                self.assert_done(self.post(ada, route))
            change("revoke-admin")
            for route in ROUTES:
                self.assertEqual(self.post(ada, route).status_code, 429)
            headers = self.csrf_headers(ada)
            self.at("2026-11-26T08:00:00+01:00")  # a new day, so nothing but the block refuses
            change("block")
            for route in ROUTES:
                self.assertEqual(ada.post(route, json=DEFINITION, headers=headers).status_code, 401)
        self.assertEqual((len(ran), self.ends()), (5, [("success", 0)]))

    def test_a_linked_local_account_comes_under_the_limit_at_once_and_leaves_it_at_once(self):
        """Covers: AC-daily-limit-008."""
        self.switch("1")
        self.store.create_user(username="bob", password="password-123")
        bob, account = self.password_browser("bob", "password-123"), self.account("bob")
        self.assert_without_limit(bob)
        code, out = run_cli("owner", "bind", "--username", "admin")
        self.assertEqual(code, 0, out)
        code, out = run_cli("legacy", "link", "--account", str(account["id"]), "--issuer", self.provider.issuer, "--subject", "subject-bob", "--reason", "A test.")
        self.assertEqual(code, 0, out)
        with self.models():
            self.assertEqual(self.allowance(bob)[0], 1)
            self.assert_done(self.post(bob, STREAM_ROUTE))
            self.assertEqual(self.post(bob).status_code, 429)
            self.execute("DELETE FROM account_identities WHERE user_id = ?", account["id"])  # a local account again
            self.assert_done(self.post(bob))
        self.assertEqual((self.allowance(bob), self.ends()), ((None, None), [("success", 0)]))


# ------------------------------------------------------------------------------------------------
# What counts and what is given back (D-021)
# ------------------------------------------------------------------------------------------------

ANSWER = json.dumps({"hasProperty": "temperature", "hasObjectOfInterest": "air", "hasMatrix": "", "hasContextObject": "", "hasConstraint": [], "hasStatisticalModifier": ""})


class Reply:
    """A provider's HTTP answer: a status, and for 200 the model's text, which is streamed when it is asked for."""

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


Outcomes = Callable[[int], Any]
"""Gives the provider's answer to the n-th request: a status, a text for status 200, or an exception."""


class FakeSession:
    """What the PSNC requests go through."""

    def __init__(self, outcomes: Outcomes) -> None:
        self.outcomes, self.requests = outcomes, 0

    def post(self, url: str, headers: Any = None, json: Any = None, timeout: Any = None, stream: bool = False) -> Reply:
        self.requests += 1
        outcome = self.outcomes(self.requests)
        if isinstance(outcome, Exception):
            raise outcome
        return Reply(200, outcome) if isinstance(outcome, str) else Reply(outcome)


def status_error(status: int) -> openai.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    return openai.APIStatusError("a provider's own words", response=response, body=None)


def openrouter(outcomes: Outcomes) -> Any:
    """An OpenRouter client with the same kind of answers."""
    seen = SimpleNamespace(requests=0)

    def create(stream: bool = False, **_: Any) -> Any:
        seen.requests += 1
        outcome = outcomes(seen.requests)
        if isinstance(outcome, int):
            raise status_error(outcome)
        if isinstance(outcome, Exception):
            raise outcome
        if stream:
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=outcome))], usage=None)])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))], usage=None)

    seen.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    return seen


def always(outcome: Any) -> Outcomes:
    return lambda _: outcome


def in_turn(*outcomes: Any) -> Outcomes:
    """The answers one after the other, the last one from then on."""
    return lambda number: outcomes[min(number, len(outcomes)) - 1]


REQUEST = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")

KEPT: Dict[str, Tuple[Outcomes, str]] = {
    "an answer": (always(ANSWER), "success"),
    "an answer after two failures": (in_turn(503, 429, ANSWER), "success"),
    "an answer after a refusal": (in_turn(401, ANSWER), "success"),
    "an answer that is no JSON": (always("I cannot do that."), "failed"),
    "an empty answer": (always(""), "failed"),
    "HTML": (always("<!DOCTYPE html><html></html>"), "failed"),
    "a server error": (always(500), "failed"),
    "an unavailable provider": (always(503), "failed"),
    "too many requests": (always(429), "failed"),
    "a request timeout": (always(408), "failed"),
    "a conflict": (always(409), "failed"),
    "a refusal among server errors": (in_turn(503, 401), "failed"),
    "a server error among refusals": (in_turn(401, 401, 503, 401), "failed"),
    "a refusal after an answer that is no use": (in_turn("no JSON", 401), "failed"),
}

RELEASED: Dict[str, Tuple[Outcomes, str]] = {
    "a refused input": (always(400), "refused"),
    "a refused key": (always(401), "refused"),
    "no credit": (always(402), "refused"),
    "a forbidden model": (always(403), "refused"),
    "an unknown model": (always(404), "refused"),
    "an input too large": (always(413), "refused"),
    "an input that is not accepted": (always(422), "refused"),
    "refusals of several kinds": (in_turn(401, 400, 404), "refused"),
}


class OutcomeTests(LimitTestCase):
    """The real pipeline on both routes and both providers, with the provider's answers faked."""

    def setUp(self) -> None:
        super().setUp()
        self.switch("1000")
        for name, value in (
            ("enabled_model_providers_raw", "psnc,openrouter"), ("psnc_api_key", "test"), ("openrouter_api_key", "test"),
            ("psnc_model_name", "Qwen3.8-27B"), ("psnc_model_names_raw", "Qwen3.8-27B"), ("model_name", "qwen/qwen3-32b"), ("model_names_raw", ""),
            ("enable_wikidata_linking", False), ("use_measured_configuration", True),
        ):
            self.replace(llm.settings, name, value)
        self.ada = self.person()

    def run_with(self, provider: str, route: str, outcomes: Outcomes) -> Tuple[Any, int]:
        """Decompose with a provider that answers like this; returns the response and how many requests it got."""
        body = {**DEFINITION, "model_provider": provider}
        if provider == "psnc":
            session = FakeSession(outcomes)
            with patch.object(llm, "get_http_session", return_value=session):
                return self.post(self.ada, route, body), session.requests
        client = openrouter(outcomes)
        with patch.object(llm, "get_openai_client", return_value=client), patch.object(pipeline, "get_openai_client", return_value=client):
            return self.post(self.ada, route, body), client.requests

    def assert_end(self, before: int, outcome: str, released: int) -> None:
        """The last use ended so, and one more use or none is gone."""
        self.assertEqual(self.ends()[-1], (outcome, released))
        self.assertEqual(before - (self.allowance(self.ada)[0] or 0), 0 if released else 1)

    def left(self) -> int:
        return self.allowance(self.ada)[0] or 0

    def test_what_a_model_may_have_worked_on_counts_and_what_the_provider_refused_is_given_back(self):
        """Covers: AC-daily-limit-010."""
        cases = [(name, *case, 0) for name, case in KEPT.items()] + [(name, *case, 1) for name, case in RELEASED.items()]
        for provider in ("psnc", "openrouter"):
            for route in ROUTES:
                for name, outcomes, outcome, released in cases:
                    with self.subTest(name, provider=provider, route=route):
                        before, rows = self.left(), len(self.uses())
                        response, sent = self.run_with(provider, route, outcomes)
                        self.assertGreater(sent, 0)
                        self.assertEqual(len(self.uses()), rows + 1, "one click is one use, whatever it calls")
                        self.assert_end(before, outcome, released)
                        if outcome == "success":
                            self.assert_done(response)
                        elif route == JSON_ROUTE:
                            self.assertEqual(response.status_code, 500, response.text)
                        else:
                            self.assertEqual(json.loads(response.text.splitlines()[-1])["type"], "error")

    def test_a_connection_that_fails_or_takes_too_long_counts(self):
        """Covers: AC-daily-limit-010.

        D-021: exhausted transient provider failures count.
        """
        failures = {
            "psnc": (requests.ConnectionError("no route"), requests.Timeout("too long"), RuntimeError("anything else")),
            "openrouter": (openai.APIConnectionError(request=REQUEST), openai.APITimeoutError(request=REQUEST),
                           openai.APIError("Provider returned error", request=REQUEST, body=None), httpx.ReadError("broken")),
        }
        for provider, errors in failures.items():
            for error in errors:
                for route in ROUTES:
                    with self.subTest(provider=provider, error=type(error).__name__, route=route):
                        before = self.left()
                        self.run_with(provider, route, always(error))
                        self.assert_end(before, "failed", 0)

    def test_a_missing_key_takes_nothing(self):
        """Covers: AC-daily-limit-010."""
        for provider, setting in (("psnc", "psnc_api_key"), ("openrouter", "openrouter_api_key")):
            for route in ROUTES:
                with self.subTest(provider=provider, route=route), patch.object(llm.settings, setting, None), \
                        patch("app.clients.openai_client._openai_client", None):
                    before = self.left()
                    response = self.post(self.ada, route, {**DEFINITION, "model_provider": provider})
                    self.assertIn("API_KEY is not set", response.text)
                    self.assert_end(before, "not_sent", 1)

    def tries(self) -> List[Optional[str]]:
        """What the call log says of the decomposition requests of the last run."""
        rows = self.rows("SELECT error FROM record_llm_calls WHERE item_id = (SELECT MAX(id) FROM record_items) AND purpose = 'decomposition'")
        return [row["error"] for row in rows]

    def test_an_address_or_a_key_with_a_mistake_in_it_takes_nothing(self):
        """Covers: AC-daily-limit-010.

        Nothing reaches the provider. A request to such an address cannot be built, but the call log has its tries.

        Such a key is refused before there is a request (test_provider_keys), like a missing one. If it were not,
        it could not be sent either, and what the call log would then say of its tries must be known too.
        """
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))  # nobody listens there
        self.addCleanup(closed.close)
        here = f"http://127.0.0.1:{closed.getsockname()[1]}"
        cases = [("psnc_api_base_url", value) for value in ("", "llm.example.org", f"{here}x", "https://", "ftp://llm.example.org", "http://[::1")]
        cases += [("psnc_api_key", value) for value in ("a\nb", "a\n", "a\r\n", "κ")]
        sent: List[Any] = []

        def connected(*args: Any, **_: Any) -> Any:
            sent.append(args)
            raise OSError("no network in this test")

        for setting, value in cases:
            for route in ROUTES:
                for checked in (True, False):  # the check of the key
                    check = psnc_client.usable_key if checked else lambda _, key, *__: key
                    with self.subTest(setting, value=value, route=route, checked=checked), patch.object(llm.settings, "psnc_api_base_url", here), \
                            patch.object(llm.settings, setting, value), patch.object(psnc_client, "usable_key", check), \
                            patch("urllib3.util.connection.create_connection", side_effect=connected):
                        before = self.left()
                        self.post(self.ada, route, {**DEFINITION, "model_provider": "psnc"})
                        self.assert_end(before, "not_sent", 1)
                        self.assertEqual(len(self.tries()), 0 if checked and setting == "psnc_api_key" else 9 if route == JSON_ROUTE else 12)
        for route in ROUTES:
            with self.subTest("openrouter", route=route):
                client = openai.OpenAI(base_url=here, api_key="schlüssel", http_client=httpx.Client(transport=httpx.MockTransport(connected)))
                with patch.object(llm, "get_openai_client", return_value=client), patch.object(pipeline, "get_openai_client", return_value=client):
                    before = self.left()
                    self.post(self.ada, route, {**DEFINITION, "model_provider": "openrouter"})
                    self.assert_end(before, "not_sent", 1)
                    self.assertEqual(len(self.tries()), 9 if route == JSON_ROUTE else 12)
            for value in ("a\nb", "a\n", "a\r\n", "κ", "schlüssel", "a "):  # with a line break these looked like a lost connection, which counts
                with self.subTest("openrouter", value=value, route=route), patch.object(llm.settings, "openrouter_api_key", value), \
                        patch.object(openai_client, "_openai_client", None), patch.object(openai_client, "OpenAI", side_effect=connected):
                    before = self.left()
                    response = self.post(self.ada, route, {**DEFINITION, "model_provider": "openrouter"})
                    self.assertIn("OPENROUTER_API_KEY holds", response.text)
                    self.assert_end(before, "not_sent", 1)
                    self.assertEqual(self.tries(), [])
        self.assertEqual(sent, [])

    def test_a_service_that_fails_before_it_asks_a_model_takes_nothing(self):
        """Covers: AC-daily-limit-010."""
        for error in (RuntimeError("No prompt files found"), ValueError("not the person's fault either"), KeyError("a bug")):
            for route in ROUTES:
                with self.subTest(error=repr(error), route=route), patch.object(pipeline, "_prepare_pipeline_inputs", side_effect=error):
                    before = self.left()
                    self.post(self.ada, route)
                    self.assert_end(before, "not_sent", 1)

    def test_a_failure_after_the_answer_counts(self):
        """Covers: AC-daily-limit-010.

        A model has answered, so whatever the rest of the service then does with the answer is an output failure.
        """
        for error in (RuntimeError("no Turtle"), ValueError("no Turtle"), KeyError("hasProperty")):
            for provider in ("psnc", "openrouter"):
                for route in ROUTES:
                    with self.subTest(error=repr(error), provider=provider, route=route), patch.object(pipeline, "_finalize_pipeline_output", side_effect=error):
                        before = self.left()
                        response, _ = self.run_with(provider, route, always(ANSWER))
                        self.assertNotIn('"final"', response.text)
                        self.assert_end(before, "failed", 0)

    def test_only_the_decomposition_itself_decides_not_the_label_or_the_links(self):
        """Covers: AC-daily-limit-010."""
        for log in (("label", "linking"), ("linking",)):
            def failed(calls: CallLog, purposes: Tuple[str, ...] = log) -> Dict[str, Any]:
                start_call(calls, "decomposition", "psnc", "m").finish("error", error="HTTP 401")
                for purpose in purposes:
                    start_call(calls, purpose, "psnc", "m").finish("error", error="HTTP 503")
                raise RuntimeError("nothing came of it")

            with self.subTest(log), self.models(failed):
                before = self.left()
                self.post(self.ada)
                self.assert_end(before, "refused", 1)

    def test_a_request_that_is_not_valid_takes_nothing_and_is_stored_nowhere(self):
        """Covers: AC-daily-limit-011."""
        invalid = (
            ({"definition": "   "}, 400), ({"definition": "air", "model_provider": "no-such"}, 400),
            ({"definition": "air", "model_provider": "psnc", "model_name": "no-such"}, 400), ({}, 422), ({"definition": 5}, 422),
        )
        session = FakeSession(always(ANSWER))
        with patch.object(llm, "get_http_session", return_value=session):
            for body, status in invalid:
                for route in ROUTES:
                    with self.subTest(body=body, route=route):
                        response = self.post(self.ada, route, body)
                        self.assertEqual(response.status_code, 200 if status == 400 and route == STREAM_ROUTE else status, response.text)
                        self.assertNotIn('"final"', response.text)
        self.assertEqual((session.requests, self.uses(), self.left()), (0, [], 1000))

    def test_an_invalid_request_takes_nobodys_last_use_at_the_same_moment(self):
        """Covers: AC-daily-limit-011.

        It is never reserved, not even for a moment.
        """
        with patch.object(admission.usage, "add_use", side_effect=AssertionError("reserved")):
            for route in ROUTES:
                self.assertNotIn("reserved", self.post(self.ada, route, {"definition": " "}).text)

    def test_the_class_of_a_run_follows_its_calls(self):
        """Covers: AC-daily-limit-010."""
        def call(status: str, error: Optional[str], purpose: str = "decomposition") -> ModelCall:
            return ModelCall(purpose, 1, "psnc", "m", "2026-11-25T12:00:00+00:00", 1, None, None, status, error)

        refused, transient, ok = call("error", "HTTP 401"), call("error", "HTTP 429"), call("ok", None)
        cases: List[Tuple[List[ModelCall], usage.Outcome]] = [
            ([], usage.Outcome.NOT_SENT), ([call("error", "HTTP 503", "label")], usage.Outcome.NOT_SENT),
            ([refused], usage.Outcome.REFUSED), ([refused, refused, call("error", "HTTP 499")], usage.Outcome.REFUSED),
            ([refused, transient], usage.Outcome.FAILED), ([transient, refused], usage.Outcome.FAILED), ([ok], usage.Outcome.FAILED),
            ([call("unusable", "HTTP 401")], usage.Outcome.FAILED), ([call("error", None)], usage.Outcome.FAILED),
            ([call("error", "HTTP error")], usage.Outcome.FAILED), ([call("error", "HTTP 4010")], usage.Outcome.FAILED),
            ([call("error", "RuntimeError: HTTP 401")], usage.Outcome.FAILED), ([call("error", "HTTP 301")], usage.Outcome.FAILED),
            ([call("error", "HTTP 500")], usage.Outcome.FAILED),
        ]
        unsent = [call("error", text) for text in (
            "MissingSchema: Invalid URL", "InvalidSchema: No connection adapters", "InvalidURL: Failed to parse", "InvalidHeader: Invalid leading whitespace",
            "UnicodeEncodeError: 'ascii' codec", "ValueError: Invalid header value b'x'")]
        cases += [(unsent, usage.Outcome.NOT_SENT), (unsent + [refused], usage.Outcome.REFUSED), (unsent + [transient], usage.Outcome.FAILED), (unsent + [ok], usage.Outcome.FAILED)]
        cases += [([call("error", text)], usage.Outcome.FAILED) for text in (
            "ValueError: anything else", "ConnectionError: InvalidURL: no", "MissingSchemaX: no", "JSONDecodeError: Expecting value")]
        cases += [([call("unusable", "InvalidURL: no")], usage.Outcome.FAILED)]
        for calls, outcome in cases:
            with self.subTest([item.error for item in calls]):
                self.assertEqual(decompose_router._failure(calls), outcome)

    def test_each_class_is_kept_with_the_use(self):
        """Covers: AC-daily-limit-010."""
        self.assertEqual(
            {outcome.label: outcome.released for outcome in usage.Outcome},
            {"success": False, "failed": False, "client_left": False, "not_sent": True, "refused": True, "interrupted": True},
        )


class EndTests(LimitTestCase):
    def test_a_use_ends_once(self):
        """Covers: AC-daily-limit-012."""
        self.switch("5")
        account = self.own_account("ada")
        for first, second in ((usage.Outcome.SUCCESS, usage.Outcome.NOT_SENT), (usage.Outcome.REFUSED, usage.Outcome.SUCCESS)):
            use = admission.admit(account["id"])
            use.finish(first)
            use.finish(second)
            self.assertEqual(self.ends()[-1], (first.label, int(first.released)))
            with storage.transaction(path=self.store.db_path) as conn:
                usage.finish_use(conn, self.uses()[-1]["id"], second)
            self.assertEqual(self.ends()[-1], (first.label, int(first.released)))

    def test_a_use_that_is_given_back_can_be_taken_again(self):
        """Covers: AC-daily-limit-012."""
        self.switch("1")
        account = self.own_account("ada")
        for _ in range(3):
            admission.admit(account["id"]).finish(usage.Outcome.REFUSED)
        admission.admit(account["id"]).finish(usage.Outcome.FAILED)
        with self.assertRaises(AccessRefused):
            admission.admit(account["id"])
        self.assertEqual(self.ends(), [("refused", 1)] * 3 + [("failed", 0)])

    def test_the_end_of_a_run_past_midnight_belongs_to_the_day_it_began(self):
        """Covers: AC-daily-limit-012."""
        self.switch("1")
        account = self.own_account("ada")
        self.at("2026-11-25T23:59:59+01:00")
        use = admission.admit(account["id"])
        self.at("2026-11-26T00:00:00+01:00")
        admission.admit(account["id"])
        use.finish(usage.Outcome.REFUSED)
        with self.assertRaises(AccessRefused):
            admission.admit(account["id"])
        self.assertEqual([(row["berlin_date"], row["outcome"]) for row in self.uses()], [("2026-11-25", "refused"), ("2026-11-26", None)])

    def test_an_end_that_cannot_be_stored_fails_nothing_and_is_stored_later(self):
        """Covers: AC-daily-limit-013."""
        self.switch("9")
        ada, real = self.person(), usage.finish_use
        for route in ROUTES:
            errors = [sqlite3.OperationalError("disk I/O error"), ValueError("anything")]

            def failing(*args: Any) -> None:
                if errors:
                    raise errors.pop()
                real(*args)

            rows = len(self.uses())
            with self.subTest(route), self.models(), patch.object(usage, "finish_use", side_effect=failing):
                with self.assertLogs("app.access.admission", "ERROR") as logs:
                    self.assert_done(self.post(ada, route))
                    self.assertEqual((self.ends()[-1], self.allowance(ada)[0]), ((None, 0), 9 - rows - 1))
                    self.await_ends([("success", 0)] * (rows + 1))
                self.assertEqual((len(logs.output), errors), (2, []))
                self.assertIn("Could not store how a decomposition", logs.output[0])
            deadline = time.monotonic() + 5
            while admission._waiting and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(admission._waiting, {})

    def test_one_timer_at_a_time_tries_again_and_one_that_cannot_start_fails_nothing(self):
        """Covers: AC-daily-limit-013."""
        self.switch("20")
        account, real, broken = self.own_account("ada"), usage.finish_use, threading.Event()
        timers: List[threading.Timer] = []

        def timer(*args: Any) -> threading.Timer:
            timers.append(original(*args))
            return timers[-1]

        def failing(*args: Any) -> None:
            if broken.is_set():
                raise sqlite3.OperationalError("disk I/O error")
            real(*args)

        original = threading.Timer
        broken.set()
        with patch.object(usage, "finish_use", side_effect=failing), self.assertLogs("app.access.admission", "ERROR"):
            with patch.object(admission.threading, "Timer", side_effect=timer):
                uses = [admission.admit(account["id"]) for _ in range(6)]
                for use in uses:
                    use.finish(usage.Outcome.SUCCESS)
                self.assertEqual(len(timers), 1)
                deadline = time.monotonic() + 20
                while len(timers) < 4 and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertLessEqual(sum(item.is_alive() for item in timers), 2)  # the one that tries, and the next
                self.assertTrue(all(item.daemon for item in timers), "a timer keeps the process from ending")
            broken.clear()
            self.await_ends([("success", 0)] * 6)
            broken.set()
            with patch.object(threading.Timer, "start", side_effect=RuntimeError("can't start new thread")):
                admission.admit(account["id"]).finish(usage.Outcome.SUCCESS)
            self.assertEqual((self.ends()[-1], admission._retried), ((None, 0), set()))
            broken.clear()
            admission.admit(account["id"]).finish(usage.Outcome.FAILED)  # the next end stores it too
        self.assertEqual(self.ends()[-2:], [("success", 0), ("failed", 0)])

    def test_a_database_that_is_locked_when_a_run_ends_loses_no_end(self):
        """Covers: AC-daily-limit-013.

        No restart gives a use back although a model answered, and a given-back one is not taken for ever.
        """
        self.replace(storage.connections, "BUSY_TIMEOUT_SECONDS", 0.2)
        ada = self.person()
        for route in ROUTES:
            for error, end in ((None, ("success", 0)), ("HTTP 503", ("failed", 0)), ("HTTP 401", ("refused", 1))):
                self.execute("DELETE FROM decomposition_uses")
                self.switch("1")
                holder = sqlite3.connect(self.store.db_path, isolation_level=None, check_same_thread=False)

                def run(calls: CallLog, error: Optional[str] = error, holder: sqlite3.Connection = holder) -> Dict[str, Any]:
                    holder.execute("BEGIN IMMEDIATE")
                    if error is None:
                        return dict(PIPELINE_RESULT)
                    start_call(calls, "decomposition", "psnc", "m").finish("error", error=error)
                    raise RuntimeError("no answer")

                browser = self.browser(raise_server_exceptions=False)
                browser.cookies.set(ROHUB_COOKIE, ada.cookies[ROHUB_COOKIE])
                headers = self.csrf_headers(ada)
                with self.subTest(route=route, error=error), self.models(run), self.assertLogs("app.access.admission", "ERROR"):
                    response = browser.post(route, json=DEFINITION, headers=headers)
                    if error is None:
                        self.assert_done(response)
                    self.assertEqual((self.ends(), self.allowance(ada)[0]), ([(None, 0)], 0))
                    holder.execute("COMMIT")
                    self.await_ends([end])
                holder.close()
                with started_app():  # a restart changes nothing any more
                    self.assertEqual(self.ends(), [end])
                with self.models():
                    self.assertEqual(self.post(ada).status_code, 200 if end[1] else 429)

    def test_what_still_waits_is_stored_when_the_server_stops(self):
        """Covers: AC-daily-limit-013."""
        self.switch("1")
        self.replace(admission, "RETRY_SECONDS", 60)
        use = admission.admit(self.own_account("ada")["id"])
        with started_app():
            # The start supposed an interruption; the end, which could not be stored, comes at the stop.
            self.assertEqual(self.ends(), [("interrupted", 1)])
            with patch.object(usage, "finish_use", side_effect=sqlite3.OperationalError("locked")), self.assertLogs("app.access.admission", "ERROR"):
                use.finish(usage.Outcome.SUCCESS)
            self.assertEqual(self.ends(), [("interrupted", 1)])
        self.assertEqual((self.ends(), admission._waiting), ([("success", 0)], {}))

    def test_a_reservation_that_cannot_be_stored_calls_no_model(self):
        """Covers: AC-daily-limit-013."""
        self.switch("2")
        ada = self.person()
        browser = self.browser(raise_server_exceptions=False)
        browser.cookies.set(ROHUB_COOKIE, ada.cookies[ROHUB_COOKIE])
        headers = self.csrf_headers(ada)
        with self.models() as ran, patch.object(usage, "add_use", side_effect=sqlite3.OperationalError("disk I/O error")):
            for route in ROUTES:
                self.assertEqual(browser.post(route, json=DEFINITION, headers=headers).status_code, 500)
        self.assertEqual((ran, self.uses()), ([], []))

    def test_a_restart_gives_back_what_was_running_and_nothing_else(self):
        """Covers: AC-daily-limit-014."""
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

    def test_a_run_that_ends_after_it_was_given_back_was_not_interrupted(self):
        """Covers: AC-daily-limit-014.

        While two processes overlap, the new one's start supposes an interruption; the run's own end is the true one.
        """
        self.switch("1")
        account = self.own_account("ada")
        for outcome in usage.Outcome:
            use = admission.admit(account["id"])
            admission.release_interrupted()
            self.assertEqual(self.ends()[-1], ("interrupted", 1))
            use.finish(outcome)
            self.assertEqual(self.ends()[-1], (outcome.label, int(outcome.released)))
            admission.release_interrupted()
            self.assertEqual(self.ends()[-1], (outcome.label, int(outcome.released)))
            self.execute("DELETE FROM decomposition_uses")


class LeavingTests(LimitTestCase):
    """A browser that goes away changes nothing (D-021); on a real server, as a test client never leaves."""

    def serve(self, **options: Any) -> Tuple[str, Callable[[], None]]:
        """Run the application on a real server; returns its address and how to stop it."""
        server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=0, log_level="critical", lifespan="off", **options))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(0.02)

        def stop() -> None:
            server.should_exit = True
            thread.join(20)
            self.assertFalse(thread.is_alive(), "a request keeps the server from stopping")

        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}", stop

    def without_garbage_collection(self) -> None:
        """Nothing may depend on it: it can come at any time, in any thread."""
        self.addCleanup(gc.collect)
        gc.collect()
        gc.disable()
        self.addCleanup(gc.enable)

    @staticmethod
    def endless(definition: str, calls: Optional[CallLog] = None, **_: Any) -> Iterator[str]:
        while True:
            yield stream_event("raw_delta", delta="{")
            time.sleep(0.02)

    def test_a_browser_that_leaves_a_stream_keeps_its_use_taken(self):
        """Covers: AC-daily-limit-015.

        At once, so that no restart gives it back; and the run's record and audit row are there.
        """
        self.switch("2")
        ada = self.person()
        headers, cookies = self.csrf_headers(ada), {ROHUB_COOKIE: ada.cookies[ROHUB_COOKIE]}
        base, _ = self.serve()
        self.without_garbage_collection()
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
            self.assertEqual([row["error"] for row in self.rows("SELECT error FROM record_runs")], ["The decomposition stopped before it finished."] * (2 - left))
            admission.release_interrupted()
            self.assertEqual((self.ends()[-1], self.allowance(ada)[0]), (("client_left", 0), left))
        answer = requests.post(f"{base}{JSON_ROUTE}", json=DEFINITION, headers=headers, cookies=cookies, timeout=20)
        self.assertEqual((answer.status_code, answer.json()["reason"], answer.headers["Retry-After"]), (429, "daily_limit", str(12 * 3600)))

    def test_a_server_that_stops_a_stream_gives_its_use_back_at_the_next_start(self):
        """Covers: AC-daily-limit-015."""
        self.switch("1")
        ada = self.person()
        headers, cookies = self.csrf_headers(ada), {ROHUB_COOKIE: ada.cookies[ROHUB_COOKIE]}
        base, stop = self.serve(timeout_graceful_shutdown=1)
        with patch.object(decompose_router, "stream_pipeline_events", side_effect=self.endless):
            response = requests.post(f"{base}{STREAM_ROUTE}", json=DEFINITION, headers=headers, cookies=cookies, stream=True, timeout=20)
            lines = response.iter_lines()
            self.assertIn(b"raw_delta", next(lines))

            def read() -> None:
                with suppress(requests.RequestException):
                    for _ in lines:
                        pass

            reading = threading.Thread(target=read, daemon=True)
            reading.start()
            stop()
            reading.join(20)
            response.close()
            gc.collect()
        self.assertEqual(self.ends(), [(None, 0)])
        admission.release_interrupted()
        self.assertEqual((self.ends(), self.allowance(ada)[0]), ([("interrupted", 1)], 1))

    def test_a_browser_that_leaves_the_json_route_has_its_result_counted(self):
        """Covers: AC-daily-limit-015."""
        self.switch("2")
        ada = self.person()
        headers, cookies = self.csrf_headers(ada), {ROHUB_COOKIE: ada.cookies[ROHUB_COOKIE]}
        (base, _), going, go_on = self.serve(), threading.Event(), threading.Event()

        def wait(_: CallLog) -> Dict[str, Any]:
            going.set()
            go_on.wait(10)
            return dict(PIPELINE_RESULT)

        with self.models(wait):
            with self.assertRaises(requests.Timeout):
                requests.post(f"{base}{JSON_ROUTE}", json=DEFINITION, headers=headers, cookies=cookies, timeout=(5, 1))
            self.assertTrue(going.is_set())
            self.assertEqual(self.ends(), [(None, 0)])
            go_on.set()
            self.await_ends([("success", 0)])

    def break_off(self, response: Any, lines: int, how: str) -> None:
        """Let a server send a response that breaks off while the given line is sent; 0 is the header.

        Of a browser that goes away a server tells with a ``message`` (before 2.4 of the specification) or
        with an ``error`` while sending. A server that stops cancels the request, which reaches the response
        through a ``scope`` of the middleware, or as its own ``task`` when the event loop ends.
        """

        async def serve() -> None:
            sent, gone = 0, anyio.Event()

            async def receive() -> Dict[str, Any]:
                await gone.wait()
                return {"type": "http.disconnect"}

            async def send(message: Dict[str, Any]) -> None:
                nonlocal sent
                sent += message["type"] == "http.response.body"
                if sent < lines:
                    return
                if how == "error":
                    raise OSError("the browser is gone")
                {"message": gone.set, "scope": around.cancel, "task": lambda: request.cancel()}[how]()
                await anyio.sleep_forever()

            scope = {"type": "http", "asgi": {"spec_version": "2.4" if how == "error" else "2.3"}, "method": "POST", "path": STREAM_ROUTE, "headers": []}
            with anyio.fail_after(20), anyio.CancelScope() as around:
                request = asyncio.get_running_loop().create_task(response(scope, receive, send)) if how == "task" else response(scope, receive, send)
                try:
                    await request
                except ClientDisconnect:
                    self.assertEqual(how, "error")
                except asyncio.CancelledError:
                    if how != "task":
                        raise
            self.assertEqual(around.cancelled_caught, how == "scope")

        asyncio.run(serve())

    def test_a_run_that_had_ended_keeps_its_end_and_one_that_was_left_counts_also_when_it_never_began(self):
        """Covers: AC-daily-limit-015."""
        self.switch("99")
        user = self.store.get_user(self.own_account("ada")["id"])
        request = SimpleNamespace(method="POST", url=SimpleNamespace(path=STREAM_ROUTE), headers={}, client=None)

        def ending(line: str, error: Optional[str]) -> Callable[..., Iterator[str]]:
            def events(definition: str, calls: Optional[CallLog] = None, **_: Any) -> Iterator[str]:
                start_call(calls, "decomposition", "psnc", "m").finish("ok" if error is None else "error", error=error)
                yield stream_event("raw_delta", delta="{")
                yield line
                raise AssertionError("a line too many")
            return events

        ends = (
            (stream_event("final", data=dict(PIPELINE_RESULT)), None, ("success", 0)),
            (stream_event("error", detail="no answer"), "HTTP 503", ("failed", 0)),
            (stream_event("error", detail="no answer"), "HTTP 401", ("refused", 1)),
        )
        for how in ("message", "error", "scope", "task"):
            for line, error, end in ends:
                for lines, outcome in ((0, ("client_left", 0)), (1, ("client_left", 0)), (2, end)):
                    with self.subTest(how=how, line=line[:20], error=error, lines=lines), \
                            patch.object(decompose_router, "stream_pipeline_events", side_effect=ending(line, error)), \
                            patch.object(decompose_router, "_record_run", return_value=None), patch.object(self.store, "audit_event"):
                        response = decompose_router.decompose_stream(decompose_router.DecomposeRequest(definition="air"), request, user)  # type: ignore[arg-type]
                        self.assertEqual(self.ends()[-1], (None, 0))
                        self.break_off(response, lines, how)
                        if how in ("scope", "task") and outcome == ("client_left", 0):
                            self.assertEqual(self.ends()[-1], (None, 0))  # interrupted, as the next start finds
                            outcome = ("interrupted", 1)
                        else:
                            self.assertEqual(self.ends()[-1], outcome)
                        admission.release_interrupted()
                        self.assertEqual(self.ends()[-1], outcome)
                        del response
                        gc.collect()  # ends what is left of the run, which changes nothing any more
                        self.assertEqual(self.ends()[-1], outcome)

    def test_a_stop_right_after_a_browser_left_changes_nothing(self):
        """Covers: AC-daily-limit-015."""
        self.switch("1")
        user = self.store.get_user(self.own_account("ada")["id"])
        request = SimpleNamespace(method="POST", url=SimpleNamespace(path=STREAM_ROUTE), headers={}, client=None)
        original = decompose_router.run_in_threadpool

        async def stopped_first(function: Callable[[], None]) -> None:
            stop[0].cancel()
            await original(function)

        async def serve() -> None:
            async def receive() -> Dict[str, Any]:
                return {"type": "http.disconnect"}

            async def send(_: Dict[str, Any]) -> None:
                await anyio.sleep_forever()

            with anyio.CancelScope() as scope:
                stop.append(scope)
                await response({"type": "http", "asgi": {"spec_version": "2.3"}, "method": "POST", "path": STREAM_ROUTE, "headers": []}, receive, send)
                await anyio.lowlevel.checkpoint()
            self.assertTrue(scope.cancelled_caught)

        stop: List[anyio.CancelScope] = []
        response = decompose_router.decompose_stream(decompose_router.DecomposeRequest(definition="air"), request, user)  # type: ignore[arg-type]
        with patch.object(decompose_router, "run_in_threadpool", side_effect=stopped_first):
            asyncio.run(serve())
        self.assertEqual(self.ends(), [("client_left", 0)])

    def test_a_stream_that_breaks_off_by_itself_counts_by_its_calls(self):
        """Covers: AC-daily-limit-010.

        A run that raises, or ends without a last line, has ended as its calls say.
        """
        self.switch("99")
        ada = self.person()
        browser = self.browser(raise_server_exceptions=False)
        browser.cookies.set(ROHUB_COOKIE, ada.cookies[ROHUB_COOKIE])
        headers = self.csrf_headers(ada)
        for error, raised, end in (("HTTP 503", True, ("failed", 0)), ("HTTP 503", False, ("failed", 0)), ("HTTP 404", True, ("refused", 1)),
                                   ("HTTP 404", False, ("refused", 1)), (None, True, ("not_sent", 1)), (None, False, ("not_sent", 1))):
            def events(definition: str, calls: Optional[CallLog] = None, error: Optional[str] = error, raised: bool = raised, **_: Any) -> Iterator[str]:
                if error:
                    start_call(calls, "decomposition", "psnc", "m").finish("error", error=error)
                yield stream_event("raw_delta", delta="{")
                if raised:
                    raise RuntimeError("broken")

            with self.subTest(error=error, raised=raised), patch.object(decompose_router, "stream_pipeline_events", side_effect=events):
                browser.post(STREAM_ROUTE, json=DEFINITION, headers=headers)
                self.assertEqual(self.ends()[-1], end)


# ------------------------------------------------------------------------------------------------
# The migration
# ------------------------------------------------------------------------------------------------


class MigrationTests(OidcTestCase):
    def migration(self) -> storage.Migration:
        return next(item for item in storage.MIGRATIONS if item.id == MIGRATION)

    def test_its_text_is_settled(self):
        """Covers: AC-daily-limit-016.

        A database that has run it refuses to start with another text (app-storage, section 4).
        """
        migration = self.migration()
        self.assertEqual((migration.kind, migration.checksum), ("sql", MIGRATION_SHA256))
        self.assertEqual(hashlib.sha256(migration.sql.encode("utf-8")).hexdigest(), MIGRATION_SHA256)
        self.assertEqual([item.id for item in storage.MIGRATIONS][:5], [
            "0001_baseline", "0002_incremental_vacuum", "0003_accounts_roles", "0004_oidc_identity", MIGRATION])

    def test_it_adds_one_table_and_the_previous_image_still_runs(self):
        """Covers: AC-daily-limit-016."""
        old = self.dir / "de1a7af.sqlite3"
        previous_image.make_de1a7af_file(old)
        position = [item.id for item in storage.MIGRATIONS].index(MIGRATION)
        migration = storage.MIGRATIONS[position]
        del storage.MIGRATIONS[position:]
        storage.migrate(old)
        schema = {row["name"]: row["sql"] for row in self.rows("SELECT name, sql FROM sqlite_master", db=old)}
        tables = [row["name"] for row in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'", db=old)]
        before = {table: self.rows(f"SELECT rowid, * FROM {table}", db=old) for table in tables if table not in ("schema_migrations", "sqlite_sequence")}
        storage.MIGRATIONS.append(migration)
        self.assertEqual(storage.migrate(old), [MIGRATION])
        after = {row["name"]: row["sql"] for row in self.rows("SELECT name, sql FROM sqlite_master", db=old)}
        self.assertEqual(set(after) - set(schema), {"decomposition_uses", "idx_decomposition_uses_account_day"})
        self.assertEqual({name: after[name] for name in schema}, schema)
        self.assertEqual({table: self.rows(f"SELECT rowid, * FROM {table}", db=old) for table in before}, before)
        self.assertEqual(self.rows("SELECT * FROM decomposition_uses", db=old), [])
        self.assertEqual(previous_image.start(old), [])
        self.assertEqual(previous_image.replay(old, include=previous_image.is_read), [])

    def test_a_use_belongs_to_an_account_and_goes_with_it(self):
        """Covers: AC-daily-limit-016."""
        account = AccountStore(self.store).ensure_account(ISSUER, "subject-ada", {"email": "ada@example.org", "email_verified": True})[0]
        with storage.transaction(path=self.store.db_path) as conn:
            usage.add_use(conn, account["id"], usage.day_of(utc("2026-11-25T12:00:00+01:00")), utc("2026-11-25T12:00:00+01:00"))
        with self.assertRaises(sqlite3.IntegrityError), storage.transaction(path=self.store.db_path) as conn:
            usage.add_use(conn, 10_000, usage.day_of(utc("2026-11-25T12:00:00+01:00")), utc("2026-11-25T12:00:00+01:00"))
        self.assertEqual([row["user_id"] for row in self.rows("SELECT * FROM decomposition_uses")], [account["id"]])
        self.execute("DELETE FROM account_identities WHERE user_id = ?", account["id"])
        self.execute("DELETE FROM users WHERE id = ?", account["id"])
        self.assertEqual(self.rows("SELECT * FROM decomposition_uses"), [])


if __name__ == "__main__":
    unittest.main()
