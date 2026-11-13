"""Decomposition records (D-036): who changed what, version by version, from draft to publication.

The routes run against a temporary auth database, like ``test_saved_orcids``. Model calls and
the nanopublication registry are always mocked: nothing here reaches an LLM or publishes.
"""

import json
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import main
from app.core import dependencies as core_deps
from app.records import (
    MAX_BLANK_NODE_TRIPLES,
    MAX_BLANK_NODES,
    core_graph,
    graph_sha256,
    parse_turtle,
    turtle_sha256,
)
from app.routers import decompose as decompose_router
from app.routers import nanopub as nanopub_router
from app.services.llm import stream_event

DRAFT = """@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://example.org/variable/1> a iop:Variable ;
    rdfs:label "dissolved oxygen in water" ;
    iop:hasMatrix <https://www.wikidata.org/entity/Q283> .
<https://www.wikidata.org/entity/Q283> rdfs:label "water" .
"""
EDITED = DRAFT.replace('rdfs:label "water"', 'rdfs:label "sea water"')
# The same graph as EDITED, with other prefixes and the triples in another order.
EDITED_REWRITTEN = """@prefix o: <https://w3id.org/iadopt/ont/> .
<https://www.wikidata.org/entity/Q283> <http://www.w3.org/2000/01/rdf-schema#label> "sea water" .
<https://example.org/variable/1> o:hasMatrix <https://www.wikidata.org/entity/Q283> ;
    <http://www.w3.org/2000/01/rdf-schema#label> "dissolved oxygen in water" ;
    a o:Variable .
"""
WITH_METADATA = DRAFT + "<https://example.org/variable/1> <http://purl.org/dc/terms/conformsTo> <https://w3id.org/iadopt/ont/1.1> .\n"
# What the preparation step writes: a minted IRI for the variable, its FAIR Digital Object type,
# its metadata and the creator.
PREPARED = """@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dct: <http://purl.org/dc/terms/> .
<https://w3id.org/iadopt/variable/20261113T120000-07> a iop:Variable , <https://w3id.org/fdof/ontology#FAIRDigitalObject> ;
    rdfs:label "dissolved oxygen in water" ;
    iop:hasMatrix <https://www.wikidata.org/entity/Q283> ;
    dct:identifier <https://w3id.org/iadopt/variable/20261113T120000-07> ;
    dct:created "2026-11-13T12:00:00Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> ;
    dct:conformsTo <https://w3id.org/iadopt/ont/1.1> ;
    <http://purl.org/pav/createdWith> "I-ADOPT Variable Description Service" ;
    dct:creator <https://orcid.org/0000-0002-1825-0097> ;
    <http://www.w3.org/ns/prov#wasAttributedTo> <https://orcid.org/0000-0002-1825-0097> .
<https://www.wikidata.org/entity/Q283> rdfs:label "water" .
"""
CLIENT = {
    "screen": {"width": 1512, "height": 982, "pixel_ratio": 2},
    "device": {"platform": "macOS", "mobile": False, "touch_points": 0},
}
ORCID = "https://orcid.org/0000-0002-1825-0097"
MATRIX = "https://www.wikidata.org/entity/Q283"
PIPELINE_RESULT = {
    "raw_llm_output": '{"definition": "dissolved oxygen in water"}',
    "parsed_json": {"definition": "dissolved oxygen in water", "label": "dissolved oxygen in water"},
    "schema_valid": True,
    "validation_errors": [],
    "enriched_json": {},
    "ttl": DRAFT,
}


