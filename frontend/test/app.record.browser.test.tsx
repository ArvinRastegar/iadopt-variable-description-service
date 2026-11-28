import { afterEach, expect, test, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import { EditorView } from '@codemirror/view';
import { Parser } from 'n3';
import { toast } from 'sonner';

/*
 * The decomposition record in the workspace (D-037), driven through the real interface against a
 * fake backend. A signed-in account's work is recorded step by step: the decomposition's record is
 * kept, opened or pasted Turtle starts one, each applied change, check and uncheck is one step,
 * "Mark as best" marks the version shown, and publishing names the record and needs that mark.
 * Anonymous visitors and deployments without accounts send nothing to the record. The fake checks
 * every step with the backend's own field rules (RecordEventRequest) and keeps versions and marks
 * the way the record store does, so a step the backend would refuse fails here too. No request
 * reaches a real API, a model or the nanopublication registry.
 */

const RECORD_ID = /^[0-9a-f]{16}$/;
const ORCID = 'https://orcid.org/0000-0002-1825-0097';
const PUBLISHED = `https://w3id.org/np/RA${'d'.repeat(43)}`;
const DECOMPOSED_RECORD = 'aaaaaaaaaaaaaaaa';

const PASTED = `@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://example.org/variable/1> a iop:Variable ;
    rdfs:label "dissolved oxygen in water" ;
    iop:hasProperty <http://qudt.org/vocab/quantitykind/MassConcentration> ;
    iop:hasObjectOfInterest <https://www.wikidata.org/entity/Q629> ;
    iop:hasMatrix <https://www.wikidata.org/entity/Q283> ;
    iop:hasConstraint _:c1 .
<http://qudt.org/vocab/quantitykind/MassConcentration> a iop:Property ; rdfs:label "mass concentration" .
<https://www.wikidata.org/entity/Q629> a iop:Entity ; rdfs:label "oxygen" .
<https://www.wikidata.org/entity/Q283> a iop:Entity ; rdfs:label "water" .
_:c1 a iop:Constraint ; rdfs:label "dissolved" ; iop:constrains <https://www.wikidata.org/entity/Q629> .
`;
const DRAFT = PASTED.replace('<https://example.org/variable/1>', '<https://example.org/variable/2>');

const OPERATIONS = ['add', 'remove', 'relabel', 'relink', 'change_shape', 'add_constraint', 'move_constraint', 'confirm', 'unconfirm', 'other'];
const ROLES = ['', 'variable', 'hasProperty', 'hasObjectOfInterest', 'hasMatrix', 'hasContextObject', 'hasStatisticalModifier', 'hasConstraint'];
const STEP_TYPES = ['edit', 'metadata', 'check', 'uncheck', 'mark_best'];

interface Call {
  method: string;
  path: string;
  body: Record<string, unknown>;
}

interface Change {
  operation: string;
  role?: string;
  component?: string;
  before?: string | null;
  after?: string | null;
}

interface FakeRecord {
  turtles: string[];
  /** For each version, the version whose description it has: itself for an edit, its parent's for metadata. */
  cores: number[];
  marks: number[];
  events: string[];
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

const same = (a: string, b: string) => a.replace(/\r\n?/g, '\n').trim() === b.replace(/\r\n?/g, '\n').trim();

type Quad = { predicate: { value: string }; object: { value: string } };
/** The dct:creator and prov:wasAttributedTo of a Turtle. */
const creatorsOf = (turtle: string): string[] => new Parser().parse(turtle)
  .filter((quad: Quad) => ['http://purl.org/dc/terms/creator', 'http://www.w3.org/ns/prov#wasAttributedTo'].includes(quad.predicate.value))
  .map((quad: Quad) => quad.object.value);

/** The backend's RecordEventRequest and RecordChange rules; returns what it would refuse, or null. */
function refusal(body: Record<string, unknown>): string | null {
  const type = body.type as string;
  if (!STEP_TYPES.includes(type)) return `unknown type ${type}`;
  if (typeof body.version !== 'number' || !Number.isInteger(body.version) || body.version < 0) return 'bad version';
  if (body.occurred_at !== undefined && Number.isNaN(Date.parse(String(body.occurred_at)))) return 'bad time';
  const changes = (body.changes ?? []) as Change[];
  if (!Array.isArray(changes) || changes.length > 200) return 'bad changes';
  for (const change of changes) {
    if (!OPERATIONS.includes(change.operation)) return `bad operation ${change.operation}`;
    if (!ROLES.includes(change.role ?? '')) return `bad role ${change.role}`;
    if ((change.component ?? '').length > 500) return 'component too long';
    for (const value of [change.before, change.after]) {
      if (value !== undefined && value !== null && (typeof value !== 'string' || value.length > 2000)) return 'bad value';
    }
  }
  const withTurtle = type === 'edit' || type === 'metadata';
  if (withTurtle !== (typeof body.turtle === 'string')) return `${type} with or without turtle`;
  if (type === 'mark_best') {
    if (typeof body.unchecked_components !== 'number' || body.unchecked_components < 0) return 'mark without unchecked count';
    if (changes.length) return 'mark with changes';
    if (body.note !== undefined && (typeof body.note !== 'string' || body.note.length > 500)) return 'bad note';
  } else if (body.note !== undefined || body.unchecked_components !== undefined) {
    return 'note or count outside a mark';
  }
  const operations = new Set(changes.map((change) => change.operation));
  if (type === 'check' || type === 'uncheck') {
    const expected = type === 'check' ? 'confirm' : 'unconfirm';
    if (operations.size !== 1 || !operations.has(expected)) return `${type} without ${expected}`;
    if (changes.some((change) => !(change.role || change.component))) return `${type} without role or component`;
  } else if (operations.has('confirm') || operations.has('unconfirm')) {
    return 'confirm outside a check';
  }
  return null;
}

/** A stand-in for the backend: accounts, the record store and its publish rule, and the other routes the page calls. */
function fakeBackend({ signedIn = true, user = { id: 7, username: 'reader', display_name: 'Local reader', roles: ['user'] } as Record<string, unknown>, authEnabled = true } = {}) {
  const calls: Call[] = [];
  const refused: string[] = [];
  const records = new Map<string, FakeRecord>();
  const state = {
    signedIn,
    refuseNextPublish: false,
    /** Answer the next record step with this status instead of taking it. */
    failNextStep: null as null | { status: number; detail: string },
    /** Refuse the next metadata step, as the backend does when it changes more than metadata. */
    refuseNextMetadata: false,
    /** The Turtle the fake pipeline returns as the decomposition's draft. */
    draft: DRAFT,
  };
  let next = 1;

  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    const method = options.method || 'GET';
    const body = typeof options.body === 'string' ? JSON.parse(options.body) as Record<string, unknown> : {};
    calls.push({ method, path, body });
    const session = state.signedIn || !authEnabled;
    if (path === '/api/auth/me') return session ? json({ user, auth_enabled: authEnabled }) : json({ detail: 'Authentication required.' }, 401);
    if (path === '/api/auth/me/orcids') return json({ orcids: [] });
    if (path === '/api/model-options') {
      return json({ default_model_provider: 'psnc', providers: { psnc: { label: 'PSNC', default_model_name: 'test-model', model_names: ['test-model'] } } });
    }
    if (path === '/api/nanopub/preparation-options') {
      return json({ conforms_to_uri: 'https://w3id.org/iadopt/ont/Variable', created_with_label: 'I-ADOPT Variable Description Service' });
    }
    if (path === '/api/auth/login' && method === 'POST') {
      state.signedIn = true;
      return json({ user });
    }
    if (path === '/api/events') return json({ status: 'ok' });
    if (path === '/api/decompose/stream' && method === 'POST') {
      if (!session) return json({ detail: 'Authentication required.' }, 401);
      records.set(DECOMPOSED_RECORD, { turtles: [state.draft], cores: [0], marks: [], events: ['draft'] });
      const result = { raw_llm_output: '{}', parsed_json: {}, schema_valid: true, validation_errors: [], enriched_json: {}, ttl: state.draft, record_id: DECOMPOSED_RECORD };
      const lines = [{ type: 'raw_delta', delta: '{' }, { type: 'final', data: result }].map((line) => JSON.stringify(line)).join('\n');
      return new Response(lines, { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } });
    }
    if (path === '/api/records' && method === 'POST') {
      if (!session) return json({ detail: 'Authentication required.' }, 401);
      if (!authEnabled) return json({ detail: 'Records need accounts, and authentication is disabled.' }, 409);
      if (typeof body.turtle !== 'string' || !body.turtle.trim()) {
        refused.push('record without turtle');
        return json({ detail: 'invalid' }, 422);
      }
      const id = (next++).toString(16).padStart(16, '0');
      records.set(id, { turtles: [body.turtle], cores: [0], marks: [], events: ['import'] });
      return json({ record_id: id, version: 0 }, 201);
    }
    const step = /^\/api\/records\/([^/]+)\/events$/.exec(path);
    if (step && method === 'POST') {
      if (!session) return json({ detail: 'Authentication required.' }, 401);
      if (state.failNextStep) {
        const failure = state.failNextStep;
        state.failNextStep = null;
        return json({ detail: failure.detail }, failure.status);
      }
      if (state.refuseNextMetadata && body.type === 'metadata') {
        state.refuseNextMetadata = false;
        return json({ detail: 'A metadata step may only add or change nanopublication metadata. Record other changes as an edit.' }, 400);
      }
      const problem = refusal(body);
      if (problem) {
        refused.push(problem);
        return json({ detail: problem }, 422);
      }
      const record = records.get(step[1]);
      const version = body.version as number;
      if (!record || version >= record.turtles.length) return json({ detail: 'Record not found.' }, 404);
      let now = version;
      if (body.type === 'edit' || body.type === 'metadata') {
        if (!same(record.turtles[version], body.turtle as string)) {
          record.turtles.push(body.turtle as string);
          record.cores.push(body.type === 'metadata' ? record.cores[version] : record.turtles.length - 1);
          now = record.turtles.length - 1;
        }
      }
      if (body.type === 'mark_best') record.marks.push(version);
      record.events.push(body.type as string);
      return json({ record_id: step[1], event: record.events.length, version: now }, 201);
    }
    if (path === '/api/nanopub/publish' && method === 'POST') {
      if (!authEnabled) return json({ detail: 'Publishing needs a signed-in account, so that the version can be marked as best first.' }, 409);
      const record = records.get(String(body.record_id ?? ''));
      if (!record) return json({ detail: 'Mark this version as best before publishing.' }, 409);
      const marked = record.marks.at(-1);
      const latest = record.turtles.length - 1;
      const notMarked = marked === undefined || !same(record.turtles[latest], String(body.ttl)) || record.cores[latest] !== record.cores[marked];
      if (notMarked || state.refuseNextPublish) {
        state.refuseNextPublish = false;
        return json({ detail: 'This is not the version you marked as best. Mark this version as best before publishing.' }, 409);
      }
      record.events.push('publish');
      return json({ nanopub_url: PUBLISHED, published_to: 'test', variable_identifier: 'variable', variable_uri: 'https://w3id.org/iadopt/variable/x' });
    }
    throw new Error(`Unexpected API request: ${method} ${path}`);
  }));

  return {
    state,
    records,
    refused,
    calls,
    /** The record steps and publications, in order, as "METHOD path [type]". */
    steps: () => calls
      .filter((call) => call.path.startsWith('/api/records') || call.path === '/api/nanopub/publish')
      .map((call) => (call.path.startsWith('/api/records/') ? String(call.body.type) : call.path === '/api/records' ? 'start' : 'publish')),
    bodies: (type: string) => calls.filter((call) => call.path.startsWith('/api/records/') && call.body.type === type).map((call) => call.body),
    recordCalls: () => calls.filter((call) => call.path.startsWith('/api/records')),
  };
}

