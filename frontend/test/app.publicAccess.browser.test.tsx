import { afterEach, expect, test, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import { Parser } from 'n3';

/*
 * Replaces the previous publicAccess.browser.test.js, which drove the old page's element ids.
 * It checks the same promises on the new interface: public tools work without an account and
 * send nothing, protected actions ask for a sign-in in place and keep the work, a sign-in never
 * repeats a protected action, the ORCID rules and the publish/retract payloads are unchanged.
 * No request reaches a real API.
 */

const publishedStorageKey = 'iadopt-published-nanopubs';
let container: HTMLElement | null = null;
let unmount: (() => void) | null = null;

afterEach(() => {
  unmount?.();
  container?.remove();
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

type Quad = { predicate: { value: string }; object: { value: string } };
const creatorQuads = (turtle: string): Quad[] => new Parser().parse(turtle).filter((quad: Quad) => [
  'http://purl.org/dc/terms/creator', 'http://www.w3.org/ns/prov#wasAttributedTo',
].includes(quad.predicate.value));

const editorText = () => (document.querySelector('.cm-content') as HTMLElement | null)?.innerText ?? '';

test('public tools keep local work while sign-in gates protected actions', async () => {
  localStorage.clear();
  window.location.hash = '#/';

  const user = { username: 'reader', display_name: 'Local reader', roles: ['user'] };
  const calls: Array<{ path: string; method: string; body?: string }> = [];
  const unexpected: string[] = [];
  let sessionActive = false;
  let rejectLogin = false;
  let rejectDecomposition = false;
  let expireTelemetry = false;
  const publishedUrl = `https://w3id.org/np/RA${'b'.repeat(43)}`;
  const retractionUrl = `https://w3id.org/np/RA${'c'.repeat(43)}`;

  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    const method = options.method || 'GET';
    calls.push({ path, method, body: typeof options.body === 'string' ? options.body : undefined });
    if (path === '/api/auth/me') return sessionActive ? json({ user }) : json({ detail: 'Authentication required.' }, 401);
    if (path === '/api/model-options') {
      return json({ default_model_provider: 'psnc', providers: { psnc: { label: 'PSNC', default_model_name: 'public-model', model_names: ['public-model'] } } });
    }
    if (path === '/api/nanopub/preparation-options') {
      return json({ conforms_to_uri: 'https://w3id.org/iadopt/ont/Variable', created_with_label: 'I-ADOPT Variable Description Service' });
    }
    if (path === '/api/auth/login' && method === 'POST') {
      if (rejectLogin) return json({ detail: 'Invalid credentials.' }, 401);
      sessionActive = true;
      return json({ user });
    }
    if (path === '/api/auth/logout') {
      sessionActive = false;
      return json({ status: 'ok' });
    }
    if (path === '/api/events') {
      if (expireTelemetry) {
        sessionActive = false;
        return json({ detail: 'Session expired.' }, 401);
      }
      return json({ status: 'ok' });
    }
    if (path === '/api/decompose/stream' && rejectDecomposition) {
      sessionActive = false;
      return json({ detail: 'Session expired.' }, 401);
    }
    if (path === '/api/nanopub/publish') {
      return json({ nanopub_url: publishedUrl, published_to: 'test', variable_identifier: 'guest-edited-variable', variable_uri: 'https://w3id.org/iadopt/variable/guest' });
    }
    if (path === '/api/nanopub/retract') return json({ retraction_url: retractionUrl, published_to: 'test', retracted_nanopub_url: publishedUrl });
    unexpected.push(`${method} ${path}`);
    throw new Error(`Unexpected API request: ${method} ${path}`);
  }));

  const popup = vi.spyOn(window, 'open').mockReturnValue(null);
  const objectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:public-export-test');
  const anchorClick = HTMLAnchorElement.prototype.click;
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    if (!this.download) anchorClick.call(this);
  });
  const events = () => calls.filter((call) => call.path === '/api/events');
  const protectedCalls = () => calls.filter((call) => ['/api/decompose/stream', '/api/nanopub/publish', '/api/nanopub/retract'].includes(call.path));

  // The layout engine measures text with this element, as on the real page.
  const helper = document.createElement('div');
  helper.id = 'helper';
  helper.innerHTML = '<svg><text></text></svg>';
  document.body.append(helper);
  container = document.createElement('div');
  document.body.append(container);
  const { mountApp } = await import('../src/app/main');
  const root = mountApp(container);
  unmount = () => {
    root.unmount();
    helper.remove();
  };

  // Anonymous visitors see the tools, a sign-in button and no account features.
  await expect.element(page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
  await expect.poll(() => calls.some((call) => call.path === '/api/auth/me')).toBe(true);
  expect(events()).toEqual([]);

  // They can open a variable, edit it in the inspector, prepare and export it.
  await page.getByRole('button', { name: 'Open', exact: true }).click();
  await page.getByRole('button', { name: 'Variable: some example variable' }).click();
  await userEvent.fill(page.getByLabelText('Label', { exact: true }), 'Guest edited variable');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await page.getByRole('tab', { name: /Turtle/ }).click();
  await expect.poll(editorText).toContain('Guest edited variable');
  await page.getByRole('button', { name: 'Add nanopub metadata' }).click();
  await expect.poll(editorText).toContain('conformsTo');
  const preparedTurtle = editorText();
  expect(preparedTurtle).not.toContain('orcid');
  await page.getByRole('button', { name: 'Export' }).click();
  await page.getByRole('menuitem', { name: /Turtle/ }).click();
  await expect.poll(() => objectURL.mock.calls.length).toBe(1);
  expect(await (objectURL.mock.calls[0][0] as Blob).text()).toContain('Guest edited variable');
  expect(events()).toEqual([]);

  // Protected actions ask for a sign-in in place, send nothing and keep the work.
  // The example has no definition yet, so the definition field is already open.
  await userEvent.fill(page.getByLabelText('Variable definition'), 'Keep this variable definition');
  const signInDialog = page.getByRole('dialog', { name: 'Sign in' });
  const cancelSignIn = async () => {
    await signInDialog.getByRole('button', { name: 'Cancel' }).click();
    await expect.element(signInDialog).not.toBeInTheDocument();
  };
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(signInDialog).toBeVisible();
  await cancelSignIn();
  await page.getByRole('button', { name: 'Publish', exact: true }).click();
  await expect.element(signInDialog).toBeVisible();
  await cancelSignIn();
  expect(protectedCalls()).toEqual([]);
  expect(popup).not.toHaveBeenCalled();
  expect((page.getByLabelText('Variable definition').element() as HTMLTextAreaElement).value).toBe('Keep this variable definition');

  // A failed sign-in keeps the dialog open and clears the password.
  rejectLogin = true;
  await page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true }).click();
  await userEvent.fill(signInDialog.getByLabelText('Username'), 'reader');
  await userEvent.fill(signInDialog.getByLabelText('Password'), 'wrong-password');
  await signInDialog.getByRole('button', { name: 'Sign in' }).click();
  await expect.element(signInDialog.getByText('Invalid credentials.')).toBeVisible();
  expect((signInDialog.getByLabelText('Password').element() as HTMLInputElement).value).toBe('');

  // A successful sign-in sends exactly the credentials and repeats no protected action.
  rejectLogin = false;
  await userEvent.fill(signInDialog.getByLabelText('Password'), 'local-test-password');
  await signInDialog.getByRole('button', { name: 'Sign in' }).click();
  await expect.element(page.getByRole('button', { name: /Account menu for Local reader/ })).toBeVisible();
  const loginCall = calls.filter((call) => call.path === '/api/auth/login').at(-1);
  expect(JSON.parse(loginCall?.body ?? '{}')).toEqual({ username: 'reader', password: 'local-test-password' });
  expect(protectedCalls()).toEqual([]);

  // A 401 from the stream after the preflight asks for a sign-in again and re-enables the button.
  rejectDecomposition = true;
  await page.getByRole('button', { name: /^Decompose/ }).click();
  await expect.element(signInDialog).toBeVisible();
  expect(protectedCalls().map((call) => call.path)).toEqual(['/api/decompose/stream']);
  await cancelSignIn();
  // The modal dialog hides the page from assistive technology, so check the button after it closed.
  await expect.element(page.getByRole('button', { name: /^Decompose/ })).toBeEnabled();
  rejectDecomposition = false;

  // Sign in again; expired telemetry then signs out quietly, without a dialog.
  await page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true }).click();
  await userEvent.fill(signInDialog.getByLabelText('Username'), 'reader');
  await userEvent.fill(signInDialog.getByLabelText('Password'), 'local-test-password');
  await signInDialog.getByRole('button', { name: 'Sign in' }).click();
  await expect.element(page.getByRole('button', { name: /Account menu/ })).toBeVisible();
  expireTelemetry = true;
  await page.getByRole('tab', { name: /Turtle/ }).click();
  await page.getByRole('button', { name: 'Add nanopub metadata' }).click();
  await expect.element(page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expireTelemetry = false;

  // Publishing: sign in, the ORCID is checked in place, and the payload is the visible Turtle.
  await page.getByRole('banner').getByRole('button', { name: 'Sign in', exact: true }).click();
  await userEvent.fill(signInDialog.getByLabelText('Username'), 'reader');
  await userEvent.fill(signInDialog.getByLabelText('Password'), 'local-test-password');
  await signInDialog.getByRole('button', { name: 'Sign in' }).click();
  await expect.element(page.getByRole('button', { name: /Account menu/ })).toBeVisible();
  await page.getByRole('button', { name: 'Publish', exact: true }).click();
  const publishDialog = page.getByRole('dialog', { name: 'Publish as a nanopublication' });
  await expect.element(publishDialog.getByText('Nanopublication metadata is in place')).toBeVisible();
  await publishDialog.getByRole('button', { name: 'Continue' }).click();
  await publishDialog.getByRole('button', { name: 'Continue' }).click();
  await expect.element(publishDialog.getByText('Please put in your ORCID for nanopublication.')).toBeVisible();
  await userEvent.fill(publishDialog.getByLabelText('Your ORCID iD'), '0000-0002-1825-0098');
  await expect.element(publishDialog.getByText('The ORCID check digit is invalid.')).toBeVisible();
  await userEvent.fill(publishDialog.getByLabelText('Your ORCID iD'), '0000-0002-1825-0097');
  await publishDialog.getByRole('button', { name: 'Continue' }).click();
  await expect.element(publishDialog.getByRole('button', { name: 'Publish nanopublication' })).toBeDisabled();
  await publishDialog.getByRole('checkbox', { name: /I checked the components and links/ }).click();
  await publishDialog.getByRole('button', { name: 'Publish nanopublication' }).click();
  const doneDialog = page.getByRole('dialog', { name: 'Published' });
  await expect.element(doneDialog.getByRole('link', { name: publishedUrl })).toBeVisible();
  const resultLink = doneDialog.getByRole('link', { name: publishedUrl }).element() as HTMLAnchorElement;
  expect(resultLink.target).toBe('_blank');
  expect(resultLink.rel).toContain('noopener');

  const publishCalls = calls.filter((call) => call.path === '/api/nanopub/publish');
  expect(publishCalls).toHaveLength(1);
  const publishBody = JSON.parse(publishCalls[0].body ?? '{}');
  expect(Object.keys(publishBody).sort()).toEqual(['creator_orcid_id', 'ttl']);
  expect(publishBody.creator_orcid_id).toBe('https://orcid.org/0000-0002-1825-0097');
  expect(creatorQuads(publishBody.ttl).map((quad: Quad) => quad.object.value)).toEqual([
    'https://orcid.org/0000-0002-1825-0097', 'https://orcid.org/0000-0002-1825-0097',
  ]);
  const draft = JSON.parse(localStorage.getItem('iadopt-workspace-draft') ?? '{}');
  await expect.poll(() => JSON.parse(localStorage.getItem('iadopt-workspace-draft') ?? '{}').turtle?.trim()).toBe(publishBody.ttl);
  expect(draft).toBeTruthy();
  expect(JSON.parse(localStorage.getItem(publishedStorageKey) ?? '[]')).toHaveLength(1);
  expect(popup).not.toHaveBeenCalled();

  // Retraction has no server default either; it is refused in place without an ORCID.
  await doneDialog.getByRole('button', { name: 'View publications' }).click();
  await page.getByRole('button', { name: 'Retract', exact: true }).click();
  const sheet = page.getByRole('dialog', { name: 'Retract this nanopublication?' });
  await userEvent.fill(sheet.getByLabelText('Your ORCID iD'), '');
  await sheet.getByRole('button', { name: 'Publish retraction' }).click();
  await expect.element(sheet.getByText('Please put in your ORCID for the retraction.')).toBeVisible();
  await userEvent.fill(sheet.getByLabelText('Your ORCID iD'), '0000-0002-1825-0098');
  await expect.element(sheet.getByText('The ORCID check digit is invalid.')).toBeVisible();
  expect(calls.filter((call) => call.path === '/api/nanopub/retract')).toHaveLength(0);
  await userEvent.fill(sheet.getByLabelText('Your ORCID iD'), '0000-0002-1825-0097');
  await sheet.getByRole('button', { name: 'Publish retraction' }).click();
  await expect.poll(() => calls.filter((call) => call.path === '/api/nanopub/retract').length).toBe(1);
  const retractCall = calls.find((call) => call.path === '/api/nanopub/retract');
  expect(JSON.parse(retractCall?.body ?? '{}')).toEqual({ nanopub_uri: publishedUrl, creator_orcid_id: 'https://orcid.org/0000-0002-1825-0097' });
  await expect.poll(() => JSON.parse(localStorage.getItem(publishedStorageKey) ?? '[]')[0]?.retractionUrl).toBe(retractionUrl);
  expect(protectedCalls()).toHaveLength(3);
  expect(popup).not.toHaveBeenCalled();
  expect(unexpected).toEqual([]);
}, 60_000);
