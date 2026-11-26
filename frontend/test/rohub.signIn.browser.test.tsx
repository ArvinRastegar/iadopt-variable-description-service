import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import { toast } from 'sonner';

import { navigation } from '../src/lib/leaving';
import mergeCurrentTurtle from '../src/lib/mergeCurrentTurtle.js';

// As it is, but a test can hold one of its answers back.
vi.mock('../src/lib/mergeCurrentTurtle.js', { spy: true });

/*
 * oidc-identity (components/oidc-identity.md, approved under gate G-020), section 13, on the pages:
 * what the sign-in offers, leaving for ROHub with the work kept in this tab, the codes of a refused
 * sign-in, the CSRF token, signing out with and without clearing, and an account whose work is not
 * recorded. Leaving the page is watched, never done. No request reaches a real API.
 */

const TAB_COPY = 'iadopt-workspace-tab-copy';
const DRAFT = 'iadopt-workspace-draft';
const METHODS: Array<[string | null, boolean, boolean]> = [
  ['password', true, false], ['rohub', false, true], ['password, rohub', true, true], [null, true, false], ['something-new', true, false],
];
const OWN = ['public_tools', 'session', 'account_data', 'decompose', 'save_and_review', 'publish', 'retract_own'];

interface Call { method: string; path: string; headers: Headers; body: string }

let calls: Call[] = [];
let leave: ReturnType<typeof vi.spyOn>;
let unmount: (() => void) | null = null;

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });

function capabilities(refusedOwn: string | null = null) {
  return Object.fromEntries(OWN.map((name) => [name, refusedOwn && !['public_tools', 'session', 'account_data'].includes(name)
    ? { allowed: false, reason: 'terms_not_accepted', message: refusedOwn }
    : { allowed: true, reason: null, message: null }]));
}

const rohubSession = (extra: Record<string, unknown> = {}) => ({
  user: { id: 7, username: 'ada', display_name: 'Ada Example', roles: ['user'], role: 'user', auth_provider: 'oidc' },
  auth_enabled: true, capabilities: capabilities(), sign_in: 'rohub', csrf_token: 'token-1', ...extra,
});

/** Answer the API; `routes` come first, then an anonymous visitor's answers. */
function stubApi(routes: Record<string, () => Response | Promise<Response>> = {}, methods: string | null = 'password, rohub') {
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    const method = options.method ?? 'GET';
    calls.push({ method, path, headers: new Headers(options.headers), body: typeof options.body === 'string' ? options.body : '' });
    const route = routes[`${method} ${path}`];
    if (route) return route();
    if (path === '/api/auth/me') return json({ detail: 'Authentication required.' }, 401, methods === null ? {} : { 'X-IADOPT-Sign-In': methods });
    if (path === '/api/auth/me/orcids') return json({ orcids: [] });
    if (path === '/api/events') return json({ status: 'ok' });
    if (path === '/api/model-options') return json({ default_model_provider: 'psnc', providers: { psnc: { label: 'PSNC', default_model_name: 'm', model_names: ['m'] } } });
    if (path === '/api/nanopub/preparation-options') return json({ conforms_to_uri: 'https://w3id.org/iadopt/ont/Variable', created_with_label: 'x' });
    throw new Error(`Unexpected API request: ${method} ${path}`);
  }));
}

const unsafe = () => calls.filter((call) => call.method !== 'GET');
const signedInAs = (body: Record<string, unknown>) => () => json(body, 200, { 'X-IADOPT-Sign-In': 'password, rohub' });

beforeEach(() => {
  calls = [];
  localStorage.clear();
  sessionStorage.clear();
  leave = vi.spyOn(navigation, 'leave').mockImplementation(() => {});
});

afterEach(() => {
  toast.dismiss();
  unmount?.();
  unmount = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState(null, '', window.location.pathname);
});

async function mountApp(hash = '#/workspace') {
  window.location.hash = hash;
  const helper = document.createElement('div');
  helper.id = 'helper';
  helper.innerHTML = '<svg><text></text></svg>';
  const container = document.createElement('div');
  document.body.append(helper, container);
  const { mountApp: mount } = await import('../src/app/main');
  const root = mount(container);
  unmount = () => {
    root.unmount();
    container.remove();
    helper.remove();
  };
}

