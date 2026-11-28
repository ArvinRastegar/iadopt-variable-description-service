import { afterEach, expect, test, vi } from 'vitest';

/*
 * owner-sign-in (components/owner-sign-in.md, approved under gate G-025), section 7: where the header names password
 * sign-in too, the admin page still offers "Reset password". The admin page mounts itself once per test file, which is
 * why this case has a file of its own (owner.signIn.browser.test.tsx has the header naming ROHub alone). No request
 * reaches a real API.
 */

const OWN = ['public_tools', 'session', 'account_data', 'decompose', 'save_and_review', 'publish', 'retract_own'];
const ADMIN = ['review_applications', 'manage_users', 'view_activity', 'retract_any', 'export_research', 'manage_admins'];

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });

const allowed = (names: string[]) => Object.fromEntries(names.map((name) => [name, { allowed: true, reason: null, message: null }]));

const account = (id: number, username: string, role: string) => ({
  id, username, role, display_name: username, email: `${username}@example.org`,
  roles: role === 'user' ? ['user'] : ['admin', 'user'], is_active: true, blocked: false, auth_provider: 'local',
  external_subject: username, email_verified: null, local: true, identities: [], approval: 'not_needed',
  created_at: '2026-10-01T09:00:00+00:00', last_login_at: null,
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test('where the header names password sign-in too, the admin page still offers "Reset password"', async () => {
  // Covers: AC-owner-sign-in-013 (the admin page, the header naming both).
  const users = [account(1, 'admin', 'owner'), account(2, 'carol', 'admin'), account(4, 'bob', 'user')];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const path = new URL(String(url), window.location.href).pathname;
    if (path === '/api/auth/me') {
      return json({ user: users[0], auth_enabled: true, capabilities: allowed([...OWN, ...ADMIN]), sign_in: 'password', csrf_token: null },
        200, { 'X-IADOPT-Sign-In': 'password, rohub' });
    }
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
  expect(rowText('bob')).toContain('Reset password');
  root.remove();
});
