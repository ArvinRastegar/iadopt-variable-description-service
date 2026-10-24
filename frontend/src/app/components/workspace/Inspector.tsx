import * as React from 'react';
import { AlertTriangleIcon, CheckIcon, ExternalLinkIcon, InfoIcon, MousePointerClickIcon, PlusIcon, RefreshCwIcon, Trash2Icon, Undo2Icon } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/feedback';
import { Input, Label, Textarea } from '@/components/ui/fields';
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Segmented, SegmentedItem, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/controls';
import {
  addableToVariable,
  addConstraint,
  addToVariable,
  applyEdit,
  ASYMMETRIC_PAIRS,
  canAddConstraint,
  canChangeShape,
  canDelete,
  deleteComponent,
  fieldsOf,
  shapeOf,
  type AddableToVariable,
  type Fields,
  type Shape,
  type ShapeChoice,
} from '@/lib/iadopt/editing';
import { KIND_CHIP, KIND_DOT, KIND_NAMES } from '@/lib/iadopt/kinds';
import { describeLink, isLinkableRole, isVocabularyLink, labelOf, type ComponentInfo } from '@/lib/iadopt/model';
import { cn } from '@/lib/utils';
import { Entity, Variable } from '@/model/models.js';
import { useWorkspace } from '../../state/workspace';

const ADD_LABELS: Record<AddableToVariable | 'Constraint', string> = {
  ContextObject: 'Context object',
  Matrix: 'Matrix',
  StatisticalModifier: 'Statistical modifier',
  Constraint: 'Constraint',
};

export function Inspector() {
  const workspace = useWorkspace();
  const item = workspace.components.find((entry) => entry.comp === workspace.selected) ?? null;

  if (!item) {
    return (
      <div className="flex flex-1 flex-col">
        <TurtleChanges className="mx-[18px] mt-[18px]" />
        <div className="flex flex-1 flex-col items-center justify-center gap-2.5 p-8 text-center">
          <span aria-hidden="true" className="flex size-11 items-center justify-center rounded-xl bg-surface-2 text-muted-foreground">
            <MousePointerClickIcon className="size-5" />
          </span>
          <div className="font-semibold">Select a component</div>
          <p className="m-0 max-w-60 text-[13px] text-muted-foreground">Click a box in the diagram, or a row in the list, to check its label and vocabulary link.</p>
        </div>
      </div>
    );
  }
  return <InspectorFor key={componentKeyOf(item)} item={item} />;
}

/** Edits made in the Turtle tab are saved here too, where the diagram's own edits are saved. */
function TurtleChanges({ className }: { className?: string }) {
  const workspace = useWorkspace();
  if (!workspace.turtleDirty || workspace.decomposing) return null;
  const error = workspace.turtleError;
  return (
    <Callout
      tone={error ? 'danger' : 'warning'}
      title={error ? `Error during ${error.phase.toLowerCase()}` : 'The Turtle has unsaved edits'}
      className={className}
    >
      <div className="flex flex-col gap-2.5">
        <span className={cn(error && 'font-mono text-xs break-words')}>
          {error ? error.message : 'Update the diagram to save them. Editing here waits until then.'}
        </span>
        <Button size="sm" className="self-start" disabled={workspace.rendering} onClick={() => void workspace.visualize(workspace.turtle)}>
          <RefreshCwIcon aria-hidden="true" /> Update diagram
        </Button>
      </div>
    </Callout>
  );
}

function constraintsOf(comp: ComponentInfo['comp']): ComponentInfo['comp'][] {
  return comp instanceof Entity ? comp.getConstraints() : [];
}

let nextKey = 0;
const keys = new WeakMap<object, number>();
function componentKeyOf(item: ComponentInfo): number {
  if (!keys.has(item.comp)) keys.set(item.comp, ++nextKey);
  return keys.get(item.comp) as number;
}

