import { afterEach, expect, test, vi } from 'vitest';
import { server } from 'vitest/browser';
import { Parser } from 'n3';

let container;
const publishedStorageKey = 'iadopt-published-nanopubs';
let previousPublishedNanopubs;

afterEach(() => {
  container?.remove();
  if (previousPublishedNanopubs === null) {
    localStorage.removeItem(publishedStorageKey);
  } else if (previousPublishedNanopubs !== undefined) {
    localStorage.setItem(publishedStorageKey, previousPublishedNanopubs);
  }
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test('public tools preserve local work while local login gates protected actions', async () => {
  // Use the real application markup and entry point so account controls and action
  // listeners are covered together. No request is allowed to reach a real API.
  previousPublishedNanopubs = localStorage.getItem(publishedStorageKey);
  localStorage.removeItem(publishedStorageKey);
  const html = await server.commands.readFile('index.html');
  const page = new DOMParser().parseFromString(html, 'text/html');
  page.querySelectorAll('script').forEach((script) => script.remove());
  container = document.createElement('div');
  container.innerHTML = page.body.innerHTML;
  document.body.append(container);

  const element = (selector) => {
    const node = container.querySelector(selector);
    expect(node, `${selector} exists in the application`).not.toBeNull();
    return node;
  };
  const json = (body, status = 200) => new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
  const user = { username: 'reader', display_name: 'Local reader', roles: ['user'] };
  const calls = [];
  const unexpectedCalls = [];
  let sessionActive = false;
  let rejectLogin = false;
  let rejectDecomposition = false;
  let expireTelemetry = false;
  let holdTelemetry = false;
  let releaseTelemetry;
  const publishedUrl = `https://w3id.org/np/RA${'b'.repeat(43)}`;
  const retractionUrl = `https://w3id.org/np/RA${'c'.repeat(43)}`;

  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), window.location.href).pathname;
    const method = options.method || 'GET';
    calls.push({ path, method, options });

    if (path === '/api/auth/me' && method === 'GET') {
      return sessionActive ? json({ user }) : json({ detail: 'Authentication required.' }, 401);
    }
    if (path === '/api/model-options' && method === 'GET') {
      return json({
        default_model_provider: 'psnc',
        providers: {
          psnc: { label: 'PSNC', default_model_name: 'public-model', model_names: ['public-model'] },
        },
      });
    }
    if (path === '/api/nanopub/preparation-options' && method === 'GET') {
      return json({
        default_creator_orcid_id: null,
        conforms_to_uri: 'https://w3id.org/iadopt/ont/Variable',
        created_with_label: 'I-ADOPT Variable Description Service',
      });
    }
    if (path === '/api/auth/login' && method === 'POST') {
      if (rejectLogin) return json({ detail: 'Invalid credentials.' }, 401);
      sessionActive = true;
      return json({ user });
    }
    if (path === '/api/auth/logout' && method === 'POST') {
      sessionActive = false;
      return json({ ok: true });
    }
    if (path === '/api/events' && method === 'POST') {
      if (holdTelemetry) {
        return new Promise((resolve) => { releaseTelemetry = resolve; });
      }
      if (expireTelemetry) {
        sessionActive = false;
        return json({ detail: 'Session expired.' }, 401);
      }
      return json({ ok: true });
    }
    if (path === '/api/decompose/stream' && method === 'POST' && rejectDecomposition) {
      sessionActive = false;
      return json({ detail: 'Session expired.' }, 401);
    }
    if (path === '/api/nanopub/publish' && method === 'POST') {
      return json({
        nanopub_url: publishedUrl,
        variable_identifier: 'guest-edited-variable',
        variable_uri: 'https://example.org/guest-edited-variable',
      });
    }
    if (path === '/api/nanopub/retract' && method === 'POST') {
      return json({ retraction_url: retractionUrl, retracted_nanopub_url: publishedUrl });
    }

    unexpectedCalls.push({ path, method });
    throw new Error(`Unexpected API request: ${method} ${path}`);
  }));

  const popup = vi.spyOn(window, 'open').mockReturnValue(null);
  const objectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:public-export-test');
  // Capture the download without navigating the test browser away from its runner.
  const anchorClick = HTMLAnchorElement.prototype.click;
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    if (!this.download) anchorClick.call(this);
  });

  await import('../src/main.js');
  await expect.poll(() => element('#svg').querySelector('svg')).not.toBeNull();
  await expect.poll(() => element('#applyPreNanopubSettings').disabled).toBe(false);
  await expect.poll(() => calls.filter((call) => call.path === '/api/auth/me').length).toBeGreaterThan(0);
  expect(getComputedStyle(element('#loginButton')).display).not.toBe('none');
  expect(getComputedStyle(element('#logoutButton')).display).toBe('none');
  expect(element('#loginDialog').open).toBe(false);
  expect(calls.some((call) => call.path === '/api/events')).toBe(false);

  // Anonymous visitors can edit the rendered model and prepare/export the result.
  element('#svg .action.edit').click();
  element('#editor-label').value = 'Guest edited variable';
  element('#editor-save').click();
  await expect.poll(() => element('#input').value).toContain('Guest edited variable');
  element('#creatorOrcidInput').value = '0000-0002-1825-0097';
  element('#applyPreNanopubSettings').click();
  await expect.poll(() => element('#preNanopubStatus').textContent).toContain('settings applied');
  const preparedTurtle = element('#input').value;
  expect(new Parser().parse(preparedTurtle).some((quad) => (
    quad.predicate.value === 'http://purl.org/dc/terms/creator'
    && quad.object.value === 'https://orcid.org/0000-0002-1825-0097'
  ))).toBe(true);
  element('#visualize').click();
  await expect.poll(() => element('#svg').textContent).toContain('Guest edited variable');
  element('#export a[data-format="ttl"]').dispatchEvent(new MouseEvent('click', {
    bubbles: true, cancelable: true,
  }));
  await expect.poll(() => objectURL.mock.calls.length).toBe(1);
  expect(await objectURL.mock.calls[0][0].text()).toContain('Guest edited variable');
  expect(calls.some((call) => call.path === '/api/events')).toBe(false);

  const originalLocation = window.location.href;
  element('#definitionInput').value = 'Keep this variable definition';
  element('#rawOutput').value = 'Keep this previous raw output';
  element('#retractInput').value = `https://w3id.org/np/RA${'a'.repeat(43)}`;
  element('#retractInput').dispatchEvent(new Event('input', { bubbles: true }));
  const protectedCalls = () => calls.filter((call) => [
    '/api/decompose/stream', '/api/nanopub/publish', '/api/nanopub/retract',
  ].includes(call.path));
  const expectWorkPreserved = () => {
    expect(element('#input').value).toBe(preparedTurtle);
    expect(element('#definitionInput').value).toBe('Keep this variable definition');
    expect(element('#rawOutput').value).toBe('Keep this previous raw output');
    expect(element('#retractInput').value).toBe(`https://w3id.org/np/RA${'a'.repeat(43)}`);
    expect(window.location.href).toBe(originalLocation);
  };
  const cancelLogin = async () => {
    const closed = new Promise((resolve) => element('#loginDialog').addEventListener('close', resolve, { once: true }));
    element('#cancelInlineLogin').click();
    await closed;
  };

  for (const selector of ['#decompose', '#publishNanopubButton', '#retractButton']) {
    element(selector).click();
    await expect.poll(() => element('#loginDialog').open).toBe(true);
    expectWorkPreserved();
    expect(protectedCalls()).toEqual([]);
    expect(popup).not.toHaveBeenCalled();
    await cancelLogin();
    expect(element('#loginDialog').open).toBe(false);
  }

  const signIn = async () => {
    element('#loginButton').click();
    expect(element('#loginDialog').open).toBe(true);
    element('#inlineUsername').value = 'reader';
    element('#inlinePassword').value = 'local-test-password';
    const closed = new Promise((resolve) => element('#loginDialog').addEventListener('close', resolve, { once: true }));
    element('#inlineLoginForm').dispatchEvent(new Event('submit', {
      bubbles: true, cancelable: true,
    }));
    await closed;
    await expect.poll(() => element('#loginDialog').open).toBe(false);
    expect(element('#currentUser').textContent).toContain('Local reader');
    expect(getComputedStyle(element('#loginButton')).display).toBe('none');
    expect(getComputedStyle(element('#logoutButton')).display).not.toBe('none');
    expect(element('#inlinePassword').value).toBe('');
  };

  rejectLogin = true;
  element('#loginButton').click();
  element('#inlineUsername').value = 'reader';
  element('#inlinePassword').value = 'wrong-password';
  element('#inlineLoginForm').dispatchEvent(new Event('submit', {
    bubbles: true, cancelable: true,
  }));
  await expect.poll(() => element('#inlineLoginStatus').textContent).toContain('Invalid credentials.');
  expect(element('#loginDialog').open).toBe(true);
  expect(element('#inlinePassword').value).toBe('');
  expectWorkPreserved();
  expect(protectedCalls()).toEqual([]);
  rejectLogin = false;
  await signIn();
  const loginCall = calls.filter((call) => call.path === '/api/auth/login').at(-1);
  expect(JSON.parse(loginCall.options.body)).toEqual({
    username: 'reader', password: 'local-test-password',
  });
  // Signing in never repeats the decomposition or irreversible publication action.
  expect(protectedCalls()).toEqual([]);
  expect(popup).not.toHaveBeenCalled();
  expectWorkPreserved();

  element('#logoutButton').click();
  await expect.poll(() => getComputedStyle(element('#loginButton')).display).not.toBe('none');
  expect(calls.filter((call) => call.path === '/api/auth/logout')).toHaveLength(1);
  expectWorkPreserved();

  // A session that expires before the preflight leaves every input untouched.
  await signIn();
  sessionActive = false;
  element('#decompose').click();
  await expect.poll(() => element('#loginDialog').open).toBe(true);
  expectWorkPreserved();
  expect(protectedCalls()).toEqual([]);
  await cancelLogin();

  // Cover the race where auth/me succeeds but the protected request returns 401.
  await signIn();
  rejectDecomposition = true;
  element('#decompose').click();
  await expect.poll(() => element('#loginDialog').open).toBe(true);
  await expect.poll(() => element('#decompose').disabled).toBe(false);
  expectWorkPreserved();
  expect(protectedCalls()).toHaveLength(1);
  expect(protectedCalls()[0].path).toBe('/api/decompose/stream');
  await cancelLogin();

  // Optional telemetry cannot interrupt a public visualization when a session ends.
  await signIn();
  expireTelemetry = true;
  element('#visualize').click();
  await expect.poll(() => getComputedStyle(element('#loginButton')).display).not.toBe('none');
  expect(element('#loginDialog').open).toBe(false);
  expectWorkPreserved();
  expect(popup).not.toHaveBeenCalled();

  // A late 401 from an older session cannot sign out a newly authenticated user.
  expireTelemetry = false;
  await signIn();
  holdTelemetry = true;
  element('#visualize').click();
  await expect.poll(() => typeof releaseTelemetry).toBe('function');
  element('#logoutButton').click();
  await expect.poll(() => getComputedStyle(element('#loginButton')).display).not.toBe('none');
  await signIn();
  holdTelemetry = false;
  releaseTelemetry(json({ detail: 'Old session expired.' }, 401));
  await new Promise((resolve) => requestAnimationFrame(resolve));
  expect(element('#currentUser').textContent).toContain('Local reader');
  expect(getComputedStyle(element('#loginButton')).display).toBe('none');
  expect(element('#loginDialog').open).toBe(false);
  expectWorkPreserved();

  // A blocked popup still leaves a usable result link after mocked publication.
  element('#publishNanopubButton').click();
  await expect.poll(() => container.querySelector('#preNanopubStatus a')?.href).toBe(publishedUrl);
  expect(element('#preNanopubStatus a').target).toBe('_blank');
  expect(element('#preNanopubStatus a').rel).toContain('noopener');
  expect(calls.filter((call) => call.path === '/api/nanopub/publish')).toHaveLength(1);
  const publishRequest = calls.find((call) => call.path === '/api/nanopub/publish');
  expect(JSON.parse(publishRequest.options.body)).toEqual({
    ttl: preparedTurtle.trim(), creator_orcid_id: 'https://orcid.org/0000-0002-1825-0097',
  });
  expect(JSON.parse(localStorage.getItem(publishedStorageKey))).toHaveLength(1);
  expect(element('#retractSelect').value).toBe(publishedUrl);

  element('#retractInput').value = '';
  element('#retractInput').dispatchEvent(new Event('input', { bubbles: true }));
  element('#retractButton').click();
  await expect.poll(() => container.querySelector('#retractStatus a')?.href).toBe(retractionUrl);
  expect(element('#retractStatus a').target).toBe('_blank');
  expect(element('#retractStatus a').rel).toContain('noopener');
  expect(calls.filter((call) => call.path === '/api/nanopub/retract')).toHaveLength(1);
  const retractRequest = calls.find((call) => call.path === '/api/nanopub/retract');
  expect(JSON.parse(retractRequest.options.body)).toEqual({ nanopub_uri: publishedUrl });
  expect(JSON.parse(localStorage.getItem(publishedStorageKey))).toEqual([]);
  expect(protectedCalls()).toHaveLength(3);
  expect(popup).toHaveBeenCalledWith(publishedUrl, '_blank', 'noopener,noreferrer');
  expect(popup).toHaveBeenCalledWith(retractionUrl, '_blank', 'noopener,noreferrer');
  expect(window.location.href).toBe(originalLocation);
  expect(unexpectedCalls).toEqual([]);
}, 30_000);
