import { afterEach, assert, describe, test } from 'vitest';

import { getCurrentTurtle } from '../src/lib/export.js';

const originalDocument = globalThis.document;

describe( 'getCurrentTurtle', () => {

  afterEach( () => {
    globalThis.document = originalDocument;
  } );

  test( 'returns the exact updated Turtle currently visible in the textarea', () => {
    // The export/publish flow must read the live textarea, not any stale internal state.
    const visibleTurtle = `@prefix iop: <https://w3id.org/iadopt/ont/> .

<https://example.org/variable>
    a iop:Variable ;
    <http://purl.org/dc/terms/creator> <https://orcid.org/0009-0006-1978-4302> .
`;

    globalThis.document = {
      querySelector: (selector) => selector === '#input'
        ? { value: visibleTurtle }
        : null,
    };

    assert.equal( getCurrentTurtle(), visibleTurtle.trim() );
  } );

} );
