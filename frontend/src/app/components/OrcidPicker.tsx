import * as React from 'react';
import { XIcon } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/controls';
import { Spinner } from '@/components/ui/feedback';
import { Input, Label } from '@/components/ui/fields';
import { normalizeOrcid } from '@/lib/applyPreNanopubSettingsToTurtle.js';
import { bareOrcid } from '@/lib/orcid';
import { readString, REMEMBERED_ORCID_KEY, writeString } from '@/lib/storage';
import { cn } from '@/lib/utils';
import { AuthRequiredError, fetchSavedOrcids, removeSavedOrcid, saveOrcid, type SavedOrcid } from '@/shared/api';
import { useAuth } from '@/shared/auth';

/*
 * The creator ORCID iD for publishing and retracting (D-035). An account keeps several saved
 * iDs: the person picks one of them or types another, which they may save to the account. The
 * saved list replaces the old one-iD memory of this browser.
 */

export function OrcidMark({ className }: { className?: string }) {
  return (
    <span aria-hidden="true" className={cn('inline-flex size-5 shrink-0 items-center justify-center rounded-full bg-[#A6CE39] text-[10px] font-bold tracking-tight text-white', className)}>
      iD
    </span>
  );
}

/** Checks an ORCID iD as it is typed; the messages are the ones the previous page showed. */
function orcidStatus(value: string): { uri: string | null; error: string | null } {
  if (!value.trim()) return { uri: null, error: null };
  try {
    return { uri: normalizeOrcid(value), error: null };
  } catch (error) {
    return { uri: null, error: error instanceof Error ? error.message : String(error) };
  }
}

function validUri(value: string | null): string | null {
  return value ? orcidStatus(value).uri : null;
}

export interface OrcidChoice {
  /** False until the saved list has arrived; nothing can be chosen or used before. */
  loaded: boolean;
  /** The account's saved iDs, most recently used first. */
  saved: SavedOrcid[];
  /** False when the saved list could not be loaded (for example without accounts): nothing is saved then. */
  canSave: boolean;
  /** The chosen saved iD, or null while the typed "Another ORCID iD" is in use. */
  chosen: SavedOrcid | null;
  /** Choose a saved iD by its URI, or null for the typed one. */
  select: (uri: string | null) => void;
  text: string;
  setText: (value: string) => void;
  /** Whether a typed iD is saved to the account after a successful publication or retraction. */
  save: boolean;
  setSave: (value: boolean) => void;
  setTouched: (value: boolean) => void;
  /** The chosen iD as a canonical URI; null while none is chosen or the typed one is invalid. */
  uri: string | null;
  error: string | null;
  inputRef: React.RefObject<HTMLInputElement | null>;
  /** Fixes the choice: shows what is missing, focuses the field, and returns the iD or null. */
  require: () => string | null;
  /** The iD whose removal is running; only one runs at a time. */
  removing: string | null;
  remove: (uri: string) => Promise<void>;
  /** Call after success: saves a typed iD when asked and moves a used saved iD to the top. */
  remember: (uri: string) => Promise<void>;
}

/**
 * State of the ORCID choice. Every new `session` (a dialog opening, a retraction target) starts
 * over and waits for the saved list. The first choice is then the draft's or publication's own
 * creator (`findPreferred`), then the iD this browser remembered, then the most recently used
 * saved iD; an iD that is not saved is shown in the typed field, so whatever is used is always
 * visible.
 */
