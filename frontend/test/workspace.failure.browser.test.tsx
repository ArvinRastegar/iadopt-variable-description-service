import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import { toast } from 'sonner';

/*
 * A decomposition that fails says why in the definition panel. On a phone that panel is below the
 * diagram, so the message has to come into view by itself. No request reaches a real API.
 */

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const user = { id: 1, username: 'reader', display_name: 'Local reader', roles: ['user'] };

let unmount: (() => void) | null = null;
let failure = '';
/** Lets the decomposition that runs go on to its failure. */
let goOn: (() => void) | null = null;

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    if (path === '/api/auth/me') return json({ user, auth_enabled: true });
    if (path === '/api/auth/me/orcids') return json({ orcids: [] });
    if (path === '/api/events') return json({ status: 'ok' });
    if (path === '/api/model-options') return json({ default_model_provider: 'psnc', providers: { psnc: { label: 'PSNC', default_model_name: 'm', model_names: ['m'] } } });
    if (path === '/api/nanopub/preparation-options') return json({ conforms_to_uri: 'https://w3id.org/iadopt/ont/Variable', created_with_label: 'x' });
    if (path === '/api/decompose/stream') {
      await new Promise<void>((resolve) => { goOn = resolve; });
      return json({ detail: failure }, 502);
    }
    throw new Error(`Unexpected API request: ${options.method ?? 'GET'} ${path}`);
  }));
});

afterEach(async () => {
  toast.dismiss();
  unmount?.();
  unmount = null;
  await page.viewport(1280, 900);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function mountApp(hash: string) {
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
  await expect.element(page.getByRole('button', { name: /^Account menu for/ })).toBeVisible();
}

/** The decomposition that runs fails with this message, which must then be on the screen. */
async function failWith(message: string) {
  await expect.poll(() => goOn).not.toBeNull();
  // Wherever the person looks while it runs.
  window.scrollTo(0, 0);
  failure = message;
  goOn?.();
  goOn = null;
  const shown = page.getByRole('alert').filter({ hasText: message });
  await expect.element(shown).toBeInViewport({ ratio: 0.99 });  // all of it, but for parts of a pixel
  await expect.element(shown.getByText('Decomposition failed')).toBeVisible();
}

test('on a phone a failed decomposition is seen, each time it fails', async () => {
  await page.viewport(390, 844);
  await mountApp('#/');
  await userEvent.fill(page.getByLabelText('Variable definition'), 'Dissolved oxygen in water');
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await failWith('The model is not available.');

  // The same failure again, and another one.
  for (const message of ['The model is not available.', 'No decompositions left today.']) {
    await page.getByRole('button', { name: /^Decompose/ }).click();
    await expect.poll(() => page.getByRole('alert').query()).toBeNull();
    await failWith(message);
  }
});
