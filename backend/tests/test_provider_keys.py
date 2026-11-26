"""A provider's key that no request can carry is refused before any request, and no text ever shows a key.

A line break at the end of a key, as a hand-made secret often has one, made the HTTP libraries refuse the
request with an error that quotes its header. That text went to the browser, the record, the audit log and the
log. The first half tests the refusal. The second half takes the refusal away, or lets the provider quote the
key it refuses, and looks for the key wherever an error text goes.

Nothing here leaves this machine: both providers are one small server on the loopback address. Every key is
made up, of two halves that occur in no other text.
"""

from __future__ import annotations

import io
import json
import logging
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import zipfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

import httpx
import openai
import requests
from fastapi.testclient import TestClient
from increment1_support import TempDatabaseTestCase

from app import main, pipeline
from app.clients import openai_client, psnc_client
from app.clients.http import get_http_session
from app.core import provider_keys
from app.core.config import settings
from app.core.logging import AccessKeysFilter, KeysFilter
from app.core.provider_keys import REDACTED, without_keys
from app.core.state import app_state
from app.services import entity_linking, label_comment, llm
from app.services.call_log import MAX_ERROR_CHARS, CallLog
from app.services.entity_linking import Linker
from app.services.vocabularies import Candidate

BACKEND = pathlib.Path(__file__).resolve().parents[1]
JSON_ROUTE, STREAM_ROUTE = ROUTES = ("/api/decompose", "/api/decompose/stream")
DEFINITION = {"definition": "air temperature"}
SETTINGS = {"psnc": "PSNC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
MODELS = {"psnc": "Qwen3.8-27B", "openrouter": "qwen/qwen3-32b"}
MESSAGES = {
    provider: f"{setting} holds a character that cannot be sent in a request header, such as a line break at its end."
    for provider, setting in SETTINGS.items()
}

FIRST, SECOND = HALVES = ("sk-Quokka71x", "Wombat52yAardvark")
GOOD_KEY = FIRST + SECOND

QUOTED_BY_REQUESTS = {
    "a line break at the end": f"{GOOD_KEY}\n",
    "a line break inside": f"{FIRST}\n{SECOND}",
    "a line break at the start": f"\n{GOOD_KEY}",
    "a folded line": f"{FIRST}\n {SECOND}",
    "CR LF at the end": f"{GOOD_KEY}\r\n",
    "CR LF inside": f"{FIRST}\r\n{SECOND}",
    "a carriage return at the end": f"{GOOD_KEY}\r",
}
"""Keys that ``requests`` refuses with an error that quotes the header."""

NOT_SENDABLE = {
    **QUOTED_BY_REQUESTS,
    "a character that is not ASCII": f"{FIRST}é{SECOND}",
    "a character beyond Latin-1": f"{FIRST}κ{SECOND}",
    "a non-breaking space at the end": f"{GOOD_KEY} ",
    "a byte order mark": f"﻿{GOOD_KEY}",
    "a byte that is no text": f"{FIRST}\udcff{SECOND}",
    "a zero byte": f"{FIRST}\0{SECOND}",
    "a vertical tab": f"{FIRST}\x0b{SECOND}",
    "a form feed at the end": f"{GOOD_KEY}\x0c",
    "an escape character": f"{FIRST}\x1b{SECOND}",
    "a delete character": f"{FIRST}\x7f{SECOND}",
}
"""Keys that hold a character that cannot stand in a header, whatever the provider."""

NOT_SENDABLE_BY_SDK = {"a blank at the end": f"{GOOD_KEY} ", "a tab at the end": f"{GOOD_KEY}\t"}
"""Keys that only OpenRouter's client cannot send."""

REFUSED = {"psnc": NOT_SENDABLE, "openrouter": {**NOT_SENDABLE, **NOT_SENDABLE_BY_SDK}}
CASES = [(provider, name, key) for provider, keys in REFUSED.items() for name, key in keys.items()]

ANSWER = json.dumps({
    "hasProperty": "temperature", "hasObjectOfInterest": "air", "hasMatrix": "", "hasContextObject": "",
    "hasConstraint": [], "hasStatisticalModifier": "",
})
CONNECTIONS = httpx.Client()
"""What the OpenAI client sends its requests with; one for all tests, because a new one is slow to make."""

CANDIDATES = [
    Candidate("wikidata", "http://www.wikidata.org/entity/Q3230", "Q3230", "air", "mixture of gases"),
    Candidate("wikidata", "http://www.wikidata.org/entity/Q1", "Q1", "AIR", "software"),
]


class Provider:
    """Both providers: a server on this machine that refuses every request and quotes the key, as LiteLLM does."""

    def __init__(self) -> None:
        self.keys: List[str] = []
        self.refusal: Callable[[str], str] = lambda key: json.dumps(
            {"error": {"message": f"Malformed API Key passed in. Ensure Key has `Bearer ` prefix. Passed in: {key}", "code": 401}}
        )
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                key = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                provider.keys.append(key)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = provider.refusal(key).encode("utf-8")
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@contextmanager
def captured(*, as_configured: bool) -> Iterator[io.StringIO]:
    """Collect everything that is printed or logged, at the most talkative level.

    With ``as_configured`` the log goes through the filter the application puts on its handlers; without, this
    is everything that reaches the logging library.
    """
    out, root = io.StringIO(), logging.getLogger()
    handler = logging.StreamHandler(out)
    if as_configured:
        handler.addFilter(KeysFilter())
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        with redirect_stdout(out), redirect_stderr(out):
            yield out
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


class KeyTestCase(unittest.TestCase):
    """Both providers on this machine, and neither key set."""

    def setUp(self) -> None:
        self.provider = Provider()
        self.addCleanup(self.provider.close)
        for target, name, value in (
            (settings, "enabled_model_providers_raw", "psnc,openrouter"), (settings, "default_model_provider_raw", "psnc"),
            (settings, "psnc_model_name", MODELS["psnc"]), (settings, "psnc_model_names_raw", MODELS["psnc"]),
            (settings, "model_name", MODELS["openrouter"]), (settings, "model_names_raw", ""),
            (settings, "use_measured_configuration", True), (settings, "enable_wikidata_linking", False),
            (settings, "psnc_api_key", None), (settings, "openrouter_api_key", None),
            (settings, "psnc_api_base_url", self.provider.url), (openai_client, "OPENROUTER_BASE_URL", self.provider.url),
            (openai_client, "_openai_client", None), (openai_client, "OpenAI", partial(openai.OpenAI, max_retries=0, http_client=CONNECTIONS)),
            (entity_linking, "_sleep", lambda _: None), (entity_linking, "retrieve_pool", lambda key: (CANDIDATES, ())),
        ):
            self.replace(target, name, value)
        env = patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        env.start()
        self.addCleanup(env.stop)

    def replace(self, target: Any, name: str, value: Any) -> None:
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    @contextmanager
    def key(self, provider: str, key: Optional[str]) -> Iterator[None]:
        """Set a provider's key; a client made with another one is forgotten."""
        with patch.object(settings, SETTINGS[provider].lower(), key), patch.object(openai_client, "_openai_client", None):
            yield

    def assert_nowhere(self, *texts: str) -> None:
        for text in texts:
            for half in HALVES:
                at = text.find(half)
                self.assertEqual(at, -1, f"half a key is shown: {text[max(0, at - 100):at + 100]!r}")


class RunTestCase(TempDatabaseTestCase, KeyTestCase):
    """A signed-in browser and the real pipeline."""

    def setUp(self) -> None:
        TempDatabaseTestCase.setUp(self)
        KeyTestCase.setUp(self)
        self.store.init()
        self.records.init()
        self.store.create_user(username="bob", password="password-123")
        self.browser, self.admin = self.client(), self.client()
        self.login(self.browser, "bob")
        self.login(self.admin, "admin", "admin-password")

    def post(self, provider: str, route: str) -> Any:
        return self.browser.post(route, json={**DEFINITION, "model_provider": provider})

    def stored(self) -> str:
        """Everything the database holds, its journal and what an admin sees of it included."""
        conn = sqlite3.connect(self.db)
        try:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            rows = [str(row) for table in tables for row in conn.execute(f"SELECT * FROM {table}")]
        finally:
            conn.close()
        files = [path.read_bytes().decode("latin-1") for path in sorted(self.dir.rglob("*")) if path.is_file()]
        activity = self.admin.get("/api/admin/audit", params={"limit": 500})
        export = self.admin.get("/api/admin/records/export")
        self.assertEqual((activity.status_code, export.status_code), (200, 200))
        with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
            exported = [archive.read(name).decode("utf-8") for name in archive.namelist()]
        return "\n".join([*rows, *files, activity.text, *exported])

    def last(self, table: str) -> Dict[str, Any]:
        return self.rows(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1")[0]

    def calls(self) -> List[Dict[str, Any]]:
        return self.rows("SELECT * FROM record_llm_calls ORDER BY id")

    @contextmanager
    def decomposed(self, *, streamed: bool = True) -> Iterator[None]:
        """Let the decomposition itself succeed; the label call and the linking question are the real ones.

        When it is not ``streamed``, the stream of the streaming route fails and its fallback has the answer.
        """
        prediction = llm.parse_llm_json(ANSWER, DEFINITION["definition"])
        delta = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=ANSWER))], usage=None)

        def stream(*_: Any, **__: Any) -> Iterator[Tuple[str, str]]:
            if not streamed:
                raise RuntimeError("no stream today")
            return iter([("", ANSWER)])

        def create(**_: Any) -> Iterator[Any]:
            if not streamed:
                raise RuntimeError("no stream today")
            return iter([delta])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(pipeline, "call_llm_loose", return_value=(ANSWER, prediction)), patch.object(pipeline, "stream_psnc_model", stream), \
                patch.object(pipeline, "get_openai_client", return_value=client), patch.object(pipeline, "call_model", return_value=ANSWER), \
                patch.object(pipeline, "call_psnc_model", return_value=ANSWER), patch.object(settings, "enable_wikidata_linking", True):
            yield

    def final(self, response: Any) -> Dict[str, Any]:
        self.assertEqual(response.status_code, 200, response.text)
        last = json.loads(response.text.splitlines()[-1])
        return last["data"] if last.get("type") == "final" else last


