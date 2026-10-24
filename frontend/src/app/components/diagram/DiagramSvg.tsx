import * as React from 'react';

import { kindOfBoxClass, KIND_NAMES } from '@/lib/iadopt/kinds';
import { HEADER_HEIGHT, LAYOUT_WIDTH, layoutHeight, type Layout, type LayoutBox, type LayoutText } from '@/lib/iadopt/layout';
import { describeLink, isLinkableRole, isVocabularyLink, labelOf, type Component } from '@/lib/iadopt/model';
import type { DiagramPalette } from '@/lib/iadopt/palette';

/*
 * Draws a layout from the existing layout engine with the redesigned look. One component renders
 * both the interactive diagram on screen and the exported SVG/PNG files.
 */

const PAD = 10;
const SANS = "'Geist Variable', 'Helvetica Neue', Arial, sans-serif";
const MONO = "'Geist Mono Variable', 'SF Mono', Menlo, Consolas, monospace";

export interface DiagramSvgProps {
  layout: Layout;
  palette: DiagramPalette;
  idPrefix: string;
  mode: 'live' | 'export';
  title?: string;
  width?: number;
  selected?: Component | null;
  isChecked?: (comp: Component) => boolean;
  onSelect?: (comp: Component) => void;
  fontCss?: string;
}

function headerPath(box: LayoutBox, radius: number): string {
  const { x, y, width } = box;
  const h = HEADER_HEIGHT;
  return `M ${x},${y + radius} a ${radius},${radius} 0 0 1 ${radius},-${radius} h ${width - 2 * radius} a ${radius},${radius} 0 0 1 ${radius},${radius} v ${h - radius} h -${width} z`;
}

function boxLabel(box: LayoutBox): string {
  const kind = kindOfBoxClass(box.className);
  const label = labelOf(box.comp);
  return `${KIND_NAMES[kind]}${box.comp.getRole() === 'SystemComponent' ? ' part' : ''}: ${label || 'unnamed'}`;
}

function TextLine({ text, palette, fg, exportLinks }: { text: LayoutText; palette: DiagramPalette; fg: string; exportLinks: boolean }) {
  let props: React.SVGProps<SVGTextElement>;
  let content: string = text.text;
  if (text.className === 'type') {
    content = content.toUpperCase();
    props = { fontSize: 10.5, fontWeight: 650, letterSpacing: 0.6, fill: fg, fontFamily: SANS };
  } else if (text.className === 'title') {
    props = { fontSize: 14, fontWeight: 600, fill: palette.foreground, fontFamily: SANS };
  } else if (text.link) {
    props = { fontSize: 11, fill: palette.text2, fontFamily: MONO };
  } else {
    props = { fontSize: 11.5, fill: palette.text2, fontFamily: SANS };
  }

  const node = (
    <text x={text.x} y={text.y} textAnchor="middle" dominantBaseline="central" {...props}>
      {content}
    </text>
  );
  if (exportLinks && text.link) {
    return (
      <a href={text.link} target="_blank" rel="noopener noreferrer">
        {node}
      </a>
    );
  }
  return node;
}

function LinkGlyph({ x, y, color }: { x: number; y: number; color: string }) {
  return (
    <svg x={x} y={y} width={12} height={12} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" />
      <path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />
    </svg>
  );
}

function WarningGlyph({ x, y, color }: { x: number; y: number; color: string }) {
  return (
    <svg x={x} y={y} width={12} height={12} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M10.3 3.9 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4" />
      <path d="M12 17h.01" />
    </svg>
  );
}