class FingerprintTests(unittest.TestCase):
    def test_the_same_graph_in_other_words_has_one_fingerprint(self):
        self.assertEqual(turtle_sha256(EDITED), turtle_sha256(EDITED_REWRITTEN))
        self.assertNotEqual(turtle_sha256(DRAFT), turtle_sha256(EDITED))

    def test_the_core_ignores_publication_metadata_and_a_minted_iri(self):
        def core(turtle):
            return graph_sha256(core_graph(parse_turtle(turtle)))

        self.assertEqual(core(DRAFT), core(WITH_METADATA))
        self.assertEqual(core(DRAFT), core(PREPARED))
        self.assertNotEqual(core(DRAFT), core(EDITED))

    def test_the_core_ignores_system_labels_the_preparation_derives(self):
        def core(turtle):
            return graph_sha256(core_graph(parse_turtle(turtle)))

        with_system = DRAFT + (
            "<https://example.org/variable/1> <https://w3id.org/iadopt/ont/hasContextObject> _:s .\n"
            "_:s a <https://w3id.org/iadopt/ont/AsymmetricSystem> ;\n"
            "    <https://w3id.org/iadopt/ont/hasSource> <https://www.wikidata.org/entity/Q283> .\n"
        )
        labelled = with_system + '_:s <http://www.w3.org/2000/01/rdf-schema#label> "water" .\n'
        self.assertEqual(core(with_system), core(labelled))
        # A label on anything that is not a system still counts.
        self.assertNotEqual(core(DRAFT), core(DRAFT + '<https://example.org/variable/1> <http://www.w3.org/2000/01/rdf-schema#label> "x" .\n'))

    def test_text_that_is_not_turtle_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Could not parse the Turtle"):
            turtle_sha256("this is not turtle")

    def test_graphs_that_are_slow_to_fingerprint_are_refused_quickly(self):
        many = "".join(f"_:a{i} <http://e.org/p> _:b{i} . _:b{i} <http://e.org/p> _:a{i} .\n" for i in range(8))
        # Ten blank nodes, all linked to each other through two predicates: 180 statements about them.
        dense = "".join(
            f"_:n{a} <http://e.org/p{p}> _:n{b} .\n" for p in range(2) for a in range(10) for b in range(10) if a != b
        )
        for turtle, message in ((many, f"at most {MAX_BLANK_NODES}"), (dense, f"at most {MAX_BLANK_NODE_TRIPLES}")):
            with self.subTest(message=message):
                started = time.perf_counter()
                with self.assertRaisesRegex(ValueError, message):
                    turtle_sha256(turtle)
                self.assertLess(time.perf_counter() - started, 1)

    def test_an_iri_without_a_valid_form_is_refused_as_bad_turtle(self):
        with self.assertRaisesRegex(ValueError, "Could not fingerprint"):
            turtle_sha256('<http://e.org/a\\u0020b> <http://e.org/p> "x" .')


class RecordTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_patcher = patch.dict(
            os.environ,
            {
                "IADOPT_BOOTSTRAP_ADMIN_USERNAME": "admin",
                "IADOPT_BOOTSTRAP_ADMIN_PASSWORD": "admin-password",
            },
        )
        self.env_patcher.start()
        # Reconfigure the shared store in place, as test_saved_orcids does, and restore it after.
        self.store = core_deps.auth_store
        self._saved = {attr: getattr(self.store, attr) for attr in ("db_path", "enabled", "session_secret", "cookie_secure")}
        self.store.db_path = pathlib.Path(self.temp_dir.name) / "iadopt.sqlite3"
        self.store.enabled = True
        self.store.session_secret = "test-secret"
        self.store.cookie_secure = False
        self.store.init()
        core_deps.record_store.init()
        self.bob = self.store.create_user(username="bob", password="password-123")
        self.alice = self.store.create_user(username="alice", password="password-123")
        self.client = TestClient(main.app, headers={"User-Agent": "RecordTest/1.0"})

    def tearDown(self):
        for attr, value in self._saved.items():
            setattr(self.store, attr, value)
        self.env_patcher.stop()
        self.temp_dir.cleanup()

    def login(self, username="bob", password="password-123"):
        response = self.client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200)

    def start(self, turtle=DRAFT):
        response = self.client.post("/api/records", json={"turtle": turtle, "client": CLIENT})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["version"], 0)
        return response.json()["record_id"]

    def step(self, record_id, **body):
        body.setdefault("client", CLIENT)
        return self.client.post(f"/api/records/{record_id}/events", json=body)

    def check(self, record_id, version, operation="confirm", step_type="check"):
        return self.step(record_id, type=step_type, version=version,
                         changes=[{"operation": operation, "role": "hasMatrix", "component": MATRIX}])

    def mark(self, record_id, version, unchecked=0):
        response = self.step(record_id, type="mark_best", version=version, unchecked_components=unchecked)
        self.assertEqual(response.status_code, 201, response.text)
        return response

    def summary(self, record_id):
        response = self.client.get(f"/api/records/{record_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def rows(self, sql, *params):
        conn = sqlite3.connect(self.store.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def audit(self, action):
        return [event for event in self.store.get_audit_events(limit=50) if event["action"] == action]


class RecordRouteTests(RecordTestCase):
    def test_records_need_a_session(self):
        self.assertEqual(self.client.post("/api/records", json={"turtle": DRAFT}).status_code, 401)
        self.assertEqual(self.check("0123456789abcdef", 0).status_code, 401)
        self.assertEqual(self.client.get("/api/records/0123456789abcdef").status_code, 401)

    def test_records_need_accounts(self):
        self.store.enabled = False
        response = self.client.post("/api/records", json={"turtle": DRAFT})
        self.assertEqual(response.status_code, 409)

    def test_opened_turtle_starts_a_record_at_version_0(self):
        self.login()
        record_id = self.start()

        summary = self.summary(record_id)
        self.assertEqual(summary["origin"], "import")
        self.assertEqual(summary["current_version"], 0)
        self.assertEqual([version["kind"] for version in summary["versions"]], ["imported"])
        self.assertEqual(summary["event_count"], 1)

        [item] = self.rows("SELECT user_id, definition FROM record_items")
        self.assertEqual(item, {"user_id": self.bob["id"], "definition": None})
        [context] = self.rows("SELECT ip_address, user_agent, device, screen FROM client_contexts")
        self.assertEqual(context["ip_address"], "testclient")
        self.assertEqual(context["user_agent"], "RecordTest/1.0")
        self.assertEqual(json.loads(context["screen"]), CLIENT["screen"])
        self.assertEqual(json.loads(context["device"]), CLIENT["device"])

    def test_unusable_turtle_starts_nothing(self):
        self.login()
        too_many = "".join(f"_:n{i} <http://e.org/p> \"{i}\" .\n" for i in range(MAX_BLANK_NODES + 1))
        for turtle in ("this is not turtle", too_many, '<http://e.org/a\\u0020b> <http://e.org/p> "x" .'):
            with self.subTest(turtle=turtle[:30]):
                self.assertEqual(self.client.post("/api/records", json={"turtle": turtle}).status_code, 400)
        self.assertEqual(self.rows("SELECT * FROM record_items"), [])

    def test_an_applied_change_adds_a_version_with_its_changes(self):
        self.login()
        record_id = self.start()
        change = {"operation": "relabel", "role": "hasMatrix", "component": MATRIX, "before": "water", "after": "sea water"}

        response = self.step(record_id, type="edit", version=0, turtle=EDITED, changes=[change])
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"record_id": record_id, "event": 2, "version": 1})

        # The same graph written differently is the same version.
        again = self.step(record_id, type="edit", version=1, turtle=EDITED_REWRITTEN)
        self.assertEqual(again.json()["version"], 1)

        summary = self.summary(record_id)
        self.assertEqual([(v["number"], v["kind"], v["parent"]) for v in summary["versions"]],
                         [(0, "imported", None), (1, "edited", 0)])
        self.assertEqual(len(self.rows("SELECT * FROM record_contents")), 2)
        [stored] = self.rows("SELECT operation, role, component, before_value, after_value FROM record_changes")
        self.assertEqual(stored, {"operation": "relabel", "role": "hasMatrix", "component": MATRIX,
                                  "before_value": "water", "after_value": "sea water"})

    def test_the_server_decides_what_is_only_metadata(self):
        self.login()
        record_id = self.start()

        # An edit that only adds publication metadata is a metadata version.
        self.assertEqual(self.step(record_id, type="edit", version=0, turtle=WITH_METADATA).json()["version"], 1)
        # A metadata step that changes the description itself is refused.
        refused = self.step(record_id, type="metadata", version=1, turtle=EDITED)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("only add or change nanopublication metadata", refused.json()["detail"])

        self.assertEqual([v["kind"] for v in self.summary(record_id)["versions"]], ["imported", "metadata"])

    def test_checks_and_the_best_mark_point_at_a_version(self):
        self.login()
        record_id = self.start()
        self.step(record_id, type="edit", version=0, turtle=EDITED)

        check = self.check(record_id, 1)
        self.assertEqual(check.status_code, 201, check.text)
        self.assertEqual(check.json()["version"], 1)
        mark = self.step(record_id, type="mark_best", version=1, note="The model used plain water.", unchecked_components=2)
        self.assertEqual(mark.status_code, 201, mark.text)

        summary = self.summary(record_id)
        self.assertEqual(summary["best_version"], 1)
        self.assertEqual(len(summary["versions"]), 2)
        [stored] = self.rows("SELECT note, unchecked_components FROM record_events WHERE type = 'mark_best'")
        self.assertEqual(stored, {"note": "The model used plain water.", "unchecked_components": 2})

    def test_steps_are_checked_before_they_are_stored(self):
        self.login()
        record_id = self.start()
        confirm = [{"operation": "confirm", "role": "hasMatrix"}]
        invalid = (
            {"type": "edit", "version": 0, "turtle": EDITED, "changes": [{"operation": "rewrite"}]},
            {"type": "edit", "version": 0},
            {"type": "edit", "version": 0, "turtle": EDITED, "changes": confirm},
            {"type": "check", "version": 0, "turtle": EDITED, "changes": confirm},
            {"type": "check", "version": 0},
            {"type": "check", "version": 0, "changes": [{"operation": "relabel", "role": "hasMatrix"}]},
            {"type": "check", "version": 0, "changes": [{"operation": "confirm"}]},
            {"type": "uncheck", "version": 0, "changes": confirm},
            {"type": "check", "version": 0, "changes": confirm, "note": "only a mark carries a note"},
            {"type": "mark_best", "version": 0},
            {"type": "mark_best", "version": 0, "unchecked_components": 0, "changes": confirm},
            {"type": "mark_best", "version": 0, "unchecked_components": 0, "note": "x" * 501},
            {"type": "mark_best", "version": -1, "unchecked_components": 0},
            {"type": "mark_best", "version": 10**20, "unchecked_components": 0},
            {"type": "publish", "version": 0},
        )
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.step(record_id, **body).status_code, 422)
        self.assertEqual(self.step(record_id, type="mark_best", version=9, unchecked_components=0).status_code, 404)
        self.assertEqual(self.step(record_id, type="edit", version=0, turtle="not turtle").status_code, 400)
        self.assertEqual(self.check("not-a-record", 0).status_code, 422)
        self.assertEqual(self.summary(record_id)["event_count"], 1)

    def test_implausible_browser_times_are_replaced_by_the_server_time(self):
        self.login()
        record_id = self.start()
        for occurred_at in ("0001-01-01T00:30:00+01:00", "2015-06-01T00:00:00Z", "9999-12-31T23:30:00-01:00"):
            with self.subTest(occurred_at=occurred_at):
                response = self.step(record_id, type="check", version=0, occurred_at=occurred_at,
                                     changes=[{"operation": "confirm", "role": "hasMatrix"}])
                self.assertEqual(response.status_code, 201, response.text)
        now = datetime.now(timezone.utc)
        for row in self.rows("SELECT occurred_at FROM record_events WHERE type = 'check'"):
            self.assertLess(abs(datetime.fromisoformat(row["occurred_at"]) - now), timedelta(minutes=5))

    def test_other_peoples_records_stay_closed(self):
        self.login("bob")
        record_id = self.start()

        self.login("alice")
        self.assertEqual(self.step(record_id, type="mark_best", version=0, unchecked_components=0).status_code, 404)
        self.assertEqual(self.client.get(f"/api/records/{record_id}").status_code, 404)

        self.login("admin", "admin-password")
        self.assertEqual(self.summary(record_id)["record_id"], record_id)

    def test_the_same_place_is_stored_once(self):
        self.login()
        record_id = self.start()
        self.check(record_id, 0)
        self.check(record_id, 0, operation="unconfirm", step_type="uncheck")
        self.assertEqual(len(self.rows("SELECT * FROM client_contexts")), 1)

        other_screen = {**CLIENT, "screen": {"width": 390, "height": 844, "pixel_ratio": 3}}
        self.step(record_id, type="check", version=0, client=other_screen, changes=[{"operation": "confirm", "role": "hasMatrix"}])
        self.assertEqual(len(self.rows("SELECT * FROM client_contexts")), 2)

    def test_records_cannot_be_changed_deleted_or_replaced(self):
        self.login()
        self.start()
        [before] = self.rows("SELECT sha256, turtle FROM record_contents")
        conn = sqlite3.connect(self.store.db_path)
        try:
            for statement in (
                "UPDATE record_events SET note = 'changed'",
                "DELETE FROM record_versions",
                "DELETE FROM client_contexts",
                "REPLACE INTO record_versions (id, item_id, number, kind, content_sha256, parent_id, created_at)"
                " SELECT id, item_id, number, 'edited', content_sha256, parent_id, created_at FROM record_versions",
            ):
                with self.subTest(statement=statement[:40]):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                        conn.execute(statement)
            # A stored content cannot be overwritten either: the insert is dropped.
            conn.execute(
                "INSERT OR REPLACE INTO record_contents (sha256, core_sha256, turtle, byte_size, created_at)"
                " SELECT sha256, core_sha256, 'TAMPERED', byte_size, created_at FROM record_contents"
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.rows("SELECT sha256, turtle FROM record_contents"), [before])
        self.assertEqual([row["kind"] for row in self.rows("SELECT kind FROM record_versions")], ["imported"])

    def test_record_tables_come_back_when_the_database_is_replaced(self):
        self.login()
        self.start()
        os.remove(self.store.db_path)
        self.store.init()
        self.store.create_user(username="bob", password="password-123")
        self.login()
        self.start()
        self.assertEqual(len(self.rows("SELECT * FROM record_items")), 1)