# ------------------------------------------------------------------------------------------------
# Refused before any request
# ------------------------------------------------------------------------------------------------


class UsableKeyTests(KeyTestCase):
    def uses(self, provider: str) -> Dict[str, Callable[[], str]]:
        """Every place that hands a provider's key on, as a function that returns the key it hands on."""
        if provider == "psnc":
            return {"headers": lambda: psnc_client.psnc_chat_headers()["Authorization"].removeprefix("Bearer ")}
        return {
            "client": lambda: openai_client.get_openai_client().api_key,
            "headers": lambda: openai_client.openrouter_chat_headers()["Authorization"].removeprefix("Bearer "),
        }

    def test_a_key_that_cannot_be_sent_is_refused_with_one_message_that_holds_none_of_it(self):
        """Covers: AC-provider-keys-001."""
        for provider, name, key in CASES:
            for place, use in self.uses(provider).items():
                with self.subTest(name, provider=provider, place=place), self.key(provider, key):
                    with self.assertRaises(RuntimeError) as refused:
                        use()
                    self.assertEqual(str(refused.exception), MESSAGES[provider])
                    self.assert_nowhere(str(refused.exception), repr(refused.exception))
                    self.assertIsNone(openai_client._openai_client)

    def test_a_key_that_is_only_a_line_break_is_refused_too(self):
        """Covers: AC-provider-keys-001."""
        for provider in SETTINGS:
            for key in ("\n", "\r\n", "\n\n", " \n"):
                for place, use in self.uses(provider).items():
                    with self.subTest(provider=provider, key=key, place=place), self.key(provider, key):
                        with self.assertRaisesRegex(RuntimeError, f"^{MESSAGES[provider]}$"):
                            use()

    def test_a_missing_key_is_still_called_missing(self):
        """Covers: AC-provider-keys-001."""
        for provider, setting in SETTINGS.items():
            for key in (None, ""):
                for place, use in self.uses(provider).items():
                    with self.subTest(provider=provider, key=key, place=place), self.key(provider, key):
                        with self.assertRaisesRegex(RuntimeError, rf"^{setting} is not set\.$"):
                            use()

    def test_any_other_key_is_handed_on_as_it_is(self):
        """Covers: AC-provider-keys-002.

        White space is not trimmed: a blank around PSNC's key is sent today, so it is sent after this too.
        """
        visible = "".join(map(chr, range(0x21, 0x7F)))
        keys = [GOOD_KEY, "x", visible, f"{FIRST} {SECOND}", f"{FIRST}\t{SECOND}", f" {GOOD_KEY}", f"\t{GOOD_KEY}"]
        for provider in SETTINGS:
            for key in keys + (list(NOT_SENDABLE_BY_SDK.values()) if provider == "psnc" else []):
                for place, use in self.uses(provider).items():
                    with self.subTest(provider=provider, key=key, place=place), self.key(provider, key):
                        self.assertEqual(use(), key)

    def test_what_is_not_refused_is_what_the_http_clients_send(self):
        """Covers: AC-provider-keys-002.

        A key that the check lets through never fails on the way out, for any character at any place.
        """
        session = get_http_session()

        def by_session(url: str, headers: Dict[str, str]) -> None:
            session.post(url, headers=headers, json={}, timeout=10)

        def by_client() -> None:
            with self.assertRaises(openai.AuthenticationError):  # the provider has read the request and refused it
                openai_client.get_openai_client().chat.completions.create(model="m", messages=[], max_tokens=1)

        senders = {
            "psnc": [lambda: by_session(psnc_client.psnc_chat_completions_url(), psnc_client.psnc_chat_headers())],
            "openrouter": [
                by_client,
                lambda: by_session(openai_client.openrouter_chat_completions_url(), openai_client.openrouter_chat_headers()),
            ],
        }
        sent, refused = {"psnc": 0, "openrouter": 0}, {"psnc": 0, "openrouter": 0}
        for provider, ways in senders.items():
            for code in (*range(0x100), 0x3BA, 0x2028, 0xFEFF, 0x1F511, 0xDCFF):
                for key in (chr(code) + FIRST, FIRST + chr(code) + SECOND, FIRST + chr(code)):
                    for way in ways:
                        with self.key(provider, key):
                            self.provider.keys.clear()
                            try:
                                way()
                            except RuntimeError as e:
                                self.assertEqual((str(e), self.provider.keys), (MESSAGES[provider], []))
                                refused[provider] += 1
                            else:
                                self.assertEqual(self.provider.keys, [key])
                                sent[provider] += 1
        # 94 visible characters and a blank and a tab, at three places; at the end only PSNC's are sent with the last two.
        self.assertEqual(sent, {"psnc": 96 * 3, "openrouter": (96 * 3 - 2) * 2})
        self.assertEqual(refused, {"psnc": (261 - 96) * 3, "openrouter": ((261 - 96) * 3 + 2) * 2})


