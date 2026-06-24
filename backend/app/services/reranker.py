"""PSNC reranker service: score candidate documents against a query.

Thin business wrapper over the PSNC rerank endpoint; used by the Wikidata
enrichment service to pick the best entity match. Depends on ``clients`` only.
"""

from __future__ import annotations

from typing import List

from ..clients.http import get_http_session
from ..clients.psnc_client import psnc_chat_headers, psnc_rerank_url
from ..core.config import settings


def call_psnc_reranker(query: str, documents: List[str]) -> List[float]:
    """Score each document for relevance to the query via the PSNC reranker.

    Args:
        query: The query text.
        documents: Candidate document strings.

    Returns:
        One relevance score per document, in document order (empty if no documents).

    Raises:
        RuntimeError: If the response does not contain exactly one score per
            document, or contains an invalid result/index.

    Side effects:
        Performs an HTTP POST to the PSNC rerank endpoint.
    """
    if not documents:
        return []

    response = get_http_session().post(
        psnc_rerank_url(),
        headers=psnc_chat_headers(),
        json={
            "model": settings.psnc_rerank_model,
            "query": query,
            "documents": documents,
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    raw_results = payload.get("results") if isinstance(payload, dict) else None

    if not isinstance(raw_results, list) or len(raw_results) != len(documents):
        raise RuntimeError("PSNC reranker response did not contain one score per document.")

    scores = [0.0] * len(documents)
    for result in raw_results:
        if not isinstance(result, dict):
            raise RuntimeError("PSNC reranker response contained an invalid result.")
        index = int(result.get("index", -1))
        if index < 0 or index >= len(documents):
            raise RuntimeError("PSNC reranker response contained an invalid document index.")
        scores[index] = float(result.get("relevance_score"))  # type: ignore[arg-type]

    return scores
