import * as React from 'react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Callout, Pill } from '@/components/ui/feedback';
import { Input, Label, Textarea } from '@/components/ui/fields';
import { adminApi, allows, AuthRequiredError, type Account, type AccountChange, type AccountChangeRoute, type Capabilities, type Role } from '@/shared/api';
import { displayName } from '@/shared/auth';
import { NewPasswordFields } from './NewPasswordFields';

/*
 * The accounts of the admin page (accounts-roles, section 10): role, block state, approval, last
 * login and how each account signs in, with the actions the rules allow the viewer. Accounts that
 * wait for an approval come first. The backend decides every action again; its refusal is shown as
 * the error.
 */

/** The longest reason the backend keeps. */
const MAX_REASON_CHARS = 500;

const ROLE_LABELS: Record<Role, string> = { owner: 'Owner', admin: 'Admin', user: 'User' };

const CHANGE_LABELS: Record<string, string> = {
  admin_granted: 'Made admin',
  admin_revoked: 'Admin removed',
  blocked: 'Blocked',
  unblocked: 'Unblocked',
  approved: 'Approved',
  approval_withdrawn: 'Approval withdrawn',
  owner_bound: 'Became the owner',
  owner_rebound: 'Became the owner, replacing the previous one',
  identity_linked: 'Identity linked',
  account_folded: 'Account folded',
  password_purged: 'Password removed',
};

/** The role and block changes that came with another change, such as admin removed by an offline link. */
function changesWith({ details }: AccountChange): string[] {
  return [
    details.was_blocked === true && CHANGE_LABELS.unblocked,
    details.was_admin === false && CHANGE_LABELS.admin_granted,
    details.admin_removed === true && CHANGE_LABELS.admin_revoked,
    details.blocked === true && CHANGE_LABELS.blocked,
  ].filter((label): label is string => Boolean(label));
}

type Action = 'approve' | 'withdraw-approval' | 'block' | 'unblock' | 'make-admin' | 'remove-admin' | 'edit' | 'reset' | 'history';

const ACTION_LABELS: Record<Action, string> = {
  approve: 'Approve',
  'withdraw-approval': 'Withdraw approval',
  block: 'Block',
  unblock: 'Unblock',
  'make-admin': 'Make admin',
  'remove-admin': 'Remove admin',
  edit: 'Edit',
  reset: 'Reset password',
  history: 'History',
};

/** The signed-in account that looks at the page, with what the backend allows it. */
export interface Viewer {
  id?: number;
  role?: Role;
  capabilities: Capabilities;
}

/** The accounts that wait for an approval, the oldest first; a blocked one does not wait. */
export function waitingAccounts(accounts: Account[]): Account[] {
  return accounts
    .filter((account) => account.approval === 'waiting' && !account.blocked)
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
}

/** The actions section 4 of accounts-roles allows the viewer on an account. */
function actionsFor(account: Account, viewer: Viewer): Action[] {
  const actions: Action[] = [];
  const isOwner = account.role === 'owner';
  const isSelf = account.id === viewer.id;
  const byOwner = viewer.role === 'owner';
  const ordinary = account.role === 'user';
  const actsOn = !isOwner && !isSelf && (byOwner || ordinary);
  if (allows(viewer.capabilities, 'review_applications') && actsOn) {
    if (account.approval === 'waiting') actions.push('approve');
    if (account.approval === 'approved') actions.push('withdraw-approval');
  }
  if (allows(viewer.capabilities, 'manage_users') && actsOn) {
    actions.push(account.blocked ? 'unblock' : 'block');
  }
  if (allows(viewer.capabilities, 'manage_admins') && !isOwner) {
    if (account.role === 'admin') actions.push('remove-admin');
    else if (!account.blocked) actions.push('make-admin');
  }
  if (allows(viewer.capabilities, 'manage_users')) {
    if (account.local && (byOwner || isSelf || ordinary)) actions.push('edit', 'reset');
    actions.push('history');
  }
  return actions;
}

export function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

/** The identity provider's host, such as `login.rohub.org`. */
function issuerName(issuer: string): string {
  try {
    return new URL(issuer).hostname;
  } catch {
    return issuer;
  }
}

interface OpenDialog {
  /** Set apart from any earlier dialog, so that a slow request never closes a newer one. */
  serial: number;
  action: Action;
  account: Account;
  open: boolean;
}