class RefusedRunTests(RunTestCase):
    def test_a_decomposition_ends_before_any_request_and_nothing_shows_the_key(self):
        """Covers: AC-provider-keys-003."""
        for provider, name, key in CASES:
            for route in ROUTES:
                with self.subTest(name, provider=provider, route=route), self.key(provider, key):
                    with captured(as_configured=False) as out:
                        response = self.post(provider, route)
                    if route == JSON_ROUTE:
                        self.assertEqual((response.status_code, response.json()), (500, {"detail": MESSAGES[provider]}))
                    else:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(json.loads(response.text), {"type": "error", "detail": MESSAGES[provider]})
                    self.assert_nowhere(response.text, str(response.headers), out.getvalue())
                    run, audit = self.last("record_runs"), self.last("audit_events")
                    self.assertEqual((run["status"], run["error"], run["provider"]), ("error", MESSAGES[provider], provider))
                    self.assertEqual((audit["status_code"], audit["error"]), (500, MESSAGES[provider]))
        self.assertEqual((self.provider.keys, self.calls()), ([], []))
        self.assertEqual(len(self.rows("SELECT * FROM record_runs")), len(CASES) * len(ROUTES))
        self.assert_nowhere(self.stored())

    def test_it_ends_as_a_run_without_a_key_ends(self):
        """Covers: AC-provider-keys-003.

        The same lines, the same status and the same rows, apart from the message.
        """
        def outcome(provider: str, route: str, key: Optional[str]) -> Tuple[Any, ...]:
            with self.key(provider, key), captured(as_configured=False) as out:
                response = self.post(provider, route)
            run, audit = self.last("record_runs"), self.last("audit_events")
            texts = [response.text, run["error"], audit["error"], *(line for line in out.getvalue().splitlines() if "attempt" in line)]
            return (response.status_code, run["status"], audit["status_code"], audit["action"], self.calls()), texts

        for provider, setting in SETTINGS.items():
            for route in ROUTES:
                with self.subTest(provider=provider, route=route):
                    (missing, without), (malformed, texts) = outcome(provider, route, None), outcome(provider, route, f"{GOOD_KEY}\n")
                    self.assertEqual(malformed, missing)
                    self.assertEqual(texts, [text.replace(f"{setting} is not set.", MESSAGES[provider]) for text in without])
                    self.assertTrue(all(f"{setting} is not set." in text for text in without))

    def test_the_label_call_and_the_linking_question_are_not_sent(self):
        """Covers: AC-provider-keys-003.

        The decomposition came back, so the run succeeds, with the fallback label and without links.
        """
        for provider, name, key in CASES:
            for route in ROUTES:
                with self.subTest(name, provider=provider, route=route), self.key(provider, key), self.decomposed():
                    before = len(self.calls())
                    with captured(as_configured=False) as out:
                        response = self.post(provider, route)
                    result = self.final(response)
                    self.assertFalse([field for field in result["enriched_json"] if "URI" in field])
                    self.assertIn(f"Entity linking failed: {MESSAGES[provider]}", out.getvalue())
                    if provider == "psnc":  # on OpenRouter the decomposition brings its own label
                        self.assertEqual(result["parsed_json"]["label"], "air temperature")
                        self.assertIn(f"Label/comment generation failed: {MESSAGES[provider]}", out.getvalue())
                    self.assertEqual({call["purpose"] for call in self.calls()[before:]}, {"decomposition"} if route == STREAM_ROUTE else set())
                    self.assert_nowhere(response.text, out.getvalue())
        self.assertEqual(self.provider.keys, [])
        self.assert_nowhere(self.stored())

    def test_the_label_call_and_the_linking_question_are_refused_on_their_own(self):
        """Covers: AC-provider-keys-003."""
        for provider, name, key in CASES:
            with self.subTest(name, provider=provider), self.key(provider, key):
                log = CallLog()
                with captured(as_configured=False) as out:
                    label = label_comment.generate_label_and_comment(
                        "air temperature", model_provider=provider, model=MODELS[provider], temperature=0.5, calls=log
                    )
                    with self.assertRaises(RuntimeError) as refused:
                        entity_linking.ask_for_choice("air", "air temperature", CANDIDATES, Linker(provider, MODELS[provider], calls=log))
                self.assertEqual(label, ("air temperature", "air temperature"))
                self.assertEqual(out.getvalue(), f"Label/comment generation failed: {MESSAGES[provider]}\n")
                self.assertEqual(str(refused.exception), MESSAGES[provider])
                self.assertEqual(log.calls(), [])
        self.assertEqual(self.provider.keys, [])

    def test_the_service_starts_with_such_a_key_and_the_other_provider_works(self):
        """Covers: AC-provider-keys-004.

        A malformed key of one provider must not take the service down; a missing one does not either.
        """
        caches = dict(vars(app_state))
        self.addCleanup(lambda: vars(app_state).update(caches))
        with self.key("psnc", GOOD_KEY), self.key("openrouter", GOOD_KEY), TestClient(main.app) as client:
            ready = client.get("/api/readyz")
        self.assertEqual(ready.status_code, 200)
        for provider, name, key in CASES:
            other = next(one for one in SETTINGS if one != provider)
            with self.subTest(name, provider=provider), self.key(other, GOOD_KEY), self.key(provider, key):
                with captured(as_configured=False) as out, TestClient(main.app) as client:
                    self.assertEqual(client.get("/api/livez").status_code, 200)
                    self.assertEqual((client.get("/api/readyz").status_code, client.get("/api/readyz").json()), (200, ready.json()))
                self.assert_nowhere(out.getvalue())
                self.assertEqual(MESSAGES["openrouter"] in out.getvalue(), provider == "openrouter")
                self.assertEqual(openai_client._openai_client is None, provider == "openrouter")
                self.provider.keys.clear()
                self.post(other, JSON_ROUTE)
                self.assertEqual(set(self.provider.keys), {GOOD_KEY})

    def test_the_start_says_why_openrouter_cannot_be_used_without_the_key(self):
        """Covers: AC-provider-keys-004.

        The refusal holds no key; should the client's own error ever be told here, it would.
        """
        caches = dict(vars(app_state))
        self.addCleanup(lambda: vars(app_state).update(caches))
        failure = RuntimeError(f"Invalid header value {f'Bearer {GOOD_KEY}'.encode()!r}")
        with self.key("psnc", GOOD_KEY), self.key("openrouter", GOOD_KEY), patch.object(pipeline, "get_openai_client", side_effect=failure), \
                captured(as_configured=False) as out, TestClient(main.app) as client:
            self.assertEqual(client.get("/api/livez").status_code, 200)
        self.assertIn(f"OpenRouter cannot be used: Invalid header value b'Bearer {REDACTED}'", out.getvalue())
        self.assert_nowhere(out.getvalue())


