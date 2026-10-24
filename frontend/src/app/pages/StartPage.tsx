import * as React from 'react';
import { ArrowRightIcon, BracesIcon, ExternalLinkIcon, HistoryIcon, LockIcon, ShapesIcon } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Kbd } from '@/components/ui/fields';
import { EXAMPLE_DEFINITIONS, EXAMPLE_TURTLE } from '@/lib/iadopt/example';
import { KIND_CHIP, KIND_DOT, KIND_HIGHLIGHT, KIND_NAMES, KIND_TEXT, type Kind } from '@/lib/iadopt/kinds';
import { cn } from '@/lib/utils';
import { displayName, useAuth } from '@/shared/auth';
import { SiteFooter } from '@/shared/SiteFooter';
import { OpenTurtleButtons, useOpenTurtle } from '../components/OpenTurtle';
import { RunSettingsPopover } from '../components/RunSettingsPopover';
import { navigate } from '../router';
import { useWorkspace, variableTitle } from '../state/workspace';

const DEMO: Array<{ text: string; kind?: Kind }> = [
  { text: 'Daily', kind: 'con' },
  { text: 'mean', kind: 'stat' },
  { text: 'mass concentration', kind: 'prop' },
  { text: 'of' },
  { text: 'dissolved', kind: 'con' },
  { text: 'oxygen', kind: 'ooi' },
  { text: 'in' },
  { text: 'sea water', kind: 'mat' },
  { text: 'at 10 m depth', kind: 'con' },
];

const LEGEND: Array<{ kind: Kind; text: string }> = [
  { kind: 'var', text: 'What you measure or model, described by the components below.' },
  { kind: 'prop', text: 'The characteristic that is observed, such as temperature or mass concentration.' },
  { kind: 'ooi', text: 'The entity whose property is observed, such as oxygen.' },
  { kind: 'mat', text: 'The entity that contains the object of interest, such as sea water.' },
  { kind: 'ctx', text: 'Another entity that gives the observation its context.' },
  { kind: 'stat', text: 'A statistic applied to the property, such as mean or maximum.' },
  { kind: 'con', text: 'Narrows another component, such as dissolved or at 10 m depth.' },
];

