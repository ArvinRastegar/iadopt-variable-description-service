import * as React from 'react';

import { clientInfo } from '@/lib/clientInfo';
import extract from '@/lib/extract.js';
import { clampChanges, clip, diffComponents, snapshotComponents, type ComponentState } from '@/lib/iadopt/changes';
import {
  ApiError,
  AuthRequiredError,
  postRecordEvent,
  startRecord,
  type RecordChange,
  type RecordEventBody,
  type RecordRole,
} from '@/shared/api';
import type { useAuth } from '@/shared/auth';

/*
 * The decomposition record of the work in the workspace (D-037). Only a signed-in account is
 * recorded: never an anonymous visitor, and never the development user of a deployment with
 * authentication switched off. The record starts with a decomposition (the backend writes the run
 * and its draft as version 0) or with Turtle the person opens or pastes (version 0, imported). Then
 * every applied change is one step with its component changes, every "Looks right" and Undo is a
 * check or uncheck, and "Mark as best" marks the version the person sees.
 *
 * Steps go out one at a time, in order, because each names the version it was taken on. A step
 * that fails never blocks the workspace; the next step records the difference, since the record
 * always compares whole versions. Work that was not recorded, such as edits before signing in, is
 * not replayed: the record starts from the Turtle as it is at the first recorded step.
 */

type Auth = ReturnType<typeof useAuth>;

/** Longest note on a best mark, in characters, as the backend counts them. */
export const MAX_NOTE_CHARS = 500;

const SIGN_IN = 'Sign in to mark a version as best.';
const AUTH_OFF = 'Only accounts can mark a version as best, and sign-in is switched off on this deployment.';

/** The record of the workspace's work as this browser knows it; it is saved with the local draft. */
export interface RecordState {
  id: string;
  /** The account the record belongs to; no other account adds to it. */
  ownerId: number;
  /** The version the record is on. */
  version: number;
  /** The Turtle of that version, as it was sent. */
  turtle: string;
  /** The version most recently marked as best, if any. */
  markedVersion: number | null;
  /** Whether that mark still covers `version`: false once an edit changed the description. */
  markHolds: boolean;
}

/** `marked`: the version the person sees is the one marked as best; `outdated`: an earlier one is. */
export type MarkStatus = 'none' | 'marked' | 'outdated';

/**
 * Whether the version shown is the one marked as best. The mark covers the record's version, so
 * the Turtle shown must be that version, or the one a metadata step on its way will record. A step
 * that failed or was not sent (signed out, refused, offline) leaves the record behind what is
 * shown, and the mark then no longer counts until the person marks again.
 *
 * @param shown  The Turtle the diagram was drawn from; without it, only the record is consulted.
 * @param pendingMetadata  The Turtle of a metadata step not yet answered, if any.
 */
export function markStatusOf(record: RecordState | null, shown?: string, pendingMetadata?: string | null): MarkStatus {
  if (!record || record.markedVersion === null) return 'none';
  if (!record.markHolds) return 'outdated';
  if (shown === undefined || sameTurtle(record.turtle, shown)) return 'marked';
  return pendingMetadata != null && sameTurtle(pendingMetadata, shown) ? 'marked' : 'outdated';
}

/** A record state read back from browser storage, or null when it is not one. */
export function readRecordState(value: unknown): RecordState | null {
  if (!value || typeof value !== 'object') return null;
  const state = value as Partial<RecordState>;
  if (typeof state.id !== 'string' || !/^[0-9a-f]{16}$/.test(state.id)) return null;
  if (typeof state.ownerId !== 'number' || typeof state.version !== 'number' || typeof state.turtle !== 'string') return null;
  const markedVersion = typeof state.markedVersion === 'number' ? state.markedVersion : null;
  return {
    id: state.id,
    ownerId: state.ownerId,
    version: state.version,
    turtle: state.turtle,
    markedVersion,
    markHolds: markedVersion !== null && state.markHolds === true,
  };
}

/** Line endings and surrounding whitespace do not make another Turtle. */
export function sameTurtle(a: string, b: string): boolean {
  return a.replace(/\r\n?/g, '\n').trim() === b.replace(/\r\n?/g, '\n').trim();
}

