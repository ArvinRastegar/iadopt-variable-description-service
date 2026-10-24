import { afterEach, beforeEach, describe, expect, test } from 'vitest';
import { Parser } from 'n3';

import { findHighlights } from '../src/lib/iadopt/highlight';
import { kindOfBoxClass } from '../src/lib/iadopt/kinds';
import { describeLink, listComponents } from '../src/lib/iadopt/model';
import { addableToVariable, addConstraint, applyEdit, canDelete, deleteComponent, shapeOf } from '../src/lib/iadopt/editing';
import { readNdjson } from '../src/lib/ndjson';
import { safeRedirect } from '../src/lib/safeRedirect';
import { markRetracted, readPublished, rememberPublished, PUBLISHED_NANOPUBS_KEY } from '../src/lib/storage';
import { normalizeModelOptions } from '../src/shared/api';
import extract from '../src/lib/extract.js';
import { Entity, Variable } from '../src/model/models.js';
import toTurtle from '../src/model/toTurtle.js';

const TTL = `@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://w3id.org/iadopt/variable/20261023T104215-42> a iop:Variable ;
  rdfs:label "Daily mean dissolved oxygen" ;
  iop:hasProperty <http://qudt.org/vocab/quantitykind/MassConcentration> ;
  iop:hasObjectOfInterest <https://www.wikidata.org/entity/Q629> ;
  iop:hasMatrix <https://w3id.org/iadopt/variable/20261023T104215-42#hasMatrix> ;
  iop:hasConstraint _:c1 .
<http://qudt.org/vocab/quantitykind/MassConcentration> a iop:Property ; rdfs:label "mass concentration" .
<https://www.wikidata.org/entity/Q629> a iop:Entity ; rdfs:label "oxygen" .
<https://w3id.org/iadopt/variable/20261023T104215-42#hasMatrix> a iop:Entity ; rdfs:label "sea water" .
_:c1 a iop:Constraint ; rdfs:label "dissolved" ; iop:constrains <https://www.wikidata.org/entity/Q629> .
`;

async function variable(): Promise<Variable> {
  const [first] = await extract(TTL);
  return first;
}

describe('findHighlights', () => {
  test('marks labels where they occur, longest first, without overlaps', () => {
    const segments = findHighlights('Daily mean mass concentration of dissolved oxygen in sea water', [
      { label: 'concentration', kind: 'prop' },
      { label: 'mass concentration', kind: 'prop' },
      { label: 'oxygen', kind: 'ooi' },
      { label: 'sea water', kind: 'mat' },
      { label: 'missing', kind: 'ctx' },
    ]);
    expect(segments.filter((segment) => segment.kind).map((segment) => segment.text)).toEqual(['mass concentration', 'oxygen', 'sea water']);
    expect(segments.map((segment) => segment.text).join('')).toBe('Daily mean mass concentration of dissolved oxygen in sea water');
  });

  test('needs word boundaries and ignores case', () => {
    expect(findHighlights('Airmass of AIR', [{ label: 'air', kind: 'ooi' }]).filter((s) => s.kind).map((s) => s.text)).toEqual(['AIR']);
  });
});

describe('readNdjson', () => {
  test('yields one value per line, including a trailing line without newline', async () => {
    const chunks = ['{"type":"raw_delta","delta":"a"}\n{"type":"raw', '_delta","delta":"b"}\n\n', '{"type":"final","data":{"ttl":"x"}}'];
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
        controller.close();
      },
    });
    const values = [];
    for await (const value of readNdjson(stream)) values.push(value);
    expect(values).toEqual([
      { type: 'raw_delta', delta: 'a' },
      { type: 'raw_delta', delta: 'b' },
      { type: 'final', data: { ttl: 'x' } },
    ]);
  });
});

