import { afterEach, expect, test, vi } from 'vitest';
import { page } from 'vitest/browser';

/*
 * Regression tests for what the review of the increment-1 implementation found on the admin page
 * (gate G-019). They add to the approved admin.accounts.browser.test.tsx and change nothing of it.
 * Covers: AC-accounts-roles-018. No request reaches a real API.
 */

const allowed = { allowed: true, reason: null, message: null };
const refused = { allowed: false, reason: 'owner_required', message: 'Only the owner can do this.' };

function session(role: 'owner' | 'admin') {
  return {
    auth_enabled: true,
    user: { id: 1, username: 'olivia', display_name: 'Olivia', email: '', roles: ['admin', 'user'], role, is_active: true, auth_provider: 'local' },
    capabilities: { manage_users: allowed, view_activity: refused, export_research: refused, manage_admins: role === 'owner' ? allowed : refused },
  };
}

const dave = {
  id: 3, username: 'dave', display_name: 'Dave', email: '', roles: ['admin', 'user'], role: 'admin', is_active: true, blocked: false,
  auth_provider: 'local', external_subject: 'dave', email_verified: null, local: true, identities: [],
  created_at: '2026-10-01T09:00:00+00:00', last_login_at: null,
};

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test('an older, slower answer never replaces a newer one, and the history names changes made offline', async () => {
  const sessions: Array<(response: Response) => void> = [];
  let hold = true;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const path = new URL(String(url), window.location.href).pathname;
    if (path === '/api/auth/me') return hold ? new Promise<Response>((resolve) => sessions.push(resolve)) : json(session('admin'));
    if (path === '/api/admin/users') return json({ users: [dave] });
    if (path === '/api/admin/users/3/history') {
      return json({ history: [
        { action: 'identity_linked', actor: 'offline', reason: 'Cutover.', details: { issuer: 'https://login.example.org', subject: 's', admin_removed: true }, created_at: '2026-11-21T10:00:00+00:00' },
      ] });
    }
    throw new Error(`Unexpected API request: ${path}`);
  }));
  const buttons = () => [...document.querySelectorAll('tbody button')].map((button) => button.textContent?.trim());

  const root = document.createElement('div');
  root.id = 'root';
  document.body.append(root);
  await import('../src/admin/main');
  await expect.poll(() => sessions.length).toBeGreaterThan(0);

  // Refresh answers first and says admin; only then does the first load's answer say owner.
  hold = false;
  await page.getByRole('button', { name: 'Refresh' }).click();
  await expect.poll(buttons).toEqual(['History']);
  sessions.forEach((answer) => answer(json(session('owner'))));
  await new Promise((resolve) => setTimeout(resolve, 300));
  expect(buttons()).toEqual(['History']);

  // An offline link that removed admin shows it.
  await page.getByRole('button', { name: 'History dave' }).click();
  await expect.element(page.getByText('Identity linked · Admin removed')).toBeVisible();
});
