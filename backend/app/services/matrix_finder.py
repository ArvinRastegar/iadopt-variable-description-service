"""The matrix finder: one request that decides whether a variable has a matrix and of which kind (lab's best pipeline).

Before the decomposition, the model is asked to classify the definition with the I-ADOPT patterns, naming the object,
the matrix entity and the restricting phrases first (the lab's ``answer_form_check.py``, arm ``named-first``). The
matrix pattern of its answer is then stated in the fill prompt, after the definition, with the lab's wording: the lab
measured +0.016 Close F1 for this statement on top of the 25 most similar examples (0.690 against 0.674), and its
answers named the right matrix in 82% of the variables.

- The finder's data, ``backend/data/measured/matrix-finder.json`` (built by ``backend/tools/build_finder_data.py``),
  holds the prompt's head (the instruction and the pattern lists), the 102 corpus variables as the prompt shows them,
  the property pattern numbers an answer must name, and the guidance block of each matrix decision.
- :func:`finder_prompt` shows every corpus variable (but those left out) in order of shared words with the definition,
  the most similar last, as the lab's ``knowledge.nearest_last`` orders them.
- :func:`read_answer` reads an answer as strictly as the lab's ``knowledge.requested`` and
  ``walk.facts_from_direct``: anything else states nothing.
- :func:`find_matrix` sends the request (temperature 0, at most 700 answer tokens, 20 seconds, no retry) and records it
  as a call of the purpose ``matrix``, each failure named for what it is.
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..clients.http import get_http_session
from ..clients.psnc_client import build_finder_psnc_payload, psnc_chat_completions_url, psnc_chat_headers
from ..core.config import PSNC_MODEL_PROVIDER, settings
from ..core.provider_keys import without_keys
from ..core.state import app_state
from .call_log import CallLog, start_call
from .lab_json import extract_json_object
from .llm import chat_completion_text
from .psnc_failures import named, refused, request_failure

FINDER_TIMEOUT_S = 20
MATRIX_KEYS = ("none", "40", "41", "42", "43", "44", "45")
EXAMPLES = 102
REBUILD = "Rebuild it with backend/tools/build_finder_data.py."
NOT_REQUESTED = "matrix finder answer is not the requested JSON. No matrix is stated."
NOT_A_COMPLETION = "matrix finder answer is not a chat completion. PSNC's answer is not in the expected form."
_WORDS = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class FinderData:
    """The finder's data, checked.

    Attributes:
        key: The path it was read from.
        sha256: The SHA-256 of the file's bytes, for the setup's fingerprint.
        head: The prompt's instruction and pattern lists.
        examples: The corpus variables in id order, each with ``id``, ``definition``, ``decomposition`` and
            ``patterns`` as the prompt shows them.
        property_patterns: The property pattern numbers, as text.
        statements: The guidance block of each matrix decision, by ``none`` or the pattern number.
        request: The finder's ``temperature``, ``top_p`` and ``max_tokens``.
    """

    key: str
    sha256: str
    head: str
    examples: Tuple[Dict[str, str], ...]
    property_patterns: FrozenSet[str]
    statements: Dict[str, str]
    request: Dict[str, Any]


@dataclass(frozen=True)
class MatrixFinding:
    """What the finder found for one definition.

    Attributes:
        matrix: ``none`` or a matrix pattern number (``40`` to ``45``) the answer states, or ``None`` when no usable
            answer came.
        statement: The guidance block to put after the definition; empty when nothing is stated.
    """

    matrix: Optional[str]
    statement: str


NO_FINDING = MatrixFinding(None, "")


def _broken_rule(data: Any) -> Optional[str]:
    """The first rule the finder's data breaks."""
    if not isinstance(data, dict):
        return "it is not an object"
    for field, kind in (("head", str), ("examples", list), ("property_patterns", list), ("statements", dict),
                        ("request", dict)):
        if not isinstance(data.get(field), kind):
            return f"it has no {field}"
    if len(data["examples"]) != EXAMPLES:
        return f"holds {len(data['examples'])} examples, not {EXAMPLES}"
    for number, example in enumerate(data["examples"], 1):
        if not isinstance(example, dict) or not all(
                isinstance(example.get(field), str) for field in ("id", "definition", "decomposition", "patterns")):
            return f"example {number} is not complete"
    ids = [example["id"] for example in data["examples"]]
    if ids != sorted(set(ids)):
        return "its examples are not in id order without repeats"
    if sorted(data["statements"]) != sorted(MATRIX_KEYS) or not all(isinstance(v, str) for v in data["statements"].values()):
        return "it does not hold a statement for none and for each of the matrix patterns 40 to 45"
    if not data["property_patterns"] or not all(type(n) is int for n in data["property_patterns"]):
        return "its property patterns are not numbers"
    request = data["request"]
    if not (isinstance(request.get("temperature"), (int, float)) and isinstance(request.get("top_p"), (int, float))
            and type(request.get("max_tokens")) is int):
        return "its request lacks the temperature, top_p or max_tokens"
    return None


