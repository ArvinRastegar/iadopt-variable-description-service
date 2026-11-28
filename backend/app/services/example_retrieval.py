"""The measured prompt's examples, chosen by similarity with PSNC's embedding model (the lab's best pipeline).

The measured prompt shows, for each definition, the 25 of the lab's 102 corpus variables whose definitions PSNC's
``Qwen3-Embedding-0.6B`` finds most similar to it, the most similar last, instead of the fixed 25 of D-001: the lab
measured +0.08 Close F1 for these examples on PSNC. When the embedding request brings no vector to use, the prompt
shows the fixed 25, as before.

- The pool, ``backend/data/measured/example-pool.json`` (built by ``backend/tools/build_example_pool.py``), holds the
  102 variables with their definitions, answers, folds and vectors. A pool that is missing, is no JSON or breaks a
  rule stops the start (:func:`load_example_pool`).
- :func:`choose_examples` ranks the pool by cosine to a vector as the lab's run did; :func:`fallback_examples` gives
  the fixed 25; :func:`retrieve_examples` sends the embedding request and records it as a call of the purpose
  ``examples``, each failure named for what it is.

The lab's run and its digests: ``paper/tracking/PSNC_RETRIEVAL_TEST_20261127.md`` in the lab's repository.
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from ..clients.http import get_http_session
from ..clients.psnc_client import psnc_chat_headers
from ..core.config import PSNC_MODEL_PROVIDER, settings
from ..core.provider_keys import without_keys
from ..core.state import app_state
from .call_log import CallLog, start_call
from .measured_prompt import ANSWER_KEY_ORDER, load_demonstrations
from .psnc_failures import named, refused, request_failure

EMBEDDING_MODEL = "Qwen3-Embedding-0.6B"
DIMENSIONS = 1024
EXAMPLES = 25
ORDER = "most similar last"
POOL_VARIABLES = 102
EMBEDDING_TIMEOUT_S = 5

# The lab's PSNC retrieval run (paper/tracking/PSNC_RETRIEVAL_TEST_20261127.md): the definitions, the folds and the
# vectors the pool must hold, each as the SHA-256 the run computed.
DEFINITIONS_SHA256 = "c5a9f585fbcb9ffbc67dbaa91b30ffc72a78691b6cf5158ade1f77034af7e1b2"
FOLDS_SHA256 = "80f37c48d2cf004cc00a1237247c7a89269463b13698bdc3aec7d2723cc4f4e3"
VECTORS_SHA256 = "3480e2bb4f059272deaec6886f674647decc456f4722fd393626911504896924"

REBUILD = "Rebuild it with backend/tools/build_example_pool.py."
NO_VECTOR = "embedding answer holds no vector. PSNC's answer is not in the expected form."


@dataclass(frozen=True)
class Examples:
    """The examples one prompt shows.

    Attributes:
        source: ``retrieved`` (chosen by similarity) or ``fallback`` (today's fixed 25).
        ids: Their lab ids, in prompt order.
        demonstrations: Each one's ``definition`` and ``decomposition``, in prompt order.
    """

    source: str
    ids: Tuple[str, ...]
    demonstrations: Tuple[Dict[str, Any], ...]

    def as_record(self) -> Dict[str, Any]:
        """What a run's record keeps of them: ``{"source", "ids"}``."""
        return {"source": self.source, "ids": list(self.ids)}


@dataclass(frozen=True)
class ExamplePool:
    """The pool, checked and ready to rank.

    Attributes:
        key: The pool's and the fixed examples' paths it was loaded from.
        sha256: The SHA-256 of the pool file's bytes, for the setup's fingerprint.
        ids: The 102 lab ids, in id order.
        shown: Each variable as the prompt shows it: ``definition`` and ``decomposition``.
        units: Each variable's vector divided by its length.
        fixed_ids: The lab ids of the fixed 25, in stored order.
        fixed: The fixed 25 as the demonstrations file stores them.
    """

    key: Tuple[str, str]
    sha256: str
    ids: Tuple[str, ...]
    shown: Dict[str, Dict[str, Any]]
    units: Dict[str, Tuple[float, ...]]
    fixed_ids: Tuple[str, ...]
    fixed: Tuple[Dict[str, Any], ...]


# --- the pool --------------------------------------------------------------------------------------------------- #


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unit(vector: Sequence[float]) -> List[float]:
    """The vector divided by its length, computed as the lab's run computed it."""
    length = math.sqrt(sum(y * y for y in vector))
    return [x / length for x in vector]


def _finite(value: Any) -> bool:
    """Whether a JSON number is a finite float; an integer too large for a float is not."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _vector_problem(vector: Sequence[Any]) -> Optional[Tuple[str, str]]:
    """What makes a vector unusable, and its hint; ``None`` for 1,024 finite numbers of non-zero length."""
    if not all(type(x) in (int, float) for x in vector):
        return "holds a value that is not a number", "PSNC's answer is not in the expected form."
    if len(vector) != DIMENSIONS:
        return f"has {len(vector)} numbers, not {DIMENSIONS:,}", "PSNC may have changed its embedding model."
    if not all(_finite(x) for x in vector):
        return "holds a number that is not finite", "PSNC's answer is damaged."
    length = math.sqrt(sum(float(x) * float(x) for x in vector))
    if length == 0:
        return "has length zero", "It cannot be compared with the pool."
    if not math.isfinite(length):
        return "holds a number that is not finite", "PSNC's answer is damaged."
    return None


def _broken_rule(data: Any, demonstrations: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The first rule of AC-example-retrieval-001 a pool breaks: its shape first, then the digests, then the fixed 25."""
    if not isinstance(data, dict) or not isinstance(data.get("variables"), list):
        return "it holds no list of variables"
    if data.get("embedding_model") != EMBEDDING_MODEL:
        return f"it names the embedding model {data.get('embedding_model')!r}, not {EMBEDDING_MODEL}"
    entries = data["variables"]
    if len(entries) != POOL_VARIABLES:
        return f"holds {len(entries)} variables, not {POOL_VARIABLES}"
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            return f"variable {number} is not an object"
        for field, kind in (("id", str), ("definition", str), ("decomposition", dict), ("vector", list)):
            if not isinstance(entry.get(field), kind):
                return f"variable {number} has no {field}"
        if type(entry.get("fold")) is not int:
            return f"variable {number} has no fold"
    seen: set = set()
    for entry in entries:
        if entry["id"] in seen:
            return f"repeats the id {entry['id']}"
        seen.add(entry["id"])
    for entry in entries:
        problem = _vector_problem(entry["vector"])
        if problem is not None:
            return f"vector of {entry['id']} {problem[0]}"
        if set(entry["decomposition"]) != set(ANSWER_KEY_ORDER):
            return f"the answer of {entry['id']} does not hold the six fields"
    ordered = sorted(entries, key=lambda entry: entry["id"])
    digests = (
        ("definitions", _sha256_text("\n".join(f"{e['id']}\t{e['definition']}" for e in ordered)), DEFINITIONS_SHA256),
        ("folds", _sha256_text("\n".join(f"{e['id']} {e['fold']}" for e in ordered)), FOLDS_SHA256),
        ("vectors", _sha256_text(json.dumps({e["id"]: e["vector"] for e in entries}, sort_keys=True)), VECTORS_SHA256),
    )
    for name, computed, expected in digests:
        if computed != expected:
            return f"its {name} are not the lab's (digest {computed[:16]}, not {expected[:16]})"
        if data.get(f"{name}_sha256") != computed:
            return f"its header's digest of the {name} is not theirs"
    by_definition = {entry["definition"]: entry for entry in entries}
    for number, demonstration in enumerate(demonstrations, 1):
        entry = by_definition.get(demonstration.get("definition"))
        if entry is None:
            return f"demonstration {number} is not among its variables"
        if entry["decomposition"] != demonstration.get("decomposition"):
            return f"demonstration {number} has another answer in it"
    return None


def load_example_pool(path: pathlib.Path, demonstrations: Sequence[Dict[str, Any]]) -> ExamplePool:
    """Read the pool and check every rule of AC-example-retrieval-001.

    Args:
        path: The pool file.
        demonstrations: The fixed 25, which must be among its variables with the same answers.

    Returns:
        The pool, ready to rank.

    Raises:
        RuntimeError: ``Example pool <path> is missing``, ``… is not JSON`` or ``… breaks a rule: <which>``, each
            with the hint to rebuild it.
    """
    path = pathlib.Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise RuntimeError(f"Example pool {path} is missing. {REBUILD}") from None
    except OSError as error:
        raise RuntimeError(f"Example pool {path} cannot be read ({error.strerror}). {REBUILD}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError(f"Example pool {path} is not JSON. {REBUILD}") from None
    broken = _broken_rule(data, demonstrations)
    if broken is not None:
        raise RuntimeError(f"Example pool {path} breaks a rule: {broken}. {REBUILD}")
    entries = data["variables"]
    by_definition = {entry["definition"]: entry["id"] for entry in entries}
    return ExamplePool(
        key=(str(path), str(settings.measured_demonstrations_path)),
        sha256=hashlib.sha256(raw).hexdigest(),
        ids=tuple(sorted(entry["id"] for entry in entries)),
        shown={entry["id"]: {"definition": entry["definition"], "decomposition": entry["decomposition"]}
               for entry in entries},
        units={entry["id"]: tuple(_unit(entry["vector"])) for entry in entries},
        fixed_ids=tuple(by_definition[demonstration["definition"]] for demonstration in demonstrations),
        fixed=tuple(demonstrations),
    )


def warm_up(demonstrations: Sequence[Dict[str, Any]]) -> ExamplePool:
    """Load and check the pool at the start, with the fixed 25 the warmup read; raises as :func:`load_example_pool`."""
    pool = load_example_pool(settings.measured_example_pool_path, demonstrations)
    app_state.example_pool_cache = pool
    return pool


def current_pool() -> ExamplePool:
    """The pool the settings name, loaded once; raises as :func:`load_example_pool`."""
    key = (str(settings.measured_example_pool_path), str(settings.measured_demonstrations_path))
    cached = app_state.example_pool_cache
    if cached is not None and cached.key == key:
        return cached
    pool = load_example_pool(settings.measured_example_pool_path, load_demonstrations())
    app_state.example_pool_cache = pool
    return pool


# --- the examples ----------------------------------------------------------------------------------------------- #


def choose_examples(vector: Sequence[float], *, leave_out: Iterable[str] = frozenset()) -> List[str]:
    """The 25 pool variables most similar to a vector, the most similar last.

    The similarity is the cosine, the dot product of the two unit vectors, rounded to 9 places as the lab's run
    rounded it; equal ones go by ascending lab id; the first 25 are reversed.

    Args:
        vector: The definition's vector.
        leave_out: Lab ids that may not be shown.

    Returns:
        The lab ids, in prompt order.
    """
    pool = current_pool()
    left_out: FrozenSet[str] = frozenset(leave_out or ())
    direction = _unit(vector)
    scores = {identifier: round(sum(a * b for a, b in zip(direction, pool.units[identifier])), 9)
              for identifier in pool.ids if identifier not in left_out}
    ranked = sorted(scores, key=lambda identifier: (-scores[identifier], identifier))
    return ranked[:EXAMPLES][::-1]


def fallback_examples(*, leave_out: Iterable[str] = frozenset()) -> Examples:
    """Today's fixed 25 in stored order, without those left out."""
    pool = current_pool()
    left_out: FrozenSet[str] = frozenset(leave_out or ())
    kept = [(identifier, shown) for identifier, shown in zip(pool.fixed_ids, pool.fixed) if identifier not in left_out]
    return Examples("fallback", tuple(i for i, _ in kept), tuple(d for _, d in kept))


def embeddings_url() -> str:
    """PSNC's embeddings endpoint."""
    return f"{settings.psnc_api_base_url.rstrip('/')}/v1/embeddings"


def _vector_of(response: Any) -> Tuple[Optional[List[float]], Any, Optional[str]]:
    """The one vector of an embedding answer, its token counts, and why it cannot be used."""
    try:
        body = response.json()
    except Exception:  # not JSON, or nested too deeply to read; requests' JSONDecodeError is a ValueError
        return None, None, NO_VECTOR
    usage = body.get("usage") if isinstance(body, dict) else None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not data:
        return None, usage, NO_VECTOR
    if len(data) > 1:
        return None, usage, f"embedding answer holds {len(data)} vectors, not one. PSNC's answer is not in the expected form."
    vector = data[0].get("embedding") if isinstance(data[0], dict) else None
    if not isinstance(vector, list):
        return None, usage, NO_VECTOR
    problem = _vector_problem(vector)
    if problem is not None:
        return None, usage, f"embedding vector {problem[0]}. {problem[1]}"
    return [float(x) for x in vector], usage, None


def retrieve_examples(
    definition: str, *, leave_out: Iterable[str] = frozenset(), calls: Optional[CallLog] = None
) -> Examples:
    """Choose a definition's examples with one embedding request, or fall back to the fixed 25.

    The request is ``POST <PSNC base URL>/v1/embeddings`` with ``{"model": "Qwen3-Embedding-0.6B", "input":
    [definition]}``, a timeout of 5 seconds and no retry. It is recorded as a call of the purpose ``examples``:
    ``ok`` when its vector chose the examples, ``unusable`` when an answer came without a vector to use, ``error``
    when none came; also when no usable key let it be sent.

    Args:
        definition: The definition as the prompt carries it, stripped.
        leave_out: Lab ids that may not be shown, also in the fallback.
        calls: The run's call log.

    Returns:
        The examples; never raises for the request's failures, which fall back.
    """
    pending = start_call(calls, "examples", PSNC_MODEL_PROVIDER, EMBEDDING_MODEL)
    try:
        headers = psnc_chat_headers()
    except RuntimeError as error:
        cause = f"embedding request not sent: {without_keys(error)}"
        pending.finish("error", error=named(cause, "Check the PSNC key in the backend's settings."))
        return fallback_examples(leave_out=leave_out)
    try:
        response = get_http_session().post(
            embeddings_url(),
            headers=headers,
            json={"model": EMBEDDING_MODEL, "input": [definition]},
            timeout=EMBEDDING_TIMEOUT_S,
        )
    except Exception as error:  # every failure falls back; its name says why
        slow = "PSNC's embedding model did not answer in time."
        pending.finish("error", error=request_failure("embedding request", error, EMBEDDING_TIMEOUT_S, slow))
        return fallback_examples(leave_out=leave_out)
    if response.status_code != 200:
        pending.finish("error", error=refused("embedding request", response.status_code))
        return fallback_examples(leave_out=leave_out)
    vector, usage, problem = _vector_of(response)
    if vector is None:
        pending.finish("unusable", error=problem, usage=usage)
        return fallback_examples(leave_out=leave_out)
    pending.finish("ok", usage=usage)
    ids = choose_examples(vector, leave_out=leave_out)
    shown = current_pool().shown
    return Examples("retrieved", tuple(ids), tuple(shown[identifier] for identifier in ids))
