import * as React from 'react';
import { ChevronRightIcon, CloudUploadIcon, CopyIcon, DownloadIcon, FilePlus2Icon, InfoIcon, RefreshCwIcon, ShapesIcon, SparklesIcon, XIcon } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Callout, Pill } from '@/components/ui/feedback';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/controls';
import { EXAMPLE_TURTLE } from '@/lib/iadopt/example';
import { OpenTurtleButtons, useOpenTurtle } from '../components/OpenTurtle';
import { PublishDialog, usePublishDialog } from '../components/PublishDialog';
import { DiagramView } from '../components/workspace/DiagramView';
import { ExportMenu, useExport } from '../components/workspace/ExportMenu';
import { Inspector } from '../components/workspace/Inspector';
import { ComponentsTable, DecomposeProgress, DefinitionPanel, ModelOutputView, ReviewPanel, RunPanel, StatusPill } from '../components/workspace/panels';
import { TurtleEditor } from '../components/workspace/TurtleEditor';
import { navigate } from '../router';
import { useWorkspace, variableTitle } from '../state/workspace';

type Tab = 'diagram' | 'components' | 'turtle' | 'output';

export function WorkspacePage() {
  const workspace = useWorkspace();
  const publish = usePublishDialog();
  const [tab, setTab] = React.useState<Tab>('diagram');
  const [noticeClosed, setNoticeClosed] = React.useState(false);
  const inspectorRef = React.useRef<HTMLElement>(null);

  // A new result opens on the diagram.
  React.useEffect(() => {
    if (workspace.decomposing) setTab('diagram');
  }, [workspace.decomposing]);

  if (!workspace.hasContent && !workspace.decomposing) return <EmptyWorkspace />;

  const identifier = workspace.variable?.getIri()?.split('/').pop();
  const showNotice = Boolean(workspace.run) && !noticeClosed && !workspace.decomposing;

  return (
    <main id="main" className="flex min-h-0 flex-1 flex-col lg:h-[calc(100vh-56px)]">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3 border-b border-border bg-surface px-5 py-2.5">
        <div className="flex min-w-0 flex-[1_1_380px] flex-col gap-0.5">
          <div className="flex items-center gap-1.5 text-xs leading-4 text-muted-foreground">
            <span>Workspace</span>
            {identifier && (
              <>
                <ChevronRightIcon className="size-3" aria-hidden="true" />
                <span className="truncate font-mono">{identifier}</span>
              </>
            )}
          </div>
          <div className="flex min-w-0 items-center gap-2.5">
            <h1 className="m-0 min-w-0 truncate text-[17px] leading-6 font-semibold tracking-[-0.01em]">
              {workspace.variable ? variableTitle(workspace.variable) : workspace.definition || 'New variable'}
            </h1>
            <StatusPill />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="hidden text-xs text-muted-foreground md:inline">Draft saved on this device</span>
          <ExportMenu />
          <Button onClick={() => void publish.request()} disabled={!workspace.layout || workspace.decomposing}>
            <CloudUploadIcon aria-hidden="true" /> Publish
          </Button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <aside aria-label="Definition and review" className="order-2 flex flex-col gap-6 border-t border-border bg-surface px-4 pt-[18px] pb-6 lg:order-1 lg:w-[296px] lg:shrink-0 lg:overflow-y-auto lg:border-t-0 lg:border-r">
          <DefinitionPanel />
          <RunPanel />
          <ReviewPanel />
        </aside>

        <section aria-label="Decomposition" className="order-1 flex min-h-[560px] min-w-0 flex-1 flex-col lg:order-2 lg:min-h-0">
          {workspace.decomposing ? (
            <DecomposeProgress />
          ) : (
            <Tabs value={tab} onValueChange={(value) => setTab(value as Tab)} className="flex min-h-0 flex-1 flex-col">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-4">
                <TabsList aria-label="Views">
                  <TabsTrigger value="diagram"><ShapesIcon aria-hidden="true" />Diagram</TabsTrigger>
                  <TabsTrigger value="components">
                    Components
                    {workspace.components.length > 0 && <span className="flex h-[18px] min-w-5 items-center justify-center rounded-full bg-surface-3 px-1.5 text-[11px] font-semibold text-text-2">{workspace.components.length}</span>}
                  </TabsTrigger>
                  <TabsTrigger value="turtle">
                    Turtle
                    {workspace.turtleDirty && <span className="size-1.5 rounded-full bg-warning-foreground" aria-label="has changes" />}
                  </TabsTrigger>
                  {workspace.rawOutput && <TabsTrigger value="output">Model output</TabsTrigger>}
                </TabsList>
              </div>
              {showNotice && (
                <div className="flex items-center gap-2.5 border-b border-warning-line bg-warning-soft py-2 pr-3 pl-4 text-[13px] leading-[18px] text-warning-foreground">
                  <InfoIcon className="size-4 shrink-0" aria-hidden="true" />
                  <span className="flex-1">This is a model-generated draft. Check every component and link before you publish it.</span>
                  <button type="button" aria-label="Dismiss notice" onClick={() => setNoticeClosed(true)} className="flex size-7 items-center justify-center rounded-md hover:bg-black/5 dark:hover:bg-white/5">
                    <XIcon className="size-3.5" aria-hidden="true" />
                  </button>
                </div>
              )}
              <TabsContent value="diagram" className="flex min-h-0 flex-1 flex-col outline-none">
                <DiagramView />
              </TabsContent>
              <TabsContent value="components" className="min-h-0 flex-1 overflow-y-auto outline-none">
                <ComponentsTable onOpen={() => inspectorRef.current?.scrollIntoView({ block: 'nearest' })} />
              </TabsContent>
              <TabsContent value="turtle" className="flex min-h-0 flex-1 flex-col outline-none">
                <TurtlePanel />
              </TabsContent>
              <TabsContent value="output" className="min-h-0 flex-1 overflow-y-auto outline-none">
                <ModelOutputView />
              </TabsContent>
            </Tabs>
          )}
        </section>

        <aside ref={inspectorRef} aria-label="Inspector" className="order-3 flex flex-col border-t border-border bg-surface lg:w-[340px] lg:shrink-0 lg:overflow-y-auto lg:border-t-0 lg:border-l">
          {workspace.decomposing ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2.5 p-8 text-center">
              <div className="font-semibold">Components appear here</div>
              <p className="m-0 max-w-60 text-[13px] text-muted-foreground">When the draft is ready, select any component to check its label and vocabulary link.</p>
            </div>
          ) : (
            <Inspector />
          )}
        </aside>
      </div>

      <PublishDialog open={publish.open} onOpenChange={publish.setOpen} />
    </main>
  );
}

