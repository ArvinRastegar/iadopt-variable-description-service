import { assert, describe, expect, test } from 'vitest';

import extract from '../src/lib/extract.js';
import {
  applyPreNanopubSettingsToTurtle,
  normalizeOrcid,
  validatePreNanopubTurtle,
} from '../src/lib/applyPreNanopubSettingsToTurtle.js';

const OPTIONS = {
  creatorOrcid: '0009-0006-1978-4302',
  conformsToUri: 'https://w3id.org/np/RA5MTl9GFH-QuuBHYEA2hOtxOMOV4-jrhtdx5lOy9CAQE',
  createdWithLabel: 'LLM-assisted I-ADOPT variable generation',
  createdAt: new Date( '2026-06-18T09:47:20Z' ),
  randomSuffix: 86,
};

const NORMAL_TURTLE = `# The original comment must survive.
@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix ex: <http://example.org/iadopt/challenge/> .

<http://example.org/Salinity>
    a
        iop:Variable ;
    rdfs:label
        "Salinity of sea surface water" ;
    rdfs:comment
        """Salt in water (g/kg) or PSU""" ;
    iop:hasObjectOfInterest
        _:b0 ;
    iop:hasMatrix
        <http://purl.obolibrary.org/obo/ENVO_01001581> ;
    iop:hasProperty
        <http://qudt.org/vocab/quantitykind/MassFraction> .

_:b0
    a
        iop:Entity ,
        iop:AsymmetricSystem ;
    iop:hasNumerator
        <http://purl.obolibrary.org/obo/CHEBI_24866> ;
    iop:hasDenominator
        <http://vocab.nerc.ac.uk/collection/S18/current/S1800021/> .

<http://purl.obolibrary.org/obo/ENVO_01001581>
    a iop:Entity ;
    rdfs:label "sea surface layer" .

<http://qudt.org/vocab/quantitykind/MassFraction>
    a iop:Property ;
    rdfs:label "mass fraction" .

<http://purl.obolibrary.org/obo/CHEBI_24866>
    a iop:Entity ;
    rdfs:label "salt" .

<http://vocab.nerc.ac.uk/collection/S18/current/S1800021/>
    a iop:Entity ;
    rdfs:label "water" .
`;

