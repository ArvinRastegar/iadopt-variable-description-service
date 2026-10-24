/*
 * The I-ADOPT component kinds as the interface shows them. Each kind maps to the colour tokens
 * defined in src/styles/globals.css (`--<kind>`, `--<kind>-bg`, `--<kind>-fg`, `--<kind>-line`).
 */

export type Kind = 'var' | 'prop' | 'ooi' | 'mat' | 'ctx' | 'stat' | 'con';

export const KIND_NAMES: Record<Kind, string> = {
  var: 'Variable',
  prop: 'Property',
  ooi: 'Object of interest',
  mat: 'Matrix',
  ctx: 'Context object',
  stat: 'Statistical modifier',
  con: 'Constraint',
};

/** Roles set by src/model/models.js, mapped to their kind. System components use their parent's kind. */
const ROLE_KINDS: Record<string, Kind> = {
  Variable: 'var',
  Property: 'prop',
  OoI: 'ooi',
  Matrix: 'mat',
  ContextObject: 'ctx',
  StatisticalModifier: 'stat',
  Constraint: 'con',
};

export function kindOfRole(role: string | undefined): Kind | null {
  return (role && ROLE_KINDS[role]) || null;
}

/**
 * The kind of a layout box. The layout engine names boxes "<type> <role>" in lower case, where a
 * system component carries its parent's role, e.g. "entity ooi" or "system matrix".
 */
export function kindOfBoxClass(className: string): Kind {
  const tokens = className.toLowerCase().split(/\s+/);
  if (tokens.includes('variable')) return 'var';
  if (tokens.includes('property')) return 'prop';
  if (tokens.includes('constraint')) return 'con';
  if (tokens.includes('statisticalmodifier') || tokens.includes('statmod')) return 'stat';
  if (tokens.includes('matrix')) return 'mat';
  if (tokens.includes('contextobject')) return 'ctx';
  return 'ooi';
}

/** Tailwind classes for a pill or chip of a kind. Written out in full so Tailwind can see them. */
export const KIND_CHIP: Record<Kind, string> = {
  var: 'bg-var-bg text-var-fg border-var-line',
  prop: 'bg-prop-bg text-prop-fg border-prop-line',
  ooi: 'bg-ooi-bg text-ooi-fg border-ooi-line',
  mat: 'bg-mat-bg text-mat-fg border-mat-line',
  ctx: 'bg-ctx-bg text-ctx-fg border-ctx-line',
  stat: 'bg-stat-bg text-stat-fg border-stat-line',
  con: 'bg-con-bg text-con-fg border-con-line border-dashed',
};

export const KIND_TEXT: Record<Kind, string> = {
  var: 'text-var-fg',
  prop: 'text-prop-fg',
  ooi: 'text-ooi-fg',
  mat: 'text-mat-fg',
  ctx: 'text-ctx-fg',
  stat: 'text-stat-fg',
  con: 'text-con-fg',
};

export const KIND_DOT: Record<Kind, string> = {
  var: 'bg-var',
  prop: 'bg-prop',
  ooi: 'bg-ooi',
  mat: 'bg-mat',
  ctx: 'bg-ctx',
  stat: 'bg-stat',
  con: 'bg-con',
};

/** Highlight classes for words of the definition that a component label was found in. */
export const KIND_HIGHLIGHT: Record<Kind, string> = {
  var: 'bg-var-bg shadow-[inset_0_-2px_0_var(--var)]',
  prop: 'bg-prop-bg shadow-[inset_0_-2px_0_var(--prop)]',
  ooi: 'bg-ooi-bg shadow-[inset_0_-2px_0_var(--ooi)]',
  mat: 'bg-mat-bg shadow-[inset_0_-2px_0_var(--mat)]',
  ctx: 'bg-ctx-bg shadow-[inset_0_-2px_0_var(--ctx)]',
  stat: 'bg-stat-bg shadow-[inset_0_-2px_0_var(--stat)]',
  con: 'bg-con-bg shadow-[inset_0_-2px_0_var(--con)]',
};

export const ORDER_LETTERS: Record<string, { label: string; kind: Kind }> = {
  p: { label: 'Property', kind: 'prop' },
  o: { label: 'Object of interest', kind: 'ooi' },
  m: { label: 'Matrix', kind: 'mat' },
  s: { label: 'Statistical modifier', kind: 'stat' },
  c: { label: 'Context object', kind: 'ctx' },
};