class DecomposeRecordTests(RecordTestCase):
    def test_a_decomposition_starts_a_record_and_the_audit_keeps_no_content(self):
        self.login()
        with patch.object(decompose_router, "run_pipeline", return_value=dict(PIPELINE_RESULT)):
            response = self.client.post(
                "/api/decompose", json={"definition": "dissolved oxygen in water", "client": CLIENT}
            )
        self.assertEqual(response.status_code, 200, response.text)
        record_id = response.json()["record_id"]
        self.assertRegex(record_id, r"^[0-9a-f]{16}$")

        [item] = self.rows("SELECT origin, definition FROM record_items")
        self.assertEqual(item, {"origin": "decompose", "definition": "dissolved oxygen in water"})
        [run] = self.rows("SELECT * FROM record_runs")
        self.assertEqual(run["status"], "ok")
        self.assertTrue(run["provider"])
        self.assertTrue(run["model"])
        self.assertTrue(run["prompt_setup"])
        self.assertRegex(run["prompt_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(run["raw_output"], PIPELINE_RESULT["raw_llm_output"])
        self.assertEqual(run["schema_valid"], 1)
        # The definition is kept once, in record_items.
        self.assertEqual(json.loads(run["parsed_json"]), {"label": "dissolved oxygen in water"})
        self.assertEqual([v["kind"] for v in self.summary(record_id)["versions"]], ["model_draft"])

        [audit] = self.audit("decompose")
        self.assertIsNone(audit["request_payload"])
        self.assertIsNone(audit["response_payload"])
        self.assertEqual(audit["metadata_json"]["record_id"], record_id)

    def test_a_streamed_decomposition_returns_its_record_id(self):
        self.login()
        lines = [stream_event("raw_delta", delta="{"), stream_event("final", data=dict(PIPELINE_RESULT))]
        with patch.object(decompose_router, "stream_pipeline_events", return_value=iter(lines)):
            response = self.client.post("/api/decompose/stream", json={"definition": "dissolved oxygen in water"})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        self.assertEqual([event["type"] for event in events], ["raw_delta", "final"])
        record_id = events[-1]["data"]["record_id"]
        self.assertEqual(self.summary(record_id)["origin"], "decompose")

        [audit] = self.audit("decompose.stream")
        self.assertIsNone(audit["request_payload"])
        self.assertIsNone(audit["response_payload"])
        self.assertEqual(audit["metadata_json"]["record_id"], record_id)

    def test_streamed_requests_the_pipeline_refuses_are_not_recorded(self):
        self.login()
        for body in ({"definition": "x", "model_name": "no-such-model"}, {"definition": "   "}):
            with self.subTest(body=body):
                lines = [stream_event("error", detail="Unsupported model.")]
                with patch.object(decompose_router, "stream_pipeline_events", return_value=iter(lines)):
                    response = self.client.post("/api/decompose/stream", json=body)
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows("SELECT * FROM record_items"), [])

    def test_a_failed_decomposition_keeps_who_and_where_without_a_version(self):
        self.login()
        with patch.object(decompose_router, "run_pipeline", side_effect=RuntimeError("model unavailable")):
            response = self.client.post(
                "/api/decompose", json={"definition": "dissolved oxygen in water", "client": CLIENT}
            )
        self.assertEqual(response.status_code, 500)
        [run] = self.rows("SELECT status, error FROM record_runs")
        self.assertEqual(run, {"status": "error", "error": "model unavailable"})
        self.assertEqual(self.rows("SELECT * FROM record_versions"), [])
        [event] = self.rows(
            "SELECT e.type, e.version_id, c.screen FROM record_events e JOIN client_contexts c ON c.id = e.context_id"
        )
        self.assertEqual((event["type"], event["version_id"]), ("draft", None))
        self.assertEqual(json.loads(event["screen"]), CLIENT["screen"])

    def test_odd_device_data_never_fails_a_decomposition(self):
        self.login()
        odd = {"device": {"touch_points": 5000}, "screen": {"width": -1}}
        with patch.object(decompose_router, "run_pipeline", return_value=dict(PIPELINE_RESULT)):
            response = self.client.post("/api/decompose", json={"definition": "dissolved oxygen in water", "client": odd})
        self.assertEqual(response.status_code, 200, response.text)
        [context] = self.rows("SELECT device, screen FROM client_contexts")
        self.assertEqual(context, {"device": "", "screen": ""})

    def test_nothing_is_recorded_without_authentication(self):
        self.store.enabled = False
        with patch.object(decompose_router, "run_pipeline", return_value=dict(PIPELINE_RESULT)):
            response = self.client.post("/api/decompose", json={"definition": "dissolved oxygen in water"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["record_id"])

        lines = [stream_event("final", data=dict(PIPELINE_RESULT))]
        with patch.object(decompose_router, "stream_pipeline_events", return_value=iter(lines)):
            streamed = self.client.post("/api/decompose/stream", json={"definition": "dissolved oxygen in water"})
        final = json.loads(streamed.text.splitlines()[-1])
        self.assertIn("record_id", final["data"])
        self.assertIsNone(final["data"]["record_id"])
        self.assertEqual(self.rows("SELECT * FROM record_items"), [])


class PublishRecordTests(RecordTestCase):
    def publish(self, turtle, record_id, client=None):
        nanopub = MagicMock()
        nanopub.publish.return_value = ("https://w3id.org/np/RAtest", "https://registry.example.org")
        body = {"ttl": turtle, "creator_orcid_id": "0000-0002-1825-0097"}
        if record_id:
            body["record_id"] = record_id
        if client:
            body["client"] = client
        with (
            patch.object(nanopub_router, "get_nanopub_profile", return_value=MagicMock()),
            patch.object(nanopub_router, "get_nanopub_agent_uri", return_value=None),
            patch.object(nanopub_router, "NanopubConf", return_value=MagicMock()),
            patch.object(nanopub_router, "Nanopub", return_value=nanopub),
            patch.object(nanopub_router, "add_nanopub_metadata"),
        ):
            response = self.client.post("/api/nanopub/publish", json=body)
        return response, nanopub

    def test_publishing_needs_the_version_marked_best(self):
        self.login()
        record_id = self.start()

        for turtle, target in ((DRAFT, record_id), (DRAFT, None), (EDITED, record_id)):
            with self.subTest(target=target, edited=turtle is EDITED):
                response, nanopub = self.publish(turtle, target)
                self.assertEqual(response.status_code, 409)
                nanopub.publish.assert_not_called()
        self.assertEqual(self.summary(record_id)["publications"], [])

    def test_publishing_needs_an_account(self):
        self.store.enabled = False
        response, nanopub = self.publish(DRAFT, None)
        self.assertEqual(response.status_code, 409)
        nanopub.publish.assert_not_called()

    def test_a_marked_version_is_published_and_recorded_with_who_and_where(self):
        self.login()
        record_id = self.start()
        self.mark(record_id, 0)

        response, nanopub = self.publish(DRAFT, record_id, client=CLIENT)
        self.assertEqual(response.status_code, 200, response.text)
        nanopub.publish.assert_called_once()

        [publication] = self.summary(record_id)["publications"]
        self.assertEqual(
            (publication["version"], publication["nanopub_uri"], publication["orcid"]),
            (0, "https://w3id.org/np/RAtest", ORCID),
        )
        # The publication happened in the same browser as the import: one context, with device and screen.
        self.assertEqual(len(self.rows("SELECT * FROM client_contexts")), 1)
        [audit] = [event for event in self.audit("nanopub.publish") if event["status_code"] == 200]
        stored = json.loads(audit["request_payload"])
        self.assertNotIn("ttl", stored)
        self.assertEqual(stored["record_id"], record_id)

    def test_preparing_for_publication_keeps_the_mark_and_becomes_a_version(self):
        self.login()
        record_id = self.start()
        self.mark(record_id, 0)

        # Minted IRI, metadata and creator are added without a recorded step: still the marked version.
        response, _ = self.publish(PREPARED, record_id)
        self.assertEqual(response.status_code, 200, response.text)
        summary = self.summary(record_id)
        self.assertEqual([(v["number"], v["kind"], v["parent"]) for v in summary["versions"]],
                         [(0, "imported", None), (1, "metadata", 0)])
        self.assertEqual(summary["publications"][0]["version"], 1)

    def test_metadata_after_the_mark_keeps_it_and_other_edits_do_not(self):
        self.login()
        record_id = self.start()
        self.mark(record_id, 0)
        metadata = self.step(record_id, type="metadata", version=0, turtle=WITH_METADATA)
        self.assertEqual(metadata.json()["version"], 1)

        response, _ = self.publish(WITH_METADATA, record_id)
        self.assertEqual(response.status_code, 200, response.text)

        edited = WITH_METADATA.replace('rdfs:label "water"', 'rdfs:label "sea water"')
        self.assertEqual(self.step(record_id, type="edit", version=1, turtle=edited).json()["version"], 2)
        response, nanopub = self.publish(edited, record_id)
        self.assertEqual(response.status_code, 409)
        nanopub.publish.assert_not_called()

    def test_only_the_latest_best_mark_counts(self):
        self.login()
        record_id = self.start()
        self.mark(record_id, 0)
        self.step(record_id, type="edit", version=0, turtle=EDITED)
        self.mark(record_id, 1)

        response, nanopub = self.publish(DRAFT, record_id)
        self.assertEqual(response.status_code, 409)
        nanopub.publish.assert_not_called()
        response, _ = self.publish(EDITED, record_id)
        self.assertEqual(response.status_code, 200, response.text)

    def test_graphs_too_large_to_check_are_refused_before_publishing(self):
        self.login()
        record_id = self.start()
        self.mark(record_id, 0)
        too_many = DRAFT + "".join(f"_:n{i} <http://e.org/p> \"{i}\" .\n" for i in range(MAX_BLANK_NODES + 1))
        response, nanopub = self.publish(too_many, record_id)
        self.assertEqual(response.status_code, 400)
        nanopub.publish.assert_not_called()


class AuditTrimTests(RecordTestCase):
    def test_frontend_events_keep_no_turtle(self):
        self.login()
        response = self.client.post(
            "/api/events",
            json={"action": "export", "payload": {"ttl": DRAFT, "format": "ttl", "filename": "variable.ttl"}},
        )
        self.assertEqual(response.status_code, 200)
        [event] = self.audit("frontend.export")
        self.assertEqual(json.loads(event["request_payload"]), {"format": "ttl", "filename": "variable.ttl"})


if __name__ == "__main__":
    unittest.main()
