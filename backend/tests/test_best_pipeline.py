"""The lab's best pipeline in the web app: examples by similarity, the matrix finder, the most typical of three answers.

Nothing here reaches PSNC or any other service. Every PSNC request goes to a fake session that answers each kind of
request (embedding, matrix finder, fill, streamed fill, label) from its own script and keeps what each request carried.
A stand-in for the thread pool runs the side-by-side requests one after another, so that the order is fixed; one test
proves they do go out side by side. The lab's own outputs, made by ``backend/tools/build_finder_data.py --fixtures``
and the earlier retrieval fixtures, let the tests compare the web app with the lab without a lab checkout
(fixtures/best_pipeline/PROVENANCE.md).

The other test modules run with the extra requests switched off (conftest.py, retrieval_support.py).
"""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import importlib
import io
import json
import math
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from collections import Counter
from types import SimpleNamespace
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple
from unittest.mock import patch

import previous_image
import pytest
import requests
from retrieval_support import switch_best_pipeline_off
from test_daily_limit import DEFINITION as LIMITED_BODY
from test_daily_limit import JSON_ROUTE, LimitTestCase
from test_quality_gate import RUN, container, make_records, qg, run_main
from test_quality_gate import answer as gate_answer
from test_records import CLIENT, RecordTestCase

from app import pipeline, storage
from app.clients import http as http_client
from app.core.config import settings
from app.services import answer_vote, example_retrieval, llm, matrix_finder, measured_prompt
from app.services.call_log import CallLog, start_call
from app.services.prompts import build_prompt, list_prompt_versions, load_examples

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
POOL_PATH = BACKEND / "data" / "measured" / "example-pool.json"
FINDER_PATH = BACKEND / "data" / "measured" / "matrix-finder.json"
FIXTURES = BACKEND / "tests" / "fixtures"
CASES_DIR = FIXTURES / "best_pipeline"
GOLDEN = (FIXTURES / "measured_prompt_golden.txt").read_text(encoding="utf-8")
TARGET = (FIXTURES / "measured_target_definition.txt").read_text(encoding="utf-8").rstrip("\n")
LAB_CASES = json.loads((CASES_DIR / "lab_cases.json").read_text(encoding="utf-8"))
LAB_PROMPTS = json.loads((CASES_DIR / "lab_prompts.json").read_text(encoding="utf-8"))
LEXICAL_CASES = json.loads((CASES_DIR / "lexical_cases.json").read_text(encoding="utf-8"))["cases"]
FIXTURE_SHA256 = {
    "lab_cases.json": "6b1a79f5170ac901890b3e5a0724e237189518dd3f1f28fa6e2706f9a05202d7",
    "lab_prompts.json": "752675052e075120d7638dade9b85ba349b84e39c66c6033201738bd613be94b",
    "lexical_cases.json": "3e553a35425fd8560178773c0b8d7597f9aa715bd7a924392314d2d92c5cb5db",
}

EMBEDDING_MODEL = "Qwen3-Embedding-0.6B"
MODEL = "Qwen3.8-27B"
BASE_URL = "https://psnc.test"
KEY = "test-psnc-key"
DIMENSIONS = 1024
DEFINITIONS_DIGEST, FOLDS_DIGEST, VECTORS_DIGEST = "c5a9f585fbcb9ffb", "80f37c48d2cf004c", "3480e2bb4f059272"
RETRIEVED_PROMPTS, FIXED_PROMPTS = "108e2e7210426983", "78fcde54455ecd22"
FINDER_PROMPTS, FILL_PROMPTS = "2c4a50e46b18415f", "1ea4710d404120f7"
SETUP = "measured-best-pipeline"
MIGRATION = "0007_record_choices"
PAYLOAD_KEYS = {"model", "messages", "stream", "temperature", "top_p", "max_tokens", "chat_template_kwargs"}
RESULT_KEYS = {"raw_llm_output", "parsed_json", "schema_valid", "validation_errors", "enriched_json", "ttl"}
REBUILD_POOL = "Rebuild it with backend/tools/build_example_pool.py."
REBUILD_FINDER = "Rebuild it with backend/tools/build_finder_data.py."
NOT_KEPT = "not kept: the most typical of the answers was kept instead."
MEASURED_SETTINGS: Dict[str, Any] = {
    "enabled_model_providers_raw": "psnc,openrouter", "default_model_provider_raw": "psnc",
    "psnc_api_key": KEY, "psnc_api_base_url": BASE_URL, "psnc_model_name": MODEL, "psnc_model_names_raw": MODEL,
    "model_name": "qwen/qwen3-32b", "model_names_raw": "",
    "use_measured_configuration": True, "enable_wikidata_linking": False,
}

# Each failure's name and hint.
HINTS = {401: "The PSNC key was refused.", 403: "The PSNC key was refused.", 429: "PSNC's limit or budget was reached.",
         500: "PSNC had a server error.", 503: "PSNC had a server error.", 404: "PSNC refused the request.",
         204: "PSNC refused the request."}
TIMED_OUT = "embedding request timed out after 5 s. PSNC's embedding model did not answer in time."
NO_CONNECTION = "embedding request failed: ConnectionError. No connection to PSNC."
NO_VECTOR = "embedding answer holds no vector. PSNC's answer is not in the expected form."
TWO_VECTORS = "embedding answer holds 2 vectors, not one. PSNC's answer is not in the expected form."
NOT_A_NUMBER = "embedding vector holds a value that is not a number. PSNC's answer is not in the expected form."
WRONG_SIZE = "embedding vector has 768 numbers, not 1,024. PSNC may have changed its embedding model."
NOT_FINITE = "embedding vector holds a number that is not finite. PSNC's answer is damaged."
ZERO_LENGTH = "embedding vector has length zero. It cannot be compared with the pool."
NO_KEY_EMBEDDING = "embedding request not sent: PSNC_API_KEY is not set. Check the PSNC key in the backend's settings."
NO_KEY_FINDER = "matrix finder not sent: PSNC_API_KEY is not set. Check the PSNC key in the backend's settings."
FINDER_TIMED_OUT = "matrix finder timed out after 20 s. PSNC's model did not answer in time."
FINDER_NO_CONNECTION = "matrix finder failed: ConnectionError. No connection to PSNC."
NOT_REQUESTED = "matrix finder answer is not the requested JSON. No matrix is stated."
NOT_A_COMPLETION = "matrix finder answer is not a chat completion. PSNC's answer is not in the expected form."
STREAM_CLOSED = "The stream closed before the answer was complete."
CLOSED_BEFORE_VOTE = "The stream closed before the answers were compared."
VOTE_RULE = ("most typical of the answers by the scorer's Exact F1, both ways; ties to the first the lab's check "
             "accepts, else the first")
TOO_DEEP = RecursionError("maximum recursion depth exceeded while decoding a JSON object")
NOT_JSON = requests.exceptions.JSONDecodeError("Expecting value", "<html><body>Bad gateway</body></html>", 0)


def refused(what: str, status: int) -> str:
    return f"{what} refused: HTTP {status}. {HINTS[status]}"


# The model's answers.
DEFINITION = "dissolved oxygen concentration in lake water"
VALID = {"hasStatisticalModifier": "", "hasProperty": "concentration", "hasObjectOfInterest": "oxygen",
         "hasMatrix": "lake water", "hasContextObject": "",
         "hasConstraint": [{"label": "state: dissolved", "on": "oxygen"}]}
FAR = {"hasStatisticalModifier": "mean", "hasProperty": "temperature", "hasObjectOfInterest": "air",
       "hasMatrix": "", "hasContextObject": "atmosphere", "hasConstraint": []}
ANSWER = json.dumps(VALID, indent=2)
FAR_ANSWER = json.dumps(FAR, indent=2)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_of(prompts: Sequence[str]) -> str:
    return sha256_text("\n".join(sha256_text(prompt) for prompt in prompts))[:16]


_POOL: Dict[str, Any] = {}


def pool() -> Dict[str, Any]:
    if not _POOL:
        _POOL.update(json.loads(POOL_PATH.read_text(encoding="utf-8")))
    return _POOL


def variables() -> Dict[str, Dict[str, Any]]:
    return {entry["id"]: entry for entry in pool()["variables"]}


def finder_data() -> Dict[str, Any]:
    return json.loads(FINDER_PATH.read_text(encoding="utf-8"))


def fold_of(identifier: str) -> FrozenSet[str]:
    fold = variables()[identifier]["fold"]
    return frozenset(i for i, entry in variables().items() if entry["fold"] == fold)


def unit(vector: Sequence[float]) -> List[float]:
    length = math.sqrt(sum(x * x for x in vector))
    return [x / length for x in vector]


def nearest_last(query: Sequence[float], leave_out: FrozenSet[str] = frozenset()) -> List[str]:
    """The lab's choice, computed here: the cosine of unit vectors rounded to 9 places, most similar last."""
    direction = unit(query)
    scores = {identifier: round(sum(a * b for a, b in zip(direction, unit(entry["vector"]))), 9)
              for identifier, entry in variables().items() if identifier not in leave_out}
    ranked = sorted(scores, key=lambda identifier: (-scores[identifier], identifier))
    return ranked[:25][::-1]


