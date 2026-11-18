"""The workspace's recorded steps, replayed against the real record routes (D-037, stage 3).

``fixtures/workspace_record_flow.json`` holds the requests the workspace sent in the browser test
``frontend/test/app.record.browser.test.tsx``: a pasted Turtle, an inspector edit, "Looks right",
its Undo and "Looks right" again, nanopublication metadata, "Mark as best" with a note, the
creator written at publication and the publication; and a decomposition followed by a Turtle-tab
edit, metadata, the best mark, the creator and the publication. Sent here unchanged, apart from
the record ids, every step must be accepted and stored with who and where, the versions the
backend answers must be the ones the workspace named next, and the marked version must be
published. The pipeline and the nanopublication registry are mocked: nothing reaches a model or
publishes.
"""

import json
import pathlib
import unittest
from unittest.mock import MagicMock, patch

from app.routers import decompose as decompose_router
from app.routers import nanopub as nanopub_router
from app.services.llm import stream_event
from test_records import ORCID, RecordTestCase

FLOW = json.loads((pathlib.Path(__file__).parent / "fixtures" / "workspace_record_flow.json").read_text())
NANOPUB = "https://w3id.org/np/RAworkspace"


class WorkspaceRecordFlowTests(RecordTestCase):
    def publish(self, body):
        """Publish with the registry, signing and metadata mocked; return the response and the mock."""
        nanopub = MagicMock()
        nanopub.publish.return_value = (NANOPUB, "https://registry.example.org")
        with (
            patch.object(nanopub_router, "get_nanopub_profile", return_value=MagicMock()),
            patch.object(nanopub_router, "get_nanopub_agent_uri", return_value=None),
            patch.object(nanopub_router, "NanopubConf", return_value=MagicMock()),
            patch.object(nanopub_router, "Nanopub", return_value=nanopub),
            patch.object(nanopub_router, "add_nanopub_metadata"),
        ):
            return self.client.post("/api/nanopub/publish", json=body), nanopub

    def replay(self, flow):
        """Send the captured requests in order, with the fake's record id replaced by the real one.

        Returns:
            The real record id, the versions the step responses named, and the publish mocks.
        """
        record_id = None
        versions = []
        published = []
        for call in flow["calls"]:
            body = dict(call["body"])
            path = call["path"]
            with self.subTest(path=path, type=body.get("type")):
                if path == "/api/decompose/stream":
                    final = {
                        "raw_llm_output": "{}",
                        "parsed_json": {"definition": body["definition"]},
                        "schema_valid": True,
                        "validation_errors": [],
                        "enriched_json": {},
                        "ttl": flow["draft"],
                    }
                    lines = [stream_event("raw_delta", delta="{"), stream_event("final", data=final)]
                    with patch.object(decompose_router, "stream_pipeline_events", return_value=iter(lines)):
                        response = self.client.post(path, json=body)
                    self.assertEqual(response.status_code, 200, response.text)
                    final_event = json.loads(response.text.splitlines()[-1])
                    record_id = final_event["data"]["record_id"]
                elif path == "/api/records":
                    response = self.client.post(path, json=body)
                    self.assertEqual(response.status_code, 201, response.text)
                    record_id = response.json()["record_id"]
                elif path == f"/api/records/{flow['record_id']}/events":
                    response = self.client.post(f"/api/records/{record_id}/events", json=body)
                    self.assertEqual(response.status_code, 201, response.text)
                    versions.append((body["type"], body["version"], response.json()["version"]))
                elif path == "/api/nanopub/publish":
                    self.assertEqual(body["record_id"], flow["record_id"])
                    body["record_id"] = record_id
                    response, nanopub = self.publish(body)
                    self.assertEqual(response.status_code, 200, response.text)
                    published.append(nanopub)
                else:
                    self.fail(f"The fixture holds an unexpected request: {path}")
        self.assertRegex(record_id, r"^[0-9a-f]{16}$")
        return record_id, versions, published

    def events(self, record_id):
        return self.rows(
            """
            SELECT e.type, v.number AS version, e.note, e.unchecked_components, e.nanopub_uri, e.orcid,
                   e.context_id, e.occurred_at
              FROM record_events e
              JOIN record_items i ON i.id = e.item_id
              LEFT JOIN record_versions v ON v.id = e.version_id
             WHERE i.public_id = ?
             ORDER BY e.seq
            """,
            record_id,
        )

    def test_pasted_turtle_edits_checks_mark_and_publication_are_all_recorded(self):
        self.login()
        record_id, versions, published = self.replay(FLOW["paste"])

        # Each step's answer is the version the workspace named in its next step.
        self.assertEqual(versions, [
            ("edit", 0, 1),
            ("check", 1, 1),
            ("uncheck", 1, 1),
            ("check", 1, 1),
            ("metadata", 1, 2),
            ("mark_best", 2, 2),
            ("metadata", 2, 3),
        ])
        [nanopub] = published
        nanopub.publish.assert_called_once()

        summary = self.summary(record_id)
        self.assertEqual(summary["origin"], "import")
        self.assertEqual([(v["number"], v["kind"], v["parent"]) for v in summary["versions"]],
                         [(0, "imported", None), (1, "edited", 0), (2, "metadata", 1), (3, "metadata", 2)])
        self.assertEqual(summary["best_version"], 2)
        self.assertEqual([(p["version"], p["nanopub_uri"], p["orcid"]) for p in summary["publications"]],
                         [(3, NANOPUB, ORCID)])

        events = self.events(record_id)
        self.assertEqual([(event["type"], event["version"]) for event in events], [
            ("import", 0), ("edit", 1), ("check", 1), ("uncheck", 1), ("check", 1),
            ("metadata", 2), ("mark_best", 2), ("metadata", 3), ("publish", 3),
        ])
        [mark] = [event for event in events if event["type"] == "mark_best"]
        self.assertEqual((mark["note"], mark["unchecked_components"]), ("The model used plain water.", 4))
        # Who and where: every step has a time and the one browser it came from, with its device and screen.
        self.assertTrue(all(event["context_id"] and event["occurred_at"] for event in events))
        [context] = self.rows("SELECT ip_address, user_agent, device, screen FROM client_contexts")
        client = FLOW["paste"]["calls"][0]["body"]["client"]
        self.assertEqual(json.loads(context["screen"]), client["screen"])
        self.assertEqual(json.loads(context["device"]), client["device"])
        self.assertEqual(context["user_agent"], "RecordTest/1.0")
        [item] = self.rows("SELECT user_id FROM record_items")
        self.assertEqual(item["user_id"], self.bob["id"])

        changes = self.rows(
            """
            SELECT e.type, c.operation, c.role, c.component, c.before_value, c.after_value
              FROM record_changes c JOIN record_events e ON e.id = c.event_id
             ORDER BY e.seq, c.position
            """
        )
        matrix = "https://www.wikidata.org/entity/Q283"
        self.assertEqual([tuple(row.values()) for row in changes[:4]], [
            ("edit", "relabel", "hasMatrix", matrix, "water", "sea water"),
            ("check", "confirm", "hasMatrix", matrix, None, None),
            ("uncheck", "unconfirm", "hasMatrix", matrix, None, None),
            ("check", "confirm", "hasMatrix", matrix, None, None),
        ])
        [relink] = changes[4:]
        self.assertEqual((relink["type"], relink["operation"], relink["role"], relink["before_value"]),
                         ("metadata", "relink", "variable", "https://example.org/variable/1"))

        [audit] = [event for event in self.audit("nanopub.publish") if event["status_code"] == 200]
        stored = json.loads(audit["request_payload"])
        self.assertNotIn("ttl", stored)
        self.assertEqual(stored["record_id"], record_id)

    def test_a_decomposition_keeps_its_record_through_edits_the_mark_and_publication(self):
        self.login()
        record_id, versions, published = self.replay(FLOW["decompose"])

        self.assertEqual(versions, [
            ("edit", 0, 1),
            ("metadata", 1, 2),
            ("mark_best", 2, 2),
            ("metadata", 2, 3),
            ("mark_best", 3, 3),
        ])
        # The browser test's fake refused the first publication on purpose; the backend accepts it,
        # because it is the marked version with only the creator added. The second one republishes it.
        self.assertEqual(len(published), 2)

        summary = self.summary(record_id)
        self.assertEqual(summary["origin"], "decompose")
        self.assertEqual([(v["number"], v["kind"]) for v in summary["versions"]],
                         [(0, "model_draft"), (1, "edited"), (2, "metadata"), (3, "metadata")])
        self.assertEqual(summary["best_version"], 3)
        self.assertEqual([p["version"] for p in summary["publications"]], [3, 3])
        self.assertEqual([(event["type"], event["version"]) for event in self.events(record_id)], [
            ("draft", 0), ("edit", 1), ("metadata", 2), ("mark_best", 2), ("metadata", 3),
            ("publish", 3), ("mark_best", 3), ("publish", 3),
        ])
        [run] = self.rows("SELECT status, provider, model FROM record_runs")
        self.assertEqual(run["status"], "ok")
        [definition] = self.rows("SELECT definition FROM record_items")
        self.assertEqual(definition["definition"], "Dissolved oxygen in water")
        self.assertEqual(len(self.rows("SELECT * FROM client_contexts")), 1)

    def test_an_edit_after_the_mark_keeps_the_marked_version_from_being_replaced_silently(self):
        """The workspace stops offering Publish after an edit; the backend refuses it too."""
        self.login()
        flow = FLOW["paste"]
        record_id, _, _ = self.replay({**flow, "calls": flow["calls"][:7]})  # up to and with the best mark
        marked = flow["calls"][5]["body"]["turtle"]
        edited = marked.replace('"sea water"', '"ocean water"')
        self.assertNotEqual(edited, marked)
        response = self.step(record_id, type="edit", version=2, turtle=edited,
                             changes=[{"operation": "relabel", "role": "hasMatrix", "component": "https://www.wikidata.org/entity/Q283",
                                       "before": "sea water", "after": "ocean water"}])
        self.assertEqual(response.json()["version"], 3)
        refused, nanopub = self.publish({"ttl": edited, "creator_orcid_id": ORCID, "record_id": record_id})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("not the version you marked as best", refused.json()["detail"])
        nanopub.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
