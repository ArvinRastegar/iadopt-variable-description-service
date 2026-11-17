"""Decomposition records: the research history of one variable description (D-036).

A record follows one piece of work by a signed-in account. Version 0 is the model's
draft, written by the decompose routes, or a Turtle the person opened or pasted. After it
come every change the person applies, each "Looks right" check, the version they mark as
best and the version they publish, written by the publish route. Visitors without an
account, and the development user of a deployment with authentication disabled, are never
recorded.

Storage rules:

* Rows are only ever added. Triggers refuse UPDATE and DELETE on every record table, and an
  INSERT whose keys already exist is ignored (for the deduplicated tables) or refused, so
  ``INSERT OR REPLACE`` cannot overwrite a row either.
* Each distinct RDF graph is stored once in ``record_contents``, keyed by the SHA-256 of its
  canonical N-Triples, so reformatting or new prefixes never count as a new version.
* Each content also carries a core fingerprint (:func:`core_graph`) that ignores what
  preparing a description for publication adds or changes: the variable's own IRI, its
  nanopublication metadata and extra type, and system labels. Two versions with the same
  core differ only in that; the server, not the browser, decides on that basis whether a
  version is an edit or only metadata, and whether a best mark still holds.
* Who: records point at ``users`` (name, username, email) and never copy those fields.
  Where: ``client_contexts`` keeps one row per distinct IP address, browser, device and
  screen of an account, and every event points at one.

The tables live in the auth database because they reference ``users``. The store reads the
auth store's database path on every connection and creates its tables when they are
missing, so it always follows that path.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

from fastapi import HTTPException, Request
from rdflib import BNode, Graph, URIRef
from rdflib.compare import to_canonical_graph
from rdflib.namespace import DCTERMS, PROV, RDF, RDFS

from .auth import AuthStore, utc_iso, utc_now
from .services.call_log import ModelCall

RECORD_SCHEMA_VERSION = 1
"""Version of the record tables. Raise it with every change to their shape."""

MAX_BLANK_NODES = 10
"""Most blank nodes a recorded graph may have. Canonical fingerprinting grows steeply with
blank nodes; real variable descriptions have one or two."""

MAX_TRIPLES = 5_000
"""Most triples a recorded graph may have."""

MAX_BLANK_NODE_TRIPLES = 100
"""Most triples that touch a blank node. With :data:`MAX_BLANK_NODES`, this keeps the
worst crafted graph to about 0.15 s of fingerprinting; real descriptions have a few dozen."""

MAX_USER_AGENT_CHARS = 500
"""Longest browser string kept for a client context."""

EARLIEST_CLIENT_TIME = datetime(2020, 1, 1, tzinfo=timezone.utc)
"""Browser times before this, or more than a day ahead, are replaced by the server's time."""

IADOPT_VARIABLE = URIRef("https://w3id.org/iadopt/ont/Variable")
"""The class of the variable a description is about."""

MANAGED_PREDICATES = frozenset(
    {
        DCTERMS.conformsTo,
        DCTERMS.identifier,
        DCTERMS.created,
        DCTERMS.creator,
        URIRef("http://purl.org/pav/createdWith"),
        PROV.wasAttributedTo,
    }
)
"""Nanopublication metadata the preparation step adds to the variable, as the frontend's
``applyPreNanopubSettingsToTurtle`` and ``applyCreatorOrcidToTurtle`` write it."""

FAIR_DIGITAL_OBJECT = URIRef("https://w3id.org/fdof/ontology#FAIRDigitalObject")
"""The extra type the preparation step gives the variable."""

SYSTEM_CLASSES = frozenset(
    {URIRef("https://w3id.org/iadopt/ont/AsymmetricSystem"), URIRef("https://w3id.org/iadopt/ont/SymmetricSystem")}
)
"""Classes of system nodes. The preparation step gives an unlabelled system a label derived
from its parts, so a system's label is presentation, not part of the core."""

