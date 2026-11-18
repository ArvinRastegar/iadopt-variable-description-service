"""Admin export of every decomposition record as CSV tables (D-040).

One download holds the whole research record: who made each record (name, username and
email), the definition and its model run, every step, every component change, every version
with its Turtle, and every model call. The tables join on ``record_id``; ``changes.csv`` also
joins ``steps.csv`` on ``step``. ``columns.csv`` says what each column means and
``README.txt`` how to read the files.

IP addresses, browsers, devices and screens stay in the database and are never exported.

Values are written as stored: UTF-8, comma-separated, with quotes around values that hold
commas, quotes or line breaks, so a Turtle that spans several lines stays in one cell. The one
exception is a version's Turtle. A graph is stored once, with the text it first arrived in,
which may belong to another person's record, so the export writes each version's graph out
again: its comments and layout are not kept. Nothing people typed is escaped, so a
spreadsheet that opens a file directly may treat a value starting with ``=`` as a formula; the
README says to read the tables with pandas or R, or to import them in Excel with Data > From
Text/CSV.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Sequence, Tuple

from .records import parse_turtle


@dataclass(frozen=True)
class ExportTable:
    """One CSV file of the export.

    Attributes:
        file_name: The file's name inside the ZIP.
        summary: One line on what a row is, for the README.
        columns: Each column's name and meaning, in file order; ``columns.csv`` lists them.
        query: The SQL that reads the rows, with one result column per entry of ``columns``.
    """

    file_name: str
    summary: str
    columns: Tuple[Tuple[str, str], ...]
    query: str


TABLES: Tuple[ExportTable, ...] = (
    ExportTable(
        file_name="records.csv",
        summary="one per record: who made it, the definition and the model run",
        columns=(
            ("record_id", "The record's id. Every table joins on it."),
            ("created_at", "When the record started (UTC, ISO 8601)."),
            ("origin", "decompose: started by a decomposition. import: started from Turtle the person opened or pasted."),
            ("user_id", "The account's number in the user database; it stays the same if the username changes."),
            ("username", "The account's username."),
            ("name", "The account's display name."),
            ("email", "The account's email address."),
            ("definition", "The definition as typed; empty for imported records."),
            ("provider", "The model provider of the run: psnc or openrouter. Empty for imported records."),
            ("model", "The model that ran."),
            ("reasoning_off", "1 when the run switched the model's reasoning off, 0 when it did not."),
            ("prompt_setup", "Which prompt the run used, such as measured."),
            ("prompt_sha256", "Fingerprint of that prompt without the definition."),
            ("run_started_at", "When the run started (UTC)."),
            ("run_ended_at", "When the run ended (UTC)."),
            ("run_status", "ok or error."),
            ("run_error", "Why the run failed, when it did."),
            ("schema_valid", "1 when the parsed answer matched the JSON schema, 0 when it did not, empty when unknown."),
            ("validation_errors", "The schema validation errors, as JSON."),
            ("parsed_answer", "The model's parsed answer, as JSON, without the definition."),
            ("raw_output", "The model's answer as it arrived."),
        ),
        query="""
            SELECT i.public_id AS record_id, i.created_at, i.origin, u.id AS user_id, u.username,
                   u.display_name AS name, u.email, i.definition, r.provider, r.model,
                   r.disable_thinking AS reasoning_off, r.prompt_setup, r.prompt_sha256,
                   r.started_at AS run_started_at, r.ended_at AS run_ended_at, r.status AS run_status,
                   r.error AS run_error, r.schema_valid, r.validation_errors,
                   r.parsed_json AS parsed_answer, r.raw_output
              FROM record_items i
              JOIN users u ON u.id = i.user_id
              LEFT JOIN record_runs r ON r.item_id = i.id
             ORDER BY i.id
        """,
    ),
    ExportTable(
        file_name="steps.csv",
        summary="one per step, in the order the steps were stored",
        columns=(
            ("record_id", "The record the step belongs to."),
            ("step", "The step's number in its record, from 1, in the order the steps were stored."),
            (
                "type",
                "draft: the model's draft. import: opened or pasted Turtle. edit: an applied change. "
                "metadata: a change to nanopublication metadata only. check, uncheck: \"Looks right\" set or "
                "cleared. mark_best: marked as best. publish: published as a nanopublication.",
            ),
            ("version", "The version the step made or refers to; empty for the draft of a failed run."),
            ("occurred_at", "When the step happened: the browser's time when plausible, else the server's (UTC)."),
            ("recorded_at", "When the server stored the step (UTC)."),
            ("note", "The optional note given with a best mark."),
            ("unchecked_components", "At a best mark, how many components were not marked \"Looks right\"."),
            ("nanopub_uri", "The nanopublication a publish step created."),
            ("orcid", "The ORCID iD that nanopublication names as its creator."),
        ),
        query="""
            SELECT i.public_id AS record_id, e.seq AS step, e.type, v.number AS version, e.occurred_at,
                   e.recorded_at, e.note, e.unchecked_components, e.nanopub_uri, e.orcid
              FROM record_events e
              JOIN record_items i ON i.id = e.item_id
              LEFT JOIN record_versions v ON v.id = e.version_id
             ORDER BY i.id, e.seq
        """,
    ),
    ExportTable(
        file_name="changes.csv",
        summary="one per component change inside a step",
        columns=(
            ("record_id", "The record the change belongs to."),
            ("step", "The step the change belongs to; joins steps.csv on record_id and step."),
            ("position", "The change's place within its step, from 1."),
            (
                "operation",
                "add, remove, relabel, relink, change_shape, add_constraint, move_constraint, confirm, "
                "unconfirm or other.",
            ),
            ("role", "The component's I-ADOPT role, such as hasMatrix, or variable for the variable itself; empty when it has none."),
            ("component", "The component's IRI, or its label when it has no IRI."),
            ("before", "The value before the change, when there was one."),
            ("after", "The value after the change, when there is one."),
        ),
        query="""
            SELECT i.public_id AS record_id, e.seq AS step, c.position, c.operation, c.role, c.component,
                   c.before_value AS before, c.after_value AS after
              FROM record_changes c
              JOIN record_events e ON e.id = c.event_id
              JOIN record_items i ON i.id = e.item_id
             ORDER BY i.id, e.seq, c.position
        """,
    ),
    ExportTable(
        file_name="versions.csv",
        summary="one per version, with its Turtle",
        columns=(
            ("record_id", "The record the version belongs to."),
            ("version", "The version's number in its record, from 0."),
            (
                "kind",
                "model_draft, imported, edited, or metadata (only nanopublication metadata changed).",
            ),
            ("parent_version", "The version it was made from; empty for version 0."),
            ("created_at", "When the version was stored (UTC)."),
            ("sha256", "SHA-256 of the graph's canonical N-Triples; equal values mean the same graph."),
            (
                "core_sha256",
                "The same fingerprint without what preparing for publication adds; equal values mean the same "
                "description.",
            ),
            (
                "turtle",
                "The version's graph, written out as Turtle. A graph is stored once, with the text it first "
                "arrived in, possibly in another record, so it is written out again: comments and layout are not kept.",
            ),
        ),
        query="""
            SELECT i.public_id AS record_id, v.number AS version, v.kind, p.number AS parent_version,
                   v.created_at, v.content_sha256 AS sha256, c.core_sha256, c.turtle
              FROM record_versions v
              JOIN record_items i ON i.id = v.item_id
              JOIN record_contents c ON c.sha256 = v.content_sha256
              LEFT JOIN record_versions p ON p.id = v.parent_id
             ORDER BY i.id, v.number
        """,
    ),
    ExportTable(
        file_name="model_calls.csv",
        summary="one per request a decomposition run sent to a model",
        columns=(
            ("record_id", "The record whose run sent the request."),
            ("purpose", "decomposition, label or linking."),
            ("attempt", "Which try of that purpose it was, from 1; for linking, which try for the same term."),
            ("provider", "The provider the request went to."),
            ("model", "The model the request named."),
            ("started_at", "When the request was sent (UTC)."),
            ("latency_ms", "Milliseconds until the answer was complete or the request failed."),
            ("input_tokens", "Tokens sent, when the provider reported them."),
            ("output_tokens", "Tokens received, when the provider reported them."),
            ("status", "ok: the answer was used. unusable: an answer came but could not be used. error: none came."),
            ("error", "A short reason when the status is not ok."),
        ),
        query="""
            SELECT i.public_id AS record_id, m.purpose, m.attempt, m.provider, m.model, m.started_at,
                   m.latency_ms, m.input_tokens, m.output_tokens, m.status, m.error
              FROM record_llm_calls m
              JOIN record_items i ON i.id = m.item_id
             ORDER BY i.id, m.id
        """,
    ),
)
"""The tables of the export, in the order the README lists them."""


def _graph_turtle(turtle: str) -> str:
    """Write a stored graph out again as Turtle, without the comments and layout of its text.

    Args:
        turtle: The text the graph was first stored with.

    Returns:
        The graph as Turtle, or an empty value if the stored text no longer parses.
    """
    try:
        graph = parse_turtle(turtle)
    except ValueError:
        return ""
    return graph.serialize(format="turtle")


def _cell(row: sqlite3.Row, column: str, graphs: Dict[str, str]) -> object:
    """Return a row's value for a column; a version's Turtle is its graph written out again.

    Args:
        row: A row of one table's query.
        column: The column to read.
        graphs: Turtle already written out in this export, by the graph's ``sha256``.
    """
    if column != "turtle":
        return row[column]
    sha = str(row["sha256"])
    if sha not in graphs:
        graphs[sha] = _graph_turtle(str(row["turtle"]))
    return graphs[sha]


def _csv(header: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """Write a header and rows as CSV text; ``None`` becomes an empty value."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def _readme(exported_at: datetime, counts: Dict[str, int]) -> str:
    """Return the README that travels with the files."""
    width = max(len(table.file_name) for table in TABLES)
    lines = [
        f"I-ADOPT decomposition records, exported {exported_at.strftime('%Y-%m-%dT%H:%M:%SZ')}.",
        "",
        "These files hold the name, username and email of everyone whose work is recorded.",
        "Keep and share them as carefully as the user database.",
        "",
        "Tables. They join on record_id; changes.csv also joins steps.csv on step.",
    ]
    for table in TABLES:
        rows = counts[table.file_name]
        lines.append(f"  {table.file_name.ljust(width)}  {rows} {'row' if rows == 1 else 'rows'}: {table.summary}")
    lines += [
        f"  {'columns.csv'.ljust(width)}  what every column means",
        "",
        "Values are as stored: UTF-8, comma-separated, with quotes around values that hold commas,",
        "quotes or line breaks, so a whole Turtle stays in one cell. A version's Turtle is its graph",
        "written out again, without the comments and layout of the text it was stored with.",
        "Read the tables with pandas (pandas.read_csv) or R (read.csv). In Excel, use",
        "Data > From Text/CSV rather than opening the files directly, so that text people typed is",
        "never run as a formula.",
        "",
    ]
    return "\n".join(lines)


