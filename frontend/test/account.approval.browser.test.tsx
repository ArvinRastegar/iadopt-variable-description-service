import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import { toast } from 'sonner';

/*
 * Approving accounts that an identity provider signs in (the owner, 2026-11-25). The main page tells a
 * waiting account so, in its menu and where its work is refused; the admin page shows the waiting
 * accounts first with their number, says by whom and when an account was approved, and offers Approve
 * and Withdraw approval only where the rules allow them. The backend decides everything; no request
 * reaches a real API.
 */

const MESSAGE = 'An admin must approve your account first.';
const ISSUER = 'https://login.rohub.example.org/auth/realms/rohub';
const OWN = ['public_tools', 'session', 'account_data', 'decompose', 'save_and_review', 'publish', 'retract_own'];
const ADMIN = ['review_applications', 'manage_users', 'view_activity', 'retract_any', 'export_research'];

type Entry = { allowed: boolean; reason: string | null; message: string | null; uses_left_today?: number | null; resets_at?: string | null };
type Role = 'owner' | 'admin' | 'user';
interface Call { method: string; path: string; body: Record<string, unknown> | null }

const YES: Entry = { allowed: true, reason: null, message: null };
const no = (reason: string, message: string): Entry => ({ allowed: false, reason, message });

/** What the backend reports; `refused` are the own uses it refuses. */
function capabilities(role: Role, refused: Record<string, Entry> = {}): Record<string, Entry> {
  const entries: Record<string, Entry> = {};
  for (const key of OWN) entries[key] = refused[key] ?? YES;
  entries.decompose = { uses_left_today: null, resets_at: null, ...entries.decompose };
  for (const key of ADMIN) entries[key] = refused[key] ?? (role === 'user' ? no('admin_required', 'Admin access required.') : YES);
  entries.manage_admins = role === 'owner' ? YES : no('owner_required', 'Only the owner can do this.');
  return entries;
}

const ASK = no('approval_required', 'Ask an admin to retract this publication.');
const WAITING = { ...Object.fromEntries(['decompose', 'save_and_review', 'publish'].map((name) => [name, no('approval_required', MESSAGE)])), retract_own: ASK };
/** While the daily limit is switched on, an account that is not approved works, without publishing. */
const LIMITED = {
  decompose: { ...YES, uses_left_today: 2, resets_at: '2026-11-26T00:00:00+01:00' } as Entry,
  publish: no('approval_required', 'Publishing needs an approved account.'),
  retract_own: ASK,
};

const USED_UP: Entry = { ...no('daily_limit', 'No decompositions left today.'), uses_left_today: 0, resets_at: '2026-11-26T00:00:00+01:00' };

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

let calls: Call[] = [];
let unmount: (() => void) | null = null;

/** Answer the API; `routes` come first. */
function stubApi(routes: Record<string, () => Response>) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    const method = options.method ?? 'GET';
    calls.push({ method, path, body: typeof options.body === 'string' ? JSON.parse(options.body) : null });
    const route = routes[`${method} ${path}`];
    if (route) return route();
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
});

