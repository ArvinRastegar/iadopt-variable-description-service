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
from app.core.config import settings
from app.core.logging import KeysFilter
from app.core.provider_keys import REDACTED, without_keys
from app.core.state import app_state
from app.services import entity_linking, label_comment, llm
from app.services.call_log import MAX_ERROR_CHARS
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
    "CR LF inside": f"{FIRST}\r\n{SECOND}",
}
"""Keys that ``requests`` refuses with an error that quotes the header."""

NOT_SENDABLE = {
    **QUOTED_BY_REQUESTS,
    "a character beyond Latin-1": f"{FIRST}κ{SECOND}",
    "a zero byte": f"{FIRST}\0{SECOND}",
}
"""Keys that hold a character that cannot stand in a header, whatever the provider."""

NOT_SENDABLE_BY_SDK = {"a blank at the end": f"{GOOD_KEY} "}
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
        for provider, name, key in CASES + [(provider, "only a line break", "\n") for provider in SETTINGS]:
            for place, use in self.uses(provider).items():
                with self.subTest(name, provider=provider, place=place), self.key(provider, key):
                    with self.assertRaises(RuntimeError) as refused:
                        use()
                    self.assertEqual(str(refused.exception), MESSAGES[provider])
                    self.assert_nowhere(str(refused.exception), repr(refused.exception))
                    self.assertIsNone(openai_client._openai_client)

    def test_a_missing_key_is_still_called_missing(self):
        for provider, setting in SETTINGS.items():
            for key in (None, ""):
                for place, use in self.uses(provider).items():
                    with self.subTest(provider=provider, key=key, place=place), self.key(provider, key):
                        with self.assertRaisesRegex(RuntimeError, rf"^{setting} is not set\.$"):
                            use()

    def test_any_other_key_is_handed_on_as_it_is(self):
        """White space is not trimmed: a blank around PSNC's key is sent today, so it is sent after this too."""
        visible = "".join(map(chr, range(0x21, 0x7F)))
        keys = [GOOD_KEY, visible, f"{FIRST} {SECOND}", f"\t{GOOD_KEY}"]
        for provider in SETTINGS:
            for key in keys + (list(NOT_SENDABLE_BY_SDK.values()) if provider == "psnc" else []):
                for place, use in self.uses(provider).items():
                    with self.subTest(provider=provider, key=key, place=place), self.key(provider, key):
                        self.assertEqual(use(), key)

    def test_what_is_not_refused_is_what_the_http_clients_send(self):
        """A key that the check lets through never fails on the way out, for any character at any place."""
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
        self.assert_nowhere(self.stored())

    def test_the_label_call_and_the_linking_question_are_not_sent(self):
        """The decomposition came back, so the run succeeds, with the fallback label and without links."""
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
                    self.assertEqual({call["purpose"] for call in self.calls()[before:]}, {"decomposition"} if route == STREAM_ROUTE else set())
                    self.assert_nowhere(response.text, out.getvalue())
        self.assertEqual(self.provider.keys, [])
        self.assert_nowhere(self.stored())

    def test_the_service_starts_with_such_a_key_and_the_other_provider_works(self):
        """A malformed key of one provider must not take the service down."""
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
                self.provider.keys.clear()
                self.post(other, JSON_ROUTE)
                self.assertEqual(set(self.provider.keys), {GOOD_KEY})

    def test_the_start_says_why_openrouter_cannot_be_used_without_the_key(self):
        """The refusal holds no key; should the client's own error ever be told here, it would."""
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
        self.assert_nowhere(self.stored())

    def test_what_they_say_of_the_label_call_and_the_linking_question_shows_the_key_nowhere(self):
        for provider, name, key in CASES:
            for route in ROUTES:
                with self.subTest(name, provider=provider, route=route), self.decomposed():
                    response, out = self.run_one(provider, route, key)
                    self.final(response)
                    self.assert_nowhere(response.text, out)
                    if name == "a line break at the end":
                        self.assertIn(f"Entity linking failed: Invalid header value b'Bearer {REDACTED}'", out)
        self.assert_nowhere(self.stored())

    def test_a_label_call_that_fails_in_another_way_is_told_without_the_key(self):
        quoted = f"Invalid header value {f'Bearer {GOOD_KEY}'.encode()!r}"
        for provider, call in (("psnc", "call_psnc_model"), ("openrouter", "call_model")):
            with self.subTest(provider), self.key(provider, GOOD_KEY), patch.object(label_comment, call, side_effect=ValueError(quoted)), \
                    captured(as_configured=True) as out:
                label_comment.generate_label_and_comment("air temperature", model_provider=provider, model=MODELS[provider], temperature=0.5)
            self.assert_nowhere(out.getvalue())
            self.assertIn(f"Label/comment generation failed: Invalid header value b'Bearer {REDACTED}'", out.getvalue())

    def test_an_error_that_leaves_the_services_is_shown_without_the_key(self):
        """Whatever a later step raises: the last texts are made in the pipeline and in the routes."""
        quoted = f"Invalid header value {f'Bearer {GOOD_KEY}'.encode()!r}"
        for kind in (ValueError, KeyError):
            for route in ROUTES:
                for place in ("_prepare_pipeline_inputs", "_finalize_pipeline_output", "_labelled_prediction"):
                    with self.subTest(kind=kind.__name__, route=route, place=place), self.decomposed(), \
                            patch.object(pipeline, place, side_effect=kind(quoted)):
                        response, out = self.run_one("psnc", route, GOOD_KEY)
                        self.assert_nowhere(response.text, out)
                        self.assertIn(f"Bearer {REDACTED}", response.text)
                        self.assertIn(f"Bearer {REDACTED}", self.last("audit_events")["error"])
        self.assert_nowhere(self.stored())

    def test_a_stream_that_breaks_in_the_route_is_recorded_without_the_key(self):
        def broken(*_: Any, **__: Any) -> Iterator[str]:
            yield llm.stream_event("raw_delta", delta="{")
            raise ValueError(f"Invalid header value {GOOD_KEY!r}")

        with self.key("psnc", GOOD_KEY), patch("app.routers.decompose.stream_pipeline_events", broken), captured(as_configured=True) as out:
            with self.assertRaises(ValueError):  # the test client raises what the server would log
                self.post("psnc", STREAM_ROUTE)
        self.assertEqual(self.last("audit_events")["error"], f"Invalid header value '{REDACTED}'")
        self.assertEqual(self.last("record_runs")["error"], f"Invalid header value '{REDACTED}'")
        self.assert_nowhere(out.getvalue(), self.stored())