export function useOrcidChoice(
  session: string | null,
  emptyMessage: string,
  findPreferred: () => Promise<string | null>,
): OrcidChoice {
  const auth = useAuth();
  const [loaded, setLoaded] = React.useState(false);
  const [saved, setSaved] = React.useState<SavedOrcid[]>([]);
  const [canSave, setCanSave] = React.useState(false);
  const [selection, setSelection] = React.useState<string | null>(null);
  const [text, setText] = React.useState('');
  const [save, setSave] = React.useState(false);
  const [touched, setTouched] = React.useState(false);
  const [removing, setRemoving] = React.useState<string | null>(null);
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  // The iD the previous frontend remembered in this browser, as a canonical URI.
  const remembered = React.useRef<string | null>(null);
  // Counts every opening and closing, so an answer that belongs to an earlier one is dropped
  // even when the session key repeats.
  const generation = React.useRef(0);
  const findPreferredRef = React.useRef(findPreferred);
  findPreferredRef.current = findPreferred;

  React.useEffect(() => {
    generation.current += 1;
    if (session === null) return;
    const started = generation.current;
    remembered.current = validUri(readString(REMEMBERED_ORCID_KEY));
    setLoaded(false);
    setSaved([]);
    setCanSave(false);
    setSelection(null);
    setText('');
    setSave(false);
    setTouched(false);
    setRemoving(null);
    Promise.all([
      fetchSavedOrcids().then(
        (list) => ({ list, ok: true }),
        (error: unknown) => {
          console.error(error);
          return { list: [] as SavedOrcid[], ok: false };
        },
      ),
      findPreferredRef.current().catch(() => null),
    ]).then(([{ list, ok }, preferred]) => {
      if (generation.current !== started) return;
      const legacy = remembered.current;
      // Already on the account, so this browser's copy is no longer needed.
      if (legacy && list.some((item) => item.orcid === legacy)) writeString(REMEMBERED_ORCID_KEY, null);
      const candidate = validUri(preferred) ?? legacy;
      if (candidate && list.some((item) => item.orcid === candidate)) {
        setSelection(candidate);
      } else if (candidate) {
        // Only an iD remembered on purpose starts with saving ticked.
        setText(bareOrcid(candidate));
        setSave(ok && candidate === legacy);
      } else if (list.length) {
        setSelection(list[0].orcid);
      }
      setSaved(list);
      setCanSave(ok);
      setLoaded(true);
    });
  }, [session]);

  const chosen = (selection && saved.find((item) => item.orcid === selection)) || null;
  const status = orcidStatus(text);
  const uri = !loaded ? null : chosen ? chosen.orcid : status.uri;
  const error = !loaded || chosen ? null : status.error ?? (touched && !text.trim() ? emptyMessage : null);

  function require(): string | null {
    if (!loaded) return null;
    setTouched(true);
    if (!uri) inputRef.current?.focus();
    return uri;
  }

  async function remove(target: string) {
    if (removing) return;
    const started = generation.current;
    setRemoving(target);
    try {
      const list = await auth.runProtected(() => removeSavedOrcid(target), 'remove a saved ORCID iD');
      if (generation.current !== started) return;
      setSaved(list);
      // A choice that is gone falls back to the typed field, so nothing hidden is ever used.
      setSelection((current) => (current && list.some((item) => item.orcid === current) ? current : null));
    } catch (caught) {
      if (caught instanceof AuthRequiredError) return;
      console.error(caught);
      toast('The ORCID iD was not removed.', { description: caught instanceof Error ? caught.message : undefined });
      // Show the account's real list again, for example when another tab removed it already.
      fetchSavedOrcids()
        .then((list) => {
          if (generation.current === started) setSaved(list);
        })
        .catch(() => {});
    } finally {
      if (generation.current === started) setRemoving(null);
    }
  }

  async function remember(used: string) {
    const started = generation.current;
    const legacy = remembered.current;
    // Without accounts nothing is saved, and this browser's iD stays where it is.
    if (!canSave) return;
    if (!saved.some((item) => item.orcid === used) && !save) {
      // The browser's iD was used without saving it: the person let it go.
      if (legacy === used) writeString(REMEMBERED_ORCID_KEY, null);
      return;
    }
    try {
      const list = await auth.runProtected(() => saveOrcid(used), 'save your ORCID iD');
      if (legacy && list.some((item) => item.orcid === legacy)) writeString(REMEMBERED_ORCID_KEY, null);
      if (generation.current !== started) return;
      setSaved(list);
      setSelection(used);
    } catch (caught) {
      if (caught instanceof AuthRequiredError) return;
      console.error(caught);
      toast('Your ORCID iD was not saved to your account.', { description: caught instanceof Error ? caught.message : undefined });
    }
  }

  return {
    loaded, saved, canSave, chosen, select: setSelection, text, setText, save, setSave, setTouched,
    uri, error, inputRef, require, removing, remove, remember,
  };
}

/**
 * The picker. Without saved iDs it is the single "Your ORCID iD" field; with them, a radio list
 * of the saved iDs plus "Another ORCID iD". The typed field has the id `${idPrefix}-orcid`.
 */