/**
 * The record a saved draft had, coming back with the draft's Turtle. When that Turtle is not the
 * record's version, as when the draft kept Turtle-tab edits that were not drawn yet (they are
 * drawn when the draft comes back), the mark no longer covers what the person sees.
 */
export function restoredRecord(value: unknown, turtle: string): RecordState | null {
  const record = readRecordState(value);
  if (!record || sameTurtle(turtle, record.turtle)) return record;
  return { ...record, markHolds: false };
}

/** One applied change: the Turtle and components before and after it. */
export interface AppliedChange {
  /** `metadata` only adds or changes nanopublication metadata; the backend checks that. */
  type: 'edit' | 'metadata';
  beforeTurtle: string;
  before: ComponentState[] | null;
  afterTurtle: string;
  after: ComponentState[] | null;
}

/** "Looks right" given (`checked`) or taken back on one component of the version the person sees. */
export interface CheckedComponent {
  checked: boolean;
  role: RecordRole;
  component: string;
  turtle: string;
  states: ComponentState[] | null;
}

export interface BestMark {
  note: string;
  /** Components without "Looks right" at this moment. */
  unchecked: number;
  turtle: string;
  states: ComponentState[] | null;
}

export type MarkResult = { ok: true; version: number } | { ok: false; message: string; signIn?: boolean };

export interface Recorder {
  record: RecordState | null;
  /** The Turtle of a metadata step not yet answered, for `markStatusOf`. */
  pendingMetadata: string | null;
  /** New work replaced the workspace: drop the record, and start one for opened Turtle when signed in. */
  opened: (turtle: string | null, states: ComponentState[] | null) => void;
  /** A decomposition finished: its record, written by the backend, has the draft as version 0. */
  adopted: (recordId: string | null, turtle: string) => void;
  /** A saved draft came back, with the record it had. */
  restored: (record: RecordState | null) => void;
  /** Record one applied change; resolves when it was sent (or skipped). */
  applied: (change: AppliedChange) => Promise<void>;
  checked: (item: CheckedComponent) => void;
  markBest: (mark: BestMark) => Promise<MarkResult>;
  /** Resolves with the record once every step before it was sent. */
  flush: () => Promise<RecordState | null>;
  /** The backend refused a publication: the mark no longer holds, or the record is gone. */
  refused: (reason: 'outdated' | 'gone') => void;
}

type Started = RecordState | { failed: string; signIn?: boolean };
type Outcome<T> = { ok: true; value: T } | { ok: false; status: number; message: string; signIn?: boolean };

const now = () => new Date().toISOString();