class QuotedByTheProviderTests(RunTestCase):
    """A key that can be sent, and a provider that quotes it when it refuses it."""

    KEYS = {
        "an ordinary key": GOOD_KEY,
        "quotes and a backslash inside": f"{FIRST}\"'\\{SECOND}",
        "other punctuation": f"{FIRST}$^[(.*+?)]|{{}}%s%d{{0}}&<>/{SECOND}",
    }
    SAVED_WITH_MORE = {
        "its own single quotes": f"'{GOOD_KEY}'",
        "its own double quotes": f'"{GOOD_KEY}"',
        "a line break written out": f"{GOOD_KEY}\\n",
    }
    """Keys as a hand-made secret holds them by mistake; a header can carry them all."""

    def run_one(self, provider: str, route: str, key: str) -> Tuple[Any, str]:
        self.provider.keys.clear()
        with self.key(provider, key), captured(as_configured=True) as out:
            response = self.post(provider, route)
        self.assertEqual(set(self.provider.keys), {key}, "the key went out as it is")
        return response, out.getvalue()

    def test_a_refusal_that_quotes_the_key_shows_it_nowhere(self):
        for provider in SETTINGS:
            for name, key in self.KEYS.items():
                for route in ROUTES:
                    with self.subTest(name, provider=provider, route=route):
                        response, out = self.run_one(provider, route, key)
                        self.assert_nowhere(response.text, out)
                        self.assertIn(REDACTED, out)
        self.assert_nowhere(self.stored())
        self.assertEqual({call["error"] for call in self.calls()}, {"HTTP 401"})

    def test_a_key_that_was_saved_with_its_quotes_is_shown_nowhere_whatever_the_refusal_does_with_them(self):
        for refused, refusal in {
            "beside other quotes": lambda key: json.dumps({"error": {"message": f"Invalid key {key} for model \"qwen/qwen3-32b\" of 'me'"}}),
            "without them": lambda key: json.dumps({"error": {"message": f"Invalid key {key.strip(chr(34) + chr(39) + '`,')} for a model"}}),
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
        """The log shows 500 characters of a refusal and the call log 300 of a reason, wherever the key begins."""
        for distance in (0, 490, 500):
            self.provider.refusal = lambda key, distance=distance: "." * distance + key + "!"
            with self.subTest(distance=distance):
                _, out = self.run_one("psnc", JSON_ROUTE, GOOD_KEY)
                printed = [line.rpartition("chat/completions ")[2] for line in out.splitlines() if line.startswith("PSNC HTTP error attempt")]
                self.assertEqual(set(printed), {("." * distance + REDACTED + "!")[:500]})
        for length in (MAX_ERROR_CHARS - len(GOOD_KEY) - 20, MAX_ERROR_CHARS - 20, MAX_ERROR_CHARS):
            with self.subTest(length=length), self.key("psnc", GOOD_KEY):
                whole = f"RuntimeError: {'.' * length}{REDACTED}"
                shown = whole if len(whole) <= MAX_ERROR_CHARS else f"{whole[:MAX_ERROR_CHARS - 1]}…"
                self.assertEqual(llm.call_error(RuntimeError("." * length + GOOD_KEY)), shown)

    def test_every_error_of_a_request_is_told_without_the_key(self):
        """Any exception the clients raise, in the call log and where it is printed."""
        request = openai_client.OPENROUTER_BASE_URL
        for provider, errors in {
            "psnc": (requests.ConnectionError(GOOD_KEY), requests.HTTPError(GOOD_KEY)),
            "openrouter": (openai.APIConnectionError(message=GOOD_KEY, request=httpx.Request("POST", request)), httpx.ReadError(GOOD_KEY)),
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
                    if not isinstance(error, requests.HTTPError):
                        self.assertEqual(self.last("record_llm_calls")["error"], f"{type(error).__name__}: {REDACTED}")
        self.assert_nowhere(self.stored())


class WithoutKeysTests(KeyTestCase):
    def test_a_key_is_found_however_a_text_writes_it(self):
        for name, key in {**NOT_SENDABLE, **QuotedByTheProviderTests.KEYS}.items():
            writings = {
                "as it is": key, "in words": f"the key {key}, as it was", "on one line": " ".join(key.split()),
                "as Python shows it": repr(key), "as Python shows its bytes": repr(key.encode("utf-8", "backslashreplace")),
                "in JSON": json.dumps({"message": key}), "in an exception": ValueError(f"Passed in: {key}"),
            }
            for provider in SETTINGS:
                for writing, text in writings.items():
                    with self.subTest(name, provider=provider, writing=writing), self.key(provider, key):
                        shown = without_keys(text)
                        self.assert_nowhere(shown)
                        self.assertIn(REDACTED, shown)

    def test_how_much_of_a_key_must_match(self):
        """Eight characters are found anywhere, shorter keys only where they stand alone; a long part of a key is found by
        itself, a key saved with quotes without them, and a key with a blank whole after ``Bearer``."""
        for key, text, shown in (
            ("Quokka71", "xQuokka71y Quokka71", f"x{REDACTED}y {REDACTED}"),
            ("Quokka7", "xQuokka7y Quokka7", f"xQuokka7y {REDACTED}"),
            ("Quokka71x\nWombat52", "a word Wombat52 alone", f"a word {REDACTED} alone"),
            ("Quokka71x\nWombat5", "a word Wombat5 alone", "a word Wombat5 alone"),
            ("Q$k7!W%m5\nZ#x9&Y*v8", "Passed in: Q$k7!W%m5 and Z#x9&Y*v8.", f"Passed in: {REDACTED} and {REDACTED}."),
            ("'Qk7_Wm5-Zx9'", "Invalid key Qk7_Wm5-Zx9 given", f"Invalid key {REDACTED} given"),
            ("test", "the latest test of testing: 'test'", f"the latest {REDACTED} of testing: '{REDACTED}'"),
            ("a\nb", "a b, a\\nb", "a b, a\\nb"),
            (None, "None of it", "None of it"),
            ("Quk71 Wmb52", "Authorization: Bearer Quk71 Wmb52; next", f"Authorization: Bearer {REDACTED} next"),
        ):
            for provider in SETTINGS:
                with self.subTest(key=key, text=text, provider=provider), self.key(provider, key):
                    self.assertEqual(without_keys(text), shown)

    def test_both_keys_are_found_and_a_new_value_counts_at_once(self):
        with self.key("psnc", f"{FIRST}-psnc"), self.key("openrouter", f"{SECOND}-openrouter"):
            self.assertEqual(without_keys(f"{FIRST}-psnc and {SECOND}-openrouter"), f"{REDACTED} and {REDACTED}")
        for key in (GOOD_KEY, f"{SECOND}-new"):
            with self.key("psnc", key):
                self.assertEqual(without_keys(f"Passed in: {key}"), f"Passed in: {REDACTED}")

    def test_a_bearer_value_is_found_whatever_key_it_is(self):
        """Also of a key that was replaced, or of another service."""
        for text, shown in (
            ("Authorization: Bearer eyJ0.abc-DEF_1~2+3/4==", f"Authorization: Bearer {REDACTED}"),
            ("AUTHORIZATION: BEARER ABC, next", f"AUTHORIZATION: BEARER {REDACTED} next"),
            ("Invalid header value b'Bearer abc\\n'", f"Invalid header value b'Bearer {REDACTED}'"),
            ("in header value: 'Bearer ab cd', sent", f"in header value: 'Bearer {REDACTED}', sent"),
            ('{"Authorization": "Bearer ab cd", "code": 1}', f'{{"Authorization": "Bearer {REDACTED}", "code": 1}}'),
            ("Bearer ab\nBearer cd", f"Bearer {REDACTED}\nBearer {REDACTED}"),
            ("?header=Bearer%20abc&x=1", f"?header=Bearer {REDACTED}"),
            ("value b'Bearer ab cd and the text ends here", f"value b'Bearer {REDACTED}"),
        ):
            with self.subTest(text):
                self.assertEqual(without_keys(text), shown)
                self.assertEqual(without_keys(shown), shown)

    def test_any_other_text_stays_as_it_is(self):
        with self.key("psnc", GOOD_KEY), self.key("openrouter", f"{GOOD_KEY}-2"):
            for text in (
                "", "PSNC_API_KEY is not set.", *MESSAGES.values(), "HTTP 401",
                "401 Client Error: Unauthorized for url: https://llm.example.org/v1/chat/completions",
                "Malformed API Key passed in. Ensure Key has `Bearer ` prefix.", "a forebearer", "sk-Quokka",
                "Die Größe ist 5 µm, „gemessen“\n\tin Zeile 2   \U0001f511",
            ):
                with self.subTest(text):
                    self.assertEqual(without_keys(text), text)

    def test_a_long_text_takes_no_long_time(self):
        """A person's own words can come back in a refusal, so no text may make this slow."""
        with self.key("psnc", GOOD_KEY), self.key("openrouter", f"{FIRST}\n{SECOND}"):
            for text in ("\\" * 400_000, "'Bearer " * 60_000, "Bearer" + " " * 400_000, "%20" * 120_000,
                         "sk-Quokka71" * 40_000, f"{FIRST}\n" * 30_000):
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

    def test_a_line_a_traceback_and_a_stack_are_written_without_the_keys(self):
        self.logger.warning("a header %s and %r", f"Bearer {GOOD_KEY}", {"key": GOOD_KEY})
        try:
            try:
                raise ValueError(f"Illegal header value {f'Bearer {GOOD_KEY}'.encode()!r}")
            except ValueError as e:
                raise RuntimeError(f"a request failed, its key was {GOOD_KEY}") from e
        except RuntimeError:
            self.logger.exception("a run failed", stack_info=True)
        written = self.out.getvalue()
        self.assert_nowhere(written)
        for part in (f"WARNING a header Bearer {REDACTED} and {{'key': '{REDACTED}'}}\n", "Traceback (most recent call last)",
                     f"ValueError: Illegal header value b'Bearer {REDACTED}'", f"RuntimeError: a request failed, its key was {REDACTED}",
                     "Stack (most recent call last)"):
            self.assertIn(part, written)

    def test_other_handlers_see_what_was_logged(self):
        try:
            raise ValueError(f"the key {GOOD_KEY}")
        except ValueError:
            self.logger.error("a key: %s", GOOD_KEY, exc_info=True)
        self.assertEqual(self.raw.getvalue().count(GOOD_KEY), 2)
        self.assert_nowhere(self.out.getvalue())

    def test_a_line_whose_arguments_do_not_fit_is_written_without_them(self):
        """Left to the handler, it would be reported with its arguments as they are, and with the error it was logged for."""
        for arguments in (("%d tries with %s", GOOD_KEY), ("%(key)s", GOOD_KEY)):
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

    def test_the_server_writes_every_line_through_it(self):
        """As it starts: uvicorn sets its log up, then imports the application."""
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
