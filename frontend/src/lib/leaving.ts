/*
 * Leaving the page for ROHub and coming back. The work goes into this tab's own storage first, beside
 * the draft that every tab of the site shares, so another tab cannot replace it meanwhile; it is used
 * once, when the page loads in this tab again soon after.
 */

/** Going to another address, in one place. */
export const navigation = {
  leave(address: string): void {
    window.location.assign(address);
  },
};

/** The backend's route that starts a ROHub sign-in and comes back to a page of the application. */
export function rohubSignInAddress(target: string): string {
  return `/api/auth/login?redirect=${encodeURIComponent(target)}`;
}

/** ROHub's sign-out page, through the backend. */
export const ROHUB_SIGN_OUT_ADDRESS = '/api/auth/end-session';

/* -------------------------------- The tab's copy ------------------------------- */

const TAB_COPY_KEY = 'iadopt-workspace-tab-copy';
const TAB_COPY_MINUTES = 15;

/** Keep the work in this tab; whether it is really there. */
export function saveTabCopy(work: unknown): boolean {
  try {
    const text = JSON.stringify({ work, leftAt: Date.now() });
    window.sessionStorage.setItem(TAB_COPY_KEY, text);
    return window.sessionStorage.getItem(TAB_COPY_KEY) === text;
  } catch {
    return false;
  }
}

/** The work this tab left with, while it is fresh. Reading does not remove it; see `dropTabCopy`. */
export function readTabCopy<T>(): T | null {
  try {
    const copy = JSON.parse(window.sessionStorage.getItem(TAB_COPY_KEY) ?? 'null') as { work?: T; leftAt?: unknown } | null;
    if (!copy || typeof copy.leftAt !== 'number' || !copy.work) return null;
    const age = Date.now() - copy.leftAt;
    return age >= 0 && age <= TAB_COPY_MINUTES * 60_000 ? copy.work : null;
  } catch {
    return null;
  }
}

export function dropTabCopy(): void {
  try {
    window.sessionStorage.removeItem(TAB_COPY_KEY);
  } catch {
    // Without storage there is no copy either.
  }
}

/* -------------------------------- The page's work ------------------------------ */

/** What a part of the page does with its work when the person leaves or clears the browser. */
export interface Work {
  /** Save it in this tab; false when it could not be saved. */
  save?: () => boolean;
  /** Empty it on the page, delete what this browser keeps of it, and save nothing more of it. */
  clear: () => void;
}

const works = new Set<Work>();

/** Announce a part's work; returns the function that takes it back. */
export function registerWork(work: Work): () => void {
  works.add(work);
  return () => {
    works.delete(work);
  };
}

/** Save all work in this tab; whether nothing would be lost by leaving. */
export function saveWork(): boolean {
  return [...works].map((work) => work.save?.() ?? true).every(Boolean);
}

/** Empty the page's work and delete what this browser keeps of it. */
export function clearWork(): void {
  for (const work of [...works]) work.clear();
  dropTabCopy();
}
