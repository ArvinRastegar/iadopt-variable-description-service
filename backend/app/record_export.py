"""Admin export of every decomposition record (D-041, which reshapes D-040's export).

One download, a ZIP, holds three files and a README:

* ``summary.csv``: one row per record, in plain words: who made it, the definition, how it
  started, the model and its result, model calls and tokens, versions, edits and checks, the
  best version with its note, and the publication.
* ``edits.csv``: one row per change inside a step, in plain words: when, who, the definition,
  the kind of step, the component's role, what changed, and the values before and after.
* ``records.jsonld``: every record in full, as one JSON-LD document. Each record sits on its
  own line inside ``@graph`` and reads in the order things happened: who made it, the
  definition, the model run with each call, every version with its Turtle, and every step
  with its changes; a best mark is a Web Annotation (``oa:assessing``). Each key maps to
  PROV-O, Web Annotation, Dublin Core, schema.org, I-ADOPT or the record vocabulary
  ``https://w3id.org/iadopt/record#``.

Who made a record (name, username and email) is included (D-040); IP addresses, browsers,
devices and screens are never read. A version's Turtle is its graph written out again: a
graph is stored once, with the text it first arrived in, which may be another person's.

The files are written one row at a time into a file object, normally a temporary file, so
memory use does not grow with the number of records. Read them inside one
:meth:`app.records.RecordStore.snapshot`, so every file shows the same moment.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile
from datetime import datetime
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Set

from rdflib import Graph

from .records import parse_turtle

RECORD_BASE = "https://w3id.org/iadopt/record/"
"""Where record IRIs live: ``<RECORD_BASE><record id>``, its versions and steps below it."""

VOCABULARY = "https://w3id.org/iadopt/record#"
"""The record vocabulary of D-036, for what the standard vocabularies do not name."""

CONTEXT: Dict[str, Any] = {
    "@vocab": VOCABULARY,
    "rec": VOCABULARY,
    "prov": "http://www.w3.org/ns/prov#",
    "oa": "http://www.w3.org/ns/oa#",
    "iop": "https://w3id.org/iadopt/ont/",
    "schema": "https://schema.org/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "created": {"@id": "dcterms:created", "@type": "xsd:dateTime"},
    "creator": {"@id": "dcterms:creator"},
    "name": {"@id": "schema:name"},
    "email": {"@id": "schema:email"},
    "startedAtTime": {"@id": "prov:startedAtTime", "@type": "xsd:dateTime"},
    "endedAtTime": {"@id": "prov:endedAtTime", "@type": "xsd:dateTime"},
    "recordedAt": {"@id": "rec:recordedAt", "@type": "xsd:dateTime"},
    "used": {"@id": "prov:used", "@type": "@id"},
    "generated": {"@id": "prov:generated", "@type": "@id"},
    "wasRevisionOf": {"@id": "prov:wasRevisionOf", "@type": "@id"},
    "calls": {"@id": "rec:call", "@container": "@list"},
    "steps": {"@id": "rec:step", "@container": "@list"},
    "changes": {"@id": "rec:change", "@container": "@list"},
    "versions": {"@id": "rec:version"},
    "bestVersion": {"@id": "rec:bestVersion", "@type": "@id"},
    "publishedAs": {"@id": "rec:publishedAs", "@type": "@id"},
    "nanopublication": {"@id": "rec:nanopublication", "@type": "@id"},
    "orcid": {"@id": "rec:orcid", "@type": "@id"},
    "role": {"@id": "rec:role", "@type": "@id"},
    "annotation": {"@id": "rec:annotation"},
    "motivatedBy": {"@id": "oa:motivatedBy", "@type": "@id"},
    "hasTarget": {"@id": "oa:hasTarget", "@type": "@id"},
    "bodyValue": {"@id": "oa:bodyValue"},
    "parsedAnswer": {"@id": "rec:parsedAnswer", "@type": "@json"},
    "validationErrors": {"@id": "rec:validationErrors", "@type": "@json"},
}
"""The JSON-LD context of ``records.jsonld``; any key it does not list falls in the record vocabulary."""

# Plain words for the stored codes. A code missing here is shown as it is stored.
ORIGIN_WORDS = {"decompose": "decomposition", "import": "opened Turtle"}
STEP_WORDS = {
    "draft": "model draft",
    "import": "opened Turtle",
    "edit": "applied change",
    "metadata": "publication metadata",
    "check": "looks right",
    "uncheck": "looks right undone",
    "mark_best": "marked as best",
    "publish": "published",
}
ROLE_WORDS = {
    "variable": "variable",
    "hasProperty": "property",
    "hasObjectOfInterest": "object of interest",
    "hasMatrix": "matrix",
    "hasContextObject": "context object",
    "hasStatisticalModifier": "statistical modifier",
    "hasConstraint": "constraint",
}
OPERATION_WORDS = {
    "add": "added",
    "remove": "removed",
    "relabel": "relabelled",
    "relink": "relinked",
    "change_shape": "changed shape",
    "add_constraint": "added constraint",
    "move_constraint": "moved constraint",
    "confirm": "marked as looking right",
    "unconfirm": "no longer marked as looking right",
    "other": "other change",
}

SUMMARY_COLUMNS = (
    "record", "started", "name", "username", "email", "definition", "started from", "model", "result",
    "model calls", "tokens in", "tokens out", "versions", "edits", "checks", "unchecks", "best version",
    "best mark note", "published as", "last published",
)
EDITS_COLUMNS = (
    "record", "when", "who", "definition", "step", "step kind", "component role", "change", "component",
    "before", "after", "version after",
)

TURTLE_PREFIXES = {
    "iop": "https://w3id.org/iadopt/ont/",
    "pav": "http://purl.org/pav/",
    "fdof": "https://w3id.org/fdof/ontology#",
}
"""Prefixes a written-out graph uses besides rdflib's own, whatever the stored text declared."""