def shown(ids: Sequence[str]) -> List[Dict[str, Any]]:
    pool_variables = variables()
    return [{"definition": pool_variables[i]["definition"], "decomposition": pool_variables[i]["decomposition"]}
            for i in ids]


def render(definition: str, ids: Sequence[str]) -> str:
    return measured_prompt.render_measured_prompt(definition, demonstrations=shown(ids))


def fixed_ids() -> List[str]:
    by_definition = {entry["definition"]: entry["id"] for entry in pool()["variables"]}
    return [by_definition[d["definition"]] for d in measured_prompt.load_demonstrations()]


def plain_ids() -> List[str]:
    fixed = set(fixed_ids())
    return [i for i, entry in sorted(variables().items())
            if i not in fixed and entry["definition"] == entry["definition"].strip()]


def query_vector() -> List[float]:
    return variables()[plain_ids()[0]]["vector"]


def legacy_prompt(definition: str) -> str:
    return build_prompt(definition, prompt_version=list_prompt_versions(settings.prompt_dir)[0],
                        examples=load_examples(settings.five_shot_dir, 5))


def by_fields(calls: Sequence[Any], *fields: str) -> List[Tuple[Any, ...]]:
    return [tuple(getattr(call, field) for field in fields) for call in calls]


# ------------------------------------------------------------------------------------------------
# The fake PSNC
# ------------------------------------------------------------------------------------------------


class Answer:
    """One answer of the fake PSNC: an HTTP status with a JSON body, or the lines of a stream."""

    text = "PSNC's own words"

    def __init__(self, body: Any = None, status: int = 200, lines: Sequence[str] = ()) -> None:
        self.status_code, self._body, self._lines = status, body, list(lines)

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)  # type: ignore[arg-type]

    def iter_lines(self, decode_unicode: bool = False) -> Iterator[str]:
        return iter(self._lines)


class Psnc:
    """The fake PSNC: answers each kind of request from its own script, and keeps what each request carried.

    Kinds: ``embedding`` (``/v1/embeddings``), ``stream`` (a streamed chat), ``finder`` (a chat of at most 700 tokens),
    ``label`` (the legacy body, which carries ``enable_thinking``), and ``fill`` (the measured body).
    """

    def __init__(self, embedding: Sequence[Any] = (), finder: Sequence[Any] = (), fill: Sequence[Any] = (),
                 stream: Sequence[Any] = (), label: Sequence[Any] = ()) -> None:
        self.scripts = {"embedding": list(embedding), "finder": list(finder), "fill": list(fill),
                        "stream": list(stream), "label": list(label)}
        self.requests: List[SimpleNamespace] = []
        self.lock = threading.Lock()
        self.barriers: Dict[str, threading.Barrier] = {}
        self.holds: Dict[str, threading.Event] = {}
        self.arrived: Counter = Counter()

    @staticmethod
    def kind(url: str, body: Any, stream: bool) -> str:
        body = body or {}
        if url.endswith("/v1/embeddings"):
            return "embedding"
        if stream or body.get("stream"):
            return "stream"
        if body.get("max_tokens") == 700:
            return "finder"
        if "enable_thinking" in body:
            return "label"
        return "fill"

    def post(self, url: str, headers: Any = None, json: Any = None, timeout: Any = None, stream: bool = False,
             **_: Any) -> Any:
        kind = self.kind(url, json, stream)
        with self.lock:
            self.arrived[kind] += 1
        barrier = self.barriers.get(kind)
        if barrier is not None:
            barrier.wait(timeout=5)  # raises BrokenBarrierError unless the others arrive meanwhile
        hold = self.holds.get(kind)
        if hold is not None:
            hold.wait(timeout=5)  # the answer is on its way until the test lets it come
        with self.lock:
            self.requests.append(SimpleNamespace(kind=kind, url=url, headers=dict(headers or {}), body=json,
                                                 timeout=timeout, stream=stream))
            script = self.scripts[kind]
            if not script:
                raise AssertionError(f"PSNC got a {kind} request the test did not expect")
            outcome = script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def kinds(self) -> List[str]:
        return [request.kind for request in self.requests]

    def wait_for(self, kind: str, count: int) -> None:
        """Wait until ``count`` requests of a kind have arrived, sent from other threads."""
        for _ in range(500):
            with self.lock:
                if self.arrived[kind] >= count:
                    return
            time.sleep(0.01)
        raise AssertionError(f"{count} {kind} requests never arrived")

    def of(self, kind: str) -> List[SimpleNamespace]:
        return [request for request in self.requests if request.kind == kind]


def embedding(vector: Sequence[Any], prompt_tokens: Optional[int] = None) -> Answer:
    body: Dict[str, Any] = {"object": "list", "model": EMBEDDING_MODEL,
                            "data": [{"object": "embedding", "index": 0, "embedding": list(vector)}]}
    if prompt_tokens is not None:
        body["usage"] = {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens}
    return Answer(body)


