"""Deterministic RDF/Turtle generation from an enriched prediction.

Serializes an enriched JSON prediction into the simple I-ADOPT Turtle shape the
frontend consumes (``json_to_ttl_repo_style``), including alt-label formula
assembly and per-component resource blocks.

Determinism note: output is byte-reproducible EXCEPT for the variable identity,
which ``make_variable_identity`` derives from the wall clock and a random suffix.
Golden tests freeze ``datetime``/``random`` in this module to pin output.

Depends on ``core.text``, ``core.config``, and ``services.orcid`` (for creator
metadata). It must never be imported by ``services.orcid`` (one-directional).
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..core.config import settings
from ..core.text import lookup_key, normalize_text, ttl_quote
from .orcid import orcid_suffix, resolve_creator_metadata

TTL_PREFIXES = """@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix pav: <http://purl.org/pav/> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix orcid: <https://orcid.org/> .
@prefix fdof: <https://w3id.org/fdof/ontology#> .

"""

WIKIDATA_ENTITY = "https://www.wikidata.org/entity/"
IADOPT_VARIABLE_BASE = "https://w3id.org/iadopt/variable/"


def wiki_to_entity(uri: Optional[str]) -> Optional[str]:
    """Normalize a Wikidata page URL into its canonical entity URL.

    Args:
        uri: A Wikidata URL/identifier or ``None``.

    Returns:
        The ``.../entity/Q...`` URL, or ``None`` if no QID is found.
    """
    if not uri:
        return None
    m = re.search(r"(Q\d+)", uri)
    if not m:
        return None
    return WIKIDATA_ENTITY + m.group(1)


def normalize_constraint_phrase_for_alt_label(label: str) -> str:
    """Convert a constraint label into a natural phrase for alt-label assembly.

    Strips a leading ``location:`` prefix and inserts ``at`` where needed, without
    changing the stored TTL constraint label.

    Args:
        label: The raw constraint label.

    Returns:
        The phrase suitable for alt-label assembly.
    """
    clean_label = normalize_text(label)

    if re.match(r"^location\s*:\s*", clean_label, re.IGNORECASE):
        clean_label = re.sub(r"^location\s*:\s*", "", clean_label, flags=re.IGNORECASE)
        if clean_label and not re.match(
            r"^(at|in|on|near|above|below|under|over|within|outside|around)\b", clean_label, re.IGNORECASE
        ):
            clean_label = f"at {clean_label}"

    return clean_label


def make_variable_identity() -> Tuple[str, str, str]:
    """Create the variable URI, textual identifier, and UTC timestamp literal.

    Reads the clock and RNG exactly once. NOT deterministic across calls — golden
    tests freeze ``datetime`` and ``random`` in this module.

    Returns:
        A ``(variable_uri, variable_identifier, created_literal)`` tuple.
    """
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    identifier_suffix = f"{created_at.strftime('%Y%m%dT%H%M%S')}-{random.randint(0, 99):02d}"
    variable_uri = f"{IADOPT_VARIABLE_BASE}{identifier_suffix}"
    variable_identifier = f"iadopt-variable-{identifier_suffix}"
    created_literal = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return variable_uri, variable_identifier, created_literal


def format_main_label(pref_label: str) -> str:
    """Capitalize the preferred label into a human-readable main label.

    Args:
        pref_label: The LLM-proposed preferred label.

    Returns:
        The capitalized label, or ``"Generated variable"`` when empty.
    """
    pref_label = normalize_text(pref_label)
    if not pref_label:
        return "Generated variable"
    return pref_label[:1].upper() + pref_label[1:]


def make_comment(formula_name: str) -> str:
    """Build the rdfs:comment explaining how the labels were produced.

    Args:
        formula_name: The alt-label formula name used.

    Returns:
        The comment text.
    """
    return (
        "LLM-proposed preferred label is stored in skos:prefLabel. "
        f"The alternative label is generated from the {formula_name} formula."
    )


def literal_join(parts: List[str]) -> str:
    """Join non-empty fragments with spaces and normalize the result.

    Args:
        parts: Text fragments (blanks are dropped).

    Returns:
        The normalized joined phrase.
    """
    return normalize_text(" ".join(part for part in parts if part))


def phrase_for_role(role: str, label: str, constraints_by_role: Dict[str, List[str]]) -> str:
    """Assemble a role's phrase, placing constraint text by role position.

    Constraint text precedes properties/modifiers and follows entities so the
    resulting label reads naturally.

    Args:
        role: The role key (e.g. ``"property"``, ``"object"``).
        label: The role's base label.
        constraints_by_role: Constraint phrases grouped by role.

    Returns:
        The assembled phrase (empty when the label is empty).
    """
    clean_label = normalize_text(label)
    if not clean_label:
        return ""

    clean_constraints = [normalize_text(item) for item in constraints_by_role.get(role, []) if normalize_text(item)]
    if not clean_constraints:
        return clean_label

    constraint_text = " ".join(clean_constraints)
    if role in {"property", "statistical_modifier"}:
        return literal_join([constraint_text, clean_label])
    return literal_join([clean_label, constraint_text])


def build_alt_label(formula_context: Dict[str, str], constraints_by_role: Dict[str, List[str]]) -> Tuple[str, str]:
    """Select the matching label formula and assemble the ``skos:altLabel`` text.

    Args:
        formula_context: Role labels plus ``ooi_kind``/``matrix_kind`` markers.
        constraints_by_role: Constraint phrases grouped by role.

    Returns:
        A ``(alt_label, formula_name)`` tuple.
    """
    uses_ooi_asymmetric = formula_context.get("ooi_kind") == "asymmetric"
    uses_matrix_asymmetric = formula_context.get("matrix_kind") == "asymmetric"

    if uses_ooi_asymmetric and formula_context.get("numerator") and formula_context.get("denominator"):
        formula_name = "asymmetric-numerator-denominator"
        phrase_plan = [
            (
                phrase_for_role(
                    "statistical_modifier", formula_context.get("statistical_modifier", ""), constraints_by_role
                ),
                None,
            ),
            (phrase_for_role("property", formula_context.get("property", ""), constraints_by_role), None),
            (phrase_for_role("numerator", formula_context.get("numerator", ""), constraints_by_role), "of"),
            (phrase_for_role("denominator", formula_context.get("denominator", ""), constraints_by_role), "in"),
            (phrase_for_role("matrix", formula_context.get("matrix", ""), constraints_by_role), "in"),
            (phrase_for_role("context", formula_context.get("context", ""), constraints_by_role), "in"),
        ]
    elif uses_ooi_asymmetric and formula_context.get("source") and formula_context.get("target"):
        formula_name = "asymmetric-source-target-object"
        phrase_plan = [
            (
                phrase_for_role(
                    "statistical_modifier", formula_context.get("statistical_modifier", ""), constraints_by_role
                ),
                None,
            ),
            (phrase_for_role("property", formula_context.get("property", ""), constraints_by_role), None),
            (phrase_for_role("source", formula_context.get("source", ""), constraints_by_role), "from"),
            (phrase_for_role("target", formula_context.get("target", ""), constraints_by_role), "to"),
            (phrase_for_role("matrix", formula_context.get("matrix", ""), constraints_by_role), "in"),
            (phrase_for_role("context", formula_context.get("context", ""), constraints_by_role), "in"),
        ]
    elif uses_matrix_asymmetric and formula_context.get("source") and formula_context.get("target"):
        formula_name = "asymmetric-source-target-matrix"
        phrase_plan = [
            (
                phrase_for_role(
                    "statistical_modifier", formula_context.get("statistical_modifier", ""), constraints_by_role
                ),
                None,
            ),
            (phrase_for_role("property", formula_context.get("property", ""), constraints_by_role), None),
            (phrase_for_role("object", formula_context.get("object", ""), constraints_by_role), "of"),
            (phrase_for_role("source", formula_context.get("source", ""), constraints_by_role), "from"),
            (phrase_for_role("target", formula_context.get("target", ""), constraints_by_role), "to"),
            (phrase_for_role("context", formula_context.get("context", ""), constraints_by_role), "in"),
        ]
    else:
        formula_name = "simple-entity"
        phrase_plan = [
            (
                phrase_for_role(
                    "statistical_modifier", formula_context.get("statistical_modifier", ""), constraints_by_role
                ),
                None,
            ),
            (phrase_for_role("property", formula_context.get("property", ""), constraints_by_role), None),
            (phrase_for_role("object", formula_context.get("object", ""), constraints_by_role), "of"),
            (phrase_for_role("matrix", formula_context.get("matrix", ""), constraints_by_role), "in"),
            (phrase_for_role("context", formula_context.get("context", ""), constraints_by_role), "in"),
        ]

    assembled: List[str] = []
    for phrase, connector in phrase_plan:
        if not phrase:
            continue
        if connector and assembled:
            assembled.append(connector)
        assembled.append(phrase)

    alt_label = literal_join(assembled)
    return alt_label or normalize_text(formula_context.get("pref_label", "")), formula_name


def json_to_ttl_repo_style(
    pred: Dict[str, Any],
    *,
    creator_orcid_id: Optional[str] = None,
) -> str:
    """Serialize an enriched prediction into the simple I-ADOPT Turtle shape.

    Args:
        pred: The enriched prediction (labels plus optional Wikidata ``*URI`` keys).
        creator_orcid_id: Optional ORCID override for creator/provenance metadata.

    Returns:
        The complete Turtle document as a string.

    Raises:
        RuntimeError: If no creator ORCID/name can be resolved (via the ORCID service).

    Side effects:
        Resolves creator metadata (may perform an ORCID HTTP lookup).
    """
    pref_label = normalize_text(pred.get("label") or "generated variable")
    main_label = format_main_label(pref_label)
    definition = normalize_text(pred.get("definition") or "")
    comment = normalize_text(pred.get("comment") or "")
    resolved_orcid, resolved_profile_name = resolve_creator_metadata(creator_orcid_id)
    variable_uri, variable_identifier, created_literal = make_variable_identity()
    creator_orcid_suffix = orcid_suffix(resolved_orcid) or "0000-0000-0000-0000"

    blocks: List[str] = []
    variable_lines: List[str] = []
    constraint_targets: Dict[str, Tuple[str, str]] = {}
    constraints_by_role: Dict[str, List[str]] = {}
    formula_context: Dict[str, str] = {
        "pref_label": pref_label,
        "ooi_kind": "simple",
        "matrix_kind": "simple",
    }

    def local_resource_ref(suffix: str) -> str:
        return f"<{variable_uri}#{suffix}>"

    def register_target(ref: str, role: str, *aliases: Optional[str]) -> None:
        # This lookup table lets constraint `on` values resolve against either field names or human-readable labels.
        for alias in aliases:
            if alias:
                constraint_targets[lookup_key(alias)] = (ref, role)

    def add_block(
        ref: str, rdf_types: List[str], label: Optional[str], extra_lines: Optional[List[str]] = None
    ) -> None:
        # Every linked resource gets its own readable TTL block so the frontend receives a self-contained graph.
        lines = [f"{ref}", "    a " + " ,\n      ".join(rdf_types) + " ;"]
        if label:
            lines.append(f"    rdfs:label {ttl_quote(label)} ;")
        for extra_line in extra_lines or []:
            lines.append(extra_line)
        # Close the block by replacing the last semicolon with a final period.
        lines[-1] = lines[-1].rstrip(" ;") + " ."
        blocks.append("\n".join(lines))

    def build_simple_component(field: str, label: str, rdf_type: str, uri_override: Optional[str]) -> Tuple[str, str]:
        clean_label = normalize_text(label)
        ref = f"<{uri_override}>" if uri_override else local_resource_ref(field)
        add_block(ref, [rdf_type], clean_label)
        return ref, clean_label

    def build_system_component(field: str, value: Dict[str, Any], role_name: str) -> Tuple[str, str]:
        system_key = "AsymmetricSystem" if "AsymmetricSystem" in value else "SymmetricSystem"
        system_uri = wiki_to_entity(value.get(f"{system_key}URI"))
        system_ref = f"<{system_uri}>" if system_uri else local_resource_ref(field)
        component_lines: List[str] = []
        # The system label is derived from its components in canonical order (first part + second
        # part), so every system is labelled consistently rather than from a per-variable phrase.
        component_labels: List[str] = []
        kind_key = "ooi_kind" if role_name == "object" else "matrix_kind" if role_name == "matrix" else f"{field}_kind"

        if system_key == "AsymmetricSystem":
            # Source/target and numerator/denominator resources are emitted explicitly so constraints
            # and alt-label formulas can target them individually.
            formula_context[kind_key] = "asymmetric"
            asym_roles = [
                ("hasSource", "source", f"{field}-source"),
                ("hasTarget", "target", f"{field}-target"),
                ("hasNumerator", "numerator", f"{field}-numerator"),
                ("hasDenominator", "denominator", f"{field}-denominator"),
            ]
            for key, role_name, suffix in asym_roles:
                role_label = normalize_text(value.get(key) or "")
                if not role_label:
                    continue
                role_uri = wiki_to_entity(value.get(f"{key}URI"))
                role_ref, clean_role_label = build_simple_component(suffix, role_label, "iop:Entity", role_uri)
                component_lines.append(f"    iop:{key} {role_ref} ;")
                component_labels.append(clean_role_label)
                formula_context[role_name] = clean_role_label
                register_target(role_ref, role_name, key, role_name, clean_role_label)

            system_label = " ".join(component_labels) or normalize_text(value.get(system_key) or field)
            add_block(system_ref, ["iop:Entity", "iop:AsymmetricSystem"], system_label, component_lines)
        else:
            formula_context[kind_key] = "symmetric"
            part_refs: List[str] = []
            raw_part_uris = value.get("hasPartURIs")
            part_uris: List[Any] = raw_part_uris if isinstance(raw_part_uris, list) else []
            for idx, part_label in enumerate(value.get("hasPart") or [], start=1):
                clean_part_label = normalize_text(part_label)
                if not clean_part_label:
                    continue
                part_uri = wiki_to_entity(part_uris[idx - 1]) if idx - 1 < len(part_uris) else None
                part_ref, _ = build_simple_component(f"{field}-part-{idx}", clean_part_label, "iop:Entity", part_uri)
                part_refs.append(part_ref)
                component_labels.append(clean_part_label)
                register_target(part_ref, f"{field}_part", clean_part_label)

            if part_refs:
                component_lines.append(f"    iop:hasPart {', '.join(part_refs)} ;")
            system_label = " ".join(component_labels) or normalize_text(value.get(system_key) or field)
            add_block(system_ref, ["iop:Entity", "iop:SymmetricSystem"], system_label, component_lines)

        return system_ref, system_label

    def build_component(field: str, rdf_type: str, role_name: str) -> Tuple[Optional[str], str]:
        # This one function keeps the simple-entity and system cases aligned so later label
        # generation and constraint resolution work from the same canonical context.
        value = pred.get(field)
        if isinstance(value, str) and normalize_text(value):
            uri = wiki_to_entity(pred.get(f"{field}URI"))
            ref, label = build_simple_component(field, value, rdf_type, uri)
            formula_context[role_name] = label
            register_target(ref, role_name, field, role_name, label)
            return ref, label

        if isinstance(value, dict):
            ref, label = build_system_component(field, value, role_name)
            formula_context[role_name] = label
            register_target(
                ref, role_name, field, role_name, label, value.get("AsymmetricSystem"), value.get("SymmetricSystem")
            )
            return ref, label

        return None, ""

    property_ref, _ = build_component("hasProperty", "iop:Property", "property")
    stat_ref, _ = build_component("hasStatisticalModifier", "iop:StatisticalModifier", "statistical_modifier")
    ooi_ref, _ = build_component("hasObjectOfInterest", "iop:Entity", "object")
    matrix_ref, _ = build_component("hasMatrix", "iop:Entity", "matrix")
    context_ref, _ = build_component("hasContextObject", "iop:Entity", "context")

    constraint_refs: List[str] = []
    for idx, constraint in enumerate(pred.get("hasConstraint") or [], start=1):
        if not isinstance(constraint, dict):
            continue

        constraint_label = normalize_text(constraint.get("label") or "")
        alt_constraint_label = normalize_constraint_phrase_for_alt_label(constraint_label)
        constraint_on = lookup_key(constraint.get("on") or "")
        if not constraint_label or not constraint_on:
            continue

        target_ref, target_role = constraint_targets.get(constraint_on, (None, None))
        if not target_ref or not target_role:
            continue

        constraints_by_role.setdefault(target_role, []).append(alt_constraint_label)
        constraint_ref = f"_:c{idx}"
        constraint_refs.append(constraint_ref)
        blocks.append(
            "\n".join(
                [
                    f"{constraint_ref}",
                    "    a iop:Constraint ;",
                    f"    rdfs:label {ttl_quote(constraint_label)} ;",
                    f"    iop:constrains {target_ref} .",
                ]
            )
        )

    alt_label, formula_name = build_alt_label(formula_context, constraints_by_role)
    ttl_comment = comment or make_comment(formula_name)

    variable_lines.extend(
        [
            f"<{variable_uri}>",
            "    a fdof:FAIRDigitalObject ,",
            "      iop:Variable ;",
            f"    dct:conformsTo <{settings.iadopt_variable_conforms_to}> ;",
            f"    rdfs:label {ttl_quote(main_label)} ;",
            f"    skos:prefLabel {ttl_quote(pref_label)} ;",
            f"    skos:altLabel {ttl_quote(alt_label)} ;",
            f"    skos:definition {ttl_quote(definition)} ;",
            f"    rdfs:comment {ttl_quote(ttl_comment)} ;",
            # The identifier is the resolvable variable IRI itself so the published id always resolves.
            f"    dct:identifier <{variable_uri}> ;",
            f'    dct:created "{created_literal}"^^xsd:dateTime ;',
            f"    dct:creator orcid:{creator_orcid_suffix} ;",
            f"    pav:createdWith {ttl_quote(settings.iadopt_created_with_label)} ;",
            f"    prov:wasAttributedTo orcid:{creator_orcid_suffix} ;",
        ]
    )

    if ooi_ref:
        variable_lines.append(f"    iop:hasObjectOfInterest {ooi_ref} ;")
    if property_ref:
        variable_lines.append(f"    iop:hasProperty {property_ref} ;")
    if matrix_ref:
        variable_lines.append(f"    iop:hasMatrix {matrix_ref} ;")
    if context_ref:
        variable_lines.append(f"    iop:hasContextObject {context_ref} ;")
    if stat_ref:
        variable_lines.append(f"    iop:hasStatisticalModifier {stat_ref} ;")
    if constraint_refs:
        variable_lines.append(f"    iop:hasConstraint {', '.join(constraint_refs)} ;")

    variable_lines[-1] = variable_lines[-1].rstrip(" ;") + " ."

    creator_block = "\n".join(
        [
            f"orcid:{creator_orcid_suffix}",
            f"    rdfs:label {ttl_quote(resolved_profile_name)} .",
        ]
    )

    return "\n".join([TTL_PREFIXES, "\n".join(variable_lines), "", *blocks, creator_block, ""])
