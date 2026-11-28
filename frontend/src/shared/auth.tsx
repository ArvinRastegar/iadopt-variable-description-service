import * as React from 'react';
import { flushSync } from 'react-dom';
import { LogInIcon } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input, Label, PasswordInput } from '@/components/ui/fields';
import { clearWork, navigation, rohubSignInAddress, saveWork } from '@/lib/leaving';
import {
  ADMIN_PAGE_CAPABILITIES, allows, AuthRequiredError, fetchSession, isRecordedAccount, onOutdatedSession, postEvent, refusalMessage,
  SessionError, signIn, signInAsOwner, signOut,
  type Capabilities, type SignInMethod, type User,
} from './api';

/*
 * Accounts on the main page. Anonymous visitors keep every public tool; protected actions
 * (decompose, mark as best, publish, retract) ask for a sign-in in place, keep the work on the
 * page, and are never repeated automatically after signing in. With a password that happens in the
 * dialog; for ROHub the page is left, with the work kept in this tab, and comes back. Where only ROHub
 * signs people in, the owner still signs in with a password in the dialog, through "Owner sign-in".
 * A response that belongs to an older session (tracked by `revision`) must never sign out a newer one.
 */

interface AuthContextValue {
  user: User | null;
  ready: boolean;
  /** Whether the backend allows anything on the admin page. */
  isAdmin: boolean;
  /** False when the deployment has authentication switched off; null until the backend said. */
  authEnabled: boolean | null;
  /**
   * Whether this session's work is recorded (D-037): a signed-in account that may save its work, never
   * the auth-disabled development user.
   */
  recorded: boolean;
  /** Why a signed-in account's work is not recorded, or null when nothing refuses it. */
  recordRefusal: string | null;
  /** Why a signed-in account may not publish, or null when nothing refuses it. */
  publishRefusal: string | null;
  /** Whether the account is not approved yet, which an admin does. */
  waitingForApproval: boolean;
  /** True when signed in; otherwise opens the sign-in dialog and resolves false. */
  requireUser: (action: string) => Promise<boolean>;
  /** Run a protected request; a 401 signs the visitor out locally and asks them to sign in. */
  runProtected: <T>(request: () => Promise<T>, action: string) => Promise<T>;
  /** Run a background request; a 401 signs the visitor out locally without asking, like expired telemetry. */
  runQuietly: <T>(request: () => Promise<T>) => Promise<T>;
  openSignIn: (action?: string) => void;
  /** Sign out and keep the work in this browser; with a ROHub session the page then goes to ROHub. */
  signOut: () => Promise<void>;
  /** Sign out, empty the page's work and delete what this browser keeps of it. */
  signOutAndClear: () => Promise<void>;
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
  const [capabilities, setCapabilities] = React.useState<Capabilities>({});
  const [ready, setReady] = React.useState(false);
  const [authEnabled, setAuthEnabled] = React.useState<boolean | null>(null);
  const [methods, setMethods] = React.useState<SignInMethod[]>(['password']);
  const [dialogReason, setDialogReason] = React.useState<string | null>(null);
  // Where the page goes although its work could not be saved, once the person said so.
  const [unsavedAddress, setUnsavedAddress] = React.useState<string | null>(null);
  const userRef = React.useRef<User | null>(null);
  const revisionRef = React.useRef(0);

  const account = isRecordedAccount(user, authEnabled);
  const recordRefusal = account ? refusalMessage(capabilities, 'save_and_review') : null;
  const recorded = account && recordRefusal === null;
  // Publishing needs an approval in any case; decomposing does not while the daily limit is switched on.
  const waitingForApproval = capabilities.publish?.reason === 'approval_required';
  const recordedRef = React.useRef(recorded);
  React.useLayoutEffect(() => {
    recordedRef.current = recorded;
  }, [recorded]);

  const setUser = React.useCallback((next: User | null, nextCapabilities: Capabilities = {}) => {
    userRef.current = next;
    revisionRef.current += 1;
    setUserState(next);
    setCapabilities(nextCapabilities);
  }, []);

  const refresh = React.useCallback(async (): Promise<User | null> => {
    const revision = revisionRef.current;
    try {
      const session = await fetchSession();
      if (session.authEnabled !== null) setAuthEnabled(session.authEnabled);
      setMethods(session.methods);
      // A slow probe must not overwrite a newer sign-in or sign-out.
      if (revision === revisionRef.current) setUser(session.user, session.capabilities);
    } catch (error) {
      console.error(error);
      if (error instanceof SessionError) setMethods(error.methods);
      if (revision === revisionRef.current) setUser(null);
    }
    return userRef.current;
  }, [setUser]);

