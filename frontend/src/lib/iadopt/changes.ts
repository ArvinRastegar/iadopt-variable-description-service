import { Constraint, Entity, Variable, type Concept } from '@/model/models.js';
import type { RecordChange, RecordRole } from '@/shared/api';

/*
 * What changed between two states of a variable, component by component, for the decomposition
 * record (D-037). A snapshot is plain data taken before and after an applied change; the diff names
 * each change with the record's fixed vocabulary (add, remove, relabel, relink, change_shape,
 * add_constraint, move_constraint, other). The record also keeps every version's exact Turtle, so
 * this list is a summary of the step, not the only trace of it.
 */

/** The backend's bounds on one change (`RecordChange`) and on a step's list (`RecordEventRequest`). */
export const MAX_COMPONENT_CHARS = 500;
export const MAX_VALUE_CHARS = 2_000;
export const MAX_CHANGES = 200;

const ROLES: Record<string, RecordRole> = {
  Variable: 'variable',
  Property: 'hasProperty',
  OoI: 'hasObjectOfInterest',
  Matrix: 'hasMatrix',
  ContextObject: 'hasContextObject',
  StatisticalModifier: 'hasStatisticalModifier',
  Constraint: 'hasConstraint',
};

/** The I-ADOPT role of a component in its variable; system parts have none of their own. */
export function recordRole(comp: Concept): RecordRole {
  return ROLES[comp.getRole()] ?? '';
}

/** One component of a variable at one moment. */
export interface ComponentState {
  /**
   * Where the component sits: a slot of the variable (`variable`, `property`, `ooi`, `matrix`,
   * `modifier`), a list (`context`, `constraint`) or the parts of a system (`matrix/part` …). Only
   * components of the same group can be the same component in two states.
   */
  group: string;
  role: RecordRole;
  /** The IRI, or null for a blank node. */
  iri: string | null;
  /** The label as written, without one derived from the IRI. */
  label: string;
  comment: string;
  /** `single`, or a system's properties such as `hasSource+hasTarget`; empty for the variable and constraints. */
  shape: string;
  /** For constraints: the positions, in the same snapshot, of the components they narrow. */
  narrows: number[];
}

function shapeOf(entity: Entity): string {
  // getComponentKeys returns the system properties as an array; its JSDoc says string.
  return entity.isSystem() ? (entity.getComponentKeys() as unknown as string[]).join('+') : 'single';
}

/** Take a plain-data snapshot of every component, in reading order. */
export function snapshotComponents(variable: Variable): ComponentState[] {
  const states: ComponentState[] = [];
  const positions = new Map<Concept, number>();

  const add = (comp: Concept, group: string) => {
    positions.set(comp, states.length);
    states.push({
      group,
      role: recordRole(comp),
      iri: comp.isBlank() ? null : comp.getIri() ?? null,
      label: comp.getLabel() ?? '',
      comment: comp.getComment() ?? '',
      shape: comp instanceof Entity ? shapeOf(comp) : '',
      narrows: [],
    });
  };
  const withParts = (entity: Entity | undefined, group: string) => {
    if (!entity) return;
    add(entity, group);
    if (!entity.isSystem()) return;
    for (const parts of Object.values(entity.getComponents())) {
      for (const part of parts) add(part, `${group}/part`);
    }
  };

  add(variable, 'variable');
  const property = variable.getProperty();
  if (property) add(property, 'property');
  withParts(variable.getObjectOfInterest(), 'ooi');
  withParts(variable.getMatrix(), 'matrix');
  for (const context of variable.getContextObjects()) withParts(context, 'context');
  const modifier = variable.getStatisticalModifier();
  if (modifier) add(modifier, 'modifier');
  const constraints: Constraint[] = variable.getConstraints();
  for (const constraint of constraints) add(constraint, 'constraint');
  for (const constraint of constraints) {
    states[positions.get(constraint) as number].narrows = constraint
      .getEntities()
      .map((target) => positions.get(target))
      .filter((position): position is number => position !== undefined);
  }
  return states;
}

