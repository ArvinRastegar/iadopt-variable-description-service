import { assert, describe, test } from 'vitest';

import mergeCurrentTurtle from '../src/lib/mergeCurrentTurtle.js';
import { parseRDF } from '../src/lib/parse.js';
import { Variable } from '../src/model/models.js';
import extract, { NS } from '../src/lib/extract.js';

const VARIABLE_IRI = 'https://example.org/variable';
const DCT_TITLE = 'http://purl.org/dc/terms/title';

const CURRENT_TURTLE = `@prefix iop: <${NS.iop}> .
@prefix rdfs: <${NS.rdfs}> .
@prefix skos: <${NS.skos}> .
@prefix dct: <http://purl.org/dc/terms/> .

<${VARIABLE_IRI}>
    a iop:Variable ;
    rdfs:label "Original main label" ;
    skos:prefLabel "Original preferred label" ;
    skos:altLabel "Formula-generated alternative label" ;
    dct:title "Preserved metadata" .
`;

async function objectsFor( turtle, predicateIri ) {
  const { store } = await parseRDF( turtle );
  return store
    .getQuads( VARIABLE_IRI, predicateIri, null, null )
    .map( (entry) => entry.object.value );
}

describe( 'mergeCurrentTurtle', () => {

  test( 'keeps entity descriptions when another component is edited', async () => {
    const turtle = `@prefix iop: <${NS.iop}> .
@prefix rdfs: <${NS.rdfs}> .

<${VARIABLE_IRI}>
    a iop:Variable ;
    rdfs:label "variable" ;
    iop:hasProperty <https://example.org/property> ;
    iop:hasObjectOfInterest <https://example.org/entity> .
<https://example.org/property> a iop:Property ; rdfs:label "property" .
<https://example.org/entity> a iop:Entity ; rdfs:label "entity" ; rdfs:comment "about the entity" .
`;
    const [ variable ] = await extract( turtle );
    variable.getProperty().setLabel( '', 'renamed property' );

    const updated = await mergeCurrentTurtle( turtle, variable );
    const { store } = await parseRDF( updated );
    assert.deepEqual(
      store.getQuads( 'https://example.org/entity', NS.rdfs + 'comment', null, null ).map( (quad) => quad.object.value ),
      [ 'about the entity' ]
    );
  }, 10_000 );

  test( 'synchronizes all Variable labels after an explicit Variable label edit', async () => {
    const variable = new Variable({
      iri: VARIABLE_IRI,
      label: 'Manually edited label',
    });

    const updated = await mergeCurrentTurtle(
      CURRENT_TURTLE,
      variable,
      { syncVariableLabels: true }
    );

    assert.deepEqual( await objectsFor( updated, NS.rdfs + 'label' ), ['Manually edited label'] );
    assert.deepEqual( await objectsFor( updated, NS.skos + 'prefLabel' ), ['Manually edited label'] );
    assert.deepEqual( await objectsFor( updated, NS.skos + 'altLabel' ), ['Manually edited label'] );
    assert.deepEqual( await objectsFor( updated, DCT_TITLE ), ['Preserved metadata'] );
  } );

  test( 'preserves formula-generated SKOS labels during other visualizer edits', async () => {
    const variable = new Variable({
      iri: VARIABLE_IRI,
      label: 'Original main label',
      comment: 'Edited description',
    });

    const updated = await mergeCurrentTurtle( CURRENT_TURTLE, variable );

    assert.deepEqual( await objectsFor( updated, NS.skos + 'prefLabel' ), ['Original preferred label'] );
    assert.deepEqual(
      await objectsFor( updated, NS.skos + 'altLabel' ),
      ['Formula-generated alternative label']
    );
  } );

} );