def load_finder_data(path: pathlib.Path) -> FinderData:
    """Read the finder's data and check its rules.

    Args:
        path: The data file.

    Returns:
        The data.

    Raises:
        RuntimeError: ``Matrix finder data <path> is missing``, ``… is not JSON`` or ``… breaks a rule: <which>``,
            each with the hint to rebuild it.
    """
    path = pathlib.Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise RuntimeError(f"Matrix finder data {path} is missing. {REBUILD}") from None
    except OSError as error:
        raise RuntimeError(f"Matrix finder data {path} cannot be read ({error.strerror}). {REBUILD}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError(f"Matrix finder data {path} is not JSON. {REBUILD}") from None
    broken = _broken_rule(data)
    if broken is not None:
        raise RuntimeError(f"Matrix finder data {path} breaks a rule: {broken}. {REBUILD}")
    return FinderData(
        key=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        head=data["head"],
        examples=tuple(data["examples"]),
        property_patterns=frozenset(str(number) for number in data["property_patterns"]),
        statements=dict(data["statements"]),
        request={key: data["request"][key] for key in ("temperature", "top_p", "max_tokens")},
    )


def warm_up(pool_ids: Iterable[str]) -> None:
    """Load the finder's data at the start; its variables must be the example pool's.

    Raises:
        RuntimeError: As :func:`load_finder_data`, or when its variables are not the pool's.
    """
    data = load_finder_data(settings.measured_finder_path)
    if [example["id"] for example in data.examples] != sorted(pool_ids):
        raise RuntimeError(f"Matrix finder data {data.key} breaks a rule: its variables are not the example pool's. "
                           f"{REBUILD}")
    app_state.finder_cache = data


def current_finder() -> FinderData:
    """The finder's data the settings name, loaded once; raises as :func:`load_finder_data`."""
    cached = app_state.finder_cache
    if cached is not None and cached.key == str(settings.measured_finder_path):
        return cached
    data = load_finder_data(settings.measured_finder_path)
    app_state.finder_cache = data
    return data


def words(text: str) -> List[str]:
    """The lower-case runs of letters and digits of a text (the lab's ``knowledge.words``)."""
    return _WORDS.findall(text.lower())


def nearest_last(definition: str, examples: Sequence[Mapping[str, str]]) -> List[Mapping[str, str]]:
    """The examples in order of similarity to the definition, the most similar last (the lab's ``nearest_last``).

    The similarity is the cosine of word counts weighted by how rare a word is among the definition and these
    examples, rounded to 12 places; equally similar examples go by ascending id.
    """
    documents = [Counter(words(definition)), *(Counter(words(example["definition"])) for example in examples)]
    seen = Counter(word for document in documents for word in document)
    weight = {word: math.log((1 + len(documents)) / (1 + count)) + 1.0 for word, count in seen.items()}
    vectors = [{word: count * weight[word] for word, count in document.items()} for document in documents]
    norms = [math.sqrt(sum(value * value for value in vector.values())) for vector in vectors]

    def similarity(position: int) -> float:
        if not norms[0] or not norms[position]:
            return 0.0
        shared = sum(value * vectors[position].get(word, 0.0) for word, value in vectors[0].items())
        return shared / (norms[0] * norms[position])

    scored = [(round(similarity(position + 1), 12), example["id"], example) for position, example in enumerate(examples)]
    return [example for _, _, example in sorted(scored, key=lambda item: (item[0], item[1]))]


def finder_prompt(definition: str, *, leave_out: Iterable[str] = frozenset()) -> str:
    """The finder's prompt: the head, the examples most similar last, then the definition.

    Args:
        definition: The definition.
        leave_out: Lab ids of corpus variables the prompt may not show.

    Returns:
        The prompt, as the lab's ``conventions_test.build`` writes it for the arm ``plain``.
    """
    data = current_finder()
    left_out = frozenset(leave_out or ())
    lines = [data.head, "EXAMPLES"]
    for example in nearest_last(definition, [e for e in data.examples if e["id"] not in left_out]):
        lines += [f"Definition: {example['definition']}", f"Decomposition: {example['decomposition']}",
                  f"Patterns: {example['patterns']}", ""]
    lines += [f"Definition: {definition}", "Patterns:"]
    return "\n".join(lines)


def read_answer(text: Optional[str]) -> Optional[str]:
    """The matrix decision an answer states: ``none`` or ``40`` to ``45``, or ``None`` when it is not usable.

    An answer is usable when it is one JSON object with the keys ``property`` (a property pattern), ``matrix`` (null or
    40 to 45), ``context`` and ``modifier`` (true or false) and ``constraints`` (a list of texts), as the lab reads it.
    """
    data = extract_json_object(text or "")
    if data is None or not {"property", "matrix", "context", "modifier", "constraints"} <= set(data):
        return None
    matrix = data["matrix"]
    if not (matrix is None or (type(matrix) is int and 40 <= matrix <= 45)):
        return None
    if type(data["context"]) is not bool or type(data["modifier"]) is not bool:
        return None
    names = data["constraints"]
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return None
    if str(data["property"]) not in current_finder().property_patterns:
        return None
    return "none" if matrix is None else str(matrix)


def statement(matrix: Optional[str]) -> str:
    """The guidance block that states a matrix decision; empty for ``None``."""
    return "" if matrix is None else current_finder().statements[matrix]


def find_matrix(
    definition: str, *, model: str, leave_out: Iterable[str] = frozenset(), calls: Optional[CallLog] = None
) -> MatrixFinding:
    """Ask the finder once and read the matrix decision of its answer.

    The request goes to PSNC's chat completions with the finder's settings (temperature 0, top_p 1, at most 700
    answer tokens, reasoning off), a timeout of 20 seconds and no retry. It is recorded as a call of the purpose
    ``matrix``: ``ok`` when its answer states a decision, ``unusable`` when the model answered but not with the requested
    JSON, ``error`` when no answer came: also when PSNC's body is not a chat completion with a choice, or no usable key
    let the request be sent.

    Args:
        definition: The definition as the prompt carries it, stripped.
        model: The PSNC model of the run.
        leave_out: Lab ids the finder's examples may not show.
        calls: The run's call log.

    Returns:
        The finding; never raises for the request's failures, which state nothing.
    """
    pending = start_call(calls, "matrix", PSNC_MODEL_PROVIDER, model)
    try:
        headers = psnc_chat_headers()
    except RuntimeError as error:
        cause = f"matrix finder not sent: {without_keys(error)}"
        pending.finish("error", error=named(cause, "Check the PSNC key in the backend's settings."))
        return NO_FINDING
    data = current_finder()
    body = build_finder_psnc_payload(model, finder_prompt(definition, leave_out=leave_out), **data.request)
    try:
        response = get_http_session().post(psnc_chat_completions_url(), headers=headers, json=body,
                                           timeout=FINDER_TIMEOUT_S)
    except Exception as error:  # every failure states nothing; its name says why
        slow = "PSNC's model did not answer in time."
        pending.finish("error", error=request_failure("matrix finder", error, FINDER_TIMEOUT_S, slow))
        return NO_FINDING
    if response.status_code != 200:
        pending.finish("error", error=refused("matrix finder", response.status_code))
        return NO_FINDING
    try:
        payload = response.json()
    except Exception:  # not JSON, or nested too deeply to read; requests' JSONDecodeError is a ValueError
        pending.finish("error", error=NOT_A_COMPLETION)
        return NO_FINDING
    usage = payload.get("usage") if isinstance(payload, dict) else None
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        pending.finish("error", error=NOT_A_COMPLETION, usage=usage)
        return NO_FINDING
    try:
        matrix = read_answer(chat_completion_text(payload))
    except Exception:  # an answer no rule foresaw states nothing either
        matrix = None
    if matrix is None:
        pending.finish("unusable", error=NOT_REQUESTED, usage=usage)
        return NO_FINDING
    pending.finish("ok", usage=usage)
    return MatrixFinding(matrix, statement(matrix))