  /** Read the session again where nobody asked for it: a failure to read changes nothing. */
  const readIfPossible = React.useCallback(async () => {
    const revision = revisionRef.current;
    try {
      const session = await fetchSession();
      if (revision !== revisionRef.current) return;
      // Another tab may have signed out, or signed in somebody else: then the page is theirs as a whole.
      if (session.user && session.user.id === userRef.current?.id) setCapabilities(session.capabilities);
      else setUser(session.user, session.capabilities);
    } catch (error) {
      console.error(error);
    }
  }, [setUser]);

  React.useEffect(() => onOutdatedSession(refresh, readIfPossible), [readIfPossible, refresh]);

  /** Learn what a session that just started may do: the sign-in answers only who it is. */
  const loadCapabilities = React.useCallback(async () => {
    const revision = revisionRef.current;
    try {
      const session = await fetchSession();
      if (revision === revisionRef.current) setCapabilities(session.capabilities);
    } catch (error) {
      console.error(error);
    }
  }, []);

  // Somebody who waits for an approval hears of it elsewhere and comes back to this tab.
  React.useEffect(() => {
    if (!waitingForApproval) return undefined;
    const again = () => {
      if (document.visibilityState === 'visible') void readIfPossible();
    };
    window.addEventListener('focus', again);
    document.addEventListener('visibilitychange', again);
    return () => {
      window.removeEventListener('focus', again);
      document.removeEventListener('visibilitychange', again);
    };
  }, [readIfPossible, waitingForApproval]);

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

  const runQuietly = React.useCallback(async <T,>(request: () => Promise<T>): Promise<T> => {
    const revision = revisionRef.current;
    try {
      return await request();
    } catch (error) {
      // Background work (the record of edits and checks) must not interrupt editing with a login prompt.
      if (error instanceof AuthRequiredError && revision === revisionRef.current) setUser(null);
      throw error;
    }
  }, [setUser]);

  const logEvent = React.useCallback((action: string, payload: Record<string, unknown> = {}, metadata: Record<string, unknown> = {}) => {
    if (!userRef.current) return;
    const revision = revisionRef.current;
    // Whose work is not recorded is told that nothing is sent: then an event says what was done, and nothing of the work.
    (recordedRef.current ? postEvent(action, payload, metadata) : postEvent(action))
      .then((response) => {
        // Expired telemetry must not interrupt local editing or export with a login prompt.
        if (response.status === 401 && revision === revisionRef.current) setUser(null);
      })
      .catch((error) => console.error(error));
  }, [setUser]);

  /** Go to ROHub, or ask first when the work could not be saved in this tab. */
  const leave = React.useCallback((address: string) => {
    if (saveWork()) navigation.leave(address);
    else setUnsavedAddress(address);
  }, []);

  const doSignOut = React.useCallback(async () => {
    const address = await signOut();
    setUser(null);
    if (address) leave(address);
  }, [leave, setUser]);

  const doSignOutAndClear = React.useCallback(async () => {
    const address = await signOut();
    // Messages name the work and offer it back.
    toast.dismiss();
    // At once, so that nothing a page saves while it is left can bring the work back.
    flushSync(() => {
      setUser(null);
      clearWork();
    });
    if (address) navigation.leave(address);
  }, [setUser]);

  const value = React.useMemo<AuthContextValue>(() => ({
    user,
    ready,
    isAdmin: ADMIN_PAGE_CAPABILITIES.some((name) => allows(capabilities, name)),
    authEnabled,
    recorded,
    recordRefusal,
    publishRefusal: account ? refusalMessage(capabilities, 'publish') : null,
    waitingForApproval,
    requireUser,
    runProtected,
    runQuietly,
    openSignIn,
    signOut: doSignOut,
    signOutAndClear: doSignOutAndClear,
    logEvent,
  }), [
    account, authEnabled, capabilities, doSignOut, doSignOutAndClear, logEvent, openSignIn, ready, recordRefusal, recorded, requireUser,
    runProtected, runQuietly, user, waitingForApproval,
  ]);

