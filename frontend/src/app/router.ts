import * as React from 'react';

/*
 * Views of the main page live in the URL hash (#/workspace, #/publications), so the page keeps
 * working behind any static file server and the existing nginx rules need no change.
 */

export type Route = 'start' | 'workspace' | 'publications';

function parse(hash: string): Route {
  const path = hash.replace(/^#\/?/, '').split(/[?#]/)[0];
  if (path === 'workspace') return 'workspace';
  if (path === 'publications') return 'publications';
  return 'start';
}

function subscribe(callback: () => void) {
  window.addEventListener('hashchange', callback);
  return () => window.removeEventListener('hashchange', callback);
}

export function useRoute(): Route {
  return parse(React.useSyncExternalStore(subscribe, () => window.location.hash, () => ''));
}

export function routeHref(route: Route): string {
  return route === 'start' ? '#/' : `#/${route}`;
}

export function navigate(route: Route): void {
  const target = routeHref(route);
  if (window.location.hash !== target) window.location.hash = target;
  window.scrollTo({ top: 0 });
}