export function OrcidPicker({
  choice,
  idPrefix,
  hint,
  describedBy,
  autoFocus,
  disabled,
}: {
  choice: OrcidChoice;
  idPrefix: string;
  /** Shown under the field when nothing is wrong; without it, a valid typed iD is confirmed. */
  hint?: string;
  /** Id of help text elsewhere that describes the field. */
  describedBy?: string;
  autoFocus?: boolean;
  /** While the publication or retraction runs, the choice cannot change. */
  disabled?: boolean;
}) {
  const inputId = `${idPrefix}-orcid`;
  const messageId = `${idPrefix}-orcid-message`;

  // The layout depends on the saved list, so nothing is shown, focused or used before it arrives.
  if (!choice.loaded) {
    return (
      <div className="flex flex-col gap-1.5" aria-busy="true">
        <span className="text-[13px] leading-5 font-medium text-text-2">Your ORCID iD</span>
        <div className="flex h-11 items-center gap-2 rounded-md border border-dashed border-border px-3 text-[13px] text-muted-foreground">
          <Spinner className="size-4" />
          Loading your saved ORCID iDs…
        </div>
      </div>
    );
  }

  const typing = choice.chosen === null;
  const message = choice.error ?? hint ?? (typing && choice.uri ? 'Valid ORCID iD. The check digit matches.' : '');

  const field = (props: { 'aria-label'?: string; autoFocus?: boolean }) => (
    <div className="relative">
      <OrcidMark className="absolute top-3 left-3" />
      <Input
        id={inputId}
        ref={choice.inputRef}
        {...props}
        disabled={disabled}
        autoComplete="off"
        spellCheck={false}
        placeholder="0000-0000-0000-0000"
        value={choice.text}
        aria-invalid={Boolean(choice.error)}
        aria-describedby={[messageId, describedBy].filter(Boolean).join(' ')}
        onChange={(event) => choice.setText(event.target.value)}
        onBlur={() => choice.setTouched(true)}
        className="h-11 pl-10 font-mono text-[15px] tracking-[0.02em]"
      />
    </div>
  );

  const messageLine = (
    <div
      id={messageId}
      role="alert"
      className={cn(
        'min-h-[18px] text-[13px] leading-[18px]',
        choice.error ? 'text-destructive-soft-foreground' : hint ? 'text-muted-foreground' : 'text-primary-soft-foreground',
      )}
    >
      {message}
    </div>
  );

  const saveBox = choice.canSave && (
    <div className="flex items-center gap-2.5 text-[13px] text-text-2">
      <Checkbox id={`${idPrefix}-save-orcid`} disabled={disabled} checked={choice.save} onCheckedChange={(value) => choice.setSave(value === true)} />
      <label htmlFor={`${idPrefix}-save-orcid`}>Save this ORCID iD to my account</label>
    </div>
  );

  if (!choice.saved.length) {
    return (
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={inputId} className={cn(choice.error && 'text-destructive-soft-foreground')}>Your ORCID iD</Label>
        {field({ autoFocus })}
        {messageLine}
        {saveBox}
      </div>
    );
  }

  const name = `${idPrefix}-orcid-choice`;
  return (
    <fieldset className="m-0 flex min-w-0 flex-col gap-2 border-0 p-0" aria-describedby={describedBy}>
      <legend className="mb-1.5 p-0 text-[13px] leading-5 font-medium text-text-2">Your ORCID iD</legend>
      {choice.saved.map((item) => {
        const selected = choice.chosen?.orcid === item.orcid;
        // While its removal runs, a row cannot be chosen; while any removal runs, none starts.
        const removing = choice.removing === item.orcid;
        return (
          <div
            key={item.orcid}
            className={cn('flex items-center gap-1 rounded-[10px] border pr-1.5 transition-colors', selected ? 'border-primary bg-primary-soft' : 'border-border bg-surface hover:bg-surface-2')}
          >
            <label className={cn('flex min-w-0 flex-1 items-center gap-3 py-2.5 pl-3', removing || disabled ? 'opacity-60' : 'cursor-pointer')}>
              <input
                type="radio"
                name={name}
                value={item.orcid}
                checked={selected}
                disabled={removing || disabled}
                autoFocus={autoFocus && selected}
                onChange={() => choice.select(item.orcid)}
                className="size-4 shrink-0 accent-primary"
              />
              <OrcidMark className="size-[18px] text-[9px]" />
              <span className="flex min-w-0 flex-col">
                {item.name && <span className="truncate text-sm font-medium text-foreground">{item.name}</span>}
                <span className="font-mono text-xs text-text-2">{bareOrcid(item.orcid)}</span>
              </span>
            </label>
            <Button type="button" variant="ghost" size="icon-sm" disabled={choice.removing !== null || disabled} onClick={() => void choice.remove(item.orcid)}>
              <XIcon aria-hidden="true" />
              <span className="sr-only">Remove {bareOrcid(item.orcid)} from my account</span>
            </Button>
          </div>
        );
      })}
      <div className={cn('flex flex-col rounded-[10px] border transition-colors', typing ? 'border-primary' : 'border-border')}>
        <label className={cn('flex items-center gap-3 px-3 py-2.5 text-sm', disabled ? 'opacity-60' : 'cursor-pointer')}>
          <input
            type="radio"
            name={name}
            value="another"
            checked={typing}
            disabled={disabled}
            autoFocus={autoFocus && typing}
            onChange={() => choice.select(null)}
            className="size-4 shrink-0 accent-primary"
          />
          Another ORCID iD
        </label>
        {typing && (
          <div className="flex flex-col gap-1.5 px-3 pb-3">
            {field({ 'aria-label': 'Another ORCID iD' })}
            {messageLine}
            {saveBox}
          </div>
        )}
      </div>
      {!typing && hint && <div className="text-[13px] leading-[18px] text-muted-foreground">{hint}</div>}
    </fieldset>
  );
}