  return (
    <AuthContext.Provider value={value}>
      {children}
      <SignInDialog
        reason={dialogReason}
        methods={methods}
        onClose={() => setDialogReason(null)}
        onSignedIn={(next) => {
          setUser(next);
          setDialogReason(null);
          void loadCapabilities();
          // Signing in never runs a decomposition or publishes anything by itself.
        }}
        onRohub={() => leave(rohubSignInAddress(window.location.pathname + window.location.hash))}
      />
      <UnsavedWorkDialog
        open={unsavedAddress !== null}
        onStay={() => setUnsavedAddress(null)}
        onLeave={() => {
          const address = unsavedAddress;
          setUnsavedAddress(null);
          if (address) navigation.leave(address);
        }}
      />
    </AuthContext.Provider>
  );
}

/** Asks before the page is left although its work could not be saved. */
function UnsavedWorkDialog({ open, onStay, onLeave }: { open: boolean; onStay: () => void; onLeave: () => void }) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onStay()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Your work could not be saved</DialogTitle>
          <DialogDescription>
            This browser did not let the page keep your work for when you come back from ROHub. Copy or download your Turtle first if you want to keep it.
          </DialogDescription>
        </DialogHeader>
        <DialogBody />
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onStay}>Stay on this page</Button>
          <Button type="button" onClick={onLeave}>Leave without saving</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The button that leaves for ROHub. */
export function RohubButton({ onClick, autoFocus, size }: { onClick: () => void; autoFocus?: boolean; size?: 'lg' }) {
  return (
    <Button type="button" variant="outline" size={size} autoFocus={autoFocus} onClick={onClick}>
      <LogInIcon aria-hidden="true" />
      Sign in with ROHub
    </Button>
  );
}

/**
 * The owner's own way in where only ROHub signs everybody else in (owner-sign-in, section 7): a
 * small link under the ROHub button that opens a password form.
 */
export function OwnerSignInLink({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <Button
      type="button"
      variant="link"
      className="h-auto self-center px-0 text-[13px] font-medium"
      aria-expanded={open}
      onClick={onToggle}
    >
      Owner sign-in
    </Button>
  );
}

/** Separates the two ways to sign in. */
export function OrDivider() {
  return (
    <div className="flex items-center gap-3 text-xs text-muted-foreground" aria-hidden="true">
      <span className="h-px flex-1 bg-border" />
      or
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}

function SignInDialog({
  reason,
  methods,
  onClose,
  onSignedIn,
  onRohub,
}: {
  reason: string | null;
  methods: SignInMethod[];
  onClose: () => void;
  onSignedIn: (user: User) => void;
  onRohub: () => void;
}) {
  const [username, setUsername] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [error, setError] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [ownerOpen, setOwnerOpen] = React.useState(false);
  const open = reason !== null;

  React.useEffect(() => {
    if (!open) {
      setUsername('');
      setPassword('');
      setError('');
      setOwnerOpen(false);
    }
  }, [open]);

  const withPassword = methods.includes('password');
  const withRohub = methods.includes('rohub');
  // Where the password form is there, the owner uses it; the link is for where only ROHub signs in.
  const ownerLink = withRohub && !withPassword;
  const ownerForm = ownerLink && ownerOpen;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError('');
    setBusy(true);
    try {
      const user = await (ownerForm ? signInAsOwner : signIn)(username.trim(), password);
      onSignedIn(user);
    } catch (caught) {
      setError(caught instanceof Error && caught.message ? caught.message : 'Login failed.');
    } finally {
      setPassword('');
      setBusy(false);
    }
  }

  const fields = (
    <>
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
    </>
  );

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md" aria-describedby="sign-in-reason">
        <form onSubmit={submit} className="flex min-h-0 flex-col">
          <DialogHeader>
            <DialogTitle>Sign in</DialogTitle>
            <DialogDescription id="sign-in-reason">
              Sign in to {reason}. {withPassword ? 'Your work will stay on this page.' : 'You come back to this page with your work.'}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="flex flex-col gap-4">
            {withPassword && fields}
            {withPassword && withRohub && <OrDivider />}
            {withRohub && <RohubButton onClick={onRohub} autoFocus={!withPassword} />}
            {ownerLink && <OwnerSignInLink open={ownerOpen} onToggle={() => setOwnerOpen((current) => !current)} />}
            {ownerForm && fields}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            {(withPassword || ownerForm) && <Button type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</Button>}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
