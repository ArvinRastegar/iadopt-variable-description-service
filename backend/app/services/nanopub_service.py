"""Nanopublication signing, publishing, and retraction (security-sensitive).

Owns the signing ``Profile`` (built from ``NANOPUB_PRIVATE_KEY`` / ``_PUBLIC_KEY``),
the software-agent URI/label resolution, the publish-metadata assembly, and the
retraction nanopub construction plus its key-ownership guard.

SECURITY: this module loads the private signing key and builds publications that
write IRREVERSIBLY to the public nanopub registry. Key handling is isolated here.

Depends on ``core.text``, ``core.config``, and ``services.orcid``.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from nanopub import Nanopub, NanopubConf, Profile
from nanopub.namespaces import NPX, NTEMPLATE
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, FOAF, PROV, RDF, RDFS, SKOS, XSD

from ..core.config import settings
from ..core.text import normalize_text
from .orcid import lookup_orcid_display_name, normalize_orcid, resolve_creator_metadata

IADOPT_VARIABLE_CLASS = URIRef("https://w3id.org/iadopt/ont/Variable")

# Lazily-resolved, process-wide caches.
_nanopub_profile: Optional[Profile] = None
_nanopub_agent_uri_cache: Optional[str] = None
_nanopub_agent_label_cache: Optional[str] = None


def normalize_env_multiline(value: Optional[str]) -> Optional[str]:
    r"""Turn ``\n`` escapes in a ``.env`` value into real newlines before key parsing.

    Args:
        value: Raw env value or ``None``.

    Returns:
        The de-escaped, stripped value, or ``None``.
    """
    if value is None:
        return None
    return value.strip().replace("\\n", "\n")


def normalize_nanopub_key(value: Optional[str]) -> Optional[str]:
    """Normalize a PEM block or base64 body to the base64 form ``nanopub-py`` expects.

    Tolerates surrounding quotes (Portainer/Swarm pass values verbatim) and strips
    PEM armor lines.

    Args:
        value: The raw key value from the environment.

    Returns:
        The concatenated base64 key body, or ``None`` if empty.
    """
    normalized = normalize_env_multiline(value)
    if not normalized:
        return None

    # python-dotenv strips surrounding quotes from `.env` values, but Portainer/Swarm pass environment
    # values verbatim. Drop any quotes that were copied along with the key so the PEM armor below is
    # still recognized (otherwise the markers stay glued to the base64 body and decoding fails).
    normalized = normalized.strip()
    while len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in ("'", '"'):
        normalized = normalized[1:-1].strip()

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines:
        return None

    # Strip PEM armor wherever it appears, tolerating stray characters left around the markers.
    lines = [line for line in lines if "-----BEGIN " not in line and "-----END " not in line]

    return "".join(lines)


def get_nanopub_profile() -> Profile:
    """Build (once) and return the signing profile from the configured key + ORCID.

    Returns:
        The cached ``nanopub.Profile``.

    Raises:
        RuntimeError: If required signing configuration is missing, or no signing
            name can be resolved.

    Side effects:
        May fetch the agent-intro nanopub and/or perform an ORCID lookup.
    """
    global _nanopub_profile

    if _nanopub_profile is None:
        missing = []
        if not settings.nanopub_private_key:
            missing.append("NANOPUB_PRIVATE_KEY")
        if not settings.nanopub_orcid_id:
            missing.append("NANOPUB_ORCID_ID")
        if missing:
            raise RuntimeError(f"Missing nanopub publishing configuration: {', '.join(missing)}")

        agent_uri = get_nanopub_agent_uri()
        signing_uri = agent_uri or normalize_orcid(settings.nanopub_orcid_id)
        signing_name = get_nanopub_agent_label() if agent_uri else lookup_orcid_display_name(signing_uri)
        if not signing_name:
            raise RuntimeError(
                "No signing profile name is available from the configured software-agent intro or ORCID."
            )

        _nanopub_profile = Profile(
            # `nanopub-py` names this argument `orcid_id`, but it writes it directly to `npx:signedBy`.
            # When the private/public key pair belongs to the service, the signer URI must be the service URI.
            orcid_id=signing_uri,  # type: ignore[arg-type]  # validated non-None above via missing-config check
            name=signing_name,
            private_key=normalize_nanopub_key(settings.nanopub_private_key),
            public_key=normalize_nanopub_key(settings.nanopub_public_key),
        )

    return _nanopub_profile


def get_nanopub_agent_uri() -> Optional[str]:
    """Resolve and cache the software-agent concept URI from its introduction nanopub.

    Returns:
        The agent concept URI, or ``None`` if no intro URI is configured.

    Raises:
        RuntimeError: If the configured intro nanopub introduces no concept.

    Side effects:
        Fetches the introduction nanopub on first resolution.
    """
    global _nanopub_agent_uri_cache, _nanopub_agent_label_cache

    if _nanopub_agent_uri_cache:
        return _nanopub_agent_uri_cache

    if not settings.nanopub_agent_intro_uri:
        return None

    intro_nanopub = Nanopub(source_uri=settings.nanopub_agent_intro_uri)
    introduced_concept = intro_nanopub.introduces_concept
    if introduced_concept is None:
        raise RuntimeError(
            "Configured NANOPUB_AGENT_INTRO_URI does not introduce a concept. "
            "Provide a valid introduction nanopub for the software agent."
        )

    _nanopub_agent_uri_cache = str(introduced_concept)
    concept_ref = URIRef(_nanopub_agent_uri_cache)
    for graph in (intro_nanopub.assertion, intro_nanopub.pubinfo, intro_nanopub.rdf):
        for predicate in (RDFS.label, SKOS.prefLabel, FOAF.name):
            label = graph.value(concept_ref, predicate)
            if label and str(label).strip():
                _nanopub_agent_label_cache = str(label).strip()
                return _nanopub_agent_uri_cache
    return _nanopub_agent_uri_cache


def get_nanopub_agent_label() -> Optional[str]:
    """Return the software-agent label, falling back to a slug of its URI.

    Returns:
        The label, or ``None`` if no agent URI is configured.
    """
    agent_uri = get_nanopub_agent_uri()
    if not agent_uri:
        return None
    if _nanopub_agent_label_cache:
        return _nanopub_agent_label_cache
    slug = urllib.parse.unquote(agent_uri.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1])
    return normalize_text(slug.replace("_", " ").replace("-", " "))


def nanopub_created_literal() -> Literal:
    """Create a single ``xsd:dateTime`` literal for the publication timestamp."""
    created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return Literal(created_at.replace("+00:00", "Z"), datatype=XSD.dateTime)


def extract_variable_uri(assertion_graph: Graph) -> URIRef:
    """Find the ``iop:Variable`` resource URI in the assertion graph.

    Args:
        assertion_graph: The parsed Turtle assertion.

    Returns:
        The variable resource URI.

    Raises:
        RuntimeError: If no URI-subject variable resource is present.
    """
    for subject in assertion_graph.subjects(RDF.type, IADOPT_VARIABLE_CLASS):
        if isinstance(subject, URIRef):
            return subject

    raise RuntimeError("The Turtle assertion does not contain an `iop:Variable` resource with a URI subject.")


def extract_assertion_label(assertion_graph: Graph, variable_uri: URIRef) -> Optional[str]:
    """Return the variable's ``rdfs:label`` to reuse as the nanopub label, if present."""
    label = assertion_graph.value(variable_uri, RDFS.label)
    if label is None:
        return None
    label_text = str(label).strip()
    return label_text or None