# ------------------------------------------------------------------------------------------------
# Nowhere in a text
# ------------------------------------------------------------------------------------------------


class SecondLineTests(RunTestCase):
    """The refusal is taken away, so that a malformed key reaches the HTTP clients, as it did before."""

    def setUp(self) -> None:
        super().setUp()
        for module in (psnc_client, openai_client):
            self.replace(module, "usable_key", lambda setting, key, *_: key)

    def run_one(self, provider: str, route: str, key: str) -> Tuple[Any, str]:
        with self.key(provider, key), captured(as_configured=True) as out:
            response = self.post(provider, route)
        return response, out.getvalue()

    def test_what_the_http_clients_say_of_a_decomposition_shows_the_key_nowhere(self):
        """Covers: AC-provider-keys-005."""
        for provider, name, key in CASES:
            for route in ROUTES:
                with self.subTest(name, provider=provider, route=route):
                    before = len(self.calls())
                    response, out = self.run_one(provider, route, key)
                    self.assert_nowhere(response.text, str(response.headers), out)
                    self.assertGreater(len(self.calls()), before, "the request was not even tried")
                    if provider == "psnc" and name in QUOTED_BY_REQUESTS:
                        texts = [self.last("record_llm_calls")["error"], out]
                        if route == STREAM_ROUTE:
                            texts += [response.text, self.last("record_runs")["error"], self.last("audit_events")["error"]]
                        for text in texts:
                            self.assertIn(f"Bearer {REDACTED}", text)
                    if provider == "openrouter" and (name in QUOTED_BY_REQUESTS or name in NOT_SENDABLE_BY_SDK):
                        self.assertIn(f"Illegal header value b'Bearer {REDACTED}'", out)  # the client's own log
        self.assert_nowhere(self.stored())

    def test_what_they_say_of_the_label_call_and_the_linking_question_shows_the_key_nowhere(self):
        """Covers: AC-provider-keys-005."""
        for provider, name, key in CASES:
            for route in ROUTES:
                with self.subTest(name, provider=provider, route=route), self.decomposed():
                    response, out = self.run_one(provider, route, key)
                    self.final(response)
                    self.assert_nowhere(response.text, out)
                    if name == "a line break at the end":
                        self.assertIn(f"Entity linking failed: Invalid header value b'Bearer {REDACTED}'", out)
                        if provider == "psnc":
                            self.assertIn(f"Bearer {REDACTED}", [call["error"] for call in self.calls() if call["purpose"] == "label"][-1])
        self.assert_nowhere(self.stored())

    def test_a_label_call_of_its_own_shows_the_key_nowhere(self):
        """Covers: AC-provider-keys-005."""
        for provider, name, key in CASES:
            with self.subTest(name, provider=provider), self.key(provider, key), captured(as_configured=True) as out:
                log = CallLog()
                label_comment.generate_label_and_comment(
                    "air temperature", model_provider=provider, model=MODELS[provider], temperature=0.5, calls=log
                )
            self.assert_nowhere(out.getvalue(), str(log.calls()))
            self.assertEqual(len(log.calls()), 3)

    def test_a_label_call_that_fails_in_another_way_is_told_without_the_key(self):
        """Covers: AC-provider-keys-005."""
        quoted = f"Invalid header value {f'Bearer {GOOD_KEY}'.encode()!r}"
        for provider, call in (("psnc", "call_psnc_model"), ("openrouter", "call_model")):
            with self.subTest(provider), self.key(provider, GOOD_KEY), patch.object(label_comment, call, side_effect=ValueError(quoted)), \
                    captured(as_configured=True) as out:
                label_comment.generate_label_and_comment("air temperature", model_provider=provider, model=MODELS[provider], temperature=0.5)
            self.assert_nowhere(out.getvalue())
            self.assertIn(f"Label/comment generation failed: Invalid header value b'Bearer {REDACTED}'", out.getvalue())

    def test_an_error_that_leaves_the_services_is_shown_without_the_key(self):
        """Covers: AC-provider-keys-005.

        Whatever a later step raises: the last texts are made in the pipeline and in the routes.
        """
        quoted = f"Invalid header value {f'Bearer {GOOD_KEY}'.encode()!r}"
        for kind in (ValueError, RuntimeError, KeyError, UnicodeError):
            for provider in SETTINGS:
                for route in ROUTES:
                    for place in ("_prepare_pipeline_inputs", "_finalize_pipeline_output", "_labelled_prediction"):
                        for streamed in (True, False):
                            with self.subTest(kind=kind.__name__, provider=provider, route=route, place=place, streamed=streamed), \
                                    self.decomposed(streamed=streamed), patch.object(pipeline, place, side_effect=kind(quoted)):
                                response, out = self.run_one(provider, route, GOOD_KEY)
                                self.assert_nowhere(response.text, out)
                                self.assertIn(f"Bearer {REDACTED}", response.text)
                                self.assertIn(f"Bearer {REDACTED}", self.last("audit_events")["error"])
        self.assert_nowhere(self.stored())
        self.assertIn(f"Bearer {REDACTED}", str(self.rows("SELECT error FROM record_runs")))

    def test_a_stream_that_breaks_in_the_route_is_recorded_without_the_key(self):
        """Covers: AC-provider-keys-005."""
        def broken(*_: Any, **__: Any) -> Iterator[str]:
            yield llm.stream_event("raw_delta", delta="{")
            raise ValueError(f"Invalid header value {GOOD_KEY!r}")

        with self.key("psnc", GOOD_KEY), patch("app.routers.decompose.stream_pipeline_events", broken), captured(as_configured=True) as out:
            with self.assertRaises(ValueError):  # the test client raises what the server would log
                self.post("psnc", STREAM_ROUTE)
        self.assertEqual(self.last("audit_events")["error"], f"Invalid header value '{REDACTED}'")
        self.assertEqual(self.last("record_runs")["error"], f"Invalid header value '{REDACTED}'")
        self.assert_nowhere(out.getvalue(), self.stored())

    def test_an_answer_that_is_no_json_is_reported_without_the_key(self):
        """Covers: AC-provider-keys-005.

        The parser quotes no answer today; if it did, the same rule holds for what it says.
        """
        failure = ValueError(f"JSON decode failure near {GOOD_KEY}")
        delta = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="not JSON"))], usage=None)
        whole = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="not JSON"))], usage=None)
        for streamed in (True, False):
            def create(stream: bool = False, streamed: bool = streamed, **_: Any) -> Any:
                if stream and not streamed:
                    raise RuntimeError("no stream today")
                return iter([delta]) if stream else whole

            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            for route in ROUTES:
                with self.subTest(route=route, streamed=streamed), patch.object(pipeline, "parse_llm_json", side_effect=failure), \
                        patch.object(llm, "parse_llm_json", side_effect=failure), patch.object(llm, "get_openai_client", return_value=client), \
                        patch.object(pipeline, "get_openai_client", return_value=client):
                    before = len(self.calls())
                    response, out = self.run_one("openrouter", route, GOOD_KEY)
                    self.assert_nowhere(response.text, out)
                    self.assertIn(f"failed: JSON decode failure near {REDACTED}", out)
                    self.assertIn(f"Not valid JSON: JSON decode failure near {REDACTED}", {call["error"] for call in self.calls()[before:]})
                    if route == STREAM_ROUTE:
                        self.assertIn(f"Last error: JSON decode failure near {REDACTED}", response.text)
        self.assert_nowhere(self.stored())


