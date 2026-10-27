import * as React from 'react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input, Label, PasswordInput } from '@/components/ui/fields';
import { AuthRequiredError, fetchCurrentUser, postEvent, signIn, signOut, type User } from './api';

/*
 * Accounts on the main page. Anonymous visitors keep every public tool; protected actions
 * (decompose, publish, retract) ask for a sign-in in place, keep the work on the page, and are
 * never repeated automatically after signing in. A response that belongs to an older session
 * (tracked by `revision`) must never sign out a newer one.
 */

interface AuthContextValue {
  user: User | null;
  ready: boolean;
  isAdmin: boolean;
  /** True when signed in; otherwise opens the sign-in dialog and resolves false. */
  requireUser: (action: string) => Promise<boolean>;
  /** Run a protected request; a 401 signs the visitor out locally and asks them to sign in. */
  runProtected: <T>(request: () => Promise<T>, action: string) => Promise<T>;
  openSignIn: (action?: string) => void;
  signOut: () => Promise<void>;
  /** Activity event for signed-in users only; anonymous use sends nothing. */
  logEvent: (action: string, payload?: Record<string, unknown>, metadata?: Record<string, unknown>) => void;
}

const AuthContext = React.createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const value = React.useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}

export function displayName(user: User | null): string {
  return user ? user.display_name || user.username : '';
}

export function initials(user: User | null): string {
  const name = displayName(user).trim();
  if (!name) return '?';
  const parts = name.split(/[\s._-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? '') + (parts.length > 1 ? parts[parts.length - 1][0] : parts[0]?.[1] ?? '')).toUpperCase();
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUserState] = React.useState<User | null>(null);
  const [ready, setReady] = React.useState(false);
  const [dialogReason, setDialogReason] = React.useState<string | null>(null);
  const userRef = React.useRef<User | null>(null);
  const revisionRef = React.useRef(0);

  const setUser = React.useCallback((next: User | null) => {
    userRef.current = next;
    revisionRef.current += 1;
    setUserState(next);
  }, []);

  const refresh = React.useCallback(async (): Promise<User | null> => {
    const revision = revisionRef.current;
    try {
      const current = await fetchCurrentUser();
      // A slow probe must not overwrite a newer sign-in or sign-out.
      if (revision === revisionRef.current) setUser(current);
    } catch (error) {
      console.error(error);
      if (revision === revisionRef.current) setUser(null);
    }
    return userRef.current;
  }, [setUser]);

  React.useEffect(() => {
    void refresh().finally(() => setReady(true));
  }, [refresh]);

  const openSignIn = React.useCallback((action = 'decompose variables or publish nanopublications') => {
    setDialogReason(action);
  }, []);

  const requireUser = React.useCallback(async (action: string) => {
    if (await refresh()) return true;
    openSignIn(action);
    return false;
  }, [openSignIn, refresh]);

  const runProtected = React.useCallback(async <T,>(request: () => Promise<T>, action: string): Promise<T> => {
    const revision = revisionRef.current;
    try {
      return await request();
    } catch (error) {
      if (error instanceof AuthRequiredError && revision === revisionRef.current) {
        setUser(null);
        openSignIn(action);
      }
      throw error;
    }
  }, [openSignIn, setUser]);

  const logEvent = React.useCallback((action: string, payload: Record<string, unknown> = {}, metadata: Record<string, unknown> = {}) => {
    if (!userRef.current) return;
    const revision = revisionRef.current;
    postEvent(action, payload, metadata)
      .then((response) => {
        // Expired telemetry must not interrupt local editing or export with a login prompt.
        if (response.status === 401 && revision === revisionRef.current) setUser(null);
      })
      .catch((error) => console.error(error));
  }, [setUser]);

  const doSignOut = React.useCallback(async () => {
    await signOut();
    setUser(null);
  }, [setUser]);

  const value = React.useMemo<AuthContextValue>(() => ({
    user,
    ready,
    isAdmin: Boolean(user?.roles?.includes('admin')),
    requireUser,
    runProtected,
    openSignIn,
    signOut: doSignOut,
    logEvent,
  }), [doSignOut, logEvent, openSignIn, ready, requireUser, runProtected, user]);

  return (
    <AuthContext.Provider value={value}>
      {children}
      <SignInDialog
        reason={dialogReason}
        onClose={() => setDialogReason(null)}
        onSignedIn={(next) => {
          setUser(next);
          setDialogReason(null);
          // Signing in never runs a decomposition or publishes anything by itself.
        }}
      />
    </AuthContext.Provider>
  );
}

function SignInDialog({
  reason,
  onClose,
  onSignedIn,
}: {
  reason: string | null;
  onClose: () => void;
  onSignedIn: (user: User) => void;
}) {
  const [username, setUsername] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [error, setError] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const open = reason !== null;

  React.useEffect(() => {
    if (!open) {
      setUsername('');
      setPassword('');
      setError('');
    }
  }, [open]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError('');
    setBusy(true);
    try {
      const user = await signIn(username.trim(), password);
      onSignedIn(user);
    } catch (caught) {
      setError(caught instanceof Error && caught.message ? caught.message : 'Login failed.');
    } finally {
      setPassword('');
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md" aria-describedby="sign-in-reason">
        <form onSubmit={submit} className="flex min-h-0 flex-col">
          <DialogHeader>
            <DialogTitle>Sign in</DialogTitle>
            <DialogDescription id="sign-in-reason">
              Sign in to {reason}. Your work will stay on this page.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="sign-in-username">Username</Label>
              <Input
                id="sign-in-username"
                name="username"
                autoComplete="username"
                required
                autoFocus
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="sign-in-password">Password</Label>
              <PasswordInput
                id="sign-in-password"
                name="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>
            <p role="status" aria-live="polite" className="m-0 min-h-5 text-[13px] text-destructive-soft-foreground">
              {error}
            </p>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