describe('safeRedirect', () => {
  test('keeps same-site paths and refuses other sites', () => {
    expect(safeRedirect('/admin.html')).toBe('/admin.html');
    expect(safeRedirect('/index.html?x=1#/workspace')).toBe('/index.html?x=1#/workspace');
    expect(safeRedirect('https://example.org/')).toBe('/');
    expect(safeRedirect('//example.org/')).toBe('/');
    expect(safeRedirect('/\\example.org')).toBe('/');
    expect(safeRedirect(null)).toBe('/');
  });
});

describe('published nanopublications in storage', () => {
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    (globalThis as { window?: unknown }).window = {
      localStorage: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => store.set(key, value),
        removeItem: (key: string) => store.delete(key),
      },
    };
  });
  afterEach(() => {
    delete (globalThis as { window?: unknown }).window;
  });

  test('reads entries saved by the previous frontend and keeps retracted ones', () => {
    // The exact shape the previous frontend stored.
    store.set(PUBLISHED_NANOPUBS_KEY, JSON.stringify([
      { variableIdentifier: 'old', variableUri: '', nanopubUrl: 'https://w3id.org/np/RAold', publishedTo: '', savedAt: '2026-10-01T00:00:00Z' },
    ]));
    rememberPublished({ variableIdentifier: 'new', variableUri: '', nanopubUrl: 'https://w3id.org/np/RAnew', publishedTo: '', savedAt: '2026-10-23T00:00:00Z' });
    expect(readPublished().map((entry) => entry.variableIdentifier)).toEqual(['new', 'old']);

    markRetracted(['https://w3id.org/np/RAold'], { retractionUrl: 'https://w3id.org/np/RAretraction', retractedAt: '2026-10-23T01:00:00Z' });
    const old = readPublished().find((entry) => entry.variableIdentifier === 'old');
    expect(old?.retractionUrl).toBe('https://w3id.org/np/RAretraction');
    expect(readPublished()).toHaveLength(2);
  });

  test('keeps the list on the page complete when storage cannot be read', () => {
    const shown = [{ variableIdentifier: 'a', variableUri: '', nanopubUrl: 'https://w3id.org/np/RAa', publishedTo: '', savedAt: '2026-10-23T00:00:00Z' }];
    Object.defineProperty(globalThis.window, 'localStorage', { configurable: true, get: () => { throw new Error('blocked'); } });
    const next = rememberPublished({ variableIdentifier: 'b', variableUri: '', nanopubUrl: 'https://w3id.org/np/RAb', publishedTo: '', savedAt: '2026-10-24T00:00:00Z' }, shown);
    expect(next.map((entry) => entry.variableIdentifier)).toEqual(['b', 'a']);
    const retracted = markRetracted(['https://w3id.org/np/RAa'], { retractionUrl: 'https://w3id.org/np/RAr', retractedAt: '2026-10-24T01:00:00Z' }, next);
    expect(retracted.map((entry) => entry.retractionUrl ?? null)).toEqual([null, 'https://w3id.org/np/RAr']);
  });

  test('survives broken or foreign storage content', () => {
    store.set(PUBLISHED_NANOPUBS_KEY, '{not json');
    expect(readPublished()).toEqual([]);
    store.set(PUBLISHED_NANOPUBS_KEY, JSON.stringify([{ nope: true }, null]));
    expect(readPublished()).toEqual([]);
  });
});

describe('normalizeModelOptions', () => {
  test('keeps the backend default and falls back when the response is empty', () => {
    const options = normalizeModelOptions({
      default_model_provider: 'psnc',
      providers: { psnc: { label: 'PSNC', default_model_name: 'B', model_names: ['A', 'B', 'B'] } },
    });
    expect(options.defaultProvider).toBe('psnc');
    expect(options.providers.psnc).toEqual({ label: 'PSNC', default_model_name: 'B', model_names: ['A', 'B'] });
    expect(Object.keys(normalizeModelOptions({}).providers)).toEqual(['psnc']);
  });
});

