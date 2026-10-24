import type { Kind } from './kinds';

export interface Term {
  label: string;
  kind: Kind;
}

export interface Segment {
  text: string;
  kind?: Kind;
}

function isWordChar(char: string | undefined): boolean {
  return Boolean(char) && /[\p{L}\p{N}]/u.test(char as string);
}

/**
 * Split a definition into plain and highlighted segments, marking where component labels occur.
 * Matching ignores case, needs word boundaries, prefers longer labels and never overlaps.
 * Labels that do not occur in the text are simply not highlighted.
 */
export function findHighlights(text: string, terms: Term[]): Segment[] {
  if (!text) return [];
  const lower = text.toLocaleLowerCase();
  const taken: Array<{ start: number; end: number; kind: Kind }> = [];

  const candidates = terms
    .map((term) => ({ ...term, needle: term.label.trim().toLocaleLowerCase() }))
    .filter((term) => term.needle.length > 0)
    .sort((a, b) => b.needle.length - a.needle.length);

  for (const term of candidates) {
    let from = 0;
    while (from <= lower.length) {
      const start = lower.indexOf(term.needle, from);
      if (start === -1) break;
      const end = start + term.needle.length;
      const bounded = !isWordChar(lower[start - 1]) && !isWordChar(lower[end]);
      const overlaps = taken.some((range) => start < range.end && end > range.start);
      if (bounded && !overlaps) {
        taken.push({ start, end, kind: term.kind });
        break;
      }
      from = start + 1;
    }
  }

  taken.sort((a, b) => a.start - b.start);
  const segments: Segment[] = [];
  let cursor = 0;
  for (const range of taken) {
    if (range.start > cursor) segments.push({ text: text.slice(cursor, range.start) });
    segments.push({ text: text.slice(range.start, range.end), kind: range.kind });
    cursor = range.end;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor) });
  return segments;
}
