import { ChevronDownIcon, DownloadIcon } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/menus';
import toTurtle from '@/model/toTurtle.js';
import { useAuth } from '@/shared/auth';
import { diagramPngBlob, diagramSvgBlob, downloadBlob, exportFileName } from '../diagram/exportDiagram';
import { useWorkspace, variableTitle } from '../../state/workspace';

/** Local exports, open to everyone: SVG and PNG of the diagram, and the visible Turtle. */
export function useExport() {
  const workspace = useWorkspace();
  const { logEvent } = useAuth();

  return async function run(format: 'svg' | 'png' | 'ttl') {
    try {
      const iri = workspace.variable?.getIri();
      const filename = exportFileName(iri, format);
      // The visible Turtle is what download and publishing use, as before.
      const ttl = workspace.turtle.trim() || (workspace.variable ? toTurtle(workspace.variable) : '');
      let blob: Blob;
      if (format === 'ttl') {
        blob = new Blob([ttl], { type: 'text/turtle;charset=utf-8' });
      } else {
        if (!workspace.layout) throw new Error('There is no diagram to export yet.');
        const title = variableTitle(workspace.variable);
        blob = format === 'svg' ? await diagramSvgBlob(workspace.layout, title) : await diagramPngBlob(workspace.layout, title);
      }
      downloadBlob(blob, filename);
      logEvent('export', { format, filename, ttl });
    } catch (error) {
      console.error(error);
      const message = error instanceof Error ? error.message : String(error);
      logEvent('export_failed', {}, { error: message });
      toast.error(message || 'Export failed.');
    }
  };
}

export function ExportMenu() {
  const workspace = useWorkspace();
  const run = useExport();

  const disabled = !workspace.layout && !workspace.turtle.trim();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" disabled={disabled}>
          <DownloadIcon aria-hidden="true" /> Export <ChevronDownIcon className="text-muted-foreground" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60">
        <DropdownMenuItem disabled={!workspace.layout} onSelect={() => void run('svg')}>
          <span className="flex-1">Diagram as SVG</span><span className="font-mono text-xs text-muted-foreground">.svg</span>
        </DropdownMenuItem>
        <DropdownMenuItem disabled={!workspace.layout} onSelect={() => void run('png')}>
          <span className="flex-1">Diagram as PNG</span><span className="font-mono text-xs text-muted-foreground">.png</span>
        </DropdownMenuItem>
        <DropdownMenuItem disabled={!workspace.turtle.trim() && !workspace.variable} onSelect={() => void run('ttl')}>
          <span className="flex-1">Turtle</span><span className="font-mono text-xs text-muted-foreground">.ttl</span>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="font-normal">Exports stay on your device and need no account.</DropdownMenuLabel>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
