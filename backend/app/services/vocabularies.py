"""Candidate search in the three vocabularies entity linking draws on.

Wikidata items (``wbsearchentities``), QUDT quantity kinds (the public SPARQL
endpoint has no text index, so the query filters on the words of the term) and OM
quantities (the EBI Ontology Lookup Service search). Each source turns a query key
into one GET request and its response into at most ten candidates. These are the
queries measured in the sibling ``iadopt-linking`` project (its DECISIONS L-006); a
golden test pins them.

Parsing is strict. A response that does not have the shape its source promises raises
:class:`VocabularyError`, so a broken answer is never mistaken for "no candidates",
and every IRI that can reach Turtle is matched against its vocabulary's own pattern
first. Messages and log lines never carry the term or any response text.

Depends on ``clients.http`` and ``core.text``; ``entity_choice``, ``entity_linking``,
``enrichment`` and ``rdf_ttl`` depend on it.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from ..clients.http import get_http_session
from ..core.text import normalize_text

logger = logging.getLogger(__name__)

WIKIDATA = "wikidata"
QUDT = "qudt"
OM = "om"
# The pooling order. It does not decide what the model sees first: candidates are shuffled.
SOURCES = (WIKIDATA, QUDT, OM)

RESULT_LIMIT = 10
REQUEST_TIMEOUT_S = 20

WIKIDATA_ENDPOINT = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY = "http://www.wikidata.org/entity/"
QUDT_ENDPOINT = "https://www.qudt.org/fuseki/qudt/sparql"
OLS_ENDPOINT = "https://www.ebi.ac.uk/ols4/api/search"
OM_QUANTITY = "http://www.ontology-of-units-of-measure.org/resource/om-2/Quantity"
# Words that carry no meaning in a quantity-kind label; dropping them lets "amount of substance
# concentration" find QUDT's "Amount of Substance of Concentration".
QUDT_STOPWORDS = frozenset({"of", "the", "a", "an", "and"})

_QID = re.compile(r"Q[0-9]+")
_QUDT_IRI = re.compile(r"http://qudt\.org/vocab/quantitykind/([A-Za-z0-9_.-]+)")
_OM_IRI = re.compile(r"http://www\.ontology-of-units-of-measure\.org/resource/om-2/([A-Za-z0-9_.-]+)")


class VocabularyError(ValueError):
    """A vocabulary did not answer, or answered with something other than its promised shape."""


@dataclass(frozen=True)
class Candidate:
    """One entry a vocabulary offered for a term.

    Attributes:
        source: ``wikidata``, ``qudt`` or ``om``.
        iri: The vocabulary's own IRI for the entry.
        local_id: The Wikidata item id, or the vocabulary's local name.
        label: The entry's label.
        description: The entry's description; empty when the source has none.
    """

    source: str
    iri: str
    local_id: str
    label: str
    description: str

    @property
    def key(self) -> str:
        """Name the entry by its vocabulary and local identifier, e.g. ``qudt:Temperature``."""
        return f"{self.source}:{self.local_id}"


@dataclass(frozen=True)
class SearchRequest:
    """Everything needed to ask one source about one key.

    Attributes:
        url: The endpoint.
        params: The query parameters.
        headers: Extra request headers.
    """

    url: str
    params: Dict[str, str]
    headers: Dict[str, str]


def quantity_vocabulary_iri(iri: Any) -> Optional[str]:
    """Return the IRI when it is exactly a QUDT quantity-kind or an OM-2 IRI.

    This is the gate that lets a vocabulary IRI into Turtle, where it is written
    between angle brackets: nothing but the two vocabularies' own canonical forms
    passes, so no character that could end the IRI early does.

    Args:
        iri: Any value read from a prediction or a vocabulary response.

    Returns:
        The IRI unchanged when it matches, otherwise ``None``.
    """
    if not isinstance(iri, str):
        return None
    if _QUDT_IRI.fullmatch(iri) or _OM_IRI.fullmatch(iri):
        return iri
    return None


def _local_id(source: str, iri: Any) -> str:
    """Read the local identifier out of a QUDT or OM IRI, refusing any other form.

    Args:
        source: ``qudt`` or ``om``.
        iri: The IRI as the source wrote it.

    Returns:
        The vocabulary's local name.

    Raises:
        VocabularyError: If the IRI is not in the vocabulary's canonical form.
    """
    pattern = _QUDT_IRI if source == QUDT else _OM_IRI
    found = pattern.fullmatch(iri) if isinstance(iri, str) else None
    if found is None:
        raise VocabularyError(f"{source}: an IRI is not in the vocabulary's canonical form")
    return found.group(1)


def _json(text: str, source: str) -> Any:
    """Decode a JSON response body.

    Args:
        text: The response body.
        source: The source name, for the message.

    Returns:
        The decoded value.

    Raises:
        VocabularyError: If the body is not JSON.
    """
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise VocabularyError(f"{source}: response is not JSON") from exc


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


def _wikidata_request(key: str) -> SearchRequest:
    """Build the Wikidata item search.

    Args:
        key: The query key.

    Returns:
        A GET request for at most ``RESULT_LIMIT`` English items.
    """
    return SearchRequest(
        WIKIDATA_ENDPOINT,
        {
            "action": "wbsearchentities",
            "search": key,
            "language": "en",
            "uselang": "en",
            "type": "item",
            "limit": str(RESULT_LIMIT),
            "format": "json",
        },
        {"Accept": "application/json"},
    )


def _qudt_request(key: str) -> Optional[SearchRequest]:
    """Build the QUDT query: kinds whose English label contains every word of the key.

    The only text that reaches the query is made of letters and digits (the words of
    the key), so no quote or other SPARQL syntax can pass through it.

    Args:
        key: The query key.

    Returns:
        A GET request ordered by label length, so an exact label sorts first, for at most
        ``RESULT_LIMIT`` active kinds; ``None`` when the key has no searchable word.
    """
    words = [w for w in re.findall(r"[^\W_]+", key.lower()) if w not in QUDT_STOPWORDS]
    if not words:
        return None
    filters = "\n      ".join(f'FILTER(CONTAINS(LCASE(STR(?l)), "{w}"))' for w in words)
    query = f"""PREFIX qudt: <http://qudt.org/schema/qudt/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX dcterms: <http://purl.org/dc/terms/>
