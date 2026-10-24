import { afterAll, beforeAll, expect, test } from 'vitest';

/*
 * The diagram exports: an SVG that embeds the fonts and the light palette, and a PNG drawn from it.
 * Rendered in a real browser because the layout measures text and the PNG needs a canvas.
 */

const TTL = `@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://w3id.org/iadopt/variable/20261023T104215-42> a iop:Variable ;
  rdfs:label "Daily mean dissolved oxygen" ;
  iop:hasProperty <http://qudt.org/vocab/quantitykind/MassConcentration> ;
  iop:hasObjectOfInterest <https://www.wikidata.org/entity/Q629> ;
  iop:hasConstraint _:c1 .
<http://qudt.org/vocab/quantitykind/MassConcentration> a iop:Property ; rdfs:label "mass concentration" .
<https://www.wikidata.org/entity/Q629> a iop:Entity ; rdfs:label "oxygen" .
_:c1 a iop:Constraint ; rdfs:label "dissolved" ; iop:constrains <https://www.wikidata.org/entity/Q629> .
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

test('exports the diagram as SVG with embedded fonts and as PNG', async () => {
  await import('../src/styles/globals.css');
  const { default: extract } = await import('../src/lib/extract.js');
  const { layoutVariable } = await import('../src/lib/iadopt/layout');
  const { diagramSvgMarkup, diagramPngBlob, exportFileName } = await import('../src/app/components/diagram/exportDiagram');

  const [variable] = await extract(TTL);
  const layout = await layoutVariable(variable);
  const markup = await diagramSvgMarkup(layout, 'Daily mean dissolved oxygen');

  expect(markup.startsWith('<?xml')).toBe(true);
  expect(markup).toContain('xmlns="http://www.w3.org/2000/svg"');
  expect(markup).toContain('@font-face');
  expect(markup).toContain('data:font/woff2;base64,');
  for (const label of ['Daily mean dissolved oxygen', 'mass concentration', 'oxygen', 'dissolved']) {
    expect(markup).toContain(label);
  }
  // Exports use fixed light colours, never the page's CSS variables.
  expect(markup).not.toContain('var(--');
  // The file is well-formed SVG.
  expect(new DOMParser().parseFromString(markup, 'image/svg+xml').querySelector('parsererror')).toBeNull();

  const png = await diagramPngBlob(layout, 'Daily mean dissolved oxygen');
  expect(png.type).toBe('image/png');
  expect(png.size).toBeGreaterThan(5000);

  expect(exportFileName('https://w3id.org/iadopt/variable/20261023T104215-42', 'svg')).toBe('20261023T104215-42.svg');
  expect(exportFileName(null, 'png')).toBe('visualization.png');
}, 30_000);