async function mountLogin(search = '') {
  window.history.replaceState(null, '', `${window.location.pathname}${search}`);
  const container = document.createElement('div');
  document.body.append(container);
  const { mountLogin: mount } = await import('../src/login/main');
  const root = mount(container);
  unmount = () => {
    root.unmount();
    container.remove();
  };
}

async function remount(hash = '#/workspace') {
  unmount?.();
  unmount = null;
  await mountApp(hash);
}

const menu = () => page.getByRole('button', { name: /^Account menu for/ });
const rohubButton = () => page.getByRole('button', { name: 'Sign in with ROHub' });

async function openSignIn() {
  await page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect.element(page.getByRole('dialog', { name: 'Sign in' })).toBeVisible();
}

// ---------------------------------------------------------------------------------------------
// AC-oidc-identity-047
// ---------------------------------------------------------------------------------------------

test.each(METHODS)('the dialog offers what the header %j says', async (header, password, rohub) => {
  // Covers: AC-oidc-identity-047 (the dialog).
  stubApi({}, header);
  await mountApp('#/');
  await expect.poll(() => calls.some((call) => call.path === '/api/auth/me')).toBe(true);
  await openSignIn();
  await expect.poll(() => [page.getByLabelText('Username').query() !== null, rohubButton().query() !== null]).toEqual([password, rohub]);
});

test.each(METHODS)('the sign-in page offers what the header %j says, also after a refusal with a session', async (header, password, rohub) => {
  // Covers: AC-oidc-identity-047 (the sign-in page).
  stubApi({}, header);
  await mountLogin();
  await expect.poll(() => [page.getByLabelText('Username').query() !== null, rohubButton().query() !== null]).toEqual([password, rohub]);
  unmount?.();
  stubApi({ 'GET /api/auth/me': () => json(rohubSession(), 200, header === null ? {} : { 'X-IADOPT-Sign-In': header }) });
  await mountLogin('?error=cancelled');
  await expect.poll(() => [page.getByLabelText('Username').query() !== null, rohubButton().query() !== null]).toEqual([password, rohub]);
  await expect.element(page.getByText('The sign-in was cancelled.')).toBeVisible();
});

test('without an answer the sign-in page offers the password form', async () => {
  // Covers: AC-oidc-identity-047 (without the header).
  stubApi({ 'GET /api/auth/me': () => Promise.reject(new Error('offline')) });
  await mountLogin();
  await expect.element(page.getByLabelText('Username')).toBeVisible();
  expect(rohubButton().query()).toBeNull();
});

test('while the answer is slow the sign-in page shows the password form, and keeps what was typed', async () => {
  // Covers: AC-oidc-identity-047 (without the header, as today).
  let answer: (response: Response) => void = () => {};
  stubApi({ 'GET /api/auth/me': () => new Promise<Response>((resolve) => { answer = resolve; }) });
  await mountLogin();
  await expect.element(page.getByLabelText('Username'), { timeout: 1000 }).toBeVisible();
  expect(rohubButton().query()).toBeNull();
  await userEvent.fill(page.getByLabelText('Username'), 'ada');
  answer(json({ detail: 'Authentication required.' }, 401, { 'X-IADOPT-Sign-In': 'password, rohub' }));
  await expect.element(rohubButton()).toBeVisible();
  await expect.element(page.getByLabelText('Username')).toHaveValue('ada');
});

// ---------------------------------------------------------------------------------------------
// AC-oidc-identity-048
// ---------------------------------------------------------------------------------------------

