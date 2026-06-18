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
  rdfs: 'http://www.w3.org/2000/01/rdf-schema#',
  xsd: 'http://www.w3.org/2001/XMLSchema#',
};

const VARIABLE_CLASS = namedNode( NS.iop + 'Variable' );
const FAIR_DIGITAL_OBJECT_CLASS = namedNode( NS.fdof + 'FAIRDigitalObject' );

const RDF_TYPE = NS.rdf + 'type';
const RDFS_LABEL = NS.rdfs + 'label';

// System classes and the component predicates that make up a system, in canonical order:
// first part (numerator/source) before second part (denominator/target); symmetric parts in order.
const SYSTEM_CLASSES = [ NS.iop + 'AsymmetricSystem', NS.iop + 'SymmetricSystem' ];
const SYSTEM_COMPONENT_PREDICATES = [
  NS.iop + 'hasNumerator',
  NS.iop + 'hasSource',
  NS.iop + 'hasDenominator',
  NS.iop + 'hasTarget',
  NS.iop + 'hasPart',
];

// Resolvable namespace for minted variable URIs (w3id redirects). The variable subject and its
// dct:identifier must both live under this base so the published identifier always resolves.
const IADOPT_VARIABLE_BASE = 'https://w3id.org/iadopt/variable/';

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
 * Build the identifier suffix matching the backend's `YYYYMMDDTHHMMSS-NN` shape, where the date/time
 * component is derived from the same created timestamp and NN is a 2-digit random number (0-99).
 * An explicit `randomSuffix` can be supplied for deterministic tests.
 *
 * @param   {string}  createdTimestamp  e.g. "2026-06-18T09:47:20Z"
 * @param   {?number} randomSuffix      optional fixed 0-99 value
 * @returns {string}                    e.g. "20260618T094720-86"
 */
function buildIdentifierSuffix( createdTimestamp, randomSuffix ) {
  // "2026-06-18T09:47:20Z" -> "20260618T094720"
  const compactDateTime = createdTimestamp.replace( /[-:]/g, '' ).replace( /Z$/, '' );
  const number = Number.isInteger( randomSuffix )
    ? Math.min( 99, Math.max( 0, randomSuffix ) )
    : Math.floor( Math.random() * 100 );
  const paddedNumber = String( number ).padStart( 2, '0' );
  return `${compactDateTime}-${paddedNumber}`;
}


/**
 * Whether a variable subject is already a resolvable minted variable URI of the form
 * `https://w3id.org/iadopt/variable/<datetime>-<NN>`. Such subjects are kept as-is; anything
 * else (example.org, urn:, blank-derived, etc.) is replaced with a freshly minted resolvable URI.
 *
 * @param   {string}  uri
 * @returns {boolean}
 */
function isResolvableVariableUri( uri ) {
  if( !uri.startsWith( IADOPT_VARIABLE_BASE ) ) {
    return false;
  }
  const suffix = uri.slice( IADOPT_VARIABLE_BASE.length );
  return /^\d{8}T\d{6}-\d{2}$/.test( suffix );
}


/**
 * Replace every whole term (an <IRI> or a prefixed name) that resolves to `fromUri` with
 * `replacementText`, while skipping comments, string literals and the insides of unrelated IRIs.
 * This is how a non-resolvable variable subject (e.g. <http://example.org/Temperature> or ex:Temp)
 * is renamed to the minted resolvable URI everywhere it occurs (subject and any object references),
 * without disturbing surrounding formatting or unrelated tokens.
 *
 * @param   {string}                    text
 * @param   {Object.<string,string>}    prefixes
 * @param   {string}                    fromUri
 * @param   {string}                    replacementText  e.g. "<https://w3id.org/iadopt/variable/...>"
 * @returns {string}
 */
