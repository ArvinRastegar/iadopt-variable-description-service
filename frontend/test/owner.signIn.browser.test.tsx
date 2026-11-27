import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import { toast } from 'sonner';

import { navigation } from '../src/lib/leaving';

/*
 * owner-sign-in (components/owner-sign-in.md, approved under gate G-025), section 7, on the pages: the link
 * "Owner sign-in" where only ROHub signs people in, its form and its answers, the owner's token, and the admin page
 * without "Reset password" there. The sign-in page goes elsewhere after a sign-in, which a test page cannot follow,
 * so a success is followed in the dialog. No request reaches a real API.
 */

interface Call { method: string; path: string; headers: Headers; body: string }

const OWN = ['public_tools', 'session', 'account_data', 'decompose', 'save_and_review', 'publish', 'retract_own'];
const ADMIN = ['review_applications', 'manage_users', 'view_activity', 'retract_any', 'export_research', 'manage_admins'];
const LINK = 'Owner sign-in';
const PASSWORD = 'a-long-owner-password';
const REFUSAL = 'Invalid username or password.';
const LIMITED = 'Too many requests. Try again in 42 seconds.';

let calls: Call[] = [];
let unmount: (() => void) | null = null;

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });

const allowed = (names: string[]) => Object.fromEntries(names.map((name) => [name, { allowed: true, reason: null, message: null }]));

const ownerUser = { id: 1, username: 'admin', display_name: 'Admin User', roles: ['admin', 'user'], role: 'owner', auth_provider: 'local' };
const ownerMe = (methods = 'rohub') => json({
  user: ownerUser, auth_enabled: true, capabilities: allowed([...OWN, ...ADMIN]), sign_in: 'password', csrf_token: 'owner-token',
}, 200, { 'X-IADOPT-Sign-In': methods });

