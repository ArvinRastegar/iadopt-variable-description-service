import * as React from 'react';
import { BookOpenIcon, LogOutIcon, ShieldCheckIcon } from 'lucide-react';
import { toast } from 'sonner';

import logoUrl from '../../logo.svg';
import { Button } from '@/components/ui/button';
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

export function AppHeader({
  nav,
  auth,
  brandHref,
  className,
}: {
  nav: NavItem[];
  auth: Pick<ReturnType<typeof useAuth>, 'user' | 'ready' | 'isAdmin' | 'openSignIn' | 'signOut'>;
  /** On the main page the logo is a route (#/), so following it does not reload the page. */
  brandHref?: string;
  className?: string;
}) {
  return (
    <header className={cn('sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b border-border bg-surface px-3 sm:px-5', className)}>
      <Brand href={brandHref} />
      <nav aria-label="Main" className="ml-1 flex min-w-0 items-center gap-0.5 overflow-x-auto sm:ml-3">
        {nav.map((item) => (
          <a
            key={item.href}
            href={item.href}
            aria-current={item.current ? 'page' : undefined}
            className={cn(
              'flex h-8 items-center rounded-md px-2.5 text-sm font-medium whitespace-nowrap no-underline transition-colors',
              item.current ? 'bg-surface-2 text-foreground' : 'text-text-2 hover:bg-surface-2 hover:text-foreground',
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

function AccountButton({ auth }: { auth: Pick<ReturnType<typeof useAuth>, 'user' | 'ready' | 'isAdmin' | 'openSignIn' | 'signOut'> }) {
  const [busy, setBusy] = React.useState(false);
  if (!auth.user) {
    return (
      <Button variant="outline" onClick={() => auth.openSignIn()}>
        Sign in
      </Button>
    );
  }
  const name = displayName(auth.user);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="flex h-[38px] items-center gap-2 rounded-full border border-border py-0 pr-3 pl-1 text-foreground transition-colors outline-none hover:bg-surface-2 focus-visible:ring-[3px] focus-visible:ring-ring/40"
          aria-label={`Account menu for ${name}`}
        >
          <span aria-hidden="true" className="flex size-7 items-center justify-center rounded-full bg-prop-bg text-xs font-semibold text-prop-fg">
            {initials(auth.user)}
          </span>
          <span className="hidden max-w-40 truncate text-[13px] font-medium sm:inline">{name}</span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60">
        <DropdownMenuLabel className="flex flex-col gap-0.5">
          <span className="text-sm font-semibold text-foreground">{name}</span>
          <span className="font-normal">Signed in as {auth.user.username}</span>
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
        <DropdownMenuItem
          disabled={busy}
          onSelect={async () => {
            setBusy(true);
            try {
              await auth.signOut();
            } catch (error) {
              console.error(error);
              toast.error('Could not sign out. Please try again.');
            } finally {
              setBusy(false);
            }
          }}
        >
          <LogOutIcon aria-hidden="true" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
