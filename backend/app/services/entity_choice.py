"""The measured entity-linking question: prompt, option labels, candidate order, answer check.

A chat model is shown a term, the definition of the variable it belongs to, and a numbered
list of candidates from Wikidata, QUDT and OM. It answers with one character: a digit or a
capital letter naming a candidate, or ``0`` for "none of these". Every option label is a
single token, so one generated token is the whole answer.

The instruction text, the label alphabet, the 300-character description cap and the decoding
parameters are those measured in the sibling ``iadopt-linking`` project (its DECISIONS L-016
to L-018, prompt version ``psnc-chat-entity-link-v1``, fingerprint ``20d69e9a52e2``). The
prompt was written there on invented slots only and is frozen. Changing any part of it means
the service no longer runs what was measured; see ``decisions.md`` D-032 in the private
workflow repository. A golden test pins the request byte for byte.

Everything here is pure: no I/O, no clock, no randomness. Depends on ``clients.psnc_client``
(the request body) and ``services.vocabularies`` (the candidate type).
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any, Dict, List, Optional, Sequence

from ..clients.psnc_client import build_entity_link_payload
from ..core.text import normalize_text
from .vocabularies import Candidate

MODEL = "Qwen3.8-27B"
PROMPT_VERSION = "psnc-chat-entity-link-v1"
# One character, one token, no look-alikes (no I or O): 33 options, and a pool holds at most 30.
LABELS = "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
NONE_LABEL = "0"
NONE_DESCRIPTION = "no candidate is the same concept as the term"
ANSWER_LINE = "Answer with one label."
MAX_DESCRIPTION_CHARS = 300
# The measured passes ordered candidates by a seeded hash keyed by corpus slot. The service has
# no slot, so it orders by term instead, with a seed of its own.
ORDER_SEED = "iadopt-variable-description-service/entity-link-v1/order"

SYSTEM_PROMPT = (
    "You link a term from a scientific variable name to the knowledge-base entry that names the "
    "same concept. You are given a `term`, the `variable_definition` of the variable the term is a "
    "component of, and a numbered list of candidate entries. Pick the candidate that is the entry "
    "for the concept that `term` names. Use `variable_definition` only to tell which meaning of "
    "`term` is intended. A candidate matches only if it is the same concept as `term`: a broader, "
    "narrower or merely related concept does not match, and neither does a concept for the whole "
    "variable. Differences in capitalization, plural form or word order do not matter. If no "
    "candidate is the same concept, pick option 0. Reply with the label of exactly one option (a "
    "digit or a capital letter) and nothing else."
)

# Hyphen, non-breaking hyphen, figure dash, en dash, em dash and minus sign, all read as "-".
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")


class InvalidAnswer(ValueError):
    """A chat response that is not a valid answer to the question it answers."""


def normalise_term(term: str) -> str:
    """Make a label safe to send to a search service and to show to the model.

    Args:
        term: The label as the prediction wrote it.

    Returns:
        The label in NFKC form with dash variants read as ``-``, whitespace collapsed and
        both ends stripped. Case is kept.
    """
    return normalize_text(unicodedata.normalize("NFKC", term).translate(_DASHES))


def query_key(term: str) -> str:
    """Name the search every spelling of a term shares.

    Args:
        term: The label as the prediction wrote it.

    Returns:
        The normalised label in lower case.
    """
    return normalise_term(term).lower()


def truncate_description(text: str, max_chars: Optional[int]) -> str:
    """Cap a description at a word boundary.

    Args:
        text: The description.
        max_chars: The cap, or ``None`` for no cap.

    Returns:
        The text with whitespace collapsed, unchanged if short enough, otherwise its first
        words followed by an ellipsis.
    """
    text = normalize_text(text)
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"


def option_label(position: int) -> str:
    """Name the option shown at a 1-based position with its one-token label.

    Args:
        position: The 1-based place in the request.

    Returns:
        ``1``..``9``, then ``A``, ``B`` and so on, skipping ``I`` and ``O``.

    Raises:
        ValueError: If the position is outside ``1..len(LABELS)``.
    """
    if not 1 <= position <= len(LABELS):
        raise ValueError(f"Option position {position} is outside 1..{len(LABELS)}")
    return LABELS[position - 1]


def presentation_order(candidates: Sequence[Candidate], term_key: str) -> List[int]:
    """Decide in which order a term's candidates are shown.

    The order is a seeded hash of each candidate's identity, so it neither follows the
    sources' own ranking nor puts one vocabulary first, and it is the same on every call
    for the same term and the same candidates.

    Args:
        candidates: The pooled candidates.
        term_key: The term's query key.

    Returns:
        The indices of ``candidates`` in presentation order.
    """
    keyed = [
        (hashlib.sha256(f"{ORDER_SEED}|{term_key}|{candidate.key}".encode()).digest(), index)
        for index, candidate in enumerate(candidates)
    ]
    return [index for _, index in sorted(keyed)]


def shown_candidate(candidate: Candidate) -> str:
    """Write the part of a candidate the model may see: its label and capped description.

    No vocabulary, identifier or rank is shown. A description has all its whitespace collapsed;
    a label has only its line breaks replaced by spaces, so that text from a vocabulary cannot
    start a line of its own and pass for another option. (No label measured so far had one.)

    Args:
        candidate: A pooled candidate.

    Returns:
        ``"label": description``, or ``"label"`` for a candidate with no description.
    """
    label = " ".join(candidate.label.splitlines())
    description = truncate_description(candidate.description, MAX_DESCRIPTION_CHARS)
    return f'"{label}"' + (f": {description}" if description else "")


def user_message(term: str, definition: str, shown: Sequence[Candidate]) -> str:
    """Write the case the model is asked about.

    Args:
        term: The normalised term.
        definition: The variable definition.
        shown: The candidates in presentation order.

    Returns:
        The term, the definition, the labelled candidates, the "none" option last, and the
        answer line.

    Raises:
        ValueError: If there are more candidates than option labels.
    """
    if len(shown) > len(LABELS):
        raise ValueError(f"A pool of {len(shown)} candidates does not fit {len(LABELS)} option labels")
    lines = [f"{option_label(place)}. {shown_candidate(candidate)}" for place, candidate in enumerate(shown, start=1)]
    lines.append(f"{NONE_LABEL}. none of these: {NONE_DESCRIPTION}")
    return f"term: {term}\nvariable_definition: {definition}\n\ncandidates:\n" + "\n".join(lines) + f"\n\n{ANSWER_LINE}"


def build_request(term: str, definition: str, shown: Sequence[Candidate]) -> Dict[str, Any]:
    """Build the chat-completions request for one term.

    Args:
        term: The normalised term.
        definition: The variable definition.
        shown: The candidates in presentation order.

    Returns:
        The request body.

    Raises:
        ValueError: If there is nothing to choose from, or too many candidates for the labels.
    """
    if not shown:
        raise ValueError("There is nothing to ask about an empty pool")
    return build_entity_link_payload(MODEL, SYSTEM_PROMPT, user_message(term, definition, shown))


def read_choice(data: Any, count: int) -> int:
    """Read the answer to an entity-linking request strictly.

    Nothing is repaired: a response that is not exactly a single completed choice naming an
    offered label is refused, so the caller can ask again.

    Args:
        data: The decoded response body.
        count: How many candidates the request offered.

    Returns:
        ``0`` when the model chose "none of these", otherwise the 1-based position of the
        candidate it chose.

    Raises:
        InvalidAnswer: Unless the response holds exactly one choice that finished normally,
            carries no reasoning text, and whose trimmed text is ``0`` or one of the first
            ``count`` labels, in upper case.
    """
    if not isinstance(data, dict):
        raise InvalidAnswer("the response is not a JSON object")
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise InvalidAnswer("the response does not hold exactly one choice")
    choice = choices[0]
    if choice.get("finish_reason") not in ("stop", "length"):
        raise InvalidAnswer("the generation did not finish normally")
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise InvalidAnswer("the message has no text")
    for field in ("reasoning_content", "reasoning"):
        if message.get(field):
            raise InvalidAnswer("the model reasoned although reasoning is switched off")
    label = message["content"].strip()
    if label == NONE_LABEL:
        return 0
    position = LABELS.find(label) + 1 if len(label) == 1 else 0
    if not 1 <= position <= count:
        raise InvalidAnswer("the text is not one of the offered labels")
    return position
