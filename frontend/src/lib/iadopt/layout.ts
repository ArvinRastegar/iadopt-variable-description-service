import Cfg from '@/config.js';
import createLayout from '@/lib/createLayout.js';
import type { Concept, Variable } from '@/model/models.js';

/*
 * The diagram reuses the existing layout engine (src/lib/createLayout.js), so boxes, systems and
 * constraints sit exactly where the previous visualizer put them. Only the drawing is new.
 */

export interface LayoutText {
  x: number;
  y: number;
  text: string;
  className: string;
  link?: string;
}

export interface LayoutBox {
  comp: Concept;
  x: number;
  y: number;
  width: number;
  height: number;
  descSeparator: number;
  className: string;
  texts: LayoutText[];
}

export interface LayoutArrow {
  path: Array<{ x: number; y: number }>;
  text?: string;
  x?: number;
  y?: number;
  rotate?: boolean;
  hideHead?: boolean;
  type: string;
}

export interface Layout {
  boxes: LayoutBox[];
  arrows: LayoutArrow[];
}

export const LAYOUT_WIDTH: number = Cfg.layout.width;
export const LAYOUT_MARGIN: number = Cfg.layout.margin;
export const HEADER_HEIGHT: number = Cfg.layout.entity.header.height;

/** Default display order of the components: property, object of interest, matrix, modifier, context. */
export const DEFAULT_ORDER = 'POMSC';

/**
 * Text is measured with the diagram font, so wait for it before laying out. Otherwise the first
 * layout measures a fallback font and long labels can overflow their boxes.
 */
export async function waitForDiagramFont(): Promise<void> {
  try {
    await document.fonts?.load('600 14px "Geist Variable"');
    await document.fonts?.ready;
  } catch {
    // Measuring a fallback font is still better than not drawing at all.
  }
}

export async function layoutVariable(variable: Variable, order: string = DEFAULT_ORDER): Promise<Layout> {
  await waitForDiagramFont();
  return createLayout(variable, order.toLowerCase()) as Layout;
}

/** Height of the drawing, computed the same way as the previous SVG renderer. */
export function layoutHeight(layout: Layout): number {
  const boxBottom = Math.max(0, ...layout.boxes.map((box) => box.y + box.height));
  const arrowBottom = Math.max(0, ...layout.arrows.flatMap((arrow) => arrow.path.map((point) => point.y)));
  return Math.max(boxBottom, arrowBottom) + LAYOUT_MARGIN;
}