def build_export(conn: sqlite3.Connection, *, exported_at: datetime) -> Tuple[bytes, Dict[str, int]]:
    """Read every record table and pack the CSV files, ``columns.csv`` and the README into a ZIP.

    Args:
        conn: A connection from :meth:`app.records.RecordStore.snapshot`, so all tables are read
            at the same moment.
        exported_at: When the export was made (UTC), for the README.

    Returns:
        The ZIP's bytes, and the number of rows in each table, keyed by file name.
    """
    counts: Dict[str, int] = {}
    graphs: Dict[str, str] = {}
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for table in TABLES:
            names = [name for name, _ in table.columns]
            rows = conn.execute(table.query).fetchall()
            archive.writestr(table.file_name, _csv(names, ([_cell(row, name, graphs) for name in names] for row in rows)))
            counts[table.file_name] = len(rows)
        archive.writestr(
            "columns.csv",
            _csv(
                ("file", "column", "meaning"),
                ((table.file_name, name, meaning) for table in TABLES for name, meaning in table.columns),
            ),
        )
        archive.writestr("README.txt", _readme(exported_at, counts))
    return archive_bytes.getvalue(), counts


def export_file_name(exported_at: datetime) -> str:
    """Return the download's file name, such as ``iadopt-records-20261118T101500Z.zip``."""
    return f"iadopt-records-{exported_at.strftime('%Y%m%dT%H%M%SZ')}.zip"
