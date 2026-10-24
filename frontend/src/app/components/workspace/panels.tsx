import * as React from 'react';
import { AlertTriangleIcon, CheckIcon, CircleCheckIcon, CircleIcon, CopyIcon, PencilIcon, RotateCcwIcon } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Callout, Pill, Spinner } from '@/components/ui/feedback';
import { Kbd, SectionLabel, Textarea } from '@/components/ui/fields';
import { findHighlights } from '@/lib/iadopt/highlight';
import { KIND_CHIP, KIND_DOT, KIND_HIGHLIGHT } from '@/lib/iadopt/kinds';
import { isLinkableRole, isVocabularyLink, type ComponentInfo } from '@/lib/iadopt/model';
import { cn } from '@/lib/utils';
import { useWorkspace } from '../../state/workspace';
import { RunSettingsPopover } from '../RunSettingsPopover';

/* ------------------------------- Definition ------------------------------ */

export function DefinitionPanel() {
  const workspace = useWorkspace();
  const [editing, setEditing] = React.useState(!workspace.definition.trim());
  const terms = workspace.components
    .filter((item) => item.role !== 'Variable')
    .map((item) => ({ label: item.label, kind: item.kind }));
  const segments = findHighlights(workspace.definition, terms);
  const hasHighlights = segments.some((segment) => segment.kind);

  return (
    <section aria-labelledby="definition-heading" className="flex flex-col gap-2.5">
      <div className="flex items-center justify-between">
        <SectionLabel id="definition-heading">Definition</SectionLabel>
        {!editing && (
          <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
            <PencilIcon aria-hidden="true" /> Edit
          </Button>
        )}
      </div>
      {editing ? (
        <>
          <label htmlFor="workspace-definition" className="sr-only">Variable definition</label>
          <Textarea
            id="workspace-definition"
            rows={4}
            value={workspace.definition}
            placeholder="Describe the variable in plain words"
            onChange={(event) => workspace.setDefinition(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                void workspace.decompose();
              }
            }}
            className="text-[15px] leading-[22px]"
          />
          {workspace.definition.trim() && workspace.layout && (
            <Button variant="ghost" size="sm" className="self-start" onClick={() => setEditing(false)}>Done</Button>
          )}
        </>
      ) : (
        <>
          <p className="m-0 text-[15px] leading-[27px] text-pretty">
            {segments.map((segment, index) =>
              segment.kind ? (
                <span key={index} className={cn('rounded-[4px] px-[3px] py-px', KIND_HIGHLIGHT[segment.kind])}>{segment.text}</span>
              ) : (
                <React.Fragment key={index}>{segment.text}</React.Fragment>
              ),
            )}
          </p>
          {hasHighlights && <p className="m-0 text-xs leading-[17px] text-muted-foreground">Highlights show where each component label appears in your text.</p>}
        </>
      )}
      <div className="flex flex-col gap-2">
        <Button
          variant={workspace.layout ? 'outline' : 'default'}
          disabled={workspace.decomposing || !workspace.definition.trim()}
          onClick={() => void workspace.decompose()}
        >
          {workspace.decomposing ? <Spinner /> : <RotateCcwIcon aria-hidden="true" />}
          {workspace.decomposing ? 'Decomposing…' : workspace.run ? 'Run again' : 'Decompose'}
          {!workspace.decomposing && <Kbd>⌘↵</Kbd>}
        </Button>
        <div className="rounded-lg bg-surface-2 p-1">
          <RunSettingsPopover />
        </div>
      </div>
      {workspace.decomposeError && !workspace.decomposing && (
        <Callout tone="danger" title="Decomposition failed">{workspace.decomposeError}</Callout>
      )}
    </section>
  );
}

/* ---------------------------------- Run ---------------------------------- */

