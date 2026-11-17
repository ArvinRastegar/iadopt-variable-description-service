import * as React from 'react';
import { Parser } from 'n3';
import { toast } from 'sonner';

import extract from '@/lib/extract.js';
import mergeCurrentTurtle from '@/lib/mergeCurrentTurtle.js';
import { applyPreNanopubSettingsToTurtle } from '@/lib/applyPreNanopubSettingsToTurtle.js';
import type { Concept, Variable } from '@/model/models.js';
import { clientInfo } from '@/lib/clientInfo';
import { componentName, recordRole, snapshotComponents, type ComponentState } from '@/lib/iadopt/changes';
import { DEFAULT_ORDER, layoutVariable, type Layout } from '@/lib/iadopt/layout';
import { labelOf, listComponents, stableKey, type ComponentInfo } from '@/lib/iadopt/model';
import { readJson, WORKSPACE_DRAFT_KEY, writeJson, writeString } from '@/lib/storage';
import { AuthRequiredError, fetchModelOptions, fetchPreparationOptions, streamDecomposition, type ModelOptions, type PreparationOptions } from '@/shared/api';
import { useAuth } from '@/shared/auth';
import { markStatusOf, restoredRecord, sameTurtle, useRecorder, type MarkResult, type MarkStatus, type RecordState } from './recording';

/*
 * Everything the workspace shows and edits. The visible Turtle is the source of truth for export
 * and publishing, as before; the diagram model is rebuilt from it whenever the user applies it.
 * For a signed-in account, the work is also recorded step by step (D-037, see ./recording).
 */

export interface RunSettings {
  provider: string;
  model: string;
}

export interface RunInfo extends RunSettings {
  finishedAt: string;
}

export interface RenderError {
  phase: 'Parsing' | 'Rendering';
  message: string;
  /** The Turtle that failed, so the error is only shown next to that text. */
  source: string;
}

interface Draft {
  definition: string;
  rawOutput: string;
  validationErrors: string[] | null;
  schemaValid: boolean | null;
  turtle: string;
  checks: Record<string, true>;
  run: RunInfo | null;
  savedAt: string;
  /** The record of this work, for the account it belongs to (D-037). */
  record?: RecordState | null;
}

interface WorkspaceValue {
  definition: string;
  setDefinition: (value: string) => void;
  rawOutput: string;
  validationErrors: string[] | null;
  schemaValid: boolean | null;
  decomposeError: string | null;
  decomposing: boolean;
  decomposeStartedAt: number | null;
  run: RunInfo | null;
  runSettings: RunSettings;
  setRunSettings: (settings: RunSettings) => void;
  modelOptions: ModelOptions | null;
  preparationOptions: PreparationOptions | null;
  preparationError: string | null;

  turtle: string;
  setTurtle: (value: string) => void;
  renderedTurtle: string;
  turtleDirty: boolean;
  variable: Variable | null;
  layout: Layout | null;
  components: ComponentInfo[];
  renderError: RenderError | null;
  /** The render error of the Turtle that is visible now, if drawing that text failed. */
  turtleError: RenderError | null;
  rendering: boolean;
  order: string;
  setOrder: (order: string) => void;

  selected: Concept | null;
  select: (comp: Concept | null) => void;
  isChecked: (comp: Concept) => boolean;
  toggleChecked: (comp: Concept) => void;
  checkedCount: number;

  hasContent: boolean;
  /** The page was opened with a shared ?ttl= link. */
  openedFromUrl: boolean;
  decompose: () => Promise<void>;
  visualize: (turtle: string, options?: { log?: boolean }) => Promise<boolean>;
  /** "Update diagram": draw the visible Turtle and record it as an applied change. */
  applyTurtle: () => Promise<boolean>;
  openTurtle: (turtle: string) => Promise<boolean>;
  mutate: (change: () => { syncVariableLabels?: boolean } | void) => Promise<void>;
  applyNanopubMetadata: () => Promise<boolean>;
  reset: () => void;

  /** The record of this work, when the signed-in account is recorded (D-037). */
  record: RecordState | null;
  /** Whether the version the person sees is the one marked as best. */
  markStatus: MarkStatus;
  /** Mark the drawn version as best, with an optional note. */
  markBest: (note: string) => Promise<MarkResult>;
  /**
   * Make the visible Turtle the one about to be published (with the creator written in), record
   * that metadata step, and resolve with the record id once every step was sent.
   */
  preparePublication: (turtle: string) => Promise<string | null>;
  /** The backend refused a publication: the best mark does not hold, or the record is gone. */
  publicationRefused: (reason: 'outdated' | 'gone') => void;
}

