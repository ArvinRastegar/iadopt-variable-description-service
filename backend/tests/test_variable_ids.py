"""Variable IRIs from the nanopublication's own artifact code, kept by a record's later publications (D-057).

On Tobias Kuhn's advice the first publication of a record writes its variable as
``https://w3id.org/iadopt/variable/~~~ARTIFACTCODE~~~``, and signing puts the nanopublication's artifact code in
place of the placeholder (nanopub-py 2.2.1 and later). Every later publication from the record keeps that IRI and
supersedes the record's latest nanopublication, so the network, Nanodash's lookup and the I-ADOPT catalogue list the
variable once, at its newest version.

Signing runs with the real library and a key made in memory for these tests. Nothing is ever published: the
library's publish step is replaced by signing alone, and the ORCID lookup by a fixed name.
"""

from __future__ import annotations

import io
import json
import unittest
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from nanopub import Nanopub, NanopubConf, Profile
from nanopub.namespaces import NPX
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, XSD
from test_records import CLIENT, PREPARED, RecordTestCase

from app.core import dependencies as core_deps
from app.routers import nanopub as nanopub_router
from app.services import nanopub_service
from app.services.nanopub_service import (
    ARTIFACT_CODE_PLACEHOLDER,
    NEW_VARIABLE_URI,
    add_nanopub_metadata,
    as_published,
    with_variable_uri,
)

VARIABLE_BASE = "https://w3id.org/iadopt/variable/"
DRAFT_IRI = URIRef("https://w3id.org/iadopt/variable/20261113T120000-07")  # the IRI PREPARED carries
IOP_VARIABLE = URIRef("https://w3id.org/iadopt/ont/Variable")
ORCID_ID = "0000-0002-1825-0097"
SERVER = "https://registry.example.org"
CODE = r"RA[A-Za-z0-9_\-]{43}"

_PROFILE: List[Profile] = []


def profile() -> Profile:
    """A signing key made in memory once for this module; it is never written anywhere."""
    if not _PROFILE:
        _PROFILE.append(Profile(orcid_id=f"https://orcid.org/{ORCID_ID}", name="Test signer"))
    return _PROFILE[0]


def conf() -> NanopubConf:
    return NanopubConf(profile=profile(), use_server="http://127.0.0.1:9/", add_prov_generated_time=False,
                       add_pubinfo_generated_time=False, attribute_assertion_to_profile=False,
                       attribute_publication_to_profile=False)


def code_of(nanopub_uri: str) -> str:
    return nanopub_uri.rstrip("/").rsplit("/", 1)[-1]


def variables(graph: Graph) -> List[str]:
    return sorted(str(s) for s in graph.subjects(RDF.type, IOP_VARIABLE))


def objects(graph: Graph, predicate: URIRef) -> List[str]:
    return sorted(str(o) for o in graph.objects(None, predicate))


def signed(supersedes: Optional[str] = None, variable: URIRef = NEW_VARIABLE_URI) -> Nanopub:
    """PREPARED as the publish route builds it, signed by the real library."""
    graph = with_variable_uri(Graph().parse(data=PREPARED, format="turtle"), DRAFT_IRI, variable)
    nanopub = Nanopub(assertion=graph, conf=conf())
    with patch.object(nanopub_service, "resolve_creator_metadata", return_value=(f"https://orcid.org/{ORCID_ID}", "Test")):
        add_nanopub_metadata(nanopub, variable_uri=variable, created_at=Literal("2026-11-27T21:00:00Z", datatype=XSD.dateTime),
                             agent_uri=None, creator_orcid_id=ORCID_ID, supersedes=supersedes)
    nanopub.sign()
    return nanopub


# ------------------------------------------------------------------------------------------------
# The library: signing puts the code where the placeholder was
# ------------------------------------------------------------------------------------------------