def extract_variable_identifier(assertion_graph: Graph, variable_uri: URIRef) -> str:
    """Return the variable identifier (``dct:identifier`` or the URI's last segment)."""
    identifier = assertion_graph.value(variable_uri, DCTERMS.identifier)
    if identifier is not None and str(identifier).strip():
        return str(identifier).strip()
    return str(variable_uri).rstrip("/").rsplit("/", 1)[-1]


def normalize_target_nanopub_uri(raw_value: str) -> str:
    """Normalize a nanopub reference to its canonical ``https://w3id.org/np/RA...`` URI.

    Accepts saved nanopub URLs, raw ``RA...`` identifiers, or Nanodash explore links.

    Args:
        raw_value: The user-provided reference.

    Returns:
        The canonical nanopub URI.

    Raises:
        RuntimeError: If empty or not a recognizable nanopub reference.
    """
    candidate = (raw_value or "").strip()
    if not candidate:
        raise RuntimeError("No nanopub URI was provided for retraction.")

    # Support Nanodash explore URLs such as `.../explore?id=RA...` by extracting the underlying nanopub identifier.
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        query_id = urllib.parse.parse_qs(parsed.query).get("id", [])
        if query_id and query_id[0]:
            candidate = query_id[0].strip()
        else:
            trusty_match = re.search(r"(RA[A-Za-z0-9_-]+)", candidate)
            if trusty_match:
                candidate = trusty_match.group(1)

    if re.fullmatch(r"RA[A-Za-z0-9_-]+", candidate):
        return f"https://w3id.org/np/{candidate}"

    if candidate.startswith("https://w3id.org/np/"):
        return candidate

    raise RuntimeError(
        "Unsupported nanopub reference. Provide a `https://w3id.org/np/RA...` URI, "
        "a raw `RA...` identifier, or a Nanodash explore URL."
    )