export function RunPanel() {
  const { run, schemaValid, validationErrors } = useWorkspace();
  if (!run) return null;
  const errors = validationErrors ?? [];
  return (
    <section aria-labelledby="run-heading" className="flex flex-col gap-2.5">
      <SectionLabel id="run-heading">Run</SectionLabel>
      <dl className="m-0 grid grid-cols-[92px_minmax(0,1fr)] gap-x-3 gap-y-2 text-[13px] leading-[18px]">
        <dt className="text-muted-foreground">Model</dt>
        <dd className="m-0 font-medium break-words">{run.model || 'Default'}{run.provider ? ` on ${run.provider.toUpperCase()}` : ''}</dd>
        <dt className="text-muted-foreground">Reasoning</dt>
        <dd className="m-0 font-medium">{run.disableThinking ? 'Off' : 'On'}</dd>
        <dt className="text-muted-foreground">Schema</dt>
        <dd className={cn('m-0 flex items-center gap-1.5 font-medium', schemaValid ? 'text-primary-soft-foreground' : 'text-warning-foreground')}>
          {schemaValid ? <CheckIcon className="size-3.5" aria-hidden="true" /> : <AlertTriangleIcon className="size-3.5" aria-hidden="true" />}
          {schemaValid ? 'No errors' : `${errors.length} ${errors.length === 1 ? 'error' : 'errors'}`}
        </dd>
        <dt className="text-muted-foreground">Finished</dt>
        <dd className="m-0 font-medium">{new Date(run.finishedAt).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}</dd>
      </dl>
      <p className="m-0 rounded-[10px] bg-surface-2 px-3 py-2.5 text-xs leading-[17px] text-text-2">
        In the service’s held-out test (24 variables), the default setup got 3 exactly right and 7 scored zero. Treat every result as a draft and check it.
      </p>
    </section>
  );
}

/* --------------------------------- Review -------------------------------- */

export function needsLink(item: ComponentInfo): boolean {
  return isLinkableRole(item.role) && !isVocabularyLink(item.link) && (item.link.source === 'Local' || item.link.source === 'None');
}

export function ReviewPanel() {
  const workspace = useWorkspace();
  const total = workspace.components.length;
  if (!total) return null;
  const done = workspace.checkedCount;

  return (
    <section aria-labelledby="review-heading" className="flex flex-col gap-2.5">
      <div className="flex items-baseline justify-between">
        <SectionLabel id="review-heading">Review</SectionLabel>
        <span className="text-xs font-medium text-text-2">{done} of {total} checked</span>
      </div>
      <div
        role="progressbar"
        aria-label="Components checked"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done}
        className="h-1.5 overflow-hidden rounded-full bg-surface-3"
      >
        <div className="h-full rounded-full bg-primary transition-[width] duration-300" style={{ width: `${Math.round((done / total) * 100)}%` }} />
      </div>
      <ul className="m-0 -mx-1.5 flex list-none flex-col gap-0.5 p-0">
        {workspace.components.map((item, index) => {
          const checked = workspace.isChecked(item.comp);
          const warn = !checked && needsLink(item);
          return (
            <li key={index}>
              <button
                type="button"
                onClick={() => workspace.select(item.comp)}
                aria-current={workspace.selected === item.comp ? 'true' : undefined}
                className={cn(
                  'flex h-8 w-full items-center gap-2.5 rounded-lg px-2 text-left text-[13px] transition-colors outline-none hover:bg-surface-2 focus-visible:ring-[3px] focus-visible:ring-ring/40',
                  workspace.selected === item.comp && 'bg-surface-2',
                  item.depth > 0 && 'pl-6',
                )}
              >
                <span aria-hidden="true" className={cn('size-2 shrink-0 rounded-[3px]', KIND_DOT[item.kind])} />
                <span className="min-w-0 flex-1 truncate">{item.label || 'Unnamed'}</span>
                {checked ? (
                  <CircleCheckIcon className="size-4 shrink-0 text-primary" aria-label="Checked" />
                ) : warn ? (
                  <AlertTriangleIcon className="size-4 shrink-0 text-warning-foreground" aria-label="No vocabulary link" />
                ) : (
                  <CircleIcon className="size-4 shrink-0 text-input" aria-label="Not checked yet" />
                )}
              </button>
            </li>
          );
        })}
      </ul>
      <p className="m-0 text-xs leading-[17px] text-muted-foreground">Select a component and mark it as checked in the inspector. Checks are kept on this device.</p>
    </section>
  );
}