_RECORDS = """
    SELECT i.id, i.public_id, i.origin, i.definition, i.created_at, u.id AS user_id, u.username,
           u.display_name, u.email
      FROM record_items i
      JOIN users u ON u.id = i.user_id
     ORDER BY i.id
"""
_SUMMARY = """
    SELECT i.public_id, i.created_at, i.origin, i.definition, u.username, u.display_name, u.email,
           r.provider, r.model, r.status AS run_status, r.error AS run_error,
           (SELECT COUNT(*) FROM record_llm_calls m WHERE m.item_id = i.id) AS calls,
           (SELECT SUM(m.input_tokens) FROM record_llm_calls m WHERE m.item_id = i.id) AS tokens_in,
           (SELECT SUM(m.output_tokens) FROM record_llm_calls m WHERE m.item_id = i.id) AS tokens_out,
           (SELECT COUNT(*) FROM record_versions v WHERE v.item_id = i.id) AS versions,
           (SELECT COUNT(*) FROM record_events e WHERE e.item_id = i.id AND e.type = 'edit') AS edits,
           (SELECT COUNT(*) FROM record_events e WHERE e.item_id = i.id AND e.type = 'check') AS checks,
           (SELECT COUNT(*) FROM record_events e WHERE e.item_id = i.id AND e.type = 'uncheck') AS unchecks,
           (SELECT v.number FROM record_events e JOIN record_versions v ON v.id = e.version_id
             WHERE e.item_id = i.id AND e.type = 'mark_best' ORDER BY e.seq DESC LIMIT 1) AS best_version,
           (SELECT e.note FROM record_events e
             WHERE e.item_id = i.id AND e.type = 'mark_best' ORDER BY e.seq DESC LIMIT 1) AS best_note,
           (SELECT e.occurred_at FROM record_events e
             WHERE e.item_id = i.id AND e.type = 'publish' ORDER BY e.seq DESC LIMIT 1) AS published_at,
           i.id AS item_id
      FROM record_items i
      JOIN users u ON u.id = i.user_id
      LEFT JOIN record_runs r ON r.item_id = i.id
     ORDER BY i.id
"""
_EDITS = """
    SELECT i.public_id, e.occurred_at, u.username, u.display_name, i.definition, e.seq, e.type,
           v.number AS version, c.operation, c.role, c.component, c.before_value, c.after_value
      FROM record_changes c
      JOIN record_events e ON e.id = c.event_id
      JOIN record_items i ON i.id = e.item_id
      JOIN users u ON u.id = i.user_id
      LEFT JOIN record_versions v ON v.id = e.version_id
     ORDER BY i.id, e.seq, c.position
"""