test('leaving for ROHub keeps the work in this tab, and coming back shows it and runs nothing', async () => {
  // Covers: AC-oidc-identity-048.
  stubApi();
  await mountApp('#/workspace');
  await page.getByRole('button', { name: 'Open the example' }).click();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
  await openSignIn();
  await rohubButton().click();
  expect(leave).toHaveBeenCalledExactlyOnceWith(`/api/auth/login?redirect=${encodeURIComponent(`${window.location.pathname}#/workspace`)}`);
  const copy = JSON.parse(sessionStorage.getItem(TAB_COPY) ?? 'null');
  expect(copy.work.turtle).toContain('some example variable');

  // Meanwhile another tab saved other work, and ROHub signed the person in.
  const other = { definition: 'Other work of another tab', rawOutput: '', validationErrors: null, schemaValid: null, turtle: '', checks: {}, run: null, savedAt: '' };
  unmount?.();
  unmount = null;
  localStorage.setItem(DRAFT, JSON.stringify(other));
  calls = [];
  stubApi({ 'GET /api/auth/me': signedInAs(rohubSession()) });
  await mountApp('#/workspace');
  await expect.element(menu()).toBeVisible();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
  expect(sessionStorage.getItem(TAB_COPY)).toBeNull();
  await page.getByRole('button', { name: 'Restore draft' }).click();
  await expect.element(page.getByText('Other work of another tab').first()).toBeVisible();
  expect(unsafe()).toEqual([]);

  // It served once: the next load shows the draft, which is saved after a moment.
  await new Promise((resolve) => setTimeout(resolve, 800));
  expect(JSON.parse(localStorage.getItem(DRAFT) ?? '{}').definition).toBe('Other work of another tab');
  await remount();
  await expect.element(page.getByText('Other work of another tab').first()).toBeVisible();
  expect(page.getByRole('button', { name: 'Restore draft' }).query()).toBeNull();
});

test('the definition and the checks come back too', async () => {
  // Covers: AC-oidc-identity-048 (definition, Turtle and checks).
  stubApi();
  await mountApp('#/workspace');
  await page.getByRole('button', { name: 'Open the example' }).click();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
  await userEvent.fill(page.getByLabelText('Variable definition'), 'Dissolved oxygen in sea water');
  await page.getByRole('button', { name: /Property/ }).first().click();
  await page.getByRole('button', { name: 'Looks right' }).click();
  await expect.element(page.getByText('1 of 10 checked')).toBeVisible();
  await openSignIn();
  await rohubButton().click();
  localStorage.clear();
  await remount();
  await expect.element(page.getByText('1 of 10 checked')).toBeVisible();
  await expect.element(page.getByText('Dissolved oxygen in sea water').first()).toBeVisible();
});

test('an old copy, or one left when the browser shows the page again, is not used', async () => {
  // Covers: AC-oidc-identity-048 (15 minutes; shown again without loading).
  const work = { definition: 'Left behind', rawOutput: '', validationErrors: null, schemaValid: null, turtle: '', checks: {}, run: null, savedAt: '' };
  stubApi();
  for (const leftAt of [Date.now() - 15 * 60_000 - 2000, Date.now() + 60_000]) {
    sessionStorage.setItem(TAB_COPY, JSON.stringify({ work, leftAt }));
    await remount();
    await expect.element(page.getByText('Your workspace is empty')).toBeVisible();
  }
  sessionStorage.setItem(TAB_COPY, JSON.stringify({ work, leftAt: Date.now() }));
  window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
  expect(sessionStorage.getItem(TAB_COPY)).toBeNull();
  sessionStorage.setItem(TAB_COPY, JSON.stringify({ work, leftAt: Date.now() - 14 * 60_000 }));
  await remount();
  await expect.element(page.getByText('Left behind').first()).toBeVisible();
});

test('with a storage that refuses the work, the page asks before it leaves, to sign in or out', async () => {
  // Covers: AC-oidc-identity-048 (a storage that refuses the work).
  const real = Storage.prototype.setItem;
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
    if (this === sessionStorage) throw new DOMException('full', 'QuotaExceededError');
    real.call(this, key, value);
  });
  stubApi();
  await mountApp('#/workspace');
  await page.getByRole('button', { name: 'Open the example' }).click();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
  await openSignIn();
  await rohubButton().click();
  await expect.element(page.getByText('Your work could not be saved')).toBeVisible();
  expect(leave).not.toHaveBeenCalled();
  await page.getByRole('button', { name: 'Stay on this page' }).click();
  expect(leave).not.toHaveBeenCalled();
  await page.getByRole('dialog', { name: 'Sign in' }).getByRole('button', { name: 'Cancel' }).click();
  await expect.poll(() => localStorage.getItem(DRAFT)).toContain('some example variable');

  stubApi({ 'GET /api/auth/me': signedInAs(rohubSession()), 'POST /api/auth/logout': () => json({ status: 'ok', end_session_url: '/api/auth/end-session' }) });
  await remount();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
  await menu().click();
  await page.getByRole('menuitem', { name: 'Sign out', exact: true }).click();
  await expect.element(page.getByText('Your work could not be saved')).toBeVisible();
  expect(leave).not.toHaveBeenCalled();
  await page.getByRole('button', { name: 'Leave without saving' }).click();
  expect(leave).toHaveBeenCalledExactlyOnceWith('/api/auth/end-session');
});