export function DiagramSvg({
  layout,
  palette,
  idPrefix,
  mode,
  title,
  width,
  selected = null,
  isChecked,
  onSelect,
  fontCss,
}: DiagramSvgProps) {
  const live = mode === 'live';
  const height = layoutHeight(layout);
  const viewBox = `${-PAD} ${-PAD} ${LAYOUT_WIDTH + 2 * PAD} ${height + 2 * PAD}`;
  const edgeMarker = `${idPrefix}-edge`;
  const conMarker = `${idPrefix}-con`;

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox={viewBox}
      width={width ?? (live ? undefined : LAYOUT_WIDTH + 2 * PAD)}
      height={live ? undefined : height + 2 * PAD}
      role={live ? 'group' : 'img'}
      aria-label={title ? `I-ADOPT diagram of ${title}` : 'I-ADOPT diagram'}
      style={live ? { display: 'block', width: width ?? '100%', height: 'auto', overflow: 'visible' } : undefined}
    >
      {!live && title && <title>{title}</title>}
      <defs>
        {fontCss && <style>{fontCss}</style>}
        <marker id={edgeMarker} viewBox="0 0 10 10" refX={2} refY={5} markerWidth={7} markerHeight={7} markerUnits="userSpaceOnUse" orient="auto">
          <path d="M 0,0 L 10,5 L 0,10 z" fill={palette.edge} />
        </marker>
        <marker id={conMarker} viewBox="0 0 10 10" refX={2} refY={5} markerWidth={7} markerHeight={7} markerUnits="userSpaceOnUse" orient="auto">
          <path d="M 0,0 L 10,5 L 0,10 z" fill={palette.con} />
        </marker>
      </defs>

      {!live && <rect x={-PAD} y={-PAD} width={LAYOUT_WIDTH + 2 * PAD} height={height + 2 * PAD} fill={palette.background} />}

      {layout.arrows.map((arrow, index) => {
        const isConstraint = arrow.type === 'constrains' || arrow.type === 'hasConstraint';
        const d = `M ${arrow.path.map((point) => `${point.x},${point.y}`).join(' L ')}`;
        return (
          <g key={`a${index}`}>
            <path
              d={d}
              fill="none"
              stroke={isConstraint ? palette.con : palette.edge}
              strokeWidth={1.25}
              strokeDasharray={isConstraint ? '4 3' : undefined}
              markerEnd={arrow.hideHead ? undefined : `url(#${isConstraint ? conMarker : edgeMarker})`}
            />
            {arrow.text && arrow.x !== undefined && arrow.y !== undefined && (
              <text
                x={arrow.x}
                y={arrow.y}
                transform={arrow.rotate ? `rotate(-90 ${arrow.x} ${arrow.y})` : undefined}
                textAnchor="middle"
                dominantBaseline="central"
                fontSize={10.5}
                fontFamily={MONO}
                fill={palette.text3}
                stroke={palette.background}
                strokeWidth={4}
                strokeLinejoin="round"
                paintOrder="stroke"
              >
                {arrow.text}
              </text>
            )}
          </g>
        );
      })}

      {layout.boxes.map((box, index) => {
        const kind = kindOfBoxClass(box.className);
        const colors = palette.kinds[kind];
        const isSelected = live && selected === box.comp;
        const isConstraint = kind === 'con';
        const descLines = box.texts.filter((text) => text.className === 'desc');
        const role = box.comp.getRole();
        const link = describeLink(box.comp);
        const showLink = live && isLinkableRole(role) && isVocabularyLink(link);
        const showWarning = live && isLinkableRole(role) && (link.source === 'Local' || link.source === 'None');
        const checked = live && Boolean(isChecked?.(box.comp));

        const interactive = live
          ? {
              role: 'button',
              tabIndex: 0,
              'aria-label': boxLabel(box),
              'aria-pressed': isSelected,
              // Selection changes on click, Enter or Space only: moving focus with Tab must not
              // select every box it passes, which would also discard unsaved inspector edits.
              onClick: () => onSelect?.(box.comp),
              onKeyDown: (event: React.KeyboardEvent) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  onSelect?.(box.comp);
                }
              },
              style: { cursor: 'pointer', outline: 'none' } as React.CSSProperties,
            }
          : {};

        return (
          <g key={`b${index}`} {...interactive}>
            {isSelected && (
              <rect x={box.x - 4} y={box.y - 4} width={box.width + 8} height={box.height + 8} rx={14} fill="none" stroke={palette.primary} strokeWidth={2} />
            )}
            <rect
              x={box.x}
              y={box.y}
              width={box.width}
              height={box.height}
              rx={10}
              fill={colors.bg}
              stroke={isSelected ? palette.primary : colors.line}
              strokeWidth={isSelected ? 1.5 : 1}
              strokeDasharray={isConstraint && !isSelected ? '5 4' : undefined}
            />
            <path d={headerPath(box, 10)} fill={colors.mark} fillOpacity={0.13} />
            <line x1={box.x} y1={box.y + HEADER_HEIGHT} x2={box.x + box.width} y2={box.y + HEADER_HEIGHT} stroke={colors.line} strokeWidth={1} />
            {descLines.length > 0 && box.descSeparator > box.y + HEADER_HEIGHT && (
              <line x1={box.x + 10} y1={box.descSeparator} x2={box.x + box.width - 10} y2={box.descSeparator} stroke={colors.line} strokeWidth={1} />
            )}
            {box.texts.map((text, textIndex) => (
              <TextLine key={textIndex} text={text} palette={palette} fg={colors.fg} exportLinks={!live} />
            ))}
            {showLink && <LinkGlyph x={box.x + 8} y={box.y + 9} color={colors.fg} />}
            {showWarning && <WarningGlyph x={box.x + 8} y={box.y + 9} color={palette.warning} />}
            {checked && (
              <g aria-hidden="true">
                <circle cx={box.x + box.width - 12} cy={box.y + 15} r={7.5} fill={palette.primary} />
                <path
                  d={`M ${box.x + box.width - 15.5},${box.y + 15} l 2.4,2.4 l 4.6,-4.8`}
                  fill="none"
                  stroke={palette.primaryForeground}
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </g>
            )}
          </g>
        );
      })}
    </svg>
  );
}
