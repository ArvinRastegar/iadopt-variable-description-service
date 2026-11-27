import * as React from 'react';
import { BookOpenIcon, CheckIcon, EraserIcon, LogOutIcon, MenuIcon, ShieldCheckIcon } from 'lucide-react';
import logoUrl from '../../logo.svg';
import { Button } from '@/components/ui/button';
import { Pill, toastError } from '@/components/ui/feedback';
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/menus';
import { cn } from '@/lib/utils';
import { displayName, initials, type useAuth } from './auth';
import { ThemeToggle } from './theme';

export interface NavItem {
  href: string;
  label: string;
  current?: boolean;
}

export function Brand({ href = './index.html#/', compact = false }: { href?: string; compact?: boolean }) {
  return (
    <a href={href} className="flex shrink-0 items-center gap-3 text-foreground no-underline" aria-label="I-ADOPT Variable Description Service, start page">
      <img src={logoUrl} alt="I-ADOPT" className="block h-[30px] w-auto" />
      {!compact && <span className="hidden text-[13px] font-medium whitespace-nowrap text-text-2 sm:inline">Variable Description Service</span>}
    </a>
  );
}

type HeaderAuth = Pick<ReturnType<typeof useAuth>, 'user' | 'ready' | 'isAdmin' | 'waitingForApproval' | 'openSignIn' | 'signOut' | 'signOutAndClear'>;

export function AppHeader({
  nav,
  auth,
  brandHref,
  className,
}: {
  nav: NavItem[];
  auth: HeaderAuth;
  /** On the main page the logo is a route (#/), so following it does not reload the page. */
  brandHref?: string;
  className?: string;
}) {
  return (
    <header className={cn('sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b border-border bg-surface px-3 sm:px-5', className)}>
      <Brand href={brandHref} />
      {/*
        Below 1024px the links sit behind a menu button: from 640px the logo text and, from 768px, the guide
        link leave too little room, and the strip would run over the buttons beside it.
      */}
      <nav aria-label="Main" className="lg:hidden">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" className="gap-1.5 px-2">
              <MenuIcon aria-hidden="true" />
              Menu
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-56">
            {nav.map((item) => (
              <DropdownMenuItem key={item.href} asChild>
                <a href={item.href} aria-current={item.current ? 'page' : undefined} className="justify-between text-foreground no-underline">
                  {item.label}
                  {item.current && <CheckIcon aria-hidden="true" />}
                </a>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </nav>
      <nav aria-label="Main" className="ml-3 hidden min-w-0 items-center gap-0.5 lg:flex">
        {nav.map((item) => (
          <a
            key={item.href}
            href={item.href}
            aria-current={item.current ? 'page' : undefined}
            className={cn(
              'flex h-8 items-center rounded-md px-2.5 text-sm font-medium whitespace-nowrap no-underline transition-colors',
              // The current page carries the tabs' underline, not only a tint.
              item.current ? 'bg-surface-2 text-foreground shadow-[inset_0_-2px_0_var(--foreground)]' : 'text-text-2 hover:bg-surface-2 hover:text-foreground',
            )}
          >
            {item.label}
          </a>
        ))}
      </nav>
      <div className="flex-1" />
      <a
        href="https://w3id.org/iadopt"
        target="_blank"
        rel="noopener noreferrer"
        className="hidden h-9 items-center gap-1.5 rounded-md px-2.5 text-sm font-medium text-text-2 no-underline hover:bg-surface-2 hover:text-foreground md:flex"
      >
        <BookOpenIcon className="size-4" aria-hidden="true" />
        I-ADOPT guide
      </a>
      <ThemeToggle />
      <AccountButton auth={auth} />
    </header>
  );
}

function AccountButton({ auth }: { auth: HeaderAuth }) {
  const [busy, setBusy] = React.useState(false);
  const [clearing, setClearing] = React.useState(false);

  /** Sign out, or show that it failed; nothing is cleared or left then. */
  async function run(signOut: () => Promise<void>) {
    setBusy(true);
    try {
      await signOut();
    } catch (error) {
      console.error(error);
      toastError('Could not sign out. Try again.', { id: 'sign-out-failed', description: error instanceof Error && error.message ? error.message : undefined });
    } finally {
      setBusy(false);
    }
  }

  if (!auth.user) {
    return (
      <Button variant="outline" onClick={() => auth.openSignIn()}>
        Sign in
      </Button>
    );
  }
  const name = displayName(auth.user);
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="flex h-[38px] items-center gap-2 rounded-full border border-border py-0 pr-3 pl-1 text-foreground transition-colors outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            aria-label={`Account menu for ${name}`}
          >
            <span aria-hidden="true" className="flex size-7 items-center justify-center rounded-full bg-prop-bg text-xs font-semibold text-prop-fg">
              {initials(auth.user)}
            </span>
            <span className="hidden max-w-40 truncate text-[13px] font-medium sm:inline">{name}</span>
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72">
          <DropdownMenuLabel className="flex flex-col gap-0.5">
            <span className="text-sm font-semibold text-foreground">{name}</span>
            <span className="font-normal">Signed in as {auth.user.username}</span>
            {auth.waitingForApproval && <Pill tone="warning" dot className="mt-1 self-start">Waiting for approval</Pill>}
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          {auth.isAdmin && (
            <DropdownMenuItem asChild>
              <a href="./admin.html" className="text-foreground no-underline">
                <ShieldCheckIcon aria-hidden="true" />
                Admin
              </a>
            </DropdownMenuItem>
          )}
          <DropdownMenuItem disabled={busy} onSelect={() => void run(auth.signOut)}>
            <LogOutIcon aria-hidden="true" />
            Sign out
          </DropdownMenuItem>
          <DropdownMenuItem disabled={busy} onSelect={() => setClearing(true)}>
            <EraserIcon aria-hidden="true" />
            Sign out and clear this browser
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog open={clearing} onOpenChange={setClearing}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Sign out and clear this browser?</DialogTitle>
            <DialogDescription>
              This deletes what this browser keeps for you: the draft in the workspace, the remembered ORCID iD and the list of
              publications. Published nanopublications stay public. Other open tabs of this site keep their work.
            </DialogDescription>
          </DialogHeader>
          <DialogBody />
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setClearing(false)}>Cancel</Button>
            <Button
              type="button"
              variant="destructive"
              disabled={busy}
              onClick={() => {
                setClearing(false);
                void run(auth.signOutAndClear);
              }}
            >
              Sign out and clear
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