test('the sign-in page leaves with its own page to return to', async () => {
  // Covers: AC-oidc-identity-048 (the sign-in page's own target), AC-oidc-identity-008 (its part).
  stubApi();
  for (const [search, target] of [['?redirect=%2Fadmin.html%3Fx%3D1', '/admin.html?x=1'], ['?redirect=%2F.%2F%2Fevil.example', '/'], ['', '/']]) {
    unmount?.();
    await mountLogin(search);
    await rohubButton().click();
    expect(leave).toHaveBeenLastCalledWith(`/api/auth/login?redirect=${encodeURIComponent(target)}`);
  }
});

// ---------------------------------------------------------------------------------------------
// AC-oidc-identity-049
// ---------------------------------------------------------------------------------------------

const CODES: Array<[string, string]> = [
  ['cancelled', 'The sign-in was cancelled.'],
  ['sign_in_expired', 'This sign-in is no longer valid. Please start again.'],
  ['provider_unavailable', 'ROHub cannot be reached right now. Please try again later.'],
  ['provider_refused', 'ROHub did not complete the sign-in. Please start again.'],
  ['token_invalid', "ROHub's answer could not be verified, so you were not signed in."],
  ['email_not_verified', 'ROHub did not confirm your email address. Verify it, then sign in again.'],
  ['blocked', 'This account is blocked.'],
  ['rate_limited', 'Too many sign-in attempts from your network. Please wait a minute.'],
  ['sign_out_incomplete', "ROHub's sign-out page cannot be reached, so you may still be signed in there."],
  ['no_such_code', 'The sign-in did not work. Please try again.'],
  ['', 'The sign-in did not work. Please try again.'],
  ['__proto__', 'The sign-in did not work. Please try again.'],
  ['<img src=x onerror=alert(1)>', 'The sign-in did not work. Please try again.'],
];

test.each(CODES)('the code %j shows its text and the page stays, also with a session', async (code, text) => {
  // Covers: AC-oidc-identity-049.
  stubApi({ 'GET /api/auth/me': signedInAs(rohubSession()) });
  const here = window.location.pathname;
  await mountLogin(`?error=${encodeURIComponent(code)}&redirect=${encodeURIComponent('<b>x</b>')}`);
  await expect.element(page.getByText(text, { exact: false })).toBeVisible();
  await new Promise((resolve) => setTimeout(resolve, 100));
  expect(window.location.pathname).toBe(here);
  expect(document.querySelector('main img, main b')).toBeNull();
  const signOut = page.getByRole('link', { name: 'Sign out at ROHub' }).query();
  expect(signOut?.getAttribute('href') ?? null).toBe(['blocked', 'email_not_verified'].includes(code) ? '/api/auth/end-session' : null);
  expect(page.getByRole('link', { name: 'Continue without signing in' }).query()).not.toBeNull();
});

// ---------------------------------------------------------------------------------------------
// AC-oidc-identity-050
// ---------------------------------------------------------------------------------------------

test('unsafe requests carry the token, and an outdated page says so and repeats nothing', async () => {
  // Covers: AC-oidc-identity-050.
  let token = 'token-1';
  let refuse = false;
  stubApi({
    'GET /api/auth/me': () => signedInAs(rohubSession({ csrf_token: token }))(),
    'POST /api/decompose/stream': () => (refuse
      ? json({ detail: 'This page is out of date. Reload it and try again.', reason: 'csrf_failed' }, 403)
      : json({ detail: 'No decompositions left today.', reason: 'daily_limit' }, 429)),
  });
  await mountApp('#/');
  await expect.element(menu()).toBeVisible();
  await userEvent.fill(page.getByLabelText('Variable definition'), 'Dissolved oxygen');
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(page.getByText('No decompositions left today.').first()).toBeVisible();
  expect(unsafe().length).toBeGreaterThan(0);
  expect(unsafe().every((call) => call.headers.get('X-CSRF-Token') === 'token-1')).toBe(true);
  expect(calls.filter((call) => call.method === 'GET').every((call) => !call.headers.has('X-CSRF-Token'))).toBe(true);
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain('token-1');

  // The session changed in another tab: the token of this page is no longer the session's.
  refuse = true;
  token = 'token-2';
  calls = [];
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(page.getByText(/Your session changed, so nothing was done/).first()).toBeVisible();
  const streams = () => calls.filter((call) => call.path === '/api/decompose/stream');
  expect(streams()).toHaveLength(1);
  refuse = false;
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.poll(() => streams().length).toBe(2);
  expect(streams()[1].headers.get('X-CSRF-Token')).toBe('token-2');
});