function TurtlePanel() {
  const workspace = useWorkspace();
  const exportFile = useExport();
  const [applying, setApplying] = React.useState(false);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 px-6 pt-5 pb-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <h2 className="m-0 text-[15px] leading-[22px] font-semibold">Turtle</h2>
          {workspace.turtleError ? (
            <Pill tone="danger" dot>{workspace.turtleError.phase === 'Parsing' ? 'Does not parse' : 'Could not draw'}</Pill>
          ) : workspace.turtleDirty ? (
            <Pill tone="warning" dot>Not drawn yet</Pill>
          ) : workspace.layout ? (
            <Pill tone="success">Parses</Pill>
          ) : null}
          <span className="text-xs text-muted-foreground">This exact text is what you export and publish.</span>
        </div>
        <div className="flex flex-wrap gap-2">
          {workspace.turtleDirty && (
            <Button size="sm" disabled={workspace.rendering} onClick={() => void workspace.visualize(workspace.turtle)}>
              <RefreshCwIcon aria-hidden="true" /> Update diagram
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            disabled={!workspace.turtle.trim() || applying || !workspace.preparationOptions}
            title="Add dct:conformsTo, dct:identifier, dct:created and pav:createdWith"
            onClick={async () => {
              setApplying(true);
              const ok = await workspace.applyNanopubMetadata();
              setApplying(false);
              if (ok) toast('Nanopublication metadata added. Review the updated Turtle.');
            }}
          >
            <SparklesIcon aria-hidden="true" /> Add nanopub metadata
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!workspace.turtle.trim()}
            onClick={async () => {
              await navigator.clipboard.writeText(workspace.turtle);
              toast('Turtle copied');
            }}
          >
            <CopyIcon aria-hidden="true" /> Copy
          </Button>
          <Button size="sm" variant="outline" disabled={!workspace.turtle.trim()} onClick={() => void exportFile('ttl')}>
            <DownloadIcon aria-hidden="true" /> Download .ttl
          </Button>
        </div>
      </div>
      {!workspace.preparationOptions && workspace.preparationError && (
        <p className="m-0 text-xs text-muted-foreground">Adding nanopub metadata is unavailable: {workspace.preparationError}</p>
      )}
      {workspace.turtleError && (
        <Callout tone="danger" title={`Error during ${workspace.turtleError.phase.toLowerCase()}`}>
          <span className="font-mono text-xs break-words">{workspace.turtleError.message}</span>
        </Callout>
      )}
      <div className="min-h-[360px] flex-1 overflow-hidden rounded-xl border border-border">
        <TurtleEditor label="Turtle" value={workspace.turtle} onChange={workspace.setTurtle} />
      </div>
    </div>
  );
}

function EmptyWorkspace() {
  const workspace = useWorkspace();
  const open = useOpenTurtle();
  return (
    <main id="main" className="canvas-dots flex flex-1 items-center justify-center px-5 py-16">
      {open.element}
      <div className="flex w-full max-w-[560px] flex-col gap-5 rounded-2xl border border-border bg-surface p-7 shadow-sm">
        <div className="flex flex-col gap-1.5">
          <h1 className="m-0 text-xl leading-[26px] font-semibold tracking-[-0.015em]">Your workspace is empty</h1>
          <p className="m-0 text-text-2">Describe a variable to get a draft, or open Turtle you already have.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => navigate('start')}><FilePlus2Icon aria-hidden="true" />Describe a variable</Button>
          <Button
            variant="outline"
            onClick={() => void workspace.openTurtle(EXAMPLE_TURTLE)}
          >
            <ShapesIcon aria-hidden="true" />Open the example
          </Button>
        </div>
        <div className="flex flex-col gap-2 border-t border-border pt-4">
          <span className="text-[13px] font-medium text-text-2">Already have Turtle?</span>
          <OpenTurtleButtons open={open} />
        </div>
      </div>
    </main>
  );
}
