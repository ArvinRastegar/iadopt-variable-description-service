import { afterEach, expect, test, vi } from 'vitest';
import { page } from 'vitest/browser';

import { navigation } from '../src/lib/leaving';

/*
 * oidc-identity (components/oidc-identity.md, approved under gate G-020), section 13, on the admin
 * page: signing out goes where the answer says, without an address to the sign-in page, and a sign-out
 * that fails is shown and leaves nowhere; its requests carry the CSRF token. Leaving the page is
 * watched, never done. No request reaches a real API.
 */

const YES = { allowed: true, reason: null, message: null };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const ada = {
  id: 7, username: 'ada', display_name: 'Ada Example', email: 'ada@example.org', roles: ['admin', 'user'], role: 'owner', is_active: true, blocked: false,
  auth_provider: 'oidc', external_subject: 'subject-ada', email_verified: true, local: false,
  identities: [{ issuer: 'https://login.example.org/auth/realms/rohub', subject: 'subject-ada', last_login_at: null }],
  created_at: '2026-11-25T09:00:00+00:00', last_login_at: null, approval: 'not_needed', approved_at: null, approved_by: null,
};
const zoe = {
  ...ada, id: 8, username: 'zoe', display_name: 'Zoe', email: 'zoe@example.org', roles: ['user'], role: 'user', external_subject: 'subject-zoe', approval: 'waiting',
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test('the admin page signs out to where the answer says, else to the sign-in page, and shows a failure', async () => {
  // The admin page; its unsafe requests.
  const calls: Array<{ method: string; path: string; token: string | null }> = [];
  let answer: () => Response | Promise<Response> = () => json({ status: 'ok', end_session_url: '/api/auth/end-session' });
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    const method = options.method ?? 'GET';
    calls.push({ method, path, token: new Headers(options.headers).get('X-CSRF-Token') });
    if (path === '/api/auth/me') {
      return json({
        user: ada, auth_enabled: true, sign_in: 'rohub', csrf_token: 'token-1',
        capabilities: { manage_users: YES, review_applications: YES, manage_admins: YES },
      });
    }
    if (path === '/api/admin/users') return json({ users: [ada, zoe] });
    if (path === '/api/admin/users/8/approve') return json({ user: { ...zoe, approval: 'approved' } });
    if (path === '/api/auth/logout') return answer();
    throw new Error(`Unexpected API request: ${method} ${path}`);
  }));
  const leave = vi.spyOn(navigation, 'leave').mockImplementation(() => {});
  const root = document.createElement('div');
  root.id = 'root';
  document.body.append(root);
  await import('../src/admin/main');
  const signOut = page.getByRole('button', { name: 'Sign out', exact: true });

  await signOut.click();
  await expect.poll(() => leave.mock.calls).toEqual([['/api/auth/end-session']]);

  // A password session names no address.
  leave.mockClear();
  answer = () => json({ status: 'ok', end_session_url: null });
  await signOut.click();
  await expect.poll(() => leave.mock.calls).toEqual([['./login.html']]);

  // A sign-out that fails leaves nowhere, and what the page shows stays.
  leave.mockClear();
  for (const failure of [() => json({ detail: 'broken' }, 500), () => Promise.reject(new TypeError('Failed to fetch'))]) {
    answer = failure;
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect.poll(() => page.getByText('Could not sign out. Please try again.').query()).toBeNull();
    await signOut.click();
    await expect.element(page.getByText('Could not sign out. Please try again.')).toBeVisible();
    await expect.element(page.getByText('zoe@example.org')).toBeVisible();
  }
  expect(leave).not.toHaveBeenCalled();

  await page.getByRole('button', { name: 'Approve zoe', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Approve' }).click();
  await expect.poll(() => calls.some((call) => call.path.endsWith('/approve'))).toBe(true);
  const unsafe = calls.filter((call) => call.method !== 'GET');
  expect(unsafe.map((call) => call.path)).toEqual([...Array(4).fill('/api/auth/logout'), '/api/admin/users/8/approve']);
  // Signing out forgets the token; a page that reads its session again has the new one.
  expect(unsafe.every((call) => call.token === 'token-1' || call.path === '/api/auth/logout')).toBe(true);
  expect(unsafe[0].token).toBe('token-1');
  expect(calls.filter((call) => call.method === 'GET').every((call) => call.token === null)).toBe(true);
  root.remove();
});