class SigningTests(unittest.TestCase):
    def test_signing_puts_the_nanopublications_code_wherever_the_placeholder_was(self):
        nanopub = signed()
        expected = VARIABLE_BASE + code_of(nanopub.source_uri)
        self.assertRegex(code_of(nanopub.source_uri), f"^{CODE}$")
        self.assertEqual(variables(nanopub.assertion), [expected])
        self.assertEqual(objects(nanopub.assertion, DCTERMS.identifier), [expected])
        self.assertEqual(objects(nanopub.pubinfo, NPX.introduces), [expected])
        self.assertEqual(objects(nanopub.pubinfo, NPX.supersedes), [])
        self.assertNotIn(ARTIFACT_CODE_PLACEHOLDER, nanopub.rdf.serialize(format="trig"))
        self.assertEqual(as_published(str(NEW_VARIABLE_URI), nanopub.source_uri), expected)

    def test_a_new_version_keeps_the_iri_and_names_the_one_it_replaces(self):
        first = signed()
        kept = URIRef(as_published(str(NEW_VARIABLE_URI), first.source_uri))
        second = signed(supersedes=first.source_uri, variable=kept)
        self.assertNotEqual(code_of(second.source_uri), code_of(first.source_uri))
        self.assertEqual(variables(second.assertion), [str(kept)])
        self.assertEqual(objects(second.pubinfo, NPX.introduces), [str(kept)])
        self.assertEqual(objects(second.pubinfo, NPX.supersedes), [first.source_uri])
        self.assertEqual(as_published(str(kept), second.source_uri), str(kept))

    def test_renaming_the_variable_leaves_the_draft_graph_and_the_other_resources_alone(self):
        draft = Graph().parse(data=PREPARED, format="turtle")
        before = set(draft)
        renamed = with_variable_uri(draft, DRAFT_IRI, NEW_VARIABLE_URI)
        self.assertEqual(set(draft), before)
        self.assertEqual(len(renamed), len(draft))
        self.assertNotIn(DRAFT_IRI, {term for triple in renamed for term in triple})
        self.assertEqual(objects(renamed, DCTERMS.identifier), [str(NEW_VARIABLE_URI)])
        self.assertIn(URIRef("https://www.wikidata.org/entity/Q283"), set(renamed.subjects()))


# ------------------------------------------------------------------------------------------------
# The publish route and the record
# ------------------------------------------------------------------------------------------------