class QuotedByTheProviderTests(RunTestCase):
    """A key that can be sent, and a provider that quotes it when it refuses it."""

    KEYS = {
        "an ordinary key": GOOD_KEY,
        "a blank inside": f"{FIRST} {SECOND}",
        "quotes and a backslash inside": f"{FIRST}\"'\\{SECOND}",
        "other punctuation": f"{FIRST}$^[(.*+?)]|{{}}%s%d{{0}}&<>/{SECOND}",
        "a blank at the start": f" {GOOD_KEY}",
    }
    SAVED_WITH_MORE = {
        "its own single quotes": f"'{GOOD_KEY}'",
        "its own double quotes": f'"{GOOD_KEY}"',
        "both": f"\"'{GOOD_KEY}'\"",
        "backticks": f"`{GOOD_KEY}`",
        "a line break written out": f"{GOOD_KEY}\\n",
        "a comma": f"{GOOD_KEY},",
    }
    """Keys as a hand-made secret holds them by mistake; a header can carry them all."""

    def run_one(self, provider: str, route: str, key: str) -> Tuple[Any, str]:
        self.provider.keys.clear()
        with self.key(provider, key), captured(as_configured=True) as out:
            response = self.post(provider, route)
        self.assertEqual(set(self.provider.keys), {key}, "the key went out as it is")
        return response, out.getvalue()

    def test_a_refusal_that_quotes_the_key_shows_it_nowhere(self):
        """Covers: AC-provider-keys-006."""
        for provider in SETTINGS:
            for name, key in self.KEYS.items():
                for route in ROUTES:
                    with self.subTest(name, provider=provider, route=route):
                        response, out = self.run_one(provider, route, key)
                        self.assert_nowhere(response.text, out)
                        self.assertIn(REDACTED, out)
                        if provider == "openrouter" and route == STREAM_ROUTE:
                            self.assertIn(REDACTED, response.text)
                            self.assertIn("Malformed API Key passed in. Ensure Key has `Bearer ` prefix. Passed in:", response.text)
        self.assert_nowhere(self.stored())
        self.assertEqual({call["error"] for call in self.calls()}, {"HTTP 401"})

    def test_a_key_that_was_saved_with_its_quotes_is_shown_nowhere_whatever_the_refusal_does_with_them(self):
        """Covers: AC-provider-keys-006."""
        for refused, refusal in {
            "beside other quotes": lambda key: json.dumps({"error": {"message": f"Invalid key {key} for model \"qwen/qwen3-32b\" of 'me'"}}),
            "without them": lambda key: json.dumps({"error": {"message": f"Invalid key {key.strip(chr(34) + chr(39) + '`,')} for a model"}}),
            "in an answer inside the answer": lambda key: json.dumps({"error": {"message": "401", "metadata": {"raw": json.dumps({"message": f"Invalid key {key}"})}}}),
        }.items():
            self.provider.refusal = refusal
            for provider in SETTINGS:
                for name, key in self.SAVED_WITH_MORE.items():
                    with self.subTest(name, refused=refused, provider=provider):
                        response, out = self.run_one(provider, STREAM_ROUTE, key)
                        self.assert_nowhere(response.text, out)
                        self.assertIn(REDACTED, out)
        self.assert_nowhere(self.stored())

    def test_no_first_part_of_a_key_stays_where_a_text_is_cut(self):
        """Covers: AC-provider-keys-006.

        The log shows 500 characters of a refusal, wherever in it the key begins.
        """
        for distance in (0, 470, 480, 490, 499, 500, 600):
            self.provider.refusal = lambda key, distance=distance: "." * distance + key + "!"
            with self.subTest(distance=distance):
                _, out = self.run_one("psnc", JSON_ROUTE, GOOD_KEY)
                printed = [line.rpartition("chat/completions ")[2] for line in out.splitlines() if line.startswith("PSNC HTTP error attempt")]
                self.assertEqual(len(printed), 9)
                self.assertEqual(set(printed), {("." * distance + REDACTED + "!")[:500]})

    def test_no_first_part_of_a_key_stays_where_the_record_is_cut(self):
        """Covers: AC-provider-keys-006.

        The call log keeps 300 characters of a reason, wherever in it the key begins.
        """
        for length in range(MAX_ERROR_CHARS - len(GOOD_KEY) - 30, MAX_ERROR_CHARS + 2):
            with self.subTest(length=length), self.key("psnc", GOOD_KEY):
                whole = f"RuntimeError: {'.' * length}{REDACTED}"
                shown = whole if len(whole) <= MAX_ERROR_CHARS else f"{whole[:MAX_ERROR_CHARS - 1]}…"
                self.assertEqual(llm.call_error(RuntimeError("." * length + GOOD_KEY)), shown)

    def test_every_error_of_a_request_is_told_without_the_key(self):
        """Covers: AC-provider-keys-006.

        Any exception the clients raise, in the call log and where it is printed.
        """
        request = openai_client.OPENROUTER_BASE_URL
        for provider, errors in {
            "psnc": (requests.ConnectionError(GOOD_KEY), requests.Timeout(GOOD_KEY), ValueError(GOOD_KEY), requests.HTTPError(GOOD_KEY)),
            "openrouter": (
                openai.APIConnectionError(message=GOOD_KEY, request=httpx.Request("POST", request)),
                openai.APIError(GOOD_KEY, request=httpx.Request("POST", request), body=None),
                httpx.ReadError(GOOD_KEY), ValueError(GOOD_KEY),
            ),
        }.items():
            for error in errors:
                for route in ROUTES:
                    def failing(*_: Any, error: Exception = error, **__: Any) -> Any:
                        raise error

                    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=failing)))
                    with self.subTest(provider=provider, error=type(error).__name__, route=route), self.key(provider, GOOD_KEY), \
                            patch.object(llm, "get_http_session", return_value=SimpleNamespace(post=failing)), \
                            patch.object(llm, "get_openai_client", return_value=client), \
                            patch.object(pipeline, "get_openai_client", return_value=client), captured(as_configured=False) as out:
                        response = self.post(provider, route)
                    self.assert_nowhere(response.text, out.getvalue())
                    self.assertIn(REDACTED, out.getvalue())
                    if route == STREAM_ROUTE:
                        self.assertIn(f"Last error: {REDACTED}", response.text)
                    if not isinstance(error, requests.HTTPError):
                        self.assertEqual(self.last("record_llm_calls")["error"], f"{type(error).__name__}: {REDACTED}")
        self.assert_nowhere(self.stored())