/** Answer the API: `routes` first, then an anonymous visitor's answers with the header `methods`. */
function stubApi(routes: Record<string, () => Response | Promise<Response>> = {}, methods: string | null = 'rohub') {
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

beforeEach(() => {
  calls = [];
  localStorage.clear();
  sessionStorage.clear();
  vi.spyOn(navigation, 'leave').mockImplementation(() => {});
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

async function mountApp(hash = '#/') {
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

const rohubButton = () => page.getByRole('button', { name: 'Sign in with ROHub' });
const link = () => page.getByText(LINK, { exact: true });
const field = (label: string) => page.getByLabelText(label, { exact: true }).element() as HTMLInputElement;
const ownerCalls = () => calls.filter((call) => call.method === 'POST' && call.path === '/api/auth/owner-login');

async function openSignIn() {
  await page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect.element(page.getByRole('dialog', { name: 'Sign in' })).toBeVisible();
}

/** Open the owner's form, fill it and send it with its own submit button. */
async function sendOwnerForm(username = 'admin', password = PASSWORD) {
  await link().click();
  await userEvent.fill(page.getByLabelText('Username', { exact: true }), username);
  await userEvent.fill(page.getByLabelText('Password', { exact: true }), password);
  const form = field('Password').closest('form');
  expect(form).not.toBeNull();
  const submit = form?.querySelector('button[type="submit"]') as HTMLButtonElement | null;
  expect(submit).not.toBeNull();
  submit?.click();
}

// ---------------------------------------------------------------------------------------------
// AC-owner-sign-in-012
// ---------------------------------------------------------------------------------------------

const HEADERS: Array<[string, boolean]> = [['rohub', true], ['password', false], ['password, rohub', false]];

test.each(HEADERS)('the dialog shows "Owner sign-in" under the ROHub button only where the header is %j', async (header, shown) => {
  // Covers: AC-owner-sign-in-012 (the dialog: where the link shows).
  stubApi({}, header);
  await mountApp();
  await expect.poll(() => calls.some((call) => call.path === '/api/auth/me')).toBe(true);
  await openSignIn();
  if (shown) {
    await expect.element(link()).toBeVisible();
    expect(rohubButton().element().compareDocumentPosition(link().element()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(page.getByLabelText('Username', { exact: true }).query()).toBeNull(); // closed until the link is used
  } else {
    await expect.element(page.getByLabelText('Username', { exact: true })).toBeVisible(); // the page has settled
    expect(link().query()).toBeNull();
  }
});

test.each(HEADERS)('the sign-in page shows the link under the ROHub button only where the header is %j', async (header, shown) => {
  // Covers: AC-owner-sign-in-012 (the sign-in page: where the link shows).
  stubApi({}, header);
  await mountLogin();
  if (shown) {
    await expect.element(link()).toBeVisible();
    expect(rohubButton().element().compareDocumentPosition(link().element()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  } else {
    await expect.poll(() => calls.some((call) => call.path === '/api/auth/me')).toBe(true);
    if (header.includes('rohub')) await expect.element(rohubButton()).toBeVisible(); // the answer has been applied
    else await new Promise((resolve) => setTimeout(resolve, 200)); // the page looks the same before and after it
    expect(link().query()).toBeNull();
  }
});

test('the form of the dialog signs the owner in through the owner\'s route, as a password sign-in does', async () => {
  // Covers: AC-owner-sign-in-012 (a success continues as a password sign-in).
  let signedIn = false;
  stubApi({
    'GET /api/auth/me': () => (signedIn ? ownerMe() : json({ detail: 'Authentication required.' }, 401, { 'X-IADOPT-Sign-In': 'rohub' })),
    'POST /api/auth/owner-login': () => {
      signedIn = true;
      return json({ user: ownerUser, auth_enabled: true });
    },
  });
  await mountApp();
  await openSignIn();
  await sendOwnerForm();
  await expect.poll(() => ownerCalls().length).toBe(1);
  expect(JSON.parse(ownerCalls()[0].body)).toEqual({ username: 'admin', password: PASSWORD });
  expect(calls.some((call) => call.method === 'POST' && call.path === '/api/auth/login')).toBe(false);
  await expect.element(page.getByRole('dialog', { name: 'Sign in' })).not.toBeInTheDocument();
  await expect.element(page.getByRole('button', { name: /^Account menu for/ })).toBeVisible();
});

const REFUSED: Array<[string, () => Response, string]> = [
  ['a refusal', () => json({ detail: REFUSAL }, 401, { 'X-IADOPT-Sign-In': 'rohub' }), REFUSAL],
  ['the limit', () => json({ detail: LIMITED, reason: 'rate_limited' }, 429, { 'Retry-After': '42' }), LIMITED],
];

test.each(REFUSED)('after %s the dialog shows the route\'s message, keeps the username and empties the password', async (_, answer, message) => {
  // Covers: AC-owner-sign-in-012 (the dialog's refusals).
  stubApi({ 'POST /api/auth/owner-login': answer });
  await mountApp();
  await openSignIn();
  await sendOwnerForm();
  await expect.element(page.getByText(message)).toBeVisible();
  expect(field('Username').value).toBe('admin');
  expect(field('Password').value).toBe('');
  await expect.element(page.getByRole('dialog', { name: 'Sign in' })).toBeVisible();
});

test.each(REFUSED)('after %s the sign-in page shows the route\'s message, keeps the username and empties the password', async (_, answer, message) => {
  // Covers: AC-owner-sign-in-012 (the sign-in page's refusals).
  stubApi({ 'POST /api/auth/owner-login': answer });
  await mountLogin();
  await sendOwnerForm();
  await expect.element(page.getByText(message)).toBeVisible();
  expect(ownerCalls()).toHaveLength(1);
  expect(field('Username').value).toBe('admin');
  expect(field('Password').value).toBe('');
});

// ---------------------------------------------------------------------------------------------
// AC-owner-sign-in-013
// ---------------------------------------------------------------------------------------------

test('with the owner\'s session where only ROHub signs in, every change carries the token of the session', async () => {
  // Covers: AC-owner-sign-in-013 (the token).
  stubApi({
    'GET /api/auth/me': () => ownerMe(),
    'POST /api/decompose/stream': () => json({ detail: 'No decompositions left today.', reason: 'daily_limit' }, 429),
  });
  await mountApp('#/');
  await expect.element(page.getByRole('button', { name: /^Account menu for/ })).toBeVisible();
  await userEvent.fill(page.getByLabelText('Variable definition'), 'Dissolved oxygen');
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(page.getByText('No decompositions left today.').first()).toBeVisible();
  const unsafe = calls.filter((call) => call.method !== 'GET');
  expect(unsafe.length).toBeGreaterThan(0);
  expect(unsafe.every((call) => call.headers.get('X-CSRF-Token') === 'owner-token')).toBe(true);
});

test('where only ROHub signs in, the admin page offers the owner\'s actions but no "Reset password"', async () => {
  // Covers: AC-owner-sign-in-013 (the admin page; owner.adminPage.browser.test.tsx has the header naming both).
  // The admin page mounts itself once per test file, so this file mounts it here only.
  const header = 'rohub';
  const reset = false;
  const account = (id: number, username: string, role: string, extra: Record<string, unknown> = {}) => ({
    id, username, role, display_name: username, email: `${username}@example.org`,
    roles: role === 'user' ? ['user'] : ['admin', 'user'], is_active: true, blocked: false, auth_provider: 'local',
    external_subject: username, email_verified: null, local: true, identities: [], approval: 'not_needed',
    created_at: '2026-10-01T09:00:00+00:00', last_login_at: null, ...extra,
  });
  const users = [account(1, 'admin', 'owner'), account(2, 'carol', 'admin'), account(4, 'bob', 'user')];
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    calls.push({ method: options.method ?? 'GET', path, headers: new Headers(options.headers), body: '' });
    if (path === '/api/auth/me') return ownerMe(header);
    if (path === '/api/admin/stats') {
      return json({ auth_enabled: true, total_users: 3, active_users: 3, active_usernames_30d: [], event_count_30d: 0, failures_30d: 0,
        average_latency_ms_30d: 0, events_by_action_30d: {}, model_usage_30d: {}, readiness: { status: 'ready', checks: {} } });
    }
    if (path === '/api/admin/users') return json({ users });
    if (path === '/api/admin/audit') return json({ events: [] });
    throw new Error(`Unexpected API request: ${path}`);
  }));
  const root = document.createElement('div');
  root.id = 'root';
  document.body.append(root);
  await import('../src/admin/main');
  const rowText = (username: string) => ([...document.querySelectorAll('tbody tr')] as HTMLElement[])
    .find((row) => [...row.querySelectorAll('*')].some((element) => element.childElementCount === 0 && element.textContent?.trim() === username))
    ?.textContent ?? '';
  await expect.poll(() => rowText('bob')).toContain('Make admin');
  expect(rowText('carol')).toContain('Remove admin');
  expect(rowText('bob').includes('Reset password')).toBe(reset);
  root.remove();
});