/** How a change names its component: the IRI, else the label, else where it sits. */
export function componentName(state: Pick<ComponentState, 'iri' | 'label' | 'group'>): string {
  return state.iri || state.label || state.group;
}

/**
 * Pair each component after a change with the one it was before: in the same group, first by
 * IRI, then by label, then what is left in order. Returns, for each position after, the
 * position before, or -1 for a component that is new.
 */
function pairComponents(before: ComponentState[], after: ComponentState[]): number[] {
  const pairs = after.map(() => -1);
  const taken = new Set<number>();
  const passes: Array<(was: ComponentState, now: ComponentState) => boolean> = [
    (was, now) => Boolean(now.iri) && was.iri === now.iri,
    (was, now) => Boolean(now.label) && was.label === now.label,
    () => true,
  ];
  for (const matches of passes) {
    after.forEach((now, index) => {
      if (pairs[index] >= 0) return;
      const found = before.findIndex((was, position) => !taken.has(position) && was.group === now.group && matches(was, now));
      if (found >= 0) {
        pairs[index] = found;
        taken.add(found);
      }
    });
  }
  return pairs;
}

const orNull = (value: string) => value || null;

/**
 * The changes that turn one snapshot into the other. A system part that only moved between the
 * properties of its system is covered by the system's `change_shape`. A step that changed nothing
 * a component shows, such as prefixes or metadata, gives an empty list.
 */
export function diffComponents(before: ComponentState[], after: ComponentState[]): RecordChange[] {
  const pairs = pairComponents(before, after);
  const afterOf = new Map<number, number>();
  pairs.forEach((was, now) => {
    if (was >= 0) afterOf.set(was, now);
  });
  const names = (states: ComponentState[], positions: number[]) => positions.map((position) => componentName(states[position])).join(', ');

  const changes: RecordChange[] = [];
  after.forEach((now, index) => {
    const role = now.role;
    const component = componentName(now);
    const position = pairs[index];
    if (position < 0) {
      changes.push({ operation: role === 'hasConstraint' ? 'add_constraint' : 'add', role, component, after: orNull(now.label || now.iri || '') });
      return;
    }
    const was = before[position];
    if (was.label !== now.label) changes.push({ operation: 'relabel', role, component, before: orNull(was.label), after: orNull(now.label) });
    if (was.iri !== now.iri) changes.push({ operation: 'relink', role, component, before: was.iri, after: now.iri });
    if (was.shape !== now.shape) changes.push({ operation: 'change_shape', role, component, before: orNull(was.shape), after: orNull(now.shape) });
    if (was.comment !== now.comment) changes.push({ operation: 'other', role, component, before: orNull(was.comment), after: orNull(now.comment) });
    if (role === 'hasConstraint') {
      // Targets that have no counterpart after the change count as gone.
      const moved = was.narrows.map((target) => afterOf.get(target) ?? -1);
      const same = moved.length === now.narrows.length && moved.every((target) => now.narrows.includes(target));
      if (!same) {
        changes.push({ operation: 'move_constraint', role, component, before: orNull(names(before, was.narrows)), after: orNull(names(after, now.narrows)) });
      }
    }
  });
  before.forEach((was, position) => {
    if (!afterOf.has(position)) {
      changes.push({ operation: 'remove', role: was.role, component: componentName(was), before: orNull(was.label || was.iri || '') });
    }
  });
  return changes;
}

/** Cut a text to a number of characters as the backend counts them (code points), never inside one. */
export function clip(value: string, max: number): string {
  const characters = Array.from(value);
  return characters.length > max ? characters.slice(0, max).join('') : value;
}

/** Keep a step's changes within the backend's bounds: values are cut and changes past the limit dropped. */
export function clampChanges(changes: RecordChange[]): RecordChange[] {
  return changes.slice(0, MAX_CHANGES).map((change) => ({
    ...change,
    ...(typeof change.component === 'string' ? { component: clip(change.component, MAX_COMPONENT_CHARS) } : {}),
    ...(typeof change.before === 'string' ? { before: clip(change.before, MAX_VALUE_CHARS) } : {}),
    ...(typeof change.after === 'string' ? { after: clip(change.after, MAX_VALUE_CHARS) } : {}),
  }));
}