/* ------------------------------- Components ------------------------------ */

function linkText(item: ComponentInfo): string {
  if (item.role === 'Variable') return item.link.iri ?? 'Blank node';
  if (item.role === 'Constraint') {
    return item.constrains.length ? `narrows ${item.constrains.map((comp) => comp.getLabel(false)).join(', ')}` : 'narrows nothing';
  }
  if (isVocabularyLink(item.link)) return `${item.link.source} · ${item.link.name ?? item.link.iri}`;
  if (needsLink(item)) return 'No vocabulary link';
  return item.link.iri ?? 'Blank node';
}

export function ComponentsTable({ onOpen }: { onOpen: () => void }) {
  const workspace = useWorkspace();
  return (
    <div className="flex flex-col gap-3.5 px-6 pt-5 pb-8">
      <div>
        <h2 className="m-0 text-[15px] leading-[22px] font-semibold">Components</h2>
        <p className="m-0 text-[13px] text-muted-foreground">Everything in the variable, with its vocabulary link. Select a row to edit it.</p>
      </div>
      <div className="overflow-x-auto rounded-xl border border-border bg-surface">
        <table className="w-full min-w-[640px] border-collapse text-left">
          <thead>
            <tr className="border-b border-border bg-surface-2 text-xs font-semibold text-muted-foreground">
              <th scope="col" className="px-4 py-2.5 font-semibold">Type</th>
              <th scope="col" className="px-3 py-2.5 font-semibold">Label</th>
              <th scope="col" className="px-3 py-2.5 font-semibold">Link</th>
              <th scope="col" className="px-4 py-2.5 font-semibold">Review</th>
            </tr>
          </thead>
          <tbody>
            {workspace.components.map((item, index) => {
              const checked = workspace.isChecked(item.comp);
              const missing = needsLink(item);
              const selected = workspace.selected === item.comp;
              return (
                <tr key={index} className={cn('border-b border-border last:border-b-0', selected && 'bg-surface-2')}>
                  <td className="px-4 py-2.5">
                    <span className={cn('inline-flex h-6 items-center gap-1.5 rounded-full border px-2 text-xs font-semibold whitespace-nowrap', KIND_CHIP[item.kind], item.depth > 0 && 'ml-4')}>
                      <span aria-hidden="true" className={cn('size-[7px] rounded-[2px]', KIND_DOT[item.kind])} />
                      {item.typeName}
                    </span>
                  </td>
                  <td className="max-w-[260px] px-3 py-2.5">
                    <button
                      type="button"
                      className="max-w-full truncate text-left font-medium text-foreground underline-offset-4 outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/40"
                      onClick={() => {
                        workspace.select(item.comp);
                        onOpen();
                      }}
                    >
                      {item.label || 'Unnamed'}
                    </button>
                  </td>
                  <td className={cn('max-w-[280px] truncate px-3 py-2.5 text-[13px]', missing ? 'text-warning-foreground' : 'text-text-2')}>{linkText(item)}</td>
                  <td className="px-4 py-2.5 text-[13px] font-medium">
                    {checked ? (
                      <span className="text-primary-soft-foreground">Checked</span>
                    ) : missing ? (
                      <span className="text-warning-foreground">Needs a look</span>
                    ) : (
                      <span className="text-muted-foreground">To check</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ------------------------------ Model output ----------------------------- */

export function ModelOutputView() {
  const { rawOutput, validationErrors, schemaValid } = useWorkspace();
  return (
    <div className="flex flex-col gap-3.5 px-6 pt-5 pb-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="m-0 text-[15px] leading-[22px] font-semibold">Model output</h2>
          <p className="m-0 text-[13px] text-muted-foreground">The raw response of the decomposition call, as streamed.</p>
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={!rawOutput}
          onClick={async () => {
            await navigator.clipboard.writeText(rawOutput);
            toast('Model output copied');
          }}
        >
          <CopyIcon aria-hidden="true" /> Copy
        </Button>
      </div>
      <pre className="m-0 max-h-[460px] overflow-auto rounded-xl border border-border bg-surface px-4 py-3.5 font-mono text-[12.5px] leading-5 whitespace-pre-wrap">
        {rawOutput || 'The raw response from the backend will appear here.'}
      </pre>
      <section aria-labelledby="validation-heading" aria-live="polite">
        <h3 id="validation-heading" className="sr-only">JSON schema validation</h3>
        {validationErrors === null ? (
          <Callout tone="neutral">Validation results will appear here.</Callout>
        ) : validationErrors.length === 0 ? (
          <Callout tone="success" title="JSON schema validation passed">
            Every field matches the schema and every constraint points at a component label.
          </Callout>
        ) : (
          <Callout tone="danger" title={schemaValid === false ? 'JSON schema validation errors' : 'Validation messages'}>
            <ul className="m-0 flex list-disc flex-col gap-1 pl-4 font-mono text-xs leading-[18px] break-words">
              {validationErrors.map((line, index) => <li key={index}>{line}</li>)}
            </ul>
          </Callout>
        )}
      </section>
    </div>
  );
}

/* ------------------------------- Decomposing ----------------------------- */

export function DecomposeProgress() {
  const { rawOutput, decomposeStartedAt, runSettings } = useWorkspace();
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const seconds = decomposeStartedAt ? Math.max(0, Math.floor((now - decomposeStartedAt) / 1000)) : 0;

  return (
    <div className="canvas-dots flex min-h-0 flex-1 justify-center overflow-auto px-6 py-8">
      <div className="flex w-full max-w-[680px] flex-col gap-4">
        <div className="flex flex-col gap-4 rounded-2xl border border-border bg-surface p-5 shadow-sm">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="m-0 flex items-center gap-2 text-[15px] leading-[22px] font-semibold"><Spinner className="text-primary" />Decomposing your definition</h2>
              <p className="m-0 text-[13px] text-muted-foreground">This usually takes under a minute.</p>
            </div>
            <Pill tone="info" className="font-mono" aria-label={`${seconds} seconds elapsed`}>{seconds}s</Pill>
          </div>
          <ol className="m-0 flex list-none flex-col gap-2 p-0 text-[13px] leading-[18px] text-text-2">
            <li className="flex items-center gap-2.5"><Spinner className="text-primary" />The model writes the decomposition{rawOutput ? '' : ' (waiting for the first words)'}</li>
            <li className="flex items-center gap-2.5"><CircleIcon className="size-4 text-input" aria-hidden="true" />Then the service writes a label and comment, checks the schema, links each label to Wikidata, QUDT and OM, and builds the Turtle</li>
          </ol>
        </div>
        <div className="overflow-hidden rounded-2xl border border-border bg-surface">
          <div className="flex items-center justify-between border-b border-border px-4 py-2.5 text-xs text-muted-foreground">
            <span className="flex items-center gap-2 font-semibold text-text-2"><span aria-hidden="true" className="size-[7px] animate-pulse rounded-full bg-primary" />Live model output</span>
            <span className="font-mono">{runSettings.model}{runSettings.disableThinking ? ' · reasoning off' : ''}</span>
          </div>
          <pre aria-live="off" className="m-0 max-h-[420px] min-h-[220px] overflow-auto px-4 py-3.5 font-mono text-[12.5px] leading-5 whitespace-pre-wrap">
            {rawOutput}
            <span aria-hidden="true" className="caret-blink ml-px inline-block h-[15px] w-[7px] translate-y-[2px] bg-primary" />
          </pre>
        </div>
      </div>
    </div>
  );
}

export function StatusPill() {
  const { decomposing, run, components, checkedCount } = useWorkspace();
  if (decomposing) return <Pill tone="info"><Spinner className="size-3" />Decomposing</Pill>;
  if (!components.length) return null;
  if (checkedCount >= components.length) return <Pill tone="success"><CheckIcon strokeWidth={3} aria-hidden="true" />All checked</Pill>;
  if (run) return <Pill tone="warning" dot>Draft · needs review</Pill>;
  return null;
}