def plain_time(value: Optional[str]) -> str:
    """Return a stored UTC time as ``2026-11-18 10:15:02``, or the text unchanged if it is not one."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def words(table: Dict[str, str], code: Optional[str]) -> str:
    """Return the plain words for a stored code, or the code itself when it has none."""
    return table.get(code or "", code or "")


def graph_turtle(turtle: str) -> str:
    """Write a stored graph out again as Turtle, keeping nothing of its text but the triples.

    A graph is stored once, with the text it first arrived in, which may be another person's.
    The triples are copied into a new graph with fixed prefixes, so neither that text's
    comments and layout nor its prefix names reach the export.

    Args:
        turtle: The text the graph was first stored with.

    Returns:
        The graph as Turtle, or an empty value if the stored text no longer parses.
    """
    try:
        parsed = parse_turtle(turtle)
    except ValueError:
        return ""
    graph = Graph()
    for prefix, namespace in TURTLE_PREFIXES.items():
        graph.bind(prefix, namespace, override=True)
    for triple in parsed:
        graph.add(triple)
    return graph.serialize(format="turtle")


def short_reason(error: Optional[str]) -> str:
    """Return the first part of a run's error, which never quotes the model's answer.

    Errors can quote what the model wrote, such as a Turtle parser showing the text around a
    syntax error. ``summary.csv`` shows only the first line up to its first ``. `` or ``: ``;
    ``records.jsonld`` keeps the whole message.
    """
    if not error or not error.strip():
        return ""
    text = error.strip().splitlines()[0]
    sentence = text.find(". ")
    if sentence != -1:
        text = text[: sentence + 1]
    colon = text.find(": ")
    if colon != -1:
        text = text[:colon]
    return text[:120]


def _json_or_text(value: Optional[str]) -> Any:
    """Return stored JSON text as data, or the text itself when it is not JSON.

    ``NaN`` and ``Infinity``, which Python's JSON accepts but JSON does not, come back as text,
    so the export stays valid JSON.
    """
    if value is None:
        return None
    try:
        return json.loads(value, parse_constant=str)
    except ValueError:
        return value


def _role_iri(role: Optional[str]) -> Optional[str]:
    """Return a change's I-ADOPT role as a compact IRI: the property it fills, or the Variable class."""
    if not role:
        return None
    return "iop:Variable" if role == "variable" else f"iop:{role}"


def _drop_empty(node: Dict[str, Any]) -> Dict[str, Any]:
    """Leave out keys without a value, so each record only says what it has."""
    return {key: value for key, value in node.items() if value is not None and value != [] and value != ""}


