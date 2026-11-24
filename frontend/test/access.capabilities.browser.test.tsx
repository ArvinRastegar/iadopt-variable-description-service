import { afterEach, expect, test, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import { toast } from 'sonner';

/*
 * access-policy (components/access-policy.md, approved under gate G-017), section 8, on the main
 * page; increment-1 tests gate G-018. The frontend shows what `capabilities` in GET /api/auth/me
 * reports and never decides access itself: the admin link follows `capabilities`, not `roles`, and
 * a refusal's `detail` is the error the person sees. Covers: AC-access-policy-017 (the admin page's
 * controls are in admin.accounts.browser.test.tsx). No request reaches a real API.
 */

type Entry = { allowed: boolean; reason: string | null; message: string | null; uses_left_today?: number | null; resets_at?: string | null };

const OWN = ['public_tools', 'session', 'account_data', 'decompose', 'save_and_review', 'publish', 'retract_own'];
const ADMIN = ['review_applications', 'manage_users', 'view_activity', 'retract_any', 'export_research'];

function capabilities(adminAllowed: boolean): Record<string, Entry> {
  const entries: Record<string, Entry> = {};
  for (const key of OWN) entries[key] = { allowed: true, reason: null, message: null };
  entries.decompose = { ...entries.decompose, uses_left_today: null, resets_at: null };
  for (const key of ADMIN) {
    entries[key] = adminAllowed
      ? { allowed: true, reason: null, message: null }
      : { allowed: false, reason: 'admin_required', message: 'Admin access required.' };
  }
  entries.manage_admins = { allowed: false, reason: 'owner_required', message: 'Only the owner can do this.' };
  return entries;
}

function session(roles: string[], role: string, adminAllowed: boolean) {
  return {
    auth_enabled: true,
    user: { id: 7, username: 'carol', display_name: 'Carol Example', email: 'carol@example.org', roles, role, is_active: true, auth_provider: 'local' },
    capabilities: capabilities(adminAllowed),
  };
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

let unmount: (() => void) | null = null;

afterEach(() => {
  toast.dismiss();
  unmount?.();
  unmount = null;
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function mount(me: unknown, routes: Record<string, () => Response> = {}) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    const method = options.method || 'GET';
    if (routes[`${method} ${path}`]) return routes[`${method} ${path}`]();
    if (path === '/api/auth/me') return json(me);
    if (path === '/api/auth/me/orcids') return json({ orcids: [] });
    if (path === '/api/events') return json({ status: 'ok' });
    if (path === '/api/model-options') {
      return json({ default_model_provider: 'psnc', providers: { psnc: { label: 'PSNC', default_model_name: 'test-model', model_names: ['test-model'] } } });
    }
    if (path === '/api/nanopub/preparation-options') {
      return json({ conforms_to_uri: 'https://w3id.org/iadopt/ont/Variable', created_with_label: 'I-ADOPT Variable Description Service' });
    }
    throw new Error(`Unexpected API request: ${method} ${path}`);
  }));
  localStorage.clear();
  window.location.hash = '#/';
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
  await expect.element(page.getByRole('button', { name: /Account menu for Carol Example/ })).toBeVisible();
}

async function adminLinkShown(): Promise<boolean> {
  await page.getByRole('button', { name: /Account menu for Carol Example/ }).click();
  await expect.element(page.getByRole('menuitem', { name: 'Sign out' })).toBeVisible();
  const shown = page.getByRole('menuitem', { name: 'Admin' }).query() !== null;
  await userEvent.keyboard('{Escape}');
  return shown;
}

test('the admin link follows capabilities, not roles', async () => {
  // An account whose roles still say admin, but whose capabilities allow no admin capability.
  await mount(session(['admin', 'user'], 'admin', false));
  expect(await adminLinkShown()).toBe(false);
  unmount?.();
  unmount = null;

  await mount(session(['admin', 'user'], 'admin', true));
  expect(await adminLinkShown()).toBe(true);
});

test('a refusal shows its detail as the error', async () => {
  let refusal = json({ detail: 'Accept the current terms to continue.', reason: 'terms_not_accepted' }, 403);
  await mount(session(['user'], 'user', false), { 'POST /api/decompose/stream': () => refusal });

  await userEvent.fill(page.getByLabelText('Variable definition'), 'Dissolved oxygen in water');
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(page.getByText('Accept the current terms to continue.').first()).toBeVisible();

  toast.dismiss();
  refusal = new Response(
    JSON.stringify({ detail: 'No decompositions left today.', reason: 'daily_limit', resets_at: '2026-11-24T00:00:00+01:00' }),
    { status: 429, headers: { 'Content-Type': 'application/json', 'Retry-After': '3600' } },
  );
  await expect.element(page.getByRole('button', { name: /^Decompose/ })).toBeEnabled();
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(page.getByText('No decompositions left today.').first()).toBeVisible();
});