class PublishedIriTests(RecordTestCase):
    """The route with the real library; its publish step only signs."""

    def setUp(self) -> None:
        super().setUp()
        self.published: List[Nanopub] = []

        def sign_only(nanopub: Nanopub) -> Tuple[str, str]:
            nanopub.sign()
            self.published.append(nanopub)
            return nanopub.source_uri, SERVER

        for target, name, value in (
            (nanopub_router, "get_nanopub_profile", lambda: profile()),
            (nanopub_router, "get_nanopub_agent_uri", lambda: None),
            (nanopub_service, "resolve_creator_metadata", lambda orcid: (f"https://orcid.org/{ORCID_ID}", "Test")),
            (Nanopub, "publish", sign_only),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.login()
        self.record_id = self.start(PREPARED)
        self.mark(self.record_id, 0)

    def publish_once(self) -> Dict[str, Any]:
        response = self.client.post("/api/nanopub/publish", json={
            "ttl": PREPARED, "creator_orcid_id": ORCID_ID, "record_id": self.record_id, "client": CLIENT})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def events(self) -> List[Dict[str, Any]]:
        return self.rows("SELECT nanopub_uri, variable_uri FROM record_events WHERE type = 'publish' ORDER BY seq")

    def test_the_first_publication_gets_its_iri_from_its_own_code(self):
        result = self.publish_once()
        [nanopub] = self.published
        expected = VARIABLE_BASE + code_of(nanopub.source_uri)
        self.assertEqual((result["nanopub_url"], result["variable_uri"], result["variable_identifier"]),
                         (nanopub.source_uri, expected, expected))
        self.assertEqual(variables(nanopub.assertion), [expected])
        self.assertEqual(objects(nanopub.pubinfo, NPX.introduces), [expected])
        self.assertEqual(objects(nanopub.pubinfo, NPX.supersedes), [])
        self.assertEqual(self.events(), [{"nanopub_uri": nanopub.source_uri, "variable_uri": expected}])
        # The record's versions stay as they were: the marked version is what was published, draft IRI and all.
        summary = self.summary(self.record_id)
        self.assertEqual([v["number"] for v in summary["versions"]], [0])
        self.assertEqual([p["version"] for p in summary["publications"]], [0])
        [audit] = [e for e in self.audit("nanopub.publish") if e["status_code"] == 200]
        self.assertEqual(json.loads(audit["metadata"])["variable_uri"], expected)

    def test_later_publications_keep_the_iri_and_replace_the_one_before(self):
        first = self.publish_once()
        second = self.publish_once()
        third = self.publish_once()
        kept = first["variable_uri"]
        self.assertEqual([second["variable_uri"], third["variable_uri"]], [kept, kept])
        nanopubs = [nanopub.source_uri for nanopub in self.published]
        self.assertEqual(len(set(nanopubs)), 3)
        for nanopub, replaced in zip(self.published[1:], nanopubs):
            self.assertEqual(variables(nanopub.assertion), [kept])
            self.assertEqual(objects(nanopub.assertion, DCTERMS.identifier), [kept])
            self.assertEqual(objects(nanopub.pubinfo, NPX.introduces), [kept])
            self.assertEqual(objects(nanopub.pubinfo, NPX.supersedes), [replaced])
        self.assertEqual(self.events(), [{"nanopub_uri": n, "variable_uri": kept} for n in nanopubs])

    def test_a_record_published_before_keeps_the_iri_it_published(self):
        """A publication recorded before 0008 has no IRI of its own: the one in its version's Turtle counts.

        The record store writes it as the code before this change did, without ``variable_uri`` (the events are
        append-only, so an existing one cannot be emptied).
        """
        target = core_deps.record_store.publish_target(
            user=self.bob, record_id=self.record_id, graph=Graph().parse(data=PREPARED, format="turtle"))
        earlier = "https://w3id.org/np/RA" + "e" * 43
        core_deps.record_store.record_publish(user=self.bob, request=None, target=target, turtle=PREPARED,
                                              nanopub_uri=earlier, orcid=f"https://orcid.org/{ORCID_ID}")
        self.assertEqual(self.events(), [{"nanopub_uri": earlier, "variable_uri": None}])
        second = self.publish_once()
        self.assertEqual(second["variable_uri"], str(DRAFT_IRI))
        nanopub = self.published[-1]
        self.assertEqual(variables(nanopub.assertion), [str(DRAFT_IRI)])
        self.assertEqual(objects(nanopub.pubinfo, NPX.supersedes), [earlier])
        self.assertEqual(self.events()[-1], {"nanopub_uri": nanopub.source_uri, "variable_uri": str(DRAFT_IRI)})

    def test_another_record_starts_a_variable_of_its_own(self):
        first = self.publish_once()
        other = self.start(PREPARED)
        self.mark(other, 0)
        self.record_id = other
        second = self.publish_once()
        self.assertNotEqual(second["variable_uri"], first["variable_uri"])
        self.assertEqual(objects(self.published[-1].pubinfo, NPX.supersedes), [])

    def test_the_export_names_the_iri_each_publication_introduced(self):
        first = self.publish_once()
        self.publish_once()
        self.client.post("/api/auth/logout")
        self.login("admin", "admin-password")
        response = self.client.get("/api/admin/records/export")
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            document = json.loads(archive.read("records.jsonld"))
        [record] = [node for node in document["@graph"] if node["@id"].endswith(self.record_id)]
        published = [step for step in record["steps"] if step["kind"] == "publish"]
        self.assertEqual([step["introduced"] for step in published], [first["variable_uri"]] * 2)
        self.assertEqual(document["@context"]["introduced"], {"@id": "rec:introduced", "@type": "@id"})
