import * as React from 'react';
import { createRoot } from 'react-dom/client';
import { AlertTriangleIcon, ArrowLeftIcon, CircleCheckIcon, DownloadIcon, LogOutIcon, PlusIcon, RefreshCwIcon } from 'lucide-react';

import '@/styles/globals.css';
import { Button } from '@/components/ui/button';
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Callout, Pill, Toaster } from '@/components/ui/feedback';
import { Input, Label } from '@/components/ui/fields';
import { downloadBlob } from '@/lib/download';
import { cn } from '@/lib/utils';
import {
  ADMIN_PAGE_CAPABILITIES, adminApi, allows, ApiError, AuthRequiredError, fetchSession, signOut,
  type Account, type AdminStats, type AuditEvent, type Capabilities, type User,
} from '@/shared/api';
import { Brand } from '@/shared/AppHeader';
import { displayName } from '@/shared/auth';
import { ThemeProvider, ThemeToggle, useTheme } from '@/shared/theme';
import { AccountsTable, formatTime } from './accounts';
import { NewPasswordFields } from './NewPasswordFields';

/*
 * Admin dashboard over the admin API: statistics, readiness, accounts, the records export and the
 * audit log. Each part shows when the capabilities of the session allow it.
 */

const CHECK_LABELS: Record<string, string> = {
  schema_exists: 'JSON schema file',
  prompt_dir_exists: 'Prompt templates',
  five_shot_dir_exists: 'Few-shot examples',
  enabled_provider_keys_set: 'Provider API keys',
  wikidata_reranker_ready: 'Entity linking model',
};

function toLogin() {
  window.location.href = `./login.html?redirect=${encodeURIComponent(window.location.pathname)}`;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat('en-US').format(value);
}

