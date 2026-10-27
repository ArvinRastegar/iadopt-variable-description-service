import * as React from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, expect, test, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';

import { OrcidPicker, useOrcidChoice } from '../src/app/components/OrcidPicker';
import { AuthProvider } from '../src/shared/auth';

/*
 * The creator ORCID choice of the publish and retract dialogs (D-035): nothing can be chosen
 * before the saved list arrives; then the draft's own creator comes first, then the iD this
 * browser remembered, then the most recently used saved iD; an iD that is not saved is shown in
 * the typed field; another iD is saved only when asked; answers that belong to an earlier
 * opening are ignored. No request reaches a real API.
 */

const CARBERRY = 'https://orcid.org/0000-0002-1825-0097';
const HAAK = 'https://orcid.org/0000-0001-5109-3700';
const WITH_X = 'https://orcid.org/0000-0002-1694-233X';
const REMEMBERED_KEY = 'iadopt-creator-orcid';

let root: Root | null = null;
let container: HTMLElement | null = null;

afterEach(() => {
  root?.unmount();
  container?.remove();
  root = null;
  container = null;
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

function deferred() {
  let release: () => void = () => {};
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

interface Holds {
  failList?: boolean;
  holdList?: Promise<void>;
  holdPost?: Promise<void>;
  holdDelete?: Promise<void>;
}

/** A fake saved-iD API; the first iD is the most recently used one. */
function stubApi(initial: Array<{ orcid: string; name: string | null }>, holds: Holds = {}) {
  let saved = initial.map((item) => ({ ...item, created_at: '2026-10-26T10:00:00+00:00', last_used_at: '2026-10-26T10:00:00+00:00' }));
  const calls: Array<{ method: string; path: string; body?: string }> = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    const method = init.method || 'GET';
    calls.push({ method, path, body: typeof init.body === 'string' ? init.body : undefined });
    if (path === '/api/auth/me') return json({ user: { username: 'reader', roles: ['user'] } });
    if (path === '/api/auth/me/orcids' && method === 'GET') {
      await holds.holdList;
      return holds.failList ? json({ detail: 'Saved ORCID iDs need accounts, and authentication is disabled.' }, 409) : json({ orcids: saved });
    }
    if (path === '/api/auth/me/orcids' && method === 'POST') {
      await holds.holdPost;
      const orcid = JSON.parse(String(init.body)).orcid as string;
      const previous = saved.find((item) => item.orcid === orcid);
      saved = [previous ?? { orcid, name: null, created_at: 'now', last_used_at: 'now' }, ...saved.filter((item) => item.orcid !== orcid)];
      return json({ orcids: saved });
    }
    const removal = /^\/api\/auth\/me\/orcids\/([^/]+)$/.exec(path);
    if (removal && method === 'DELETE') {
      await holds.holdDelete;
      saved = saved.filter((item) => item.orcid !== `https://orcid.org/${decodeURIComponent(removal[1])}`);
      return json({ orcids: saved });
    }
    throw new Error(`Unexpected API request: ${method} ${path}`);
  }));
  return {
    posted: () => calls.filter((call) => call.method === 'POST').map((call) => JSON.parse(call.body ?? '{}').orcid),
    deleted: () => calls.filter((call) => call.method === 'DELETE').map((call) => call.path),
  };
}

function Picker({ session = 'opening', preferred, used, disabled }: { session?: string | null; preferred: string | null; used: Array<string | null>; disabled?: boolean }) {
  const choice = useOrcidChoice(session, 'Please put in your ORCID for nanopublication.', async () => preferred);
  return (
    <div>
      {session === null ? <p>Closed</p> : <OrcidPicker choice={choice} idPrefix="test" disabled={disabled} />}
      {/* "Continue" only fixes the choice; "Use" also does what a successful publication does. */}
      <button type="button" onClick={() => used.push(choice.require())}>Continue</button>
      <button
        type="button"
        onClick={() => {
          const uri = choice.require();
          used.push(uri);
          if (uri) void choice.remember(uri);
        }}
      >
        Use
      </button>
    </div>
  );
}

function mount(element: React.ReactElement) {
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  root.render(<AuthProvider>{element}</AuthProvider>);
}

/** Render new props into the same tree, so the hook keeps its state. */
function rerender(element: React.ReactElement) {
  root?.render(<AuthProvider>{element}</AuthProvider>);
}

