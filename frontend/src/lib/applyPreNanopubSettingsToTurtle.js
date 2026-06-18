import N3 from 'n3';
import { parseRDF } from './parse.js';

const { DataFactory } = N3;
const { literal, namedNode } = DataFactory;

// Namespaces required for nanopublication metadata. These mirror TTL_PREFIXES in backend/app/main.py
// so pasted-Turtle preparation produces the same metadata shape as backend-generated Turtle.
const NS = {
  dct: 'http://purl.org/dc/terms/',
  fdof: 'https://w3id.org/fdof/ontology#',
  iop: 'https://w3id.org/iadopt/ont/',
  orcid: 'https://orcid.org/',
  pav: 'http://purl.org/pav/',
  prov: 'http://www.w3.org/ns/prov#',
  rdf: 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
  xsd: 'http://www.w3.org/2001/XMLSchema#',
};

const VARIABLE_CLASS = namedNode( NS.iop + 'Variable' );
const FAIR_DIGITAL_OBJECT_CLASS = namedNode( NS.fdof + 'FAIRDigitalObject' );

// The managed predicates whose objects we own. Anything else on the variable is preserved verbatim.
const TARGET_PREDICATES = {
  conformsTo: NS.dct + 'conformsTo',
  identifier: NS.dct + 'identifier',
  created: NS.dct + 'created',
  creator: NS.dct + 'creator',
  createdWith: NS.pav + 'createdWith',
  attributedTo: NS.prov + 'wasAttributedTo',
};


/**
 * ISO-7064 mod-11-2 checksum used by ORCID identifiers.
 * @param   {string}  compactOrcid  16 chars without dashes
 * @returns {boolean}
 */
function isOrcidChecksumValid( compactOrcid ) {
  let total = 0;

  for( const digit of compactOrcid.slice( 0, 15 ) ) {
    total = (total + Number( digit )) * 2;
  }

  const result = (12 - (total % 11)) % 11;
  const expectedCheckDigit = result === 10 ? 'X' : String( result );
  return compactOrcid.at( -1 ) === expectedCheckDigit;
}


/**
 * Accept a bare ORCID (0009-0006-1978-4302) or its URL form and return the canonical URL.
 * Throws a user-facing error on malformed or checksum-invalid input.
 * @param   {string} value  raw ORCID input
 * @returns {string}        canonical https://orcid.org/... URL
 */
