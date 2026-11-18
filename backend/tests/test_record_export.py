"""The admin CSV export of every decomposition record (D-040).

Runs against a temporary auth database, like ``test_records``. Records are made through the
record routes and the record store; nothing reaches a model or the nanopublication registry.
"""

import csv
import io
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone

from rdflib.compare import isomorphic

from app.core import dependencies as core_deps
from app.record_export import TABLES
from app.records import parse_turtle
from app.services.call_log import ModelCall
from test_records import DRAFT, EDITED, MATRIX, ORCID, PIPELINE_RESULT, RecordTestCase

EXPORT = "/api/admin/records/export"
TABLE_FILES = [table.file_name for table in TABLES]
NANOPUB = "https://w3id.org/np/RAexampleExportedNanopub"
# A note with everything CSV has to quote, and a leading "=" a spreadsheet could take for a formula.
NOTE = '=SUM(1,2) "the matrix", not water\nsecond line'
STARTED = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
CALLS = [
    ModelCall("decomposition", 1, "openrouter", "qwen/qwen3-32b", "2026-11-18T10:00:00+00:00", 1538, 2805, 126, "ok", None),
    ModelCall("linking", 1, "openrouter", "qwen/qwen3-32b", "2026-11-18T10:00:02+00:00", 640, 669, 1, "unusable",
              "The text is not one of the offered labels."),
]


def read_csv(text):
    """Parse one exported file the way pandas or R would, keeping line breaks inside values."""
    return list(csv.DictReader(io.StringIO(text, newline="")))