test('without saved iDs the typed field offers the remembered iD and moves it to the account', async () => {
  localStorage.setItem(REMEMBERED_KEY, CARBERRY);
  const api = stubApi([]);
  const used: Array<string | null> = [];
  mount(<Picker preferred={null} used={used} />);

  const field = page.getByLabelText('Your ORCID iD');
  await expect.element(field).toHaveValue('0000-0002-1825-0097');
  // It was remembered on purpose, so saving it to the account is already ticked.
  await expect.element(page.getByRole('checkbox', { name: 'Save this ORCID iD to my account' })).toBeChecked();
  await page.getByRole('button', { name: 'Use' }).click();

  expect(used).toEqual([CARBERRY]);
  await expect.poll(api.posted).toEqual([CARBERRY]);
  await expect.poll(() => localStorage.getItem(REMEMBERED_KEY)).toBeNull();
  await expect.element(page.getByRole('radio', { name: /0000-0002-1825-0097/ })).toBeChecked();
});

test('saved iDs come first, the preferred one is chosen, and another iD is saved only when asked', async () => {
  const api = stubApi([{ orcid: HAAK, name: 'Laure Haak' }, { orcid: CARBERRY, name: null }]);
  const used: Array<string | null> = [];
  mount(<Picker preferred={CARBERRY} used={used} />);

  await expect.element(page.getByRole('radio', { name: /0000-0002-1825-0097/ })).toBeChecked();
  await expect.element(page.getByRole('radio', { name: /Laure Haak/ })).not.toBeChecked();
  await page.getByRole('button', { name: 'Use' }).click();
  expect(used).toEqual([CARBERRY]);
  // A saved iD that was used moves to the top of the list.
  await expect.poll(api.posted).toEqual([CARBERRY]);

  await page.getByRole('radio', { name: 'Another ORCID iD' }).click();
  const another = page.getByRole('textbox', { name: 'Another ORCID iD' });
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect.element(page.getByText('Please put in your ORCID for nanopublication.')).toBeVisible();
  await expect.element(another).toHaveFocus();
  await userEvent.fill(another, '0000-0002-1825-0098');
  await expect.element(page.getByText('The ORCID check digit is invalid.')).toBeVisible();
  await userEvent.fill(another, '0000-0002-1694-233x');
  await expect.element(page.getByText('Valid ORCID iD. The check digit matches.')).toBeVisible();
  await page.getByRole('button', { name: 'Use' }).click();
  expect(used).toEqual([CARBERRY, null, WITH_X]);
  expect(api.posted()).toEqual([CARBERRY]);

  await page.getByRole('checkbox', { name: 'Save this ORCID iD to my account' }).click();
  await page.getByRole('button', { name: 'Use' }).click();
  await expect.poll(api.posted).toEqual([CARBERRY, WITH_X]);
  await expect.element(page.getByRole('radio', { name: /0000-0002-1694-233X/ })).toBeChecked();
});

test("a draft's creator that is not saved is shown in the typed field, not replaced by a saved iD", async () => {
  localStorage.setItem(REMEMBERED_KEY, WITH_X);
  const api = stubApi([{ orcid: HAAK, name: 'Laure Haak' }]);
  const used: Array<string | null> = [];
  mount(<Picker preferred={CARBERRY} used={used} />);

  await expect.element(page.getByRole('radio', { name: 'Another ORCID iD' })).toBeChecked();
  await expect.element(page.getByRole('textbox', { name: 'Another ORCID iD' })).toHaveValue('0000-0002-1825-0097');
  await expect.element(page.getByRole('radio', { name: /Laure Haak/ })).not.toBeChecked();
  // The creator is not the remembered iD, so saving it is not ticked for the person.
  await expect.element(page.getByRole('checkbox', { name: 'Save this ORCID iD to my account' })).not.toBeChecked();
  await page.getByRole('button', { name: 'Use' }).click();

  expect(used).toEqual([CARBERRY]);
  expect(api.posted()).toEqual([]);
  // The remembered iD was neither used nor saved, so this browser keeps it.
  expect(localStorage.getItem(REMEMBERED_KEY)).toBe(WITH_X);
});

test('nothing can be chosen or used before the saved list arrives', async () => {
  localStorage.setItem(REMEMBERED_KEY, CARBERRY);
  const hold = deferred();
  stubApi([{ orcid: HAAK, name: 'Laure Haak' }], { holdList: hold.promise });
  const used: Array<string | null> = [];
  mount(<Picker preferred={null} used={used} />);

  await expect.element(page.getByText('Loading your saved ORCID iDs…')).toBeVisible();
  expect(document.querySelector('input')).toBeNull();
  await page.getByRole('button', { name: 'Continue' }).click();
  expect(used).toEqual([null]);

  hold.release();
  // The remembered iD is not saved, so it opens in the typed field, still visible.
  await expect.element(page.getByRole('radio', { name: 'Another ORCID iD' })).toBeChecked();
  await expect.element(page.getByRole('textbox', { name: 'Another ORCID iD' })).toHaveValue('0000-0002-1825-0097');
  await page.getByRole('button', { name: 'Continue' }).click();
  expect(used).toEqual([null, CARBERRY]);
});

