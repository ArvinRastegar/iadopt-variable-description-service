"""Entity linking: attach vocabulary IRIs to the labels of a prediction.

For each label in a prediction (simple entities and the parts of asymmetric and symmetric
systems), asks ``entity_linking`` which Wikidata item, QUDT quantity kind or OM quantity names
the same concept, and attaches a ``*URI`` / ``*URIs`` field when one is named. The traversal
and the keys it adds are those documented in ``components/entity-linking.md``; what changed is
how one label is linked (``decisions.md`` D-032, which supersedes D-006).

Depends on ``services.entity_linking``, ``services.entity_choice``, ``services.vocabularies`` and
``services.prompts`` (for ``strip_all_uri_fields``).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .entity_choice import normalise_term
from .entity_linking import choose_candidate
from .prompts import strip_all_uri_fields
from .vocabularies import WIKIDATA, Candidate


def qid_from_uri_or_text(s: Optional[str]) -> Optional[str]:
    """Extract a ``Q...`` Wikidata id from a URI or arbitrary text.

    Args:
        s: A URI/text possibly containing a QID, or ``None``.

    Returns:
        The QID (e.g. ``Q42``), or ``None`` if absent.
    """
    if not s:
        return None
    m = re.search(r"(Q\d+)", s)
    return m.group(1) if m else None


def to_wiki_url(uri: Optional[str]) -> Optional[str]:
    """Normalize a Wikidata reference into a ``.../wiki/Q...`` URL.

    Only for Wikidata. Any other reference has ``http`` upgraded to ``https``, which would
    turn a QUDT or OM IRI into one that names nothing; those never pass through here.

    Args:
        uri: A Wikidata URI/identifier or ``None``.

    Returns:
        The canonical wiki URL when a QID is present; otherwise the input with
        ``http`` upgraded to ``https``. ``None`` for falsy input.
    """
    if not uri:
        return None
    q = qid_from_uri_or_text(uri)
    return f"https://www.wikidata.org/wiki/{q}" if q else uri.strip().replace("http://", "https://")


def candidate_link(candidate: Candidate) -> str:
    """Write the link stored in the prediction for a chosen candidate.

    Wikidata items keep the ``.../wiki/Q...`` form this field has always held. QUDT and OM
    IRIs are stored exactly as the vocabularies write them, ``http://`` included.

    Args:
        candidate: The candidate the model chose.

    Returns:
        The value for the ``*URI`` field.
    """
    if candidate.source == WIKIDATA:
        return to_wiki_url(candidate.local_id) or candidate.iri
    return candidate.iri


def enrich_with_uris(pred: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of the prediction with ``*URI`` fields added.

    Links simple-entity labels and the components of asymmetric/symmetric systems. A label
    that occurs more than once is linked once: the answer for its normalised text is reused.
    Any ``*URI`` / ``*URIs`` key already in the prediction (the decomposition model can emit
    one) is dropped first, so that every link in the result is one the linker chose.

    Args:
        pred: The prediction dict.

    Returns:
        A new dict with ``*URI`` / ``*URIs`` keys added where a vocabulary entry was chosen.
        The input is never mutated.

    Side effects:
        Searches the vocabularies and asks PSNC once per distinct linkable label. A
        ``RuntimeError`` from ``entity_linking`` (PSNC unusable) is not caught here; the
        pipeline falls back to the prediction without URIs.
    """
    out = strip_all_uri_fields(json.loads(json.dumps(pred)))
    definition = pred.get("definition", "")
    answers: Dict[str, Optional[str]] = {}

    def link(label: Any) -> Optional[str]:
        """Link one label, or return ``None`` for a blank or non-string one."""
        if not isinstance(label, str) or not label.strip():
            return None
        key = normalise_term(label)
        if key not in answers:
            candidate = choose_candidate(label, definition)
            answers[key] = candidate_link(candidate) if candidate else None
        return answers[key]

    def add_uri_field(container: Dict[str, Any], key: str, label_value: Any) -> None:
        """Link a string label and set ``container[f"{key}URI"]`` on a hit."""
        uri = link(label_value)
        if uri:
            container[f"{key}URI"] = uri

    for p in ["hasProperty", "hasMatrix", "hasObjectOfInterest", "hasContextObject", "hasStatisticalModifier"]:
        if p in out and isinstance(out[p], str):
            add_uri_field(out, p, out[p])

    for p in ["hasMatrix", "hasObjectOfInterest", "hasContextObject"]:
        val = out.get(p)
        if isinstance(val, dict):
            if "AsymmetricSystem" in val:
                # Link both system-level and component-level asymmetric system labels so the serializer
                # can emit readable labels and URIs for all formula variants.
                for kk in ["AsymmetricSystem", "hasSource", "hasTarget", "hasNumerator", "hasDenominator"]:
                    if val.get(kk):
                        add_uri_field(val, kk, val[kk])

            if "SymmetricSystem" in val:
                if val.get("SymmetricSystem"):
                    add_uri_field(val, "SymmetricSystem", val["SymmetricSystem"])

                parts = val.get("hasPart", [])
                if isinstance(parts, list) and parts:
                    part_uris: List[Optional[str]] = [link(part) for part in parts]
                    if any(part_uris):
                        val["hasPartURIs"] = part_uris

    return out
