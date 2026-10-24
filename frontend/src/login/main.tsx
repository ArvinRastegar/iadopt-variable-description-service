import * as React from 'react';
import { createRoot } from 'react-dom/client';

import '@/styles/globals.css';
import { Button } from '@/components/ui/button';
import { Input, Label } from '@/components/ui/fields';
import { safeRedirect } from '@/lib/safeRedirect';
import { fetchCurrentUser, signIn } from '@/shared/api';
import { Brand } from '@/shared/AppHeader';
import { ThemeProvider, ThemeToggle } from '@/shared/theme';

/*
 * Stand-alone sign-in page. nginx sends visitors here when a protected page (the admin page)
 * needs a session; afterwards they go back to `?redirect=`.
 */

function LoginPage() {
  const redirect = React.useMemo(() => safeRedirect(new URLSearchParams(window.location.search).get('redirect')), []);
  const [username, setUsername] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [error, setError] = React.useState('');
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    fetchCurrentUser()
      .then((user) => {
        if (user) window.location.replace(redirect);
      })
      .catch((caught) => console.error(caught));
  }, [redirect]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError('');
    setBusy(true);
    try {
      await signIn(username.trim(), password);
      window.location.replace(redirect);
    } catch (caught) {
      setError(caught instanceof Error && caught.message ? caught.message : 'Login failed.');
      setPassword('');
    } finally {
      setBusy(false);
    }
  }

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
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="username">Username</Label>
            <Input id="username" autoComplete="username" required autoFocus value={username} onChange={(event) => setUsername(event.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="password">Password</Label>
            <Input id="password" type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
          </div>
          <p role="status" aria-live="polite" className="m-0 min-h-5 text-[13px] text-destructive-soft-foreground">{error}</p>
          <Button type="submit" size="lg" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</Button>
        </form>
      </main>
    </div>
  );
}

const element = document.getElementById('root');
if (element) {
  createRoot(element).render(
    <React.StrictMode>
      <ThemeProvider>
        <LoginPage />
      </ThemeProvider>
    </React.StrictMode>,
  );
}