def chat(content: str, usage: Optional[Dict[str, int]] = None) -> Answer:
    body: Dict[str, Any] = {"choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = usage
    return Answer(body)


def streamed(content: str, reasoning: Optional[str] = None) -> Answer:
    events = [{"choices": [{"index": 0, "delta": {"reasoning_content": reasoning}}]}] if reasoning else []
    events.append({"choices": [{"index": 0, "delta": {"content": content}}]})
    return Answer(lines=[f"data: {json.dumps(event)}" for event in events] + ["data: [DONE]"])


def labelled() -> Answer:
    return chat(json.dumps({"label": "dissolved oxygen in lake water",
                            "comment": "How much oxygen is dissolved in the water of a lake."}))


def finder_says(matrix: Optional[int] = 41, prop: Optional[Any] = None) -> Answer:
    """A finder answer in the requested form."""
    number = finder_data()["property_patterns"][0] if prop is None else prop
    return chat(json.dumps({"object_of_interest": "oxygen", "matrix_entity": "lake water" if matrix else None,
                            "restrictions": ["dissolved"], "property": number, "matrix": matrix, "context": False,
                            "modifier": False, "constraints": ["state"]}, indent=2))


def statement_of(matrix: Optional[int]) -> str:
    return finder_data()["statements"]["none" if matrix is None else str(matrix)]


class SerialPool:
    """A thread pool that runs each submitted call at once, in order: the side-by-side requests in a fixed order."""

    def __init__(self, max_workers: Optional[int] = None) -> None:
        pass

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as error:  # the caller's result() raises it, as with a real pool
            future.set_exception(error)
        return future

    def shutdown(self, wait: bool = True) -> None:
        pass

    def __enter__(self) -> "SerialPool":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


ORIGINAL_SESSION = http_client.get_http_session


def session_places() -> List[Any]:
    """Every module of the application that holds the shared HTTP session's getter."""
    return [module for name, module in list(sys.modules.items())
            if module is not None and (name == "app" or name.startswith("app."))
            and getattr(module, "get_http_session", None) is ORIGINAL_SESSION]


@pytest.fixture
def measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured path on PSNC as prod runs it, with a key that is no secret and the linking off."""
    for name, value in MEASURED_SETTINGS.items():
        monkeypatch.setattr(settings, name, value)
    pipeline.warmup_assets()


@pytest.fixture
def psnc(monkeypatch: pytest.MonkeyPatch, measured: None) -> Callable[..., Psnc]:
    """Installs a fake PSNC; the side-by-side requests run one after another unless ``parallel`` is given."""
    places = session_places()

    def install(*, parallel: bool = False, **scripts: Any) -> Psnc:
        session = Psnc(**scripts)
        for module in places:
            monkeypatch.setattr(module, "get_http_session", lambda: session)
        if not parallel:
            monkeypatch.setattr(pipeline, "ThreadPoolExecutor", SerialPool)
        return session

    return install


@pytest.fixture
def rewarm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """After a test that pointed the artifacts elsewhere, warm the caches up again from the real ones."""
    yield
    monkeypatch.undo()
    pipeline.warmup_assets()


def point_at(monkeypatch: pytest.MonkeyPatch, setting: str, path: pathlib.Path) -> None:
    monkeypatch.setattr(type(settings), setting, property(lambda self: path), raising=False)


def run(definition: str = DEFINITION) -> Tuple[Dict[str, Any], CallLog]:
    log = CallLog()
    return pipeline.run_pipeline(definition, model_provider="psnc", calls=log), log


def stream(definition: str = DEFINITION) -> Tuple[List[Dict[str, Any]], CallLog]:
    log = CallLog()
    lines = pipeline.stream_pipeline_events(definition, model_provider="psnc", calls=log)
    return [json.loads(line) for line in lines], log


def happy(query: Optional[List[float]] = None, *, fills: Sequence[Any] = (), **more: Any) -> Dict[str, Any]:
    """A script where the examples and the finder work; the fills are given."""
    return dict(embedding=[embedding(query or query_vector(), prompt_tokens=11)], finder=[finder_says(41)],
                fill=list(fills), label=[labelled()], **more)


# ------------------------------------------------------------------------------------------------
# The data: the pool and the finder's (the lab's)
# ------------------------------------------------------------------------------------------------


def test_the_fixtures_are_the_ones_recorded():
    """The fixtures made from the lab are the files fixtures/PROVENANCE.md records."""
    for name, expected in FIXTURE_SHA256.items():
        assert hashlib.sha256((CASES_DIR / name).read_bytes()).hexdigest() == expected, name


def test_the_pool_holds_the_labs_102_variables_with_their_digests():
    data = pool()
    entries = data["variables"]
    ids = [entry["id"] for entry in entries]
    assert len(entries) == 102 and len(set(ids)) == 102
    assert all(identifier.startswith("urn:iadopt-lab:variable:") for identifier in ids)
    corpus = json.dumps(data["corpus"], ensure_ascii=False)
    assert "I-ADOPT Corpus" in corpus and "v2.0.1" in corpus
    assert data["embedding_model"] == EMBEDDING_MODEL
    ordered = sorted(entries, key=lambda entry: entry["id"])
    definitions = sha256_text("\n".join(f"{entry['id']}\t{entry['definition']}" for entry in ordered))
    folds = sha256_text("\n".join(f"{entry['id']} {entry['fold']}" for entry in ordered))
    vectors = sha256_text(json.dumps({entry["id"]: entry["vector"] for entry in entries}, sort_keys=True))
    assert (definitions[:16], folds[:16], vectors[:16]) == (DEFINITIONS_DIGEST, FOLDS_DIGEST, VECTORS_DIGEST)
    assert (data["definitions_sha256"], data["folds_sha256"], data["vectors_sha256"]) == (definitions, folds, vectors)
    assert sorted(Counter(entry["fold"] for entry in entries).items()) == [(fold, 17) for fold in range(6)]
    for entry in entries:
        assert set(entry["decomposition"]) == set(measured_prompt.ANSWER_KEY_ORDER), entry["id"]
        assert len(entry["vector"]) == DIMENSIONS and all(math.isfinite(x) for x in entry["vector"]) and any(entry["vector"])


def test_the_fixed_examples_are_in_the_pool_with_the_same_answers():
    in_pool = {entry["definition"]: entry for entry in pool()["variables"]}
    for demonstration in measured_prompt.load_demonstrations():
        assert in_pool[demonstration["definition"]]["decomposition"] == demonstration["decomposition"]
    assert len(set(fixed_ids())) == 25


def test_the_finders_data_holds_the_pools_variables_and_a_statement_for_each_decision():
    data = finder_data()
    assert [example["id"] for example in data["examples"]] == sorted(variables())
    for example in data["examples"]:
        assert example["definition"] == variables()[example["id"]]["definition"]
    assert sorted(data["statements"]) == sorted(["none", "40", "41", "42", "43", "44", "45"])
    assert data["statements"]["none"].endswith('- hasMatrix: none; use "".')
    assert data["request"] == {"temperature": 0.0, "top_p": 1.0, "max_tokens": 700}
    assert pathlib.Path(settings.measured_finder_path).resolve() == FINDER_PATH.resolve()
    assert pathlib.Path(settings.measured_example_pool_path).resolve() == POOL_PATH.resolve()


# ------------------------------------------------------------------------------------------------
# The examples
# ------------------------------------------------------------------------------------------------


def test_the_25_most_similar_are_shown_with_the_most_similar_last():
    pool_variables = variables()
    for identifier in sorted(pool_variables)[::20]:
        query = pool_variables[identifier]["vector"]
        chosen = list(example_retrieval.choose_examples(query))
        assert chosen == nearest_last(query) and chosen[-1] == identifier
    leave_out = frozenset(nearest_last(query_vector())[-5:])
    chosen = list(example_retrieval.choose_examples(query_vector(), leave_out=leave_out))
    assert chosen == nearest_last(query_vector(), leave_out) and not leave_out & set(chosen)


def test_equal_similarities_go_by_ascending_id():
    """Halfway between a variable and its nearest neighbour, both are equally similar to 9 places."""
    pool_variables = variables()
    first = plain_ids()[0]
    direction = unit(pool_variables[first]["vector"])
    neighbour = max((i for i in pool_variables if i != first),
                    key=lambda i: sum(a * b for a, b in zip(direction, unit(pool_variables[i]["vector"]))))
    query = [a + b for a, b in zip(direction, unit(pool_variables[neighbour]["vector"]))]
    smaller, larger = sorted((first, neighbour))
    chosen = list(example_retrieval.choose_examples(query))
    assert chosen == nearest_last(query) and chosen[-2:] == [larger, smaller]


def test_the_labs_retrieval_and_fallback_prompts_are_rebuilt_byte_for_byte():
    """The lab's PSNC run: retrieved examples with each variable's fold left out, and the fixed 25 without itself."""
    retrieved, fixed = [], []
    for identifier, entry in sorted(variables().items()):
        chosen = list(example_retrieval.choose_examples(entry["vector"], leave_out=fold_of(identifier)))
        retrieved.append(render(entry["definition"], chosen))
        fallback = example_retrieval.fallback_examples(leave_out=frozenset({identifier}))
        fixed.append(measured_prompt.render_measured_prompt(entry["definition"],
                                                            demonstrations=list(fallback.demonstrations)))
    assert digest_of(retrieved) == LAB_PROMPTS["digests"]["rag-25-psnc"] == RETRIEVED_PROMPTS
    assert digest_of(fixed) == LAB_PROMPTS["digests"]["base"] == FIXED_PROMPTS
    everything = example_retrieval.fallback_examples()
    assert (everything.source, list(everything.ids)) == ("fallback", fixed_ids())
    assert measured_prompt.render_measured_prompt(TARGET, demonstrations=list(everything.demonstrations)) == GOLDEN


# ------------------------------------------------------------------------------------------------
# The matrix finder
# ------------------------------------------------------------------------------------------------


def test_the_labs_finder_prompts_are_rebuilt_byte_for_byte():
    """Each corpus variable's finder prompt with its fold left out is the lab's (answer_form_check, named-first)."""
    prompts = {identifier: matrix_finder.finder_prompt(entry["definition"], leave_out=fold_of(identifier))
               for identifier, entry in sorted(variables().items())}
    expected = LAB_CASES["finder_prompts"]["by_variable"]
    assert [i for i in prompts if sha256_text(prompts[i]) != expected[i]] == []
    assert digest_of([prompts[i] for i in sorted(prompts)]) == FINDER_PROMPTS
    whole = matrix_finder.finder_prompt(DEFINITION)
    assert whole.count("\nDefinition: ") == 103 and whole.endswith(f"\nDefinition: {DEFINITION}\nPatterns:")


def test_the_finders_stored_answers_are_read_as_the_lab_read_them():
    for identifier, stored in LAB_CASES["finder_answers"].items():
        expected = None if stored["matrix"] is None else str(stored["matrix"])
        assert matrix_finder.read_answer(stored["answer"]) == expected, identifier


READINGS = [
    ("a matrix pattern", finder_says(44), "44"),
    ("no matrix", finder_says(None), "none"),
    ("the property as text", finder_says(42, prop=str(json.loads(finder_says(None).json()["choices"][0]["message"]["content"])["property"])), "42"),
    ("in a fence with prose", chat("Here:\n```json\n" + finder_says(45).json()["choices"][0]["message"]["content"] + "\n```"), "45"),
    ("a matrix that is no matrix pattern", finder_says(46), None),
    ("a property that is no property pattern", finder_says(41, prop=41), None),
    ("a matrix as text", chat(json.dumps({"property": 130, "matrix": "41", "context": False, "modifier": False,
                                          "constraints": []})), None),
    ("context not a boolean", chat(json.dumps({"property": 130, "matrix": 41, "context": "no", "modifier": False,
                                               "constraints": []})), None),
    ("a key missing", chat(json.dumps({"property": 130, "matrix": 41, "context": False, "modifier": False})), None),
    ("two objects", chat('{"property": 130} {"matrix": 41}'), None),
    ("prose", chat("I cannot classify this."), None),
]


@pytest.mark.parametrize("answer, expected", [case[1:] for case in READINGS], ids=[case[0] for case in READINGS])
def test_the_finders_answer_is_read_strictly(measured, answer, expected):
    assert matrix_finder.read_answer(answer.json()["choices"][0]["message"]["content"]) == expected


def test_the_labs_fill_prompts_with_the_finders_statement_are_rebuilt_byte_for_byte():
    """PSNC's examples (fold left out) and the statement of the finder's stored answer, after the definition."""
    prompts = []
    for identifier, entry in sorted(variables().items()):
        chosen = list(example_retrieval.choose_examples(entry["vector"], leave_out=fold_of(identifier)))
        matrix = matrix_finder.read_answer(LAB_CASES["finder_answers"][identifier]["answer"])
        prompts.append(render(entry["definition"] + matrix_finder.statement(matrix), chosen))
    expected = LAB_CASES["fill_prompts"]["by_variable"]
    assert [i for i, p in zip(sorted(variables()), prompts) if sha256_text(p) != expected[i]] == []
    assert sha256_text("\n".join(sha256_text(p) for p in prompts))[:16] == FILL_PROMPTS


# ------------------------------------------------------------------------------------------------
# The vote
# ------------------------------------------------------------------------------------------------


def test_the_answers_lab_check_matches_the_lab():
    for case in LEXICAL_CASES:
        expected = None if case["problems"] else case["answer"]
        found = answer_vote.lab_answer(json.dumps(case["answer"]))
        assert (found is None) == (expected is None), case["case"]


def test_the_vote_keeps_the_answer_the_labs_scorer_would_keep():
    """306 groups of three stored PSNC answers: the same usable answers, agreements and kept answer as the lab's
    scorer's Exact F1 and the lab's most-typical rule."""
    for case in LAB_CASES["votes"]:
        readings = [answer_vote.lab_answer(text) for text in case["answers"]]
        assert [r is not None for r in readings] == case["usable"], case["variable_id"]
        agreements = [answer_vote.agreement(readings[0], readings[1]), answer_vote.agreement(readings[0], readings[2]),
                      answer_vote.agreement(readings[1], readings[2])]
        assert agreements == case["agreements"], case["variable_id"]
        assert answer_vote.pick(readings) == case["kept"], case["variable_id"]


def test_the_scorers_exact_f1_on_small_cases():
    same = answer_vote.lab_answer(ANSWER)
    far = answer_vote.lab_answer(FAR_ANSWER)
    assert same == VALID
    assert answer_vote.exact_f1(same, same) == 1.0
    assert answer_vote.agreement(same, far) < 0.3
    assert answer_vote.agreement(same, None) == 0.0
    assert answer_vote.pick([far, same, same]) == 1
    assert answer_vote.pick([same, far, same]) == 0
    assert answer_vote.pick([None, None, None]) == 0
    # A tie goes to the first answer the lab could use: the lab's own rule (the first) would keep None here.
    assert answer_vote.pick([None, same, None]) == 1
    assert answer_vote.pick([None, far, same]) == 1


# ------------------------------------------------------------------------------------------------
# The pipeline
# ------------------------------------------------------------------------------------------------


def test_the_examples_and_the_finder_come_first_then_three_answers_side_by_side(psnc):
    query = query_vector()
    session = psnc(**happy(query, fills=[chat(ANSWER, {"prompt_tokens": 5208, "completion_tokens": 90}),
                                         chat(ANSWER), chat(ANSWER)]))
    result, log = run(f"  {DEFINITION}\n")
    assert session.kinds() == ["embedding", "finder", "fill", "fill", "fill", "label"]
    asked = session.of("embedding")[0]
    assert asked.body == {"model": EMBEDDING_MODEL, "input": [DEFINITION]}
    assert (asked.timeout, asked.headers.get("Authorization")) == (5, f"Bearer {KEY}")
    finder = session.of("finder")[0]
    assert set(finder.body) == PAYLOAD_KEYS and finder.timeout == 20
    assert {key: finder.body[key] for key in ("model", "stream", "temperature", "top_p", "max_tokens",
                                             "chat_template_kwargs")} == {
        "model": MODEL, "stream": False, "temperature": 0.0, "top_p": 1.0, "max_tokens": 700,
        "chat_template_kwargs": {"enable_thinking": False}}
    assert finder.body["messages"] == [{"role": "user", "content": matrix_finder.finder_prompt(DEFINITION)}]
    prompt = render(DEFINITION + statement_of(41), nearest_last(query))
    for fill in session.of("fill"):
        assert set(fill.body) == PAYLOAD_KEYS and fill.timeout == 120 and fill.body["stream"] is False
        assert fill.body["messages"] == [{"role": "user", "content": prompt}]
        assert (fill.body["temperature"], fill.body["max_tokens"]) == (0.5, 16000)
    assert set(result) == RESULT_KEYS and result["raw_llm_output"] == ANSWER
    assert result["parsed_json"]["hasMatrix"] == "lake water"
    assert by_fields(log.calls(), "purpose", "attempt", "model", "status", "error", "input_tokens") == [
        ("examples", 1, EMBEDDING_MODEL, "ok", None, 11),
        ("matrix", 1, MODEL, "ok", None, None),
        ("decomposition", 1, MODEL, "ok", None, 5208),
        ("decomposition", 2, MODEL, "unusable", NOT_KEPT, None),
        ("decomposition", 3, MODEL, "unusable", NOT_KEPT, None),
        ("label", 1, MODEL, "ok", None, None),
    ]
    assert log.choices() == {"examples": {"source": "retrieved", "ids": nearest_last(query)}, "matrix": "41",
                             "answers": {"asked": 3, "kept": 1}}


def test_the_requests_go_out_side_by_side(psnc):
    """Neither the embedding and the finder nor the three fill answers wait for each other: each waits for the
    others of its kind to arrive, which only works when they are sent side by side."""
    session = psnc(parallel=True, **happy(fills=[chat(ANSWER), chat(ANSWER), chat(ANSWER)]))
    together = threading.Barrier(2)
    session.barriers = {"embedding": together, "finder": together, "fill": threading.Barrier(3)}
    result, _ = run()
    assert Counter(session.kinds()) == {"embedding": 1, "finder": 1, "fill": 3, "label": 1}
    assert result["raw_llm_output"] == ANSWER


def test_the_most_typical_answer_is_kept(psnc):
    psnc(**happy(fills=[chat(FAR_ANSWER), chat(ANSWER), chat(ANSWER)]))
    result, log = run()
    assert result["raw_llm_output"] == ANSWER and result["parsed_json"]["hasProperty"] == "concentration"
    assert by_fields([c for c in log.calls() if c.purpose == "decomposition"], "attempt", "status", "error") == [
        (1, "unusable", NOT_KEPT), (2, "ok", None), (3, "unusable", NOT_KEPT)]
    assert log.choices()["answers"] == {"asked": 3, "kept": 2}


def test_answers_that_cannot_be_used_lose_the_vote(psnc):
    psnc(**happy(fills=[chat("I cannot do that."), Answer(status=503), chat(ANSWER)]))
    result, log = run()
    assert result["raw_llm_output"] == ANSWER
    calls = [c for c in log.calls() if c.purpose == "decomposition"]
    assert by_fields(calls, "attempt", "status") == [(1, "unusable"), (2, "error"), (3, "ok")]
    assert calls[0].error.startswith("Not valid JSON") and calls[1].error == "HTTP 503"
    assert log.choices()["answers"] == {"asked": 3, "kept": 3}


def test_a_tie_keeps_an_answer_the_lab_could_use(psnc):
    """The first answer reads as JSON but breaks the lab's schema, the third never came: every agreement is 0, and
    the tie goes to the answer the lab's check accepts, not to the first."""
    broken = json.dumps({**VALID, "hasUnit": "mg/L"}, indent=2)
    assert answer_vote.lab_answer(broken) is None
    psnc(**happy(fills=[chat(broken), chat(ANSWER), Answer(status=500)]))
    result, log = run()
    assert result["raw_llm_output"] == ANSWER and result["schema_valid"] is True
    assert by_fields([c for c in log.calls() if c.purpose == "decomposition"], "attempt", "status", "error") == [
        (1, "unusable", NOT_KEPT), (2, "ok", None), (3, "error", "HTTP 500")]
    assert log.choices()["answers"] == {"asked": 3, "kept": 2}


def test_a_kept_answer_that_cannot_be_used_is_not_recorded_as_kept(psnc, monkeypatch):
    psnc(**happy(fills=[chat(ANSWER)] * 3))
    monkeypatch.setattr(pipeline, "_finalize_pipeline_output",
                        lambda *args, **kwargs: (_ for _ in ()).throw(KeyError("hasProperty")))
    log = CallLog()
    with pytest.raises(KeyError):
        pipeline.run_pipeline(DEFINITION, model_provider="psnc", calls=log)
    assert by_fields([c for c in log.calls() if c.purpose == "decomposition"], "attempt", "status", "error") == [
        (1, "unusable", "The answer could not be used: KeyError"), (2, "unusable", NOT_KEPT), (3, "unusable", NOT_KEPT)]
    assert log.choices()["answers"] == {"asked": 3, "kept": None}


def test_when_no_answer_can_be_read_todays_tries_follow(psnc):
    session = psnc(**happy(fills=[chat("no"), chat(""), Answer(status=500), chat(ANSWER)]))
    result, log = run()
    assert session.kinds() == ["embedding", "finder", "fill", "fill", "fill", "fill", "label"]
    assert session.of("fill")[3].body == session.of("fill")[0].body
    assert result["raw_llm_output"] == ANSWER
    assert by_fields([c for c in log.calls() if c.purpose == "decomposition"], "attempt", "status") == [
        (1, "unusable"), (2, "unusable"), (3, "error"), (4, "ok")]
    assert log.choices()["answers"] == {"asked": 3, "kept": None}


FINDER_FAILURES = [
    ("the timeout", requests.Timeout("read timed out"), "error", FINDER_TIMED_OUT),
    ("no connection", requests.ConnectionError("refused"), "error", FINDER_NO_CONNECTION),
    *[(f"HTTP {status}", Answer(status=status), "error", refused("matrix finder", status)) for status in (401, 429, 503, 404)],
    ("a body that is not JSON", Answer(NOT_JSON), "error", NOT_A_COMPLETION),
    ("a body nested too deeply", Answer(TOO_DEEP), "error", NOT_A_COMPLETION),
    ("no choices", Answer({"object": "chat.completion", "choices": []}), "error", NOT_A_COMPLETION),
    ("prose", chat("The matrix is the lake."), "unusable", NOT_REQUESTED),
    ("a matrix that is no pattern", finder_says(46), "unusable", NOT_REQUESTED),
]


@pytest.mark.parametrize("outcome, status, error", [c[1:] for c in FINDER_FAILURES], ids=[c[0] for c in FINDER_FAILURES])
def test_without_a_usable_finder_answer_nothing_is_stated_and_the_run_goes_on(psnc, outcome, status, error):
    query = query_vector()
    session = psnc(embedding=[embedding(query)], finder=[outcome], fill=[chat(ANSWER)] * 3, label=[labelled()])
    result, log = run()
    assert len(session.of("finder")) == 1
    assert session.of("fill")[0].body["messages"][0]["content"] == render(DEFINITION, nearest_last(query))
    assert result["raw_llm_output"] == ANSWER
    assert by_fields(log.calls(), "purpose", "status", "error")[1] == ("matrix", status, error)
    assert log.choices()["matrix"] is None


EMBEDDING_FAILURES = [
    ("the timeout", requests.Timeout("read timed out"), "error", TIMED_OUT),
    ("no connection", requests.ConnectionError("connection refused"), "error", NO_CONNECTION),
    *[(f"HTTP {status}", Answer(status=status), "error", refused("embedding request", status))
      for status in (401, 403, 429, 500, 503, 404)],
    ("HTTP 204", Answer(embedding([0.5] * DIMENSIONS).json(), status=204), "error", refused("embedding request", 204)),
    ("a body that is not JSON", Answer(NOT_JSON), "unusable", NO_VECTOR),
    ("a body nested too deeply", Answer(TOO_DEEP), "unusable", NO_VECTOR),
    ("no vector", Answer({"data": []}), "unusable", NO_VECTOR),
    ("two vectors", Answer({"data": [{"index": 0, "embedding": [0.5] * DIMENSIONS},
                                     {"index": 1, "embedding": [0.5] * DIMENSIONS}]}), "unusable", TWO_VECTORS),
    ("a vector of words", embedding(["word"] * DIMENSIONS), "unusable", NOT_A_NUMBER),
    ("768 numbers", embedding([0.5] * 768), "unusable", WRONG_SIZE),
    ("a number that is not finite", embedding([float("inf")] + [0.5] * (DIMENSIONS - 1)), "unusable", NOT_FINITE),
    ("an integer too large for a float", embedding([10 ** 400] + [0.5] * (DIMENSIONS - 1)), "unusable", NOT_FINITE),
    ("numbers whose length is not finite", embedding([10 ** 200] * DIMENSIONS), "unusable", NOT_FINITE),
    ("length zero", embedding([0.0] * DIMENSIONS), "unusable", ZERO_LENGTH),
]


@pytest.mark.parametrize("outcome, status, error", [c[1:] for c in EMBEDDING_FAILURES],
                         ids=[c[0] for c in EMBEDDING_FAILURES])
def test_without_a_usable_vector_the_fixed_examples_are_shown_and_the_run_goes_on(psnc, outcome, status, error):
    """With the finder stating nothing, the prompt is today's: for the fixture target, the golden."""
    session = psnc(embedding=[outcome], finder=[chat("no answer")], fill=[chat(ANSWER)] * 3, label=[labelled()])
    result, log = run(TARGET)
    assert len(session.of("embedding")) == 1
    assert session.of("fill")[0].body["messages"] == [{"role": "user", "content": GOLDEN}]
    assert result["raw_llm_output"] == ANSWER
    assert by_fields(log.calls(), "purpose", "model", "status", "error")[0] == ("examples", EMBEDDING_MODEL, status, error)
    assert log.choices()["examples"] == {"source": "fallback", "ids": fixed_ids()}


def test_the_fixed_examples_still_get_the_finders_statement(psnc):
    session = psnc(embedding=[Answer(status=503)], finder=[finder_says(41)], fill=[chat(ANSWER)] * 3,
                   label=[labelled()])
    _, log = run()
    fixed = list(example_retrieval.fallback_examples().demonstrations)
    assert session.of("fill")[0].body["messages"][0]["content"] == measured_prompt.render_measured_prompt(
        DEFINITION + statement_of(41), demonstrations=fixed)
    assert log.choices()["examples"]["source"] == "fallback" and log.choices()["matrix"] == "41"


def test_an_error_neither_request_foresaw_still_does_not_fail_the_run(psnc, monkeypatch):
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("nobody foresaw this")

    query = query_vector()
    session = psnc(embedding=[embedding(query)], fill=[chat(ANSWER)] * 3, label=[labelled()])
    monkeypatch.setattr(matrix_finder, "find_matrix", broken)
    result, log = run()
    assert result["raw_llm_output"] == ANSWER
    assert session.of("fill")[0].body["messages"][0]["content"] == render(DEFINITION, nearest_last(query))
    assert by_fields(log.calls(), "purpose", "status")[0] == ("examples", "ok")
    assert log.choices()["matrix"] is None


def test_without_a_psnc_key_nothing_is_sent_and_the_failures_are_named(psnc, monkeypatch):
    session = psnc()
    monkeypatch.setattr(settings, "psnc_api_key", None)
    log = CallLog()
    with pytest.raises(RuntimeError, match="PSNC_API_KEY is not set"):
        pipeline.run_pipeline(DEFINITION, model_provider="psnc", calls=log)
    assert session.requests == []
    assert by_fields(log.calls(), "purpose", "status", "error") == [
        ("examples", "error", NO_KEY_EMBEDDING), ("matrix", "error", NO_KEY_FINDER)]


def test_no_failure_text_holds_the_key(psnc, monkeypatch):
    leaked = requests.ConnectionError(f"Connection broken; headers {{'Authorization': 'Bearer {KEY}'}}")
    psnc(embedding=[leaked], finder=[leaked], fill=[chat(ANSWER)] * 3, label=[labelled()])
    _, log = run()
    assert [c.error for c in log.calls() if c.error and c.purpose in ("examples", "matrix")] == [
        NO_CONNECTION, FINDER_NO_CONNECTION]
    monkeypatch.setattr(settings, "psnc_api_key", "a\nb")
    log = CallLog()
    with pytest.raises(RuntimeError):
        pipeline.run_pipeline(DEFINITION, model_provider="psnc", calls=log)
    assert all("a\nb" not in (c.error or "") for c in log.calls())
    assert log.calls()[0].error.startswith("embedding request not sent: PSNC_API_KEY holds a character")


def test_the_gate_leave_out_keeps_a_variable_out_of_both_example_lists(psnc):
    target = plain_ids()[0]
    entry = variables()[target]
    session = psnc(embedding=[embedding(entry["vector"])], finder=[finder_says(None)], fill=[chat(ANSWER)] * 3,
                   label=[labelled()])
    pipeline.run_pipeline(entry["definition"], model_provider="psnc", leave_out=frozenset({target}))
    finder_prompt = session.of("finder")[0].body["messages"][0]["content"]
    assert f"Definition: {entry['definition']}\nDecomposition:" not in finder_prompt
    assert finder_prompt == matrix_finder.finder_prompt(entry["definition"], leave_out=frozenset({target}))
    assert session.of("fill")[0].body["messages"][0]["content"] == render(
        entry["definition"] + statement_of(None), nearest_last(entry["vector"], frozenset({target})))


# --- the stream ------------------------------------------------------------------------------- #


def test_the_stream_streams_the_first_answer_and_keeps_it_when_typical(psnc):
    query = query_vector()
    session = psnc(**happy(query, fills=[chat(ANSWER), chat(ANSWER)], stream=[streamed(ANSWER)]))
    events, log = stream()
    assert session.kinds() == ["embedding", "finder", "fill", "fill", "stream", "label"]
    assert session.of("stream")[0].body["messages"][0]["content"] == render(DEFINITION + statement_of(41),
                                                                             nearest_last(query))
    assert [e["type"] for e in events] == ["raw_delta", "final"] and events[0]["delta"] == ANSWER
    assert set(events[-1]["data"]) == RESULT_KEYS and events[-1]["data"]["raw_llm_output"] == ANSWER
    decomposition = [c for c in log.calls() if c.purpose == "decomposition"]
    assert by_fields(decomposition, "attempt", "status") == [(1, "ok"), (2, "unusable"), (3, "unusable")]
    assert log.choices()["answers"] == {"asked": 3, "kept": 1}


def test_the_stream_shows_another_kept_answer_after_a_note(psnc):
    psnc(**happy(fills=[chat(ANSWER), chat(ANSWER)], stream=[streamed(FAR_ANSWER, reasoning="Air, I think.")]))
    events, log = stream()
    note = pipeline.VOTE_NOTE.format(kept=2, asked=3)
    assert [e.get("delta") for e in events[:-1]] == ["Air, I think.", "\n\n", FAR_ANSWER, note + ANSWER]
    final = events[-1]["data"]
    assert final["raw_llm_output"] == "Air, I think.\n\n" + FAR_ANSWER + note + ANSWER
    assert final["parsed_json"]["hasProperty"] == "concentration"
    assert log.choices()["answers"] == {"asked": 3, "kept": 2}


def test_a_stream_that_fails_still_keeps_a_background_answer(psnc):
    psnc(**happy(fills=[chat(ANSWER), chat(ANSWER)], stream=[Answer(status=503)]))
    events, log = stream()
    assert [e["type"] for e in events] == ["raw_delta", "final"]
    assert events[0]["delta"] == pipeline.VOTE_NOTE.format(kept=2, asked=3) + ANSWER
    decomposition = [c for c in log.calls() if c.purpose == "decomposition"]
    assert by_fields(decomposition, "attempt", "status", "error") == [
        (1, "error", "HTTP 503"), (2, "ok", None), (3, "unusable", NOT_KEPT)]


def test_a_stream_whose_kept_answer_cannot_be_used_goes_on_and_records_no_kept_answer(psnc, monkeypatch):
    psnc(embedding=[embedding(query_vector())], finder=[finder_says(41)], fill=[chat(ANSWER)] * 2,
         stream=[streamed(ANSWER), streamed(ANSWER)], label=[labelled(), labelled()])
    original = pipeline._finalize_pipeline_output
    failures = [KeyError("hasProperty")]

    def finalize(*args: Any, **kwargs: Any) -> Any:
        if failures:
            raise failures.pop()
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_finalize_pipeline_output", finalize)
    events, log = stream()
    assert events[-1]["type"] == "final" and events[-1]["data"]["parsed_json"]["hasMatrix"] == "lake water"
    assert by_fields([c for c in log.calls() if c.purpose == "decomposition"], "attempt", "status") == [
        (1, "unusable"), (2, "unusable"), (3, "unusable"), (4, "ok")]
    assert log.choices()["answers"] == {"asked": 3, "kept": None}


def test_a_client_who_leaves_mid_stream_leaves_no_call_unrecorded(psnc):
    """The two background answers had come: both are recorded as not used."""
    session = psnc(**happy(fills=[chat(ANSWER), chat(ANSWER)], stream=[streamed(ANSWER)]))
    log = CallLog()
    events = pipeline.stream_pipeline_events(DEFINITION, model_provider="psnc", calls=log)
    assert json.loads(next(events))["type"] == "raw_delta"
    events.close()
    assert by_fields(log.calls(), "purpose", "attempt", "status", "error")[2:] == [
        ("decomposition", 1, "error", STREAM_CLOSED), ("decomposition", 2, "unusable", CLOSED_BEFORE_VOTE),
        ("decomposition", 3, "unusable", CLOSED_BEFORE_VOTE)]
    assert session.kinds() == ["embedding", "finder", "fill", "fill", "stream"]


def test_a_client_who_leaves_while_answers_are_on_their_way_leaves_no_call_unrecorded(psnc, monkeypatch):
    """The two background answers are still on their way: their calls end with the stream, and nothing is recorded
    twice when they come."""
    pools: List[concurrent.futures.ThreadPoolExecutor] = []

    class Recorded(concurrent.futures.ThreadPoolExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            pools.append(self)

    session = psnc(parallel=True, **happy(fills=[chat(ANSWER), chat(ANSWER)], stream=[streamed(ANSWER)]))
    monkeypatch.setattr(pipeline, "ThreadPoolExecutor", Recorded)
    session.holds["fill"] = threading.Event()
    log = CallLog()
    events = pipeline.stream_pipeline_events(DEFINITION, model_provider="psnc", calls=log)
    try:
        assert json.loads(next(events))["type"] == "raw_delta"
        session.wait_for("fill", 2)
        events.close()
        closed = by_fields(log.calls(), "purpose", "attempt", "status", "error")
        assert closed[2:] == [("decomposition", attempt, "error", STREAM_CLOSED) for attempt in (1, 2, 3)]
    finally:
        session.holds["fill"].set()
        for pool in pools:
            pool.shutdown(wait=True)
    assert by_fields(log.calls(), "purpose", "attempt", "status", "error") == closed
    assert Counter(session.kinds()) == {"embedding": 1, "finder": 1, "fill": 2, "stream": 1}


# --- the legacy path, OpenRouter, the switch the other tests use ------------------------------------ #


def test_the_legacy_path_sends_one_request_and_chooses_nothing(psnc, monkeypatch):
    monkeypatch.setattr(settings, "use_measured_configuration", False)
    session = psnc(label=[chat(ANSWER)])  # the legacy body carries enable_thinking
    result, log = run()
    assert session.kinds() == ["label"]
    assert session.requests[0].body["messages"] == [{"role": "user", "content": legacy_prompt(DEFINITION)}]
    assert result["raw_llm_output"] == ANSWER and log.choices() is None
    assert by_fields(log.calls(), "purpose", "status") == [("decomposition", "ok")]


def test_openrouter_gets_no_psnc_request(psnc, monkeypatch):
    session = psnc()
    seen: List[Dict[str, Any]] = []

    def create(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=ANSWER))], usage=None)

    monkeypatch.setattr(llm, "get_openai_client", lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    log = CallLog()
    result = pipeline.run_pipeline(DEFINITION, model_provider="openrouter", calls=log)
    assert session.requests == [] and len(seen) == 1 and result["raw_llm_output"] == ANSWER
    assert [c.purpose for c in log.calls()] == ["decomposition"] and log.choices() is None


def test_the_switch_the_other_tests_use_reaches_both_routes(psnc, monkeypatch):
    """Switched off, neither route asks for examples or the finder and the fill goes out once: today's prompt."""
    switch_best_pipeline_off(monkeypatch)
    session = psnc(fill=[chat(ANSWER)], label=[labelled()])
    result, log = run(TARGET)
    assert session.kinds() == ["fill", "label"]
    assert session.requests[0].body["messages"] == [{"role": "user", "content": GOLDEN}]
    assert [c.purpose for c in log.calls()] == ["decomposition", "label"]
    session = psnc(stream=[streamed(ANSWER)], label=[labelled()])
    events, log = stream(TARGET)
    assert session.kinds() == ["stream", "label"] and [e["type"] for e in events] == ["raw_delta", "final"]


# ------------------------------------------------------------------------------------------------
# The setup, the start
# ------------------------------------------------------------------------------------------------


def setup_fingerprint() -> str:
    canonical = {
        "fallback_prompt_sha256": sha256_text(measured_prompt.render_measured_prompt("{definition}")),
        "pool_sha256": hashlib.sha256(POOL_PATH.read_bytes()).hexdigest(),
        "finder_sha256": hashlib.sha256(FINDER_PATH.read_bytes()).hexdigest(),
        "finder_request": {"temperature": 0.0, "top_p": 1.0, "max_tokens": 700},
        "embedding_model": EMBEDDING_MODEL, "examples": 25, "order": "most similar last", "answers": 3,
        "vote": VOTE_RULE,
    }
    return sha256_text(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def test_the_setup_is_named_and_fingerprinted_without_a_request(psnc):
    session = psnc()
    assert pipeline.prompt_setup("psnc") == (SETUP, setup_fingerprint())
    assert session.requests == []


def test_the_fingerprint_follows_the_prompt_the_pool_and_the_finder(psnc, monkeypatch, tmp_path, rewarm):
    psnc()
    changed = {
        "measured_template_path": settings.measured_template_path.read_text(encoding="utf-8") + "\n",
        "measured_example_pool_path": json.dumps(pool(), ensure_ascii=False, indent=1),
        "measured_finder_path": json.dumps(finder_data(), ensure_ascii=False),
    }
    seen = {pipeline.prompt_setup("psnc")[1]}
    for setting, text in changed.items():
        path = tmp_path / f"{setting}.json"
        path.write_text(text, encoding="utf-8")
        with monkeypatch.context() as patched:
            patched.setattr(type(settings), setting, property(lambda self, path=path: path), raising=False)
            pipeline.warmup_assets()
            setup, fingerprint = pipeline.prompt_setup("psnc")
        assert setup == SETUP and fingerprint not in seen, setting
        seen.add(fingerprint)


def test_the_legacy_setup_is_unchanged(psnc, monkeypatch):
    session = psnc()
    expected = (f"few-shot:{list_prompt_versions(settings.prompt_dir)[0]}", sha256_text(legacy_prompt("{definition}")))
    assert pipeline.prompt_setup("openrouter") == expected
    monkeypatch.setattr(settings, "use_measured_configuration", False)
    assert pipeline.prompt_setup("psnc") == expected and session.requests == []


def test_a_broken_pool_or_finder_data_stops_the_start_and_says_what_to_do(measured, monkeypatch, tmp_path, rewarm):
    pipeline.warmup_assets()
    garbled = tmp_path / "garbled.json"
    garbled.write_text('{"variables": [', encoding="utf-8")
    short_pool = tmp_path / "short-pool.json"
    data = copy.deepcopy(pool())
    data["variables"].pop()
    short_pool.write_text(json.dumps(data), encoding="utf-8")
    cases = [
        ("measured_example_pool_path", tmp_path / "absent.json", "Example pool {} is missing. " + REBUILD_POOL),
        ("measured_example_pool_path", garbled, "Example pool {} is not JSON. " + REBUILD_POOL),
        ("measured_example_pool_path", short_pool,
         "Example pool {} breaks a rule: holds 101 variables, not 102. " + REBUILD_POOL),
        ("measured_finder_path", tmp_path / "absent.json", "Matrix finder data {} is missing. " + REBUILD_FINDER),
        ("measured_finder_path", garbled, "Matrix finder data {} is not JSON. " + REBUILD_FINDER),
    ]
    other_finder = finder_data()
    other_finder["examples"] = other_finder["examples"][1:] + [dict(other_finder["examples"][0], id="urn:other")]
    other = tmp_path / "other-finder.json"
    other.write_text(json.dumps(other_finder), encoding="utf-8")
    no_statement = finder_data()
    del no_statement["statements"]["43"]
    missing = tmp_path / "no-statement.json"
    missing.write_text(json.dumps(no_statement), encoding="utf-8")
    cases += [
        ("measured_finder_path", other,
         "Matrix finder data {} breaks a rule: its variables are not the example pool's. " + REBUILD_FINDER),
        ("measured_finder_path", missing, "Matrix finder data {} breaks a rule: it does not hold a statement for none "
                                          "and for each of the matrix patterns 40 to 45. " + REBUILD_FINDER),
    ]
    for setting, path, message in cases:
        with monkeypatch.context() as patched:
            patched.setattr(type(settings), setting, property(lambda self, path=path: path), raising=False)
            with pytest.raises(RuntimeError) as raised:
                pipeline.warmup_assets()
        assert str(raised.value) == message.format(path), message


def test_with_the_measured_configuration_off_neither_file_is_read(measured, monkeypatch, tmp_path, rewarm):
    monkeypatch.setattr(settings, "use_measured_configuration", False)
    point_at(monkeypatch, "measured_example_pool_path", tmp_path / "absent.json")
    point_at(monkeypatch, "measured_finder_path", tmp_path / "absent.json")
    pipeline.warmup_assets()


# ------------------------------------------------------------------------------------------------
# The record, the export, the migration, the daily limit
# ------------------------------------------------------------------------------------------------


class RecordedChoicesTests(RecordTestCase):
    """The decompose routes with the real pipeline on the measured path and a fake PSNC."""

    def setUp(self) -> None:
        super().setUp()
        for name, value in MEASURED_SETTINGS.items():
            self.replace(settings, name, value)
        self.replace(pipeline, "ThreadPoolExecutor", SerialPool)
        pipeline.warmup_assets()
        self.places = session_places()
        self.login()

    def replace(self, target: Any, name: str, value: Any) -> None:
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def psnc(self, **scripts: Any) -> Psnc:
        session = Psnc(**scripts)
        for module in self.places:
            self.replace(module, "get_http_session", lambda: session)
        return session

    def decompose(self, route: str = "/api/decompose", status: int = 200) -> Any:
        response = self.client.post(route, json={"definition": DEFINITION, "model_provider": "psnc", "client": CLIENT})
        self.assertEqual(response.status_code, status, response.text)
        return response

    def recorded(self) -> List[Optional[Dict[str, Any]]]:
        rows = self.rows("SELECT choices FROM record_runs ORDER BY item_id")
        return [None if row["choices"] is None else json.loads(row["choices"]) for row in rows]

    def test_a_run_keeps_what_it_chose(self):
        query = query_vector()
        self.psnc(**happy(query, fills=[chat(FAR_ANSWER), chat(ANSWER), chat(ANSWER)]))
        response = self.decompose()
        self.assertEqual(set(response.json()), RESULT_KEYS | {"record_id"})
        self.assertEqual(self.recorded(), [{"examples": {"source": "retrieved", "ids": nearest_last(query)},
                                            "matrix": "41", "answers": {"asked": 3, "kept": 2}}])
        calls = self.rows("SELECT purpose, attempt, status FROM record_llm_calls ORDER BY id")
        self.assertEqual([(c["purpose"], c["attempt"], c["status"]) for c in calls], [
            ("examples", 1, "ok"), ("matrix", 1, "ok"), ("decomposition", 1, "unusable"), ("decomposition", 2, "ok"),
            ("decomposition", 3, "unusable"), ("label", 1, "ok")])

    def test_a_streamed_run_keeps_it_too(self):
        query = query_vector()
        self.psnc(**happy(query, fills=[chat(ANSWER), chat(ANSWER)], stream=[streamed(ANSWER)]))
        response = self.decompose("/api/decompose/stream")
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        self.assertEqual([e["type"] for e in events], ["raw_delta", "final"])
        self.assertEqual(set(events[-1]["data"]), RESULT_KEYS | {"record_id"})
        self.assertEqual(self.recorded()[0]["answers"], {"asked": 3, "kept": 1})

    def test_a_failed_run_keeps_its_examples_and_matrix(self):
        query = query_vector()
        self.psnc(embedding=[embedding(query)], finder=[finder_says(None)], fill=[Answer(status=503)] * 12)
        self.decompose(status=500)
        [run] = self.rows("SELECT status, choices FROM record_runs")
        self.assertEqual(run["status"], "error")
        self.assertEqual(json.loads(run["choices"]), {"examples": {"source": "retrieved", "ids": nearest_last(query)},
                                                      "matrix": "none", "answers": {"asked": 3, "kept": None}})

    def test_the_legacy_path_keeps_no_choices(self):
        self.replace(settings, "use_measured_configuration", False)
        self.psnc(label=[chat(ANSWER)])
        self.decompose()
        self.assertEqual(self.recorded(), [None])

    def test_the_export_carries_the_choices_and_its_readme_says_what_they_are(self):
        query = query_vector()
        self.psnc(**happy(query, fills=[chat(ANSWER)] * 3))
        best = self.decompose().json()["record_id"]
        self.replace(settings, "use_measured_configuration", False)
        self.psnc(label=[chat(ANSWER)])
        legacy = self.decompose().json()["record_id"]
        self.client.post("/api/auth/logout")
        self.login("admin", "admin-password")
        response = self.client.get("/api/admin/records/export")
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            files = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}
        document = json.loads(files["records.jsonld"])
        records = {node["@id"].rsplit("/", 1)[-1]: node for node in document["@graph"]}
        self.assertEqual(records[best]["modelRun"]["choices"], {
            "examples": {"source": "retrieved", "ids": nearest_last(query)}, "matrix": "41",
            "answers": {"asked": 3, "kept": 1}})
        self.assertNotIn("choices", records[legacy]["modelRun"])
        self.assertEqual(document["@context"]["choices"], {"@id": "rec:choices", "@type": "@json"})
        for word in ("choices", "retrieved", "fallback", "finder"):
            self.assertIn(word, files["README.txt"])


class DailyLimitTests(LimitTestCase):
    """The limit switched on; somebody whose account is not approved decomposes."""

    def setUp(self) -> None:
        super().setUp()
        self.switch("1000")
        for name, value in MEASURED_SETTINGS.items():
            self.replace(settings, name, value)
        self.replace(pipeline, "ThreadPoolExecutor", SerialPool)
        pipeline.warmup_assets()
        self.ada = self.person()

    def test_the_extra_requests_take_no_use_of_their_own(self):
        session = Psnc(**happy(fills=[chat(ANSWER)] * 3))
        for module in session_places():
            self.replace(module, "get_http_session", lambda: session)
        before = self.allowance(self.ada)[0]
        response = self.post(self.ada, JSON_ROUTE, {**LIMITED_BODY, "model_provider": "psnc"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(session.requests), 6)
        self.assertEqual(self.ends(), [("success", 0)])
        self.assertEqual(before - self.allowance(self.ada)[0], 1)


class RecordChoicesMigrationTests(unittest.TestCase):
    """The previous image is the live ``c103d4d``; its record statements are those of the ``de1a7af`` capture (the
    INSERT of ``record_runs`` and the export's queries did not change between them)."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = pathlib.Path(temporary.name)
        saved = list(storage.MIGRATIONS)
        self.addCleanup(lambda: storage.MIGRATIONS.__setitem__(slice(None), saved))

    def rows(self, sql: str, db: pathlib.Path) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql)]
        finally:
            conn.close()

    def test_it_follows_0006_and_adds_one_empty_column_that_the_previous_image_does_not_mind(self):
        ids = [item.id for item in storage.MIGRATIONS]
        index = ids.index(MIGRATION)
        self.assertEqual(ids[index - 1], "0006_account_approval")
        migration = storage.MIGRATIONS[index]
        self.assertEqual(migration.checksum, hashlib.sha256(migration.sql.encode("utf-8")).hexdigest())
        old = self.dir / "previous.sqlite3"
        previous_image.make_de1a7af_file(old)
        del storage.MIGRATIONS[index:]  # the previous image's migrations: up to 0006
        storage.migrate(old)
        others = "SELECT type, name, sql FROM sqlite_master WHERE name <> 'record_runs' ORDER BY type, name"
        schema = self.rows(others, old)
        runs_sql = "SELECT sql FROM sqlite_master WHERE name = 'record_runs'"
        [stored] = self.rows(runs_sql, old)
        columns = self.rows("PRAGMA table_info(record_runs)", old)
        tables = [row["name"] for row in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'", old)]
        before = {table: self.rows(f"SELECT rowid, * FROM {table}", old)
                  for table in tables if table not in ("schema_migrations", "sqlite_sequence")}
        self.assertTrue(before["record_runs"])
        storage.MIGRATIONS.append(migration)
        self.assertEqual(storage.migrate(old), [MIGRATION])
        self.assertEqual(self.rows(others, old), schema)
        self.assertEqual(self.rows(runs_sql, old), [{"sql": stored["sql"][:-1] + ", choices TEXT)"}])
        added = self.rows("PRAGMA table_info(record_runs)", old)
        self.assertEqual(added[: len(columns)], columns)
        self.assertEqual([(row["name"], row["type"], row["notnull"], row["dflt_value"]) for row in added[len(columns):]],
                         [("choices", "TEXT", 0, None)])
        after = {table: self.rows(f"SELECT rowid, * FROM {table}", old) for table in before}
        self.assertEqual([row.pop("choices") for row in after["record_runs"]], [None] * len(before["record_runs"]))
        self.assertEqual(after, before)
        self.assertEqual(previous_image.start(old), [])
        self.assertEqual(previous_image.replay(old, include=previous_image.is_read), [])
        fresh = self.dir / "fresh.sqlite3"
        storage.migrate(fresh)
        self.assertEqual(previous_image.replay(fresh), [])


# ------------------------------------------------------------------------------------------------
# The release quality gate
# ------------------------------------------------------------------------------------------------


GATE_FIELDS = ("prompt_tokens", "examples", "examples_error", "matrix", "matrix_status", "matrix_error", "answers_kept")
SERVER_ERROR = refused("embedding request", 503)
WRONG = json.dumps({"hasProperty": "something else"})


def gate_line(record: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    """A prediction line of the container: retrieved, a matrix stated, the first answer kept, unless told otherwise."""
    return {**gate_answer(record), "prompt_tokens": 5208, "examples": "retrieved", "examples_error": None,
            "matrix": "none", "matrix_status": "ok", "matrix_error": None, "answers_kept": 1, **fields}


def test_the_gate_measures_the_best_pipeline():
    assert qg.RELEASE_BACKEND["prompt_setup"] == SETUP == RUN["prompt_setup"]


def test_the_container_leaves_each_scored_variable_out(monkeypatch):
    seen: List[Tuple[str, FrozenSet[str]]] = []

    def answered(definition: str, calls: Optional[CallLog] = None, leave_out: Any = frozenset(), **_: Any) -> Dict[str, Any]:
        seen.append((definition, frozenset(leave_out)))
        start_call(calls, "decomposition", "psnc", MODEL).finish("ok", usage={"prompt_tokens": 5208})
        return {"raw_llm_output": "{}"}

    monkeypatch.setattr(pipeline, "run_pipeline", answered)
    container.decompose_one({"variable_id": "urn:iadopt-lab:variable:abc", "definition": "d"})
    assert seen == [("d", frozenset({"urn:iadopt-lab:variable:abc"}))]


def test_the_container_reports_what_each_variable_got(psnc):
    target = plain_ids()[0]
    entry = variables()[target]
    psnc(embedding=[embedding(entry["vector"])], finder=[finder_says(44)],
         fill=[Answer(status=503), chat(ANSWER, {"prompt_tokens": 5100, "completion_tokens": 9}),
               chat(FAR_ANSWER, {"prompt_tokens": 5101, "completion_tokens": 9})],
         label=[labelled()])
    line = container.decompose_one({"variable_id": target, "definition": entry["definition"]})
    assert {key: line[key] for key in ("ok", *GATE_FIELDS)} == {
        "ok": True, "prompt_tokens": 5100, "examples": "retrieved", "examples_error": None, "matrix": "44",
        "matrix_status": "ok", "matrix_error": None, "answers_kept": 2}
    psnc(embedding=[Answer(status=503)], finder=[requests.Timeout("slow")], fill=[chat(ANSWER)] * 3, label=[labelled()])
    line = container.decompose_one({"variable_id": target, "definition": entry["definition"]})
    assert {key: line[key] for key in GATE_FIELDS[1:]} == {
        "examples": "fallback", "examples_error": SERVER_ERROR, "matrix": None, "matrix_status": "error",
        "matrix_error": FINDER_TIMED_OUT, "answers_kept": 1}


def test_the_container_lists_the_fixed_examples_for_the_best_pipeline(monkeypatch):
    monkeypatch.setattr(measured_prompt, "load_demonstrations", lambda: [{"definition": "shown one"}])
    monkeypatch.setattr(pipeline, "prompt_setup", lambda provider=None: (SETUP, "f" * 64))
    assert container.run_description()["example_definitions"] == ["shown one"]


def test_the_gate_prints_what_was_chosen(tmp_path, capsys):
    records = make_records()
    predictions = {r["variable_id"]: gate_line(r) for r in records[:65]}
    predictions.update({r["variable_id"]: gate_line(r, raw_output=WRONG) for r in records[65:]})
    for index, error in ((30, TIMED_OUT), (31, TIMED_OUT), (70, SERVER_ERROR)):
        predictions[records[index]["variable_id"]].update(examples="fallback", examples_error=error)
    for index in (40, 41):
        predictions[records[index]["variable_id"]].update(matrix="41")
    predictions[records[42]["variable_id"]].update(matrix=None, matrix_status="error", matrix_error=FINDER_TIMED_OUT)
    predictions[records[43]["variable_id"]].update(matrix=None, matrix_status="unusable", matrix_error=NOT_REQUESTED)
    for index in (50, 51):
        predictions[records[index]["variable_id"]].update(answers_kept=2)
    result = run_main(tmp_path, records, predictions)
    assert (result.code, result.report["verdict"]) == (qg.EXIT_PASS, "pass")
    lines = capsys.readouterr().out.splitlines()
    for line in ("  the 25 fixed examples: 1.0000", "  the other 77: 0.5195",
                 "Examples: 99 retrieved, 3 fell back to the 25 fixed examples", f"  2 {TIMED_OUT}",
                 f"  1 {SERVER_ERROR}",
                 "Matrix finder: 100 stated (none 98, 41 2), 1 not the requested JSON, 1 without an answer",
                 f"  1 {FINDER_TIMED_OUT}", "Kept answer: the first 100, the second 2"):
        assert line in lines, line
    rows = {row["variable_id"]: row for row in result.report["rows"]}
    assert {key: rows["v030"][key] for key in GATE_FIELDS} == {
        "prompt_tokens": 5208, "examples": "fallback", "examples_error": TIMED_OUT, "matrix": "none",
        "matrix_status": "ok", "matrix_error": None, "answers_kept": 1}


def test_a_run_below_the_bar_with_psnc_failures_is_undecided_and_says_why(tmp_path, capsys):
    records = make_records()
    predictions = {r["variable_id"]: gate_line(r) for r in records[:50]}
    predictions.update({r["variable_id"]: gate_line(r, raw_output=WRONG) for r in records[50:]})
    predictions[records[60]["variable_id"]].update(examples="fallback", examples_error=TIMED_OUT)
    predictions[records[61]["variable_id"]].update(matrix=None, matrix_status="error", matrix_error=FINDER_TIMED_OUT)
    result = run_main(tmp_path, records, predictions)
    assert (result.code, result.report["verdict"]) == (qg.EXIT_ERROR, "undecided")
    [verdict] = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Threshold 0.60 on all 102: ")]
    assert "CANNOT DECIDE" in verdict and "1 definition used the 25 fixed examples" in verdict
    assert "1 definition got no matrix statement" in verdict
    predictions[records[60]["variable_id"]].update(examples="retrieved", examples_error=None)
    predictions[records[61]["variable_id"]].update(matrix="none", matrix_status="ok", matrix_error=None)
    assert run_main(tmp_path, records, predictions).code == qg.EXIT_FAIL


# ------------------------------------------------------------------------------------------------
# The credits
# ------------------------------------------------------------------------------------------------


def test_the_corpus_and_the_patterns_are_credited():
    notices = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    sections = {part.split("\n", 1)[0]: part for part in notices.split("\n## ")[1:]}
    corpus = sections["I-ADOPT Corpus"]
    for text in ("v2.0.1", "CC BY 4.0", "10.5281/zenodo.18101358", "backend/data/measured/demonstrations-top25.json",
                 "backend/data/measured/example-pool.json", "backend/data/measured/matrix-finder.json"):
        assert text in corpus, text
    patterns = sections["I-ADOPT Patterns"]
    for text in ("Barbara Magagna", "Sirko", "CC BY 4.0", "ecca3470bebc90cc70ce6dca9785f2dd3bf7f9c0",
                 "backend/data/measured/matrix-finder.json", "declares no licence"):
        assert text in patterns, text