def _record_node(conn: sqlite3.Connection, item: sqlite3.Row) -> Dict[str, Any]:
    """Build one record's JSON-LD node, its parts in the order they happened.

    Args:
        conn: The export's snapshot connection.
        item: The record's row from ``_RECORDS``.

    Returns:
        The record as a JSON-LD node.
    """
    item_id = int(item["id"])
    iri = f"{RECORD_BASE}{item['public_id']}"

    def version_iri(number: Optional[int]) -> Optional[str]:
        return None if number is None else f"{iri}/v{number}"

    versions: List[Dict[str, Any]] = []
    parents: Dict[int, Optional[int]] = {}
    for row in conn.execute(
        """
        SELECT v.number, v.kind, v.created_at, v.content_sha256, c.core_sha256, c.turtle, p.number AS parent
          FROM record_versions v
          JOIN record_contents c ON c.sha256 = v.content_sha256
          LEFT JOIN record_versions p ON p.id = v.parent_id
         WHERE v.item_id = ?
         ORDER BY v.number
        """,
        (item_id,),
    ):
        parents[int(row["number"])] = None if row["parent"] is None else int(row["parent"])
        versions.append(
            _drop_empty(
                {
                    "@id": version_iri(row["number"]),
                    "@type": ["prov:Entity", "Version"],
                    "number": row["number"],
                    "kind": row["kind"],
                    "created": row["created_at"],
                    "wasRevisionOf": version_iri(row["parent"]),
                    "sha256": row["content_sha256"],
                    "coreSha256": row["core_sha256"],
                    "turtle": graph_turtle(str(row["turtle"])),
                }
            )
        )

    run = conn.execute("SELECT * FROM record_runs WHERE item_id = ?", (item_id,)).fetchone()
    model_run: Optional[Dict[str, Any]] = None
    if run is not None:
        calls = [
            _drop_empty(
                {
                    "@type": ["prov:Activity", "ModelCall"],
                    "purpose": call["purpose"],
                    "attempt": call["attempt"],
                    "provider": call["provider"],
                    "model": call["model"],
                    "startedAtTime": call["started_at"],
                    "latencyMs": call["latency_ms"],
                    "inputTokens": call["input_tokens"],
                    "outputTokens": call["output_tokens"],
                    "status": call["status"],
                    "error": call["error"],
                }
            )
            for call in conn.execute("SELECT * FROM record_llm_calls WHERE item_id = ? ORDER BY id", (item_id,))
        ]
        model_run = _drop_empty(
            {
                "@type": ["prov:Activity", "ModelRun"],
                "provider": run["provider"],
                "model": run["model"],
                "reasoningOff": bool(run["disable_thinking"]),
                "promptSetup": run["prompt_setup"],
                "promptSha256": run["prompt_sha256"],
                "startedAtTime": run["started_at"],
                "endedAtTime": run["ended_at"],
                "status": run["status"],
                "error": run["error"],
                "schemaValid": None if run["schema_valid"] is None else bool(run["schema_valid"]),
                "validationErrors": _json_or_text(run["validation_errors"]),
                "parsedAnswer": _json_or_text(run["parsed_json"]),
                "rawOutput": run["raw_output"],
                "calls": calls,
            }
        )

    changes_by_event: Dict[int, List[Dict[str, Any]]] = {}
    for change in conn.execute(
        """
        SELECT c.event_id, c.operation, c.role, c.component, c.before_value, c.after_value
          FROM record_changes c
          JOIN record_events e ON e.id = c.event_id
         WHERE e.item_id = ?
         ORDER BY c.event_id, c.position
        """,
        (item_id,),
    ):
        changes_by_event.setdefault(int(change["event_id"]), []).append(
            _drop_empty(
                {
                    "@type": "Change",
                    "operation": change["operation"],
                    "role": _role_iri(change["role"]),
                    "component": change["component"],
                    "before": change["before_value"],
                    "after": change["after_value"],
                }
            )
        )

    steps: List[Dict[str, Any]] = []
    seen: Set[int] = set()
    best: Optional[str] = None
    published: List[str] = []
    for event in conn.execute(
        """
        SELECT e.id, e.seq, e.type, v.number AS version, e.occurred_at, e.recorded_at, e.note,
               e.unchecked_components, e.nanopub_uri, e.orcid
          FROM record_events e
          LEFT JOIN record_versions v ON v.id = e.version_id
         WHERE e.item_id = ?
         ORDER BY e.seq
        """,
        (item_id,),
    ):
        number = None if event["version"] is None else int(event["version"])
        # A step that first names a version made it, from its parent; any other step used it.
        generated: Optional[str] = None
        used = version_iri(number)
        if number is not None and number not in seen and event["type"] in ("draft", "import", "edit", "metadata", "publish"):
            generated, used = version_iri(number), version_iri(parents.get(number))
        if number is not None:
            seen.add(number)
        step: Dict[str, Any] = {
            "@id": f"{iri}/step/{event['seq']}",
            "@type": ["prov:Activity", "Step"],
            "number": event["seq"],
            "kind": event["type"],
            "endedAtTime": event["occurred_at"],
            "recordedAt": event["recorded_at"],
            "generated": generated,
            "used": used,
            "changes": changes_by_event.get(int(event["id"]), []),
        }
        if event["type"] == "mark_best":
            best = version_iri(number)
            step["annotation"] = _drop_empty(
                {
                    "@type": ["oa:Annotation", "BestMark"],
                    "motivatedBy": "oa:assessing",
                    "hasTarget": best,
                    "created": event["occurred_at"],
                    "bodyValue": event["note"],
                    "uncheckedComponents": event["unchecked_components"],
                }
            )
        if event["type"] == "publish":
            step["nanopublication"] = event["nanopub_uri"]
            step["orcid"] = event["orcid"]
            if event["nanopub_uri"]:
                published.append(event["nanopub_uri"])
        steps.append(_drop_empty(step))

    return _drop_empty(
        {
            "@id": iri,
            "@type": ["prov:Entity", "Record"],
            "created": item["created_at"],
            "startedFrom": item["origin"],
            "creator": _drop_empty(
                {
                    "@id": f"{RECORD_BASE}account/{item['user_id']}",
                    "@type": "schema:Person",
                    "name": item["display_name"],
                    "username": item["username"],
                    "email": item["email"],
                }
            ),
            "definition": item["definition"],
            "modelRun": model_run,
            "versions": versions,
            "steps": steps,
            "bestVersion": best,
            "publishedAs": published,
        }
    )


