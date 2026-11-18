"""The admin export of every decomposition record (D-040, reshaped by D-041).

Runs against a temporary auth database, like ``test_records``. Records are made through the
record routes and the record store; nothing reaches a model or the nanopublication registry.
"""

import asyncio
import csv
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from rdflib import RDF, Graph, Literal, Namespace, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import PROV

from app.core import dependencies as core_deps
from app.record_export import (
    EDITS_COLUMNS,
    RECORD_BASE,
    STEP_WORDS,
    SUMMARY_COLUMNS,
    iter_file,
    plain_time,
    short_reason,
    words,
    write_export,
)
from app.records import parse_turtle
from app.routers.admin import ExportResponse
from app.services.call_log import ModelCall
from test_records import DRAFT, EDITED, MATRIX, ORCID, PIPELINE_RESULT, RecordTestCase

EXPORT = "/api/admin/records/export"
FILES = ["summary.csv", "edits.csv", "records.jsonld", "README.txt"]
NANOPUB = "https://w3id.org/np/RAexampleExportedNanopub"
NANOPUB_2 = "https://w3id.org/np/RAexampleSecondNanopub"
# EDITED with publication metadata only: the same description, so a metadata version.
EDITED_WITH_METADATA = EDITED + (
    "<https://example.org/variable/1> <http://purl.org/dc/terms/conformsTo> <https://w3id.org/iadopt/ont/1.1> .\n"
)
PUBLISHED = EDITED + (
    "<https://example.org/variable/1> <http://purl.org/dc/terms/identifier> <https://example.org/variable/1> .\n"
)
# A note with everything CSV has to quote, and a leading "=" a spreadsheet could take for a formula.
NOTE = '=SUM(1,2) "the matrix", not water\nsecond line'
STARTED = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
CALLS = [
    ModelCall("decomposition", 1, "openrouter", "qwen/qwen3-32b", "2026-11-18T10:00:00+00:00", 1538, 2805, 126, "ok", None),
    ModelCall("linking", 1, "openrouter", "qwen/qwen3-32b", "2026-11-18T10:00:02+00:00", 640, 669, 1, "unusable",
              "The text is not one of the offered labels."),
]
PLAIN_TIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
REC = Namespace("https://w3id.org/iadopt/record#")
OA = Namespace("http://www.w3.org/ns/oa#")
IOP = Namespace("https://w3id.org/iadopt/ont/")
SCHEMA = Namespace("https://schema.org/")


def read_csv(text):
    """Parse one exported CSV file the way pandas or R would, keeping line breaks inside values."""
    return list(csv.DictReader(io.StringIO(text, newline="")))