export function StartPage() {
  const auth = useAuth();
  const workspace = useWorkspace();
  const open = useOpenTurtle();
  const textarea = React.useRef<HTMLTextAreaElement>(null);

  async function startDecomposition() {
    if (!workspace.definition.trim()) {
      textarea.current?.focus();
      return;
    }
    if (!(await auth.requireUser('decompose a variable'))) return;
    navigate('workspace');
    void workspace.decompose();
  }

  return (
    <main id="main" className="flex-1">
      {open.element}
      <section aria-labelledby="hero-heading" className="canvas-dots border-b border-border px-5 pt-16 pb-14 sm:pt-20">
        <div className="mx-auto flex max-w-[860px] flex-col items-center gap-[22px] text-center">
          <span className="flex h-7 items-center gap-2 rounded-full border border-border bg-surface py-0 pr-3 pl-1.5 text-[13px] text-text-2 shadow-xs">
            <span aria-hidden="true" className="flex gap-0.5">
              {(['var', 'prop', 'ooi', 'con', 'stat'] as Kind[]).map((kind) => (
                <span key={kind} className={cn('h-3.5 w-[5px] rounded-[2px]', KIND_DOT[kind])} />
              ))}
            </span>
            Interoperable variable descriptions with I-ADOPT
          </span>
          <h1 id="hero-heading" className="m-0 max-w-[780px] text-[clamp(36px,5vw,56px)] leading-[1.04] font-[650] tracking-[-0.035em] text-balance">
            Describe a variable in plain words. Get it back in I-ADOPT.
          </h1>
          <p className="m-0 max-w-[660px] text-lg leading-7 text-pretty text-text-2">
            The service proposes each I-ADOPT component, links them to Wikidata, QUDT and OM, and lets you review, export or publish the result as a nanopublication.
          </p>

          <form
            className="mt-2.5 w-full max-w-[820px] text-left"
            onSubmit={(event) => {
              event.preventDefault();
              void startDecomposition();
            }}
          >
            <div className="flex flex-col rounded-[18px] border border-input bg-surface shadow-md transition-[box-shadow,border-color] focus-within:border-ring focus-within:shadow-[0_0_0_4px_color-mix(in_oklab,var(--ring)_25%,transparent),var(--shadow-md)]">
              <label htmlFor="definition" className="px-5 pt-4 text-[13px] font-semibold text-text-2">Variable definition</label>
              <textarea
                id="definition"
                ref={textarea}
                rows={3}
                value={workspace.definition}
                onChange={(event) => workspace.setDefinition(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                    event.preventDefault();
                    void startDecomposition();
                  }
                }}
                placeholder="For example: Maximum daily air temperature at 2 meters"
                className="min-h-24 w-full resize-none border-0 bg-transparent px-5 pt-2 pb-3 text-[19px] leading-[29px] tracking-[-0.005em] outline-none"
              />
              <div className="flex flex-wrap items-center gap-2 px-4 pb-3.5">
                <span className="px-1 text-xs text-muted-foreground">Try</span>
                {EXAMPLE_DEFINITIONS.map((example) => (
                  <button
                    key={example}
                    type="button"
                    onClick={() => workspace.setDefinition(example)}
                    className="h-[30px] max-w-full truncate rounded-full border border-border bg-surface-2 px-3 text-[13px] text-text-2 transition-colors outline-none hover:border-input hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/40"
                  >
                    {example}
                  </button>
                ))}
              </div>
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-b-[18px] border-t border-border bg-surface-2 py-3 pr-3 pl-2">
                <RunSettingsPopover />
                <Button type="submit" size="lg" disabled={workspace.decomposing} className="ml-auto">
                  {workspace.decomposing ? 'Decomposing…' : 'Decompose'}
                  <Kbd className="border-transparent bg-white/15 text-current">⌘↵</Kbd>
                </Button>
              </div>
            </div>
          </form>

          <p className="m-0 flex flex-wrap items-center justify-center gap-1.5 text-[13px] leading-[18px] text-text-2">
            {auth.user ? (
              <span>Signed in as {displayName(auth.user)}.</span>
            ) : (
              <>
                <LockIcon className="size-[15px] text-muted-foreground" aria-hidden="true" />
                <span>
                  <button type="button" className="font-semibold text-link underline-offset-4 hover:underline" onClick={() => auth.openSignIn('decompose a variable')}>Sign in</button>{' '}
                  to decompose. Visualizing, editing and exporting Turtle need no account.
                </span>
              </>
            )}
          </p>

          <div className="grid w-full max-w-[820px] grid-cols-[repeat(auto-fit,minmax(min(360px,100%),1fr))] gap-3 text-left">
            <div className="flex flex-wrap items-center gap-3.5 rounded-[14px] border border-border bg-surface px-4 py-3.5">
              <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center rounded-[10px] bg-surface-2 text-text-2"><BracesIcon className="size-[18px]" /></span>
              <div className="min-w-0 flex-1">
                <div className="font-semibold">Already have Turtle?</div>
                <div className="text-[13px] text-muted-foreground">Visualize, edit and export it.</div>
              </div>
              <OpenTurtleButtons open={open} />
            </div>
            {workspace.hasContent ? (
              <div className="flex items-center gap-3.5 rounded-[14px] border border-border bg-surface px-4 py-3.5">
                <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center rounded-[10px] bg-surface-2 text-text-2"><HistoryIcon className="size-[18px]" /></span>
                <div className="min-w-0 flex-1">
                  <div className="font-semibold">Continue your draft</div>
                  <div className="truncate text-[13px] text-muted-foreground">
                    {workspace.variable ? variableTitle(workspace.variable) : workspace.definition || 'Saved on this device'}
                  </div>
                </div>
                <Button variant="outline" size="sm" asChild>
                  <a href="#/workspace" className="text-foreground no-underline">Open</a>
                </Button>
              </div>
            ) : (
              <div className="flex items-center gap-3.5 rounded-[14px] border border-border bg-surface px-4 py-3.5">
                <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center rounded-[10px] bg-surface-2 text-text-2"><ShapesIcon className="size-[18px]" /></span>
                <div className="min-w-0 flex-1">
                  <div className="font-semibold">See an example</div>
                  <div className="text-[13px] text-muted-foreground">A variable with every kind of component.</div>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    navigate('workspace');
                    void workspace.openTurtle(EXAMPLE_TURTLE);
                  }}
                >
                  Open
                </Button>
              </div>
            )}
          </div>
        </div>
      </section>

      <section aria-labelledby="decomposed-heading" className="border-b border-border bg-surface px-5 py-[72px]">
        <div className="mx-auto flex max-w-[1120px] flex-col gap-9">
          <div className="flex max-w-[640px] flex-col gap-2">
            <span className="text-[13px] font-semibold text-primary-soft-foreground">What you get back</span>
            <h2 id="decomposed-heading" className="m-0 text-[32px] leading-[38px] font-[650] tracking-[-0.025em] text-balance">
              Your sentence, taken apart into components you can check.
            </h2>
          </div>
          <figure className="m-0 rounded-[20px] border border-border bg-background px-6 py-8 sm:px-8">
            <figcaption className="mb-5 text-[13px] text-muted-foreground">
              Example: Daily mean mass concentration of dissolved oxygen in sea water at 10 m depth
            </figcaption>
            <div className="flex flex-wrap items-start gap-x-3 gap-y-[18px] text-[clamp(22px,2.6vw,32px)] leading-[1.2] font-semibold tracking-[-0.02em]">
              {DEMO.map((token) => (
                <span key={token.text} className="flex flex-col gap-2">
                  <span className={cn('rounded-lg px-2 pt-0.5 pb-1', token.kind ? KIND_HIGHLIGHT[token.kind] : 'px-0 font-medium text-muted-foreground')}>
                    {token.text}
                  </span>
                  {token.kind && (
                    <span className={cn('text-xs leading-[14px] font-semibold tracking-normal', KIND_TEXT[token.kind])}>{KIND_NAMES[token.kind]}</span>
                  )}
                </span>
              ))}
            </div>
            <div className="mt-8 grid grid-cols-[repeat(auto-fit,minmax(min(240px,100%),1fr))] gap-3">
              <LinkExample source="QUDT" label="mass concentration" target="Mass Concentration" />
              <LinkExample source="Wikidata" label="oxygen" target="Q629" />
              <div className="flex items-center gap-3 rounded-xl border border-warning-line bg-surface px-3.5 py-3">
                <span className="inline-flex h-[22px] shrink-0 items-center rounded-md bg-warning-soft px-2 text-[11px] font-semibold text-warning-foreground">No link</span>
                <span className="min-w-0 text-[13px]"><strong className="font-semibold">sea water</strong> <span className="text-muted-foreground">→ flagged for you to check</span></span>
              </div>
            </div>
          </figure>
        </div>
      </section>

      <section aria-labelledby="steps-heading" className="border-b border-border px-5 py-[72px]">
        <div className="mx-auto flex max-w-[1120px] flex-col gap-8">
          <h2 id="steps-heading" className="m-0 text-[32px] leading-[38px] font-[650] tracking-[-0.025em]">Three steps, and you stay in charge</h2>
          <ol className="m-0 grid list-none grid-cols-[repeat(auto-fit,minmax(min(300px,100%),1fr))] gap-4 p-0">
            {[
              ['01', 'Describe', 'Write the definition as you would in a paper or a data dictionary. One sentence is enough.'],
              ['02', 'Review', 'Results are drafts. Check each component and vocabulary link, and fix them in the diagram, the component list or the Turtle.'],
              ['03', 'Export or publish', 'Download SVG, PNG or Turtle, or publish a signed nanopublication with your ORCID iD as creator. You can retract it later.'],
            ].map(([number, title, text]) => (
              <li key={number} className="flex flex-col gap-3 rounded-2xl border border-border bg-surface p-[22px]">
                <span className="font-mono text-xs font-semibold text-muted-foreground">{number}</span>
                <h3 className="m-0 text-lg leading-6 font-semibold">{title}</h3>
                <p className="m-0 leading-[21px] text-text-2">{text}</p>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section aria-labelledby="components-heading" className="border-b border-border bg-surface px-5 py-[72px]">
        <div className="mx-auto flex max-w-[1120px] flex-col gap-8">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div className="flex max-w-[620px] flex-col gap-2">
              <span className="text-[13px] font-semibold text-primary-soft-foreground">The I-ADOPT components</span>
              <h2 id="components-heading" className="m-0 text-[32px] leading-[38px] font-[650] tracking-[-0.025em]">One colour per component, everywhere in the app</h2>
            </div>
            <a href="https://w3id.org/iadopt" target="_blank" rel="noopener noreferrer" className="flex items-center gap-1.5 font-semibold">
              Read the I-ADOPT framework <ExternalLinkIcon className="size-3.5" aria-hidden="true" />
            </a>
          </div>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(min(250px,100%),1fr))] gap-3">
            {LEGEND.map((item) => (
              <div key={item.kind} className={cn('flex flex-col gap-2.5 rounded-[14px] border p-[18px]', KIND_CHIP[item.kind])}>
                <span className="flex items-center gap-2 font-semibold">
                  <span aria-hidden="true" className={cn('size-2.5 rounded-[3px]', KIND_DOT[item.kind])} />
                  {KIND_NAMES[item.kind]}
                </span>
                <p className="m-0 text-[13px] leading-[19px] text-text-2">{item.text}</p>
              </div>
            ))}
            <div className="flex flex-col gap-2.5 rounded-[14px] border border-border bg-surface-2 p-[18px]">
              <span className="flex items-center gap-2 font-semibold">
                <span aria-hidden="true" className="flex gap-0.5">
                  <span className="size-2.5 rounded-[3px] border-[1.5px] border-ooi" />
                  <span className="size-2.5 rounded-[3px] border-[1.5px] border-ooi" />
                </span>
                Systems
              </span>
              <p className="m-0 text-[13px] leading-[19px] text-text-2">An entity can be a system: a source and a target, a numerator and a denominator, or a set of parts.</p>
            </div>
          </div>
          <div>
            <Button variant="outline" onClick={() => document.getElementById('definition')?.focus()}>
              Describe a variable <ArrowRightIcon aria-hidden="true" />
            </Button>
          </div>
        </div>
      </section>

      <SiteFooter />
    </main>
  );
}

function LinkExample({ source, label, target }: { source: string; label: string; target: string }) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-surface px-3.5 py-3">
      <span className="inline-flex h-[22px] shrink-0 items-center rounded-md bg-surface-3 px-2 font-mono text-[11px] font-semibold text-text-2">{source}</span>
      <span className="min-w-0 text-[13px]"><strong className="font-semibold">{label}</strong> <span className="text-muted-foreground">→ {target}</span></span>
    </div>
  );
}