def public_key_prefix(public_key: Optional[str], prefix_length: int = 32) -> str:
    """Return a short prefix of a public key for safe display in error messages."""
    clean_key = (public_key or "").strip()
    if not clean_key:
        return "missing"
    return clean_key[:prefix_length]


def assert_retraction_allowed(target_nanopub_uri: str, profile: Profile) -> None:
    """Verify the target nanopub was signed with the configured key before retracting.

    Args:
        target_nanopub_uri: The canonical URI of the nanopub to retract.
        profile: The configured signing profile.

    Raises:
        RuntimeError: If the target cannot be loaded, exposes no public key, the
            profile has no public key, or the keys do not match.

    Side effects:
        Loads the target nanopub from the registry.
    """
    try:
        target_nanopub = Nanopub(
            source_uri=target_nanopub_uri,
            conf=NanopubConf(use_server=settings.nanopub_publish_server),
        )
    except Exception as e:
        raise RuntimeError(f"Could not load the target nanopub for retraction: {e}") from e

    target_public_key = (target_nanopub.metadata.public_key or "").strip()
    profile_public_key = (profile.public_key or "").strip()

    if not target_public_key:
        raise RuntimeError(
            "The target nanopub does not expose a public key, so retraction ownership cannot be verified."
        )

    if not profile_public_key:
        raise RuntimeError("The configured nanopub profile does not expose a public key.")

    if target_public_key != profile_public_key:
        raise RuntimeError(
            "The target nanopub was not signed with the key currently configured in this backend, so it cannot be "
            "retracted here. "
            f"Target key prefix: {public_key_prefix(target_public_key)} ; "
            f"current key prefix: {public_key_prefix(profile_public_key)}."
        )


def build_retraction_nanopub(
    target_nanopub_uri: str,
    profile: Profile,
    creator_orcid_id: Optional[str] = None,
) -> Nanopub:
    """Build the retraction nanopub mirroring the Nanodash-style pubinfo shape.

    Args:
        target_nanopub_uri: The nanopub being retracted.
        profile: The signing profile.
        creator_orcid_id: Optional ORCID override for provenance/pubinfo.

    Returns:
        The unsigned retraction ``Nanopub`` ready to publish.

    Side effects:
        Resolves creator metadata (possible ORCID lookup) and the agent URI.
    """
    resolved_orcid, resolved_profile_name = resolve_creator_metadata(creator_orcid_id)
    orcid_uri = URIRef(resolved_orcid)
    agent_uri = get_nanopub_agent_uri()
    pubinfo_creator_uri = URIRef(agent_uri) if agent_uri else orcid_uri
    target_identifier = target_nanopub_uri.rsplit("/", 1)[-1]
    retraction_label = f"Retraction of {target_identifier[:10]}"

    assertion_graph = Graph()
    assertion_graph.add((orcid_uri, NPX.retracts, URIRef(target_nanopub_uri)))

    nanopub = Nanopub(
        assertion=assertion_graph,
        conf=NanopubConf(
            profile=profile,
            use_server=settings.nanopub_publish_server,
            add_prov_generated_time=False,
            add_pubinfo_generated_time=False,
            attribute_assertion_to_profile=False,
            attribute_publication_to_profile=False,
        ),
    )

    # The registries accept retractions when they mirror the current Nanodash-style pubinfo shape.
    nanopub.provenance.add((nanopub.assertion.identifier, PROV.wasAttributedTo, orcid_uri))
    if agent_uri:
        nanopub.provenance.add((nanopub.assertion.identifier, PROV.wasGeneratedBy, URIRef(agent_uri)))
    nanopub.pubinfo.add((orcid_uri, FOAF.name, Literal(resolved_profile_name)))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], DCTERMS.created, nanopub_created_literal()))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], DCTERMS.creator, pubinfo_creator_uri))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], DCTERMS.license, URIRef(settings.nanopub_license_uri)))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], NPX.hasNanopubType, NPX.retracts))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], NPX["wasCreatedAt"], URIRef(settings.nanopub_was_created_at)))
    nanopub.pubinfo.add((nanopub.metadata.namespace[""], RDFS.label, Literal(retraction_label)))

    if settings.nanopub_retract_provenance_template_uri:
        nanopub.pubinfo.add(
            (
                nanopub.metadata.namespace[""],
                NTEMPLATE["wasCreatedFromProvenanceTemplate"],
                URIRef(settings.nanopub_retract_provenance_template_uri),
            )
        )

    for template_uri in settings.nanopub_retract_pubinfo_template_uris:
        nanopub.pubinfo.add(
            (
                nanopub.metadata.namespace[""],
                NTEMPLATE["wasCreatedFromPubinfoTemplate"],
                URIRef(template_uri),
            )
        )

    if settings.nanopub_retract_template_uri:
        nanopub.pubinfo.add(
            (
                nanopub.metadata.namespace[""],
                NTEMPLATE["wasCreatedFromTemplate"],
                URIRef(settings.nanopub_retract_template_uri),
            )
        )

    return nanopub