test('a preselected saved iD leaves the typed field empty and its save box unticked', async () => {
  localStorage.setItem(REMEMBERED_KEY, CARBERRY);
  const api = stubApi([{ orcid: HAAK, name: 'Laure Haak' }, { orcid: CARBERRY, name: null }]);
  const used: Array<string | null> = [];
  mount(<Picker preferred={null} used={used} />);

  await expect.element(page.getByRole('radio', { name: /0000-0002-1825-0097/ })).toBeChecked();
  await expect.poll(() => localStorage.getItem(REMEMBERED_KEY)).toBeNull();
  // Removing the chosen iD opens the typed field empty, so it cannot come straight back.
  await page.getByRole('button', { name: 'Remove 0000-0002-1825-0097 from my account' }).click();
  const another = page.getByRole('textbox', { name: 'Another ORCID iD' });
  await expect.element(another).toHaveValue('');
  await expect.element(page.getByRole('checkbox', { name: 'Save this ORCID iD to my account' })).not.toBeChecked();
  await userEvent.fill(another, '0000-0002-1694-233X');
  await page.getByRole('button', { name: 'Use' }).click();

  expect(used).toEqual([WITH_X]);
  expect(api.posted()).toEqual([]);
});

test('an answer that belongs to an earlier opening never changes the next one', async () => {
  const hold = deferred();
  const api = stubApi([{ orcid: HAAK, name: 'Laure Haak' }, { orcid: CARBERRY, name: null }], { holdPost: hold.promise });
  const used: Array<string | null> = [];
  mount(<Picker preferred={null} used={used} />);

  await page.getByRole('radio', { name: /0000-0002-1825-0097/ }).click();
  await page.getByRole('button', { name: 'Use' }).click();
  await expect.poll(api.posted).toEqual([CARBERRY]);
  // The dialog closes and opens again with the same session key while the save still runs.
  rerender(<Picker session={null} preferred={null} used={used} />);
  await expect.element(page.getByText('Closed')).toBeVisible();
  rerender(<Picker preferred={null} used={used} />);
  await expect.element(page.getByRole('radio', { name: /Laure Haak/ })).toBeChecked();

  hold.release();
  await new Promise((resolve) => setTimeout(resolve, 50));
  await expect.element(page.getByRole('radio', { name: /Laure Haak/ })).toBeChecked();
});

test('while one removal runs, its row cannot be chosen and no other removal starts', async () => {
  const hold = deferred();
  const api = stubApi([{ orcid: HAAK, name: 'Laure Haak' }, { orcid: CARBERRY, name: null }], { holdDelete: hold.promise });
  mount(<Picker preferred={CARBERRY} used={[]} />);

  await page.getByRole('button', { name: 'Remove 0000-0001-5109-3700 from my account' }).click();
  await expect.element(page.getByRole('radio', { name: /Laure Haak/ })).toBeDisabled();
  await expect.element(page.getByRole('button', { name: 'Remove 0000-0002-1825-0097 from my account' })).toBeDisabled();
  hold.release();
  await expect.element(page.getByRole('radio', { name: /Laure Haak/ })).not.toBeInTheDocument();
  await expect.element(page.getByRole('radio', { name: /0000-0002-1825-0097/ })).toBeChecked();
  await expect.element(page.getByRole('button', { name: 'Remove 0000-0002-1825-0097 from my account' })).toBeEnabled();
  expect(api.deleted()).toEqual(['/api/auth/me/orcids/0000-0001-5109-3700']);
});

test('while a publication or retraction runs, the choice cannot change', async () => {
  stubApi([{ orcid: HAAK, name: 'Laure Haak' }]);
  mount(<Picker preferred={null} used={[]} disabled />);

  await expect.element(page.getByRole('radio', { name: /Laure Haak/ })).toBeDisabled();
  await expect.element(page.getByRole('radio', { name: 'Another ORCID iD' })).toBeDisabled();
  await expect.element(page.getByRole('button', { name: 'Remove 0000-0001-5109-3700 from my account' })).toBeDisabled();
});

test('without accounts nothing is offered for saving and the remembered iD stays in this browser', async () => {
  localStorage.setItem(REMEMBERED_KEY, CARBERRY);
  const api = stubApi([], { failList: true });
  vi.spyOn(console, 'error').mockImplementation(() => {});
  const used: Array<string | null> = [];
  mount(<Picker preferred={HAAK} used={used} />);

  await expect.element(page.getByLabelText('Your ORCID iD')).toHaveValue('0000-0001-5109-3700');
  await expect.element(page.getByRole('checkbox', { name: 'Save this ORCID iD to my account' })).not.toBeInTheDocument();
  await page.getByRole('button', { name: 'Use' }).click();

  expect(used).toEqual([HAAK]);
  expect(api.posted()).toEqual([]);
  expect(localStorage.getItem(REMEMBERED_KEY)).toBe(CARBERRY);
});