describe('the I-ADOPT model views', () => {
  test('lists components with their kind and recognises vocabulary links', async () => {
    const parsed = await variable();
    const items = listComponents(parsed);
    expect(items.map((item) => [item.kind, item.label])).toEqual([
      ['var', 'Daily mean dissolved oxygen'],
      ['prop', 'mass concentration'],
      ['ooi', 'oxygen'],
      ['mat', 'sea water'],
      ['con', 'dissolved'],
    ]);
    expect(describeLink(parsed.getProperty())).toMatchObject({ source: 'QUDT', name: 'MassConcentration' });
    expect(describeLink(parsed.getObjectOfInterest())).toMatchObject({ source: 'Wikidata', name: 'Q629' });
    expect(describeLink(parsed.getMatrix()).source).toBe('Local');
  });

  test('maps layout box classes to kinds, including system parts', () => {
    expect(kindOfBoxClass('variable variable')).toBe('var');
    expect(kindOfBoxClass('statmod statisticalmodifier')).toBe('stat');
    expect(kindOfBoxClass('entity contextobject')).toBe('ctx');
    expect(kindOfBoxClass('system matrix')).toBe('mat');
    expect(kindOfBoxClass('entity ooi')).toBe('ooi');
  });
});

describe('editing', () => {
  test('edits fields, turns an entity into a system and back, and adds or deletes parts', async () => {
    const parsed = await variable();
    const matrix = parsed.getMatrix() as Entity;

    expect(applyEdit(parsed, { label: 'Renamed', iri: parsed.getIri() ?? '', comment: '' })).toEqual({ syncVariableLabels: true });
    expect(parsed.getLabel()).toBe('Renamed');

    applyEdit(matrix, { label: 'sea water', iri: '', comment: 'the matrix' }, { shape: 'asym', pairIndex: 1 });
    expect(matrix.isBlank()).toBe(true);
    expect(matrix.getComment()).toBe('the matrix');
    expect(shapeOf(matrix)).toEqual({ shape: 'asym', pairIndex: 1 });
    applyEdit(matrix, { label: 'sea water', iri: '', comment: '' }, { shape: 'simple', pairIndex: 0 });
    expect(matrix.isSystem()).toBe(false);

    expect(addableToVariable(parsed)).toEqual(['ContextObject', 'StatisticalModifier']);
    const constraint = addConstraint(matrix, { label: 'at 10 m depth', iri: '', comment: '' });
    expect(matrix.getConstraints()).toContain(constraint);

    expect(canDelete(parsed.getObjectOfInterest())).toBe(false);
    expect(canDelete(matrix)).toBe(true);
    deleteComponent(matrix);
    expect(parsed.getMatrix()).toBeUndefined();
    expect(parsed.getConstraints().map((item) => item.getLabel())).toEqual(['dissolved']);
  });

  test('gives same-named parts of two systems their own review keys', async () => {
    const parsed = await variable();
    applyEdit(parsed.getObjectOfInterest() as Entity, { label: 'oxygen', iri: '', comment: '' }, { shape: 'sym', pairIndex: 0 });
    applyEdit(parsed.getMatrix() as Entity, { label: 'sea water', iri: '', comment: '' }, { shape: 'sym', pairIndex: 0 });
    const keys = listComponents(parsed).map((item) => item.key);
    // Both systems got blank "Component 1" and "Component 2" parts.
    expect(keys).toHaveLength(9);
    expect(new Set(keys).size).toBe(keys.length);
    // The same Turtle gives the same keys again, so checks survive a re-parse.
    const [again] = await extract(TTL);
    expect(listComponents(again).map((item) => item.key)).toEqual(listComponents(await variable()).map((item) => item.key));
  });

  test('writes labels with quotes, backslashes and line breaks as valid Turtle', async () => {
    const parsed = await variable();
    applyEdit(parsed.getMatrix() as Entity, { label: '2" soil \\ depth', iri: '', comment: 'line one\nsaid "two"' });
    const quads: Array<{ object: { value: string } }> = new Parser().parse(toTurtle(parsed));
    const literals = quads.map((quad) => quad.object.value);
    expect(literals).toContain('2" soil \\ depth');
    expect(literals).toContain('line one\nsaid "two"');
  });
});
