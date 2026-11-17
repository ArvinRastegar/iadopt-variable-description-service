import { afterEach, describe, expect, test, vi } from 'vitest';

import { clampChanges, clip, componentName, diffComponents, MAX_CHANGES, MAX_COMPONENT_CHARS, MAX_VALUE_CHARS, snapshotComponents } from '../src/lib/iadopt/changes';
import { addConstraint, addToVariable, applyEdit, deleteComponent } from '../src/lib/iadopt/editing';
import { clientInfo } from '../src/lib/clientInfo';
import { markStatusOf, readRecordState, restoredRecord, sameTurtle } from '../src/app/state/recording';
import extract from '../src/lib/extract.js';
import { Entity, Variable } from '../src/model/models.js';
import type { RecordChange } from '../src/shared/api';

/*
 * The decomposition record's view of an applied change (D-037): each change is named with the
 * record's vocabulary and kept within the backend's bounds, and the browser reports its device
 * and screen only within the bounds the backend keeps.
 */

const MATRIX = 'https://w3id.org/iadopt/variable/20261023T104215-42#hasMatrix';
const OXYGEN = 'https://www.wikidata.org/entity/Q629';
const TTL = `@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://w3id.org/iadopt/variable/20261023T104215-42> a iop:Variable ;
  rdfs:label "Daily mean dissolved oxygen" ;
  iop:hasProperty <http://qudt.org/vocab/quantitykind/MassConcentration> ;
  iop:hasObjectOfInterest <${OXYGEN}> ;
  iop:hasMatrix <${MATRIX}> ;
  iop:hasConstraint _:c1 .
<http://qudt.org/vocab/quantitykind/MassConcentration> a iop:Property ; rdfs:label "mass concentration" .
<${OXYGEN}> a iop:Entity ; rdfs:label "oxygen" .
<${MATRIX}> a iop:Entity ; rdfs:label "sea water" .
_:c1 a iop:Constraint ; rdfs:label "dissolved" ; iop:constrains <${OXYGEN}> .
`;

async function parse(turtle = TTL): Promise<Variable> {
  const [first] = await extract(turtle);
  return first;
}

/** The changes one edit makes, as the workspace records them: snapshots before and after. */
async function changesOf(edit: (variable: Variable) => void): Promise<RecordChange[]> {
  const variable = await parse();
  const before = snapshotComponents(variable);
  edit(variable);
  return diffComponents(before, snapshotComponents(variable));
}

describe('component snapshots', () => {
  test('list every component with its I-ADOPT role, and what each constraint narrows', async () => {
    const states = snapshotComponents(await parse());
    expect(states.map((state) => [state.group, state.role, state.label])).toEqual([
      ['variable', 'variable', 'Daily mean dissolved oxygen'],
      ['property', 'hasProperty', 'mass concentration'],
      ['ooi', 'hasObjectOfInterest', 'oxygen'],
      ['matrix', 'hasMatrix', 'sea water'],
      ['constraint', 'hasConstraint', 'dissolved'],
    ]);
    const constraint = states[4];
    expect(constraint.iri).toBeNull();
    expect(constraint.narrows.map((position) => states[position].label)).toEqual(['oxygen']);
    expect(states[3].shape).toBe('single');
  });
});

