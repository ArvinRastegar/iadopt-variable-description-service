import * as React from 'react';

import {
  markRetracted as storeRetracted,
  PUBLISHED_NANOPUBS_KEY,
  readPublished,
  rememberPublished,
  type PublishedNanopub,
} from '@/lib/storage';

/*
 * Nanopublications published from this browser. They are kept in browser storage under the same
 * key as before, so publications saved by the previous frontend appear here too.
 */

interface PublicationsValue {
  entries: PublishedNanopub[];
  remember: (entry: PublishedNanopub) => void;
  markRetracted: (urls: string[], retraction: { retractionUrl: string; retractedAt: string }) => void;
}

const PublicationsContext = React.createContext<PublicationsValue | null>(null);

export function usePublications(): PublicationsValue {
  const value = React.useContext(PublicationsContext);
  if (!value) throw new Error('usePublications must be used inside PublicationsProvider');
  return value;
}

export function PublicationsProvider({ children }: { children: React.ReactNode }) {
  const [entries, setEntries] = React.useState<PublishedNanopub[]>(() => readPublished());

  React.useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === PUBLISHED_NANOPUBS_KEY) setEntries(readPublished());
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  // Changes start from the list on the page, not from storage, so entries of this session stay
  // listed when storage is blocked or full. Other tabs' changes arrive through `storage` events.
  const value = React.useMemo<PublicationsValue>(() => ({
    entries,
    remember: (entry) => setEntries(rememberPublished(entry, entries)),
    markRetracted: (urls, retraction) => setEntries(storeRetracted(urls, retraction, entries)),
  }), [entries]);

  return <PublicationsContext.Provider value={value}>{children}</PublicationsContext.Provider>;
}