SELECT ?qk ?len ?label ?description ?wikidata ?definedBy WHERE {{
  {{ SELECT ?qk (MIN(STRLEN(STR(?l))) AS ?len) WHERE {{
      ?qk a qudt:QuantityKind ; rdfs:label ?l .
      FILTER(STRSTARTS(LANG(?l), "en") || LANG(?l) = "")
      FILTER NOT EXISTS {{ ?qk qudt:deprecated true }}
      {filters}
  }} GROUP BY ?qk ORDER BY ?len ?qk LIMIT {RESULT_LIMIT} }}
  ?qk rdfs:label ?label . FILTER(STRSTARTS(LANG(?label), "en") || LANG(?label) = "")
  OPTIONAL {{ ?qk dcterms:description ?d1 }}
  OPTIONAL {{ ?qk qudt:plainTextDescription ?d2 }}
  OPTIONAL {{ ?qk qudt:wikidataMatch ?wikidata }}
  OPTIONAL {{ ?qk rdfs:isDefinedBy ?definedBy }}
  BIND(COALESCE(?d1, ?d2) AS ?description)
}}"""
    return SearchRequest(QUDT_ENDPOINT, {"query": query}, {"Accept": "application/sparql-results+json"})


def _om_request(key: str) -> SearchRequest:
    """Build the OLS search restricted to descendants of ``om:Quantity``.

    Args:
        key: The query key.

    Returns:
        A GET request for at most ``RESULT_LIMIT`` OM quantity classes.
    """
    return SearchRequest(
        OLS_ENDPOINT,
        {
            "q": key,
            "ontology": "om",
            "allChildrenOf": OM_QUANTITY,
            "rows": str(RESULT_LIMIT),
            "fieldList": "iri,label,description,type,ontology_name",
        },
        {"Accept": "application/json"},
    )


_REQUESTS: Dict[str, Callable[[str], Optional[SearchRequest]]] = {
    WIKIDATA: _wikidata_request,
    QUDT: _qudt_request,
    OM: _om_request,
}


def search_request(source: str, key: str) -> Optional[SearchRequest]:
    """Build the search request of one source.

    Args:
        source: ``wikidata``, ``qudt`` or ``om``.
        key: The query key: the normalised term in lower case.

    Returns:
        The request, or ``None`` when the source cannot search for this key.
    """
    return _REQUESTS[source](key)


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


def _parse_wikidata(text: str) -> List[Candidate]:
    """Read the ranked items from a ``wbsearchentities`` response.

    Args:
        text: The response body.

    Returns:
        Candidates in the API's own order. Their IRI is rebuilt from the item id, which is
        the only part of the entry used afterwards.

    Raises:
        VocabularyError: For an API error, a missing success flag, a malformed item or too
            many items.
    """
    data = _json(text, WIKIDATA)
    if not isinstance(data, dict) or "error" in data or "search" not in data:
        raise VocabularyError("wikidata: unexpected response")
    if data.get("success") != 1:
        raise VocabularyError("wikidata: the API did not report success")
    items = data["search"]
    if not isinstance(items, list) or len(items) > RESULT_LIMIT:
        raise VocabularyError("wikidata: search is not a list of at most 10 items")
    candidates: List[Candidate] = []
    for item in items:
        identifier = item.get("id", "")
        if not isinstance(identifier, str) or not _QID.fullmatch(identifier):
            raise VocabularyError("wikidata: an item id is not Q<digits>")
        label, description = item.get("label", ""), item.get("description", "")
        if not isinstance(label, str) or not isinstance(description, str):
            raise VocabularyError("wikidata: a label or description is not text")
        candidates.append(Candidate(WIKIDATA, WIKIDATA_ENTITY + identifier, identifier, label, description))
    return candidates


def _parse_qudt(text: str) -> List[Candidate]:
    """Group the rows of a SPARQL result into one candidate per quantity kind.

    Args:
        text: The SPARQL JSON results.

    Returns:
        Candidates ordered by label length, then IRI. The English label is preferred to
        ``en-US``.

    Raises:
        VocabularyError: For a missing section, a row of the wrong shape, a label or
            description that is not text, an IRI that is not a QUDT quantity kind, or more
            than ``RESULT_LIMIT`` kinds.
    """
    data = _json(text, QUDT)
    try:
        rows = data["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise VocabularyError("qudt: unexpected response") from exc
    if not isinstance(rows, list):
        raise VocabularyError("qudt: bindings is not a list")
    kinds: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        label, language = row["label"]["value"], row["label"].get("xml:lang", "")
        if not isinstance(label, str) or not isinstance(language, str):
            raise VocabularyError("qudt: a label is not text")
        entry = kinds.setdefault(
            row["qk"]["value"], {"length": int(row["len"]["value"]), "labels": {}, "descriptions": set()}
        )
        entry["labels"].setdefault(language, set()).add(label)
        if "description" in row:
            description = row["description"]["value"]
            if not isinstance(description, str):
                raise VocabularyError("qudt: a description is not text")
            entry["descriptions"].add(normalize_text(description))
    if len(kinds) > RESULT_LIMIT:
        raise VocabularyError(f"qudt: more than {RESULT_LIMIT} kinds returned")
    candidates: List[Candidate] = []
    for iri, entry in sorted(kinds.items(), key=lambda item: (item[1]["length"], item[0])):
        labels = entry["labels"].get("en") or entry["labels"].get("en-US") or next(iter(entry["labels"].values()))
        descriptions = sorted(d for d in entry["descriptions"] if d)
        candidates.append(
            Candidate(QUDT, iri, _local_id(QUDT, iri), sorted(labels)[0], descriptions[0] if descriptions else "")
        )
    return candidates


def _parse_om(text: str) -> List[Candidate]:
    """Read the ranked classes from an OLS search response.

    Args:
        text: The OLS search JSON.

    Returns:
        Candidates in OLS relevance order.

    Raises:
        VocabularyError: For a missing section, a non-zero service status, a document count
            that contradicts ``numFound``, a foreign ontology, an IRI that is not an OM-2
            IRI, a label or description that is not text, or too many documents.
    """
    data = _json(text, OM)
    try:
        documents = data["response"]["docs"]
        found = data["response"]["numFound"]
    except (KeyError, TypeError) as exc:
        raise VocabularyError("om: unexpected response") from exc
    status = (data.get("responseHeader") or {}).get("status")
    if status != 0:
        raise VocabularyError("om: the service did not report status 0")
    if not isinstance(documents, list) or len(documents) > RESULT_LIMIT:
        raise VocabularyError("om: docs is not a list of at most 10 items")
    if isinstance(found, bool) or not isinstance(found, int) or len(documents) != min(found, RESULT_LIMIT):
        raise VocabularyError("om: numFound contradicts the number of documents")
    candidates: List[Candidate] = []
    for document in documents:
        if document.get("ontology_name") != OM or not document.get("iri"):
            raise VocabularyError("om: a document is not an OM entry")
        label = document.get("label")
        raw = document.get("description") or []
        if not isinstance(label, str) or not label:
            raise VocabularyError("om: a document has no text label")
        if not isinstance(raw, list) or not all(isinstance(d, str) for d in raw):
            raise VocabularyError("om: a description is not a list of text")
        descriptions = [d for d in raw if d]
        iri = document["iri"]
        candidates.append(
            Candidate(OM, iri, _local_id(OM, iri), label, normalize_text(descriptions[0]) if descriptions else "")
        )
    return candidates


_PARSERS: Dict[str, Callable[[str], List[Candidate]]] = {
    WIKIDATA: _parse_wikidata,
    QUDT: _parse_qudt,
    OM: _parse_om,
}


def parse_response(source: str, text: str) -> List[Candidate]:
    """Read the candidates out of one source's response body.

    Args:
        source: ``wikidata``, ``qudt`` or ``om``.
        text: The response body.

    Returns:
        At most ``RESULT_LIMIT`` candidates; an empty list is a genuine "nothing found".

    Raises:
        VocabularyError: If the body does not have the shape the source promises.
    """
    try:
        return _PARSERS[source](text)
    except VocabularyError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, ArithmeticError) as exc:
        # ArithmeticError covers a JSON number such as Infinity where a count is expected.
        raise VocabularyError(f"{source}: malformed response ({type(exc).__name__})") from exc


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


def search_source(source: str, key: str) -> List[Candidate]:
    """Ask one vocabulary for the candidates of a key.

    Args:
        source: ``wikidata``, ``qudt`` or ``om``.
        key: The query key: the normalised term in lower case.

    Returns:
        The candidates; empty when the source has none or cannot search for the key.

    Raises:
        VocabularyError: On a transport failure, a non-200 status, an undecodable body or a
            response of the wrong shape.

    Side effects:
        One HTTP GET to the source's public endpoint.
    """
    request = search_request(source, key)
    if request is None:
        return []
    try:
        response = get_http_session().get(
            request.url, params=request.params, headers=request.headers, timeout=REQUEST_TIMEOUT_S
        )
    except requests.RequestException as exc:
        raise VocabularyError(f"{source}: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise VocabularyError(f"{source}: HTTP {response.status_code}")
    try:
        text = response.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VocabularyError(f"{source}: the response is not UTF-8") from exc
    return parse_response(source, text)


def retrieve_pool(key: str) -> Tuple[List[Candidate], List[str]]:
    """Search every vocabulary for a key and pool what answered.

    The three searches run at the same time: they go to three different hosts, so no host
    sees more than one request from a label. A source that fails is left out of the pool
    and named in the second result, never turned into an empty answer.

    Args:
        key: The query key: the normalised term in lower case.

    Returns:
        The pooled candidates in ``SOURCES`` order, and the names of the sources that failed.

    Side effects:
        Up to three HTTP GETs; a warning per failed source, carrying neither the key nor any
        response text.
    """
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as executor:
        futures = {source: executor.submit(search_source, source, key) for source in SOURCES}
    pool: List[Candidate] = []
    failed: List[str] = []
    for source in SOURCES:
        try:
            pool.extend(futures[source].result())
        except VocabularyError as exc:
            failed.append(source)
            logger.warning("entity linking: search failed: %s", exc)
    return pool, failed