afterEach(() => {
  toast.dismiss();
  unmount?.();
  unmount = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------------------------
// The main page
// ---------------------------------------------------------------------------------------------

const session = (entries: Record<string, Entry>) => ({
  user: { id: 7, username: 'ada', display_name: 'Ada Example', roles: ['user'], role: 'user', auth_provider: 'oidc' },
  auth_enabled: true, capabilities: entries, sign_in: 'rohub', csrf_token: 'token-1',
});
const menu = () => page.getByRole('button', { name: /^Account menu for/ });
const events = () => calls.filter((call) => call.path === '/api/events').map((call) => call.body);

/** The record routes; they refuse an account that is not allowed to save its work. */
function records(entries: () => Record<string, Entry> = () => capabilities('user')): Record<string, () => Response> {
  let version = 0;
  const refusal = () => {
    const { allowed, message, reason } = entries().save_and_review;
    return allowed ? null : json({ detail: message, reason }, 403);
  };
  return {
    'POST /api/records': () => refusal() ?? json({ record_id: 'record-1', version: 0 }, 201),
    'POST /api/records/record-1/events': () => refusal() ?? json({ record_id: 'record-1', event: (version += 1), version: 0 }, 201),
  };
}

async function mountApp(hash = '#/workspace') {
  unmount?.();
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
  await expect.element(menu()).toBeVisible();
}

async function menuSaysWaiting(): Promise<boolean> {
  await menu().click();
  await expect.element(page.getByRole('menuitem', { name: 'Sign out', exact: true })).toBeVisible();
  const shown = page.getByRole('menu').getByText('Waiting for approval').query() !== null;
  await userEvent.keyboard('{Escape}');
  await expect.poll(() => page.getByRole('menu').query()).toBeNull();
  return shown;
}

test('the account menu says that an account waits, and only then', async () => {
  let entries: Record<string, Entry> = {};
  stubApi({ 'GET /api/auth/me': () => json(session(entries)) });
  // Waiting, also while the daily limit lets it decompose and when its day is used up.
  for (const waiting of [WAITING, LIMITED, { ...LIMITED, decompose: USED_UP }]) {
    entries = capabilities('user', waiting);
    await mountApp();
    expect(await menuSaysWaiting()).toBe(true);
  }
  // Approved, an admin, and an account whose work another rule refuses.
  const refused = Object.fromEntries(Object.keys(WAITING).map((name) => [name, no('email_not_verified', 'Verify your email address to use this account.')]));
  for (const other of [capabilities('user'), capabilities('admin'), capabilities('user', refused), {}]) {
    entries = other;
    await mountApp();
    expect(await menuSaysWaiting()).toBe(false);
  }
});

test('a waiting account reads why its work is refused, and nothing of it is recorded', async () => {
  stubApi({
    'GET /api/auth/me': () => json(session(capabilities('user', WAITING))),
    'POST /api/decompose/stream': () => json({ detail: MESSAGE, reason: 'approval_required' }, 403),
  });
  await mountApp('#/');
  await userEvent.fill(page.getByLabelText('Variable definition'), 'Dissolved oxygen in water');
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(page.getByText(MESSAGE).first()).toBeVisible();

  await mountApp('#/workspace');
  await page.getByRole('button', { name: 'Paste Turtle' }).click();
  await expect.element(page.getByText(/It is drawn in your browser; nothing is sent\./)).toBeVisible();
  await userEvent.keyboard('{Escape}');
  await page.getByRole('button', { name: 'Open the example' }).click();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
  await page.getByRole('button', { name: 'Mark as best' }).first().click();
  await expect.element(page.getByRole('dialog').getByText(MESSAGE)).toBeVisible();
  await expect.element(page.getByRole('dialog').getByRole('button', { name: 'Mark as best' })).toBeDisabled();
  await userEvent.keyboard('{Escape}');
  await page.getByRole('button', { name: 'Publish', exact: true }).click();
  await expect.element(page.getByRole('dialog').getByText(MESSAGE)).toBeVisible();
  await expect.element(page.getByRole('dialog').getByRole('button', { name: 'Continue' })).toBeDisabled();
  expect(calls.filter((call) => call.path.startsWith('/api/records') || call.path.startsWith('/api/nanopub/p') && call.method === 'POST')).toEqual([]);
  expect(document.body.textContent).not.toMatch(/accept the (current )?terms/i);
  // What the page says is sent is all that is sent: what was done, and nothing of the work.
  expect(events().length).toBeGreaterThan(0);
  expect(new Set(events().map((event) => JSON.stringify(Object.keys(event ?? {}).sort()) + JSON.stringify([event?.payload, event?.metadata])))).toEqual(new Set(['["action","metadata","payload"][{},{}]']));
});

test('an account that may not save its work reads so when it publishes, whatever else it may', async () => {
  const message = 'Verify your email address to use this account.';
  stubApi({ 'GET /api/auth/me': () => json(session(capabilities('user', { save_and_review: no('email_not_verified', message) }))) });
  await mountApp();
  await page.getByRole('button', { name: 'Open the example' }).click();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
  await page.getByRole('button', { name: 'Publish', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Publish as a nanopublication' });
  await expect.element(dialog.getByText('This account cannot publish yet')).toBeVisible();
  await expect.element(dialog.getByText(message)).toBeVisible();
  await expect.element(dialog.getByRole('button', { name: 'Continue' })).toBeDisabled();
});

test('a withdrawn approval shows at the first refusal, also of a step that nobody asked for', async () => {
  let entries = capabilities('user');
  stubApi({ 'GET /api/auth/me': () => json(session(entries)), ...records(() => entries) });
  await mountApp();
  await page.getByRole('button', { name: 'Open the example' }).click();
  await expect.element(page.getByText('some example variable').first()).toBeVisible();
  await expect.element(page.getByText('Checks are kept on this device and recorded with your account.')).toBeVisible();
  await expect.poll(() => calls.filter((call) => call.path === '/api/records').length).toBe(1);
  entries = capabilities('user', WAITING);
  await page.getByRole('button', { name: /Property/ }).first().click();
  await page.getByRole('button', { name: 'Looks right' }).click();
  await expect.element(page.getByText('1 of 10 checked')).toBeVisible();
  await expect.poll(() => page.getByText(/recorded with your account/).query()).toBeNull();
  await expect.element(page.getByText(/Checks are kept on this device\./)).toBeVisible();
  expect(await menuSaysWaiting()).toBe(true);
  // Nothing more of the work is sent.
  const sent = calls.filter((call) => call.path.startsWith('/api/records')).length;
  expect(sent).toBe(2);
  await page.getByRole('button', { name: /Object of Interest/ }).first().click();
  await page.getByRole('button', { name: 'Looks right' }).click();
  await expect.element(page.getByText('2 of 10 checked')).toBeVisible();
  expect(calls.filter((call) => call.path.startsWith('/api/records')).length).toBe(sent);
});

test('an approval shows when the person comes back to the tab, and a failure to ask changes nothing', async () => {
  let answer = () => json(session(capabilities('user', WAITING)));
  stubApi({ 'GET /api/auth/me': () => answer() });
  await mountApp();
  const asked = () => calls.filter((call) => call.path === '/api/auth/me').length;
  const before = asked();
  for (const failure of [() => json({ detail: 'Unavailable.' }, 503), () => { throw new Error('offline'); }]) {
    answer = failure;
    const count = asked();
    window.dispatchEvent(new Event('focus'));
    await expect.poll(asked).toBeGreaterThan(count);
    await new Promise((resolve) => setTimeout(resolve, 100));
    await expect.element(menu()).toBeVisible();
  }
  expect(await menuSaysWaiting()).toBe(true);
  answer = () => json(session(capabilities('user')));
  const count = asked();
  document.dispatchEvent(new Event('visibilitychange'));
  await expect.poll(asked).toBeGreaterThan(count);
  await expect.poll(async () => await menuSaysWaiting()).toBe(false);
  // Nobody else is asked again for coming back.
  const last = asked();
  window.dispatchEvent(new Event('focus'));
  document.dispatchEvent(new Event('visibilitychange'));
  await new Promise((resolve) => setTimeout(resolve, 200));
  expect([asked(), before < last]).toEqual([last, true]);
});

test('coming back to a tab whose browser has another session shows that session as a whole', async () => {
  const ada = session(capabilities('user', WAITING));
  const ben = { ...session(capabilities('admin')), user: { ...ada.user, id: 8, username: 'ben', display_name: 'Ben Example', role: 'admin' }, csrf_token: 'token-2' };
  let answer = () => json(ada);
  stubApi({ 'GET /api/auth/me': () => answer(), ...records() });
  await mountApp();
  expect(await menuSaysWaiting()).toBe(true);
  // Somebody else signed in in another tab.
  answer = () => json(ben);
  window.dispatchEvent(new Event('focus'));
  await expect.element(page.getByRole('button', { name: 'Account menu for Ben Example' })).toBeVisible();
  expect(await menuSaysWaiting()).toBe(false);
  // And somebody signed out.
  answer = () => json(ada);
  await mountApp();
  answer = () => json({ detail: 'Authentication required.' }, 401);
  window.dispatchEvent(new Event('focus'));
  await expect.element(page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
});

test('an approval shows at the next action, without a reload', async () => {
  let entries = capabilities('user', WAITING);
  stubApi({
    'GET /api/auth/me': () => json(session(entries)),
    'POST /api/decompose/stream': () => json({ detail: 'The model was asked.' }, 500),
  });
  await mountApp('#/');
  expect(await menuSaysWaiting()).toBe(true);
  entries = capabilities('user');
  await userEvent.fill(page.getByLabelText('Variable definition'), 'Dissolved oxygen in water');
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(page.getByText('The model was asked.').first()).toBeVisible();
  expect(await menuSaysWaiting()).toBe(false);
});

// ---------------------------------------------------------------------------------------------
// The admin page
// ---------------------------------------------------------------------------------------------

type Account = Record<string, unknown> & { id: number; username: string; role: Role };

function account(id: number, username: string, role: Role, extra: Record<string, unknown> = {}): Account {
  return {
    id, username, role, display_name: username[0].toUpperCase() + username.slice(1), email: `${username}@example.org`,
    roles: role === 'user' ? ['user'] : ['admin', 'user'], is_active: true, blocked: false, auth_provider: 'oidc', external_subject: username,
    email_verified: true, local: false, identities: [{ issuer: ISSUER, subject: username, last_login_at: null }],
    created_at: '2026-11-25T09:00:00+00:00', last_login_at: '2026-11-25T09:30:00+00:00',
    approval: 'waiting', approved_at: null, approved_by: null, ...extra,
  };
}

const NOT_NEEDED = { approval: 'not_needed' };
const LOCAL = { ...NOT_NEEDED, local: true, identities: [], auth_provider: 'local', email_verified: null };
const APPROVED = { approval: 'approved', approved_at: '2026-11-25T10:00:00+00:00', approved_by: 'carol' };

const olivia = account(1, 'olivia', 'owner', LOCAL);
const carol = account(2, 'carol', 'admin', NOT_NEEDED);
const dave = account(3, 'dave', 'admin', APPROVED); // an admin somebody approved before
const bob = account(4, 'bob', 'user', LOCAL);
const pat = account(5, 'pat', 'user', APPROVED);
const anna = account(6, 'anna', 'user', { created_at: '2026-11-25T12:00:00+00:00' });
const zoe = account(7, 'zoe', 'user', { created_at: '2026-11-24T08:00:00+00:00' });
const gone = account(8, 'gone', 'user', { blocked: true, is_active: false });
const erin = account(9, 'erin', 'admin', { ...NOT_NEEDED, blocked: true, is_active: false });

const STATS = {
  auth_enabled: true, total_users: 8, active_users: 7, active_usernames_30d: [], event_count_30d: 0, failures_30d: 0,
  average_latency_ms_30d: 0, events_by_action_30d: {}, model_usage_30d: {}, readiness: { status: 'ready', checks: { schema_exists: true } },
};
const NAMES = ['Approve', 'Withdraw approval', 'Block', 'Unblock', 'Make admin', 'Remove admin', 'Edit', 'Reset password', 'History'];

/** The actions the page offers on an account, from the buttons' accessible names. */
function actionsOn(username: string): string[] {
  return [...document.querySelectorAll('button')].map((button) => button.getAttribute('aria-label') ?? '')
    .filter((label) => label.endsWith(` ${username}`)).map((label) => label.slice(0, -username.length - 1)).filter((name) => NAMES.includes(name)).sort();
}

const waitingNames = () => [...document.querySelectorAll('#waiting-for-approval li')].map((item) => item.querySelector('.font-mono')?.textContent);
const rowOf = (username: string) => [...document.querySelectorAll('tbody tr')].find((row) => row.querySelector('.font-mono')?.textContent === username);
const press = (name: string) => page.getByRole('button', { name, exact: true }).click();

test('the admin page shows who waits first, approves with a note and withdraws', async () => {
  let viewer: Account = carol;
  let refused: Record<string, Entry> = {};
  let users = [anna, bob, carol, dave, olivia, pat, zoe, erin, gone];
  let refusal: Response | null = null;
  const change = (id: number, to: Record<string, unknown>) => () => {
    if (refusal) return refusal;
    users = users.map((user) => (user.id === id ? { ...user, ...to } : user));
    return json({ user: users.find((user) => user.id === id) });
  };
  stubApi({
    'GET /api/auth/me': () => json({ user: viewer, auth_enabled: true, capabilities: capabilities(viewer.role, refused) }),
    'GET /api/admin/stats': () => json(STATS),
    'GET /api/admin/audit': () => json({ events: [] }),
    'GET /api/admin/users': () => json({ users }),
    'POST /api/admin/users/6/approve': change(6, { ...APPROVED, approved_at: '2026-11-25T13:00:00+00:00' }),
    'POST /api/admin/users/7/approve': change(7, APPROVED),
    'POST /api/admin/users/5/withdraw-approval': change(5, { approval: 'waiting', approved_at: null, approved_by: null }),
    'GET /api/admin/users/6/history': () => json({ history: [
      { action: 'approval_withdrawn', actor: 'olivia', reason: 'Asked to leave.', details: {}, created_at: '2026-11-25T14:00:00+00:00' },
      { action: 'approved', actor: 'carol', reason: 'accepted the terms by email on 25 November', details: {}, created_at: '2026-11-25T13:00:00+00:00' },
    ] }),
  });
  const posts = (path: string) => calls.filter((call) => call.method === 'POST' && call.path === `/api/admin/users/${path}`).map((call) => call.body);
  const root = document.createElement('div');
  root.id = 'root';
  document.body.append(root);
  await import('../src/admin/main');

  // Who waits comes first, the oldest account on top, with the number; a blocked account does not wait.
  await expect.element(page.getByRole('heading', { name: 'Waiting for approval (2)' })).toBeVisible();
  await expect.element(page.getByText('2 accounts wait for approval')).toBeVisible();
  expect(page.getByRole('link', { name: 'Go to them' }).query()?.getAttribute('href')).toBe('#waiting-for-approval');
  expect(waitingNames()).toEqual(['zoe', 'anna']);
  expect(document.querySelector('#waiting-for-approval')?.compareDocumentPosition(document.querySelector('tbody') as Node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  expect(['zoe', 'anna'].map(rowOf)).toEqual([undefined, undefined]);
  expect(document.querySelector('#waiting-for-approval')?.textContent).toMatch(/zoe@example\.org.*login\.rohub\.example\.orgcreated .*2026/);
  // Where each other account stands.
  expect(rowOf('pat')?.textContent).toMatch(/Active.*Approved by carol, .*2026/);
  expect(rowOf('gone')?.textContent).toMatch(/Blocked.*Not approved/);
  for (const name of ['bob', 'carol', 'olivia']) expect(rowOf(name)?.textContent).not.toMatch(/pprove/);

  // On a phone nothing is wider than the screen, where a dialog's buttons would be too.
  for (const width of [1280, 390, 320]) {
    await page.viewport(width, 844);
    expect(document.documentElement.scrollWidth, `${width} pixels`).toBeLessThanOrEqual(width);
    for (const button of document.querySelectorAll('header a, header button, #waiting-for-approval button')) {
      const { left, right } = button.getBoundingClientRect();
      expect(left >= 0 && right <= width, `${button.textContent} at ${width} pixels`).toBe(true);
    }
  }
  expect(page.getByRole('link', { name: 'Back to the app' }).query()).not.toBeNull();
  expect(page.getByRole('button', { name: 'Sign out', exact: true }).query()).not.toBeNull();
  await page.viewport(390, 844);

  // An admin acts on ordinary accounts only.
  const asAdmin: Record<string, string[]> = {
    zoe: ['Approve', 'Block', 'History'], anna: ['Approve', 'Block', 'History'], gone: ['Approve', 'History', 'Unblock'],
    pat: ['Block', 'History', 'Withdraw approval'], bob: ['Block', 'Edit', 'History', 'Reset password'],
    carol: ['History'], dave: ['History'], erin: ['History'], olivia: ['History'],
  };
  for (const [name, expected] of Object.entries(asAdmin)) expect(actionsOn(name), `${name}, viewed by an admin`).toEqual(expected);

  // Approving takes an optional note.
  await press('Approve anna');
  const dialog = page.getByRole('dialog', { name: 'Approve anna?' });
  await expect.element(dialog).toBeVisible();
  // What an approval lets the account do, retracting other people's publications included (the owner, 2026-11-26).
  await expect.element(dialog.getByText(/decompose, save their work, publish, and retract any publication of this service/)).toBeVisible();
  await userEvent.fill(dialog.getByLabelText('Note (optional)'), '  accepted the terms by email on 25 November ');
  await dialog.getByRole('button', { name: 'Approve' }).click();
  await expect.element(page.getByRole('heading', { name: 'Waiting for approval (1)' })).toBeVisible();
  expect(posts('6/approve')).toEqual([{ reason: 'accepted the terms by email on 25 November' }]);
  expect(rowOf('anna')?.textContent).toContain('Approved by carol');
  expect(actionsOn('anna')).toEqual(['Block', 'History', 'Withdraw approval']);
  await expect.element(page.getByText('1 account waits for approval')).toBeVisible();

  // Without a note nothing is sent for it; with nobody waiting nothing says so.
  await press('Approve zoe');
  await page.getByRole('dialog').getByRole('button', { name: 'Approve' }).click();
  await expect.poll(() => document.querySelector('#waiting-for-approval')).toBeNull();
  expect(posts('7/approve')).toEqual([{}]);
  expect(page.getByText(/waits? for approval/).query()).toBeNull();

  // Withdrawing.
  await press('Withdraw approval pat');
  const withdraw = page.getByRole('dialog', { name: 'Withdraw the approval of pat?' });
  await userEvent.fill(withdraw.getByLabelText('Note (optional)'), 'Left the project.');
  await withdraw.getByRole('button', { name: 'Withdraw approval' }).click();
  await expect.element(page.getByRole('heading', { name: 'Waiting for approval (1)' })).toBeVisible();
  expect(posts('5/withdraw-approval')).toEqual([{ reason: 'Left the project.' }]);
  expect(waitingNames()).toEqual(['pat']);

  // The history says it.
  await press('History anna');
  await expect.element(page.getByText('Approval withdrawn', { exact: true })).toBeVisible();
  await expect.element(page.getByText('Approved', { exact: true })).toBeVisible();
  await expect.element(page.getByText('accepted the terms by email on 25 November')).toBeVisible();
  await userEvent.keyboard('{Escape}');

  // The owner also acts on admins, never on the own account; a refusal's detail is the error.
  viewer = olivia;
  users = [anna, bob, carol, dave, olivia, pat, zoe, erin, gone];
  await press('Refresh');
  await expect.poll(() => actionsOn('zoe')).toEqual(['Approve', 'Block', 'History', 'Make admin']);
  expect(actionsOn('dave')).toEqual(['Block', 'History', 'Remove admin', 'Withdraw approval']);
  expect(actionsOn('carol')).toEqual(['Block', 'History', 'Remove admin']);
  expect(actionsOn('olivia')).toEqual(['Edit', 'History', 'Reset password']);
  refusal = json({ detail: 'Only the owner can do this.', reason: 'owner_required' }, 403);
  await press('Approve zoe');
  await page.getByRole('dialog').getByRole('button', { name: 'Approve' }).click();
  await expect.element(page.getByRole('dialog').getByText('Only the owner can do this.')).toBeVisible();
  await userEvent.keyboard('{Escape}');
  // What each dialog says is true of that account.
  for (const [button, text] of [
    ['Approve zoe', 'It takes effect at once.'],
    ['Approve gone', 'They stay blocked until you unblock them.'],
    ['Withdraw approval pat', 'What the approval allowed ends at once.'],
    ['Withdraw approval dave', 'nothing changes as long as they are one.'],
    ['Withdraw approval pat', 'They stay signed in and their data stays.'],
    ['Remove admin carol', 'as an ordinary user, who waits for approval.'],
    ['Remove admin dave', 'They stay signed in as an ordinary user.'],
    ['Remove admin erin', 'They become an ordinary user, who waits for approval.'],
  ]) {
    await press(button);
    await expect.element(page.getByRole('dialog').getByText(text)).toBeVisible();
    await userEvent.keyboard('{Escape}');
    await expect.poll(() => page.getByRole('dialog').query()).toBeNull();
  }

  // Only who may review may approve, whatever the role says.
  refused = { review_applications: no('admin_required', 'Admin access required.') };
  await press('Refresh');
  await expect.poll(() => actionsOn('zoe')).toEqual(['Block', 'History', 'Make admin']);
  expect(actionsOn('pat')).toEqual(['Block', 'History', 'Make admin']);

  // The list may be newer than what the page knows of the viewer: still nothing on the own account or the owner's.
  refused = {};
  viewer = { ...olivia, id: 2 };
  users = [account(2, 'carol', 'user'), account(5, 'pat', 'owner', APPROVED)];
  await press('Refresh');
  await expect.poll(() => actionsOn('carol')).toEqual(['History', 'Make admin']);
  expect(actionsOn('pat')).toEqual(['History']);
  root.remove();
}, 60_000);
