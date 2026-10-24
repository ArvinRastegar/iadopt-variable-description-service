import * as React from 'react';
import { Annotation, EditorState } from '@codemirror/state';
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { HighlightStyle, StreamLanguage, syntaxHighlighting, bracketMatching } from '@codemirror/language';
import { turtle as turtleMode } from '@codemirror/legacy-modes/mode/turtle';
import { tags } from '@lezer/highlight';

/*
 * Turtle editor. Colours come from the component tokens, so it follows the page theme:
 * keywords in the property colour, IRIs in the entity colour, literals in the variable colour.
 */

const turtleLanguage = StreamLanguage.define(turtleMode);

// Marks changes that come from outside the editor, so they are not reported back as edits.
const external = Annotation.define<boolean>();

const highlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.meta, tags.operatorKeyword], color: 'var(--prop-fg)' },
  { tag: [tags.atom, tags.url, tags.link], color: 'var(--ooi-fg)' },
  { tag: [tags.string, tags.special(tags.string)], color: 'var(--var-fg)' },
  { tag: [tags.variableName, tags.typeName, tags.propertyName], color: 'var(--foreground)' },
  { tag: [tags.comment], color: 'var(--muted-foreground)', fontStyle: 'italic' },
  { tag: [tags.number, tags.bool], color: 'var(--con-fg)' },
  { tag: [tags.punctuation, tags.bracket, tags.operator], color: 'var(--muted-foreground)' },
]);

const theme = EditorView.theme({
  '&': {
    fontSize: '12.5px',
    backgroundColor: 'var(--surface)',
    color: 'var(--foreground)',
    height: '100%',
  },
  '.cm-scroller': {
    fontFamily: 'var(--font-mono)',
    lineHeight: '20px',
  },
  '.cm-content': { padding: '12px 0', caretColor: 'var(--foreground)' },
  '.cm-gutters': {
    backgroundColor: 'var(--surface-2)',
    color: 'var(--muted-foreground)',
    borderRight: '1px solid var(--border)',
  },
  '.cm-activeLine': { backgroundColor: 'color-mix(in oklab, var(--surface-2) 70%, transparent)' },
  '.cm-activeLineGutter': { backgroundColor: 'var(--surface-3)', color: 'var(--foreground)' },
  '&.cm-focused': { outline: 'none' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': {
    backgroundColor: 'color-mix(in oklab, var(--primary) 22%, transparent) !important',
  },
  '.cm-cursor': { borderLeftColor: 'var(--foreground)' },
});

export function TurtleEditor({
  value,
  onChange,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
}) {
  const host = React.useRef<HTMLDivElement>(null);
  const view = React.useRef<EditorView | null>(null);
  const onChangeRef = React.useRef(onChange);
  onChangeRef.current = onChange;
  // Texts the editor reported recently. A `value` among them is its own edit coming back, possibly
  // behind newer typing, and must not be written over the document.
  const ownTexts = React.useRef<string[]>([value]);

  React.useEffect(() => {
    if (!host.current) return;
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          highlightActiveLineGutter(),
          highlightActiveLine(),
          drawSelection(),
          history(),
          bracketMatching(),
          turtleLanguage,
          syntaxHighlighting(highlight),
          keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
          theme,
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({ 'aria-label': label, spellcheck: 'false' }),
          EditorView.updateListener.of((update) => {
            if (!update.docChanged || update.transactions.some((tr) => tr.annotation(external))) return;
            const text = update.state.doc.toString();
            ownTexts.current = [...ownTexts.current.slice(-19), text];
            onChangeRef.current(text);
          }),
        ],
      }),
    });
    view.current = editor;
    return () => {
      editor.destroy();
      view.current = null;
    };
    // The editor is created once; later value changes are applied below.
  }, [label]);

  // Apply outside changes (edits from the inspector, metadata, a new decomposition).
  React.useEffect(() => {
    const editor = view.current;
    if (!editor || ownTexts.current.includes(value)) return;
    ownTexts.current = [value];
    const current = editor.state.doc.toString();
    // The editor keeps \n line breaks, so CRLF text with the same lines is the same document.
    if (current !== value.replace(/\r\n?/g, '\n')) {
      editor.dispatch({ changes: { from: 0, to: current.length, insert: value }, annotations: external.of(true) });
    }
  }, [value]);

  return <div ref={host} className="h-full min-h-[360px] overflow-hidden" />;
}
