import * as React from 'react';
import { createRoot } from 'react-dom/client';

import '@/styles/globals.css';
import { Button } from '@/components/ui/button';
import { Callout } from '@/components/ui/feedback';
import { Input, Label, PasswordInput } from '@/components/ui/fields';
import { navigation, ROHUB_SIGN_OUT_ADDRESS, rohubSignInAddress } from '@/lib/leaving';
import { safeRedirect } from '@/lib/safeRedirect';
import { fetchSession, SessionError, signIn, signInAsOwner, type SignInMethod } from '@/shared/api';
import { Brand } from '@/shared/AppHeader';
import { OrDivider, OwnerSignInLink, RohubButton } from '@/shared/auth';
import { ThemeProvider, ThemeToggle } from '@/shared/theme';

/*
 * Stand-alone sign-in page. nginx sends visitors here when a protected page (the admin page)
 * needs a session; afterwards they go back to `?redirect=`. A ROHub sign-in that did not work
 * ends here too, with a code in `?error=`: the page then says what happened and stays. Where only
 * ROHub signs people in, the owner signs in with a password through "Owner sign-in".
 */

const GENERAL_ERROR = 'The sign-in did not work. Please try again.';

const ERROR_TEXTS = new Map([
  ['cancelled', 'The sign-in was cancelled.'],
  ['sign_in_expired', 'This sign-in is no longer valid. Please start again.'],
  ['provider_unavailable', 'ROHub cannot be reached right now. Please try again later.'],
  ['provider_refused', 'ROHub did not complete the sign-in. Please start again.'],
  ['token_invalid', 'ROHub\'s answer could not be verified, so you were not signed in.'],
  ['email_not_verified', 'ROHub did not confirm your email address. Verify it, then sign in again.'],
  ['blocked', 'This account is blocked.'],
  ['rate_limited', 'Too many sign-in attempts from your network. Please wait a minute.'],
  ['sign_out_incomplete', 'ROHub\'s sign-out page cannot be reached, so you may still be signed in there.'],
]);

/** How long the page waits to learn how people sign in here before it shows the password form. */
const ANSWER_MS = 150;

/** The codes after which the person is still signed in at ROHub and may want to be no more. */
const SIGNED_IN_AT_ROHUB = ['blocked', 'email_not_verified'];

export function LoginPage() {
  const { redirect, errorCode } = React.useMemo(() => {
    const parameters = new URLSearchParams(window.location.search);
    return { redirect: safeRedirect(parameters.get('redirect')), errorCode: parameters.get('error') };
  }, []);
  // Null until the backend said how people sign in here.
  const [methods, setMethods] = React.useState<SignInMethod[] | null>(null);
  const [username, setUsername] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [error, setError] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [ownerOpen, setOwnerOpen] = React.useState(false);

  // The password form does not wait for a slow answer: without ROHub it was there from the start.
  React.useEffect(() => {
    const handle = window.setTimeout(() => setMethods((current) => current ?? ['password']), ANSWER_MS);
    return () => window.clearTimeout(handle);
  }, []);

  React.useEffect(() => {
    fetchSession()
      .then((session) => {
        // A sign-in that was refused is explained first, also to someone who still has a session.
        if (session.user && errorCode === null) window.location.replace(redirect);
        else setMethods(session.methods);
      })
      .catch((caught) => {
        console.error(caught);
        setMethods(caught instanceof SessionError ? caught.methods : ['password']);
      });
  }, [errorCode, redirect]);

  const withPassword = methods?.includes('password') ?? false;
  const withRohub = methods?.includes('rohub') ?? false;
  // Where the password form is there, the owner uses it; the link is for where only ROHub signs in.
  const ownerLink = withRohub && !withPassword;
  const ownerForm = ownerLink && ownerOpen;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError('');
    setBusy(true);
    try {
      await (ownerForm ? signInAsOwner : signIn)(username.trim(), password);
      window.location.replace(redirect);
    } catch (caught) {
      setError(caught instanceof Error && caught.message ? caught.message : 'Login failed.');
      setPassword('');
    } finally {
      setBusy(false);
    }
  }

  const fields = (
    <>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="username">Username</Label>
        <Input id="username" autoComplete="username" required autoFocus value={username} onChange={(event) => setUsername(event.target.value)} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="password">Password</Label>
        <PasswordInput id="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
      </div>
      <p role="status" aria-live="polite" className="m-0 min-h-5 text-[13px] text-destructive-soft-foreground">{error}</p>
      <Button type="submit" size="lg" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</Button>
    </>
  );

  return (
    <div className="canvas-dots flex min-h-screen flex-col">
      <header className="flex h-14 items-center justify-between border-b border-border bg-surface px-5">
        <Brand href="./index.html#/" />
        <ThemeToggle />
      </header>
      <main id="main" className="flex flex-1 items-center justify-center px-5 py-12">
        <form onSubmit={submit} className="flex w-full max-w-[420px] flex-col gap-5 rounded-2xl border border-border bg-surface p-7 shadow-md">
          <div className="flex flex-col gap-1.5">
            <h1 className="m-0 text-2xl leading-8 font-[650] tracking-[-0.02em]">Sign in</h1>
            <p className="m-0 text-[15px] leading-[22px] text-text-2">
              Sign in to decompose variables or publish and retract nanopublications.{' '}
              <a href="./index.html#/">Continue without signing in</a> to visualize, edit and export Turtle.
            </p>
          </div>
          {errorCode !== null && (
            <Callout tone="warning" role="alert">
              {ERROR_TEXTS.get(errorCode) ?? GENERAL_ERROR}
              {SIGNED_IN_AT_ROHUB.includes(errorCode) && (
                <>
                  {' '}You are still signed in at ROHub. <a href={ROHUB_SIGN_OUT_ADDRESS} className="font-medium text-current underline">Sign out at ROHub</a> to use another account.
                </>
              )}
            </Callout>
          )}
          {withPassword && fields}
          {withPassword && withRohub && <OrDivider />}
          {withRohub && <RohubButton size="lg" autoFocus={!withPassword} onClick={() => navigation.leave(rohubSignInAddress(redirect))} />}
          {ownerLink && <OwnerSignInLink open={ownerOpen} onToggle={() => setOwnerOpen((current) => !current)} />}
          {ownerForm && fields}
        </form>
      </main>
    </div>
  );
}

/** Mount the sign-in page into an element; tests mount it into their own container. */
export function mountLogin(element: HTMLElement) {
  const root = createRoot(element);
  root.render(
    <React.StrictMode>
      <ThemeProvider>
        <LoginPage />
      </ThemeProvider>
    </React.StrictMode>,
  );
  return root;
}

const element = document.getElementById('root');
if (element) mountLogin(element);
