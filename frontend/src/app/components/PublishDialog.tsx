import * as React from 'react';
import { AlertTriangleIcon, CheckIcon, CircleCheckIcon, CopyIcon, ExternalLinkIcon, InfoIcon } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/controls';
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Callout, Spinner } from '@/components/ui/feedback';
import { Input, Label } from '@/components/ui/fields';
import { applyCreatorOrcidToTurtle, normalizeOrcid, readCreatorOrcidFromTurtle, validatePreNanopubTurtle } from '@/lib/applyPreNanopubSettingsToTurtle.js';
import { readString, REMEMBERED_ORCID_KEY, writeString } from '@/lib/storage';
import { cn } from '@/lib/utils';
import { AuthRequiredError, publishNanopub, type PublishResult } from '@/shared/api';
import { useAuth } from '@/shared/auth';
import { navigate } from '../router';
import { usePublications } from '../state/publications';
import { useWorkspace, variableTitle } from '../state/workspace';
import { needsLink } from './workspace/panels';

type Step = 'check' | 'creator' | 'confirm' | 'publishing' | 'done';

export function OrcidMark({ className }: { className?: string }) {
  return (
    <span aria-hidden="true" className={cn('inline-flex size-5 shrink-0 items-center justify-center rounded-full bg-[#A6CE39] text-[10px] font-bold tracking-tight text-white', className)}>
      iD
    </span>
  );
}

/** Checks an ORCID iD as it is typed; the messages are the ones the previous page showed. */
export function orcidStatus(value: string): { uri: string | null; error: string | null } {
  if (!value.trim()) return { uri: null, error: null };
  try {
    return { uri: normalizeOrcid(value), error: null };
  } catch (error) {
    return { uri: null, error: error instanceof Error ? error.message : String(error) };
  }
}

export function usePublishDialog() {
  const auth = useAuth();
  const [open, setOpen] = React.useState(false);
  return {
    open,
    setOpen,
    request: async () => {
      if (await auth.requireUser('publish a nanopublication')) setOpen(true);
    },
  };
}

