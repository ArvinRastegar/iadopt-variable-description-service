import type { Kind } from './kinds';

/** Colours the diagram is drawn with: CSS variables on screen, fixed values in exported files. */
export interface DiagramPalette {
  background: string;
  foreground: string;
  text2: string;
  text3: string;
  edge: string;
  con: string;
  primary: string;
  primaryForeground: string;
  warning: string;
  kinds: Record<Kind, { mark: string; bg: string; fg: string; line: string }>;
}

const KINDS: Kind[] = ['var', 'prop', 'ooi', 'mat', 'ctx', 'stat', 'con'];

/** Follows the page theme through CSS variables. */
export const LIVE_PALETTE: DiagramPalette = {
  background: 'var(--background)',
  foreground: 'var(--foreground)',
  text2: 'var(--text-2)',
  text3: 'var(--muted-foreground)',
  edge: 'var(--edge)',
  con: 'var(--con)',
  primary: 'var(--primary)',
  primaryForeground: 'var(--primary-foreground)',
  warning: 'var(--warning-foreground)',
  kinds: Object.fromEntries(
    KINDS.map((kind) => [kind, {
      mark: `var(--${kind})`,
      bg: `var(--${kind}-bg)`,
      fg: `var(--${kind}-fg)`,
      line: `var(--${kind}-line)`,
    }]),
  ) as DiagramPalette['kinds'],
};

/**
 * Exported SVG and PNG files always use the light values on white, so they read well in papers
 * and slides whatever theme the page was in. These mirror the light tokens in globals.css.
 */
export const EXPORT_PALETTE: DiagramPalette = {
  background: '#ffffff',
  foreground: '#101418',
  text2: '#454d59',
  text3: '#646c79',
  edge: '#7c8593',
  con: '#d2504a',
  primary: '#1e7a4c',
  primaryForeground: '#ffffff',
  warning: '#7a4f00',
  kinds: {
    var: { mark: '#2e8b57', bg: '#e9f5ee', fg: '#1b5e3c', line: '#b9dec9' },
    prop: { mark: '#8b5cc7', bg: '#f3ecfa', fg: '#5b3290', line: '#dcc8ef' },
    ooi: { mark: '#2f6fb0', bg: '#dceaf8', fg: '#163f69', line: '#a9c8ea' },
    mat: { mark: '#4c8fcb', bg: '#e7f1fb', fg: '#1d4b75', line: '#bcd6f0' },
    ctx: { mark: '#7da9d6', bg: '#f1f6fc', fg: '#27507a', line: '#cfe0f2' },
    stat: { mark: '#6b7280', bg: '#f0f1f3', fg: '#3b4150', line: '#d3d6dc' },
    con: { mark: '#d2504a', bg: '#fdedec', fg: '#8f2a25', line: '#f3c2bf' },
  },
};