export function useRecorder(auth: Auth, initial: RecordState | null): Recorder {
  const [record, setRecord] = React.useState<RecordState | null>(initial);
  const [pendingMetadata, setPendingMetadata] = React.useState<string | null>(null);
  const recordRef = React.useRef<RecordState | null>(initial);
  // The components of the record's current version, kept so that a step need not parse it again.
  const snapshotRef = React.useRef<{ turtle: string; states: ComponentState[] | null } | null>(null);
  const queueRef = React.useRef<Promise<unknown>>(Promise.resolve());
  const authRef = React.useRef(auth);
  authRef.current = auth;
  // The backend said records need accounts (409): nothing more is sent from this page.
  const offRef = React.useRef(false);
  // Turtle the backend would not record (400), with its reason, so it is not sent again.
  const refusedRef = React.useRef<{ turtle: string; message: string } | null>(null);

  const actions = React.useMemo(() => {
    const commit = (next: RecordState | null, states?: ComponentState[] | null) => {
      recordRef.current = next;
      if (!next) snapshotRef.current = null;
      else if (states !== undefined) snapshotRef.current = { turtle: next.turtle, states };
      setRecord(next);
    };

    const enqueue = <T,>(task: () => Promise<T>): Promise<T> => {
      const run = queueRef.current.then(task);
      queueRef.current = run.catch((error) => console.error(error));
      return run;
    };

    /** The account whose work is recorded now, or why nothing is. */
    const account = (): number | string => {
      const { user, recorded, recordRefusal } = authRef.current;
      if (!user) return SIGN_IN;
      if (recordRefusal) return recordRefusal;
      if (offRef.current || !recorded) return AUTH_OFF;
      return user.id as number;
    };

    const owned = (accountId: number) => {
      const current = recordRef.current;
      return current && current.ownerId === accountId ? current : null;
    };

    /** Send a request; `action` names a step the person asked for, which may ask them to sign in again. */
    const call = async <T,>(request: () => Promise<T>, action: string | null): Promise<Outcome<T>> => {
      const { runProtected, runQuietly } = authRef.current;
      try {
        return { ok: true, value: await (action ? runProtected(request, action) : runQuietly(request)) };
      } catch (error) {
        if (error instanceof AuthRequiredError) return { ok: false, status: 401, message: error.message, signIn: true };
        if (error instanceof ApiError) {
          console.error(`The record step was refused (${error.status}): ${error.message}`);
          // The record routes answer 409 only when the deployment has no accounts.
          if (error.status === 409) offRef.current = true;
          return { ok: false, status: error.status, message: error.status === 409 ? AUTH_OFF : error.message };
        }
        console.error(error);
        return { ok: false, status: 0, message: 'Could not reach the service. Check the connection and try again.' };
      }
    };

    const start = async (accountId: number, turtle: string, states: ComponentState[] | null, at: string, action: string | null): Promise<Started> => {
      if (!turtle.trim()) return { failed: 'There is no Turtle to record yet.' };
      const refused = refusedRef.current;
      if (refused && sameTurtle(refused.turtle, turtle)) return { failed: refused.message };
      const outcome = await call(() => startRecord({ turtle, occurred_at: at, client: clientInfo() }), action);
      if (!outcome.ok) {
        if (outcome.status === 400) refusedRef.current = { turtle, message: outcome.message };
        return { failed: outcome.message, signIn: outcome.signIn };
      }
      const next: RecordState = {
        id: outcome.value.record_id,
        ownerId: accountId,
        version: outcome.value.version,
        turtle,
        markedVersion: null,
        markHolds: false,
      };
      commit(next, states);
      return next;
    };

    const snapshotOf = async (current: RecordState): Promise<ComponentState[] | null> => {
      const known = snapshotRef.current;
      if (known && known.turtle === current.turtle) return known.states;
      let states: ComponentState[] | null = null;
      try {
        const [variable] = await extract(current.turtle);
        states = variable ? snapshotComponents(variable) : null;
      } catch {
        // A version that cannot be drawn has no components to compare with.
      }
      if (recordRef.current === current) snapshotRef.current = { turtle: current.turtle, states };
      return states;
    };

    const send = async (
      current: RecordState,
      body: Omit<RecordEventBody, 'occurred_at' | 'client'>,
      at: string,
      action: string | null,
    ) => {
      const outcome = await call(() => postRecordEvent(current.id, { ...body, occurred_at: at, client: clientInfo() }), action);
      // The record or its version is gone, as after the database was replaced: the next step starts a new record.
      if (!outcome.ok && outcome.status === 404 && recordRef.current === current) commit(null);
      return outcome;
    };

    const edit = async (change: AppliedChange, at: string, action: string | null, retried = false): Promise<Started> => {
      const accountId = account();
      if (typeof accountId === 'string') return { failed: accountId };
      let current = owned(accountId);
      if (!current) {
        // With nothing drawn before the change, what it produced is the first version.
        if (!change.beforeTurtle.trim()) return start(accountId, change.afterTurtle, change.after, at, action);
        const started = await start(accountId, change.beforeTurtle, change.before, at, action);
        if ('failed' in started) return started;
        current = started;
      }
      if (sameTurtle(current.turtle, change.afterTurtle)) return current;
      // Compare with the record's version: normally the Turtle the change was made on, or, after
      // steps that were not recorded, the last version the record has.
      const base = sameTurtle(current.turtle, change.beforeTurtle) ? change.before : await snapshotOf(current);
      let changes: RecordChange[] = base && change.after ? diffComponents(base, change.after) : [];
      if (!changes.length && change.type === 'edit') changes = [{ operation: 'other' }];
      const outcome = await send(
        current,
        { type: change.type, version: current.version, turtle: change.afterTurtle, changes: clampChanges(changes) },
        at,
        action,
      );
      if (!outcome.ok) {
        if (!retried && outcome.status === 404) return edit(change, at, action, true);
        // The backend found more than publication metadata in a metadata step: it is an edit.
        if (!retried && outcome.status === 400 && change.type === 'metadata') return edit({ ...change, type: 'edit' }, at, action, true);
        return { failed: outcome.message, signIn: outcome.signIn };
      }
      const next: RecordState = {
        ...current,
        version: outcome.value.version,
        turtle: change.afterTurtle,
        // An edit that made a new version leaves the mark behind; the backend accepts a metadata
        // step only when it keeps the marked description.
        markHolds: current.markHolds && (change.type === 'metadata' || outcome.value.version === current.version),
      };
      commit(next, change.after);
      return next;
    };

    /** The record, brought up to the Turtle the person sees, or why that is not possible. */
    const caughtUp = async (turtle: string, states: ComponentState[] | null, at: string, action: string | null): Promise<Started> => {
      const accountId = account();
      if (typeof accountId === 'string') return { failed: accountId };
      const current = owned(accountId);
      if (!current) return start(accountId, turtle, states, at, action);
      if (sameTurtle(current.turtle, turtle)) return current;
      return edit({ type: 'edit', beforeTurtle: current.turtle, before: await snapshotOf(current), afterTurtle: turtle, after: states }, at, action);
    };

    const check = async (item: CheckedComponent, at: string) => {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const current = await caughtUp(item.turtle, item.states, at, null);
        if ('failed' in current) return;
        const outcome = await send(
          current,
          {
            type: item.checked ? 'check' : 'uncheck',
            version: current.version,
            changes: clampChanges([{ operation: item.checked ? 'confirm' : 'unconfirm', role: item.role, component: item.component }]),
          },
          at,
          null,
        );
        if (outcome.ok || outcome.status !== 404) return;
      }
    };

    const mark = async (input: BestMark, at: string): Promise<MarkResult> => {
      const action = 'mark a version as best';
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const current = await caughtUp(input.turtle, input.states, at, action);
        if ('failed' in current) return { ok: false, message: current.failed, signIn: current.signIn };
        const note = clip(input.note.trim(), MAX_NOTE_CHARS);
        const unchecked = Math.min(10_000, Math.max(0, Math.round(input.unchecked)));
        const outcome = await send(
          current,
          { type: 'mark_best', version: current.version, unchecked_components: unchecked, ...(note ? { note } : {}) },
          at,
          action,
        );
        if (outcome.ok) {
          commit({ ...current, version: outcome.value.version, markedVersion: current.version, markHolds: true });
          return { ok: true, version: current.version };
        }
        if (outcome.status !== 404) return { ok: false, message: outcome.message, signIn: outcome.signIn };
      }
      return { ok: false, message: 'The record of this work was not found. Try again to start a new record of it.' };
    };

    return {
      opened: (turtle: string | null, states: ComponentState[] | null) => {
        const at = now();
        void enqueue(async () => {
          commit(null);
          const accountId = account();
          if (turtle === null || typeof accountId === 'string') return;
          await start(accountId, turtle, states, at, null);
        });
      },
      adopted: (recordId: string | null, turtle: string) => {
        void enqueue(async () => {
          const accountId = account();
          if (!recordId || typeof accountId === 'string' || !turtle.trim()) {
            commit(null);
            return;
          }
          commit({ id: recordId, ownerId: accountId, version: 0, turtle, markedVersion: null, markHolds: false });
        });
      },
      restored: (next: RecordState | null) => {
        void enqueue(async () => commit(next));
      },
      applied: (change: AppliedChange) => {
        const at = now();
        // A metadata step keeps the mark: until it is answered, the Turtle it carries still counts as marked.
        if (change.type === 'metadata') setPendingMetadata(change.afterTurtle);
        return enqueue(async () => {
          try {
            await edit(change, at, null);
          } finally {
            if (change.type === 'metadata') setPendingMetadata((current) => (current === change.afterTurtle ? null : current));
          }
        });
      },
      checked: (item: CheckedComponent) => {
        const at = now();
        void enqueue(() => check(item, at));
      },
      markBest: (input: BestMark) => {
        const at = now();
        return enqueue(() => mark(input, at));
      },
      flush: () => enqueue(async () => recordRef.current),
      refused: (reason: 'outdated' | 'gone') => {
        void enqueue(async () => {
          const current = recordRef.current;
          if (current) commit(reason === 'gone' ? null : { ...current, markHolds: false });
        });
      },
    };
  }, []);

  return { record, pendingMetadata, ...actions };
}