const WorkspaceContext = React.createContext<WorkspaceValue | null>(null);

export function useWorkspace(): WorkspaceValue {
  const value = React.useContext(WorkspaceContext);
  if (!value) throw new Error('useWorkspace must be used inside WorkspaceProvider');
  return value;
}

function initialTurtleFromUrl(): string | null {
  try {
    const value = new URL(window.location.href).searchParams.get('ttl');
    if (!value?.trim()) return null;
    try {
      return decodeURI(value);
    } catch {
      // A literal % (as in "50% saturation") is not an escape; keep the text as it is.
      return value;
    }
  } catch {
    return null;
  }
}

function parsesAsTurtle(text: string): boolean {
  try {
    new Parser().parse(text);
    return true;
  } catch {
    return false;
  }
}

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const auth = useAuth();
  const { logEvent, requireUser, runProtected } = auth;

  const urlTurtle = React.useMemo(() => initialTurtleFromUrl(), []);
  const savedDraft = React.useMemo(() => readJson<Draft | null>(WORKSPACE_DRAFT_KEY, null), []);
  // A shared ?ttl= link opens its Turtle; the earlier draft can still be restored from a notice.
  const draft = urlTurtle === null ? savedDraft : null;

  const [definition, setDefinition] = React.useState(draft?.definition ?? '');
  const [rawOutput, setRawOutput] = React.useState(draft?.rawOutput ?? '');
  const [validationErrors, setValidationErrors] = React.useState<string[] | null>(draft?.validationErrors ?? null);
  const [schemaValid, setSchemaValid] = React.useState<boolean | null>(draft?.schemaValid ?? null);
  const [decomposeError, setDecomposeError] = React.useState<string | null>(null);
  const [decomposing, setDecomposing] = React.useState(false);
  const [decomposeStartedAt, setDecomposeStartedAt] = React.useState<number | null>(null);
  const [run, setRun] = React.useState<RunInfo | null>(draft?.run ?? null);
  const [modelOptions, setModelOptions] = React.useState<ModelOptions | null>(null);
  const [runSettings, setRunSettings] = React.useState<RunSettings>({ provider: 'psnc', model: '' });
  const [preparationOptions, setPreparationOptions] = React.useState<PreparationOptions | null>(null);
  const [preparationError, setPreparationError] = React.useState<string | null>(null);

  const [turtle, setTurtle] = React.useState(urlTurtle ?? draft?.turtle ?? '');
  const [renderedTurtle, setRenderedTurtleState] = React.useState('');
  // The Turtle the diagram was drawn from, readable at once after it changes.
  const renderedRef = React.useRef('');
  const setRenderedTurtle = React.useCallback((value: string) => {
    renderedRef.current = value;
    setRenderedTurtleState(value);
  }, []);
  const initialRecord = React.useMemo(() => (draft ? restoredRecord(draft.record, draft.turtle ?? '') : null), [draft]);
  const recorder = useRecorder(auth, initialRecord);
  // The recorder's functions never change, so callbacks can depend on them.
  const {
    opened: recordOpened,
    adopted: recordAdopted,
    restored: recordRestored,
    applied: recordApplied,
    checked: recordChecked,
    markBest: recordBest,
    flush: flushRecord,
    refused: recordRefused,
  } = recorder;
  const [layout, setLayout] = React.useState<Layout | null>(null);
  const [renderError, setRenderError] = React.useState<RenderError | null>(null);
  const [rendering, setRendering] = React.useState(false);
  const [order, setOrderState] = React.useState(DEFAULT_ORDER);
  const [version, setVersion] = React.useState(0);
  const [selected, setSelected] = React.useState<Concept | null>(null);
  const [checks, setChecks] = React.useState<Record<string, true>>(draft?.checks ?? {});

  const variableRef = React.useRef<Variable | null>(null);
  const orderRef = React.useRef(order);
  const decomposingRef = React.useRef(false);
  const turtleRef = React.useRef(turtle);
  turtleRef.current = turtle;
  const checksRef = React.useRef(checks);
  checksRef.current = checks;
  /** The components of the drawn variable now, for the record. */
  const currentStates = React.useCallback((): ComponentState[] | null => (variableRef.current ? snapshotComponents(variableRef.current) : null), []);
  // Whether a shared ?ttl= link's record was started or is no longer due (see below).
  const urlRecordHandled = React.useRef(urlTurtle === null);
  // Counts drawings: only the latest one may change what the workspace shows.
  const renderSeq = React.useRef(0);
  // Maps a component of the current model to its review key (set below, once components exist).
  const keyOfRef = React.useRef<(comp: Concept) => string>(stableKey);

  /* ----------------------------- configuration ---------------------------- */

  React.useEffect(() => {
    void fetchModelOptions().then((options) => {
      setModelOptions(options);
      const provider = options.providers[options.defaultProvider];
      setRunSettings((current) => ({
        ...current,
        provider: options.defaultProvider,
        model: provider?.default_model_name ?? current.model,
      }));
    });
    fetchPreparationOptions()
      .then((options) => setPreparationOptions(options))
      .catch((error: Error) => {
        console.error(error);
        setPreparationError(error.message || 'Could not load nanopublication preparation settings.');
      });
  }, []);

  /* -------------------------------- rendering ------------------------------ */

  const relayout = React.useCallback(async (variable: Variable, nextOrder = orderRef.current) => {
    const seq = renderSeq.current;
    const next = await layoutVariable(variable, nextOrder);
    // A newer drawing started meanwhile; it lays out with the current order itself.
    if (seq !== renderSeq.current) return;
    setLayout(next);
    setVersion((value) => value + 1);
  }, []);

  const visualize = React.useCallback(async (source: string, { log = true }: { log?: boolean } = {}) => {
    const seq = ++renderSeq.current;
    const latest = () => seq === renderSeq.current;
    setRendering(true);
    try {
      let variables: Variable[];
      try {
        variables = await extract(source);
      } catch (error) {
        console.error(error);
        if (latest()) setRenderError({ phase: 'Parsing', message: error instanceof Error ? error.message : String(error), source });
        return false;
      }
      if (!latest()) return false;
      const variable = variables[0];
      if (!variable) {
        setRenderError({ phase: 'Parsing', message: 'The Turtle contains no iop:Variable.', source });
        return false;
      }
      try {
        const next = await layoutVariable(variable, orderRef.current);
        if (!latest()) return false;
        variableRef.current = variable;
        setLayout(next);
        setRenderedTurtle(source);
        setRenderError(null);
        setSelected((current) => {
          if (!current) return null;
          const key = keyOfRef.current(current);
          return listComponents(variable).find((item) => item.key === key)?.comp ?? null;
        });
        setVersion((value) => value + 1);
        if (log) logEvent('visualize', { ttl: source });
        return true;
      } catch (error) {
        console.error(error);
        if (!latest()) return false;
        const message = error instanceof Error ? error.message : String(error);
        logEvent('visualize_failed', { ttl: source }, { error: message });
        setRenderError({ phase: 'Rendering', message, source });
        return false;
      }
    } finally {
      if (latest()) setRendering(false);
    }
  }, [logEvent, setRenderedTurtle]);

  const clearDiagram = React.useCallback(() => {
    renderSeq.current += 1;
    variableRef.current = null;
    setRenderedTurtle('');
    setLayout(null);
    setRenderError(null);
    setRendering(false);
    setSelected(null);
    setVersion((value) => value + 1);
  }, [setRenderedTurtle]);

  // Show the Turtle that came from the URL or the restored draft.
  const initialTurtle = React.useRef(turtle);
  React.useEffect(() => {
    if (initialTurtle.current.trim()) void visualize(initialTurtle.current, { log: false });
  }, [visualize]);

  const restoreDraft = React.useCallback((next: Draft) => {
    setDefinition(next.definition ?? '');
    setRawOutput(next.rawOutput ?? '');
    setValidationErrors(next.validationErrors ?? null);
    setSchemaValid(next.schemaValid ?? null);
    setRun(next.run ?? null);
    setChecks(next.checks ?? {});
    setDecomposeError(null);
    setTurtle(next.turtle ?? '');
    urlRecordHandled.current = true;
    recordRestored(restoredRecord(next.record, next.turtle ?? ''));
    clearDiagram();
    if (next.turtle?.trim()) void visualize(next.turtle, { log: false });
  }, [clearDiagram, recordRestored, visualize]);

  // A shared link: drop ?ttl= from the address, so a reload keeps later edits (they are in the
  // draft), and offer the draft it replaced.
  const urlHandled = React.useRef(false);
  React.useEffect(() => {
    if (urlTurtle === null || urlHandled.current) return;
    urlHandled.current = true;
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete('ttl');
      window.history.replaceState(window.history.state, '', url.href);
    } catch {
      // Keeping the parameter only means that a reload opens the link again.
    }
    const earlier = savedDraft;
    const replaced = earlier && (
      earlier.definition?.trim() || earlier.rawOutput?.trim() || (earlier.turtle?.trim() && !sameTurtle(earlier.turtle, urlTurtle))
    );
    if (earlier && replaced) {
      toast('The shared Turtle replaced the draft you had in this browser.', {
        duration: 20_000,
        action: { label: 'Restore draft', onClick: () => restoreDraft(earlier) },
      });
    }
  }, [restoreDraft, savedDraft, urlTurtle]);

  // A shared ?ttl= link opens Turtle. For an account signed in when the page loads, its record
  // starts as soon as both the drawing and the sign-in check are done, if nothing changed meanwhile.
  React.useEffect(() => {
    if (urlRecordHandled.current || urlTurtle === null || !auth.ready || !renderedTurtle) return;
    urlRecordHandled.current = true;
    if (auth.recorded && sameTurtle(renderedTurtle, urlTurtle)) recordOpened(urlTurtle, currentStates());
  }, [auth.ready, auth.recorded, currentStates, recordOpened, renderedTurtle, urlTurtle]);

  const setOrder = React.useCallback((next: string) => {
    orderRef.current = next;
    setOrderState(next);
    if (variableRef.current) {
      void relayout(variableRef.current, next);
      // The previous page redrew, and logged, the diagram when the order changed.
      logEvent('visualize', { ttl: turtleRef.current });
    }
  }, [logEvent, relayout]);

  /**
   * Open a file, pasted Turtle or the example: like a new document, it replaces the workspace.
   * For a signed-in account it starts a record, with the opened Turtle as version 0.
   */
  const openTurtle = React.useCallback(async (text: string) => {
    setDefinition('');
    setRawOutput('');
    setValidationErrors(null);
    setSchemaValid(null);
    setRun(null);
    setChecks({});
    setDecomposeError(null);
    setTurtle(text);
    clearDiagram();
    const drawn = await visualize(text);
    // Turtle that cannot be drawn is no variable description yet; its record starts once it is.
    recordOpened(drawn ? text : null, drawn ? currentStates() : null);
    return drawn;
  }, [clearDiagram, currentStates, recordOpened, visualize]);

  /* --------------------------------- edits -------------------------------- */

  const mutate = React.useCallback(async (change: () => { syncVariableLabels?: boolean } | void) => {
    const variable = variableRef.current;
    if (!variable) return;
    const before = turtleRef.current;
    const beforeTurtle = renderedRef.current;
    const beforeStates = snapshotComponents(variable);
    let result: { syncVariableLabels?: boolean } | void;
    try {
      result = change();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
      // The change may have stopped halfway: rebuild the model from the Turtle, the source of truth.
      await visualize(before, { log: false });
      return;
    }
    const merged = await mergeCurrentTurtle(before, variable, {
      syncVariableLabels: Boolean(result && result.syncVariableLabels),
    });
    if (!parsesAsTurtle(merged)) {
      toast.error('The change could not be written as valid Turtle, so it was not saved.');
      await visualize(before, { log: false });
      return;
    }
    setTurtle(merged);
    setRenderedTurtle(merged);
    // One applied change, one recorded step with its component changes.
    void recordApplied({ type: 'edit', beforeTurtle, before: beforeStates, afterTurtle: merged, after: snapshotComponents(variable) });
    await relayout(variable);
  }, [recordApplied, relayout, setRenderedTurtle, visualize]);

  const applyTurtle = React.useCallback(async () => {
    const source = turtleRef.current;
    const beforeTurtle = renderedRef.current;
    const beforeStates = currentStates();
    const drawn = await visualize(source);
    if (drawn && !sameTurtle(beforeTurtle, source)) {
      void recordApplied({ type: 'edit', beforeTurtle, before: beforeStates, afterTurtle: source, after: currentStates() });
    }
    return drawn;
  }, [currentStates, recordApplied, visualize]);

  const applyNanopubMetadata = React.useCallback(async () => {
    if (!preparationOptions) {
      toast.error(preparationError || 'Nanopublication preparation settings could not be loaded from the backend.');
      return false;
    }
    const input = turtleRef.current;
    const beforeTurtle = renderedRef.current;
    const beforeStates = currentStates();
    const settings = {
      conformsToUri: preparationOptions.conforms_to_uri,
      createdWithLabel: preparationOptions.created_with_label,
    };
    try {
      const output = await applyPreNanopubSettingsToTurtle(input, settings);
      setTurtle(output);
      logEvent('apply_pre_nanopub_settings', { input_turtle: input, output_turtle: output, settings });
      // Redraw for a new IRI; the previous page logged only the apply event here.
      const drawn = await visualize(output, { log: false });
      if (drawn && !sameTurtle(beforeTurtle, output)) {
        // Only metadata, unless it was added to Turtle edits that had not been drawn yet.
        void recordApplied({
          type: sameTurtle(input, beforeTurtle) ? 'metadata' : 'edit',
          beforeTurtle,
          before: beforeStates,
          afterTurtle: output,
          after: currentStates(),
        });
      }
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      logEvent('apply_pre_nanopub_settings_failed', { input_turtle: input, settings }, { error: message });
      toast.error(message || 'Could not apply pre-nanopublication settings.');
      return false;
    }
  }, [currentStates, logEvent, preparationError, preparationOptions, recordApplied, visualize]);

  const preparePublication = React.useCallback(async (next: string) => {
    const beforeTurtle = renderedRef.current;
    if (!sameTurtle(beforeTurtle, next)) {
      // Publishing writes the creator into the Turtle: the diagram does not change, the record gets a metadata step.
      setTurtle(next);
      setRenderedTurtle(next);
      const states = currentStates();
      void recordApplied({ type: 'metadata', beforeTurtle, before: states, afterTurtle: next, after: states });
    }
    const current = await flushRecord();
    return current?.id ?? null;
  }, [currentStates, recordApplied, flushRecord, setRenderedTurtle]);

  /* ------------------------------ decomposition ---------------------------- */

  const decompose = React.useCallback(async () => {
    if (decomposingRef.current) return;
    if (!(await requireUser('decompose a variable'))) return;
    if (decomposingRef.current) return;
    const text = definition.trim();
    if (!text) {
      setDecomposeError('Please enter a variable definition first.');
      return;
    }

    const provider = modelOptions?.providers[runSettings.provider] ? runSettings.provider : modelOptions?.defaultProvider ?? runSettings.provider;
    const model = runSettings.model || modelOptions?.providers[provider]?.default_model_name || '';
    const settings: RunSettings = { provider, model };

    decomposingRef.current = true;
    setDecomposing(true);
    setDecomposeError(null);
    setDecomposeStartedAt(Date.now());
    try {
      const result = await runProtected(() => streamDecomposition(
        // Always without reasoning, the default setup; the page offers no switch for it. The
        // device and screen go with the run's record (D-037).
        { definition: text, model_provider: provider, model_name: model, disable_thinking: true, client: clientInfo() },
        {
          onStart: () => {
            setRawOutput('');
            setValidationErrors(null);
            setSchemaValid(null);
          },
          onDelta: (delta) => {
            if (delta) setRawOutput((current) => current + delta);
          },
        },
      ), 'decompose a variable');

      setRawOutput((current) => result.raw_llm_output || current);
      setValidationErrors(result.validation_errors ?? []);
      setSchemaValid(Boolean(result.schema_valid));
      setRun({ ...settings, finishedAt: new Date().toISOString() });
      setChecks({});
      setSelected(null);
      const ttl = result.ttl || '';
      setTurtle(ttl);
      // The backend started the run's record, with this draft as version 0. The previous diagram
      // goes, so that nothing of the earlier work is shown, checked or recorded under this record.
      recordAdopted(result.record_id ?? null, ttl);
      clearDiagram();
      if (ttl.trim()) await visualize(ttl);
    } catch (error) {
      console.error(error);
      if (!(error instanceof AuthRequiredError)) {
        const message = error instanceof Error ? error.message : 'Unknown backend error.';
        setDecomposeError(message || 'Unknown backend error.');
      }
    } finally {
      decomposingRef.current = false;
      setDecomposing(false);
      setDecomposeStartedAt(null);
    }
  }, [clearDiagram, definition, modelOptions, recordAdopted, requireUser, runProtected, runSettings, visualize]);

  /* --------------------------------- review -------------------------------- */

  // `version` changes whenever the model or its layout changes, so it is the dependency here.
  const components = React.useMemo(
    () => (version >= 0 && variableRef.current ? listComponents(variableRef.current) : []),
    [version],
  );
  const keyByComp = React.useMemo(() => new Map(components.map((item) => [item.comp, item.key])), [components]);
  const keyOf = React.useCallback((comp: Concept) => keyByComp.get(comp) ?? stableKey(comp), [keyByComp]);
  keyOfRef.current = keyOf;

  const isChecked = React.useCallback((comp: Concept) => Boolean(checks[keyOf(comp)]), [checks, keyOf]);
  const toggleChecked = React.useCallback((comp: Concept) => {
    const key = keyOf(comp);
    const checked = !checksRef.current[key];
    setChecks((current) => {
      const next = { ...current };
      if (next[key]) delete next[key];
      else next[key] = true;
      return next;
    });
    // "Looks right" and its Undo are steps of the record, on the version the diagram shows.
    recordChecked({
      checked,
      role: recordRole(comp),
      component: componentName({
        iri: comp.isBlank() ? null : comp.getIri() ?? null,
        label: comp.getLabel() ?? '',
        group: components.find((item) => item.comp === comp)?.relation ?? comp.getRole(),
      }),
      turtle: renderedRef.current,
      states: currentStates(),
    });
  }, [components, currentStates, keyOf, recordChecked]);
  const checkedCount = components.filter((item) => checks[item.key]).length;

  const markBest = React.useCallback((note: string) => recordBest({
    note,
    unchecked: components.length - components.filter((item) => checksRef.current[item.key]).length,
    turtle: renderedRef.current,
    states: currentStates(),
  }), [components, currentStates, recordBest]);

  const publicationRefused = React.useCallback((reason: 'outdated' | 'gone') => recordRefused(reason), [recordRefused]);

  /* --------------------------------- drafts -------------------------------- */

  // A drawn diagram counts too: clearing the editor to paste new text keeps the workspace open.
  const hasContent = Boolean(definition.trim() || turtle.trim() || rawOutput.trim() || layout);

  const record = recorder.record;
  const ownRecord = record && auth.user?.id === record.ownerId ? record : null;
  React.useEffect(() => {
    const save = () => {
      if (!hasContent) {
        writeString(WORKSPACE_DRAFT_KEY, null);
        return;
      }
      const next: Draft = { definition, rawOutput, validationErrors, schemaValid, turtle, checks, run, savedAt: new Date().toISOString(), record };
      writeJson(WORKSPACE_DRAFT_KEY, next);
    };
    const handle = window.setTimeout(save, 600);
    // Leaving the page within the delay still keeps the latest edit.
    window.addEventListener('pagehide', save);
    return () => {
      window.clearTimeout(handle);
      window.removeEventListener('pagehide', save);
    };
  }, [checks, definition, hasContent, rawOutput, record, run, schemaValid, turtle, validationErrors]);

  const reset = React.useCallback(() => {
    setDefinition('');
    setRawOutput('');
    setValidationErrors(null);
    setSchemaValid(null);
    setDecomposeError(null);
    setRun(null);
    setTurtle('');
    setChecks({});
    recordOpened(null, null);
    clearDiagram();
    writeString(WORKSPACE_DRAFT_KEY, null);
  }, [clearDiagram, recordOpened]);

  const value: WorkspaceValue = {
    definition,
    setDefinition,
    rawOutput,
    validationErrors,
    schemaValid,
    decomposeError,
    decomposing,
    decomposeStartedAt,
    run,
    runSettings,
    setRunSettings,
    modelOptions,
    preparationOptions,
    preparationError,
    turtle,
    setTurtle,
    renderedTurtle,
    turtleDirty: !sameTurtle(turtle, renderedTurtle),
    variable: variableRef.current,
    layout,
    components,
    renderError,
    turtleError: renderError && sameTurtle(renderError.source, turtle) ? renderError : null,
    rendering,
    order,
    setOrder,
    selected,
    select: setSelected,
    isChecked,
    toggleChecked,
    checkedCount,
    hasContent,
    openedFromUrl: urlTurtle !== null,
    decompose,
    visualize,
    applyTurtle,
    openTurtle,
    mutate,
    applyNanopubMetadata,
    reset,
    // Another account's record (a draft left in this browser) is not this person's.
    record: ownRecord,
    markStatus: markStatusOf(ownRecord, renderedTurtle, recorder.pendingMetadata),
    markBest,
    preparePublication,
    publicationRefused,
  };

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

/** The variable's label for headings, falling back to a neutral name. */
export function variableTitle(variable: Variable | null): string {
  return (variable && labelOf(variable)) || 'Untitled variable';
}