export function normalizeOrcid( value ) {
  const rawValue = String( value ?? '' ).trim();
  const suffix = rawValue
    .replace( /^https?:\/\/orcid\.org\//i, '' )
    .toUpperCase();

  if( !/^\d{4}-\d{4}-\d{4}-[\dX]{4}$/.test( suffix ) ) {
    throw new Error( 'Enter a valid ORCID such as 0009-0006-1978-4302.' );
  }

  const compactOrcid = suffix.replaceAll( '-', '' );
  if( !isOrcidChecksumValid( compactOrcid ) ) {
    throw new Error( 'The ORCID check digit is invalid.' );
  }

  return `${NS.orcid}${suffix}`;
}


/**
 * Locate the single URI-identified iop:Variable in the parsed store.
 * @param   {N3.Store} store
 * @returns {string}   the variable IRI
 */
function getUniqueVariableUri( store ) {
  const variableUris = [
    ... new Set(
      store
        .getQuads( null, namedNode( NS.rdf + 'type' ), VARIABLE_CLASS, null )
        .filter( (quad) => quad.subject.termType === 'NamedNode' )
        .map( (quad) => quad.subject.value )
    ),
  ];

  if( variableUris.length === 0 ) {
    throw new Error( 'The Turtle must contain one URI-identified iop:Variable.' );
  }
  if( variableUris.length > 1 ) {
    throw new Error( 'The Turtle contains multiple iop:Variable resources. Prepare one variable at a time.' );
  }

  return variableUris[0];
}


/**
 * Scan Turtle text and return character ranges split on a delimiter, while skipping over comments,
 * IRIs (<...>), quoted/triple-quoted strings, and nested [], (), {} groups. This is what makes the
 * transform non-destructive: comments, blank nodes and string literals are never split or rewritten.
 *
 * @param   {string}   text
 * @param   {string}   delimiter     character to split on
 * @param   {Function} isDelimiter   (char, index, text) => boolean
 * @returns {Array.<{start:number,end:number,delimiterIndex:?number}>}
 */
function scanDelimitedRanges( text, delimiter, isDelimiter = (char) => char === delimiter ) {
  const ranges = [];
  let start = 0;
  let comment = false;
  let iri = false;
  let quote = null;
  let tripleQuote = false;
  let escaped = false;
  let squareDepth = 0;
  let roundDepth = 0;
  let curlyDepth = 0;

  for( let index = 0; index < text.length; index += 1 ) {
    const char = text[index];

    if( comment ) {
      if( char === '\n' || char === '\r' ) {
        comment = false;
      }
      continue;
    }

    if( iri ) {
      if( escaped ) {
        escaped = false;
      } else if( char === '\\' ) {
        escaped = true;
      } else if( char === '>' ) {
        iri = false;
      }
      continue;
    }

    if( quote ) {
      if( escaped ) {
        escaped = false;
        continue;
      }
      if( char === '\\' ) {
        escaped = true;
        continue;
      }
      if( tripleQuote ) {
        if( text.slice( index, index + 3 ) === quote.repeat( 3 ) ) {
          quote = null;
          tripleQuote = false;
          index += 2;
        }
      } else if( char === quote ) {
        quote = null;
      }
      continue;
    }

    if( char === '#' ) {
      comment = true;
      continue;
    }
    if( char === '<' ) {
      iri = true;
      continue;
    }
    if( char === '"' || char === '\'' ) {
      quote = char;
      tripleQuote = text.slice( index, index + 3 ) === char.repeat( 3 );
      if( tripleQuote ) {
        index += 2;
      }
      continue;
    }

    if( char === '[' ) squareDepth += 1;
    if( char === ']' ) squareDepth = Math.max( 0, squareDepth - 1 );
    if( char === '(' ) roundDepth += 1;
    if( char === ')' ) roundDepth = Math.max( 0, roundDepth - 1 );
    if( char === '{' ) curlyDepth += 1;
    if( char === '}' ) curlyDepth = Math.max( 0, curlyDepth - 1 );

    if(
      squareDepth === 0
      && roundDepth === 0
      && curlyDepth === 0
      && isDelimiter( char, index, text )
    ) {
      ranges.push({ start, end: index, delimiterIndex: index });
      start = index + 1;
    }
  }

  ranges.push({ start, end: text.length, delimiterIndex: null });
  return ranges;
}


/**
 * Split Turtle into top-level statements terminated by a `.` followed by whitespace/comment/EOF.
 * @param   {string} turtle
 * @returns {Array.<{start:number,end:number}>}
 */
function scanTurtleStatements( turtle ) {
  return scanDelimitedRanges(
    turtle,
    '.',
    (char, index, text) => {
      if( char !== '.' ) {
        return false;
      }
      const nextChar = text[index + 1];
      return nextChar === undefined || /\s|#/.test( nextChar );
    }
  )
    .filter( (range) => range.delimiterIndex !== null )
    .map( (range) => ({
      start: range.start,
      end: range.delimiterIndex + 1,
    }) );
}


/**
 * Skip whitespace and `#` comments from a starting index.
 * @returns {number} index of the next significant character
 */
function skipTrivia( text, initialIndex = 0 ) {
  let index = initialIndex;

  while( index < text.length ) {
    if( /\s/.test( text[index] ) ) {
      index += 1;
      continue;
    }
    if( text[index] === '#' ) {
      const newlineIndex = text.indexOf( '\n', index );
      return newlineIndex === -1 ? text.length : skipTrivia( text, newlineIndex + 1 );
    }
    break;
  }

  return index;
}


/**
 * Read the next token (an <IRI>, a prefixed name, or `a`) from text.
 * @returns {?{start:number,end:number,value:string}}
 */
function readToken( text, initialIndex = 0 ) {
  const start = skipTrivia( text, initialIndex );
  if( start >= text.length ) {
    return null;
  }

  if( text[start] === '<' ) {
    let escaped = false;
    for( let index = start + 1; index < text.length; index += 1 ) {
      if( escaped ) {
        escaped = false;
      } else if( text[index] === '\\' ) {
        escaped = true;
      } else if( text[index] === '>' ) {
        return { start, end: index + 1, value: text.slice( start, index + 1 ) };
      }
    }
    return null;
  }

  let end = start;
  while( end < text.length && !/[\s;,.[\](){}]/.test( text[end] ) ) {
    end += 1;
  }

  return end > start
    ? { start, end, value: text.slice( start, end ) }
    : null;
}


/**
 * Resolve a token to a full IRI using the document prefixes.
 * @returns {?string}
 */
function resolveToken( token, prefixes ) {
  if( !token ) {
    return null;
  }
  if( token === 'a' ) {
    return NS.rdf + 'type';
  }
  if( token.startsWith( '<' ) && token.endsWith( '>' ) ) {
    return token.slice( 1, -1 );
  }

  const colonIndex = token.indexOf( ':' );
  if( colonIndex === -1 ) {
    return null;
  }

  const prefix = token.slice( 0, colonIndex );
  const localName = token.slice( colonIndex + 1 );
  const namespace = prefixes[prefix];
  return namespace ? `${namespace}${localName}` : null;
}


/**
 * Pick the prefix alias to use for a namespace. Reuses an existing prefix bound to the same
 * namespace (so we never duplicate), otherwise registers the preferred alias as an addition.
 * @returns {string} the prefix alias (without colon)
 */
function choosePrefix( prefixes, additions, preferredPrefix, namespace ) {
  const existingEntry = Object.entries( prefixes )
    .find( ([, iri]) => String( iri ) === namespace );
  if( existingEntry ) {
    return existingEntry[0];
  }

  let prefix = preferredPrefix;
  let suffix = 2;
  while( prefix in prefixes || prefix in additions ) {
    prefix = `${preferredPrefix}${suffix}`;
    suffix += 1;
  }

  additions[prefix] = namespace;
  return prefix;
}


/**
 * Replace the object of a `predicate object` clause while keeping its leading predicate and trailing whitespace.
 */
function replaceClauseObject( clause, predicateEnd, objectText ) {
  const trailingWhitespace = clause.match( /\s*$/ )?.[0] ?? '';
  return `${clause.slice( 0, predicateEnd )} ${objectText}${trailingWhitespace}`;
}


/**
 * Append an extra object to an existing `a A , B` type clause.
 */
function appendTypeObject( clause, objectText ) {
  const trailingWhitespace = clause.match( /\s*$/ )?.[0] ?? '';
  const contentEnd = clause.length - trailingWhitespace.length;
  return `${clause.slice( 0, contentEnd )}, ${objectText}${trailingWhitespace}`;
}


/**
 * Insert missing @prefix declarations right after the last existing prefix line (or at the top).
 */
function insertPrefixDeclarations( turtle, additions ) {
  const entries = Object.entries( additions );
  if( entries.length === 0 ) {
    return turtle;
  }

  const newline = turtle.includes( '\r\n' ) ? '\r\n' : '\n';
  const declarationText = entries
    .map( ([prefix, iri]) => `@prefix ${prefix}: <${iri}> .` )
    .join( newline );
  const lines = turtle.match( /.*(?:\r\n|\n|\r|$)/g ) ?? [];
  let offset = 0;
  let lastPrefixEnd = null;

  for( const line of lines ) {
    offset += line.length;
    if( /^\s*(?:@prefix|PREFIX)\s+/i.test( line ) ) {
      lastPrefixEnd = offset;
    }
  }

  if( lastPrefixEnd === null ) {
    return `${declarationText}${newline}${newline}${turtle}`;
  }

  return (
    turtle.slice( 0, lastPrefixEnd )
    + declarationText
    + newline
    + turtle.slice( lastPrefixEnd )
  );
}


/**
 * Quote a string as a Turtle literal (handles escaping via JSON).
 */
function turtleLiteral( value ) {
  return JSON.stringify( String( value ) );
}


/**
 * Format a creation timestamp as an xsd:dateTime literal value, matching the backend's
 * "%Y-%m-%dT%H:%M:%SZ" shape (no milliseconds).
 */
function formatCreatedTimestamp( createdAt ) {
  const date = createdAt instanceof Date ? createdAt : new Date( createdAt );
  if( Number.isNaN( date.getTime() ) ) {
    throw new Error( 'Could not create a valid nanopublication preparation timestamp.' );
  }
  return date.toISOString().replace( /\.\d{3}Z$/, 'Z' );
}


/**
 * Build the variable identifier matching the backend's `iadopt-variable-YYYYMMDDTHHMMSS-NN` shape,
 * where the date/time component is derived from the same created timestamp and NN is a 2-digit
 * random number (0-99). An explicit `randomSuffix` can be supplied for deterministic tests.
 *
 * @param   {string}  createdTimestamp  e.g. "2026-06-18T09:47:20Z"
 * @param   {?number} randomSuffix      optional fixed 0-99 value
 * @returns {string}
 */
function buildVariableIdentifier( createdTimestamp, randomSuffix ) {
  // "2026-06-18T09:47:20Z" -> "20260618T094720"
  const compactDateTime = createdTimestamp.replace( /[-:]/g, '' ).replace( /Z$/, '' );
  const number = Number.isInteger( randomSuffix )
    ? Math.min( 99, Math.max( 0, randomSuffix ) )
    : Math.floor( Math.random() * 100 );
  const paddedNumber = String( number ).padStart( 2, '0' );
  return `iadopt-variable-${compactDateTime}-${paddedNumber}`;
}


/**
 * Validate that a Turtle string already carries the required nanopublication metadata on its
 * single iop:Variable. Throws a descriptive error otherwise. Used both internally (post-apply
 * self-check) and by the publish flow as a pre-export gate.
 *
 * @param   {string} inputTurtle
 * @param   {{creatorOrcid:string, conformsToUri:string, createdWithLabel:string}} options
 * @returns {Promise.<{creatorOrcidUri:string, variableUri:string}>}
 */
export async function validatePreNanopubTurtle( inputTurtle, options ) {
  const turtle = String( inputTurtle ?? '' );
  if( !turtle.trim() ) {
    throw new Error( 'Paste or generate Turtle before preparing it for nanopublication.' );
  }

  const creatorOrcidUri = normalizeOrcid( options.creatorOrcid );
  const { store } = await parseRDF( turtle );
  const variableUri = getUniqueVariableUri( store );
  const variable = namedNode( variableUri );
  const required = [
    {
      valid: store.countQuads( variable, namedNode( NS.rdf + 'type' ), FAIR_DIGITAL_OBJECT_CLASS, null ) > 0,
      label: 'fdof:FAIRDigitalObject type',
    },
    {
      valid: store.countQuads( variable, namedNode( TARGET_PREDICATES.conformsTo ), namedNode( options.conformsToUri ), null ) > 0,
      label: 'dct:conformsTo',
    },
    {
      // The identifier value is generated (and random), so only require a non-empty literal.
      valid: store.getQuads( variable, namedNode( TARGET_PREDICATES.identifier ), null, null )
        .some( (quad) => quad.object.termType === 'Literal' && quad.object.value.trim().length > 0 ),
      label: 'dct:identifier',
    },
    {
      valid: store.getQuads( variable, namedNode( TARGET_PREDICATES.created ), null, null )
        .some( (quad) => quad.object.termType === 'Literal' && quad.object.datatype.value === NS.xsd + 'dateTime' ),
      label: 'dct:created',
    },
    {
      valid: store.countQuads( variable, namedNode( TARGET_PREDICATES.creator ), namedNode( creatorOrcidUri ), null ) > 0,
      label: 'dct:creator',
    },
    {
      valid: store.countQuads(
        variable,
        namedNode( TARGET_PREDICATES.createdWith ),
        literal( options.createdWithLabel ),
        null
      ) > 0,
      label: 'pav:createdWith',
    },
    {
      valid: store.countQuads(
        variable,
        namedNode( TARGET_PREDICATES.attributedTo ),
        namedNode( creatorOrcidUri ),
        null
      ) > 0,
      label: 'prov:wasAttributedTo',
    },
  ];
  const missing = required.filter( (item) => !item.valid ).map( (item) => item.label );

  if( missing.length > 0 ) {
    throw new Error(
      `The Turtle is not ready for nanopublication. Apply Pre-Nanopub Settings first. Missing: ${missing.join( ', ' )}.`
    );
  }

  return { creatorOrcidUri, variableUri };
}


/**
 * Enrich pasted/normal I-ADOPT Turtle with the metadata required for nanopublication, preserving
 * all unrelated content (prefixes, comments, blank nodes, labels, constraints, matrix, property,
 * numerator/denominator, and any other statements). Only the single iop:Variable subject is touched,
 * and only its managed predicates are added or updated.
 *
 * @param   {string} inputTurtle
 * @param   {{creatorOrcid:string, conformsToUri:string, createdWithLabel:string, createdAt?:Date, randomSuffix?:number}} options
 * @returns {Promise.<string>} the enriched Turtle
 */
export async function applyPreNanopubSettingsToTurtle( inputTurtle, options ) {
  const turtle = String( inputTurtle ?? '' );
  if( !turtle.trim() ) {
    throw new Error( 'Paste or generate Turtle before applying pre-nanopublication settings.' );
  }
  if( !options?.conformsToUri || !options?.createdWithLabel ) {
    throw new Error( 'Nanopublication preparation settings are unavailable from the backend.' );
  }

  // Validate ORCID up front so a bad value never mutates the Turtle text.
  const creatorOrcidUri = normalizeOrcid( options.creatorOrcid );
  const orcidSuffix = creatorOrcidUri.slice( NS.orcid.length );
  const createdTimestamp = formatCreatedTimestamp( options.createdAt ?? new Date() );
  const variableIdentifier = buildVariableIdentifier( createdTimestamp, options.randomSuffix );

  const { store, prefixes } = await parseRDF( turtle );
  const variableUri = getUniqueVariableUri( store );

  // Resolve (or register) the prefix aliases we need for the managed metadata.
  const prefixAdditions = {};
  const aliases = {
    dct: choosePrefix( prefixes, prefixAdditions, 'dct', NS.dct ),
    fdof: choosePrefix( prefixes, prefixAdditions, 'fdof', NS.fdof ),
    orcid: choosePrefix( prefixes, prefixAdditions, 'orcid', NS.orcid ),
    pav: choosePrefix( prefixes, prefixAdditions, 'pav', NS.pav ),
    prov: choosePrefix( prefixes, prefixAdditions, 'prov', NS.prov ),
    xsd: choosePrefix( prefixes, prefixAdditions, 'xsd', NS.xsd ),
  };
  const desiredObjects = {
    [TARGET_PREDICATES.conformsTo]: `<${options.conformsToUri}>`,
    [TARGET_PREDICATES.identifier]: turtleLiteral( variableIdentifier ),
    [TARGET_PREDICATES.created]: `${turtleLiteral( createdTimestamp )}^^${aliases.xsd}:dateTime`,
    [TARGET_PREDICATES.creator]: `${aliases.orcid}:${orcidSuffix}`,
    [TARGET_PREDICATES.createdWith]: turtleLiteral( options.createdWithLabel ),
    [TARGET_PREDICATES.attributedTo]: `${aliases.orcid}:${orcidSuffix}`,
  };

  // Collect the variable's statement(s) and split each into predicate;object clauses.
  const statementRanges = scanTurtleStatements( turtle );
  const variableStatements = [];

  for( const range of statementRanges ) {
    const statement = turtle.slice( range.start, range.end );
    const subjectToken = readToken( statement );
    if( resolveToken( subjectToken?.value, prefixes ) !== variableUri ) {
      continue;
    }

    const terminalDotIndex = statement.lastIndexOf( '.' );
    const bodyStart = subjectToken.end;
    const body = statement.slice( bodyStart, terminalDotIndex );
    const clauses = scanDelimitedRanges( body, ';' )
      .map( (clauseRange) => body.slice( clauseRange.start, clauseRange.end ) );

    variableStatements.push({
      ... range,
      statement,
      bodyStart,
      terminalDotIndex,
      clauses,
    });
  }

  if( variableStatements.length === 0 ) {
    throw new Error(
      'The iop:Variable was parsed, but its URI statement could not be safely located in the Turtle text.'
    );
  }

  const seenPredicates = new Set();
  const rdfTypeIri = NS.rdf + 'type';
  let fairDigitalObjectAdded = store.countQuads(
    namedNode( variableUri ),
    namedNode( rdfTypeIri ),
    FAIR_DIGITAL_OBJECT_CLASS,
    null
  ) > 0;

  for( const statementInfo of variableStatements ) {
    const nextClauses = [];

    for( const clause of statementInfo.clauses ) {
      const predicateToken = readToken( clause );
      const predicateIri = resolveToken( predicateToken?.value, prefixes );

      // Add fdof:FAIRDigitalObject to the existing type clause (preserving iop:Variable etc.).
      if( predicateIri === rdfTypeIri ) {
        if( !fairDigitalObjectAdded ) {
          nextClauses.push( appendTypeObject( clause, `${aliases.fdof}:FAIRDigitalObject` ) );
          fairDigitalObjectAdded = true;
        } else {
          nextClauses.push( clause );
        }
        continue;
      }

      // Unrelated predicate: keep it untouched.
      if( !(predicateIri in desiredObjects) ) {
        nextClauses.push( clause );
        continue;
      }

      // Managed predicate seen again: drop the duplicate (we already rewrote the first).
      if( seenPredicates.has( predicateIri ) ) {
        continue;
      }

      seenPredicates.add( predicateIri );
      nextClauses.push(
        replaceClauseObject( clause, predicateToken.end, desiredObjects[predicateIri] )
      );
    }

    statementInfo.clauses = nextClauses;
  }

  // Append any managed predicates that were not already present to the first variable statement.
  const firstStatement = variableStatements[0];
  const missingClauses = [
    [ TARGET_PREDICATES.conformsTo, `${aliases.dct}:conformsTo` ],
    [ TARGET_PREDICATES.identifier, `${aliases.dct}:identifier` ],
    [ TARGET_PREDICATES.created, `${aliases.dct}:created` ],
    [ TARGET_PREDICATES.creator, `${aliases.dct}:creator` ],
    [ TARGET_PREDICATES.createdWith, `${aliases.pav}:createdWith` ],
    [ TARGET_PREDICATES.attributedTo, `${aliases.prov}:wasAttributedTo` ],
  ];

  for( const [predicateIri, predicateToken] of missingClauses ) {
    if( seenPredicates.has( predicateIri ) ) {
      continue;
    }
    firstStatement.clauses.push( `\n    ${predicateToken} ${desiredObjects[predicateIri]} ` );
    seenPredicates.add( predicateIri );
  }

  // Rebuild each touched statement from its (possibly edited) clauses; patch from the end so
  // earlier offsets stay valid.
  const patches = variableStatements.map( (statementInfo) => {
    const newBody = statementInfo.clauses.join( ';' );
    const newStatement = (
      statementInfo.statement.slice( 0, statementInfo.bodyStart )
      + newBody
      + statementInfo.statement.slice( statementInfo.terminalDotIndex )
    );
    return {
      start: statementInfo.start,
      end: statementInfo.end,
      replacement: newStatement,
    };
  } );

  let updatedTurtle = turtle;
  for( const patch of patches.sort( (left, right) => right.start - left.start ) ) {
    updatedTurtle = (
      updatedTurtle.slice( 0, patch.start )
      + patch.replacement
      + updatedTurtle.slice( patch.end )
    );
  }

  updatedTurtle = insertPrefixDeclarations( updatedTurtle, prefixAdditions );

  // Self-check: the result must satisfy the same gate the publish flow uses.
  await validatePreNanopubTurtle( updatedTurtle, {
    ... options,
    creatorOrcid: creatorOrcidUri,
  } );

  return updatedTurtle;
}