def _write_records(conn: sqlite3.Connection, out: io.TextIOBase) -> int:
    """Write ``records.jsonld``: one JSON-LD document, each record on its own line in ``@graph``."""
    out.write('{"@context": ' + json.dumps(CONTEXT, ensure_ascii=False) + ',\n"@graph": [\n')
    count = 0
    for item in conn.execute(_RECORDS):
        out.write(("" if count == 0 else ",\n") + json.dumps(_record_node(conn, item), ensure_ascii=False))
        count += 1
    out.write("\n]}\n")
    return count


def _write_summary(conn: sqlite3.Connection, out: io.TextIOBase) -> int:
    """Write ``summary.csv``: one row per record, in plain words."""
    writer = csv.writer(out)
    writer.writerow(SUMMARY_COLUMNS)
    count = 0
    for row in conn.execute(_SUMMARY):
        if row["run_status"] is None:
            result = ""
        elif row["run_status"] == "ok":
            result = "draft made"
        else:
            reason = short_reason(row["run_error"])
            result = f"failed: {reason}" if reason else "failed"
        published = [
            str(uri)
            for (uri,) in conn.execute(
                "SELECT nanopub_uri FROM record_events WHERE item_id = ? AND type = 'publish' AND nanopub_uri IS NOT NULL"
                " ORDER BY seq",
                (row["item_id"],),
            )
        ]
        writer.writerow(
            (
                row["public_id"],
                plain_time(row["created_at"]),
                row["display_name"],
                row["username"],
                row["email"],
                row["definition"],
                words(ORIGIN_WORDS, row["origin"]),
                f"{row['model']} ({row['provider']})" if row["model"] else "",
                result,
                row["calls"],
                row["tokens_in"],
                row["tokens_out"],
                row["versions"],
                row["edits"],
                row["checks"],
                row["unchecks"],
                "" if row["best_version"] is None else f"v{row['best_version']}",
                row["best_note"],
                " ".join(published),
                plain_time(row["published_at"]),
            )
        )
        count += 1
    return count


def _write_edits(conn: sqlite3.Connection, out: io.TextIOBase) -> int:
    """Write ``edits.csv``: one row per change inside a step, in plain words."""
    writer = csv.writer(out)
    writer.writerow(EDITS_COLUMNS)
    count = 0
    for row in conn.execute(_EDITS):
        writer.writerow(
            (
                row["public_id"],
                plain_time(row["occurred_at"]),
                row["display_name"] or row["username"],
                row["definition"],
                row["seq"],
                words(STEP_WORDS, row["type"]),
                words(ROLE_WORDS, row["role"]),
                words(OPERATION_WORDS, row["operation"]),
                row["component"],
                row["before_value"],
                row["after_value"],
                "" if row["version"] is None else f"v{row['version']}",
            )
        )
        count += 1
    return count