def add_nanopub_metadata(
    nanopub: Nanopub,
    *,
    variable_uri: URIRef,
    created_at: Literal,
    agent_uri: Optional[str],
    creator_orcid_id: Optional[str] = None,
) -> None:
    """Add provenance + pubinfo template metadata to a publish nanopub before signing.

    Args:
        nanopub: The nanopub being assembled (mutated in place).
        variable_uri: The introduced variable URI.
        created_at: The publication timestamp literal.
        agent_uri: The software-agent URI, or ``None``.
        creator_orcid_id: Optional ORCID override.

    Side effects:
        Resolves creator metadata (possible ORCID lookup).
    """
    nanopub_uri = nanopub.metadata.namespace[""]
    resolved_orcid, resolved_profile_name = resolve_creator_metadata(creator_orcid_id)
    orcid_uri = URIRef(resolved_orcid)
    pubinfo_creator_uri = URIRef(agent_uri) if agent_uri else orcid_uri

    # The provenance graph must describe who is responsible for the assertion and which software agent generated it.
    nanopub.provenance.add((nanopub.assertion.identifier, PROV.wasAttributedTo, orcid_uri))
    if agent_uri:
        nanopub.provenance.add((nanopub.assertion.identifier, PROV.wasGeneratedBy, URIRef(agent_uri)))

    # The publication info graph mirrors the creator, license, template, and software metadata requested by the user.
    nanopub.pubinfo.add((orcid_uri, FOAF.name, Literal(resolved_profile_name)))
    nanopub.pubinfo.add((nanopub_uri, DCTERMS.created, created_at))
    nanopub.pubinfo.add((nanopub_uri, DCTERMS.creator, pubinfo_creator_uri))
    nanopub.pubinfo.add((nanopub_uri, DCTERMS.license, URIRef(settings.nanopub_license_uri)))
    nanopub.pubinfo.add((nanopub_uri, NPX.introduces, variable_uri))
    nanopub.pubinfo.add((nanopub_uri, NPX["wasCreatedAt"], URIRef(settings.nanopub_was_created_at)))

    if settings.nanopub_template_uri:
        nanopub.pubinfo.add((nanopub_uri, NTEMPLATE["wasCreatedFromTemplate"], URIRef(settings.nanopub_template_uri)))

    if settings.nanopub_provenance_template_uri:
        nanopub.pubinfo.add(
            (
                nanopub_uri,
                NTEMPLATE["wasCreatedFromProvenanceTemplate"],
                URIRef(settings.nanopub_provenance_template_uri),
            )
        )

    for template_uri in settings.nanopub_pubinfo_template_uris:
        nanopub.pubinfo.add((nanopub_uri, NTEMPLATE["wasCreatedFromPubinfoTemplate"], URIRef(template_uri)))
