import { Constraint, Entity, Variable, type Concept } from '@/model/models.js';
import { KIND_NAMES, type Kind } from './kinds';

/*
 * Read-only views over the I-ADOPT model of src/model/models.js, shaped for the interface.
 * The model objects themselves stay the single source of truth.
 */

export type Component = Concept;

export type LinkSource = 'Wikidata' | 'QUDT' | 'OM' | 'Local' | 'External' | 'None';

export interface LinkInfo {
  source: LinkSource;
  /** The IRI as written, or null for blank nodes. */
  iri: string | null;
  /** A short name for the linked entry, such as Q629 or MassConcentration. */
  name: string | null;
}

export interface ComponentInfo {
  comp: Component;
  /** Unique within the list and the same after re-parsing the same Turtle; review checks use it. */
  key: string;
  role: string;
  kind: Kind;
  typeName: string;
  label: string;
  depth: number;
  /** For system parts: the system property linking them to their parent, such as hasSource. */
  relation?: string;
  /** For constraints: the labels of the components they narrow. */
  constrains: Component[];
  link: LinkInfo;
}

const WIKIDATA = /^https?:\/\/(?:www\.)?wikidata\.org\/(?:entity|wiki)\/(Q\d+)\b/i;
const QUDT_QUANTITY_KIND = 'http://qudt.org/vocab/quantitykind/';
const OM = /^https?:\/\/(?:www\.)?ontology-of-units-of-measure\.org\/resource\/om-2\/([^/#?]+)/i;
const LOCAL_VARIABLE = 'https://w3id.org/iadopt/variable/';

/** Recognise where an IRI points: the vocabularies the backend links to, a local IRI, or elsewhere. */
export function describeLink(comp: Component): LinkInfo {
  if (comp.isBlank()) return { source: 'None', iri: null, name: null };
  const iri = comp.getIri();
  if (!iri) return { source: 'None', iri: null, name: null };
  const wikidata = WIKIDATA.exec(iri);
  if (wikidata) return { source: 'Wikidata', iri, name: wikidata[1].toUpperCase() };
  if (iri.startsWith(QUDT_QUANTITY_KIND)) {
    return { source: 'QUDT', iri, name: iri.slice(QUDT_QUANTITY_KIND.length) || null };
  }
  const om = OM.exec(iri);
  if (om) return { source: 'OM', iri, name: om[1] };
  if (iri.startsWith(LOCAL_VARIABLE)) return { source: 'Local', iri, name: null };
  return { source: 'External', iri, name: iri.split(/[/#]/).filter(Boolean).pop() ?? null };
}

export function isVocabularyLink(link: LinkInfo): boolean {
  return link.source === 'Wikidata' || link.source === 'QUDT' || link.source === 'OM';
}

/** Roles the backend tries to link to Wikidata, QUDT or OM. */
export function isLinkableRole(role: string): boolean {
  return ['Property', 'OoI', 'Matrix', 'ContextObject', 'StatisticalModifier', 'SystemComponent'].includes(role);
}

export function labelOf(comp: Component): string {
  return comp.getLabel(false) ?? '';
}

function typeNameOf(comp: Component, kind: Kind): string {
  if (comp instanceof Entity && comp.isSystem()) {
    return `${KIND_NAMES[kind]} · ${comp.isSymmetricSystem() ? 'parts' : 'source and target'}`;
  }
  if (comp.getRole() === 'SystemComponent') return 'System part';
  return KIND_NAMES[kind];
}

function info(comp: Component, kind: Kind, depth: number, relation?: string): ComponentInfo {
  return {
    comp,
    key: '',
    role: comp.getRole(),
    kind,
    typeName: typeNameOf(comp, kind),
    label: labelOf(comp),
    depth,
    relation,
    constrains: comp instanceof Constraint ? comp.getEntities() : [],
    link: describeLink(comp),
  };
}

function withParts(list: ComponentInfo[], entity: Entity | undefined, kind: Kind) {
  if (!entity) return;
  list.push(info(entity, kind, 0));
  if (!entity.isSystem()) return;
  const parts = entity.getComponents();
  for (const relation of Object.keys(parts)) {
    for (const part of parts[relation]) list.push(info(part, kind, 1, relation));
  }
}

/** Every component of a variable in reading order: the variable, its parts, then the constraints. */
export function listComponents(variable: Variable): ComponentInfo[] {
  const list: ComponentInfo[] = [info(variable, 'var', 0)];
  const property = variable.getProperty();
  if (property) list.push(info(property, 'prop', 0));
  withParts(list, variable.getObjectOfInterest(), 'ooi');
  withParts(list, variable.getMatrix(), 'mat');
  for (const context of variable.getContextObjects()) withParts(list, context, 'ctx');
  const modifier = variable.getStatisticalModifier();
  if (modifier) list.push(info(modifier, 'stat', 0));
  for (const constraint of variable.getConstraints()) list.push(info(constraint, 'con', 0));

  // Blank parts and constraints can share role and label (two systems with "Component 1"), so
  // the key adds where the component sits and numbers repeats in reading order.
  const seen = new Map<string, number>();
  for (const item of list) {
    const base = `${item.kind}|${item.relation ?? ''}|${stableKey(item.comp)}`;
    const count = (seen.get(base) ?? 0) + 1;
    seen.set(base, count);
    item.key = count === 1 ? base : `${base}#${count}`;
  }
  return list;
}

/**
 * The part of a component's key that survives re-parsing the same Turtle. It changes when the
 * label or IRI changes, which is intended: an edited component should be checked again.
 */
export function stableKey(comp: Component): string {
  return `${comp.getRole()}|${comp.isBlank() ? '' : comp.getIri()}|${labelOf(comp)}`;
}
