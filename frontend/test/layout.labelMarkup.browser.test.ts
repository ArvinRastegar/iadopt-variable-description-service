import { afterAll, beforeAll, expect, test } from 'vitest';

/*
 * Labels come from user-supplied Turtle (shared ?ttl= links, files, drafts). Measuring them for
 * the layout must treat them as plain text: markup in a label must never become live elements.
 */

const TTL = `@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://example.org/variable> a iop:Variable ;
  rdfs:label "probe <image href='missing.png' onerror='window.__labelProbe = 1'/>" ;
  iop:hasProperty <https://example.org/property> ;
  iop:hasObjectOfInterest <https://example.org/entity> .
<https://example.org/property> a iop:Property ; rdfs:label "property" .
<https://example.org/entity> a iop:Entity ;
  rdfs:label "<foreignObject><img/src='missing.png'/onerror='window.__labelProbe=2'></foreignObject>" .
`;

let helper: HTMLElement;

beforeAll(() => {
  // The layout engine measures text with this element, as on the real page.
  helper = document.createElement('div');
  helper.id = 'helper';
  helper.innerHTML = '<svg><text></text></svg>';
  document.body.append(helper);
});

afterAll(() => helper.remove());

test('measures markup in labels as text without running it', async () => {
  const { default: extract } = await import('../src/lib/extract.js');
  const { layoutVariable } = await import('../src/lib/iadopt/layout');
  const probe = window as unknown as { __labelProbe?: number };

  const [variable] = await extract(TTL);
  const layout = await layoutVariable(variable);
  // Give a created element time to load and fail, which is when its handler would run.
  await new Promise((resolve) => setTimeout(resolve, 500));

  expect(probe.__labelProbe).toBeUndefined();
  expect(helper.querySelector('text')?.childElementCount).toBe(0);
  // The markup is measured as the characters it consists of, so the box fits the visible text.
  expect(layout.boxes.length).toBeGreaterThan(0);
}, 30_000);

test('draws markup in labels as text on the embed page (remote.html)', async () => {
  const { default: extract } = await import('../src/lib/extract.js');
  const { default: createLayout } = await import('../src/lib/createLayout.js');
  const { default: draw } = await import('../src/lib/draw.js');
  const probe = window as unknown as { __labelProbe?: number };

  const [variable] = await extract(TTL);
  const container = document.createElement('div');
  document.body.append(container);
  draw(container, await createLayout(variable));
  await new Promise((resolve) => setTimeout(resolve, 500));

  expect(probe.__labelProbe).toBeUndefined();
  expect(container.querySelector('text foreignObject, text img, text image')).toBeNull();
  expect(container.textContent).toContain("probe <image href='missing.png'");
  container.remove();
}, 30_000);