function rewriteTermOccurrences( text, prefixes, fromUri, replacementText ) {
  const replacements = [];
  let comment = false;
  let quote = null;
  let tripleQuote = false;
  let escaped = false;

  for( let index = 0; index < text.length; index += 1 ) {
    const char = text[index];

    if( comment ) {
      if( char === '\n' || char === '\r' ) {
        comment = false;
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
    if( char === '"' || char === '\'' ) {
      quote = char;
      tripleQuote = text.slice( index, index + 3 ) === char.repeat( 3 );
      if( tripleQuote ) {
        index += 2;
      }
      continue;
    }

    // A term begins at an '<' (IRI) or a non-delimiter, non-whitespace character (prefixed name / `a`).
    if( char === '<' || !/[\s;,.[\](){}<>"']/.test( char ) ) {
      const token = readToken( text, index );
      if( token ) {
        if( resolveToken( token.value, prefixes ) === fromUri ) {
          replacements.push({ start: token.start, end: token.end });
        }
        // Skip past the whole token so its inner characters are never re-scanned.
        index = token.end - 1;
        continue;
      }
    }
  }

  let updated = text;
  for( const range of replacements.sort( (left, right) => right.start - left.start ) ) {
    updated = updated.slice( 0, range.start ) + replacementText + updated.slice( range.end );
  }
  return updated;
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
      // The variable subject must be a resolvable minted w3id URI so the published identifier resolves.
      valid: isResolvableVariableUri( variableUri ),
      label: 'resolvable variable URI (https://w3id.org/iadopt/variable/<datetime>-NN)',
    },
    {
      valid: store.countQuads( variable, namedNode( NS.rdf + 'type' ), FAIR_DIGITAL_OBJECT_CLASS, null ) > 0,
      label: 'fdof:FAIRDigitalObject type',
    },
    {
      valid: store.countQuads( variable, namedNode( TARGET_PREDICATES.conformsTo ), namedNode( options.conformsToUri ), null ) > 0,
      label: 'dct:conformsTo',
    },
    {
      // dct:identifier must be the resolvable variable IRI itself (not a string literal).
      valid: store.countQuads( variable, namedNode( TARGET_PREDICATES.identifier ), variable, null ) > 0,
      label: 'dct:identifier (resolvable IRI)',
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
 * Extract the plain string value of a Turtle literal object (e.g. `"salt"`, `"salt"@en`,
 * `"""multi"""`, `'x'^^xsd:string`). Returns null when the object is not a literal.
 * @param   {string} objectText
 * @returns {?string}
 */
function readLiteralValue( objectText ) {
  const text = objectText.trim();
  const quote = text[0];
  if( quote !== '"' && quote !== '\'' ) {
    return null;
  }

  const triple = text.slice( 0, 3 ) === quote.repeat( 3 );
  const open = triple ? 3 : 1;
  let end = -1;

  for( let index = open; index < text.length; index += 1 ) {
    if( text[index] === '\\' ) {
      index += 1;
      continue;
    }
    if( triple ) {
      if( text.slice( index, index + 3 ) === quote.repeat( 3 ) ) {
        end = index;
        break;
      }
    } else if( text[index] === quote ) {
      end = index;
      break;
    }
  }

  if( end < 0 ) {
    return null;
  }

  return text
    .slice( open, end )
    .replace( /\\(["'\\ntr])/g, (match, char) => (
      char === 'n' ? '\n' : char === 't' ? '\t' : char === 'r' ? '\r' : char
    ) );
}


/**
 * Parse a single Turtle statement into its subject token and `predicate object` clauses.
 * @param   {string} statement
 * @returns {?{subjectToken:object, terminalDotIndex:number, clauses:string[]}}
 */
function parseStatement( statement ) {
  const subjectToken = readToken( statement );
  if( !subjectToken ) {
    return null;
  }
  const terminalDotIndex = statement.lastIndexOf( '.' );
  const body = statement.slice( subjectToken.end, terminalDotIndex );
  const clauses = scanDelimitedRanges( body, ';' )
    .map( (range) => body.slice( range.start, range.end ) );
  return { subjectToken, terminalDotIndex, clauses };
}


/**
 * Read the object tokens of a clause's object list (handles comma-separated objects).
 * @param   {string} objectText  text following the predicate
 * @returns {string[]}           raw object token strings
 */
function readObjectTokens( objectText ) {
  return scanDelimitedRanges( objectText, ',' )
    .map( (range) => readToken( objectText.slice( range.start, range.end ) )?.value )
    .filter( Boolean );
}


/**
 * Write a derived rdfs:label onto every system node (iop:AsymmetricSystem / iop:SymmetricSystem)
 * that does not already carry one. The label is built from the system's component labels in
 * canonical order — first part (numerator/source) then second part (denominator/target), or the
 * parts in order for symmetric systems — e.g. "salt water", "oxygen water".
 *
 * The label is written into the Turtle text itself so the visualization and the published
 * nanopublication both read the same label (the text stays the single source of truth). Systems
 * that already have a label, or whose components have no resolvable labels, are left untouched.
 *
 * @param   {string}                  turtle
 * @param   {Object.<string,string>}  prefixes
 * @param   {string}                  rdfsAlias  prefix alias to use when writing rdfs:label
 * @returns {string}
 */
function applySystemLabels( turtle, prefixes, rdfsAlias ) {
  const statements = scanTurtleStatements( turtle )
    .map( (range) => {
      const statement = turtle.slice( range.start, range.end );
      const info = parseStatement( statement );
      return info ? { range, ...info } : null;
    } )
    .filter( Boolean );

  const keyOf = (tokenValue) => resolveToken( tokenValue, prefixes ) || tokenValue;

  // Pass 1: map each subject (resolved IRI, or raw token for blank nodes) to its rdfs:label value.
  const labelByKey = new Map();
  for( const stmt of statements ) {
    const subjectKey = keyOf( stmt.subjectToken.value );
    for( const clause of stmt.clauses ) {
      const predToken = readToken( clause );
      if( resolveToken( predToken?.value, prefixes ) === RDFS_LABEL ) {
        const value = readLiteralValue( clause.slice( predToken.end ) );
        if( value != null && !labelByKey.has( subjectKey ) ) {
          labelByKey.set( subjectKey, value );
        }
      }
    }
  }

  // Pass 2: for each labelless system statement, derive a label from its components and append it.
  const patches = [];
  for( const stmt of statements ) {
    let isSystem = false;
    let hasLabel = false;
    const componentsByPredicate = new Map();

    for( const clause of stmt.clauses ) {
      const predToken = readToken( clause );
      const predIri = resolveToken( predToken?.value, prefixes );
      const objectText = clause.slice( predToken?.end ?? 0 );

      if( predIri === RDF_TYPE ) {
        if( readObjectTokens( objectText ).some( (token) => SYSTEM_CLASSES.includes( resolveToken( token, prefixes ) ) ) ) {
          isSystem = true;
        }
      } else if( predIri === RDFS_LABEL ) {
        hasLabel = true;
      } else if( SYSTEM_COMPONENT_PREDICATES.includes( predIri ) ) {
        componentsByPredicate.set( predIri, readObjectTokens( objectText ) );
      }
    }

    if( !isSystem || hasLabel ) {
      continue;
    }

    const parts = [];
    for( const predIri of SYSTEM_COMPONENT_PREDICATES ) {
      for( const token of componentsByPredicate.get( predIri ) ?? [] ) {
        const label = labelByKey.get( keyOf( token ) );
        if( label ) {
          parts.push( label );
        }
      }
    }

    if( parts.length < 1 ) {
      continue;
    }

    patches.push({
      at: stmt.range.start + stmt.terminalDotIndex,
      text: `;\n    ${rdfsAlias}:label ${turtleLiteral( parts.join( ' ' ) )} `,
    });
  }

  let updated = turtle;
  for( const patch of patches.sort( (left, right) => right.at - left.at ) ) {
    updated = updated.slice( 0, patch.at ) + patch.text + updated.slice( patch.at );
  }
  return updated;
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

  const { store, prefixes } = await parseRDF( turtle );
  const variableUri = getUniqueVariableUri( store );

  // The published variable must carry a resolvable w3id URI. If the pasted subject already is one,
  // keep it; otherwise mint `https://w3id.org/iadopt/variable/<datetime>-<NN>` and rename the old
  // subject everywhere it occurs at the end of the transform.
  const mintNewUri = !isResolvableVariableUri( variableUri );
  const mintedVariableUri = mintNewUri
    ? `${IADOPT_VARIABLE_BASE}${buildIdentifierSuffix( createdTimestamp, options.randomSuffix )}`
    : variableUri;

  // Resolve (or register) the prefix aliases we need for the managed metadata.
  const prefixAdditions = {};
  const aliases = {
    dct: choosePrefix( prefixes, prefixAdditions, 'dct', NS.dct ),
    fdof: choosePrefix( prefixes, prefixAdditions, 'fdof', NS.fdof ),
    orcid: choosePrefix( prefixes, prefixAdditions, 'orcid', NS.orcid ),
    pav: choosePrefix( prefixes, prefixAdditions, 'pav', NS.pav ),
    prov: choosePrefix( prefixes, prefixAdditions, 'prov', NS.prov ),
    rdfs: choosePrefix( prefixes, prefixAdditions, 'rdfs', NS.rdfs ),
    xsd: choosePrefix( prefixes, prefixAdditions, 'xsd', NS.xsd ),
  };
  const desiredObjects = {
    [TARGET_PREDICATES.conformsTo]: `<${options.conformsToUri}>`,
    // The identifier is the resolvable variable IRI itself (matches the minted subject).
    [TARGET_PREDICATES.identifier]: `<${mintedVariableUri}>`,
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

  // Rename a non-resolvable subject to the minted resolvable URI everywhere it occurs (subject
  // statements and any object references), so the whole graph points at the resolvable identifier.
  if( mintNewUri ) {
    updatedTurtle = rewriteTermOccurrences(
      updatedTurtle,
      prefixes,
      variableUri,
      `<${mintedVariableUri}>`
    );
  }

  // Write a derived rdfs:label onto any system node that lacks one, so the published nanopub and
  // the visualization (which reads the actual TTL) both carry the same first-part + second-part label.
  updatedTurtle = applySystemLabels( updatedTurtle, prefixes, aliases.rdfs );

  // Self-check: the result must satisfy the same gate the publish flow uses.
  await validatePreNanopubTurtle( updatedTurtle, {
    ... options,
    creatorOrcid: creatorOrcidUri,
  } );

  return updatedTurtle;
}
