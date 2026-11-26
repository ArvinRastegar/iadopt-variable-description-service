import * as React from 'react';
import { BadgeCheckIcon } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Callout, Spinner } from '@/components/ui/feedback';
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Label, Textarea } from '@/components/ui/fields';
import { useAuth } from '@/shared/auth';
import { MAX_NOTE_CHARS } from '../state/recording';
import { useWorkspace } from '../state/workspace';

/*
 * "Mark as best" (D-037): the person says which exact version of the decomposition is right.
 * Publishing needs it. Components without "Looks right" bring a warning, not a block, and their
 * number is kept with the mark; the note is optional and invited. A later mark replaces an earlier
 * one; a mark cannot be taken back.
 */

/** The owner's wording for the note (D-037). */
export const NOTE_PROMPT = 'To help us improve, please tell us how you changed it or what was the logic of your change, if you feel like it.';

/** Why marking is not possible now, or null. */
export function markBlockedReason(
  workspace: Pick<ReturnType<typeof useWorkspace>, 'decomposing' | 'turtleDirty' | 'layout'>,
  auth: Pick<ReturnType<typeof useAuth>, 'user' | 'recorded' | 'recordRefusal'>,
): string | null {
  if (auth.recordRefusal) return auth.recordRefusal;
  if (auth.user && !auth.recorded) {
    return 'Only accounts can mark a version as best, and sign-in is switched off on this deployment, so nothing can be published from it.';
  }
  if (workspace.decomposing) return 'Wait until the decomposition is finished.';
  if (workspace.turtleDirty) return 'The Turtle has edits the diagram does not show. Update the diagram first, so you mark what you reviewed.';
  if (!workspace.layout) return 'There is no drawn variable to mark yet.';
  return null;
}

export function useMarkBestDialog() {
  const auth = useAuth();
  const [open, setOpen] = React.useState(false);
  return {
    open,
    setOpen,
    request: async () => {
      if (await auth.requireUser('mark a version as best')) setOpen(true);
    },
  };
}

export function MarkBestDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const auth = useAuth();
  const workspace = useWorkspace();
  const [note, setNote] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) return;
    setNote('');
    setError(null);
  }, [open]);

  const total = workspace.components.length;
  const unchecked = total - workspace.checkedCount;
  const blocked = markBlockedReason(workspace, auth);
  const length = Array.from(note).length;

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      const result = await workspace.markBest(note);
      if (result.ok) {
        onOpenChange(false);
        toast(`Version ${result.version} is marked as best`);
      } else if (result.signIn) {
        // The sign-in dialog takes over; the work and the note's page stay as they are.
        onOpenChange(false);
      } else {
        setError(result.message);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="max-w-[540px]">
        <form
          className="flex min-h-0 flex-col"
          onSubmit={(event) => {
            event.preventDefault();
            if (!blocked && !busy) void confirm();
          }}
        >
          <DialogHeader>
            <DialogTitle>Mark this as the best decomposition?</DialogTitle>
            <DialogDescription>
              Only the version you mark as best can be published. It is kept with your account, together with the model’s draft and the changes you made, so we can measure and improve the service.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="flex flex-col gap-4">
            {blocked ? (
              <Callout tone="warning">{blocked}</Callout>
            ) : unchecked > 0 ? (
              <Callout
                tone="warning"
                title={unchecked === total ? `None of the ${total} components has “Looks right” yet` : `${unchecked} of ${total} components have no “Looks right” yet`}
              >
                You can still mark this version as best. How many components were not checked is kept with the mark.
              </Callout>
            ) : (
              <Callout tone="success">You checked all {total} components.</Callout>
            )}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="mark-best-note" className="leading-[19px]">
                {NOTE_PROMPT} <span className="font-normal text-muted-foreground">(optional)</span>
              </Label>
              <Textarea
                id="mark-best-note"
                rows={4}
                maxLength={MAX_NOTE_CHARS}
                value={note}
                disabled={Boolean(blocked) || busy}
                aria-describedby="mark-best-count"
                placeholder="For example: the model used water instead of sea water."
                onChange={(event) => setNote(event.target.value)}
              />
              <div id="mark-best-count" className="text-right text-xs text-muted-foreground tabular-nums">
                {length} / {MAX_NOTE_CHARS}
              </div>
            </div>
            {error && <Callout tone="danger" title="This version was not marked as best">{error}</Callout>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" disabled={busy} onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={Boolean(blocked) || busy}>
              {busy ? <Spinner /> : <BadgeCheckIcon aria-hidden="true" />}
              Mark as best
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** The header control next to Publish: mark the version, see that it is marked, or that an earlier one is. */
export function MarkBestButton({ onRequest }: { onRequest: () => void }) {
  const workspace = useWorkspace();
  const disabled = !workspace.layout || workspace.decomposing;
  if (workspace.markStatus === 'marked') {
    return (
      <Button
        variant="secondary"
        className="bg-primary-soft text-primary-soft-foreground hover:bg-primary-soft"
        disabled={disabled}
        title="Mark it again, for example to add a note"
        onClick={onRequest}
      >
        <BadgeCheckIcon aria-hidden="true" /> Marked as best · v{workspace.record?.markedVersion}
      </Button>
    );
  }
  if (workspace.markStatus === 'outdated') {
    return (
      <span className="inline-flex h-9 items-center gap-1 rounded-md border border-warning-line bg-warning-soft pl-3 text-[13px] font-semibold text-warning-foreground">
        You marked an earlier version
        <Button variant="ghost" size="sm" className="text-warning-foreground hover:bg-transparent hover:underline" disabled={disabled} onClick={onRequest}>
          Mark this version
        </Button>
      </span>
    );
  }
  return (
    <Button variant="outline" disabled={disabled} onClick={onRequest}>
      <BadgeCheckIcon aria-hidden="true" /> Mark as best
    </Button>
  );
}