let unmount: (() => void) | null = null;

afterEach(() => {
  // Toasts live in a module-wide store: one left over would cover the next test's page.
  toast.dismiss();
  unmount?.();
  unmount = null;
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function mount({ hash = '#/', seed }: { hash?: string; seed?: () => void } = {}) {
  localStorage.clear();
  seed?.();
  window.location.hash = hash;
  // The layout engine measures text with this element, as on the real page.
  const helper = document.createElement('div');
  helper.id = 'helper';
  helper.innerHTML = '<svg><text></text></svg>';
  document.body.append(helper);
  const container = document.createElement('div');
  document.body.append(container);
  const { mountApp } = await import('../src/app/main');
  const root = mountApp(container);
  unmount = () => {
    root.unmount();
    container.remove();
    helper.remove();
  };
}

const review = () => page.getByRole('complementary', { name: 'Definition and review' });
const inspector = () => page.getByRole('complementary', { name: 'Inspector' });

async function paste(text: string) {
  await page.getByRole('button', { name: 'Paste Turtle' }).click();
  const dialog = page.getByRole('dialog', { name: 'Paste Turtle' });
  await userEvent.fill(dialog.getByLabelText('Turtle'), text);
  await dialog.getByRole('button', { name: 'Visualize' }).click();
  await expect.element(review().getByRole('button', { name: /^water/ })).toBeVisible();
}

async function relabelMatrix(label: string) {
  await review().getByRole('button', { name: /^water/ }).click();
  await userEvent.fill(inspector().getByLabelText('Label', { exact: true }), label);
  await inspector().getByRole('button', { name: 'Save changes' }).click();
  await expect.element(review().getByRole('button', { name: new RegExp(`^${label}`) })).toBeVisible();
}

async function publishWithOrcid(dialog: ReturnType<typeof page.getByRole>) {
  await dialog.getByRole('button', { name: 'Continue' }).click();
  await userEvent.fill(dialog.getByLabelText('Your ORCID iD'), '0000-0002-1825-0097');
  await dialog.getByRole('button', { name: 'Continue' }).click();
  await dialog.getByRole('checkbox', { name: /I checked the components and links/ }).click();
  await dialog.getByRole('button', { name: 'Publish nanopublication' }).click();
}

test('a signed-in account records pasted Turtle, each edit and check, the best mark and the publication', async () => {
  const api = fakeBackend();
  await mount();
  await expect.element(page.getByRole('button', { name: /Account menu for Local reader/ })).toBeVisible();

  // Pasting says that it is recorded, and starts the record with the pasted Turtle as version 0.
  await page.getByRole('button', { name: 'Paste Turtle' }).click();
  await expect.element(page.getByRole('dialog', { name: 'Paste Turtle' }).getByText(/recorded with your account/)).toBeVisible();
  await page.getByRole('dialog', { name: 'Paste Turtle' }).getByRole('button', { name: 'Cancel' }).click();
  await paste(PASTED);
  await expect.poll(api.steps).toEqual(['start']);
  const started = api.calls.find((call) => call.path === '/api/records');
  expect(started?.body.turtle).toBe(PASTED);
  expect(Date.parse(String(started?.body.occurred_at))).not.toBeNaN();
  const client = started?.body.client as { screen?: { width?: number }; device?: Record<string, unknown> };
  expect(client.screen?.width).toBeGreaterThan(0);
  expect(client.device).toBeTruthy();
  const [recordId] = [...api.records.keys()];
  expect(recordId).toMatch(RECORD_ID);

  // One inspector save is one step on version 0, with its component change.
  await relabelMatrix('sea water');
  await expect.poll(api.steps).toEqual(['start', 'edit']);
  const [edit] = api.bodies('edit');
  expect(edit.version).toBe(0);
  expect(edit.turtle).toContain('sea water');
  expect(edit.changes).toEqual([
    { operation: 'relabel', role: 'hasMatrix', component: 'https://www.wikidata.org/entity/Q283', before: 'water', after: 'sea water' },
  ]);

  // "Looks right", its Undo, and "Looks right" again, all on version 1.
  await inspector().getByRole('button', { name: 'Looks right' }).click();
  await inspector().getByRole('button', { name: 'Undo check' }).click();
  await inspector().getByRole('button', { name: 'Looks right' }).click();
  await expect.poll(api.steps).toEqual(['start', 'edit', 'check', 'uncheck', 'check']);
  const confirm = { role: 'hasMatrix', component: 'https://www.wikidata.org/entity/Q283' };
  expect(api.bodies('check').map((body) => [body.version, body.changes])).toEqual([
    [1, [{ operation: 'confirm', ...confirm }]],
    [1, [{ operation: 'confirm', ...confirm }]],
  ]);
  expect(api.bodies('uncheck').map((body) => [body.version, body.changes])).toEqual([[1, [{ operation: 'unconfirm', ...confirm }]]]);

  // Adding nanopublication metadata is a metadata step; it mints the variable's IRI.
  await page.getByRole('tab', { name: /Turtle/ }).click();
  await page.getByRole('button', { name: 'Add nanopub metadata' }).click();
  await expect.poll(api.steps).toEqual(['start', 'edit', 'check', 'uncheck', 'check', 'metadata']);
  const [metadata] = api.bodies('metadata');
  expect(metadata.version).toBe(1);
  expect(metadata.turtle).toContain('conformsTo');
  expect(metadata.changes).toContainEqual(expect.objectContaining({ operation: 'relink', role: 'variable', before: 'https://example.org/variable/1' }));

  // Mark as best warns about the components without "Looks right" but does not block, and keeps the optional note.
  await page.getByRole('button', { name: 'Mark as best', exact: true }).click();
  const markDialog = page.getByRole('dialog', { name: 'Mark this as the best decomposition?' });
  await expect.element(markDialog.getByText('4 of 5 components have no “Looks right” yet')).toBeVisible();
  const note = markDialog.getByLabelText(/To help us improve, please tell us how you changed it/);
  await userEvent.fill(note, 'The model used plain water.');
  await expect.element(markDialog.getByText('27 / 500')).toBeVisible();
  await markDialog.getByRole('button', { name: 'Mark as best' }).click();
  await expect.element(page.getByRole('button', { name: 'Marked as best · v2' })).toBeVisible();
  const [mark] = api.bodies('mark_best');
  expect(mark).toMatchObject({ type: 'mark_best', version: 2, note: 'The model used plain water.', unchecked_components: 4 });
  expect(mark.changes).toBeUndefined();

  // Publishing: the check names the mark, the creator becomes a metadata step, and the publication names the record.
  await page.getByRole('button', { name: 'Publish', exact: true }).click();
  const publishDialog = page.getByRole('dialog', { name: 'Publish as a nanopublication' });
  await expect.element(publishDialog.getByText('You marked this version as best (v2)')).toBeVisible();
  await publishWithOrcid(publishDialog);
  await expect.element(page.getByRole('dialog', { name: 'Published' }).getByRole('link', { name: PUBLISHED })).toBeVisible();

  expect(api.steps()).toEqual(['start', 'edit', 'check', 'uncheck', 'check', 'metadata', 'mark_best', 'metadata', 'publish']);
  const creator = api.bodies('metadata')[1];
  expect(creator.version).toBe(2);
  expect(creatorsOf(String(creator.turtle))).toEqual([ORCID, ORCID]);
  expect(creator.changes).toEqual([]);
  const published = api.calls.find((call) => call.path === '/api/nanopub/publish')?.body ?? {};
  expect(Object.keys(published).sort()).toEqual(['client', 'creator_orcid_id', 'record_id', 'ttl']);
  expect(published.record_id).toBe(recordId);
  expect(published.ttl).toBe(String(creator.turtle).trim());
  expect(api.records.get(recordId)?.events).toEqual(['import', 'edit', 'check', 'uncheck', 'check', 'metadata', 'mark_best', 'metadata', 'publish']);
  // Every step went to the record this paste started, and every step was one the backend accepts.
  expect(new Set(api.calls.filter((call) => call.path.startsWith('/api/records/')).map((call) => call.path))).toEqual(new Set([`/api/records/${recordId}/events`]));
  expect(api.refused).toEqual([]);
}, 60_000);

test('a decomposition keeps its record; publishing explains a refusal and asks for the best mark again', async () => {
  const api = fakeBackend();
  await mount();
  await expect.element(page.getByRole('button', { name: /Account menu for Local reader/ })).toBeVisible();

  await userEvent.fill(page.getByLabelText('Variable definition'), 'Dissolved oxygen in water');
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(review().getByRole('button', { name: /^water/ })).toBeVisible();
  const decompose = api.calls.find((call) => call.path === '/api/decompose/stream');
  expect((decompose?.body.client as { screen?: unknown }).screen).toBeTruthy();
  // The backend started this record with the draft; the browser starts none.
  expect(api.calls.some((call) => call.path === '/api/records')).toBe(false);

  // An edit in the Turtle tab is recorded when it is applied with Update diagram.
  await page.getByRole('tab', { name: /Turtle/ }).click();
  await expect.element(page.getByRole('textbox', { name: 'Turtle' })).toBeVisible();
  const editor = EditorView.findFromDOM(document.querySelector('.cm-editor') as HTMLElement);
  expect(editor).toBeTruthy();
  // Typed into the editor as a person would, then applied.
  editor?.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: DRAFT.replace('rdfs:label "oxygen"', 'rdfs:label "dioxygen"') } });
  await page.getByRole('tabpanel', { name: /Turtle/ }).getByRole('button', { name: 'Update diagram' }).click();
  await expect.poll(api.steps).toEqual(['edit']);
  const [edit] = api.bodies('edit');
  expect(edit.version).toBe(0);
  expect(edit.changes).toEqual([
    { operation: 'relabel', role: 'hasObjectOfInterest', component: 'https://www.wikidata.org/entity/Q629', before: 'oxygen', after: 'dioxygen' },
  ]);

  // The publish check needs the mark and offers it in place, after the metadata is added there.
  await page.getByRole('button', { name: 'Publish', exact: true }).click();
  const publishDialog = page.getByRole('dialog', { name: 'Publish as a nanopublication' });
  await expect.element(publishDialog.getByText('Mark this version as best first')).toBeVisible();
  await publishDialog.getByRole('button', { name: 'Add the metadata' }).click();
  await expect.element(publishDialog.getByText('Nanopublication metadata is in place')).toBeVisible();
  await expect.element(publishDialog.getByRole('button', { name: 'Continue' })).toBeDisabled();
  await publishDialog.getByRole('button', { name: 'Mark as best' }).click();
  const markDialog = page.getByRole('dialog', { name: 'Mark this as the best decomposition?' });
  await expect.element(markDialog.getByText('None of the 5 components has “Looks right” yet')).toBeVisible();
  await markDialog.getByRole('button', { name: 'Mark as best' }).click();
  await expect.element(publishDialog.getByText('You marked this version as best (v2)')).toBeVisible();

  // A refused publication is explained, and the check asks for the mark again.
  api.state.refuseNextPublish = true;
  await publishWithOrcid(publishDialog);
  await expect.element(publishDialog.getByText('This is not the version you marked as best')).toBeVisible();
  await expect.element(publishDialog.getByText('You marked an earlier version as best')).toBeVisible();
  await publishDialog.getByRole('button', { name: 'Mark as best' }).click();
  await markDialog.getByRole('button', { name: 'Mark as best' }).click();
  await expect.element(publishDialog.getByText('You marked this version as best (v3)')).toBeVisible();
  await publishWithOrcid(publishDialog);
  await expect.element(page.getByRole('dialog', { name: 'Published' })).toBeVisible();

  expect(api.steps()).toEqual(['edit', 'metadata', 'mark_best', 'metadata', 'publish', 'mark_best', 'publish']);
  expect(api.bodies('mark_best').map((body) => [body.version, body.unchecked_components, body.note])).toEqual([[2, 5, undefined], [3, 5, undefined]]);
  const publications = api.calls.filter((call) => call.path === '/api/nanopub/publish').map((call) => call.body);
  expect(publications.map((body) => body.record_id)).toEqual([DECOMPOSED_RECORD, DECOMPOSED_RECORD]);
  // The second attempt publishes the version that already carries the creator: no new step.
  expect(publications[1].ttl).toBe(publications[0].ttl);
  expect(api.records.get(DECOMPOSED_RECORD)?.events).toEqual(['draft', 'edit', 'metadata', 'mark_best', 'metadata', 'mark_best', 'publish']);
  expect(api.refused).toEqual([]);
}, 60_000);