describe('the changes of one applied step', () => {
  test('an inspector save that renames and relinks a component lists both, on its role', async () => {
    const changes = await changesOf((variable) => {
      applyEdit(variable.getMatrix() as Entity, { label: 'ocean water', iri: 'http://purl.obolibrary.org/obo/ENVO_00002149', comment: '' });
    });
    expect(changes).toEqual([
      { operation: 'relabel', role: 'hasMatrix', component: 'http://purl.obolibrary.org/obo/ENVO_00002149', before: 'sea water', after: 'ocean water' },
      { operation: 'relink', role: 'hasMatrix', component: 'http://purl.obolibrary.org/obo/ENVO_00002149', before: MATRIX, after: 'http://purl.obolibrary.org/obo/ENVO_00002149' },
    ]);
  });

  test('renaming what a constraint narrows does not count as moving the constraint', async () => {
    const changes = await changesOf((variable) => {
      applyEdit(variable.getObjectOfInterest() as Entity, { label: 'dioxygen', iri: '', comment: 'O2' });
    });
    expect(changes.map((change) => change.operation)).toEqual(['relabel', 'relink', 'other']);
    expect(changes[0]).toMatchObject({ role: 'hasObjectOfInterest', component: 'dioxygen', before: 'oxygen', after: 'dioxygen' });
    expect(changes[1]).toMatchObject({ before: OXYGEN, after: null });
    expect(changes[2]).toMatchObject({ before: null, after: 'O2' });
  });

  test('a new shape lists the change of shape and the parts it adds', async () => {
    const changes = await changesOf((variable) => {
      applyEdit(variable.getMatrix() as Entity, { label: 'sea water', iri: MATRIX, comment: '' }, { shape: 'asym', pairIndex: 1 });
    });
    expect(changes).toEqual([
      { operation: 'change_shape', role: 'hasMatrix', component: MATRIX, before: 'single', after: 'hasSource+hasTarget' },
      { operation: 'add', role: '', component: 'Component 1', after: 'Component 1' },
      { operation: 'add', role: '', component: 'Component 2', after: 'Component 2' },
    ]);
  });

  test('added components and constraints, and removed ones with what went with them', async () => {
    expect(await changesOf((variable) => {
      addToVariable(variable, 'ContextObject', { label: 'at noon', iri: '', comment: '' });
    })).toEqual([{ operation: 'add', role: 'hasContextObject', component: 'at noon', after: 'at noon' }]);

    expect(await changesOf((variable) => {
      addConstraint(variable.getMatrix() as Entity, { label: 'at 10 m depth', iri: '', comment: '' });
    })).toEqual([{ operation: 'add_constraint', role: 'hasConstraint', component: 'at 10 m depth', after: 'at 10 m depth' }]);

    const variable = await parse();
    addConstraint(variable.getMatrix() as Entity, { label: 'at 10 m depth', iri: '', comment: '' });
    const before = snapshotComponents(variable);
    deleteComponent(variable.getMatrix() as Entity);
    expect(diffComponents(before, snapshotComponents(variable))).toEqual([
      { operation: 'remove', role: 'hasMatrix', component: MATRIX, before: 'sea water' },
      { operation: 'remove', role: 'hasConstraint', component: 'at 10 m depth', before: 'at 10 m depth' },
    ]);
  });

  test('a constraint that now narrows another component has moved', async () => {
    const before = snapshotComponents(await parse());
    const after = snapshotComponents(await parse(TTL.replace(`iop:constrains <${OXYGEN}>`, `iop:constrains <${MATRIX}>`)));
    expect(diffComponents(before, after)).toEqual([
      { operation: 'move_constraint', role: 'hasConstraint', component: 'dissolved', before: OXYGEN, after: MATRIX },
    ]);
  });

  test('Turtle that changes no component, such as other prefixes, lists nothing', async () => {
    const rewritten = TTL.replace('@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .', '@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n@prefix ex: <http://example.org/> .');
    expect(diffComponents(snapshotComponents(await parse()), snapshotComponents(await parse(rewritten)))).toEqual([]);
  });

  test('a component without IRI or label is named by where it sits', () => {
    expect(componentName({ iri: null, label: '', group: 'matrix/part' })).toBe('matrix/part');
    expect(componentName({ iri: 'https://example.org/a', label: 'a', group: 'matrix' })).toBe('https://example.org/a');
  });
});

describe('the backend bounds', () => {
  test('long values are cut by characters, never inside one, and extra changes are dropped', () => {
    const long = '😀'.repeat(MAX_VALUE_CHARS + 5);
    const [first] = clampChanges([{ operation: 'relabel', role: 'hasMatrix', component: 'x'.repeat(MAX_COMPONENT_CHARS + 9), before: long, after: null }]);
    expect(Array.from(first.component ?? '')).toHaveLength(MAX_COMPONENT_CHARS);
    expect(Array.from(first.before ?? '')).toHaveLength(MAX_VALUE_CHARS);
    expect(first.before?.endsWith('😀')).toBe(true);
    expect(first.after).toBeNull();
    const many = Array.from({ length: MAX_CHANGES + 30 }, (): RecordChange => ({ operation: 'other' }));
    expect(clampChanges(many)).toHaveLength(MAX_CHANGES);
    expect(clip('abc', 5)).toBe('abc');
  });
});