class WithoutKeysTests(KeyTestCase):
    def test_a_key_is_found_however_a_text_writes_it(self):
        """Covers: AC-provider-keys-007."""
        for name, key in {**NOT_SENDABLE, **NOT_SENDABLE_BY_SDK, **QuotedByTheProviderTests.KEYS}.items():
            writings = {
                "as it is": key, "in words": f"the key {key}, as it was", "on one line": " ".join(key.split()),
                "as Python shows it": repr(key), "as Python shows it in ASCII": ascii(key),
                "as Python shows its bytes": repr(key.encode("utf-8", "backslashreplace")),
                "as Python shows its Latin-1 bytes": repr(key.encode("latin-1", "backslashreplace")),
                "in a Python mapping": repr({"message": f"Passed in: {key}", "code": 401}),
                "in JSON": json.dumps({"message": key}), "in JSON, not escaped": json.dumps({"message": key}, ensure_ascii=False),
                "in another encoding": key.encode("utf-8", "replace").decode("latin-1"), "in an exception": ValueError(f"Passed in: {key}"),
            }
            for provider in SETTINGS:
                for writing, text in writings.items():
                    with self.subTest(name, provider=provider, writing=writing), self.key(provider, key):
                        shown = without_keys(text)
                        self.assert_nowhere(shown)
                        self.assertIn(REDACTED, shown)

    def test_each_way_to_write_a_key_is_found_by_itself(self):
        """Covers: AC-provider-keys-007.

        No part of these keys is long enough to be taken out for itself, so only the one form finds the key.
        """
        for name, key, text in (
            ("as it is", "Quk71\nWmb52", "the key Quk71\nWmb52, as it was"),
            ("on one line", "Quk71\nWmb52", "the key Quk71 Wmb52, as it was"),
            ("as Python shows it", "Quk71é\nWmb52", repr("Quk71é\nWmb52")),
            ("as Python shows it in ASCII", "Quk71\U0001f511\nWmb52", ascii("Quk71\U0001f511\nWmb52")),
            ("as JSON shows it", "Quk71\U0001f511\nWmb52", json.dumps("Quk71\U0001f511\nWmb52")),
            ("as Python shows its UTF-8 bytes", "Quk71é\nWmb52", repr("Quk71é\nWmb52".encode("utf-8"))),
            ("as Python shows its Latin-1 bytes", "Quk71κ\nWmb52", repr("Quk71κ\nWmb52".encode("latin-1", "backslashreplace"))),
            ("without the white space around it", "Quk71\nWmb52\n", repr("Quk71\nWmb52")),
            ("as it is, without the white space around it", " \nQuk71\nWmb52\n", "the key Quk71\nWmb52, as it was"),
            ("without the quotes it was saved with", "'Quk71xx.Wmb52_-y'", "the key Quk71xx.Wmb52_-y, as it was"),
            ("escaped once more than usual", "'Quk71xxWmb52'", "the key \\\\'Quk71xxWmb52\\\\', as it was"),
        ):
            for provider in SETTINGS:
                with self.subTest(name, provider=provider), self.key(provider, key):
                    shown = without_keys(text)
                    self.assertIn(REDACTED, shown)
                    for part in ("Quk71", "Wmb52"):
                        self.assertNotIn(part, shown)

    def test_nothing_stays_of_a_key_that_begins_with_the_other_one(self):
        """Covers: AC-provider-keys-007."""
        for psnc, openrouter in ((FIRST, GOOD_KEY), (GOOD_KEY, FIRST)):
            with self.subTest(psnc=psnc), self.key("psnc", psnc), self.key("openrouter", openrouter):
                self.assertEqual(without_keys(f"Passed in: {GOOD_KEY}!"), f"Passed in: {REDACTED}!")
                self.assertEqual(without_keys(f"{FIRST} and {GOOD_KEY}"), f"{REDACTED} and {REDACTED}")

    def test_a_long_key_is_found_also_inside_a_word(self):
        """Covers: AC-provider-keys-007."""
        for text, shown in (
            (f"x{GOOD_KEY}y", f"x{REDACTED}y"), (repr(f"\n{GOOD_KEY}"), f"'\\n{REDACTED}'"), (f"{GOOD_KEY}{GOOD_KEY}", f"{REDACTED}{REDACTED}"),
        ):
            for provider in SETTINGS:
                with self.subTest(text, provider=provider), self.key(provider, GOOD_KEY):
                    self.assertEqual(without_keys(text), shown)

    def test_a_long_part_of_a_key_is_found_by_itself_whatever_its_characters(self):
        """Covers: AC-provider-keys-007.

        Each half has nine visible characters, but no run of eight letters or digits.
        """
        key = "Q$k7!W%m5\nZ#x9&Y*v8"
        for provider in SETTINGS:
            with self.subTest(provider=provider), self.key(provider, key):
                self.assertEqual(without_keys("Passed in: Q$k7!W%m5 and Z#x9&Y*v8."), f"Passed in: {REDACTED} and {REDACTED}.")

    def test_a_key_saved_with_its_quotes_is_found_without_them(self):
        """Covers: AC-provider-keys-007.

        Only the run of its letters, digits, ``_``, ``.`` and ``-`` is long enough here.
        """
        for key in ("'Qk7_Wm5-Zx9'", '"Qk7.Wm5_Zx9-"', "`Qk7-Wm5.Zx9_`"):
            plain = key.strip("'\"`")
            for provider in SETTINGS:
                with self.subTest(key, provider=provider), self.key(provider, key):
                    self.assertEqual(without_keys(f"Invalid key {plain} given"), f"Invalid key {REDACTED} given")

    def test_a_key_of_eight_characters_is_long_and_one_of_seven_short(self):
        """Covers: AC-provider-keys-007."""
        for key, shown in (("Quokka71", f"x{REDACTED}y {REDACTED}"), ("Quokka7", f"xQuokka7y {REDACTED}")):
            for provider in SETTINGS:
                with self.subTest(key, provider=provider), self.key(provider, key):
                    self.assertEqual(without_keys(f"x{key}y {key}"), shown)
        # The same for a part of a key: eight characters of it are taken out by themselves, seven are not.
        for part, shown in (("Wombat52", REDACTED), ("Wombat5", "Wombat5")):
            with self.subTest(part), self.key("psnc", f"Quokka71x\n{part}"):
                self.assertEqual(without_keys(f"a word {part} alone"), f"a word {shown} alone")

    def test_a_key_with_a_blank_goes_whole_after_bearer(self):
        """Covers: AC-provider-keys-007, AC-provider-keys-008.

        Without quotes a ``Bearer`` value ends at the first blank, so the configured keys go first.
        """
        for provider in SETTINGS:
            with self.subTest(provider=provider), self.key(provider, "Quk71 Wmb52"):
                self.assertEqual(without_keys("Authorization: Bearer Quk71 Wmb52; next"), f"Authorization: Bearer {REDACTED} next")

    def test_both_keys_are_found_whoever_is_asked(self):
        """Covers: AC-provider-keys-007."""
        with self.key("psnc", f"{FIRST}-psnc"), self.key("openrouter", f"{SECOND}-openrouter"):
            self.assertEqual(without_keys(f"{FIRST}-psnc and {SECOND}-openrouter"), f"{REDACTED} and {REDACTED}")

    def test_a_bearer_value_is_found_whatever_key_it_is(self):
        """Covers: AC-provider-keys-008.

        Also of a key that was replaced, or of another service.
        """
        for text, shown in (
            ("Authorization: Bearer eyJ0.abc-DEF_1~2+3/4==", f"Authorization: Bearer {REDACTED}"),
            ("authorization: bearer abc", f"authorization: bearer {REDACTED}"),
            ("AUTHORIZATION: BEARER ABC, next", f"AUTHORIZATION: BEARER {REDACTED} next"),
            ("Invalid header value b'Bearer abc\\n'", f"Invalid header value b'Bearer {REDACTED}'"),
            ("in header value: 'Bearer ab\\ncd'", f"in header value: 'Bearer {REDACTED}'"),
            ("in header value: 'Bearer ab cd', sent", f"in header value: 'Bearer {REDACTED}', sent"),
            ('{"Authorization": "Bearer ab cd", "code": 1}', f'{{"Authorization": "Bearer {REDACTED}", "code": 1}}'),
            ('"{\\"Authorization\\": \\"Bearer ab cd\\"}, 401"', f'"{{\\"Authorization\\": \\"Bearer {REDACTED}\\"}}, 401"'),
            ("('Authorization', 'Bearer ab cd')", f"('Authorization', 'Bearer {REDACTED}')"),
            ("a header `Bearer ab cd` was sent", f"a header `Bearer {REDACTED}` was sent"),
            ("in header value: 'Bearer ab\ncd', sent", f"in header value: 'Bearer {REDACTED}', sent"),
            ('{\\"Authorization\\": \\"Bearer ab\r\n cd\\"}', f'{{\\"Authorization\\": \\"Bearer {REDACTED}\\"}}'),
            ("Bearer ab\nBearer cd", f"Bearer {REDACTED}\nBearer {REDACTED}"),
            ("Bearer\n  ab", f"Bearer {REDACTED}"),
            ("?header=Bearer%20abc&x=1", f"?header=Bearer {REDACTED}"),
            ("headers\\nBearer abc", f"headers\\nBearer {REDACTED}"),
            ('"{\\\\\\"A\\\\\\": \\\\\\"Bearer ab cd\\\\\\"}"', f'"{{\\\\\\"A\\\\\\": \\\\\\"Bearer {REDACTED}\\\\\\"}}"'),
            ("a forebearer of it", f"a forebearer {REDACTED} it"),  # a letter may come before: the n of a written-out line break
            ("value b'Bearer ab cd and the text ends here", f"value b'Bearer {REDACTED}"),
        ):
            with self.subTest(text):
                self.assertEqual(without_keys(text), shown)
                self.assertEqual(without_keys(shown), shown)

    def test_any_other_text_stays_as_it_is(self):
        """Covers: AC-provider-keys-008."""
        with self.key("psnc", GOOD_KEY), self.key("openrouter", f"{GOOD_KEY}-2"):
            for text in (
                "", "PSNC_API_KEY is not set.", *MESSAGES.values(), "HTTP 401", "Connection error.", "JSON decode failure: Expecting value: line 1",
                "401 Client Error: Unauthorized for url: https://llm.example.org/v1/chat/completions",
                "Error code: 401 - {'error': {'message': 'Missing Authentication header', 'code': 401}}",
                "Invalid URL 'x': No scheme supplied.", "'latin-1' codec can't encode character '\\u03ba' in position 7: ordinal not in range(256)",
                "Malformed API Key passed in. Ensure Key has `Bearer ` prefix.", "The \"Bearer\" scheme is needed.", "a forebearer", "sk-Quokka", "Wombat52",
                "Die Größe ist 5 µm, „gemessen“\n\tin Zeile 2   \U0001f511", "\\n \\x00 \\u00e9 %20 %s {0}",
            ):
                with self.subTest(text):
                    self.assertEqual(without_keys(text), text)

    def test_a_short_key_is_only_found_where_it_stands_alone(self):
        """Covers: AC-provider-keys-007."""
        for key, text, shown in (
            ("test", "the latest test of testing: 'test'", f"the latest {REDACTED} of testing: '{REDACTED}'"),
            ("<redo>", "undo, <redo>", f"undo, {REDACTED}"),
            ("a", "Invalid header value b'Bearer a\\n' of a request", f"Invalid header value b'Bearer {REDACTED}' of a request"),
            ("a\nb", "a b, a\\nb", "a b, a\\nb"), ("\n", "one\nline\\n", "one\nline\\n"), (" ", "one line", "one line"), ("κ", "'κ'", "'κ'"),
            ("<>", "a <> b", "a <> b"), (None, "None of it", "None of it"), ("", "nothing", "nothing"),
        ):
            for provider in SETTINGS:
                with self.subTest(key=key, provider=provider), self.key(provider, key):
                    self.assertEqual(without_keys(text), shown)

    def test_a_new_value_of_a_setting_counts_at_once(self):
        """Covers: AC-provider-keys-007."""
        for key in (GOOD_KEY, f"{SECOND}-new", GOOD_KEY):
            with self.key("psnc", key):
                self.assertEqual(without_keys(f"Passed in: {key}"), f"Passed in: {REDACTED}")

    def test_a_long_text_takes_no_long_time(self):
        """Covers: AC-provider-keys-007.

        A person's own words can come back in a refusal, so no text may make this slow.
        """
        with self.key("psnc", GOOD_KEY), self.key("openrouter", f"{FIRST}\n{SECOND}"):
            for text in (
                "\\" * 400_000, "'" * 400_000, "'Bearer " * 60_000, "Bearer" * 60_000, "Bearer " * 60_000, "'Bearer x" * 60_000, "Bearer" + " " * 400_000,
                "'Bearer" + " " * 400_000, "\\'Bearer " + "\\" * 400_000, "`Bearer '" * 60_000, "%20" * 120_000, "Bearer" + "%20" * 120_000,
                "sk-Quokka71" * 40_000, f"{FIRST}\n" * 30_000, "'Bearer \"Bearer `Bearer " * 20_000, " \n\t" * 120_000,
            ):
                with self.subTest(text[:30]):
                    started = time.perf_counter()
                    without_keys(text)
                    self.assertLess(time.perf_counter() - started, 2)


