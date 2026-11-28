import { afterEach, expect, test, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';

/*
 * The admin page's accounts: accounts-roles (components/accounts-roles.md, approved under gate
 * G-017), section 10, and access-policy section 8; increment-1 tests gate G-018. The table shows
 * role, block state, last login and the identity provider; each row offers only the actions section
 * 4 allows the viewer (buttons Block, Unblock, Make admin, Remove admin, Edit, Reset password and
 * History, the names agreed at this gate); an edit sends neither `roles` nor `is_active`; creating
 * an account offers no role; and a refusal's `detail` is shown as the error.
 * No request reaches a real API.
 */

const ISSUER = 'https://login.rohub.example.org/auth/realms/rohub';
const ACTIONS = ['Block', 'Unblock', 'Make admin', 'Remove admin', 'Edit', 'Reset password', 'History'];

type Role = 'owner' | 'admin' | 'user';
type Account = Record<string, unknown> & { id: number; username: string; role: Role };
type Entry = { allowed: boolean; reason: string | null; message: string | null; uses_left_today?: null; resets_at?: null };

function account(id: number, username: string, role: Role, extra: Record<string, unknown> = {}): Account {
  return {
    id, username, role,
    display_name: username[0].toUpperCase() + username.slice(1),
    email: `${username}@example.org`,
    roles: role === 'user' ? ['user'] : ['admin', 'user'],
    is_active: true, blocked: false, auth_provider: 'local', external_subject: username,
    email_verified: null, local: true, identities: [],
    created_at: '2026-10-01T09:00:00+00:00', last_login_at: '2026-11-20T09:30:00+00:00',
    ...extra,
  };
}

const olivia = account(1, 'olivia', 'owner');
const carol = account(2, 'carol', 'admin');
const dave = account(3, 'dave', 'admin', { last_login_at: null });
const bob = account(4, 'bob', 'user', { display_name: 'Bob Example' });
const pat = account(5, 'pat', 'user', {
  auth_provider: 'oidc', external_subject: 'pat-subject', local: false, email_verified: true,
  identities: [{ issuer: ISSUER, subject: 'pat-subject', last_login_at: '2026-11-21T08:00:00+00:00' }],
});
const erin = account(6, 'erin', 'user', { is_active: false, blocked: true });
const gina = account(7, 'gina', 'admin', { is_active: false, blocked: true });
const USERS = [bob, carol, dave, olivia, pat, erin, gina];

function capabilities(role: Role, adminAllowed = true): Record<string, Entry> {
  const entries: Record<string, Entry> = {};
  for (const key of ['public_tools', 'session', 'account_data', 'decompose', 'save_and_review', 'publish', 'retract_own']) {
    entries[key] = { allowed: true, reason: null, message: null };
  }
  entries.decompose = { ...entries.decompose, uses_left_today: null, resets_at: null };
  for (const key of ['review_applications', 'manage_users', 'view_activity', 'retract_any', 'export_research']) {
    entries[key] = adminAllowed && role !== 'user'
      ? { allowed: true, reason: null, message: null }
      : { allowed: false, reason: 'admin_required', message: 'Admin access required.' };
  }
  entries.manage_admins = role === 'owner' && adminAllowed
    ? { allowed: true, reason: null, message: null }
    : { allowed: false, reason: 'owner_required', message: 'Only the owner can do this.' };
  return entries;
}

const STATS = {
  auth_enabled: true, total_users: 6, active_users: 5, active_usernames_30d: ['bob'], event_count_30d: 0, failures_30d: 0,
  average_latency_ms_30d: 0, events_by_action_30d: {}, model_usage_30d: {},
  readiness: { status: 'ready', checks: { schema_exists: true } },
};

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** Rows of the users table: the row naming `username` as text or as a field's value. */
function rowFor(username: string): HTMLTableRowElement | undefined {
  return ([...document.querySelectorAll('tbody tr')] as HTMLTableRowElement[]).find((row) =>
    [...row.querySelectorAll('*')].some((element) =>
      (element.childElementCount === 0 && element.textContent?.trim() === username)
      || (element instanceof HTMLInputElement && element.value === username)));
}

/** The agreed action names a row offers, from each button's text or accessible name. */
function actionsIn(username: string): string[] {
  const row = rowFor(username);
  if (!row) return ['<no row>'];
  const names = [...row.querySelectorAll('button, a')].map((element) => {
    const text = element.textContent?.trim() ?? '';
    const label = element.getAttribute('aria-label')?.trim() ?? '';
    return ACTIONS.find((name) => text === name || label === name || label.startsWith(`${name} `));
  });
  return names.filter((name): name is string => Boolean(name)).sort();
}

function actionButton(username: string, action: string): HTMLElement {
  const row = rowFor(username);
  const button = row && [...row.querySelectorAll('button, a')].find((element) => {
    const label = element.getAttribute('aria-label')?.trim() ?? '';
    return element.textContent?.trim() === action || label === action || label.startsWith(`${action} `);
  });
  if (!button) throw new Error(`No ${action} for ${username}`);
  return button as HTMLElement;
}

const openDialog = () => document.querySelector('[role="dialog"]') as HTMLElement | null;

test('the admin page offers each account only what section 4 allows, and sends edits without roles', async () => {
  let viewer: Account = carol;
  let adminAllowed = true;
  let refusal: Response | null = null;
  const calls: Array<{ method: string; path: string; body: Record<string, unknown> | null }> = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    const method = options.method || 'GET';
    const body = typeof options.body === 'string' ? JSON.parse(options.body) : null;
    calls.push({ method, path, body });
    if (path === '/api/auth/me') return json({ user: viewer, auth_enabled: true, capabilities: capabilities(viewer.role, adminAllowed) });
    if (path === '/api/admin/stats') return json(STATS);
    if (path === '/api/admin/users' && method === 'GET') return json({ users: USERS });
    if (path === '/api/admin/audit') return json({ events: [] });
    if (path === '/api/admin/users/4/history') {
      return json({ history: [
        { action: 'blocked', actor: 'olivia', reason: 'Posted spam.', details: {}, created_at: '2026-11-21T10:00:00+00:00' },
        { action: 'admin_granted', actor: 'bootstrap', reason: null, details: {}, created_at: '2026-10-01T09:00:00+00:00' },
      ] });
    }
    if (path === '/api/admin/users' && method === 'POST') return json({ user: account(8, 'frank', 'user') });
    const target = /^\/api\/admin\/users\/(\d+)(?:\/(block|unblock|grant-admin|revoke-admin))?$/.exec(path);
    if (target && (method === 'PATCH' || method === 'POST')) {
      if (refusal) return refusal;
      return json({ user: USERS.find((item) => item.id === Number(target[1])) });
    }
    throw new Error(`Unexpected API request: ${method} ${path}`);
  }));
  const requests = (method: string, pattern: RegExp) => calls.filter((call) => call.method === method && pattern.test(call.path));

  const root = document.createElement('div');
  root.id = 'root';
  document.body.append(root);
  await import('../src/admin/main');
  await expect.poll(() => rowFor('bob') !== undefined).toBe(true);

  // ------------------------------------------------------------------ viewed by an admin
  const asAdmin: Record<string, string[]> = {
    olivia: ['History'],
    carol: ['Edit', 'History', 'Reset password'],
    dave: ['History'],
    bob: ['Block', 'Edit', 'History', 'Reset password'],
    erin: ['Edit', 'History', 'Reset password', 'Unblock'],
    pat: ['Block', 'History'],
    gina: ['History'], // a blocked admin: only the owner unblocks it
  };
  for (const [username, expected] of Object.entries(asAdmin)) {
    await expect.poll(() => actionsIn(username), { message: `${username}, viewed by an admin` }).toEqual(expected);
  }

  // Role, block state, last login and the identity provider are shown.
  expect(rowFor('olivia')?.textContent).toContain('Owner');
  expect(rowFor('carol')?.textContent).toContain('Admin');
  expect(rowFor('bob')?.textContent).toContain('User');
  expect(rowFor('erin')?.textContent).toContain('Blocked');
  expect(rowFor('bob')?.textContent).toContain('2026');
  expect(rowFor('pat')?.textContent?.toLowerCase()).toContain('rohub');
  expect(document.querySelector('tbody')?.textContent).not.toContain('Disabled');

  // An edit sends the username, display name and email only.
  actionButton('bob', 'Edit').click();
  await expect.poll(() => [...document.querySelectorAll('input')].some((input) => input.value === 'Bob Example')).toBe(true);
  const displayName = [...document.querySelectorAll('input')].find((input) => input.value === 'Bob Example') as HTMLInputElement;
  await userEvent.fill(displayName, 'Robert Example');
  const save = [...document.querySelectorAll('button')].filter((button) => button.textContent?.trim() === 'Save' && !button.disabled);
  expect(save).toHaveLength(1);
  save[0].click();
  await expect.poll(() => requests('PATCH', /^\/api\/admin\/users\/4$/).length).toBe(1);
  const edit = requests('PATCH', /^\/api\/admin\/users\/4$/)[0].body ?? {};
  expect(edit.display_name).toBe('Robert Example');
  expect(Object.keys(edit).every((key) => ['username', 'display_name', 'email'].includes(key))).toBe(true);

  // A password reset sends the password only.
  actionButton('bob', 'Reset password').click();
  await userEvent.fill(page.getByLabelText('New password, at least 8 characters'), 'new-password-123');
  await userEvent.fill(page.getByLabelText('Confirm the new password'), 'new-password-123');
  await page.getByRole('button', { name: 'Set password' }).click();
  await expect.poll(() => requests('PATCH', /^\/api\/admin\/users\/4$/).length).toBe(2);
  expect(requests('PATCH', /^\/api\/admin\/users\/4$/)[1].body).toEqual({ password: 'new-password-123' });

  // A block needs a reason.
  actionButton('bob', 'Block').click();
  await expect.poll(() => openDialog() !== null).toBe(true);
  const dialog = openDialog() as HTMLElement;
  const confirmBlock = [...dialog.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Block') as HTMLButtonElement;
  expect(confirmBlock).toBeTruthy();
  if (!confirmBlock.disabled) confirmBlock.click();
  expect(requests('POST', /\/block$/)).toEqual([]);
  const reason = (dialog.querySelector('textarea') ?? dialog.querySelector('input')) as HTMLTextAreaElement | HTMLInputElement;
  await userEvent.fill(reason, 'Posted spam.');
  await expect.poll(() => confirmBlock.disabled).toBe(false);
  confirmBlock.click();
  await expect.poll(() => requests('POST', /^\/api\/admin\/users\/4\/block$/).length).toBe(1);
  expect(requests('POST', /^\/api\/admin\/users\/4\/block$/)[0].body).toEqual({ reason: 'Posted spam.' });

  // Each account opens its history.
  await expect.poll(() => openDialog() === null).toBe(true);
  actionButton('bob', 'History').click();
  await expect.element(page.getByText('Posted spam.').first()).toBeVisible();
  await expect.element(page.getByText('bootstrap').first()).toBeVisible();
  await userEvent.keyboard('{Escape}');

  // Creating an account offers no role.
  await expect.poll(() => openDialog() === null).toBe(true);
  await page.getByRole('button', { name: 'Create user' }).click();
  await expect.poll(() => openDialog() !== null).toBe(true);
  expect(page.getByLabelText('Role', { exact: true }).query()).toBeNull();
  await userEvent.keyboard('{Escape}');

  // ------------------------------------------------------------------ viewed by the owner
  viewer = olivia;
  await expect.poll(() => openDialog() === null).toBe(true);
  await page.getByRole('button', { name: 'Refresh' }).click();
  const asOwner: Record<string, string[]> = {
    olivia: ['Edit', 'History', 'Reset password'],
    carol: ['Block', 'Edit', 'History', 'Remove admin', 'Reset password'],
    dave: ['Block', 'Edit', 'History', 'Remove admin', 'Reset password'],
    bob: ['Block', 'Edit', 'History', 'Make admin', 'Reset password'],
    pat: ['Block', 'History', 'Make admin'],
    gina: ['Edit', 'History', 'Remove admin', 'Reset password', 'Unblock'],
  };
  for (const [username, expected] of Object.entries(asOwner)) {
    await expect.poll(() => actionsIn(username), { message: `${username}, viewed by the owner` }).toEqual(expected);
  }
  // Making a blocked account an admin is the server's refusal to show; Unblock replaces Block.
  expect(actionsIn('erin').filter((name) => name !== 'Make admin')).toEqual(['Edit', 'History', 'Reset password', 'Unblock']);

  // A refusal's detail is shown as the error.
  refusal = json({ detail: 'Unblock the account before granting admin.', reason: 'target_blocked' }, 400);
  const grants = () => requests('POST', /^\/api\/admin\/users\/4\/grant-admin$/).length;
  actionButton('bob', 'Make admin').click();
  // The page may ask first (granting takes an optional reason): confirm when it does.
  await expect.poll(() => grants() > 0 || openDialog() !== null).toBe(true);
  const dialogAfterMake = openDialog();
  if (grants() === 0 && dialogAfterMake) {
    const confirmMake = [...dialogAfterMake.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Make admin');
    confirmMake?.click();
  }
  await expect.poll(grants).toBe(1);
  await expect.element(page.getByText('Unblock the account before granting admin.').first()).toBeVisible();
  refusal = null;

  // ------------------------------------------------------------------ roles say admin, capabilities do not
  viewer = carol;
  adminAllowed = false;
  await userEvent.keyboard('{Escape}');
  await page.getByRole('button', { name: 'Refresh' }).click();
  await expect.element(page.getByText('Admin access required').first()).toBeVisible();
  await expect.poll(() => document.querySelectorAll('tbody tr').length).toBe(0);
}, 60_000);