export function PublishDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const auth = useAuth();
  const workspace = useWorkspace();
  const publications = usePublications();
  const [step, setStep] = React.useState<Step>('check');
  const [missing, setMissing] = React.useState<string[] | null>(null);
  const [checkError, setCheckError] = React.useState<string | null>(null);
  const [orcid, setOrcid] = React.useState('');
  const [touched, setTouched] = React.useState(false);
  const [remember, setRemember] = React.useState(false);
  const [agree, setAgree] = React.useState(false);
  const [publishError, setPublishError] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<PublishResult | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [applying, setApplying] = React.useState(false);

  const settings = workspace.preparationOptions
    ? { conformsToUri: workspace.preparationOptions.conforms_to_uri, createdWithLabel: workspace.preparationOptions.created_with_label }
    : null;

  // Every time the dialog opens: start over and check the metadata of the visible Turtle.
  React.useEffect(() => {
    if (!open) return;
    setStep('check');
    setAgree(false);
    setPublishError(null);
    setResult(null);
    setTouched(false);
    const remembered = readString(REMEMBERED_ORCID_KEY);
    setRemember(Boolean(remembered));
    setOrcid(remembered?.replace(/^https?:\/\/orcid\.org\//i, '') ?? '');
    if (!remembered) {
      readCreatorOrcidFromTurtle(workspace.turtle)
        .then((uri: string | null) => uri && setOrcid(uri.replace(/^https?:\/\/orcid\.org\//i, '')))
        .catch(() => {});
    }
    // eslint-style exhaustive deps are not wanted here: this runs once per opening.
  }, [open]); // eslint-disable-line

  React.useEffect(() => {
    if (!open || step !== 'check') return;
    // No result from an earlier opening or an earlier Turtle may show while this check runs.
    setMissing(null);
    if (!settings) {
      setCheckError(workspace.preparationError || 'Nanopublication preparation settings could not be loaded from the backend.');
      return;
    }
    setCheckError(null);
    let stale = false;
    validatePreNanopubTurtle(workspace.turtle, { ...settings, requireCreator: false })
      .then(() => !stale && setMissing([]))
      .catch((error: Error) => {
        if (stale) return;
        const match = /Missing: (.*)\.$/.exec(error.message);
        if (match) setMissing(match[1].split(', '));
        else setCheckError(error.message);
      });
    return () => {
      stale = true;
    };
  }, [open, step, workspace.turtle, settings?.conformsToUri, settings?.createdWithLabel]); // eslint-disable-line

  const total = workspace.components.length;
  const unchecked = total - workspace.checkedCount;
  const unlinked = workspace.components.filter(needsLink);
  const orcidState = orcidStatus(orcid);
  const orcidError = orcidState.error ?? (touched && !orcid.trim() ? 'Please put in your ORCID for nanopublication.' : null);
  const canContinueCheck = !workspace.turtleDirty && missing !== null && missing.length === 0 && !checkError;

  async function publish() {
    if (!settings || !orcidState.uri) return;
    setBusy(true);
    setPublishError(null);
    setStep('publishing');
    try {
      const before = workspace.turtle.trim();
      const creatorSettings = { ...settings, creatorOrcid: orcidState.uri };
      // Write the ORCID into the visible Turtle so what is published is what the user sees.
      const ttl = (await applyCreatorOrcidToTurtle(before, creatorSettings)).trim();
      const prepared = await validatePreNanopubTurtle(ttl, creatorSettings);
      if (ttl !== before) workspace.replaceTurtleQuietly(ttl);

      const published = await auth.runProtected(() => publishNanopub(ttl, prepared.creatorOrcidUri ?? orcidState.uri as string), 'publish a nanopublication');
      if (!published.nanopub_url) throw new Error('Backend did not return the nanopub URL.');
      publications.remember({
        variableIdentifier: published.variable_identifier || published.variable_uri || published.nanopub_url,
        variableUri: published.variable_uri || '',
        nanopubUrl: published.nanopub_url,
        publishedTo: published.published_to || '',
        savedAt: new Date().toISOString(),
        label: variableTitle(workspace.variable),
        creatorOrcid: prepared.creatorOrcidUri ?? undefined,
      });
      writeString(REMEMBERED_ORCID_KEY, remember ? orcidState.uri : null);
      setResult(published);
      setStep('done');
    } catch (error) {
      console.error(error);
      if (error instanceof AuthRequiredError) {
        onOpenChange(false);
        return;
      }
      setPublishError(error instanceof Error ? error.message : 'Nanopub publish failed.');
      setStep('confirm');
    } finally {
      setBusy(false);
    }
  }

  const steps: Array<{ id: Step; label: string }> = [
    { id: 'check', label: 'Check' },
    { id: 'creator', label: 'Creator' },
    { id: 'confirm', label: 'Confirm' },
  ];
  const stepIndex = steps.findIndex((item) => item.id === step);

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="max-w-[640px]">
        <DialogHeader>
          <DialogTitle>{step === 'done' ? 'Published' : 'Publish as a nanopublication'}</DialogTitle>
          <DialogDescription>
            {step === 'check' && 'Step 1 of 3 · Check the draft'}
            {step === 'creator' && 'Step 2 of 3 · Say who created it'}
            {step === 'confirm' && 'Step 3 of 3 · Confirm'}
            {step === 'publishing' && 'Signing and sending, this takes a few seconds'}
            {step === 'done' && 'Signed and sent to the nanopublication network'}
          </DialogDescription>
        </DialogHeader>

        {step !== 'done' && step !== 'publishing' && (
          <ol aria-label="Steps" className="m-0 mx-6 mt-4 grid list-none grid-cols-3 gap-2 p-0">
            {steps.map((item, index) => (
              <li key={item.id} className="flex flex-col gap-2" aria-current={index === stepIndex ? 'step' : undefined}>
                <span aria-hidden="true" className={cn('h-1 rounded-full', index <= stepIndex ? 'bg-primary' : 'bg-surface-3')} />
                <span className={cn('flex items-center gap-1.5 text-xs font-semibold', index <= stepIndex ? 'text-foreground' : 'text-muted-foreground')}>
                  {index < stepIndex && <CheckIcon className="size-3" strokeWidth={3} aria-hidden="true" />}
                  {item.label}
                </span>
              </li>
            ))}
          </ol>
        )}

        <DialogBody className="flex flex-col gap-4">
          {step === 'check' && (
            <>
              <div className="flex flex-col gap-1.5 rounded-[14px] border border-border bg-background p-4">
                <div className="font-semibold">{variableTitle(workspace.variable)}</div>
                <div className="font-mono text-xs break-all text-muted-foreground">{workspace.variable?.getIri() ?? 'Blank node'}</div>
              </div>
              <ul className="m-0 flex list-none flex-col gap-1 p-0">
                <CheckItem
                  ok={unchecked <= 0}
                  warn={unchecked > 0}
                  title={unchecked <= 0 ? `You checked all ${total} components` : unchecked === total ? `None of the ${total} components is checked yet` : `${unchecked} of ${total} components are not checked yet`}
                  text={unchecked > 0 ? 'You can still publish, but every draft deserves a look first.' : 'Marked in the review list.'}
                />
                <CheckItem
                  ok={!workspace.turtleDirty}
                  warn={workspace.turtleDirty && !workspace.turtleError}
                  error={workspace.turtleDirty && Boolean(workspace.turtleError)}
                  title={workspace.turtleDirty ? 'The Turtle has edits the diagram does not show' : 'The Turtle and the diagram match'}
                  text={
                    workspace.turtleDirty && workspace.turtleError
                      ? `The diagram could not be updated: ${workspace.turtleError.message}`
                      : workspace.turtleDirty
                        ? 'Update the diagram first, so you publish what you reviewed.'
                        : 'What you see in the Turtle tab is exactly what is published.'
                  }
                  action={workspace.turtleDirty ? <Button size="sm" variant="outline" disabled={workspace.rendering} onClick={() => void workspace.visualize(workspace.turtle)}>Update diagram</Button> : undefined}
                />
                {unlinked.length > 0 && (
                  <CheckItem
                    warn
                    title={`${unlinked.length} ${unlinked.length === 1 ? 'component has' : 'components have'} no vocabulary link`}
                    text={`${unlinked.map((item) => item.label).join(', ')}. You can publish without a link.`}
                  />
                )}
                {checkError ? (
                  <CheckItem error title="The Turtle cannot be prepared" text={checkError} />
                ) : missing === null ? (
                  <CheckItem pending title="Checking the nanopublication metadata…" />
                ) : missing.length ? (
                  <CheckItem
                    warn
                    title="Nanopublication metadata is missing"
                    text={`Missing: ${missing.join(', ')}. Adding it may give the variable a new w3id IRI.`}
                    action={
                      <Button
                        size="sm"
                        disabled={applying}
                        onClick={async () => {
                          setApplying(true);
                          try {
                            await workspace.applyNanopubMetadata();
                          } finally {
                            setApplying(false);
                          }
                        }}
                      >
                        {applying && <Spinner />}
                        Add the metadata
                      </Button>
                    }
                  />
                ) : (
                  <CheckItem ok title="Nanopublication metadata is in place" text="dct:conformsTo, dct:identifier, dct:created and pav:createdWith. Your ORCID iD follows in the next step." />
                )}
              </ul>
            </>
          )}

          {step === 'creator' && (
            <>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="publish-orcid" className={cn(orcidError && 'text-destructive-soft-foreground')}>Your ORCID iD</Label>
                <div className="relative">
                  <OrcidMark className="absolute top-3 left-3" />
                  <Input
                    id="publish-orcid"
                    autoComplete="off"
                    spellCheck={false}
                    autoFocus
                    placeholder="0000-0000-0000-0000"
                    value={orcid}
                    aria-invalid={Boolean(orcidError)}
                    aria-describedby="publish-orcid-message publish-orcid-help"
                    onChange={(event) => setOrcid(event.target.value)}
                    onBlur={() => setTouched(true)}
                    className="h-11 pl-10 font-mono text-[15px] tracking-[0.02em]"
                  />
                </div>
                <div id="publish-orcid-message" role="alert" className={cn('min-h-[18px] text-[13px] leading-[18px]', orcidError ? 'text-destructive-soft-foreground' : 'text-primary-soft-foreground')}>
                  {orcidError ?? (orcidState.uri ? 'Valid ORCID iD. The check digit matches.' : '')}
                </div>
              </div>
              <Callout tone="neutral" id="publish-orcid-help">
                Your iD is written as <code className="font-mono text-xs">dct:creator</code> and <code className="font-mono text-xs">prov:wasAttributedTo</code>. The service’s key only signs the nanopublication; it is never recorded as the creator.
              </Callout>
              <div className="flex items-center gap-2.5 text-[13px] text-text-2">
                <Checkbox id="publish-remember" checked={remember} onCheckedChange={(value) => setRemember(value === true)} />
                <label htmlFor="publish-remember">Remember my ORCID iD on this device</label>
              </div>
            </>
          )}

          {step === 'confirm' && (
            <>
              <dl className="m-0 grid grid-cols-[112px_minmax(0,1fr)] overflow-hidden rounded-[14px] border border-border text-[13px] leading-[19px]">
                <SummaryRow term="Variable">{variableTitle(workspace.variable)}</SummaryRow>
                <SummaryRow term="Identifier" mono>{workspace.variable?.getIri() ?? '—'}</SummaryRow>
                <SummaryRow term="Creator" mono>
                  <span className="flex items-center gap-2"><OrcidMark className="size-[18px] text-[9px]" />{orcidState.uri}</span>
                </SummaryRow>
                <SummaryRow term="Signed by" last>The service’s signing key</SummaryRow>
              </dl>
              <Callout tone="warning" title="Publishing is permanent and public">
                Anyone can read the nanopublication, and nobody can edit or delete it. If something turns out wrong, you can publish a retraction, which also stays public.
              </Callout>
              {publishError && <Callout tone="danger" title="Publishing failed">{publishError}</Callout>}
              <div className="flex items-start gap-2.5 text-sm leading-5">
                <Checkbox id="publish-agree" className="mt-px" checked={agree} onCheckedChange={(value) => setAgree(value === true)} />
                <label htmlFor="publish-agree">I checked the components and links, and I want to publish this variable.</label>
              </div>
            </>
          )}

          {step === 'publishing' && (
            <div role="status" className="flex flex-col items-center gap-3.5 py-8 text-center">
              <Spinner className="size-9 text-primary" />
              <div className="font-semibold">Signing and publishing</div>
              <div className="text-[13px] text-muted-foreground">Sending the nanopublication to the network</div>
            </div>
          )}

          {step === 'done' && result && (
            <>
              <div className="flex items-center gap-3.5">
                <span aria-hidden="true" className="flex size-12 shrink-0 items-center justify-center rounded-full bg-primary-soft text-primary">
                  <CheckIcon className="size-6" strokeWidth={2.6} />
                </span>
                <div>
                  <div className="text-base font-semibold">Your variable is now a public nanopublication</div>
                  <div className="text-[13px] text-muted-foreground">It is listed under Publications, where you can retract it.</div>
                </div>
              </div>
              <div className="flex flex-col overflow-hidden rounded-[14px] border border-border">
                <ResultRow label="Nanopublication" value={result.nanopub_url} href={result.nanopub_url} />
                {result.variable_uri && <ResultRow label="Variable identifier" value={result.variable_uri} href={result.variable_uri} last />}
              </div>
              {result.published_to && <p className="m-0 text-xs text-muted-foreground">Published to {result.published_to}</p>}
            </>
          )}
        </DialogBody>

        <DialogFooter>
          {step === 'check' && (
            <>
              <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button disabled={!canContinueCheck} onClick={() => setStep('creator')}>Continue</Button>
            </>
          )}
          {step === 'creator' && (
            <>
              <Button variant="ghost" onClick={() => setStep('check')}>Back</Button>
              <Button
                onClick={() => {
                  setTouched(true);
                  if (orcidState.uri) setStep('confirm');
                  else document.getElementById('publish-orcid')?.focus();
                }}
              >
                Continue
              </Button>
            </>
          )}
          {step === 'confirm' && (
            <>
              <Button variant="ghost" onClick={() => setStep('creator')}>Back</Button>
              <Button disabled={!agree || !orcidState.uri || busy} onClick={() => void publish()}>Publish nanopublication</Button>
            </>
          )}
          {step === 'done' && result && (
            <>
              <Button variant="outline" asChild>
                <a href={result.nanopub_url} target="_blank" rel="noopener noreferrer" className="text-foreground no-underline">
                  Open nanopublication <ExternalLinkIcon aria-hidden="true" />
                </a>
              </Button>
              <Button
                onClick={() => {
                  onOpenChange(false);
                  navigate('publications');
                }}
              >
                View publications
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CheckItem({
  ok,
  warn,
  error,
  pending,
  title,
  text,
  action,
}: {
  ok?: boolean;
  warn?: boolean;
  error?: boolean;
  pending?: boolean;
  title: string;
  text?: string;
  action?: React.ReactNode;
}) {
  const icon = pending ? <Spinner className="size-5 text-muted-foreground" />
    : error ? <AlertTriangleIcon className="size-5 text-destructive-soft-foreground" aria-hidden="true" />
      : warn ? <AlertTriangleIcon className="size-5 text-warning-foreground" aria-hidden="true" />
        : ok ? <CircleCheckIcon className="size-5 text-primary" aria-hidden="true" />
          : <InfoIcon className="size-5 text-muted-foreground" aria-hidden="true" />;
  return (
    <li className="flex items-start gap-3 py-2.5">
      <span className="shrink-0">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="font-medium">{title}</div>
        {text && <div className="text-[13px] break-words text-muted-foreground">{text}</div>}
        {action && <div className="mt-2">{action}</div>}
      </div>
    </li>
  );
}

function SummaryRow({ term, children, mono, last }: { term: string; children: React.ReactNode; mono?: boolean; last?: boolean }) {
  return (
    <>
      <dt className={cn('bg-surface-2 px-3.5 py-3 text-muted-foreground', !last && 'border-b border-border')}>{term}</dt>
      <dd className={cn('m-0 px-3.5 py-3 break-all', mono ? 'font-mono text-xs' : 'font-semibold', !last && 'border-b border-border')}>{children}</dd>
    </>
  );
}

function ResultRow({ label, value, href, last }: { label: string; value: string; href: string; last?: boolean }) {
  return (
    <div className={cn('flex items-center gap-3 px-3.5 py-3', !last && 'border-b border-border')}>
      <div className="min-w-0 flex-1">
        <div className="text-xs text-muted-foreground">{label}</div>
        <a href={href} target="_blank" rel="noopener noreferrer" className="block truncate font-mono text-[12.5px]">{value}</a>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={async () => {
          await navigator.clipboard.writeText(value);
          toast(`${label} copied`);
        }}
      >
        <CopyIcon aria-hidden="true" /> Copy
      </Button>
    </div>
  );
}