function formatLatency(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`;
}

function AdminPage() {
  const { theme } = useTheme();
  const [me, setMe] = React.useState<User | null>(null);
  const [capabilities, setCapabilities] = React.useState<Capabilities>({});
  const [stats, setStats] = React.useState<AdminStats | null>(null);
  const [accounts, setAccounts] = React.useState<Account[] | null>(null);
  const [events, setEvents] = React.useState<AuditEvent[]>([]);
  const [status, setStatus] = React.useState<{ text: string; error?: boolean } | null>({ text: 'Loading…' });
  const [creating, setCreating] = React.useState(false);
  const [exporting, setExporting] = React.useState(false);

  const loads = React.useRef(0);

  const load = React.useCallback(async () => {
    loads.current += 1;
    const serial = loads.current;
    // A slow answer must not replace what a later load already shows.
    const outdated = () => serial !== loads.current;
    setStatus({ text: 'Loading…' });
    try {
      const session = await fetchSession();
      if (outdated()) return;
      if (!session.user) {
        toLogin();
        return;
      }
      setMe(session.user);
      setCapabilities(session.capabilities);
      const seesActivity = allows(session.capabilities, 'view_activity');
      const seesAccounts = allows(session.capabilities, 'manage_users');
      const [nextStats, nextAccounts, nextAudit] = await Promise.all([
        seesActivity ? adminApi.stats() : null,
        seesAccounts ? adminApi.users() : null,
        seesActivity ? adminApi.audit(100) : null,
      ]);
      if (outdated()) return;
      setStats(nextStats);
      setAccounts(nextAccounts ? nextAccounts.users ?? [] : null);
      setEvents(nextAudit?.events ?? []);
      setStatus(null);
    } catch (error) {
      if (outdated()) return;
      if (error instanceof AuthRequiredError) {
        toLogin();
        return;
      }
      setStatus({ text: error instanceof Error ? error.message : 'Could not load the dashboard.', error: true });
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  const seesActivity = allows(capabilities, 'view_activity');
  const managesAccounts = allows(capabilities, 'manage_users');
  const denied = me !== null && !ADMIN_PAGE_CAPABILITIES.some((name) => allows(capabilities, name));

  async function downloadRecords() {
    setExporting(true);
    setStatus(null);
    try {
      const { blob, fileName } = await adminApi.exportRecords();
      downloadBlob(blob, fileName);
    } catch (error) {
      if (error instanceof AuthRequiredError) return toLogin();
      setStatus({ text: error instanceof Error ? error.message : 'Could not export the records.', error: true });
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-border bg-surface px-3 sm:px-5">
        <Brand href="./index.html#/" compact />
        <Pill>Admin</Pill>
        <div className="flex-1" />
        <Button variant="ghost" asChild>
          <a href="./index.html#/" className="no-underline"><ArrowLeftIcon aria-hidden="true" />Back to the app</a>
        </Button>
        <ThemeToggle />
        {me && (
          <Button
            variant="outline"
            onClick={async () => {
              await signOut().catch(() => {});
              window.location.href = './login.html';
            }}
          >
            <LogOutIcon aria-hidden="true" />Sign out
          </Button>
        )}
      </header>

      <main id="main" className="mx-auto flex w-full max-w-[1280px] flex-1 flex-col gap-5 px-4 pt-7 pb-12 sm:px-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex flex-col gap-1">
            <h1 className="m-0 text-[26px] leading-8 font-[650] tracking-[-0.02em]">Overview</h1>
            <p className="m-0 text-muted-foreground">{me ? `Signed in as ${displayName(me)}. ` : ''}Figures cover the audit retention window.</p>
          </div>
          <Button variant="outline" onClick={() => void load()}><RefreshCwIcon aria-hidden="true" />Refresh</Button>
        </div>

        {status && (
          <div role="status" aria-live="polite">
            {status.error ? <Callout tone="danger">{status.text}</Callout> : <p className="m-0 text-sm text-muted-foreground">{status.text}</p>}
          </div>
        )}

        {denied && <Callout tone="warning" title="Admin access required">This account cannot open the admin dashboard.</Callout>}

        {seesActivity && stats && (
          <>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(min(190px,100%),1fr))] gap-3">
              <StatTile label="Readiness">
                <span className="flex items-center gap-2">
                  {stats.readiness.status === 'ready'
                    ? <CircleCheckIcon className="size-[22px] text-primary" aria-hidden="true" />
                    : <AlertTriangleIcon className="size-[22px] text-destructive-soft-foreground" aria-hidden="true" />}
                  {stats.readiness.status === 'ready' ? 'Ready' : 'Not ready'}
                </span>
                <Sub>{Object.values(stats.readiness.checks).filter(Boolean).length} of {Object.keys(stats.readiness.checks).length} checks pass</Sub>
              </StatTile>
              <StatTile label="Active users">{formatNumber(stats.active_users)}<Sub>of {formatNumber(stats.total_users)} accounts</Sub></StatTile>
              <StatTile label="Events">{formatNumber(stats.event_count_30d)}<Sub>{stats.active_usernames_30d.length === 1 ? '1 person active' : `${stats.active_usernames_30d.length} people active`}</Sub></StatTile>
              <StatTile label="Failures">
                <span className="flex items-center gap-2">
                  {formatNumber(stats.failures_30d)}
                  {stats.failures_30d > 0 && <Pill tone="warning"><AlertTriangleIcon aria-hidden="true" />Review</Pill>}
                </span>
                <Sub>{stats.event_count_30d ? `${((stats.failures_30d / stats.event_count_30d) * 100).toFixed(1)}% of events` : 'No events'}</Sub>
              </StatTile>
              <StatTile label="Average latency">{formatLatency(stats.average_latency_ms_30d)}<Sub>across all actions</Sub></StatTile>
            </div>

            <div className="flex flex-wrap items-start gap-4">
              <Card title="Events by action" subtitle="Count of audit events" className="flex-[1_1_420px]">
                <Bars data={stats.events_by_action_30d} />
              </Card>
              <Card title="Decompositions by model" subtitle="Provider and model" className="flex-[1_1_360px]">
                <Bars data={stats.model_usage_30d} />
              </Card>
              <Card title="Readiness checks" subtitle="From /api/readyz" className="flex-[1_1_280px]">
                <ul className="m-0 flex list-none flex-col p-0">
                  {Object.entries(stats.readiness.checks).map(([name, ok]) => (
                    <li key={name} className="flex items-center gap-2.5 py-2">
                      {ok
                        ? <CircleCheckIcon className="size-4 shrink-0 text-primary" aria-label="Passing" />
                        : <AlertTriangleIcon className="size-4 shrink-0 text-destructive-soft-foreground" aria-label="Failing" />}
                      <div className="min-w-0">
                        <div className="text-[13px] font-medium">{CHECK_LABELS[name] ?? name}</div>
                        <div className="font-mono text-[11px] text-muted-foreground">{name}</div>
                      </div>
                    </li>
                  ))}
                </ul>
              </Card>
            </div>
          </>
        )}

        {managesAccounts && accounts && (
          <Card
            title="Users"
            subtitle="Roles, blocks and sign-in of every account"
            action={<Button size="sm" onClick={() => setCreating(true)}><PlusIcon aria-hidden="true" />Create user</Button>}
          >
            <AccountsTable
              accounts={accounts}
              viewer={{ id: me?.id, role: me?.role, capabilities }}
              onChanged={load}
              onSignInNeeded={toLogin}
            />
          </Card>
        )}

        {allows(capabilities, 'export_research') && (
          <Card
            title="Decomposition records"
            subtitle="Every record, for analysis offline"
            action={
              <Button size="sm" variant="outline" disabled={exporting} onClick={() => void downloadRecords()}>
                <DownloadIcon aria-hidden="true" />{exporting ? 'Preparing…' : 'Download records'}
              </Button>
            }
          >
            <p className="m-0 max-w-[80ch] text-[13px] text-muted-foreground">
              A ZIP with summary.csv (one row per record), edits.csv (one row per change) and records.jsonld (every
              record in full, as linked data). It includes each person&apos;s name, username and email, so keep it as
              carefully as the user list. Read the CSV files with pandas or R; in Excel, use Data &gt; From Text/CSV.
            </p>
          </Card>
        )}

        {seesActivity && stats && (
          <Card title="Recent audit events" subtitle="Latest 100">
            <AuditTable events={events} />
          </Card>
        )}
      </main>

      <CreateUserDialog
        open={creating}
        onOpenChange={setCreating}
        onCreated={async () => {
          setCreating(false);
          await load();
        }}
      />
      <Toaster theme={theme} />
    </div>
  );
}

function StatTile({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2 rounded-[14px] border border-border bg-surface p-4">
      <span className="text-[13px] text-text-2">{label}</span>
      <div className="flex flex-col gap-2 text-[26px] leading-8 font-semibold">{children}</div>
    </div>
  );
}

function Sub({ children }: { children: React.ReactNode }) {
  return <span className="text-xs leading-4 font-normal text-muted-foreground">{children}</span>;
}

function Card({ title, subtitle, action, className, children }: { title: string; subtitle?: string; action?: React.ReactNode; className?: string; children: React.ReactNode }) {
  return (
    <section className={cn('min-w-0 overflow-hidden rounded-[14px] border border-border bg-surface', className)}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-[18px] py-3.5">
        <div>
          <h2 className="m-0 text-[15px] leading-[22px] font-semibold">{title}</h2>
          {subtitle && <p className="m-0 text-[13px] text-muted-foreground">{subtitle}</p>}
        </div>
        {action}
      </div>
      <div className="px-[18px] py-3">{children}</div>
    </section>
  );
}

/** Single-series horizontal bars: one hue, value at the tip, label in text colour. */
function Bars({ data }: { data: Record<string, number> }) {
  const rows = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...rows.map(([, value]) => value));
  const total = rows.reduce((sum, [, value]) => sum + value, 0);
  if (!rows.length) return <p className="m-0 py-2 text-[13px] text-muted-foreground">No events yet.</p>;
  return (
    <ul className="m-0 -mx-2 flex list-none flex-col gap-0.5 p-0">
      {rows.map(([name, value]) => (
        <li
          key={name}
          title={`${name}: ${formatNumber(value)} (${((value / Math.max(1, total)) * 100).toFixed(1)}% of all)`}
          className="grid h-8 grid-cols-[minmax(0,180px)_minmax(0,1fr)] items-center gap-3 rounded-lg px-2 hover:bg-surface-2"
        >
          <span className="truncate font-mono text-xs text-text-2">{name}</span>
          <span className="flex min-w-0 items-center gap-2">
            <span className="h-4 shrink-0 rounded-r-[4px] bg-series-1" style={{ width: `${Math.max(2, (value / max) * 82)}%` }} />
            <span className="text-xs font-semibold tabular-nums">{formatNumber(value)}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function CreateUserDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (open: boolean) => void; onCreated: () => Promise<void> }) {
  const empty = { username: '', password: '', display_name: '', email: '' };
  const [fields, setFields] = React.useState(empty);
  const [error, setError] = React.useState('');
  React.useEffect(() => {
    if (open) {
      setFields(empty);
      setError('');
    }
    // Reset when the dialog opens.
  }, [open]); // eslint-disable-line

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError('');
    try {
      await adminApi.createUser({
        username: fields.username.trim(),
        password: fields.password,
        display_name: fields.display_name.trim(),
        email: fields.email.trim(),
      });
      await onCreated();
    } catch (caught) {
      if (caught instanceof AuthRequiredError) return toLogin();
      setError(caught instanceof ApiError || caught instanceof Error ? caught.message : 'Could not create user.');
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <form onSubmit={submit} className="flex min-h-0 flex-col">
          <DialogHeader>
            <DialogTitle>Create user</DialogTitle>
            <DialogDescription>Creates a local account with a username and password. It is an ordinary user; the owner can make it an admin.</DialogDescription>
          </DialogHeader>
          <DialogBody className="grid grid-cols-[repeat(auto-fit,minmax(min(200px,100%),1fr))] gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="new-username">Username</Label>
              <Input id="new-username" required autoComplete="off" value={fields.username} onChange={(event) => setFields({ ...fields, username: event.target.value })} />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="new-display-name">Display name</Label>
              <Input id="new-display-name" value={fields.display_name} onChange={(event) => setFields({ ...fields, display_name: event.target.value })} />
            </div>
            {/* Password and confirmation sit side by side in the two-column grid. */}
            <NewPasswordFields
              idPrefix="new-password"
              password={fields.password}
              onPasswordChange={(password) => setFields({ ...fields, password })}
              passwordLabel="Password, at least 8 characters"
              confirmationLabel="Confirm password"
            />
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="new-email">Email</Label>
              <Input id="new-email" type="email" value={fields.email} onChange={(event) => setFields({ ...fields, email: event.target.value })} />
            </div>
            {error && <div className="col-span-full"><Callout tone="danger">{error}</Callout></div>}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit">Create</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function AuditTable({ events }: { events: AuditEvent[] }) {
  if (!events.length) return <p className="m-0 py-2 text-[13px] text-muted-foreground">No audit events yet.</p>;
  return (
    <div className="-mx-[18px] overflow-x-auto">
      <table className="w-full min-w-[860px] border-collapse text-left text-[13px]">
        <thead>
          <tr className="border-b border-border text-xs text-muted-foreground">
            <th scope="col" className="px-[18px] py-2 font-semibold">Time</th>
            <th scope="col" className="px-2 py-2 font-semibold">User</th>
            <th scope="col" className="px-2 py-2 font-semibold">Action</th>
            <th scope="col" className="px-2 py-2 font-semibold">Status</th>
            <th scope="col" className="px-2 py-2 text-right font-semibold">Latency</th>
            <th scope="col" className="px-[18px] py-2 font-semibold">Details</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => {
            const code = event.status_code ?? 0;
            const tone = code >= 500 ? 'danger' : code >= 400 ? 'warning' : 'success';
            return (
              <tr key={event.id} className="border-b border-border align-top last:border-b-0">
                <td className="px-[18px] py-2 whitespace-nowrap text-text-2">{formatTime(event.created_at)}</td>
                <td className="px-2 py-2">{event.username || '—'}</td>
                <td className="px-2 py-2 font-mono text-xs">{event.action}</td>
                <td className="px-2 py-2">{event.status_code ? <Pill tone={tone} className="font-mono">{event.status_code}</Pill> : '—'}</td>
                <td className="px-2 py-2 text-right whitespace-nowrap tabular-nums text-text-2">{event.latency_ms ? `${formatNumber(event.latency_ms)} ms` : ''}</td>
                <td className="px-[18px] py-2">
                  <details>
                    <summary className="cursor-pointer font-semibold text-link">View</summary>
                    <pre className="mt-2 max-h-64 max-w-[min(900px,80vw)] overflow-auto rounded-lg border border-border bg-surface-2 p-2.5 font-mono text-xs whitespace-pre-wrap">
                      {JSON.stringify({
                        path: event.path,
                        error: event.error,
                        metadata: event.metadata_json,
                        request_payload: event.request_payload,
                        response_payload: event.response_payload,
                      }, null, 2)}
                    </pre>
                  </details>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const element = document.getElementById('root');
if (element) {
  createRoot(element).render(
    <React.StrictMode>
      <ThemeProvider>
        <AdminPage />
      </ThemeProvider>
    </React.StrictMode>,
  );
}
