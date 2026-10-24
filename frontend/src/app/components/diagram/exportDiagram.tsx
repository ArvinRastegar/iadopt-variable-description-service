import { renderToStaticMarkup } from 'react-dom/server';

import geistUrl from '@fontsource-variable/geist/files/geist-latin-wght-normal.woff2?url';
import geistMonoUrl from '@fontsource-variable/geist-mono/files/geist-mono-latin-wght-normal.woff2?url';

import { layoutHeight, LAYOUT_WIDTH, type Layout } from '@/lib/iadopt/layout';
import { EXPORT_PALETTE } from '@/lib/iadopt/palette';
import { DiagramSvg } from './DiagramSvg';

/*
 * Exported files embed the fonts, so text keeps the widths the layout measured, also in viewers
 * without Geist and when the SVG is drawn into a PNG (where page fonts are not available).
 */

async function toDataUri(url: string, type: string): Promise<string> {
  const response = await fetch(url);
  const bytes = new Uint8Array(await response.arrayBuffer());
  let binary = '';
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return `data:${type};base64,${btoa(binary)}`;
}

let fontCssPromise: Promise<string> | null = null;

function embeddedFontCss(): Promise<string> {
  fontCssPromise ??= Promise.all([toDataUri(geistUrl, 'font/woff2'), toDataUri(geistMonoUrl, 'font/woff2')])
    .then(([sans, mono]) => [
      `@font-face{font-family:'Geist Variable';font-weight:100 900;font-style:normal;src:url(${sans}) format('woff2');}`,
      `@font-face{font-family:'Geist Mono Variable';font-weight:100 900;font-style:normal;src:url(${mono}) format('woff2');}`,
    ].join(''))
    .catch((error) => {
      console.error(error);
      fontCssPromise = null;
      return '';
    });
  return fontCssPromise;
}

export async function diagramSvgMarkup(layout: Layout, title: string): Promise<string> {
  const fontCss = await embeddedFontCss();
  const markup = renderToStaticMarkup(
    <DiagramSvg layout={layout} palette={EXPORT_PALETTE} idPrefix="iadopt" mode="export" title={title} fontCss={fontCss} />,
  );
  return `<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n${markup}`;
}

export async function diagramSvgBlob(layout: Layout, title: string): Promise<Blob> {
  return new Blob([await diagramSvgMarkup(layout, title)], { type: 'image/svg+xml;charset=utf-8' });
}

/** Draw the exported SVG into a canvas at twice its size and return a PNG. */
export async function diagramPngBlob(layout: Layout, title: string): Promise<Blob> {
  const url = URL.createObjectURL(await diagramSvgBlob(layout, title));
  try {
    const image = new Image();
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = (event) => reject(new Error('Failed to generate PNG', { cause: event }));
      image.src = url;
    });
    const scale = 2;
    const width = (LAYOUT_WIDTH + 20) * scale;
    const height = (layoutHeight(layout) + 20) * scale;
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Failed to generate PNG');
    context.drawImage(image, 0, 0, width, height);
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('Failed to generate PNG'))), 'image/png');
    });
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** Trigger a download of a blob without navigating away. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** File name from the variable IRI's last segment, as before. */
export function exportFileName(iri: string | null | undefined, extension: string): string {
  const base = (iri || 'visualization').split(/[/#]/).filter(Boolean).pop() || 'visualization';
  return `${base}.${extension}`;
}