# ------------------------------------------------------------------------------------------------
# The log
# ------------------------------------------------------------------------------------------------


class LogTests(KeyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.replace(settings, "psnc_api_key", GOOD_KEY)
        self.logger = logging.Logger("test_provider_keys")  # of its own: pytest's handlers do not come to it
        self.out, self.raw = io.StringIO(), io.StringIO()
        for stream, filters in ((self.out, [KeysFilter()]), (self.raw, [])):
            handler = logging.StreamHandler(stream)
            handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            for one in filters:
                handler.addFilter(one)
            self.logger.addHandler(handler)

    def test_a_line_is_written_without_the_keys_its_arguments_included(self):
        """Covers: AC-provider-keys-009."""
        self.logger.warning("a header %s and %r", f"Bearer {GOOD_KEY}", {"key": GOOD_KEY})
        self.logger.warning(f"a message with {GOOD_KEY}")
        self.logger.warning(ValueError(GOOD_KEY))
        self.assertEqual(self.out.getvalue(), f"WARNING a header Bearer {REDACTED} and {{'key': '{REDACTED}'}}\nWARNING a message with {REDACTED}\nWARNING {REDACTED}\n")

    def test_a_traceback_is_written_without_them_and_its_causes_too(self):
        """Covers: AC-provider-keys-009."""
        try:
            try:
                raise ValueError(f"Illegal header value {f'Bearer {GOOD_KEY}'.encode()!r}")
            except ValueError as e:
                raise RuntimeError(f"a request failed, its key was {GOOD_KEY}") from e
        except RuntimeError:
            self.logger.exception("a run failed", stack_info=True)
        written = self.out.getvalue()
        self.assert_nowhere(written)
        for part in ("ERROR a run failed\n", "Traceback (most recent call last)", f"ValueError: Illegal header value b'Bearer {REDACTED}'",
                     "The above exception was the direct cause", f"RuntimeError: a request failed, its key was {REDACTED}", "Stack (most recent call last)"):
            self.assertIn(part, written)

    def test_a_stack_is_written_without_them(self):
        """Covers: AC-provider-keys-009."""
        def show() -> None:
            self.logger.warning("here", stack_info=True, extra={})

        record = self.logger.makeRecord("n", logging.WARNING, "f", 1, "here", (), None, sinfo=f"Stack (most recent call last):\n  key = '{GOOD_KEY}'")
        self.logger.handle(record)
        show()
        self.assert_nowhere(self.out.getvalue())
        self.assertIn(f"key = '{REDACTED}'", self.out.getvalue())

    def test_a_traceback_that_another_handler_has_written_before_is_cleaned_too(self):
        """Covers: AC-provider-keys-009."""
        record = self.logger.makeRecord("n", logging.ERROR, "f", 1, "failed", (), None)
        record.exc_text = f"Traceback: {GOOD_KEY}"
        self.logger.handle(record)
        self.assertEqual(self.out.getvalue(), f"ERROR failed\nTraceback: {REDACTED}\n")

    def test_other_handlers_and_other_lines_see_what_was_logged(self):
        """Covers: AC-provider-keys-009."""
        try:
            raise ValueError(f"the key {GOOD_KEY}")
        except ValueError:
            self.logger.error("a key: %s", GOOD_KEY, exc_info=True)
        self.assertEqual(self.raw.getvalue().count(GOOD_KEY), 2)
        self.out.truncate(0)
        self.out.seek(0)
        record = self.logger.makeRecord("n", logging.INFO, "f", 1, "Started %s [%d]", ("here", 5), None, extra={"color_message": "Started %s [%s]"})
        cleaned = KeysFilter().filter(record)
        self.assertEqual((cleaned.msg, cleaned.args, cleaned.color_message), ("Started %s [%d]", ("here", 5), "Started %s [%s]"))
        self.logger.handle(record)
        self.assertEqual(self.out.getvalue(), "INFO Started here [5]\n")

    def test_a_line_whose_arguments_do_not_fit_is_written_without_them(self):
        """Covers: AC-provider-keys-009.

        Left to the handler, it would be reported with its arguments as they are, and with the error it was logged for.
        """
        for arguments in (("%d tries with %s", GOOD_KEY), ("%d of %d", "one", GOOD_KEY), ("%(key)s", GOOD_KEY)):
            with self.subTest(arguments[0]), patch.object(logging, "raiseExceptions", False), redirect_stderr(io.StringIO()) as report:
                self.out.truncate(0)
                self.out.seek(0)
                try:
                    raise ValueError(f"Invalid header value {GOOD_KEY!r}")
                except ValueError:
                    self.logger.warning(*arguments, exc_info=True)  # no exception reaches the code that logs
                written = self.out.getvalue()
                self.assert_nowhere(written, report.getvalue())
                self.assertRegex(written, r"\AWARNING A line of the log does not fit its arguments: .*test_provider_keys\.py:[0-9]+\nTraceback ")
                self.assertIn(f"ValueError: Invalid header value '{REDACTED}'", written)

    def test_other_handlers_see_an_access_line_as_it_was(self):
        """Covers: AC-provider-keys-009."""
        arguments = ("127.0.0.1:1", "GET", f"/api/livez?note={GOOD_KEY}", "1.1", 200)
        record = logging.LogRecord("uvicorn.access", logging.INFO, "f", 1, '%s - "%s %s HTTP/%s" %d', arguments, None)
        cleaned = AccessKeysFilter().filter(record)
        self.assertEqual(cleaned.args, (*arguments[:2], f"/api/livez?note={REDACTED}", *arguments[3:]))
        self.assertEqual((record.args, cleaned.msg), (arguments, record.msg))
        # A line of another shape is kept, too.
        for other in ((), None, ({"key": "value"},)):
            record = logging.LogRecord("uvicorn.access", logging.INFO, "f", 1, "a line", other, None)
            self.assertEqual(AccessKeysFilter().filter(record).args, record.args)

    def test_the_server_writes_every_line_through_it(self):
        """Covers: AC-provider-keys-009.

        As it starts: uvicorn sets its log up, then imports the application.
        """
        code = f"""
import logging
from uvicorn.config import Config
Config("app.main:app")
import app.main
for name in ("openai._base_client", "httpcore.http11", "app.somewhere", "uvicorn.error", "uvicorn"):
    try:
        raise ValueError("Illegal header value b'Bearer {GOOD_KEY}'")
    except ValueError:
        logging.getLogger(name).error("%s could not send {GOOD_KEY}", name, exc_info=True)
logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/api/livez?note={GOOD_KEY}", "1.1", 200)
"""
        env = {**os.environ, "PSNC_API_KEY": GOOD_KEY, "OPENROUTER_API_KEY": "", "IADOPT_LOG_LEVEL": "DEBUG", "PYTHONPATH": str(BACKEND)}
        done = subprocess.run([sys.executable, "-W", "ignore", "-c", code], env=env, cwd=BACKEND, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_nowhere(done.stdout, done.stderr)
        self.assertEqual(done.stderr.count(f"could not send {REDACTED}"), 5, done.stderr)
        self.assertEqual(done.stderr.count(f"ValueError: Illegal header value b'Bearer {REDACTED}'"), 5, done.stderr)
        self.assertIn(f'127.0.0.1:1 - "GET /api/livez?note={REDACTED} HTTP/1.1" 200', done.stdout)


if __name__ == "__main__":
    unittest.main()