CORE_VARIABLE = URIRef("urn:iadopt:record:variable")
"""Stands in for the variable's IRI in a core fingerprint, which minting a new IRI must not change."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS record_schema (
    version INTEGER NOT NULL
);

-- One piece of work: who did it, how it started, and the definition they typed.
CREATE TABLE IF NOT EXISTS record_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    origin TEXT NOT NULL CHECK (origin IN ('decompose', 'import')),
    definition TEXT,
    created_at TEXT NOT NULL
);

-- The model run behind a decomposition record: one per record.
CREATE TABLE IF NOT EXISTS record_runs (
    item_id INTEGER PRIMARY KEY REFERENCES record_items(id),
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    disable_thinking INTEGER NOT NULL DEFAULT 1,
    prompt_setup TEXT NOT NULL DEFAULT '',
    prompt_sha256 TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    error TEXT,
    raw_output TEXT,
    parsed_json TEXT,
    schema_valid INTEGER,
    validation_errors TEXT
);

-- Each call to a model inside a run, attempt by attempt.
CREATE TABLE IF NOT EXISTS record_llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES record_runs(item_id),
    purpose TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    status TEXT NOT NULL,
    error TEXT
);

-- Each distinct graph once, as the Turtle it first arrived in, with its core fingerprint.
CREATE TABLE IF NOT EXISTS record_contents (
    sha256 TEXT PRIMARY KEY,
    core_sha256 TEXT NOT NULL,
    turtle TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- Version 0, 1, 2 ... of a record, each pointing at its content and the version before it.
CREATE TABLE IF NOT EXISTS record_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES record_items(id),
    number INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('model_draft', 'imported', 'edited', 'metadata')),
    content_sha256 TEXT NOT NULL REFERENCES record_contents(sha256),
    parent_id INTEGER REFERENCES record_versions(id),
    created_at TEXT NOT NULL,
    UNIQUE (item_id, number)
);

-- Where a step came from: one row per distinct IP address, browser, device and screen.
CREATE TABLE IF NOT EXISTS client_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    ip_address TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    screen TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (user_id, ip_address, user_agent, device, screen)
);

-- The timeline: every step and milestone of a record, in order.
CREATE TABLE IF NOT EXISTS record_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES record_items(id),
    seq INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (
        type IN ('draft', 'import', 'edit', 'metadata', 'check', 'uncheck', 'mark_best', 'publish')
    ),
    version_id INTEGER REFERENCES record_versions(id),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    context_id INTEGER REFERENCES client_contexts(id),
    note TEXT,
    unchecked_components INTEGER,
    nanopub_uri TEXT,
    orcid TEXT,
    UNIQUE (item_id, seq)
);

-- What changed inside one step, component by component.
CREATE TABLE IF NOT EXISTS record_changes (
    event_id INTEGER NOT NULL REFERENCES record_events(id),
    position INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (
        operation IN (
            'add', 'remove', 'relabel', 'relink', 'change_shape',
            'add_constraint', 'move_constraint', 'confirm', 'unconfirm', 'other'
        )
    ),
    role TEXT NOT NULL DEFAULT '',
    component TEXT NOT NULL DEFAULT '',
    before_value TEXT,
    after_value TEXT,
    PRIMARY KEY (event_id, position)
);

CREATE INDEX IF NOT EXISTS idx_record_items_user ON record_items(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_record_versions_content ON record_versions(item_id, content_sha256);
CREATE INDEX IF NOT EXISTS idx_record_events_type ON record_events(item_id, type);
CREATE INDEX IF NOT EXISTS idx_record_events_nanopub ON record_events(nanopub_uri);
CREATE INDEX IF NOT EXISTS idx_record_llm_calls_item ON record_llm_calls(item_id);
"""

# For each record table: the condition under which a new row's keys match a stored row, and
# whether such an insert is dropped (deduplicated tables) or refused. Checked before the
# conflict clause applies, so REPLACE cannot delete and rewrite a stored row.
_KEY_GUARDS: Dict[str, Tuple[str, str]] = {
    "record_items": ("id = NEW.id OR public_id = NEW.public_id", "ABORT"),
    "record_runs": ("item_id = NEW.item_id", "ABORT"),
    "record_llm_calls": ("id = NEW.id", "ABORT"),
    "record_contents": ("sha256 = NEW.sha256", "IGNORE"),
    "record_versions": ("id = NEW.id OR (item_id = NEW.item_id AND number = NEW.number)", "ABORT"),
    "client_contexts": (
        "id = NEW.id OR (user_id = NEW.user_id AND ip_address = NEW.ip_address AND user_agent = NEW.user_agent"
        " AND device = NEW.device AND screen = NEW.screen)",
        "IGNORE",
    ),
    "record_events": ("id = NEW.id OR (item_id = NEW.item_id AND seq = NEW.seq)", "ABORT"),
    "record_changes": ("event_id = NEW.event_id AND position = NEW.position", "ABORT"),
}


