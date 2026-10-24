import * as React from 'react';
import { ClipboardPasteIcon, FileUpIcon } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Label, Textarea } from '@/components/ui/fields';
import { navigate } from '../router';
import { useWorkspace } from '../state/workspace';

/** Open a Turtle file or paste Turtle: both replace the workspace content and draw the diagram. */
export function useOpenTurtle() {
  const workspace = useWorkspace();
  const fileInput = React.useRef<HTMLInputElement>(null);
  const [pasteOpen, setPasteOpen] = React.useState(false);

  const { openTurtle } = workspace;
  const load = React.useCallback(async (text: string) => {
    navigate('workspace');
    await openTurtle(text);
  }, [openTurtle]);

  const element = (
    <>
      <input
        ref={fileInput}
        type="file"
        accept=".ttl,.n3,.nt,text/turtle"
        className="hidden"
        aria-hidden="true"
        tabIndex={-1}
        onChange={async (event) => {
          const file = event.target.files?.[0];
          event.target.value = '';
          if (file) await load(await file.text());
        }}
      />
      <PasteTurtleDialog open={pasteOpen} onOpenChange={setPasteOpen} onSubmit={async (text) => { setPasteOpen(false); await load(text); }} />
    </>
  );

  return {
    element,
    openFile: () => fileInput.current?.click(),
    openPaste: () => setPasteOpen(true),
  };
}

function PasteTurtleDialog({
  open,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (text: string) => void;
}) {
  const [text, setText] = React.useState('');
  React.useEffect(() => {
    if (!open) setText('');
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Paste Turtle</DialogTitle>
          <DialogDescription>Paste an I-ADOPT variable in Turtle. It is drawn in your browser; nothing is sent.</DialogDescription>
        </DialogHeader>
        <DialogBody className="flex flex-col gap-1.5">
          <Label htmlFor="paste-turtle">Turtle</Label>
          <Textarea
            id="paste-turtle"
            rows={14}
            spellCheck={false}
            className="font-mono text-[12.5px] leading-5"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={'@prefix iop: <https://w3id.org/iadopt/ont/> .\n…'}
          />
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={!text.trim()} onClick={() => onSubmit(text)}>Visualize</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function OpenTurtleButtons({ open }: { open: ReturnType<typeof useOpenTurtle> }) {
  return (
    <div className="flex flex-wrap gap-2">
      <Button variant="outline" size="sm" onClick={open.openFile}><FileUpIcon aria-hidden="true" />Open file</Button>
      <Button variant="outline" size="sm" onClick={open.openPaste}><ClipboardPasteIcon aria-hidden="true" />Paste Turtle</Button>
    </div>
  );
}
