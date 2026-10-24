/*
 * Browser storage used by the app. Every read and write is guarded: storage can be missing or
 * throw (private windows, blocked site data), and the app must keep working without it.
 */

/** Kept from the previous frontend so publications saved by it still show up. */
export const PUBLISHED_NANOPUBS_KEY = 'iadopt-published-nanopubs';
export const WORKSPACE_DRAFT_KEY = 'iadopt-workspace-draft';
export const THEME_KEY = 'iadopt-theme';
export const REMEMBERED_ORCID_KEY = 'iadopt-creator-orcid';

export function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

export function writeJson(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage is a convenience here; failing to save must not break the page.
  }
}

export function readString(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeString(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // See writeJson.
  }
}

/* ------------------------- Published nanopublications ------------------------- */

/**
 * One nanopublication published from this browser. The first five fields are the shape the
 * previous frontend stored; the retraction fields are new and optional.
 */
export interface PublishedNanopub {
  variableIdentifier: string;
  variableUri: string;
  nanopubUrl: string;
  publishedTo: string;
  savedAt: string;
  label?: string;
  creatorOrcid?: string;
  retractedAt?: string;
  retractionUrl?: string;
}

export function readPublished(): PublishedNanopub[] {
  const value = readJson<unknown>(PUBLISHED_NANOPUBS_KEY, []);
  if (!Array.isArray(value)) return [];
  return value.filter(
    (entry): entry is PublishedNanopub =>
      Boolean(entry) && typeof entry === 'object' && typeof (entry as PublishedNanopub).nanopubUrl === 'string',
  );
}

export function writePublished(entries: PublishedNanopub[]): void {
  writeJson(PUBLISHED_NANOPUBS_KEY, entries);
}

/**
 * Store a new publication first in the list, replacing an older entry with the same URL.
 * `current` is the list the page shows; it stays complete when storage cannot be read.
 */
export function rememberPublished(entry: PublishedNanopub, current: PublishedNanopub[] = readPublished()): PublishedNanopub[] {
  const entries = [entry, ...current.filter((item) => item.nanopubUrl !== entry.nanopubUrl)];
  writePublished(entries);
  return entries;
}

/** Mark a publication as retracted, keeping it in the list so its history stays visible. */
export function markRetracted(
  nanopubUrls: string[],
  retraction: { retractionUrl: string; retractedAt: string },
  current: PublishedNanopub[] = readPublished(),
): PublishedNanopub[] {
  const targets = new Set(nanopubUrls.filter(Boolean));
  const entries = current.map((item) =>
    targets.has(item.nanopubUrl) ? { ...item, ...retraction } : item,
  );
  writePublished(entries);
  return entries;
}