class RecordExportTests(RecordTestCase):
    def export(self):
        response = self.client.get(EXPORT)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def files(self, response=None):
        response = response or self.export()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}

    def records(self, files):
        return {node["@id"].removeprefix(RECORD_BASE): node for node in json.loads(files["records.jsonld"])["@graph"]}

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

    # -------------------------------------------------------------- access

    def test_the_export_needs_an_admin(self):
        self.assertEqual(self.client.get(EXPORT).status_code, 401)
        self.login()
        self.assertEqual(self.client.get(EXPORT).status_code, 403)

    def test_the_export_is_refused_while_authentication_is_disabled(self):
        self.make_records()
        # Without authentication every visitor counts as an admin, so the records must stay closed.
        self.store.enabled = False
        response = self.client.get(EXPORT)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("carol@example.org", response.text)

    # -------------------------------------------------------------- files

    def test_an_empty_export_has_its_three_files_and_a_readme(self):
        self.login("admin", "admin-password")
        files = self.files()
        self.assertEqual(sorted(files), sorted(FILES))
        self.assertEqual(list(csv.reader(io.StringIO(files["summary.csv"], newline=""))), [list(SUMMARY_COLUMNS)])
        self.assertEqual(list(csv.reader(io.StringIO(files["edits.csv"], newline=""))), [list(EDITS_COLUMNS)])
        document = json.loads(files["records.jsonld"])
        self.assertEqual(document["@graph"], [])
        self.assertIn("@context", document)

    def test_the_summary_has_one_plain_row_per_record(self):
        self.make_records()
        rows = {row["record"]: row for row in read_csv(self.files()["summary.csv"])}
        self.assertEqual(list(rows), [self.decomposed, self.failed, self.imported])
        for row in rows.values():
            self.assertRegex(row["started"], PLAIN_TIME)

        def picked(row, *keys):
            return {key: row[key] for key in keys}

        self.assertEqual(picked(rows[self.decomposed], "name", "username", "email", "definition", "started from", "model",
                                "result", "model calls", "tokens in", "tokens out", "versions", "edits", "checks",
                                "best version", "published as"), {
            "name": "Carol Example", "username": "carol", "email": "carol@example.org",
            "definition": "dissolved oxygen in water", "started from": "decomposition",
            "model": "qwen/qwen3-32b (openrouter)", "result": "draft made", "model calls": "2",
            "tokens in": "3474", "tokens out": "127", "versions": "1", "edits": "0", "checks": "0",
            "best version": "", "published as": "",
        })
        self.assertEqual(picked(rows[self.failed], "model", "result", "model calls", "tokens in", "versions"), {
            "model": "Qwen3.8-27B (psnc)", "result": "failed: HTTP 401", "model calls": "0", "tokens in": "", "versions": "0",
        })
        imported = rows[self.imported]
        self.assertEqual(picked(imported, "username", "started from", "model", "result", "versions", "edits", "checks",
                                "unchecks", "best version", "best mark note", "published as"), {
            "username": "bob", "started from": "opened Turtle", "model": "", "result": "", "versions": "2",
            "edits": "1", "checks": "1", "unchecks": "0", "best version": "v1", "best mark note": NOTE,
            "published as": NANOPUB,
        })
        self.assertRegex(imported["last published"], PLAIN_TIME)

    def test_edits_list_each_change_in_plain_words(self):
        self.make_records()
        rows = read_csv(self.files()["edits.csv"])
        self.assertEqual(
            [(row["record"], row["who"], row["step"], row["step kind"], row["component role"], row["change"],
              row["component"], row["before"], row["after"], row["version after"]) for row in rows],
            [
                (self.imported, "bob", "2", "applied change", "matrix", "relabelled", MATRIX, "water", "sea water", "v1"),
                (self.imported, "bob", "3", "looks right", "matrix", "marked as looking right", MATRIX, "", "", "v1"),
            ],
        )
        self.assertTrue(all(PLAIN_TIME.match(row["when"]) for row in rows))

    def test_records_jsonld_tells_each_record_in_the_order_things_happened(self):
        self.make_records()
        records = self.records(self.files())
        iri = RECORD_BASE + self.imported
        v0, v1 = f"{iri}/v0", f"{iri}/v1"
        imported = records[self.imported]
        self.assertEqual(imported["startedFrom"], "import")
        self.assertEqual(imported["creator"]["username"], "bob")
        self.assertNotIn("email", imported["creator"])  # Bob's account has none.
        self.assertEqual([(v["number"], v["kind"], v.get("wasRevisionOf")) for v in imported["versions"]],
                         [(0, "imported", None), (1, "edited", v0)])
        self.assertTrue(isomorphic(parse_turtle(imported["versions"][0]["turtle"]), parse_turtle(DRAFT)))
        self.assertTrue(isomorphic(parse_turtle(imported["versions"][1]["turtle"]), parse_turtle(EDITED)))

        steps = imported["steps"]
        self.assertEqual([(s["kind"], s.get("used"), s.get("generated")) for s in steps], [
            ("import", None, v0), ("edit", v0, v1), ("check", v1, None), ("mark_best", v1, None), ("publish", v1, None),
        ])
        self.assertEqual(steps[1]["changes"], [{"@type": "Change", "operation": "relabel", "role": "iop:hasMatrix",
                                                "component": MATRIX, "before": "water", "after": "sea water"}])
        self.assertEqual(steps[2]["changes"], [{"@type": "Change", "operation": "confirm", "role": "iop:hasMatrix",
                                                "component": MATRIX}])
        mark = steps[3]["annotation"]
        self.assertEqual({key: mark[key] for key in ("motivatedBy", "hasTarget", "bodyValue", "uncheckedComponents")},
                         {"motivatedBy": "oa:assessing", "hasTarget": v1, "bodyValue": NOTE, "uncheckedComponents": 0})
        self.assertEqual((steps[4]["nanopublication"], steps[4]["orcid"]), (NANOPUB, ORCID))
        self.assertEqual((imported["bestVersion"], imported["publishedAs"]), (v1, [NANOPUB]))

        decomposed = records[self.decomposed]
        self.assertEqual({key: decomposed["creator"][key] for key in ("name", "username", "email")},
                         {"name": "Carol Example", "username": "carol", "email": "carol@example.org"})
        run = decomposed["modelRun"]
        self.assertEqual({key: run[key] for key in ("provider", "model", "reasoningOff", "promptSetup", "status", "schemaValid")},
                         {"provider": "openrouter", "model": "qwen/qwen3-32b", "reasoningOff": True,
                          "promptSetup": "measured", "status": "ok", "schemaValid": True})
        self.assertEqual(run["parsedAnswer"], {"label": "dissolved oxygen in water"})
        self.assertEqual(run["rawOutput"], PIPELINE_RESULT["raw_llm_output"])
        self.assertEqual([(c["purpose"], c["latencyMs"], c.get("inputTokens"), c.get("outputTokens"), c["status"])
                          for c in run["calls"]],
                         [("decomposition", 1538, 2805, 126, "ok"), ("linking", 640, 669, 1, "unusable")])
        self.assertEqual([(s["kind"], s.get("generated")) for s in decomposed["steps"]],
                         [("draft", f"{RECORD_BASE}{self.decomposed}/v0")])

        failed = records[self.failed]
        self.assertEqual((failed["modelRun"]["status"], failed["modelRun"]["error"]), ("error", "HTTP 401"))
        self.assertNotIn("versions", failed)
        self.assertEqual([s["kind"] for s in failed["steps"]], ["draft"])

    def test_records_jsonld_is_linked_data_any_rdf_tool_reads(self):
        self.make_records()
        graph = Graph().parse(data=self.files()["records.jsonld"], format="json-ld")
        record = URIRef(RECORD_BASE + self.imported)
        v0, v1 = URIRef(f"{record}/v0"), URIRef(f"{record}/v1")
        self.assertIn((record, RDF.type, REC.Record), graph)
        self.assertIn((v1, PROV.wasRevisionOf, v0), graph)
        marks = list(graph.subjects(OA.motivatedBy, OA.assessing))
        self.assertEqual(len(marks), 1)
        self.assertIn((marks[0], OA.hasTarget, v1), graph)
        self.assertIn((marks[0], OA.bodyValue, Literal(NOTE)), graph)
        self.assertTrue(list(graph.subjects(REC.role, IOP.hasMatrix)))
        carol = URIRef(f"{RECORD_BASE}account/{self.carol['id']}")
        self.assertIn((carol, SCHEMA.email, Literal("carol@example.org")), graph)
        answers = list(graph.objects(None, REC.parsedAnswer))
        self.assertEqual([answer.datatype for answer in answers], [RDF.JSON])
        self.assertEqual(json.loads(str(answers[0])), {"label": "dissolved oxygen in water"})

    def test_each_record_sits_on_its_own_line(self):
        self.make_records()
        lines = self.files()["records.jsonld"].splitlines()
        self.assertTrue(lines[0].startswith('{"@context": '))
        self.assertEqual((lines[1], lines[-1]), ('"@graph": [', "]}"))
        nodes = [json.loads(line.removesuffix(",")) for line in lines[2:-1]]
        self.assertEqual([node["@id"].removeprefix(RECORD_BASE) for node in nodes],
                         [self.decomposed, self.failed, self.imported])

    def test_a_version_never_carries_the_text_of_another_record(self):
        # The same graph is stored once, with the text it first arrived in: Alice's, with her
        # comment and her own prefix name.
        alices_text = "# alice's private comment\n" + DRAFT.replace(
            "@prefix iop: <https://w3id.org/iadopt/ont/> .", "@prefix alicesprivateproject: <https://w3id.org/iadopt/ont/> ."
        ).replace("iop:", "alicesprivateproject:")
        self.assertTrue(isomorphic(parse_turtle(alices_text), parse_turtle(DRAFT)))
        self.login("alice")
        self.start(alices_text)
        self.client.post("/api/auth/logout")
        self.login()
        bobs = self.start(DRAFT)
        self.client.post("/api/auth/logout")
        self.login("admin", "admin-password")
        files = self.files()
        everything = "\n".join(files.values())
        self.assertNotIn("private comment", everything)
        self.assertNotIn("alicesprivateproject", everything)
        bobs_version = self.records(files)[bobs]["versions"][0]
        self.assertIn("iop:Variable", bobs_version["turtle"])
        self.assertTrue(isomorphic(parse_turtle(bobs_version["turtle"]), parse_turtle(DRAFT)))

    def test_every_kind_of_step_says_which_version_it_used_or_made(self):
        self.login()
        rid = self.start()                                                                  # 1 import: v0
        self.assertEqual(self.step(rid, type="edit", version=0, turtle=EDITED).status_code, 201)       # 2 v0 -> v1
        self.assertEqual(self.step(rid, type="edit", version=1, turtle=EDITED).status_code, 201)       # 3 no change
        self.assertEqual(self.check(rid, 1, operation="unconfirm", step_type="uncheck").status_code, 201)  # 4
        metadata = self.step(rid, type="metadata", version=1, turtle=EDITED_WITH_METADATA)          # 5 v1 -> v2
        self.assertEqual(metadata.status_code, 201, metadata.text)
        self.mark(rid, 2)                                                                   # 6 mark v2
        for uri in (NANOPUB, NANOPUB_2):                                                    # 7 v2 -> v3, 8 v3 again
            target = core_deps.record_store.publish_target(user=self.bob, record_id=rid, graph=parse_turtle(PUBLISHED))
            core_deps.record_store.record_publish(
                user=self.bob, request=None, target=target, turtle=PUBLISHED, nanopub_uri=uri, orcid=ORCID
            )
        self.client.post("/api/auth/logout")
        self.login("admin", "admin-password")
        files = self.files()
        node = self.records(files)[rid]

        def v(number):
            return f"{RECORD_BASE}{rid}/v{number}"

        self.assertEqual([(s["kind"], s.get("used"), s.get("generated")) for s in node["steps"]], [
            ("import", None, v(0)), ("edit", v(0), v(1)), ("edit", v(1), None), ("uncheck", v(1), None),
            ("metadata", v(1), v(2)), ("mark_best", v(2), None), ("publish", v(2), v(3)), ("publish", v(3), None),
        ])
        self.assertEqual([(x["number"], x["kind"], x.get("wasRevisionOf")) for x in node["versions"]],
                         [(0, "imported", None), (1, "edited", v(0)), (2, "metadata", v(1)), (3, "metadata", v(2))])
        self.assertEqual((node["bestVersion"], node["publishedAs"]), (v(2), [NANOPUB, NANOPUB_2]))
        summary = {row["record"]: row for row in read_csv(files["summary.csv"])}[rid]
        self.assertEqual(
            {key: summary[key] for key in ("versions", "edits", "checks", "unchecks", "best version", "published as")},
            {"versions": "4", "edits": "2", "checks": "0", "unchecks": "1", "best version": "v2",
             "published as": f"{NANOPUB} {NANOPUB_2}"},
        )

    def test_a_failure_reason_never_quotes_the_model_in_the_summary(self):
        broken = {**PIPELINE_RESULT, "ttl": '<https://example.org/v> <https://example.org/p> "the model wrote this, unclosed'}
        rid = core_deps.record_store.record_decomposition(
            user=self.bob, request=None, definition="air temperature", provider="psnc", model="Qwen3.8-27B",
            disable_thinking=True, prompt_setup="measured", prompt_sha256=None, started_at=STARTED, ended_at=STARTED,
            result=broken, error=None,
        )
        self.login("admin", "admin-password")
        files = self.files()
        row = {r["record"]: r for r in read_csv(files["summary.csv"])}[rid]
        self.assertEqual(row["result"], "failed: The draft could not be stored as a version.")
        self.assertNotIn("the model wrote this", files["summary.csv"])
        self.assertIn("could not be stored as a version", self.records(files)[rid]["modelRun"]["error"])

    def test_a_model_answer_with_nan_still_gives_valid_json(self):
        odd = {**PIPELINE_RESULT, "parsed_json": {"label": "x", "value": float("nan")}}
        rid = core_deps.record_store.record_decomposition(
            user=self.bob, request=None, definition="air temperature", provider="psnc", model="Qwen3.8-27B",
            disable_thinking=True, prompt_setup="measured", prompt_sha256=None, started_at=STARTED, ended_at=STARTED,
            result=odd, error=None,
        )
        self.login("admin", "admin-password")

        def refuse(constant):
            raise ValueError(f"{constant} is not JSON")

        document = json.loads(self.files()["records.jsonld"], parse_constant=refuse)
        node = next(n for n in document["@graph"] if n["@id"] == RECORD_BASE + rid)
        self.assertEqual(node["modelRun"]["parsedAnswer"], {"label": "x", "value": "NaN"})

    def test_where_people_worked_is_never_exported(self):
        self.make_records()
        stored = self.rows("SELECT ip_address, user_agent, device, screen FROM client_contexts WHERE user_agent != ''")
        self.assertTrue(stored, "the record routes stored where bob worked")
        text = "\n".join(self.files().values())
        for context in stored:
            for value in context.values():
                if value:
                    self.assertNotIn(value, text)
        self.assertNotIn("RecordTest/1.0", text)
        self.assertNotIn("macOS", text)

    def test_typed_text_is_exported_exactly_as_stored(self):
        self.make_records()
        files = self.files()
        # Nothing is escaped or trimmed, not even a leading "=": the README says how to open it safely.
        self.assertIn(NOTE, [row["best mark note"] for row in read_csv(files["summary.csv"])])
        self.assertEqual(self.records(files)[self.imported]["steps"][3]["annotation"]["bodyValue"], NOTE)

    def test_the_download_is_named_audited_and_never_cached(self):
        self.make_records()
        response = self.export()
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertRegex(response.headers["content-disposition"], r'^attachment; filename="iadopt-records-\d{8}T\d{6}Z\.zip"$')
        self.assertEqual(response.headers["cache-control"], "no-store")
        audit = self.audit("admin.records_exported")
        self.assertEqual(len(audit), 1)
        self.assertEqual(json.loads(audit[0]["response_payload"]),
                         {"rows": {"summary.csv": 3, "edits.csv": 2, "records.jsonld": 3}})
        self.assertNotIn("carol@example.org", json.dumps(audit[0]))

    def test_the_readme_explains_the_files_and_warns_about_personal_data(self):
        self.make_records()
        readme = self.files()["README.txt"]
        self.assertIn("name, username and email", readme)
        self.assertIn("Data > From Text/CSV", readme)
        self.assertRegex(readme, r"summary\.csv\s+3 rows")
        self.assertRegex(readme, r"edits\.csv\s+2 rows")
        self.assertRegex(readme, r"records\.jsonld\s+3 records")

    def test_the_file_name_follows_the_export_time(self):
        self.login("admin", "admin-password")
        before = datetime.now(timezone.utc).replace(microsecond=0)
        disposition = self.export().headers["content-disposition"]
        after = datetime.now(timezone.utc)
        stamp = re.search(r"iadopt-records-(\d{8}T\d{6})Z\.zip", disposition).group(1)
        exported = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        self.assertTrue(before <= exported <= after)

    # -------------------------------------------------------------- reading and streaming

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

    def test_the_export_reads_through_a_snapshot_that_cannot_write(self):
        with core_deps.record_store.snapshot() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO record_schema (version) VALUES (99)")
        self.assertEqual(self.rows("SELECT version FROM record_schema"), [{"version": 1}])

    def test_many_records_are_written_to_a_file_and_sent_in_chunks(self):
        for number in range(120):
            core_deps.record_store.record_decomposition(
                user=self.bob, request=None, definition=f"variable {number}", provider="psnc", model="Qwen3.8-27B",
                disable_thinking=True, prompt_setup="measured", prompt_sha256=None, started_at=STARTED,
                ended_at=STARTED, result=PIPELINE_RESULT, error=None, calls=CALLS,
            )
        spool = tempfile.TemporaryFile()
        with core_deps.record_store.snapshot() as conn:
            counts = write_export(conn, spool, exported_at=STARTED)
        self.assertEqual(counts, {"summary.csv": 120, "edits.csv": 0, "records.jsonld": 120})
        chunks = list(iter_file(spool, chunk_size=4096))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(spool.closed)
        with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as archive:
            self.assertEqual(len(json.loads(archive.read("records.jsonld"))["@graph"]), 120)

    def test_the_route_sends_the_export_from_a_temporary_file_it_then_closes(self):
        self.make_records()
        opened = []
        make_file = tempfile.TemporaryFile

        def tracking(*args, **kwargs):
            handle = make_file(*args, **kwargs)
            opened.append(handle)
            return handle

        with patch("app.routers.admin.tempfile.TemporaryFile", side_effect=tracking):
            files = self.files()
        self.assertTrue(opened, "the export was written to a temporary file")
        self.assertTrue(all(handle.closed for handle in opened))
        self.assertEqual(len(self.records(files)), 3)

    def test_a_download_the_client_leaves_still_closes_the_file(self):
        spool = tempfile.TemporaryFile()
        spool.write(b"x" * 10_000)
        response = ExportResponse(spool, media_type="application/zip")

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("the client went away")

        scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET", "path": "/", "headers": []}

        async def download():
            with self.assertRaises(Exception):
                await response(scope, receive, send)
            # Checked while the event loop still runs: a server's loop never stops between
            # requests, so nothing but the response itself would close the file now.
            return spool.closed

        self.assertTrue(asyncio.run(download()))

    def test_short_reasons_keep_only_the_first_part(self):
        self.assertEqual(short_reason("HTTP 401"), "HTTP 401")
        self.assertEqual(
            short_reason('The draft could not be stored as a version. Could not parse the Turtle: at line 1 "quoted"'),
            "The draft could not be stored as a version.",
        )
        self.assertEqual(short_reason('Could not parse the Turtle: "quoted"'), "Could not parse the Turtle")
        self.assertEqual(
            short_reason("1 validation error for Answer\nlabel\n  Input should be a string [input_value='model text']"),
            "1 validation error for Answer",
        )
        self.assertEqual(short_reason(None), "")

    def test_plain_words_keep_codes_and_times_they_do_not_know(self):
        self.assertEqual(words(STEP_WORDS, "mark_best"), "marked as best")
        self.assertEqual(words(STEP_WORDS, "a_new_kind"), "a_new_kind")
        self.assertEqual(plain_time("2026-11-18T10:15:02.123456+00:00"), "2026-11-18 10:15:02")
        self.assertEqual(plain_time("not a time"), "not a time")
        self.assertEqual(plain_time(None), "")