export function AccountsTable({ accounts, viewer, onChanged, onSignInNeeded }: {
  accounts: Account[];
  viewer: Viewer;
  /** Load the accounts again after a change. */
  onChanged: () => Promise<void>;
  onSignInNeeded: () => void;
}) {
  const [dialog, setDialog] = React.useState<OpenDialog | null>(null);
  const close = React.useCallback((serial: number) => {
    setDialog((current) => (current?.serial === serial ? { ...current, open: false } : current));
  }, []);
  const actions = (account: Account) => actionsFor(account, viewer).map((action) => (
    <Button
      key={action}
      size="sm"
      variant={action === 'approve' ? 'default' : 'outline'}
      aria-label={`${ACTION_LABELS[action]} ${account.username}`}
      onClick={() => setDialog((current) => ({ serial: (current?.serial ?? 0) + 1, action, account, open: true }))}
    >
      {ACTION_LABELS[action]}
    </Button>
  ));

  if (!accounts.length) return <p className="m-0 py-2 text-[13px] text-muted-foreground">No accounts yet.</p>;
  const waiting = waitingAccounts(accounts);
  return (
    <div className="flex flex-col gap-3">
      {waiting.length > 0 && (
        <section id="waiting-for-approval" aria-labelledby="waiting-heading" className="flex scroll-mt-20 flex-col gap-2">
          <h3 id="waiting-heading" className="m-0 text-[13px] leading-5 font-semibold">Waiting for approval ({waiting.length})</h3>
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {waiting.map((account) => (
              <li key={account.id} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-lg border border-warning-line bg-warning-soft px-3 py-2.5 text-[13px]">
                <div className="min-w-0 break-words">
                  <AccountName account={account} />
                  <div className="flex flex-wrap gap-x-2 text-xs text-muted-foreground">
                    <SignsInWith account={account} />
                    <span>created {formatTime(account.created_at)}</span>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5">{actions(account)}</div>
              </li>
            ))}
          </ul>
        </section>
      )}
      <AccountRows accounts={accounts.filter((account) => !waiting.includes(account))} actions={actions} />
      {dialog && (
        <AccountDialog
          dialog={dialog}
          onClose={() => close(dialog.serial)}
          onChanged={onChanged}
          onSignInNeeded={onSignInNeeded}
        />
      )}
    </div>
  );
}

function AccountName({ account }: { account: Account }) {
  return (
    <>
      <div className="font-medium">{displayName(account)}</div>
      <div className="flex flex-wrap gap-x-2 text-xs text-muted-foreground">
        <span className="font-mono">{account.username}</span>
        {account.email && <span>{account.email}</span>}
      </div>
    </>
  );
}

function SignsInWith({ account }: { account: Account }) {
  return (
    <>
      {account.local && <div>Password</div>}
      {account.identities.map((identity) => (
        <div key={`${identity.issuer} ${identity.subject}`} title={identity.issuer}>{issuerName(identity.issuer)}</div>
      ))}
      {!account.local && !account.identities.length && '—'}
    </>
  );
}

function Status({ account }: { account: Account }) {
  return (
    <div className="flex flex-col items-start gap-1">
      {account.blocked ? <Pill tone="danger" dot>Blocked</Pill> : <Pill tone="success" dot>Active</Pill>}
      {account.approval === 'waiting' && <span className="text-xs text-muted-foreground">Not approved</span>}
      {account.approval === 'approved' && (
        <span className="text-xs text-muted-foreground">
          Approved{account.approved_by && ` by ${account.approved_by}`}{account.approved_at && `, ${formatTime(account.approved_at)}`}
        </span>
      )}
    </div>
  );
}

