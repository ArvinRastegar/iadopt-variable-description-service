import * as React from 'react';
import { ArrowDownIcon, ArrowUpIcon, Maximize2Icon, MinusIcon, PlusIcon } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/feedback';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/menus';
import { KIND_CHIP, KIND_DOT, KIND_NAMES, ORDER_LETTERS, type Kind } from '@/lib/iadopt/kinds';
import { LAYOUT_WIDTH } from '@/lib/iadopt/layout';
import { LIVE_PALETTE } from '@/lib/iadopt/palette';
import { cn } from '@/lib/utils';
import { DiagramSvg } from '../diagram/DiagramSvg';
import { useWorkspace, variableTitle } from '../../state/workspace';

// Starts below the smallest fit scale (0.4), so zooming out always zooms out.
const ZOOM_STEPS = [0.25, 0.33, 0.4, 0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];
const DIAGRAM_WIDTH = LAYOUT_WIDTH + 20;

function useContainerWidth(ref: React.RefObject<HTMLElement | null>) {
  const [width, setWidth] = React.useState(0);
  React.useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(element);
    setWidth(element.getBoundingClientRect().width);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

export function DiagramView() {
  const workspace = useWorkspace();
  const container = React.useRef<HTMLDivElement>(null);
  const width = useContainerWidth(container);
  const [zoom, setZoom] = React.useState<number | 'fit'>('fit');
  const idPrefix = React.useId().replace(/:/g, '');

  const fitScale = width > 0 ? Math.min(1.25, Math.max(0.4, (width - 48) / DIAGRAM_WIDTH)) : 1;
  const scale = zoom === 'fit' ? fitScale : zoom;

  function step(direction: 1 | -1) {
    const current = scale;
    const next = direction > 0
      ? ZOOM_STEPS.find((value) => value > current + 0.001) ?? current
      : [...ZOOM_STEPS].reverse().find((value) => value < current - 0.001) ?? current;
    setZoom(next);
  }

  // Only the error of the visible Turtle; an older one no longer applies.
  const { turtleError: renderError, layout } = workspace;

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div className="absolute top-3 right-3 z-10 flex items-center gap-2">
        <OrderPopover />
        <div className="flex h-8 items-center rounded-md border border-border bg-surface shadow-xs">
          <button type="button" aria-label="Zoom out" onClick={() => step(-1)} className="flex size-8 items-center justify-center rounded-l-md text-text-2 hover:bg-surface-2 hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/40 focus-visible:outline-none">
            <MinusIcon className="size-3.5" aria-hidden="true" />
          </button>
          <span className="w-12 text-center font-mono text-xs text-text-2" aria-live="polite">{Math.round(scale * 100)}%</span>
          <button type="button" aria-label="Zoom in" onClick={() => step(1)} className="flex size-8 items-center justify-center rounded-r-md text-text-2 hover:bg-surface-2 hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/40 focus-visible:outline-none">
            <PlusIcon className="size-3.5" aria-hidden="true" />
          </button>
        </div>
        <Button variant="outline" size="icon-sm" aria-label="Fit diagram to view" title="Fit diagram to view" onClick={() => setZoom('fit')}>
          <Maximize2Icon className="size-3.5" aria-hidden="true" />
        </Button>
      </div>

      <div ref={container} className="canvas-dots min-h-0 flex-1 overflow-auto px-6 pt-16 pb-24">
        {renderError && (
          <div className="mx-auto mb-6 max-w-2xl">
            <Callout tone="danger" title={`Error during ${renderError.phase.toLowerCase()}`}>
              <div className="font-mono text-xs break-words whitespace-pre-wrap">{renderError.message}</div>
              {renderError.phase === 'Rendering' && (
                <div className="mt-2">
                  Please report the bug to the{' '}
                  <a
                    href={`https://github.com/SirkoS/iadopt-vis/issues/new?template=bug_remote.yaml&src=${encodeURIComponent(renderError.source)}&what-happened=${encodeURIComponent(renderError.message)}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="font-semibold text-destructive-soft-foreground underline"
                  >
                    I-ADOPT Visualizer project
                  </a>.
                </div>
              )}
            </Callout>
          </div>
        )}
        {layout ? (
          <div style={{ width: DIAGRAM_WIDTH * scale }} className="mx-auto">
            <DiagramSvg
              layout={layout}
              palette={LIVE_PALETTE}
              idPrefix={idPrefix}
              mode="live"
              title={variableTitle(workspace.variable)}
              width={DIAGRAM_WIDTH * scale}
              selected={workspace.selected}
              isChecked={workspace.isChecked}
              onSelect={workspace.select}
            />
          </div>
        ) : (
          !renderError && <p className="mx-auto max-w-md text-center text-sm text-muted-foreground">The diagram appears here.</p>
        )}
      </div>

      <Legend />
    </div>
  );
}

function Legend() {
  const kinds: Kind[] = ['var', 'prop', 'ooi', 'mat', 'ctx', 'stat', 'con'];
  return (
    <div className="pointer-events-none absolute right-4 bottom-4 left-4 flex">
      <ul className="pointer-events-auto m-0 flex max-w-full list-none flex-wrap items-center gap-x-3.5 gap-y-1.5 rounded-[10px] border border-border bg-surface px-3 py-2 text-xs text-text-2 shadow-sm">
        {kinds.map((kind) => (
          <li key={kind} className="flex items-center gap-1.5">
            <span aria-hidden="true" className={cn('size-2 rounded-[2px]', kind === 'con' ? 'border-[1.5px] border-dashed border-con bg-transparent' : KIND_DOT[kind])} />
            {KIND_NAMES[kind]}
          </li>
        ))}
      </ul>
    </div>
  );
}

function OrderPopover() {
  const { order, setOrder } = useWorkspace();
  const letters = order.toLowerCase().split('');

  function move(index: number, delta: -1 | 1) {
    const next = [...letters];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    setOrder(next.join('').toUpperCase());
  }

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          title="Display order of components"
          className="flex h-8 items-center gap-2 rounded-md border border-border bg-surface pr-2 pl-2.5 text-xs font-medium text-text-2 shadow-xs hover:bg-surface-2 focus-visible:ring-[3px] focus-visible:ring-ring/40 focus-visible:outline-none"
        >
          Order
          <span className="flex gap-[3px]" aria-hidden="true">
            {letters.map((letter) => (
              <span key={letter} className={cn('flex size-[18px] items-center justify-center rounded-[4px] border font-mono text-[11px] font-semibold uppercase', KIND_CHIP[ORDER_LETTERS[letter]?.kind ?? 'ooi'])}>
                {letter}
              </span>
            ))}
          </span>
          <span className="sr-only">: {letters.map((letter) => ORDER_LETTERS[letter]?.label).join(', ')}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72">
        <div className="mb-1 text-sm font-semibold">Display order</div>
        <p className="mt-0 mb-3 text-xs text-muted-foreground">Left to right in the diagram.</p>
        <ol className="m-0 flex list-none flex-col gap-1 p-0">
          {letters.map((letter, index) => {
            const item = ORDER_LETTERS[letter];
            return (
              <li key={letter} className="flex items-center gap-2 rounded-md border border-border px-2 py-1">
                <span className={cn('size-2 rounded-[2px]', KIND_DOT[item.kind])} aria-hidden="true" />
                <span className="flex-1 text-[13px]">{item.label}</span>
                <Button variant="ghost" size="icon-sm" aria-label={`Move ${item.label} left`} disabled={index === 0} onClick={() => move(index, -1)}>
                  <ArrowUpIcon aria-hidden="true" />
                </Button>
                <Button variant="ghost" size="icon-sm" aria-label={`Move ${item.label} right`} disabled={index === letters.length - 1} onClick={() => move(index, 1)}>
                  <ArrowDownIcon aria-hidden="true" />
                </Button>
              </li>
            );
          })}
        </ol>
      </PopoverContent>
    </Popover>
  );
}