def _readme(exported_at: datetime, counts: Dict[str, int]) -> str:
    """Return the README that travels with the files."""

    def rows(name: str) -> str:
        return f"{counts[name]} {'row' if counts[name] == 1 else 'rows'}"

    return "\n".join(
        [
            f"I-ADOPT decomposition records, exported {exported_at.strftime('%Y-%m-%d %H:%M:%S')} UTC.",
            "",
            "These files hold the name, username and email of everyone whose work is recorded.",
            "Keep and share them as carefully as the user database.",
            "",
            f"summary.csv     {rows('summary.csv')}: one per record, in plain words.",
            f"edits.csv       {rows('edits.csv')}: one per change inside a step, in plain words.",
            f"records.jsonld  {counts['records.jsonld']} records in full, as JSON-LD.",
            "",
            "The record column links the three files. Times are UTC.",
            "",
            "In summary.csv: result gives the first part of a failure's reason (records.jsonld has",
            "the whole message). Tokens are the sums the model provider reported, so they are empty",
            "or partial when it reported none or some; PSNC's decomposition request reports none.",
            "versions counts the distinct versions; edits counts applied changes, one of which may",
            "leave the graph as it was; checks and unchecks count each \"Looks right\" set and undone.",
            "best version is the version marked as best most recently. published as lists every",
            "nanopublication made from the record, oldest first, and last published is the latest.",
            "",
            "records.jsonld is one JSON-LD document. Each record sits on its own line in @graph, in",
            "the order things happened: who made it, the definition, the model run with each call,",
            "every version with its Turtle, and every step with its changes; a best mark is a Web",
            "Annotation. A version's Turtle is its graph written out again, without the comments,",
            "layout or prefix names of the text it was stored with. In Python: json.load(file)['@graph'];",
            "with rdflib: Graph().parse('records.jsonld', format='json-ld').",
            "",
            "Read the CSV files with pandas (pandas.read_csv) or R (read.csv). In Excel, use",
            "Data > From Text/CSV rather than opening the files directly, so that text people typed",
            "is never run as a formula.",
            "",
        ]
    )


def write_export(conn: sqlite3.Connection, target: BinaryIO, *, exported_at: datetime) -> Dict[str, int]:
    """Write the export's ZIP into ``target``, one row at a time.

    Args:
        conn: A connection from :meth:`app.records.RecordStore.snapshot`, so every file shows
            the same moment.
        target: A writable binary file, normally a temporary file.
        exported_at: When the export was made (UTC), for the README.

    Returns:
        The number of rows in ``summary.csv`` and ``edits.csv`` and of records in
        ``records.jsonld``, keyed by file name.
    """
    counts: Dict[str, int] = {}
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        writers = (("summary.csv", _write_summary, ""), ("edits.csv", _write_edits, ""), ("records.jsonld", _write_records, "\n"))
        for name, write, newline in writers:
            with io.TextIOWrapper(archive.open(name, "w", force_zip64=True), encoding="utf-8", newline=newline) as out:
                counts[name] = write(conn, out)
        archive.writestr("README.txt", _readme(exported_at, counts))
    return counts


def export_file_name(exported_at: datetime) -> str:
    """Return the download's file name, such as ``iadopt-records-20261118T101500Z.zip``."""
    return f"iadopt-records-{exported_at.strftime('%Y%m%dT%H%M%SZ')}.zip"


def iter_file(handle: BinaryIO, chunk_size: int = 1 << 20) -> Iterator[bytes]:
    """Yield a file's bytes from the start, a chunk at a time, and close it when done or abandoned.

    Args:
        handle: The file the export was written to.
        chunk_size: Bytes per chunk.

    Yields:
        The file's bytes in order.
    """
    try:
        handle.seek(0)
        while chunk := handle.read(chunk_size):
            yield chunk
    finally:
        handle.close()