describe('device and screen', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  test('are reported within the bounds the backend keeps', () => {
    vi.stubGlobal('navigator', { userAgentData: { platform: 'macOS', mobile: false }, platform: 'MacIntel', maxTouchPoints: 0 });
    vi.stubGlobal('screen', { width: 1512, height: 982 });
    vi.stubGlobal('devicePixelRatio', 2);
    expect(clientInfo()).toEqual({
      screen: { width: 1512, height: 982, pixel_ratio: 2 },
      device: { platform: 'macOS', mobile: false, touch_points: 0 },
    });
  });

  test('leave out values the backend would refuse instead of sending them', () => {
    vi.stubGlobal('navigator', { platform: 'x'.repeat(300), maxTouchPoints: 5_000 });
    vi.stubGlobal('screen', { width: Number.NaN, height: 844 });
    vi.stubGlobal('devicePixelRatio', 1_000);
    expect(clientInfo()).toEqual({ screen: { height: 844 }, device: { platform: 'x'.repeat(100) } });
    vi.stubGlobal('navigator', undefined);
    vi.stubGlobal('screen', undefined);
    expect(clientInfo()).toBeUndefined();
  });
});

describe('the record kept with the local draft', () => {
  test('is read back only when it has the shape this browser wrote', () => {
    const state = { id: '0123456789abcdef', ownerId: 7, version: 2, turtle: TTL, markedVersion: 2, markHolds: true };
    expect(readRecordState(state)).toEqual(state);
    expect(readRecordState({ ...state, markedVersion: null })).toMatchObject({ markedVersion: null, markHolds: false });
    for (const broken of [null, 'x', { ...state, id: 'not-an-id' }, { ...state, ownerId: '7' }, { ...state, turtle: 1 }]) {
      expect(readRecordState(broken)).toBeNull();
    }
  });

  test('comes back with the draft, and its mark holds only when the draft shows the marked version', () => {
    const state = { id: '0123456789abcdef', ownerId: 7, version: 2, turtle: TTL, markedVersion: 2, markHolds: true };
    expect(restoredRecord(state, `${TTL}\n`)).toEqual(state);
    // Turtle-tab edits kept in the draft but never drawn are drawn when it comes back: not the marked version.
    const edited = restoredRecord(state, TTL.replace('"sea water"', '"ocean water"'));
    expect(edited).toEqual({ ...state, markHolds: false });
    expect(markStatusOf(edited)).toBe('outdated');
    expect(restoredRecord(undefined, TTL)).toBeNull();
  });

  test('says whether the version shown is the one marked as best', () => {
    const state = { id: '0123456789abcdef', ownerId: 7, version: 2, turtle: TTL, markedVersion: null, markHolds: false };
    expect(markStatusOf(null)).toBe('none');
    expect(markStatusOf(state)).toBe('none');
    expect(markStatusOf({ ...state, markedVersion: 2, markHolds: true })).toBe('marked');
    expect(markStatusOf({ ...state, markedVersion: 1, markHolds: false })).toBe('outdated');
    // The mark covers the record's version: Turtle shown that the record does not have yet is not marked,
    // unless it is what a metadata step on its way carries.
    const marked = { ...state, markedVersion: 2, markHolds: true };
    const edited = TTL.replace('"sea water"', '"ocean water"');
    expect(markStatusOf(marked, `${TTL}\n`)).toBe('marked');
    expect(markStatusOf(marked, edited)).toBe('outdated');
    expect(markStatusOf(marked, edited, edited)).toBe('marked');
    expect(markStatusOf(marked, edited, TTL)).toBe('outdated');
    expect(sameTurtle('a .\r\n', '  a .\n')).toBe(true);
  });
});
