import * as React from 'react';

import { TooltipProvider } from '@/components/ui/menus';
import { Toaster } from '@/components/ui/feedback';
import { AppHeader } from '@/shared/AppHeader';
import { AuthProvider, useAuth } from '@/shared/auth';
import { ThemeProvider, useTheme } from '@/shared/theme';
import { navigate, routeHref, useRoute } from './router';
import { useWorkspace, WorkspaceProvider } from './state/workspace';
import { PublicationsProvider } from './state/publications';
import { StartPage } from './pages/StartPage';
import { WorkspacePage } from './pages/WorkspacePage';
import { PublicationsPage } from './pages/PublicationsPage';

const SITE_NAME = 'I-ADOPT Variable Description Service';
const ROUTE_TITLES = { workspace: 'Workspace', publications: 'Publications' } as const;

function Shell() {
  const auth = useAuth();
  const { theme } = useTheme();
  const route = useRoute();
  const { openedFromUrl } = useWorkspace();

  // A shared ?ttl= link opens straight in the workspace, as it did before (first load only).
  const firstRoute = React.useRef(route);
  React.useEffect(() => {
    if (openedFromUrl && firstRoute.current === 'start' && !window.location.hash) navigate('workspace');
  }, [openedFromUrl]);

  // Each view has its own title, and moving to another view puts focus on its heading, as a page load would.
  const previousRoute = React.useRef(route);
  React.useEffect(() => {
    document.title = route === 'start' ? SITE_NAME : `${ROUTE_TITLES[route]} · ${SITE_NAME}`;
    if (previousRoute.current === route) return;
    previousRoute.current = route;
    const frame = window.requestAnimationFrame(() => {
      const heading = document.querySelector<HTMLElement>('main h1') ?? document.querySelector<HTMLElement>('main');
      if (!heading) return;
      heading.tabIndex = -1;
      heading.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [route]);

  const nav = [
    { href: routeHref('start'), label: 'New variable', current: route === 'start' },
    { href: routeHref('workspace'), label: 'Workspace', current: route === 'workspace' },
    { href: routeHref('publications'), label: 'Publications', current: route === 'publications' },
  ];

  return (
    <div className="flex min-h-screen flex-col">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-surface px-3 py-2 text-sm font-semibold focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
        onClick={(event) => {
          // The hash holds the route, so move focus without changing it.
          event.preventDefault();
          const main = document.querySelector<HTMLElement>('main');
          if (!main) return;
          main.tabIndex = -1;
          main.focus();
        }}
      >
        Skip to content
      </a>
      <AppHeader nav={nav} auth={auth} brandHref={routeHref('start')} />
      {route === 'start' && <StartPage />}
      {route === 'workspace' && <WorkspacePage />}
      {route === 'publications' && <PublicationsPage />}
      <Toaster theme={theme} />
    </div>
  );
}

export function App() {
  return (
    <ThemeProvider>
      <TooltipProvider>
        <AuthProvider>
          <WorkspaceProvider>
            <PublicationsProvider>
              <Shell />
            </PublicationsProvider>
          </WorkspaceProvider>
        </AuthProvider>
      </TooltipProvider>
    </ThemeProvider>
  );
}