class RecordExportTests(RecordTestCase):
    def export(self):
        response = self.client.get(EXPORT)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def files(self, response):
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}

    def make_records(self):
        """Carol: a decomposition with its model calls, and a failed one. Bob: imported, edited,
        checked, marked as best with a note, and published."""
        self.carol = self.store.create_user(
            username="carol", password="password-123", display_name="Carol Example", email="carol@example.org"
        )
        self.decomposed = core_deps.record_store.record_decomposition(
            user=self.carol, request=None, definition="dissolved oxygen in water", provider="openrouter",
            model="qwen/qwen3-32b", disable_thinking=True, prompt_setup="measured", prompt_sha256="abc123",
            started_at=STARTED, ended_at=STARTED + timedelta(seconds=3), result=PIPELINE_RESULT, error=None,
            calls=CALLS,
        )
        self.failed = core_deps.record_store.record_decomposition(
            user=self.carol, request=None, definition="air temperature", provider="psnc", model="Qwen3.8-27B",
            disable_thinking=True, prompt_setup="measured", prompt_sha256=None, started_at=STARTED,
            ended_at=STARTED + timedelta(seconds=1), result=None, error="HTTP 401",
        )
        self.login()
        self.imported = self.start()
        edited = self.step(self.imported, type="edit", version=0, turtle=EDITED, changes=[
            {"operation": "relabel", "role": "hasMatrix", "component": MATRIX, "before": "water", "after": "sea water"},
        ])
        self.assertEqual(edited.status_code, 201, edited.text)
        self.assertEqual(self.check(self.imported, 1).status_code, 201)
        response = self.step(self.imported, type="mark_best", version=1, unchecked_components=0, note=NOTE)
        self.assertEqual(response.status_code, 201, response.text)
        target = core_deps.record_store.publish_target(user=self.bob, record_id=self.imported, graph=parse_turtle(EDITED))
        core_deps.record_store.record_publish(
            user=self.bob, request=None, target=target, turtle=EDITED, nanopub_uri=NANOPUB, orcid=ORCID
        )
        self.client.post("/api/auth/logout")
        self.login("admin", "admin-password")

    def test_the_export_needs_an_admin(self):
        self.assertEqual(self.client.get(EXPORT).status_code, 401)
        self.login()
        self.assertEqual(self.client.get(EXPORT).status_code, 403)

    def test_an_empty_export_has_every_table_with_its_header(self):
        self.login("admin", "admin-password")
        files = self.files(self.export())
        self.assertEqual(sorted(files), sorted([*TABLE_FILES, "columns.csv", "README.txt"]))
        for table in TABLES:
            rows = list(csv.reader(io.StringIO(files[table.file_name], newline="")))
            self.assertEqual(rows, [[name for name, _ in table.columns]], table.file_name)

    def test_columns_csv_says_what_every_column_means(self):
        self.login("admin", "admin-password")
        rows = read_csv(self.files(self.export())["columns.csv"])
        self.assertEqual(
            [(row["file"], row["column"]) for row in rows],
            [(table.file_name, name) for table in TABLES for name, _ in table.columns],
        )
        self.assertTrue(all(row["meaning"].strip() for row in rows))

    def test_records_name_who_made_them_and_keep_the_run(self):
        self.make_records()
        records = {row["record_id"]: row for row in read_csv(self.files(self.export())["records.csv"])}
        self.assertEqual(list(records), [self.decomposed, self.failed, self.imported])

        decomposed = records[self.decomposed]
        self.assertEqual(
            {key: decomposed[key] for key in ("origin", "user_id", "username", "name", "email", "definition")},
            {"origin": "decompose", "user_id": str(self.carol["id"]), "username": "carol", "name": "Carol Example",
             "email": "carol@example.org", "definition": "dissolved oxygen in water"},
        )
        self.assertEqual(
            {key: decomposed[key] for key in ("provider", "model", "reasoning_off", "prompt_setup", "prompt_sha256",
                                              "run_status", "run_error", "schema_valid")},
            {"provider": "openrouter", "model": "qwen/qwen3-32b", "reasoning_off": "1", "prompt_setup": "measured",
             "prompt_sha256": "abc123", "run_status": "ok", "run_error": "", "schema_valid": "1"},
        )
        self.assertEqual(decomposed["run_started_at"], "2026-11-18T10:00:00+00:00")
        self.assertEqual(decomposed["raw_output"], PIPELINE_RESULT["raw_llm_output"])
        self.assertEqual(json.loads(decomposed["parsed_answer"]), {"label": "dissolved oxygen in water"})
        self.assertEqual(json.loads(decomposed["validation_errors"]), [])

        self.assertEqual((records[self.failed]["run_status"], records[self.failed]["run_error"]), ("error", "HTTP 401"))
        imported = records[self.imported]
        self.assertEqual((imported["origin"], imported["username"], imported["definition"], imported["provider"]),
                         ("import", "bob", "", ""))

    def test_steps_changes_and_versions_tell_the_story_of_a_record(self):
        self.make_records()
        files = self.files(self.export())
        steps = [row for row in read_csv(files["steps.csv"]) if row["record_id"] == self.imported]
        self.assertEqual([(row["step"], row["type"], row["version"]) for row in steps], [
            ("1", "import", "0"), ("2", "edit", "1"), ("3", "check", "1"), ("4", "mark_best", "1"), ("5", "publish", "1"),
        ])
        self.assertEqual((steps[3]["note"], steps[3]["unchecked_components"]), (NOTE, "0"))
        self.assertEqual((steps[4]["nanopub_uri"], steps[4]["orcid"]), (NANOPUB, ORCID))

        changes = [row for row in read_csv(files["changes.csv"]) if row["record_id"] == self.imported]
        self.assertEqual(
            [(row["step"], row["position"], row["operation"], row["role"], row["component"], row["before"], row["after"])
             for row in changes],
            [("2", "1", "relabel", "hasMatrix", MATRIX, "water", "sea water"), ("3", "1", "confirm", "hasMatrix", MATRIX, "", "")],
        )

        versions = [row for row in read_csv(files["versions.csv"]) if row["record_id"] == self.imported]
        self.assertEqual([(row["version"], row["kind"], row["parent_version"]) for row in versions],
                         [("0", "imported", ""), ("1", "edited", "0")])
        # Each version's whole graph comes back, line breaks and all, in one cell.
        self.assertTrue(isomorphic(parse_turtle(versions[0]["turtle"]), parse_turtle(DRAFT)))
        self.assertTrue(isomorphic(parse_turtle(versions[1]["turtle"]), parse_turtle(EDITED)))
        self.assertNotEqual(versions[0]["sha256"], versions[1]["sha256"])

        # A failed run keeps its draft step, with no version.
        failed_steps = [row for row in read_csv(files["steps.csv"]) if row["record_id"] == self.failed]
        self.assertEqual([(row["type"], row["version"]) for row in failed_steps], [("draft", "")])

    def test_the_tables_join_on_record_and_step(self):
        self.make_records()
        files = self.files(self.export())
        steps = {(row["record_id"], row["step"]) for row in read_csv(files["steps.csv"])}
        versions = {(row["record_id"], row["version"]) for row in read_csv(files["versions.csv"])}
        records = {row["record_id"] for row in read_csv(files["records.csv"])}
        self.assertTrue({(row["record_id"], row["step"]) for row in read_csv(files["changes.csv"])} <= steps)
        self.assertTrue({(rid, version) for rid, version in
                         ((row["record_id"], row["version"]) for row in read_csv(files["steps.csv"])) if version} <= versions)
        self.assertTrue({(row["record_id"], row["parent_version"]) for row in read_csv(files["versions.csv"])
                         if row["parent_version"]} <= versions)
        for name in ("steps.csv", "changes.csv", "versions.csv", "model_calls.csv"):
            self.assertTrue({row["record_id"] for row in read_csv(files[name])} <= records, name)

    def test_model_calls_keep_their_time_tokens_and_outcome(self):
        self.make_records()
        calls = read_csv(self.files(self.export())["model_calls.csv"])
        self.assertEqual(
            [(row["record_id"], row["purpose"], row["attempt"], row["latency_ms"], row["input_tokens"],
              row["output_tokens"], row["status"], row["error"]) for row in calls],
            [(self.decomposed, "decomposition", "1", "1538", "2805", "126", "ok", ""),
             (self.decomposed, "linking", "1", "640", "669", "1", "unusable", "The text is not one of the offered labels.")],
        )
        self.assertEqual({(row["provider"], row["model"]) for row in calls}, {("openrouter", "qwen/qwen3-32b")})

    def test_typed_text_is_exported_exactly_as_stored(self):
        self.make_records()
        steps = read_csv(self.files(self.export())["steps.csv"])
        # Nothing is escaped or trimmed, not even a leading "=": the README says how to open it safely.
        self.assertIn(NOTE, [row["note"] for row in steps])

    def test_a_version_never_carries_the_text_of_another_record(self):
        # The same graph is stored once, with the text it first arrived in: Alice's, comment and all.
        self.login("alice")
        self.start("# alice's private comment\n" + DRAFT)
        self.client.post("/api/auth/logout")
        self.login()
        bobs = self.start(DRAFT)
        self.client.post("/api/auth/logout")
        self.login("admin", "admin-password")
        versions = read_csv(self.files(self.export())["versions.csv"])
        self.assertEqual(len({row["sha256"] for row in versions}), 1)
        bobs_version = next(row for row in versions if row["record_id"] == bobs)
        self.assertTrue(isomorphic(parse_turtle(bobs_version["turtle"]), parse_turtle(DRAFT)))
        for row in versions:
            self.assertNotIn("private comment", row["turtle"])

    def test_the_snapshot_reads_every_table_at_one_moment(self):
        with core_deps.record_store.snapshot() as conn:
            before = conn.execute("SELECT COUNT(*) FROM record_items").fetchone()[0]
            # Another connection stores a record while the snapshot is open.
            core_deps.record_store.record_decomposition(
                user=self.bob, request=None, definition="air temperature", provider="psnc", model="Qwen3.8-27B",
                disable_thinking=True, prompt_setup="measured", prompt_sha256=None, started_at=STARTED,
                ended_at=STARTED, result=None, error="HTTP 401",
            )
            during = conn.execute("SELECT COUNT(*) FROM record_items").fetchone()[0]
        self.assertEqual(during, before)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM record_items"), [{"n": before + 1}])

    def test_the_export_is_refused_while_authentication_is_disabled(self):
        self.make_records()
        # Without authentication every visitor counts as an admin, so the records must stay closed.
        self.store.enabled = False
        response = self.client.get(EXPORT)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("carol@example.org", response.text)

    def test_where_people_worked_is_never_exported(self):
        self.make_records()
        stored = self.rows("SELECT ip_address, user_agent, device, screen FROM client_contexts WHERE user_agent != ''")
        self.assertTrue(stored, "the record routes stored where bob worked")
        text = "\n".join(self.files(self.export()).values())
        for context in stored:
            for value in context.values():
                if value:
                    self.assertNotIn(value, text)
        self.assertNotIn("RecordTest/1.0", text)
        self.assertNotIn("macOS", text)

    def test_the_download_is_named_audited_and_never_cached(self):
        self.make_records()
        response = self.export()
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertRegex(response.headers["content-disposition"], r'^attachment; filename="iadopt-records-\d{8}T\d{6}Z\.zip"$')
        self.assertEqual(response.headers["cache-control"], "no-store")

        audit = self.audit("admin.records_exported")
        self.assertEqual(len(audit), 1)
        self.assertEqual(json.loads(audit[0]["response_payload"]), {"rows": {
            "records.csv": 3, "steps.csv": 7, "changes.csv": 2, "versions.csv": 3, "model_calls.csv": 2,
        }})
        self.assertNotIn("carol@example.org", json.dumps(audit[0]))

    def test_the_readme_warns_about_personal_data_and_spreadsheets(self):
        self.make_records()
        readme = self.files(self.export())["README.txt"]
        self.assertIn("name, username and email", readme)
        self.assertIn("Data > From Text/CSV", readme)
        self.assertRegex(readme, r"records\.csv\s+3 rows")
        self.assertRegex(readme, r"changes\.csv\s+2 rows")

    def test_the_export_reads_through_a_snapshot_that_cannot_write(self):
        with core_deps.record_store.snapshot() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO record_schema (version) VALUES (99)")
        self.assertEqual(self.rows("SELECT version FROM record_schema"), [{"version": 1}])

    def test_the_file_name_follows_the_export_time(self):
        self.login("admin", "admin-password")
        before = datetime.now(timezone.utc).replace(microsecond=0)
        disposition = self.export().headers["content-disposition"]
        after = datetime.now(timezone.utc)
        stamp = re.search(r"iadopt-records-(\d{8}T\d{6})Z\.zip", disposition).group(1)
        exported = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        self.assertTrue(before <= exported <= after)