test('without a token nothing is added', async () => {
  // Covers: AC-oidc-identity-050 (none otherwise).
  stubApi({
    'GET /api/auth/me': signedInAs(rohubSession({ sign_in: 'password', csrf_token: null })),
    'POST /api/decompose/stream': () => json({ detail: 'No decompositions left today.' }, 429),
  });
  await mountApp('#/');
  await expect.element(menu()).toBeVisible();
  await userEvent.fill(page.getByLabelText('Variable definition'), 'Dissolved oxygen');
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(page.getByText('No decompositions left today.').first()).toBeVisible();
  expect(unsafe().some((call) => call.headers.has('X-CSRF-Token'))).toBe(false);
});

// ---------------------------------------------------------------------------------------------
// AC-oidc-identity-051
// ---------------------------------------------------------------------------------------------

async function signedInWithWork(logout: () => Response | Promise<Response>) {
  stubApi({ 'GET /api/auth/me': signedInAs(rohubSession({ capabilities: {} })), 'POST /api/auth/logout': logout, 'POST /api/records': () => json({ record_id: '0123456789abcdef', version: 0 }, 201) });
  localStorage.setItem('iadopt-creator-orcid', 'https://orcid.org/0000-0002-1825-0097');
  localStorage.setItem('iadopt-published-nanopubs', JSON.stringify([
    { variableIdentifier: 'x', variableUri: '', nanopubUrl: 'https://w3id.org/np/RAx', publishedTo: '', savedAt: '2026-11-25T10:00:00Z', label: 'Published before' },
  ]));
  await mountApp('#/workspace');
  await expect.element(menu()).toBeVisible();
  await page.getByRole('button', { name: 'Open the example' }).click();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
  await expect.poll(() => localStorage.getItem(DRAFT)).not.toBeNull();
}

test('"Sign out" keeps the draft and goes where the answer says', async () => {
  // Covers: AC-oidc-identity-051 (the application's pages).
  let address: string | null = '/api/auth/end-session';
  await signedInWithWork(() => json({ status: 'ok', end_session_url: address }));
  await menu().click();
  await page.getByRole('menuitem', { name: 'Sign out', exact: true }).click();
  await expect.poll(() => leave.mock.calls).toEqual([['/api/auth/end-session']]);
  expect(localStorage.getItem(DRAFT)).toContain('some example variable');
  expect(sessionStorage.getItem(TAB_COPY)).toContain('some example variable');

  address = null;
  leave.mockClear();
  await remount();
  await expect.element(menu()).toBeVisible();
  await menu().click();
  await page.getByRole('menuitem', { name: 'Sign out', exact: true }).click();
  await expect.element(page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
  expect(leave).not.toHaveBeenCalled();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
});

test('"Sign out and clear this browser" asks, then leaves nothing of the work behind', async () => {
  // Covers: AC-oidc-identity-051 (the second entry).
  await signedInWithWork(() => json({ status: 'ok', end_session_url: '/api/auth/end-session' }));
  await menu().click();
  await page.getByRole('menuitem', { name: 'Sign out and clear this browser' }).click();
  const dialog = page.getByRole('dialog', { name: 'Sign out and clear this browser?' });
  await expect.element(dialog.getByText(/the draft in the workspace, the remembered ORCID iD and the list of publications/)).toBeVisible();
  await expect.element(dialog.getByText(/Other open tabs of this site keep their work/)).toBeVisible();
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  expect(unsafe().filter((call) => call.path === '/api/auth/logout')).toEqual([]);
  expect(localStorage.getItem(DRAFT)).not.toBeNull();

  await menu().click();
  await page.getByRole('menuitem', { name: 'Sign out and clear this browser' }).click();
  await dialog.getByRole('button', { name: 'Sign out and clear' }).click();
  await expect.poll(() => leave.mock.calls).toEqual([['/api/auth/end-session']]);
  const kept = () => [DRAFT, 'iadopt-creator-orcid', 'iadopt-published-nanopubs'].filter((key) => localStorage.getItem(key) !== null);
  expect(kept()).toEqual([]);
  expect(sessionStorage.length).toBe(0);
  await expect.element(page.getByText('Your workspace is empty')).toBeVisible();
  // Leaving, and showing the page again, bring nothing back.
  window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));
  window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
  await new Promise((resolve) => setTimeout(resolve, 900));
  expect(kept()).toEqual([]);
  expect(sessionStorage.length).toBe(0);
  window.location.hash = '#/publications';
  await expect.element(page.getByText('No publications yet')).toBeVisible();
});

