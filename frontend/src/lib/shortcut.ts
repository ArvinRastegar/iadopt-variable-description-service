/*
 * The "send with Cmd/Ctrl + Enter" shortcut of the definition boxes, named in the visitor's own keys:
 * ⌘ only exists on Apple keyboards, so everyone else sees Ctrl.
 */

/** For `aria-keyshortcuts`: both combinations work everywhere. */
export const SUBMIT_SHORTCUT_KEYS = 'Meta+Enter Control+Enter';

/** The visible hint: "⌘↵" on Apple platforms, "Ctrl ↵" elsewhere. */
export function submitShortcutLabel(): string {
  const nav = globalThis.navigator as (Navigator & { userAgentData?: { platform?: string } }) | undefined;
  const platform = nav?.userAgentData?.platform || nav?.platform || nav?.userAgent || '';
  return /mac|iphone|ipad|ipod/i.test(platform) ? '⌘↵' : 'Ctrl ↵';
}