function InspectorFor({ item }: { item: ComponentInfo }) {
  const workspace = useWorkspace();
  const comp = item.comp;
  const initialFields = React.useMemo(() => fieldsOf(comp), [comp, workspace.renderedTurtle]); // refresh after saves
  const initialShape = React.useMemo(() => shapeOf(comp), [comp, workspace.renderedTurtle]);
  const [fields, setFields] = React.useState<Fields>(initialFields);
  const [shape, setShape] = React.useState<ShapeChoice>(initialShape);
  const [adding, setAdding] = React.useState<AddableToVariable | 'Constraint' | null>(null);
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const checkButton = React.useRef<HTMLButtonElement>(null);

  React.useEffect(() => setFields(initialFields), [initialFields]);
  React.useEffect(() => setShape(initialShape), [initialShape]);

  const dirty =
    fields.label !== initialFields.label ||
    fields.iri !== initialFields.iri ||
    fields.comment !== initialFields.comment ||
    shape.shape !== initialShape.shape ||
    (shape.shape === 'asym' && shape.pairIndex !== initialShape.pairIndex);
  const locked = workspace.turtleDirty || workspace.decomposing;
  const checked = workspace.isChecked(comp);
  const link = describeLink(comp);
  const linkable = isLinkableRole(item.role);
  const isVariable = comp instanceof Variable;

  async function save() {
    setSaving(true);
    try {
      await workspace.mutate(() => applyEdit(comp, fields, canChangeShape(comp) ? shape : undefined));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-5 px-[18px] pt-[18px] pb-7">
      <div className="flex flex-col gap-2.5">
        <div className="flex items-center justify-between gap-2">
          <span className={cn('inline-flex h-6 items-center gap-1.5 rounded-full border px-2.5 text-xs font-semibold', KIND_CHIP[item.kind])}>
            <span aria-hidden="true" className={cn('size-[7px] rounded-[2px]', KIND_DOT[item.kind])} />
            {item.typeName}
          </span>
          <span className="text-xs text-muted-foreground">Inspector</span>
        </div>
        <h2 className="m-0 text-xl leading-[26px] font-semibold tracking-[-0.015em] text-balance break-words">{item.label || 'Unnamed'}</h2>
        {item.role === 'Constraint' && (
          <p className="m-0 text-[13px] text-text-2">
            Narrows{' '}
            {item.constrains.map((target, index) => (
              <React.Fragment key={index}>
                {index > 0 && ', '}
                <button type="button" className="font-semibold text-foreground underline-offset-4 hover:underline" onClick={() => workspace.select(target)}>
                  {labelOf(target) || 'unnamed'}
                </button>
              </React.Fragment>
            ))}
            .
          </p>
        )}
        {item.relation && <p className="m-0 text-[13px] text-text-2">Part of a system, linked with <code className="font-mono text-xs">{item.relation}</code>.</p>}
      </div>

      <TurtleChanges />

      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (dirty && !locked) void save();
        }}
      >
        <fieldset disabled={locked} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="inspector-label">Label</Label>
            <Input id="inspector-label" value={fields.label} onChange={(event) => setFields({ ...fields, label: event.target.value })} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="inspector-iri">IRI</Label>
            <Input
              id="inspector-iri"
              value={fields.iri}
              placeholder="Blank node"
              spellCheck={false}
              className="font-mono text-[12.5px]"
              onChange={(event) => setFields({ ...fields, iri: event.target.value })}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="inspector-description">{isVariable ? 'Definition or comment' : 'Description'}</Label>
            <Textarea
              id="inspector-description"
              rows={3}
              value={fields.comment}
              placeholder="Optional"
              onChange={(event) => setFields({ ...fields, comment: event.target.value })}
            />
          </div>
          {canChangeShape(comp) && <ShapePicker value={shape} onChange={setShape} />}
          {dirty && (
            <div className="flex gap-2">
              <Button type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save changes'}</Button>
              <Button type="button" variant="ghost" onClick={() => { setFields(initialFields); setShape(initialShape); }}>Discard</Button>
            </div>
          )}
        </fieldset>
      </form>

      {linkable && <VocabularyLink link={link} />}
      {!linkable && !isVariable && item.role !== 'Constraint' && link.iri && <IriCard iri={link.iri} />}

      {!isVariable && item.role !== 'Constraint' && (
        <div className="flex flex-col gap-2">
          <span className="text-[13px] font-medium text-text-2">Constraints</span>
          {constraintsOf(comp).length ? (
            <div className="flex flex-col gap-1.5">
              {constraintsOf(comp).map((constraint, index) => (
                <button
                  key={index}
                  type="button"
                  onClick={() => workspace.select(constraint)}
                  className="flex min-h-9 items-center gap-2 rounded-lg border border-dashed border-con-line bg-con-bg px-2.5 text-left text-[13px] font-medium text-con-fg outline-none hover:brightness-[0.98] focus-visible:ring-[3px] focus-visible:ring-ring/40"
                >
                  <span aria-hidden="true" className="size-[7px] rounded-[2px] bg-con" />
                  {labelOf(constraint) || 'Unnamed constraint'}
                </button>
              ))}
            </div>
          ) : (
            <p className="m-0 text-[13px] text-muted-foreground">No constraints narrow this component.</p>
          )}
          {canAddConstraint(comp) && (
            <Button variant="ghost" size="sm" className="self-start text-link" disabled={locked} onClick={() => setAdding('Constraint')}>
              <PlusIcon aria-hidden="true" /> Add constraint
            </Button>
          )}
        </div>
      )}

      {isVariable && workspace.variable && (
        <div className="flex flex-col gap-2">
          <span className="text-[13px] font-medium text-text-2">Add to the variable</span>
          <div className="flex flex-wrap gap-2">
            {addableToVariable(workspace.variable).map((type) => (
              <Button key={type} variant="outline" size="sm" disabled={locked} onClick={() => setAdding(type)}>
                <PlusIcon aria-hidden="true" /> {ADD_LABELS[type]}
              </Button>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-col gap-2.5 border-t border-border pt-4">
        <div className="flex gap-2">
          <Button
            ref={checkButton}
            variant={checked ? 'secondary' : 'outline'}
            size="lg"
            aria-disabled={checked || undefined}
            className={cn('flex-1', checked && 'cursor-default bg-primary-soft text-primary-soft-foreground hover:bg-primary-soft')}
            onClick={() => {
              // Only "Undo" takes a check back; clicking "Checked" keeps it.
              if (!checked) workspace.toggleChecked(comp);
            }}
          >
            <CheckIcon strokeWidth={2.6} aria-hidden="true" />
            {checked ? 'Checked' : 'Looks right'}
          </Button>
          {checked && (
            <Button
              variant="outline"
              size="lg"
              aria-label="Undo check"
              onClick={() => {
                workspace.toggleChecked(comp);
                checkButton.current?.focus();
              }}
            >
              <Undo2Icon aria-hidden="true" /> Undo
            </Button>
          )}
        </div>
        {canDelete(comp) && (
          <Button variant="ghost" className="text-destructive-soft-foreground" disabled={locked} onClick={() => setConfirmDelete(true)}>
            <Trash2Icon aria-hidden="true" /> Delete {KIND_NAMES[item.kind].toLowerCase()}
          </Button>
        )}
      </div>

      <AddComponentDialog
        type={adding}
        onClose={() => setAdding(null)}
        onAdd={async (type, values, choice) => {
          setAdding(null);
          await workspace.mutate(() => {
            if (type === 'Constraint') addConstraint(comp, values);
            else if (workspace.variable) addToVariable(workspace.variable, type, values, choice);
          });
          toast(`${ADD_LABELS[type]} added`);
        }}
      />

      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Delete this {KIND_NAMES[item.kind].toLowerCase()}?</DialogTitle>
            <DialogDescription>“{item.label || 'Unnamed'}” and the constraints on it are removed from the diagram and the Turtle.</DialogDescription>
          </DialogHeader>
          <DialogFooter className="mt-5">
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>Keep it</Button>
            <Button
              variant="destructive"
              onClick={async () => {
                setConfirmDelete(false);
                workspace.select(null);
                await workspace.mutate(() => deleteComponent(comp));
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ShapePicker({ value, onChange }: { value: ShapeChoice; onChange: (value: ShapeChoice) => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span id="shape-label" className="text-[13px] font-medium text-text-2">Shape</span>
      <Segmented
        type="single"
        aria-labelledby="shape-label"
        value={value.shape}
        onValueChange={(shape) => shape && onChange({ ...value, shape: shape as Shape })}
        className="grid grid-cols-3"
      >
        <SegmentedItem value="simple">Single</SegmentedItem>
        <SegmentedItem value="asym">Two sides</SegmentedItem>
        <SegmentedItem value="sym">Parts</SegmentedItem>
      </Segmented>
      {value.shape === 'asym' && (
        <Select value={String(value.pairIndex)} onValueChange={(index) => onChange({ ...value, pairIndex: Number(index) })}>
          <SelectTrigger aria-label="Properties of the asymmetric system"><SelectValue /></SelectTrigger>
          <SelectContent>
            {ASYMMETRIC_PAIRS.map((pair, index) => <SelectItem key={pair.join()} value={String(index)}>{pair.join(' / ')}</SelectItem>)}
          </SelectContent>
        </Select>
      )}
      <p className="m-0 text-xs text-muted-foreground">
        {value.shape === 'simple' && 'A single entity.'}
        {value.shape === 'asym' && 'An asymmetric system with two parts, such as a source and a target.'}
        {value.shape === 'sym' && 'A symmetric system made of parts.'}
      </p>
    </div>
  );
}

function VocabularyLink({ link }: { link: ReturnType<typeof describeLink> }) {
  if (isVocabularyLink(link) && link.iri) {
    return (
      <div className="flex flex-col gap-2">
        <span className="text-[13px] font-medium text-text-2">Vocabulary link</span>
        <div className="flex flex-col gap-2.5 rounded-xl border border-border bg-surface p-3">
          <div className="flex items-center gap-2.5">
            <span className="inline-flex h-[22px] shrink-0 items-center rounded-md bg-surface-3 px-2 font-mono text-[11px] font-semibold text-text-2">{link.source}</span>
            <span className="min-w-0 flex-1 truncate font-semibold">{link.name}</span>
            <a href={link.iri} target="_blank" rel="noopener noreferrer" aria-label={`Open ${link.source} entry in a new tab`} className="flex size-7 shrink-0 items-center justify-center rounded-md text-text-2 hover:bg-surface-2">
              <ExternalLinkIcon className="size-[15px]" aria-hidden="true" />
            </a>
          </div>
          <div className="font-mono text-xs leading-[17px] break-all text-muted-foreground">{link.iri}</div>
          <div className="flex gap-2 border-t border-border pt-2.5 text-xs leading-[17px] text-text-2">
            <InfoIcon className="mt-px size-3.5 shrink-0" aria-hidden="true" />
            <span>Links are drafts too. Open the entry and make sure it means the same thing as the label; change the IRI above if it does not.</span>
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[13px] font-medium text-text-2">Vocabulary link</span>
      <div className="flex gap-2.5 rounded-xl border border-warning-line bg-warning-soft p-3 text-[13px] leading-[18px] text-warning-foreground">
        <AlertTriangleIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <div>
          <strong className="font-semibold">No Wikidata, QUDT or OM link.</strong>{' '}
          {link.iri ? 'It keeps a local IRI.' : 'It is a blank node.'} To link it, paste the IRI of a matching entry into the IRI field above. A component without a link can still be published.
        </div>
      </div>
    </div>
  );
}

function IriCard({ iri }: { iri: string }) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[13px] font-medium text-text-2">IRI</span>
      <div className="flex items-center gap-2 rounded-xl border border-border bg-surface p-3">
        <span className="min-w-0 flex-1 font-mono text-xs leading-[17px] break-all text-muted-foreground">{iri}</span>
        {/^https?:\/\//.test(iri) && (
          <a href={iri} target="_blank" rel="noopener noreferrer" aria-label="Open the IRI in a new tab" className="flex size-7 shrink-0 items-center justify-center rounded-md text-text-2 hover:bg-surface-2">
            <ExternalLinkIcon className="size-[15px]" aria-hidden="true" />
          </a>
        )}
      </div>
    </div>
  );
}

function AddComponentDialog({
  type,
  onClose,
  onAdd,
}: {
  type: AddableToVariable | 'Constraint' | null;
  onClose: () => void;
  onAdd: (type: AddableToVariable | 'Constraint', fields: Fields, choice: ShapeChoice) => void;
}) {
  const [fields, setFields] = React.useState<Fields>({ label: '', iri: '', comment: '' });
  const [shape, setShape] = React.useState<ShapeChoice>({ shape: 'simple', pairIndex: 0 });
  React.useEffect(() => {
    if (type) {
      setFields({ label: '', iri: '', comment: '' });
      setShape({ shape: 'simple', pairIndex: 0 });
    }
  }, [type]);
  const canBeSystem = type === 'ContextObject' || type === 'Matrix';

  return (
    <Dialog open={type !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md">
        <form
          className="flex min-h-0 flex-col"
          onSubmit={(event) => {
            event.preventDefault();
            if (type) onAdd(type, fields, shape);
          }}
        >
          <DialogHeader>
            <DialogTitle>Add {type ? ADD_LABELS[type].toLowerCase() : ''}</DialogTitle>
            <DialogDescription>It is added to the diagram and the Turtle.</DialogDescription>
          </DialogHeader>
          <DialogBody className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="add-label">Label</Label>
              <Input id="add-label" autoFocus required value={fields.label} onChange={(event) => setFields({ ...fields, label: event.target.value })} />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="add-iri">IRI</Label>
              <Input id="add-iri" placeholder="Optional; leave empty for a blank node" spellCheck={false} className="font-mono text-[12.5px]" value={fields.iri} onChange={(event) => setFields({ ...fields, iri: event.target.value })} />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="add-description">Description</Label>
              <Textarea id="add-description" rows={2} placeholder="Optional" value={fields.comment} onChange={(event) => setFields({ ...fields, comment: event.target.value })} />
            </div>
            {canBeSystem && <ShapePicker value={shape} onChange={setShape} />}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={!fields.label.trim()}>Add</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
