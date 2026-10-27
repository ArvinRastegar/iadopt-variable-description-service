import * as React from 'react';
import { CopyIcon, ExternalLinkIcon, PlusIcon, SearchIcon } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Segmented, SegmentedItem } from '@/components/ui/controls';
import { Callout, Pill, Spinner } from '@/components/ui/feedback';
import { Input } from '@/components/ui/fields';
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { type PublishedNanopub } from '@/lib/storage';
import { AuthRequiredError, retractNanopub } from '@/shared/api';
import { useAuth } from '@/shared/auth';
import { OrcidPicker, useOrcidChoice } from '../components/OrcidPicker';
import { usePublications } from '../state/publications';

type Filter = 'all' | 'published' | 'retracted';

interface Target {
  reference: string;
  label: string;
  entry?: PublishedNanopub;
}

function shortNanopub(url: string): string {
  const match = /(RA[\w-]{6})[\w-]*([\w-]{4})$/.exec(url);
  return match ? `w3id.org/np/${match[1]}…${match[2]}` : url;
}

function formatDate(iso?: string): string {
  if (!iso) return '';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

export function PublicationsPage() {
  const auth = useAuth();
  const { entries } = usePublications();
  const [filter, setFilter] = React.useState<Filter>('all');
  const [query, setQuery] = React.useState('');
  const [reference, setReference] = React.useState('');
  const [target, setTarget] = React.useState<Target | null>(null);
  const [lastRetraction, setLastRetraction] = React.useState<{ label: string; url: string } | null>(null);

  const counts = {
    all: entries.length,
    published: entries.filter((entry) => !entry.retractedAt).length,
    retracted: entries.filter((entry) => entry.retractedAt).length,
  };
  const needle = query.trim().toLowerCase();
  const shown = entries.filter((entry) => {
    if (filter === 'published' && entry.retractedAt) return false;
    if (filter === 'retracted' && !entry.retractedAt) return false;
    if (!needle) return true;
    return [entry.label, entry.variableIdentifier, entry.variableUri, entry.nanopubUrl].some((value) => value?.toLowerCase().includes(needle));
  });

  async function openRetract(next: Target) {
    if (await auth.requireUser('retract a nanopublication')) setTarget(next);
  }

  return (
    <main id="main" className="mx-auto flex w-full max-w-[1200px] flex-1 flex-col gap-[22px] px-5 pt-9 pb-16">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1.5">
          <h1 className="m-0 text-[28px] leading-[34px] font-[650] tracking-[-0.025em]">Publications</h1>
          <p className="m-0 text-text-2">Nanopublications published from this browser. Retracting one publishes a public retraction; the original stays readable.</p>
        </div>
        <Button asChild>
          <a href="#/" className="text-primary-foreground no-underline"><PlusIcon aria-hidden="true" />New variable</a>
        </Button>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <Segmented type="single" value={filter} onValueChange={(value) => value && setFilter(value as Filter)} aria-label="Filter by status">
          <SegmentedItem value="all">All <span className="font-medium text-muted-foreground">{counts.all}</span></SegmentedItem>
          <SegmentedItem value="published">Published <span className="font-medium text-muted-foreground">{counts.published}</span></SegmentedItem>
          <SegmentedItem value="retracted">Retracted <span className="font-medium text-muted-foreground">{counts.retracted}</span></SegmentedItem>
        </Segmented>
        <div className="relative w-full max-w-[340px]">
          <SearchIcon className="pointer-events-none absolute top-2.5 left-3 size-4 text-muted-foreground" aria-hidden="true" />
          <label htmlFor="publication-search" className="sr-only">Search publications</label>
          <Input id="publication-search" type="search" placeholder="Search by label or identifier" className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} />
        </div>
      </div>

      <div className="overflow-x-auto rounded-[14px] border border-border bg-surface shadow-xs">
        {shown.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-6 py-14 text-center">
            <div className="font-semibold">{entries.length ? 'Nothing matches' : 'No publications yet'}</div>
            <p className="m-0 max-w-md text-[13px] text-muted-foreground">
              {entries.length ? 'Try another search or filter.' : 'Variables you publish from this browser are listed here, so you can open or retract them later.'}
            </p>
          </div>
        ) : (
          <table className="w-full min-w-[920px] border-collapse text-left">
            <thead>
              <tr className="border-b border-border bg-surface-2 text-xs font-semibold text-muted-foreground">
                <th scope="col" className="px-[18px] py-[11px] font-semibold">Variable</th>
                <th scope="col" className="px-3 py-[11px] font-semibold">Nanopublication</th>
                <th scope="col" className="px-3 py-[11px] font-semibold">Published</th>
                <th scope="col" className="px-3 py-[11px] font-semibold">Status</th>
                <th scope="col" className="px-[18px] py-[11px] text-right font-semibold">Actions</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((entry) => (
                <tr key={entry.nanopubUrl} className="border-b border-border last:border-b-0">
                  <td className="max-w-[360px] px-[18px] py-3">
                    <div className="truncate font-semibold">{entry.label || entry.variableIdentifier}</div>
                    <div className="truncate font-mono text-xs text-muted-foreground">{entry.variableUri || entry.variableIdentifier}</div>
                  </td>
                  <td className="px-3 py-3">
                    <div className="flex items-center gap-1.5">
                      <span className="font-mono text-[12.5px]" title={entry.nanopubUrl}>{shortNanopub(entry.nanopubUrl)}</span>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label="Copy nanopublication link"
                        onClick={async () => {
                          await navigator.clipboard.writeText(entry.nanopubUrl);
                          toast('Link copied');
                        }}
                      >
                        <CopyIcon className="size-3.5" aria-hidden="true" />
                      </Button>
                    </div>
                  </td>
                  <td className="px-3 py-3 text-[13px] whitespace-nowrap text-text-2">{formatDate(entry.savedAt)}</td>
                  <td className="px-3 py-3">
                    {entry.retractedAt ? <Pill dot>Retracted</Pill> : <Pill tone="success" dot>Published</Pill>}
                  </td>
                  <td className="px-[18px] py-3">
                    <div className="flex items-center justify-end gap-1.5">
                      <Button variant="outline" size="sm" asChild>
                        <a href={entry.nanopubUrl} target="_blank" rel="noopener noreferrer" className="text-foreground no-underline">
                          Open <ExternalLinkIcon className="size-3.5" aria-hidden="true" />
                        </a>
                      </Button>
                      {entry.retractedAt && entry.retractionUrl ? (
                        <a
                          href={entry.retractionUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          title="Open the retraction nanopublication"
                          className="flex items-center gap-1 px-1 text-xs whitespace-nowrap text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                        >
                          Retracted {formatDate(entry.retractedAt)} <ExternalLinkIcon className="size-3" aria-hidden="true" />
                        </a>
                      ) : entry.retractedAt ? (
                        <span className="px-1 text-xs whitespace-nowrap text-muted-foreground">Retracted {formatDate(entry.retractedAt)}</span>
                      ) : (
                        <Button
                          variant="outline"
                          size="sm"
                          className="text-destructive-soft-foreground hover:border-destructive-line hover:bg-destructive-soft"
                          onClick={() => void openRetract({ reference: entry.nanopubUrl, label: entry.label || entry.variableIdentifier, entry })}
                        >
                          Retract
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <section aria-labelledby="external-heading" className="grid grid-cols-[repeat(auto-fit,minmax(min(420px,100%),1fr))] items-center gap-4 rounded-[14px] border border-border bg-surface px-5 py-[18px]">
        <div className="flex flex-col gap-1">
          <h2 id="external-heading" className="m-0 text-[15px] leading-[22px] font-semibold">Retract one published elsewhere</h2>
          <p className="m-0 text-[13px] text-muted-foreground">Paste a w3id.org/np/RA… link, a raw RA identifier or a Nanodash explore link. It can only be retracted if it was signed with this service’s key.</p>
        </div>
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const value = reference.trim();
            if (value) void openRetract({ reference: value, label: value });
          }}
        >
          <label htmlFor="retract-reference" className="sr-only">Nanopublication reference to retract</label>
          <Input id="retract-reference" placeholder="https://w3id.org/np/RA…" className="font-mono text-[13px]" value={reference} onChange={(event) => setReference(event.target.value)} />
          <Button type="submit" variant="outline" disabled={!reference.trim()}>Retract…</Button>
        </form>
        {lastRetraction && (
          <Callout tone="success" title={`Retracted ${lastRetraction.label}`} className="col-span-full">
            The retraction is published:{' '}
            <a href={lastRetraction.url} target="_blank" rel="noopener noreferrer" className="font-mono break-all underline">{lastRetraction.url}</a>
          </Callout>
        )}
      </section>

      <RetractSheet
        target={target}
        onClose={() => setTarget(null)}
        onDone={(retraction) => {
          // Kept on the page, as before: a pasted reference has no row that could show it.
          setLastRetraction(retraction);
          if (target && !target.entry) setReference('');
          setTarget(null);
        }}
      />
    </main>
  );
}

function RetractSheet({
  target,
  onClose,
  onDone,
}: {
  target: Target | null;
  onClose: () => void;
  onDone: (retraction: { label: string; url: string }) => void;
}) {
  const auth = useAuth();
  const publications = usePublications();
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  // The publication's own creator iD is preselected when it is one of the saved iDs.
  const orcid = useOrcidChoice(target ? target.reference : null, 'Please put in your ORCID for the retraction.', async () => target?.entry?.creatorOrcid ?? null);

  React.useEffect(() => {
    if (target) setError(null);
  }, [target]);

  async function confirm() {
    const creatorOrcid = orcid.require();
    if (!target || !creatorOrcid) return;
    setBusy(true);
    setError(null);
    try {
      const result = await auth.runProtected(() => retractNanopub(target.reference, creatorOrcid), 'retract a nanopublication');
      if (!result.retraction_url) throw new Error('Backend did not return the retraction nanopub URL.');
      void orcid.remember(creatorOrcid);
      publications.markRetracted([result.retracted_nanopub_url, target.reference], {
        retractionUrl: result.retraction_url,
        retractedAt: new Date().toISOString(),
      });
      toast(`Retracted ${target.label}`, {
        action: { label: 'Open', onClick: () => window.open(result.retraction_url, '_blank', 'noopener,noreferrer') },
      });
      onDone({ label: target.label, url: result.retraction_url });
    } catch (caught) {
      console.error(caught);
      if (caught instanceof AuthRequiredError) {
        onClose();
        return;
      }
      // Show the backend detail, so a key mismatch and a registry rejection can be told apart.
      setError(caught instanceof Error ? caught.message : 'Nanopub retract failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Sheet open={target !== null} onOpenChange={(open) => !open && !busy && onClose()}>
      <SheetContent aria-describedby="retract-description">
        <SheetHeader>
          <SheetTitle>Retract this nanopublication?</SheetTitle>
          <SheetDescription id="retract-description">This publishes a retraction signed with the service’s key.</SheetDescription>
        </SheetHeader>
        <div className="flex flex-1 flex-col gap-[18px] overflow-y-auto px-6 py-5">
          {target && (
            <div className="flex flex-col gap-1.5 rounded-xl border border-border bg-background p-3.5">
              <span className="font-semibold break-words">{target.label}</span>
              <span className="font-mono text-xs break-all text-muted-foreground">{target.reference}</span>
              {target.entry && <span className="text-xs text-muted-foreground">Published {formatDate(target.entry.savedAt)}</span>}
            </div>
          )}
          <OrcidPicker choice={orcid} idPrefix="retract" hint="Recorded as the creator of the retraction." disabled={busy} />
          <Callout tone="danger" title="A retraction cannot be undone">
            It is a new, public nanopublication that marks this one as retracted. Tools that respect retractions stop showing the variable; the original stays readable.
          </Callout>
          {error && <Callout tone="danger" title="Retraction failed">{error}</Callout>}
        </div>
        <SheetFooter>
          <Button variant="ghost" disabled={busy} onClick={onClose}>Keep it</Button>
          <Button variant="destructive" disabled={busy} onClick={() => void confirm()}>
            {busy && <Spinner />}
            {busy ? 'Retracting…' : 'Publish retraction'}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
