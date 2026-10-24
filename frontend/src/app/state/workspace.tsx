import * as React from 'react';
import { Parser } from 'n3';
import { toast } from 'sonner';

import extract from '@/lib/extract.js';
import mergeCurrentTurtle from '@/lib/mergeCurrentTurtle.js';
import { applyPreNanopubSettingsToTurtle } from '@/lib/applyPreNanopubSettingsToTurtle.js';
import type { Concept, Variable } from '@/model/models.js';
import { DEFAULT_ORDER, layoutVariable, type Layout } from '@/lib/iadopt/layout';
import { labelOf, listComponents, stableKey, type ComponentInfo } from '@/lib/iadopt/model';
import { readJson, WORKSPACE_DRAFT_KEY, writeJson, writeString } from '@/lib/storage';
import { AuthRequiredError, fetchModelOptions, fetchPreparationOptions, streamDecomposition, type ModelOptions, type PreparationOptions } from '@/shared/api';
import { useAuth } from '@/shared/auth';

/*
 * Everything the workspace shows and edits. The visible Turtle is the source of truth for export
 * and publishing, as before; the diagram model is rebuilt from it whenever the user applies it.
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
  openTurtle: (turtle: string) => Promise<boolean>;
  mutate: (change: () => { syncVariableLabels?: boolean } | void) => Promise<void>;
  applyNanopubMetadata: () => Promise<boolean>;
  replaceTurtleQuietly: (turtle: string) => void;
  reset: () => void;
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

/** Line endings and surrounding whitespace do not count as edits. */
function sameTurtle(a: string, b: string): boolean {
  return a.replace(/\r\n?/g, '\n').trim() === b.replace(/\r\n?/g, '\n').trim();
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
  const [renderedTurtle, setRenderedTurtle] = React.useState('');
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
  }, [logEvent]);

  const clearDiagram = React.useCallback(() => {
    renderSeq.current += 1;
    variableRef.current = null;
    setRenderedTurtle('');
    setLayout(null);
    setRenderError(null);
    setRendering(false);
    setSelected(null);
    setVersion((value) => value + 1);
  }, []);

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
    clearDiagram();
    if (next.turtle?.trim()) void visualize(next.turtle, { log: false });
  }, [clearDiagram, visualize]);

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

  const setOrder = React.useCallback((next: string) => {
    orderRef.current = next;
    setOrderState(next);
    if (variableRef.current) {
      void relayout(variableRef.current, next);
      // The previous page redrew, and logged, the diagram when the order changed.
      logEvent('visualize', { ttl: turtleRef.current });
    }
  }, [logEvent, relayout]);

  /** Open a file, pasted Turtle or the example: like a new document, it replaces the workspace. */
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
    return visualize(text);
  }, [clearDiagram, visualize]);

  /* --------------------------------- edits -------------------------------- */

  const mutate = React.useCallback(async (change: () => { syncVariableLabels?: boolean } | void) => {
    const variable = variableRef.current;
    if (!variable) return;
    const before = turtleRef.current;
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
    await relayout(variable);
  }, [relayout, visualize]);

  const replaceTurtleQuietly = React.useCallback((next: string) => {
    // Used when publishing writes the creator into the Turtle: the diagram does not change.
    setTurtle(next);
    setRenderedTurtle(next);
  }, []);

  const applyNanopubMetadata = React.useCallback(async () => {
    if (!preparationOptions) {
      toast.error(preparationError || 'Nanopublication preparation settings could not be loaded from the backend.');
      return false;
    }
    const input = turtleRef.current;
    const settings = {
      conformsToUri: preparationOptions.conforms_to_uri,
      createdWithLabel: preparationOptions.created_with_label,
    };
    try {
      const output = await applyPreNanopubSettingsToTurtle(input, settings);
      setTurtle(output);
      logEvent('apply_pre_nanopub_settings', { input_turtle: input, output_turtle: output, settings });
      // Redraw for a new IRI; the previous page logged only the apply event here.
      await visualize(output, { log: false });
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      logEvent('apply_pre_nanopub_settings_failed', { input_turtle: input, settings }, { error: message });
      toast.error(message || 'Could not apply pre-nanopublication settings.');
      return false;
    }
  }, [logEvent, preparationError, preparationOptions, visualize]);

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
        // Always without reasoning, the default setup; the page offers no switch for it.
        { definition: text, model_provider: provider, model_name: model, disable_thinking: true },
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
  }, [definition, modelOptions, requireUser, runProtected, runSettings, visualize]);

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
    setChecks((current) => {
      const next = { ...current };
      if (next[key]) delete next[key];
      else next[key] = true;
      return next;
    });
  }, [keyOf]);
  const checkedCount = components.filter((item) => checks[item.key]).length;

  /* --------------------------------- drafts -------------------------------- */

  // A drawn diagram counts too: clearing the editor to paste new text keeps the workspace open.
  const hasContent = Boolean(definition.trim() || turtle.trim() || rawOutput.trim() || layout);

  React.useEffect(() => {
    const save = () => {
      if (!hasContent) {
        writeString(WORKSPACE_DRAFT_KEY, null);
        return;
      }
      const next: Draft = { definition, rawOutput, validationErrors, schemaValid, turtle, checks, run, savedAt: new Date().toISOString() };
      writeJson(WORKSPACE_DRAFT_KEY, next);
    };
    const handle = window.setTimeout(save, 600);
    // Leaving the page within the delay still keeps the latest edit.
    window.addEventListener('pagehide', save);
    return () => {
      window.clearTimeout(handle);
      window.removeEventListener('pagehide', save);
    };
  }, [checks, definition, hasContent, rawOutput, run, schemaValid, turtle, validationErrors]);

  const reset = React.useCallback(() => {
    setDefinition('');
    setRawOutput('');
    setValidationErrors(null);
    setSchemaValid(null);
    setDecomposeError(null);
    setRun(null);
    setTurtle('');
    setChecks({});
    clearDiagram();
    writeString(WORKSPACE_DRAFT_KEY, null);
  }, [clearDiagram]);

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
    openTurtle,
    mutate,
    applyNanopubMetadata,
    replaceTurtleQuietly,
    reset,
  };

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

/** The variable's label for headings, falling back to a neutral name. */
export function variableTitle(variable: Variable | null): string {
  return (variable && labelOf(variable)) || 'Untitled variable';
}
