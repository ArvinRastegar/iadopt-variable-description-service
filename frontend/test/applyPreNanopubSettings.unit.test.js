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
    assert.include( updated, '<http://example.org/Salinity>' );
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
    // identifier = date + time + two random digits, derived from the same created timestamp
    assert.include( updated, 'dct:identifier "iadopt-variable-20260618T094720-86"' );

    // each required prefix is declared exactly once
    for( const prefix of [ 'dct', 'fdof', 'orcid', 'pav', 'prov', 'xsd' ] ) {
      assert.equal(
        [... updated.matchAll( new RegExp( `@prefix\\s+${prefix}:`, 'g' ) )].length,
        1,
        `${prefix} should be declared once`
      );
    }

    // the enriched Turtle still visualizes: variable + matrix survive extraction
    const variables = await extract( updated );
    assert.equal( variables.length, 1 );
    assert.equal( variables[0].getIri(), 'http://example.org/Salinity' );
    assert.equal( variables[0].getMatrix().getLabel(), 'sea surface layer' );

    const validation = await validatePreNanopubTurtle( updated, OPTIONS );
    assert.equal( validation.variableUri, 'http://example.org/Salinity' );
  } );

  test( 'updates existing managed metadata instead of adding duplicate predicates', async () => {
    const enriched = `@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix pav: <http://purl.org/pav/> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix orcid: <https://orcid.org/> .
@prefix fdof: <https://w3id.org/fdof/ontology#> .
@prefix ex: <http://example.org/> .

ex:Salinity
    a fdof:FAIRDigitalObject, iop:Variable ;
    dct:conformsTo <https://example.org/old-profile> ;
    dct:created "2025-01-01T00:00:00Z"^^xsd:dateTime ;
    dct:creator orcid:0009-0006-1978-4302 ;
    pav:createdWith "Old generator" ;
    prov:wasAttributedTo orcid:0009-0006-1978-4302 ;
    ex:unrelated "keep this value" .
`;
    const updated = await applyPreNanopubSettingsToTurtle( enriched, {
      ... OPTIONS,
      creatorOrcid: 'https://orcid.org/0000-0003-2195-3997',
    } );

    assert.include( updated, 'ex:unrelated "keep this value"' );
    assert.notInclude( updated, 'https://example.org/old-profile' );
    assert.notInclude( updated, '"Old generator"' );
    assert.notInclude( updated, 'orcid:0009-0006-1978-4302' );
    assert.include( updated, 'orcid:0000-0003-2195-3997' );
    assert.equal( [... updated.matchAll( /dct:creator\b/g )].length, 1 );
    assert.equal( [... updated.matchAll( /prov:wasAttributedTo\b/g )].length, 1 );
    assert.equal( [... updated.matchAll( /@prefix\s+dct:/g )].length, 1 );
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

  test( 'mints a date-time + two-digit random identifier when none is supplied', async () => {
    const updated = await applyPreNanopubSettingsToTurtle( NORMAL_TURTLE, {
      ... OPTIONS,
      randomSuffix: undefined,
    } );

    const match = updated.match( /dct:identifier "iadopt-variable-20260618T094720-(\d{2})"/ );
    assert.isNotNull( match, 'identifier should follow iadopt-variable-<datetime>-NN' );
  } );

  test( 'validatePreNanopubTurtle rejects un-enriched Turtle', async () => {
    await expect(
      validatePreNanopubTurtle( NORMAL_TURTLE, OPTIONS ),
    ).rejects.toThrow( /not ready for nanopublication/i );
  } );

} );
