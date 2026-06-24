"""ORCID resolution: normalization and public display-name lookup.

This service resolves a creator's ORCID and human-readable name, used by RDF/TTL
generation and nanopub publishing for provenance metadata. It is a leaf in the
service layer: it depends only on ``core.text``, ``clients.http``, ``core.config``,
and the shared normalization helpers in ``schemas.common`` — never on the nanopub
or rdf_ttl services (which depend on it).

The ORCID/URI normalization rules live in ``schemas.common`` (``normalize_orcid`` /
``orcid_suffix``) and are re-exported here for the rest of the backend.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Tuple

from ..clients.http import get_http_session
from ..core.config import settings
from ..core.text import normalize_text
from ..schemas.common import normalize_orcid, orcid_suffix

__all__ = [
    "normalize_orcid",
    "orcid_suffix",
    "extract_orcid_display_name",
    "lookup_orcid_display_name",
    "resolve_creator_metadata",
]

# Process-wide cache of resolved ORCID display names (None = looked up, not found).
_orcid_name_cache: dict[str, Optional[str]] = {}


def extract_orcid_display_name(payload: Any) -> Optional[str]:
    """Pull a human-readable name from ORCID public JSON-LD or record-style JSON.

    Args:
        payload: A parsed ORCID record (any shape); non-dicts yield ``None``.

    Returns:
        The resolved display name, or ``None`` if none could be extracted.
    """
    if not isinstance(payload, dict):
        return None

    direct_name = normalize_text(payload.get("name") if isinstance(payload.get("name"), str) else "")
    if direct_name:
        return direct_name

    name_node = payload.get("name")
    if isinstance(name_node, dict):
        credit_name = name_node.get("credit-name")
        if isinstance(credit_name, dict):
            value = normalize_text(credit_name.get("value") or "")
            if value:
                return value
        elif isinstance(credit_name, str):
            value = normalize_text(credit_name)
            if value:
                return value

        given_names = name_node.get("given-names")
        family_name = name_node.get("family-name")
        given_value = normalize_text(given_names.get("value") if isinstance(given_names, dict) else given_names or "")
        family_value = normalize_text(family_name.get("value") if isinstance(family_name, dict) else family_name or "")
        combined = normalize_text(f"{given_value} {family_value}")
        if combined:
            return combined

    given_name = payload.get("givenName")
    family_name = payload.get("familyName")
    given_value = normalize_text(given_name.get("name") if isinstance(given_name, dict) else given_name or "")
    family_value = normalize_text(family_name.get("name") if isinstance(family_name, dict) else family_name or "")
    combined = normalize_text(f"{given_value} {family_value}")
    if combined:
        return combined

    return None


def lookup_orcid_display_name(orcid_id: Optional[str]) -> Optional[str]:
    """Resolve the public display name for an ORCID via content negotiation.

    Results (including misses) are cached for the process lifetime.

    Args:
        orcid_id: A bare ORCID, full ORCID URI, or ``None``.

    Returns:
        The public display name, or ``None`` if unavailable.

    Side effects:
        Performs an HTTP GET against the ORCID registry on a cache miss.
    """
    normalized_orcid = normalize_orcid(orcid_id)
    if not normalized_orcid:
        return None

    if normalized_orcid in _orcid_name_cache:
        return _orcid_name_cache[normalized_orcid]

    try:
        response = get_http_session().get(
            normalized_orcid,
            headers={
                # ORCID documents content negotiation on the registry URL itself, so prefer machine-readable forms.
                "Accept": "application/ld+json, application/json;q=0.9, text/html;q=0.8",
            },
            timeout=20,
        )
        response.raise_for_status()
    except Exception:
        _orcid_name_cache[normalized_orcid] = None
        return None

    resolved_name: Optional[str] = None
    body = response.text
    content_type = response.headers.get("Content-Type", "").lower()

    if "json" in content_type or body.lstrip().startswith("{"):
        try:
            resolved_name = extract_orcid_display_name(response.json())
        except Exception:
            resolved_name = None

    if not resolved_name and "<script" in body:
        for match in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            body,
            re.IGNORECASE | re.DOTALL,
        ):
            try:
                resolved_name = extract_orcid_display_name(json.loads(match.group(1).strip()))
            except Exception:
                resolved_name = None
            if resolved_name:
                break

    if not resolved_name:
        meta_match = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            body,
            re.IGNORECASE,
        )
        if meta_match:
            resolved_name = normalize_text(meta_match.group(1))

    _orcid_name_cache[normalized_orcid] = resolved_name
    return resolved_name


def resolve_creator_metadata(creator_orcid_id: Optional[str] = None) -> Tuple[str, str]:
    """Resolve the creator ORCID URI and public name for provenance metadata.

    Args:
        creator_orcid_id: Optional ORCID override; falls back to the configured
            ``NANOPUB_ORCID_ID``.

    Returns:
        A ``(orcid_uri, display_name)`` tuple.

    Raises:
        RuntimeError: If no ORCID is configured, or no public name can be resolved.

    Side effects:
        May perform an ORCID HTTP lookup (via :func:`lookup_orcid_display_name`).
    """
    resolved_orcid = normalize_orcid(creator_orcid_id) or normalize_orcid(settings.nanopub_orcid_id)
    resolved_profile_name = lookup_orcid_display_name(resolved_orcid)

    if not resolved_orcid:
        raise RuntimeError("No creator ORCID is configured. Provide it in the request or set NANOPUB_ORCID_ID.")

    if not resolved_profile_name:
        raise RuntimeError(
            "No public creator name could be resolved from the selected ORCID. Use an ORCID with a public name."
        )

    return resolved_orcid, resolved_profile_name