test('anonymous visitors are never recorded, and marking as best asks them to sign in', async () => {
  const api = fakeBackend({ signedIn: false });
  await mount();
  await expect.element(page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();

  await page.getByRole('button', { name: 'Paste Turtle' }).click();
  await expect.element(page.getByRole('dialog', { name: 'Paste Turtle' }).getByText(/nothing is sent/)).toBeVisible();
  await page.getByRole('dialog', { name: 'Paste Turtle' }).getByRole('button', { name: 'Cancel' }).click();
  await paste(PASTED);
  await relabelMatrix('sea water');
  await inspector().getByRole('button', { name: 'Looks right' }).click();
  await page.getByRole('tab', { name: /Turtle/ }).click();
  await page.getByRole('button', { name: 'Add nanopub metadata' }).click();
  await expect.element(review().getByText('Checks are kept on this device.')).toBeVisible();

  await page.getByRole('button', { name: 'Mark as best', exact: true }).click();
  const signIn = page.getByRole('dialog', { name: 'Sign in' });
  await expect.element(signIn).toBeVisible();
  await signIn.getByRole('button', { name: 'Cancel' }).click();
  expect(api.recordCalls()).toEqual([]);
  expect(api.calls.some((call) => call.path === '/api/nanopub/publish')).toBe(false);
}, 60_000);

test('a deployment without accounts records nothing and explains why nothing can be published', async () => {
  const api = fakeBackend({
    authEnabled: false,
    user: { id: 0, username: 'development', display_name: 'Development User', roles: ['admin', 'user'] },
  });
  await mount();
  await expect.element(page.getByRole('button', { name: /Account menu for Development User/ })).toBeVisible();

  await paste(PASTED);
  await relabelMatrix('sea water');
  await inspector().getByRole('button', { name: 'Looks right' }).click();

  await page.getByRole('button', { name: 'Mark as best', exact: true }).click();
  const markDialog = page.getByRole('dialog', { name: 'Mark this as the best decomposition?' });
  await expect.element(markDialog.getByText(/sign-in is switched off on this deployment/)).toBeVisible();
  await expect.element(markDialog.getByRole('button', { name: 'Mark as best' })).toBeDisabled();
  await markDialog.getByRole('button', { name: 'Cancel' }).click();

  await page.getByRole('button', { name: 'Publish', exact: true }).click();
  const publishDialog = page.getByRole('dialog', { name: 'Publish as a nanopublication' });
  await expect.element(publishDialog.getByText('Publishing needs an account')).toBeVisible();
  await expect.element(publishDialog.getByRole('button', { name: 'Continue' })).toBeDisabled();
  expect(api.recordCalls()).toEqual([]);
  expect(api.calls.some((call) => call.path === '/api/nanopub/publish')).toBe(false);
}, 60_000);

test('once every component is checked, the review list asks whether this is the best version', async () => {
  const api = fakeBackend();
  await mount();
  await expect.element(page.getByRole('button', { name: /Account menu for Local reader/ })).toBeVisible();
  await paste(PASTED);
  await expect.element(review().getByText('Checks are kept on this device and recorded with your account.')).toBeVisible();

  const rows = review().getByRole('listitem');
  for (let index = 0; index < 5; index += 1) {
    await rows.nth(index).getByRole('button').click();
    await inspector().getByRole('button', { name: 'Looks right' }).click();
  }
  const prompt = review().getByText('All components are checked. Is this the best decomposition of the variable?');
  await expect.element(prompt).toBeVisible();
  await review().getByRole('button', { name: 'Mark as best' }).click();
  const markDialog = page.getByRole('dialog', { name: 'Mark this as the best decomposition?' });
  await expect.element(markDialog.getByText('You checked all 5 components.')).toBeVisible();
  await markDialog.getByRole('button', { name: 'Mark as best' }).click();
  await expect.element(page.getByRole('button', { name: 'Marked as best · v0' })).toBeVisible();
  await expect.element(prompt).not.toBeInTheDocument();

  expect(api.steps()).toEqual(['start', 'check', 'check', 'check', 'check', 'check', 'mark_best']);
  expect(api.bodies('check').map((body) => (body.changes as Change[])[0].role)).toEqual([
    'variable', 'hasProperty', 'hasObjectOfInterest', 'hasMatrix', 'hasConstraint',
  ]);
  expect(api.bodies('mark_best')).toEqual([expect.objectContaining({ version: 0, unchecked_components: 0 })]);
  expect(api.refused).toEqual([]);
}, 60_000);

test('a lost record starts again, a metadata step that changes more is an edit, and a deployment without accounts stops the record', async () => {
  const api = fakeBackend();
  await mount();
  await expect.element(page.getByRole('button', { name: /Account menu for Local reader/ })).toBeVisible();
  await paste(PASTED);
  await expect.poll(api.steps).toEqual(['start']);
  const [first] = [...api.records.keys()];

  // The record is gone (as after the database was replaced): the step starts a new record and is sent there.
  api.state.failNextStep = { status: 404, detail: 'Record not found.' };
  await relabelMatrix('sea water');
  await expect.poll(api.steps).toEqual(['start', 'edit', 'start', 'edit']);
  const second = [...api.records.keys()].find((id) => id !== first) as string;
  expect(api.records.get(second)?.turtles[0]).toBe(PASTED);
  expect(api.records.get(second)?.events).toEqual(['import', 'edit']);

  // A metadata step the backend refuses as more than metadata is sent again as an edit.
  api.state.refuseNextMetadata = true;
  await page.getByRole('tab', { name: /Turtle/ }).click();
  await page.getByRole('button', { name: 'Add nanopub metadata' }).click();
  await expect.poll(api.steps).toEqual(['start', 'edit', 'start', 'edit', 'metadata', 'edit']);
  const [metadata] = api.bodies('metadata');
  const retried = api.bodies('edit').at(-1) ?? {};
  expect(retried.turtle).toBe(metadata.turtle);
  expect(retried.version).toBe(metadata.version);
  expect(api.records.get(second)?.events).toEqual(['import', 'edit', 'edit']);

  // A 409 says the deployment has no accounts: nothing more is sent, and marking explains why.
  api.state.failNextStep = { status: 409, detail: 'Records need accounts, and authentication is disabled.' };
  await page.getByRole('tab', { name: 'Diagram' }).click();
  await review().getByRole('button', { name: /^sea water/ }).click();
  await inspector().getByRole('button', { name: 'Looks right' }).click();
  await expect.poll(api.steps).toEqual(['start', 'edit', 'start', 'edit', 'metadata', 'edit', 'check']);
  await inspector().getByRole('button', { name: 'Undo check' }).click();
  await page.getByRole('button', { name: 'Mark as best', exact: true }).click();
  const markDialog = page.getByRole('dialog', { name: 'Mark this as the best decomposition?' });
  await markDialog.getByRole('button', { name: 'Mark as best' }).click();
  await expect.element(markDialog.getByText(/sign-in is switched off on this deployment/)).toBeVisible();
  expect(api.steps()).toEqual(['start', 'edit', 'start', 'edit', 'metadata', 'edit', 'check']);
  expect(api.refused).toEqual([]);
}, 60_000);

test("a mark stops counting when a step could not be recorded, and another account's draft is not used", async () => {
  const api = fakeBackend();
  // A draft another account (id 99) left in this browser, with its record marked as best.
  const otherRecord = { id: 'bbbbbbbbbbbbbbbb', ownerId: 99, version: 0, turtle: PASTED, markedVersion: 0, markHolds: true };
  await mount({
    hash: '#/workspace',
    seed: () => localStorage.setItem('iadopt-workspace-draft', JSON.stringify({
      definition: '', rawOutput: '', validationErrors: null, schemaValid: null, turtle: PASTED, checks: {}, run: null,
      savedAt: '2026-11-17T09:00:00.000Z', record: otherRecord,
    })),
  });
  await expect.element(page.getByRole('button', { name: /Account menu for Local reader/ })).toBeVisible();
  await expect.element(review().getByRole('button', { name: /^water/ })).toBeVisible();
  await expect.element(page.getByRole('button', { name: 'Mark as best', exact: true })).toBeVisible();

  // This account's first step starts its own record from the Turtle shown.
  await review().getByRole('button', { name: /^water/ }).click();
  await inspector().getByRole('button', { name: 'Looks right' }).click();
  await expect.poll(api.steps).toEqual(['start', 'check']);
  expect(api.calls.some((call) => call.path.includes(otherRecord.id))).toBe(false);
  await page.getByRole('button', { name: 'Mark as best', exact: true }).click();
  await page.getByRole('dialog', { name: 'Mark this as the best decomposition?' }).getByRole('button', { name: 'Mark as best' }).click();
  await expect.element(page.getByRole('button', { name: 'Marked as best · v0' })).toBeVisible();

  // The session expires: the next edit is refused quietly and signs out without a dialog.
  api.state.signedIn = false;
  await relabelMatrix('sea water');
  await expect.element(page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  // Signed out, no record is the visitor's, so no mark is claimed.
  await expect.element(page.getByRole('button', { name: 'Mark as best', exact: true })).toBeVisible();

  // Signed in again, publishing asks for the mark again; marking records the missing difference first.
  await page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true }).click();
  const signIn = page.getByRole('dialog', { name: 'Sign in' });
  await userEvent.fill(signIn.getByLabelText('Username'), 'reader');
  await userEvent.fill(signIn.getByLabelText('Password'), 'local-test-password');
  await signIn.getByRole('button', { name: 'Sign in' }).click();
  await expect.element(page.getByRole('button', { name: /Account menu for Local reader/ })).toBeVisible();
  // The record did not get that edit, so the mark no longer covers what is shown.
  await expect.element(page.getByText('You marked an earlier version')).toBeVisible();
  await page.getByRole('button', { name: 'Publish', exact: true }).click();
  const publishDialog = page.getByRole('dialog', { name: 'Publish as a nanopublication' });
  await expect.element(publishDialog.getByText('You marked an earlier version as best')).toBeVisible();
  await expect.element(publishDialog.getByRole('button', { name: 'Continue' })).toBeDisabled();
  await publishDialog.getByRole('button', { name: 'Mark as best' }).click();
  await page.getByRole('dialog', { name: 'Mark this as the best decomposition?' }).getByRole('button', { name: 'Mark as best' }).click();
  await expect.element(publishDialog.getByText('You marked this version as best (v1)')).toBeVisible();

  // The refused edit (401) and the edit that caught up are both on version 0; only the second was stored.
  expect(api.steps()).toEqual(['start', 'check', 'mark_best', 'edit', 'edit', 'mark_best']);
  const [refusedEdit, caughtUp] = api.bodies('edit');
  expect([refusedEdit.version, caughtUp.version]).toEqual([0, 0]);
  expect(caughtUp.turtle).toBe(refusedEdit.turtle);
  expect(caughtUp.changes).toEqual(refusedEdit.changes);
  const [recordId] = [...api.records.keys()];
  expect(api.records.get(recordId)?.events).toEqual(['import', 'check', 'mark_best', 'edit', 'mark_best']);
  expect(api.refused).toEqual([]);
}, 60_000);

test('a shared ?ttl= link opened by a signed-in account starts a record with that Turtle', async () => {
  const api = fakeBackend();
  const url = new URL(window.location.href);
  url.searchParams.set('ttl', PASTED);
  window.history.replaceState(window.history.state, '', url.href);
  await mount({ hash: '#/workspace' });
  await expect.element(review().getByRole('button', { name: /^water/ })).toBeVisible();
  await expect.poll(api.steps).toEqual(['start']);
  expect(api.calls.find((call) => call.path === '/api/records')?.body.turtle).toBe(PASTED);
  // The page drops the parameter, so that a reload keeps later edits.
  expect(new URL(window.location.href).searchParams.has('ttl')).toBe(false);
}, 60_000);
