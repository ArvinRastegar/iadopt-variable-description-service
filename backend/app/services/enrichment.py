"""Wikidata enrichment: link prediction labels to Wikidata entity URIs.

For each label in a prediction (simple entities and asymmetric/symmetric system
parts), searches Wikidata, reranks the candidates with the PSNC reranker, and
attaches a ``*URI`` / ``*URIs`` field when the best match clears the threshold.

Depends on ``clients.http`` (Wikidata search), ``services.reranker``, and
``core.config`` for the default threshold.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, List, Optional

from ..clients.http import get_http_session
from ..core.config import settings
from .reranker import call_psnc_reranker


def qid_from_uri_or_text(s: Optional[str]) -> Optional[str]:
    """Extract a ``Q...`` Wikidata id from a URI or arbitrary text.

    Args:
        s: A URI/text possibly containing a QID, or ``None``.

    Returns:
        The QID (e.g. ``Q42``), or ``None`` if absent.
    """
    if not s:
        return None
    import re

    m = re.search(r"(Q\d+)", s)
    return m.group(1) if m else None


def to_wiki_url(uri: Optional[str]) -> Optional[str]:
    """Normalize a Wikidata reference into a ``.../wiki/Q...`` URL.

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


def get_wikidata_entity_reranker(
    term: str,
    context: str = "",
    threshold: Optional[float] = None,
) -> Optional[str]:
    """Find the best Wikidata entity for a term using search + PSNC reranking.

    Args:
        term: The label to link.
        context: Optional surrounding definition text to disambiguate.
        threshold: Minimum rerank score to accept; defaults to the configured
            ``RERANK_THRESHOLD``.

    Returns:
        The best-matching Wikidata wiki URL, or ``None`` when nothing clears the
        threshold (or the term is empty / search fails).

    Side effects:
        Performs a Wikidata search HTTP GET and a PSNC rerank call.
    """
    if threshold is None:
        threshold = settings.rerank_threshold

    if not term:
        return None

    encoded = urllib.parse.quote_plus(term)
    url = "https://www.wikidata.org/w/api.php" f"?action=wbsearchentities&search={encoded}&language=en&format=json"

    response = get_http_session().get(url, timeout=20)

    if response.status_code != 200:
        return None

    search = response.json().get("search", [])
    if not search:
        return None

    query = f'Definition of "{term}" in context: "{context}"'
    documents = [f'label: "{s.get("label", "")}", description: "{s.get("description", "")}"' for s in search]

    scores = call_psnc_reranker(query, documents)

    ranked = sorted(zip(search, scores), key=lambda x: float(x[1]), reverse=True)
    best_s, best_score = ranked[0]

    return to_wiki_url(best_s["id"]) if float(best_score) >= float(threshold) else None


def enrich_with_uris_reranker(pred: Dict[str, Any], threshold: Optional[float] = None) -> Dict[str, Any]:
    """Return a deep copy of the prediction with Wikidata ``*URI`` fields added.

    Links simple-entity labels and the components of asymmetric/symmetric systems.

    Args:
        pred: The prediction dict.
        threshold: Minimum rerank score; defaults to the configured threshold.

    Returns:
        A new dict with ``*URI`` / ``*URIs`` keys added where matches were found.

    Side effects:
        Performs Wikidata search + PSNC rerank calls per linkable label.
    """
    if threshold is None:
        threshold = settings.rerank_threshold

    out = json.loads(json.dumps(pred))

    def add_uri_field(container: Dict[str, Any], key: str, label_value: Any) -> None:
        if isinstance(label_value, str) and label_value.strip():
            uri = get_wikidata_entity_reranker(
                label_value,
                context=pred.get("definition", ""),
                threshold=threshold,
            )
            if uri:
                container[f"{key}URI"] = to_wiki_url(uri)

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
                        uri = get_wikidata_entity_reranker(
                            val[kk],
                            context=pred.get("definition", ""),
                            threshold=threshold,
                        )
                        if uri:
                            val[f"{kk}URI"] = to_wiki_url(uri)

            if "SymmetricSystem" in val:
                if val.get("SymmetricSystem"):
                    uri = get_wikidata_entity_reranker(
                        val["SymmetricSystem"],
                        context=pred.get("definition", ""),
                        threshold=threshold,
                    )
                    if uri:
                        val["SymmetricSystemURI"] = to_wiki_url(uri)

                parts = val.get("hasPart", [])
                if isinstance(parts, list) and parts:
                    part_uris: List[Optional[str]] = []
                    for part in parts:
                        if isinstance(part, str) and part.strip():
                            uri = get_wikidata_entity_reranker(
                                part,
                                context=pred.get("definition", ""),
                                threshold=threshold,
                            )
                            part_uris.append(to_wiki_url(uri) if uri else None)
                        else:
                            part_uris.append(None)

                    if any(part_uris):
                        val["hasPartURIs"] = part_uris

    return out