describe( 'applyPreNanopubSettingsToTurtle', () => {

  test( 'enriches normal pasted Turtle without replacing unrelated RDF', async () => {
    const updated = await applyPreNanopubSettingsToTurtle( NORMAL_TURTLE, OPTIONS );

    // unrelated content is preserved verbatim
    assert.include( updated, '# The original comment must survive.' );
    assert.include( updated, '"""Salt in water (g/kg) or PSU"""' );
    assert.include( updated, 'iop:hasObjectOfInterest' );
    assert.include( updated, 'iop:hasMatrix' );
    assert.include( updated, 'iop:hasProperty' );
    assert.include( updated, '_:b0' );
    assert.include( updated, 'iop:hasNumerator' );
    assert.include( updated, 'iop:hasDenominator' );

    // required nanopub metadata was added
    assert.include( updated, 'fdof:FAIRDigitalObject' );
    assert.include( updated, 'dct:creator orcid:0009-0006-1978-4302' );
    assert.include( updated, 'prov:wasAttributedTo orcid:0009-0006-1978-4302' );
    assert.include( updated, '"2026-06-18T09:47:20Z"^^xsd:dateTime' );

    // the labelless asymmetric system gets a derived rdfs:label written into the TTL itself,
    // as first part (numerator) + second part (denominator): "salt water"
    assert.include( updated, 'rdfs:label "salt water"' );

    // The non-resolvable example.org subject is minted into a resolvable w3id URI everywhere,
    // and dct:identifier is that resolvable IRI (not a string literal).
    const mintedUri = 'https://w3id.org/iadopt/variable/20260618T094720-86';
    assert.include( updated, `<${mintedUri}>` );
    assert.include( updated, `dct:identifier <${mintedUri}>` );
    assert.notInclude( updated, '<http://example.org/Salinity>' );
    assert.notInclude( updated, 'dct:identifier "iadopt-variable' );

    // each required prefix is declared exactly once
    for( const prefix of [ 'dct', 'fdof', 'orcid', 'pav', 'prov', 'xsd' ] ) {
      assert.equal(
        [... updated.matchAll( new RegExp( `@prefix\\s+${prefix}:`, 'g' ) )].length,
        1,
        `${prefix} should be declared once`
      );
    }

    // the enriched Turtle still visualizes: variable + matrix survive extraction (under the minted URI)
    const variables = await extract( updated );
    assert.equal( variables.length, 1 );
    assert.equal( variables[0].getIri(), mintedUri );
    assert.equal( variables[0].getMatrix().getLabel(), 'sea surface layer' );

    const validation = await validatePreNanopubTurtle( updated, OPTIONS );
    assert.equal( validation.variableUri, mintedUri );
  } );

  test( 'keeps an existing resolvable variable URI and updates metadata without duplicates', async () => {
    const resolvableUri = 'https://w3id.org/iadopt/variable/20260618T112316-13';
    const enriched = `@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix pav: <http://purl.org/pav/> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix orcid: <https://orcid.org/> .
@prefix fdof: <https://w3id.org/fdof/ontology#> .

<${resolvableUri}>
    a fdof:FAIRDigitalObject, iop:Variable ;
    dct:conformsTo <https://example.org/old-profile> ;
    dct:identifier "iadopt-variable-20260618T112316-13" ;
    dct:created "2025-01-01T00:00:00Z"^^xsd:dateTime ;
    dct:creator orcid:0009-0006-1978-4302 ;
    pav:createdWith "Old generator" ;
    prov:wasAttributedTo orcid:0009-0006-1978-4302 ;
    rdfs:comment "keep this value" .
`;
    const updated = await applyPreNanopubSettingsToTurtle( enriched, {
      ... OPTIONS,
      creatorOrcid: 'https://orcid.org/0000-0003-2195-3997',
    } );

    // the already-resolvable subject is preserved (not re-minted)
    assert.include( updated, `<${resolvableUri}>` );
    assert.include( updated, 'rdfs:comment "keep this value"' );
    assert.notInclude( updated, 'https://example.org/old-profile' );
    assert.notInclude( updated, '"Old generator"' );
    assert.notInclude( updated, 'orcid:0009-0006-1978-4302' );
    assert.include( updated, 'orcid:0000-0003-2195-3997' );

    // the string-literal identifier is upgraded to the resolvable IRI, exactly once
    assert.include( updated, `dct:identifier <${resolvableUri}>` );
    assert.notInclude( updated, 'dct:identifier "iadopt-variable-20260618T112316-13"' );
    assert.equal( [... updated.matchAll( /dct:identifier\b/g )].length, 1 );
    assert.equal( [... updated.matchAll( /dct:creator\b/g )].length, 1 );
    assert.equal( [... updated.matchAll( /prov:wasAttributedTo\b/g )].length, 1 );
    assert.equal( [... updated.matchAll( /@prefix\s+dct:/g )].length, 1 );
  } );

  test( 'mints a resolvable URI and rewrites every reference to a non-resolvable subject', async () => {
    const pasted = `@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix fdof: <https://w3id.org/fdof/ontology#> .
@prefix ex: <http://example.org/> .

ex:Temperature
    a fdof:FAIRDigitalObject, iop:Variable ;
    rdfs:label "Air temperature" ;
    iop:hasProperty ex:Prop .

ex:Prop a iop:Property ; rdfs:label "temperature" .

# A statement that references the variable as an object must also be rewritten:
ex:Temperature rdfs:seeAlso ex:Temperature .
`;
    const mintedUri = 'https://w3id.org/iadopt/variable/20260618T094720-86';
    const updated = await applyPreNanopubSettingsToTurtle( pasted, OPTIONS );

    assert.notInclude( updated, 'ex:Temperature' );
    assert.notInclude( updated, '<http://example.org/Temperature>' );
    assert.include( updated, `<${mintedUri}>` );
    assert.include( updated, `dct:identifier <${mintedUri}>` );
    // unrelated example.org terms are untouched
    assert.include( updated, 'ex:Prop' );
    // both the subject occurrences and the object reference were renamed
    assert.isAtLeast( [... updated.matchAll( new RegExp( `<${mintedUri}>`, 'g' ) )].length, 3 );

    const variables = await extract( updated );
    assert.equal( variables.length, 1 );
    assert.equal( variables[0].getIri(), mintedUri );
  } );

  test( 'rejects invalid ORCIDs before changing Turtle', async () => {
    await expect(
      applyPreNanopubSettingsToTurtle( NORMAL_TURTLE, {
        ... OPTIONS,
        creatorOrcid: '0009-0006-1978-4303',
      } ),
    ).rejects.toThrow( /check digit/i );

    await expect(
      applyPreNanopubSettingsToTurtle( NORMAL_TURTLE, {
        ... OPTIONS,
        creatorOrcid: 'not-an-orcid',
      } ),
    ).rejects.toThrow( /valid ORCID/i );

    // ORCID normalization accepts both forms
    assert.equal(
      normalizeOrcid( 'https://orcid.org/0009-0006-1978-4302' ),
      'https://orcid.org/0009-0006-1978-4302'
    );
    assert.equal(
      normalizeOrcid( '0009-0006-1978-4302' ),
      'https://orcid.org/0009-0006-1978-4302'
    );

    // the source string is untouched
    assert.include( NORMAL_TURTLE, '# The original comment must survive.' );
  } );

  test( 'mints a resolvable date-time + two-digit random URI when none is supplied', async () => {
    const updated = await applyPreNanopubSettingsToTurtle( NORMAL_TURTLE, {
      ... OPTIONS,
      randomSuffix: undefined,
    } );

    const match = updated.match(
      /dct:identifier <https:\/\/w3id\.org\/iadopt\/variable\/20260618T094720-(\d{2})>/
    );
    assert.isNotNull( match, 'identifier should be a resolvable <.../variable/<datetime>-NN> IRI' );
  } );

  test( 'validatePreNanopubTurtle rejects un-enriched Turtle', async () => {
    await expect(
      validatePreNanopubTurtle( NORMAL_TURTLE, OPTIONS ),
    ).rejects.toThrow( /not ready for nanopublication/i );
  } );

  test( 'writes a symmetric system label from its parts in order', async () => {
    const symmetric = `@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <http://example.org/> .

ex:Diff a iop:Variable ;
    rdfs:label "Temperature difference" ;
    iop:hasObjectOfInterest _:s ;
    iop:hasProperty ex:Prop .

_:s a iop:Entity, iop:SymmetricSystem ;
    iop:hasPart ex:Air, ex:Sea .

ex:Air a iop:Entity ; rdfs:label "air" .
ex:Sea a iop:Entity ; rdfs:label "sea" .
ex:Prop a iop:Property ; rdfs:label "temperature" .
`;
    const updated = await applyPreNanopubSettingsToTurtle( symmetric, OPTIONS );
    assert.include( updated, 'rdfs:label "air sea"' );
  } );

  test( 'does not overwrite an existing system label', async () => {
    const labelled = `@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <http://example.org/> .

ex:V a iop:Variable ;
    rdfs:label "Some variable" ;
    iop:hasObjectOfInterest _:s ;
    iop:hasProperty ex:Prop .

_:s a iop:Entity, iop:AsymmetricSystem ;
    rdfs:label "custom system label" ;
    iop:hasNumerator ex:N ;
    iop:hasDenominator ex:D .

ex:N a iop:Entity ; rdfs:label "numer" .
ex:D a iop:Entity ; rdfs:label "denom" .
ex:Prop a iop:Property ; rdfs:label "p" .
`;
    const updated = await applyPreNanopubSettingsToTurtle( labelled, OPTIONS );
    assert.include( updated, 'rdfs:label "custom system label"' );
    assert.notInclude( updated, 'rdfs:label "numer denom"' );
  } );

} );