function AccountRows({ accounts, actions }: { accounts: Account[]; actions: (account: Account) => React.ReactNode }) {
  return (
    <div className="-mx-[18px] overflow-x-auto">
      <table className="w-full min-w-[900px] border-collapse text-left text-[13px]">
        <thead>
          <tr className="border-b border-border text-xs text-muted-foreground">
            <th scope="col" className="px-[18px] py-2 font-semibold">Account</th>
            <th scope="col" className="px-2 py-2 font-semibold">Role</th>
            <th scope="col" className="px-2 py-2 font-semibold">Status</th>
            <th scope="col" className="px-2 py-2 font-semibold">Signs in with</th>
            <th scope="col" className="px-2 py-2 font-semibold">Last login</th>
            <th scope="col" className="px-[18px] py-2 text-right font-semibold">Actions</th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((account) => (
            <tr key={account.id} className="border-b border-border last:border-b-0">
              <td className="px-[18px] py-2"><AccountName account={account} /></td>
              <td className="px-2 py-2"><Pill tone={account.role === 'user' ? 'neutral' : 'info'}>{ROLE_LABELS[account.role] ?? account.role}</Pill></td>
              <td className="px-2 py-2"><Status account={account} /></td>
              <td className="px-2 py-2 text-text-2"><SignsInWith account={account} /></td>
              <td className="px-2 py-2 whitespace-nowrap text-text-2">{account.last_login_at ? formatTime(account.last_login_at) : '—'}</td>
              <td className="px-[18px] py-2">
                <div className="flex flex-wrap justify-end gap-1.5">{actions(account)}</div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The one dialog of the accounts table: its content follows the action, and a newer action replaces it. */
function AccountDialog({ dialog, onClose, onChanged, onSignInNeeded }: {
  dialog: OpenDialog;
  onClose: () => void;
  onChanged: () => Promise<void>;
  onSignInNeeded: () => void;
}) {
  const { account, action } = dialog;
  const shared = {
    account,
    onCancel: onClose,
    onSignInNeeded,
    onDone: () => {
      onClose();
      void onChanged();
    },
  };
  return (
    <Dialog open={dialog.open} onOpenChange={(open) => !open && onClose()}>
      <DialogContent key={dialog.serial} className="max-w-md">
        {action === 'edit' && <EditForm {...shared} />}
        {action === 'reset' && <ResetPasswordForm {...shared} />}
        {action === 'history' && <History {...shared} />}
        {action === 'approve' && (
          <ChangeForm
            {...shared}
            change="approve"
            title={`Approve ${account.username}?`}
            description={`They can then decompose, save their work, publish, and retract any publication of this service, like a local account. ${account.blocked ? 'They stay blocked until you unblock them.' : 'It takes effect at once.'}`}
            button={ACTION_LABELS.approve}
            label="Note"
            placeholder="For example: accepted the terms by email on 25 November"
          />
        )}
        {action === 'withdraw-approval' && (
          <ChangeForm
            {...shared}
            change="withdraw-approval"
            title={`Withdraw the approval of ${account.username}?`}
            description={account.role !== 'user'
              ? 'An admin needs no approval: nothing changes as long as they are one.'
              : `What the approval allowed ends at once. ${account.blocked ? 'Their data stays.' : 'They stay signed in and their data stays.'}`}
            button={ACTION_LABELS['withdraw-approval']}
            label="Note"
          />
        )}
        {action === 'block' && (
          <ChangeForm
            {...shared}
            change="block"
            title={`Block ${account.username}?`}
            description="Their sessions end at once and they cannot sign in until they are unblocked. Their data stays."
            button={ACTION_LABELS.block}
            reasonNeeded
          />
        )}
        {action === 'unblock' && (
          <ChangeForm
            {...shared}
            change="unblock"
            title={`Unblock ${account.username}?`}
            description="They can sign in again, with their data as it was."
            button={ACTION_LABELS.unblock}
          />
        )}
        {action === 'make-admin' && (
          <ChangeForm
            {...shared}
            change="grant-admin"
            title={`Make ${account.username} an admin?`}
            description="Admins manage accounts, see activity and download every record. It takes effect at once."
            button={ACTION_LABELS['make-admin']}
          />
        )}
        {action === 'remove-admin' && (
          <ChangeForm
            {...shared}
            change="revoke-admin"
            title={`Remove admin from ${account.username}?`}
            description={`${account.blocked ? 'They become an ordinary user' : 'They stay signed in as an ordinary user'}${account.approval === 'not_needed' && account.identities.length > 0 ? ', who waits for approval.' : '.'}`}
            button={ACTION_LABELS['remove-admin']}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

interface FormProps {
  account: Account;
  onCancel: () => void;
  onDone: () => void;
  onSignInNeeded: () => void;
}

/** Run one change: `busy` while it runs, `error` with the backend's reason when it is refused. */
function useChange({ onDone, onSignInNeeded }: Pick<FormProps, 'onDone' | 'onSignInNeeded'>, fallback: string) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  async function run(request: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await request();
      onDone();
    } catch (caught) {
      if (caught instanceof AuthRequiredError) return onSignInNeeded();
      setError(messageOf(caught, fallback));
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, run };
}

/** A role, block or approval change, with the reason or note it is recorded with. */
function ChangeForm({ account, onCancel, onDone, onSignInNeeded, change, title, description, button, reasonNeeded = false, label = 'Reason', placeholder }: FormProps & {
  change: AccountChangeRoute;
  title: string;
  description: string;
  button: string;
  reasonNeeded?: boolean;
  label?: string;
  placeholder?: string;
}) {
  const [reason, setReason] = React.useState('');
  const { busy, error, run } = useChange({ onDone, onSignInNeeded }, 'Could not change the account.');
  const ready = !reasonNeeded || reason.trim() !== '';
  return (
    <form
      className="flex min-h-0 flex-col"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready && !busy) void run(() => adminApi.changeUser(account.id, change, reason.trim()));
      }}
    >
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="account-change-reason">
            {label} {!reasonNeeded && <span className="font-normal text-muted-foreground">(optional)</span>}
          </Label>
          <Textarea
            id="account-change-reason"
            rows={3}
            maxLength={MAX_REASON_CHARS}
            required={reasonNeeded}
            placeholder={placeholder}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
          <p className="m-0 text-xs text-muted-foreground">Kept in the account&apos;s history.</p>
        </div>
        {error && <Callout tone="danger">{error}</Callout>}
      </DialogBody>
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant={change === 'block' ? 'destructive' : 'default'} disabled={!ready || busy}>{button}</Button>
      </DialogFooter>
    </form>
  );
}

/** Username, display name and email of a local account; nothing else is sent. */
function EditForm({ account, onCancel, onDone, onSignInNeeded }: FormProps) {
  const initial = { username: account.username, display_name: account.display_name ?? '', email: account.email ?? '' };
  const [fields, setFields] = React.useState(initial);
  const { busy, error, run } = useChange({ onDone, onSignInNeeded }, 'Could not update the account.');
  const dirty = JSON.stringify(fields) !== JSON.stringify(initial);
  return (
    <form
      className="flex min-h-0 flex-col"
      onSubmit={(event) => {
        event.preventDefault();
        if (dirty && !busy) void run(() => adminApi.updateUser(account.id, fields));
      }}
    >
      <DialogHeader>
        <DialogTitle>Edit {account.username}</DialogTitle>
        <DialogDescription>The sign-in name and the profile of a local account.</DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="edit-username">Username</Label>
          <Input id="edit-username" required maxLength={120} autoComplete="off" value={fields.username} onChange={(event) => setFields({ ...fields, username: event.target.value })} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="edit-display-name">Display name</Label>
          <Input id="edit-display-name" value={fields.display_name} onChange={(event) => setFields({ ...fields, display_name: event.target.value })} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="edit-email">Email</Label>
          <Input id="edit-email" type="email" value={fields.email} onChange={(event) => setFields({ ...fields, email: event.target.value })} />
        </div>
        {error && <Callout tone="danger">{error}</Callout>}
      </DialogBody>
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={!dirty || busy}>Save</Button>
      </DialogFooter>
    </form>
  );
}

function ResetPasswordForm({ account, onCancel, onDone, onSignInNeeded }: FormProps) {
  const [password, setPassword] = React.useState('');
  const { busy, error, run } = useChange({ onDone, onSignInNeeded }, 'Could not set the password.');
  return (
    <form
      className="flex min-h-0 flex-col"
      onSubmit={(event) => {
        event.preventDefault();
        if (!busy) void run(() => adminApi.updateUser(account.id, { password }));
      }}
    >
      <DialogHeader>
        <DialogTitle>Reset password</DialogTitle>
        <DialogDescription>Set a new password for {account.username}. Tell them through a secure channel.</DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-4">
        <NewPasswordFields
          idPrefix="reset-password"
          password={password}
          onPasswordChange={setPassword}
          passwordLabel="New password, at least 8 characters"
          confirmationLabel="Confirm the new password"
        />
        {error && <Callout tone="danger">{error}</Callout>}
      </DialogBody>
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={password.length < 8 || busy}>Set password</Button>
      </DialogFooter>
    </form>
  );
}

/** Role, block, approval and identity changes of an account, newest first. */
function History({ account, onCancel, onSignInNeeded }: FormProps) {
  const [history, setHistory] = React.useState<AccountChange[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let left = false;
    adminApi.userHistory(account.id)
      .then((answer) => !left && setHistory(answer.history))
      .catch((caught) => {
        if (left) return;
        if (caught instanceof AuthRequiredError) onSignInNeeded();
        else setError(messageOf(caught, 'Could not load the history.'));
      });
    return () => {
      left = true;
    };
  }, [account.id, onSignInNeeded]);

  return (
    <div className="flex min-h-0 flex-col">
      <DialogHeader>
        <DialogTitle>History of {account.username}</DialogTitle>
        <DialogDescription>Role, block, approval and identity changes, newest first.</DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-4">
        {error && <Callout tone="danger">{error}</Callout>}
        {!error && history === null && <p className="m-0 text-[13px] text-muted-foreground">Loading…</p>}
        {history?.length === 0 && <p className="m-0 text-[13px] text-muted-foreground">No changes yet.</p>}
        {history && history.length > 0 && (
          <ul className="m-0 flex list-none flex-col p-0">
            {history.map((change, index) => (
              <li key={`${change.created_at} ${index}`} className="flex flex-col gap-0.5 border-b border-border py-2.5 last:border-b-0">
                <span className="text-[13px] font-medium">{[CHANGE_LABELS[change.action] ?? change.action, ...changesWith(change)].join(' · ')}</span>
                {change.reason && <span className="text-[13px] text-text-2">{change.reason}</span>}
                <span className="text-xs text-muted-foreground">{`${formatTime(change.created_at)} by ${change.actor}`}</span>
              </li>
            ))}
          </ul>
        )}
      </DialogBody>
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onCancel}>Close</Button>
      </DialogFooter>
    </div>
  );
}