test('"Sign out and clear this browser" leaves no message that offers the work back', async () => {
  // Covers: AC-oidc-identity-051 (nothing of the work stays on the page).
  const work = { definition: 'Work of this tab', rawOutput: '', validationErrors: null, schemaValid: null, turtle: '', checks: {}, run: null, savedAt: '' };
  sessionStorage.setItem(TAB_COPY, JSON.stringify({ work, leftAt: Date.now() }));
  localStorage.setItem(DRAFT, JSON.stringify({ ...work, definition: 'Work of another tab' }));
  stubApi({ 'GET /api/auth/me': signedInAs(rohubSession()), 'POST /api/auth/logout': () => json({ status: 'ok', end_session_url: null }) });
  await mountApp('#/workspace');
  await expect.element(menu()).toBeVisible();
  const restore = page.getByRole('button', { name: 'Restore draft' });
  await expect.element(restore).toBeVisible();
  const offered = restore.element() as HTMLElement;

  await menu().click();
  await page.getByRole('menuitem', { name: 'Sign out and clear this browser' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Sign out and clear' }).click();
  await expect.element(page.getByText('Your workspace is empty')).toBeVisible();
  // Also not while the message fades.
  offered.click();
  await expect.poll(() => restore.query()).toBeNull();
  await new Promise((resolve) => setTimeout(resolve, 900));
  await expect.element(page.getByText('Your workspace is empty')).toBeVisible();
  expect(localStorage.getItem(DRAFT)).toBeNull();
});

test('"Sign out and clear this browser" empties the publications page too', async () => {
  // Covers: AC-oidc-identity-051 (the list on the page).
  stubApi({
    'GET /api/auth/me': signedInAs(rohubSession()),
    'GET /api/auth/me/orcids': () => json({ orcids: [{ orcid: 'https://orcid.org/0000-0002-1825-0097', name: 'Ada', created_at: '', last_used_at: '' }] }),
    'POST /api/auth/me/orcids': () => json({ orcids: [] }),
    'POST /api/nanopub/retract': () => json({ retraction_url: 'https://w3id.org/np/RAretraction', published_to: '', retracted_nanopub_url: 'https://w3id.org/np/RAelsewhere' }),
    'POST /api/auth/logout': () => json({ status: 'ok', end_session_url: null }),
  });
  await mountApp('#/publications');
  await expect.element(menu()).toBeVisible();
  const reference = page.getByLabelText('Nanopublication reference to retract');
  await userEvent.fill(reference, 'https://w3id.org/np/RAelsewhere');
  await page.getByRole('button', { name: 'Retract…' }).click();
  await page.getByRole('button', { name: 'Publish retraction' }).click();
  await expect.element(page.getByRole('link', { name: 'https://w3id.org/np/RAretraction' })).toBeVisible();
  await userEvent.fill(reference, 'https://w3id.org/np/RAnext');
  await userEvent.fill(page.getByLabelText('Search publications'), 'oxygen');

  await menu().click();
  await page.getByRole('menuitem', { name: 'Sign out and clear this browser' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Sign out and clear' }).click();
  await expect.element(page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
  await expect.poll(() => page.getByText(/RAelsewhere|RAretraction/).query()).toBeNull();
  await expect.element(reference).toHaveValue('');
  await expect.element(page.getByLabelText('Search publications')).toHaveValue('');
  // Covers: AC-oidc-identity-050 (retracting, saving an ORCID iD, signing out).
  expect(new Set(unsafe().map((call) => call.path))).toEqual(new Set(['/api/nanopub/retract', '/api/auth/me/orcids', '/api/auth/logout']));
  expect(unsafe().map((call) => call.headers.get('X-CSRF-Token'))).toEqual(unsafe().map(() => 'token-1'));
});

test('a change on its way when the browser is cleared brings nothing back', async () => {
  // Covers: AC-oidc-identity-051 (nothing of the work stays on the page).
  let finish: () => void = () => {};
  const held = new Promise<void>((resolve) => { finish = resolve; });
  vi.mocked(mergeCurrentTurtle).mockImplementationOnce(async (turtle: string) => {
    await held;
    return `${turtle}\n# changed\n`;
  });
  await signedInWithWork(() => json({ status: 'ok', end_session_url: null }));
  await page.getByRole('button', { name: /Property/ }).first().click();
  await userEvent.fill(page.getByLabelText('Label', { exact: true }), 'Changed while signing out');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await menu().click();
  await page.getByRole('menuitem', { name: 'Sign out and clear this browser' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Sign out and clear' }).click();
  await expect.element(page.getByText('Your workspace is empty')).toBeVisible();
  finish();
  await new Promise((resolve) => setTimeout(resolve, 900));
  await expect.element(page.getByText('Your workspace is empty')).toBeVisible();
  expect(localStorage.getItem(DRAFT)).toBeNull();
});

test.each([
  ['an error', () => json({ detail: 'broken' }, 500)],
  ['no answer', () => Promise.reject(new TypeError('Failed to fetch'))],
])('a sign-out that fails with %s is shown and clears nothing', async (_, logout) => {
  // Covers: AC-oidc-identity-051 (a failed sign-out).
  await signedInWithWork(logout);
  for (const entry of ['Sign out', 'Sign out and clear this browser']) {
    await menu().click();
    await page.getByRole('menuitem', { name: entry, exact: true }).click();
    if (entry !== 'Sign out') await page.getByRole('dialog').getByRole('button', { name: 'Sign out and clear' }).click();
    await expect.element(page.getByText('Could not sign out. Please try again.').first()).toBeVisible();
    toast.dismiss();
    await expect.element(menu()).toBeVisible();
    await expect.element(page.getByText('some example variable').first()).toBeVisible();
    expect(localStorage.getItem(DRAFT)).toContain('some example variable');
    expect(localStorage.getItem('iadopt-creator-orcid')).not.toBeNull();
    expect(leave).not.toHaveBeenCalled();
  }
});

// ---------------------------------------------------------------------------------------------
// AC-oidc-identity-052
// ---------------------------------------------------------------------------------------------

test('an account that may not save its work is not recorded, and told why', async () => {
  // Covers: AC-oidc-identity-052.
  const message = 'Accept the current terms to continue.';
  stubApi({ 'GET /api/auth/me': signedInAs(rohubSession({ capabilities: capabilities(message) })) });
  await mountApp('#/workspace');
  await expect.element(menu()).toBeVisible();
  await page.getByRole('button', { name: 'Paste Turtle' }).click();
  await expect.element(page.getByText(/It is drawn in your browser; nothing is sent\./)).toBeVisible();
  await userEvent.keyboard('{Escape}');
  await page.getByRole('button', { name: 'Open the example' }).click();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
  await page.getByRole('button', { name: /Property/ }).first().click();
  await page.getByRole('button', { name: 'Looks right' }).click();
  await expect.element(page.getByText('1 of 10 checked')).toBeVisible();
  expect(page.getByText(/recorded with your account/).query()).toBeNull();
  await page.getByRole('button', { name: 'Mark as best' }).first().click();
  await expect.element(page.getByRole('dialog').getByText(message)).toBeVisible();
  await expect.element(page.getByRole('dialog').getByRole('button', { name: 'Mark as best' })).toBeDisabled();
  await userEvent.keyboard('{Escape}');
  await page.getByRole('button', { name: 'Publish', exact: true }).click();
  await expect.element(page.getByRole('dialog').getByText(message)).toBeVisible();
  await expect.element(page.getByRole('dialog').getByRole('button', { name: 'Continue' })).toBeDisabled();
  expect(calls.filter((call) => call.path.startsWith('/api/records'))).toEqual([]);
});

test('without that entry the work is recorded as before', async () => {
  // Covers: AC-oidc-identity-052 (today's accounts).
  await signedInWithWork(() => json({ status: 'ok', end_session_url: null }));
  await expect.poll(() => calls.filter((call) => call.path === '/api/records').length).toBe(1);
  // Covers: AC-oidc-identity-050 (the record routes).
  expect(calls.find((call) => call.path === '/api/records')?.headers.get('X-CSRF-Token')).toBe('token-1');
});