def _raise(action: str, table: str) -> str:
    """Return the trigger body for a dropped (``IGNORE``) or refused (``ABORT``) insert."""
    if action == "IGNORE":
        return "SELECT RAISE(IGNORE);"
    return f"SELECT RAISE(ABORT, '{table} is append-only');"


_TRIGGERS = "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {table}_no_overwrite BEFORE INSERT ON {table}
WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition})
BEGIN {_raise(action, table)} END;
"""
    for table, (condition, action) in _KEY_GUARDS.items()
)


def _check_size(graph: Graph) -> None:
    """Refuse graphs too large to fingerprint quickly.

    Raises:
        ValueError: If the graph has more than :data:`MAX_TRIPLES` triples,
            :data:`MAX_BLANK_NODES` blank nodes or :data:`MAX_BLANK_NODE_TRIPLES` triples
            that touch a blank node.
    """
    if len(graph) > MAX_TRIPLES:
        raise ValueError(f"This Turtle has {len(graph)} triples; a record keeps at most {MAX_TRIPLES}.")
    blank_nodes = {term for triple in graph for term in triple if isinstance(term, BNode)}
    if len(blank_nodes) > MAX_BLANK_NODES:
        raise ValueError(
            f"This Turtle has {len(blank_nodes)} blank nodes; a record keeps at most {MAX_BLANK_NODES}. "
            "Give the other resources IRIs."
        )
    touching = sum(1 for triple in graph if any(isinstance(term, BNode) for term in triple))
    if touching > MAX_BLANK_NODE_TRIPLES:
        raise ValueError(
            f"This Turtle has {touching} statements about blank nodes; a record keeps at most "
            f"{MAX_BLANK_NODE_TRIPLES}. Give the resources IRIs."
        )


def graph_sha256(graph: Graph) -> str:
    """Fingerprint an RDF graph: the SHA-256 of its canonical, sorted N-Triples.

    Blank nodes are relabelled canonically first, so two Turtle texts that describe the
    same graph, with any prefixes, order or layout, get the same fingerprint.

    Args:
        graph: The parsed graph.

    Returns:
        The hex SHA-256 digest.

    Raises:
        ValueError: If the graph is too large (see :func:`_check_size`) or holds a term that
            has no valid N-Triples form, such as an IRI with a space.
    """
    _check_size(graph)
    try:
        lines = sorted(f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in to_canonical_graph(graph))
    except Exception as e:
        raise ValueError(f"Could not fingerprint the Turtle: {e}") from e
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def core_graph(graph: Graph) -> Graph:
    """Return the graph without what preparing it for publication adds or changes.

    Left out: triples on a variable with one of :data:`MANAGED_PREDICATES`, the variable's
    :data:`FAIR_DIGITAL_OBJECT` type, and the ``rdfs:label`` of system nodes
    (:data:`SYSTEM_CLASSES`). When there is exactly one variable, its IRI is replaced by
    :data:`CORE_VARIABLE` wherever it occurs, because preparation may mint a new IRI for it.
    A system label changed by hand therefore counts as presentation too; the step that
    changed it still lists the change, and the version keeps the exact graph.

    Args:
        graph: The parsed graph.

    Returns:
        A new graph with what the person actually described.
    """
    variables = {subject for subject in graph.subjects(RDF.type, IADOPT_VARIABLE) if isinstance(subject, URIRef)}
    systems = {subject for cls in SYSTEM_CLASSES for subject in graph.subjects(RDF.type, cls)}
    rename = next(iter(variables)) if len(variables) == 1 else None
    core = Graph()
    for s, p, o in graph:
        if s in variables and (p in MANAGED_PREDICATES or (p == RDF.type and o == FAIR_DIGITAL_OBJECT)):
            continue
        if s in systems and p == RDFS.label:
            continue
        core.add((CORE_VARIABLE if s == rename else s, p, CORE_VARIABLE if o == rename else o))
    return core


def fingerprints(graph: Graph) -> Tuple[str, str]:
    """Return a graph's full and core fingerprints (see :func:`graph_sha256`, :func:`core_graph`).

    Args:
        graph: The parsed graph.

    Returns:
        ``(sha256, core_sha256)``.
    """
    return graph_sha256(graph), graph_sha256(core_graph(graph))


def parse_turtle(turtle: str) -> Graph:
    """Parse Turtle into a graph.

    Args:
        turtle: The Turtle text.

    Returns:
        The parsed graph.

    Raises:
        ValueError: If the text is not valid Turtle.
    """
    graph = Graph()
    try:
        graph.parse(data=turtle, format="turtle")
    except Exception as e:
        raise ValueError(f"Could not parse the Turtle: {e}") from e
    return graph


def turtle_sha256(turtle: str) -> str:
    """Parse Turtle and fingerprint its graph (see :func:`graph_sha256`).

    Text that does not parse, or a graph that cannot be fingerprinted, is refused with
    ``ValueError``.

    Args:
        turtle: The Turtle text.

    Returns:
        The hex SHA-256 digest.
    """
    return graph_sha256(parse_turtle(turtle))


def _compact_json(value: Optional[Dict[str, Any]]) -> str:
    """Serialize a small dict deterministically, so equal values compare equal in SQL."""
    if not value:
        return ""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _client_time(value: Optional[datetime]) -> str:
    """Return a browser-reported time as ISO-8601 UTC, or the server's time when it is implausible.

    A naive value counts as UTC. A value before :data:`EARLIEST_CLIENT_TIME`, more than a
    day ahead of the server, or out of range for a UTC conversion is replaced by now.
    """
    now = utc_now()
    if value is None:
        return utc_iso(now)
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return utc_iso(now)
    if value < EARLIEST_CLIENT_TIME or value > now + timedelta(days=1):
        return utc_iso(now)
    return utc_iso(value)


class RecordStore:
    """Append-only research records, stored in the auth database (D-036)."""

    def __init__(self, auth: AuthStore) -> None:
        """Bind the store to the auth store whose database and accounts it shares.

        Args:
            auth: The process-wide auth store.
        """
        self.auth = auth
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Database access
    # ------------------------------------------------------------------ #

    def init(self) -> None:
        """Create the record tables in the current auth database now, not on first use."""
        with self._transaction():
            pass

    def applies_to(self, user: Optional[Dict[str, Any]]) -> bool:
        """Return whether this user's work is recorded: a signed-in account with an id.

        Args:
            user: The current user, or ``None``.
        """
        return bool(self.auth.enabled and user and user.get("id"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a connection to the auth database, commit on success, and always close it.

        The record tables are created whenever they are missing, so a replaced or restored
        database file gets them back without a restart.

        Yields:
            The open connection, with the record tables in place.
        """
        conn = sqlite3.connect(self.auth.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'record_changes_no_overwrite'"
            ).fetchone():
                self._create_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        """Create the record tables, indexes and append-only triggers if absent."""
        conn.executescript(_SCHEMA + _TRIGGERS)
        if conn.execute("SELECT COUNT(*) FROM record_schema").fetchone()[0] == 0:
            conn.execute("INSERT INTO record_schema (version) VALUES (?)", (RECORD_SCHEMA_VERSION,))
            conn.commit()

    # ------------------------------------------------------------------ #
    # Writes by the server
    # ------------------------------------------------------------------ #

    def record_decomposition(
        self,
        *,
        user: Dict[str, Any],
        request: Optional[Request],
        definition: str,
        provider: str,
        model: str,
        disable_thinking: bool,
        prompt_setup: str,
        prompt_sha256: Optional[str],
        started_at: datetime,
        ended_at: datetime,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
        client: Optional[Dict[str, Any]] = None,
        calls: Sequence[ModelCall] = (),
    ) -> Optional[str]:
        """Start a record from a decomposition: the run, a draft event, and the draft as version 0.

        Every run gets a draft event, so who and where are kept for failed runs too. A run
        that failed, or whose draft Turtle cannot be stored as a version, gets status
        ``error`` and no version.

        Args:
            user: The signed-in user who asked for the decomposition.
            request: The decompose request, for the IP address and browser.
            definition: The definition as typed.
            provider: The model provider that ran.
            model: The model that ran.
            disable_thinking: Whether reasoning was switched off.
            prompt_setup: Which prompt the run used, such as ``measured`` or ``few-shot:v3``.
            prompt_sha256: Fingerprint of that prompt without the definition, if known.
            started_at: When the run started.
            ended_at: When the run ended.
            result: The pipeline's final payload, or ``None`` when the run failed.
            error: The failure detail, or ``None``.
            client: Device and screen sent by the browser, if any.
            calls: Every request the run sent to a model, in order (stage 2); kept with the
                run, so a failed run keeps them too.

        Returns:
            The new record's id, or ``None`` when this user is not recorded.
        """
        if not self.applies_to(user):
            return None
        payload = result or {}
        ttl = str(payload.get("ttl") or "")
        content: Optional[Tuple[str, str]] = None
        if result is not None and not error:
            if not ttl.strip():
                error = "The run produced no Turtle draft."
            else:
                try:
                    content = fingerprints(parse_turtle(ttl))
                except ValueError as e:
                    error = f"The draft could not be stored as a version. {e}"
        status = "ok" if result is not None and not error else "error"
        parsed = payload.get("parsed_json")
        if isinstance(parsed, dict):
            # The pipeline copies the definition into the parsed answer; record_items keeps it once.
            parsed = {key: value for key, value in parsed.items() if key != "definition"}
        errors = payload.get("validation_errors")
        schema_valid = payload.get("schema_valid")
        with self._lock, self._transaction() as conn:
            item_id, public_id = self._insert_item(conn, user_id=int(user["id"]), origin="decompose", definition=definition)
            conn.execute(
                """
                INSERT INTO record_runs (
                    item_id, provider, model, disable_thinking, prompt_setup, prompt_sha256, started_at,
                    ended_at, status, error, raw_output, parsed_json, schema_valid, validation_errors
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    provider,
                    model,
                    int(disable_thinking),
                    prompt_setup,
                    prompt_sha256,
                    utc_iso(started_at),
                    utc_iso(ended_at),
                    status,
                    error,
                    payload.get("raw_llm_output"),
                    None if parsed is None else json.dumps(parsed, ensure_ascii=False),
                    None if schema_valid is None else int(bool(schema_valid)),
                    None if errors is None else json.dumps(errors, ensure_ascii=False),
                ),
            )
            conn.executemany(
                """
                INSERT INTO record_llm_calls (
                    item_id, purpose, attempt, provider, model, started_at, latency_ms,
                    input_tokens, output_tokens, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item_id,
                        call.purpose,
                        call.attempt,
                        call.provider,
                        call.model,
                        call.started_at,
                        call.latency_ms,
                        call.input_tokens,
                        call.output_tokens,
                        call.status,
                        call.error,
                    )
                    for call in calls
                ],
            )
            version_id: Optional[int] = None
            if content is not None:
                version_id = self._insert_version(
                    conn, item_id=item_id, number=0, kind="model_draft", content=content, turtle=ttl, parent_id=None
                )
            self._insert_event(
                conn,
                item_id=item_id,
                type_="draft",
                version_id=version_id,
                occurred_at=utc_iso(ended_at),
                context_id=self._context_id(conn, user_id=int(user["id"]), request=request, client=client),
            )
        return public_id

    def record_publish(
        self,
        *,
        user: Dict[str, Any],
        request: Optional[Request],
        target: Dict[str, Any],
        turtle: str,
        nanopub_uri: str,
        orcid: str,
        client: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add a publication to its record, after the nanopublication was published.

        When the published graph is not yet a version of the record, it is added as a
        ``metadata`` version of the marked version: :meth:`publish_target` has checked that
        the two differ only in nanopublication metadata.

        Args:
            user: The signed-in user who published.
            request: The publish request, for the IP address and browser.
            target: What :meth:`publish_target` returned for this publication.
            turtle: The Turtle that was published.
            nanopub_uri: The new nanopublication's URI.
            orcid: The ORCID iD the nanopublication names as its creator.
            client: Device and screen sent by the browser, if any.
        """
        item_id = int(target["item_id"])
        with self._lock, self._transaction() as conn:
            version_id = self._version_with_content(conn, item_id, str(target["sha256"]))
            if version_id is None:
                version_id = self._insert_version(
                    conn,
                    item_id=item_id,
                    number=self._next_version_number(conn, item_id),
                    kind="metadata",
                    content=(str(target["sha256"]), str(target["core_sha256"])),
                    turtle=turtle,
                    parent_id=int(target["marked_version_id"]),
                )
            self._insert_event(
                conn,
                item_id=item_id,
                type_="publish",
                version_id=version_id,
                occurred_at=utc_iso(),
                context_id=self._context_id(conn, user_id=int(user["id"]), request=request, client=client),
                nanopub_uri=nanopub_uri,
                orcid=orcid,
            )

    # ------------------------------------------------------------------ #
    # Writes by the browser
    # ------------------------------------------------------------------ #

    def start_import(
        self,
        *,
        user: Dict[str, Any],
        request: Optional[Request],
        turtle: str,
        occurred_at: Optional[datetime],
        client: Optional[Dict[str, Any]],
    ) -> Tuple[str, int]:
        """Start a record from an opened or pasted Turtle, as version 0.

        Turtle that does not parse, or cannot be fingerprinted, is refused with ``ValueError``.

        Args:
            user: The signed-in user.
            request: The request, for the IP address and browser.
            turtle: The Turtle as opened.
            occurred_at: When the browser opened it.
            client: Device and screen sent by the browser, if any.

        Returns:
            The new record's id and the version number (0).
        """
        content = fingerprints(parse_turtle(turtle))
        with self._lock, self._transaction() as conn:
            item_id, public_id = self._insert_item(conn, user_id=int(user["id"]), origin="import", definition=None)
            version_id = self._insert_version(
                conn, item_id=item_id, number=0, kind="imported", content=content, turtle=turtle, parent_id=None
            )
            self._insert_event(
                conn,
                item_id=item_id,
                type_="import",
                version_id=version_id,
                occurred_at=_client_time(occurred_at),
                context_id=self._context_id(conn, user_id=int(user["id"]), request=request, client=client),
            )
        return public_id, 0

    def append_event(
        self,
        *,
        user: Dict[str, Any],
        request: Optional[Request],
        record_id: str,
        type_: str,
        version: int,
        occurred_at: Optional[datetime],
        turtle: Optional[str] = None,
        changes: Sequence[Dict[str, Any]] = (),
        note: Optional[str] = None,
        unchecked_components: Optional[int] = None,
        client: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, int]:
        """Add one step to the user's own record.

        ``edit`` and ``metadata`` carry the Turtle after the change, made on ``version``. A
        new version is added only when the graph differs from that version; it is a
        ``metadata`` version when only nanopublication metadata changed and an ``edited``
        version otherwise, whatever the step type says. A ``metadata`` step that changes
        more than that metadata is refused. ``check``, ``uncheck`` and ``mark_best`` refer
        to ``version`` and add no version. Refusals are ``ValueError`` for Turtle and
        ``HTTPException`` 404 for a record or version this user does not have.

        Args:
            user: The signed-in user; only the record's owner can add to it.
            request: The request, for the IP address and browser.
            record_id: The record's id.
            type_: ``edit``, ``metadata``, ``check``, ``uncheck`` or ``mark_best``.
            version: The version the step was taken on.
            occurred_at: When the step happened in the browser.
            turtle: The Turtle after an ``edit`` or ``metadata`` step.
            changes: The component changes in the step (operation, role, component, before, after).
            note: The optional note of a ``mark_best``.
            unchecked_components: How many components lacked "Looks right" at ``mark_best``.
            client: Device and screen sent by the browser, if any.

        Returns:
            The step's number in the record, and the version the record is on after it.

        Raises:
            ValueError: If a ``metadata`` step changes more than nanopublication metadata.
        """
        content = fingerprints(parse_turtle(turtle or "")) if type_ in ("edit", "metadata") else None
        with self._lock, self._transaction() as conn:
            item = self._owned_item(conn, user, record_id)
            item_id = int(item["id"])
            target = self._version(conn, item_id, version)
            version_id, version_number = int(target["id"]), int(target["number"])
            if content is not None:
                same_core = content[1] == target["core_sha256"]
                if type_ == "metadata" and not same_core:
                    raise ValueError(
                        "A metadata step may only add or change nanopublication metadata. Record other changes as an edit."
                    )
                if content[0] != target["content_sha256"]:
                    version_number = self._next_version_number(conn, item_id)
                    version_id = self._insert_version(
                        conn,
                        item_id=item_id,
                        number=version_number,
                        kind="metadata" if same_core else "edited",
                        content=content,
                        turtle=turtle or "",
                        parent_id=int(target["id"]),
                    )
            event_id, seq = self._insert_event(
                conn,
                item_id=item_id,
                type_=type_,
                version_id=version_id,
                occurred_at=_client_time(occurred_at),
                context_id=self._context_id(conn, user_id=int(user["id"]), request=request, client=client),
                note=note,
                unchecked_components=unchecked_components,
            )
            conn.executemany(
                """
                INSERT INTO record_changes (
                    event_id, position, operation, role, component, before_value, after_value
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        event_id,
                        position,
                        change["operation"],
                        change.get("role") or "",
                        change.get("component") or "",
                        change.get("before"),
                        change.get("after"),
                    )
                    for position, change in enumerate(changes, start=1)
                ],
            )
        return seq, version_number

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def summary(self, *, user: Dict[str, Any], record_id: str) -> Dict[str, Any]:
        """Return a record's versions, best mark and publications.

        Owners read their own records and admins read any; others get a 404 ``HTTPException``.

        Args:
            user: The signed-in user.
            record_id: The record's id.

        Returns:
            The summary dict (see ``RecordSummaryResponse``).
        """
        with self._transaction() as conn:
            item = self._readable_item(conn, user, record_id)
            item_id = int(item["id"])
            versions = conn.execute(
                """
                SELECT v.number, v.kind, v.content_sha256, p.number AS parent, v.created_at
                  FROM record_versions v
                  LEFT JOIN record_versions p ON p.id = v.parent_id
                 WHERE v.item_id = ?
                 ORDER BY v.number
                """,
                (item_id,),
            ).fetchall()
            best = self._latest_mark(conn, item_id)
            publications = conn.execute(
                """
                SELECT v.number AS version, e.nanopub_uri, e.orcid, e.occurred_at AS published_at
                  FROM record_events e
                  JOIN record_versions v ON v.id = e.version_id
                 WHERE e.item_id = ? AND e.type = 'publish'
                 ORDER BY e.seq
                """,
                (item_id,),
            ).fetchall()
            event_count = conn.execute("SELECT COUNT(*) FROM record_events WHERE item_id = ?", (item_id,)).fetchone()[0]
        return {
            "record_id": item["public_id"],
            "origin": item["origin"],
            "created_at": item["created_at"],
            "current_version": versions[-1]["number"] if versions else None,
            "best_version": best["number"] if best else None,
            "versions": [dict(row) for row in versions],
            "publications": [dict(row) for row in publications],
            "event_count": int(event_count),
        }

    def publish_target(self, *, user: Dict[str, Any], record_id: str, graph: Graph) -> Dict[str, Any]:
        """Check that a publication publishes the version most recently marked as best.

        The published graph must have the same core as that version: it may add or change
        nanopublication metadata, as the preparation step does, but nothing else. An earlier
        mark no longer counts once another version is marked. Nothing is written.

        Args:
            user: The signed-in user; only the record's owner can publish from it.
            record_id: The record's id.
            graph: The parsed assertion graph about to be published.

        Returns:
            ``{"item_id", "marked_version_id", "sha256", "core_sha256"}`` for :meth:`record_publish`.

        Raises:
            HTTPException: 404 when the record does not exist for this user; 409 when no
                version is marked as best or the graph is not the marked version; 400 when
                the graph cannot be fingerprinted.
        """
        with self._transaction() as conn:
            item = self._owned_item(conn, user, record_id)
            item_id = int(item["id"])
            marked = self._latest_mark(conn, item_id)
        if marked is None:
            raise HTTPException(status_code=409, detail="Mark this version as best before publishing.")
        try:
            sha, core = fingerprints(graph)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if core != marked["core_sha256"]:
            raise HTTPException(
                status_code=409,
                detail="This is not the version you marked as best. Mark this version as best before publishing.",
            )
        return {"item_id": item_id, "marked_version_id": int(marked["id"]), "sha256": sha, "core_sha256": core}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _insert_item(
        self, conn: sqlite3.Connection, *, user_id: int, origin: str, definition: Optional[str]
    ) -> Tuple[int, str]:
        """Insert a record item with a fresh random id; return its row id and public id.

        Raises:
            RuntimeError: If five random ids in a row were already taken.
        """
        for _ in range(5):
            public_id = secrets.token_hex(8)
            if conn.execute("SELECT 1 FROM record_items WHERE public_id = ?", (public_id,)).fetchone():
                continue
            cursor = conn.execute(
                "INSERT INTO record_items (public_id, user_id, origin, definition, created_at) VALUES (?, ?, ?, ?, ?)",
                (public_id, user_id, origin, definition, utc_iso()),
            )
            return int(cursor.lastrowid or 0), public_id
        raise RuntimeError("Could not allocate a record id.")

    def _insert_version(
        self,
        conn: sqlite3.Connection,
        *,
        item_id: int,
        number: int,
        kind: str,
        content: Tuple[str, str],
        turtle: str,
        parent_id: Optional[int],
    ) -> int:
        """Store the content once, then add a version pointing at it; return the version's row id."""
        now = utc_iso()
        sha, core = content
        conn.execute(
            """
            INSERT OR IGNORE INTO record_contents (sha256, core_sha256, turtle, byte_size, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sha, core, turtle, len(turtle.encode("utf-8")), now),
        )
        cursor = conn.execute(
            """
            INSERT INTO record_versions (item_id, number, kind, content_sha256, parent_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, number, kind, sha, parent_id, now),
        )
        return int(cursor.lastrowid or 0)

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        item_id: int,
        type_: str,
        version_id: Optional[int],
        occurred_at: str,
        context_id: Optional[int],
        note: Optional[str] = None,
        unchecked_components: Optional[int] = None,
        nanopub_uri: Optional[str] = None,
        orcid: Optional[str] = None,
    ) -> Tuple[int, int]:
        """Append an event at the end of the record's timeline; return its row id and number."""
        seq = int(
            conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM record_events WHERE item_id = ?", (item_id,)).fetchone()[0]
        )
        cursor = conn.execute(
            """
            INSERT INTO record_events (
                item_id, seq, type, version_id, occurred_at, recorded_at, context_id,
                note, unchecked_components, nanopub_uri, orcid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                seq,
                type_,
                version_id,
                occurred_at,
                utc_iso(),
                context_id,
                note,
                unchecked_components,
                nanopub_uri,
                orcid,
            ),
        )
        return int(cursor.lastrowid or 0), seq

    def _context_id(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        request: Optional[Request],
        client: Optional[Dict[str, Any]],
    ) -> int:
        """Return the client context for this request and browser info, adding it once."""
        client = client or {}
        values = (
            user_id,
            self.auth.client_ip(request),
            (request.headers.get("user-agent", "") if request else "")[:MAX_USER_AGENT_CHARS],
            _compact_json(client.get("device")),
            _compact_json(client.get("screen")),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO client_contexts (user_id, ip_address, user_agent, device, screen, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (*values, utc_iso()),
        )
        row = conn.execute(
            """
            SELECT id FROM client_contexts
             WHERE user_id = ? AND ip_address = ? AND user_agent = ? AND device = ? AND screen = ?
            """,
            values,
        ).fetchone()
        return int(row["id"])

    def _owned_item(self, conn: sqlite3.Connection, user: Dict[str, Any], record_id: str) -> sqlite3.Row:
        """Return the user's own record.

        Raises:
            HTTPException: 404 when it does not exist or belongs to someone else.
        """
        row = conn.execute(
            "SELECT * FROM record_items WHERE public_id = ? AND user_id = ?", (record_id, int(user["id"]))
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found.")
        return row

    def _readable_item(self, conn: sqlite3.Connection, user: Dict[str, Any], record_id: str) -> sqlite3.Row:
        """Return a record the user may read: their own, or any record for an admin.

        Raises:
            HTTPException: 404 when no such record exists for this user.
        """
        if "admin" in (user.get("roles") or []):
            row = conn.execute("SELECT * FROM record_items WHERE public_id = ?", (record_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Record not found.")
            return row
        return self._owned_item(conn, user, record_id)

    def _version(self, conn: sqlite3.Connection, item_id: int, number: int) -> sqlite3.Row:
        """Return a version of the record by number, with its content's core fingerprint.

        Raises:
            HTTPException: 404 when the record has no such version.
        """
        row = conn.execute(
            """
            SELECT v.*, c.core_sha256
              FROM record_versions v
              JOIN record_contents c ON c.sha256 = v.content_sha256
             WHERE v.item_id = ? AND v.number = ?
            """,
            (item_id, number),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Version {number} does not exist in this record.")
        return row

    def _version_with_content(self, conn: sqlite3.Connection, item_id: int, sha: str) -> Optional[int]:
        """Return the row id of the record's latest version with this exact graph, if any."""
        row = conn.execute(
            "SELECT id FROM record_versions WHERE item_id = ? AND content_sha256 = ? ORDER BY number DESC LIMIT 1",
            (item_id, sha),
        ).fetchone()
        return int(row["id"]) if row else None

    def _latest_mark(self, conn: sqlite3.Connection, item_id: int) -> Optional[sqlite3.Row]:
        """Return the version most recently marked as best, with its core fingerprint, if any."""
        return conn.execute(
            """
            SELECT v.id, v.number, c.core_sha256
              FROM record_events e
              JOIN record_versions v ON v.id = e.version_id
              JOIN record_contents c ON c.sha256 = v.content_sha256
             WHERE e.item_id = ? AND e.type = 'mark_best'
             ORDER BY e.seq DESC
             LIMIT 1
            """,
            (item_id,),
        ).fetchone()

    def _next_version_number(self, conn: sqlite3.Connection, item_id: int) -> int:
        """Return the number the record's next version gets."""
        return int(
            conn.execute(
                "SELECT COALESCE(MAX(number), -1) + 1 FROM record_versions WHERE item_id = ?", (item_id,)
            ).fetchone()[0]
        )
