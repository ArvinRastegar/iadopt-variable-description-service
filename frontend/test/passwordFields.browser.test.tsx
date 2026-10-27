import * as React from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, expect, test } from 'vitest';
import { page, userEvent } from 'vitest/browser';

import { NewPasswordFields } from '../src/admin/NewPasswordFields';
import { Label, PasswordInput } from '../src/components/ui/fields';

/*
 * Password fields: every one has an eye button that shows and hides the characters, and the
 * admin's new-password fields ask for the password twice, say when the two differ, and keep
 * the form from being submitted until they match.
 */

let root: Root | null = null;
let container: HTMLElement | null = null;

afterEach(() => {
  root?.unmount();
  container?.remove();
  root = null;
  container = null;
});

function mount(element: React.ReactElement) {
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  root.render(element);
}

test('the eye button shows and hides a password and is never taken for its label', async () => {
  mount(
    <div>
      <Label htmlFor="password">Password</Label>
      <PasswordInput id="password" defaultValue="local-test-password" />
    </div>,
  );
  // One match only: the button's name is text, not an aria-label, so it is no label of the field.
  const field = page.getByLabelText('Password');
  await expect.element(field).toHaveAttribute('type', 'password');
  await expect.element(field).toHaveAttribute('spellcheck', 'false');

  await page.getByRole('button', { name: 'Show password' }).click();
  await expect.element(field).toHaveAttribute('type', 'text');
  await expect.element(field).toHaveValue('local-test-password');

  await page.getByRole('button', { name: 'Hide password' }).click();
  await expect.element(field).toHaveAttribute('type', 'password');
});

/** A form like the Create user dialog: its submit handler does no checking of its own. */
function NewPassword({ saved }: { saved: string[] }) {
  const [password, setPassword] = React.useState('');
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        saved.push(password);
      }}
    >
      <NewPasswordFields
        idPrefix="new-password"
        password={password}
        onPasswordChange={setPassword}
        passwordLabel="Password, at least 8 characters"
        confirmationLabel="Confirm password"
      />
      <button type="submit">Create</button>
    </form>
  );
}

test('the confirmation says when the passwords differ and blocks saving until they match', async () => {
  const saved: string[] = [];
  mount(<NewPassword saved={saved} />);
  const password = page.getByLabelText('Password, at least 8 characters');
  const confirmation = page.getByLabelText('Confirm password');
  const mismatch = page.getByText('The passwords do not match.');

  await expect.element(password).toBeVisible();
  expect(page.getByRole('button', { name: 'Show password' }).elements()).toHaveLength(2);

  await userEvent.fill(password, 'correct-horse');
  // Typing the start of the password is fine; a different character is not.
  await userEvent.fill(confirmation, 'correct');
  await expect.element(mismatch).not.toBeInTheDocument();
  await userEvent.fill(confirmation, 'correcd');
  await expect.element(mismatch).toBeVisible();
  await expect.element(confirmation).toHaveAttribute('aria-invalid', 'true');

  // An unfinished confirmation keeps the form from submitting, and then the message shows.
  await userEvent.fill(confirmation, 'correct');
  await expect.element(mismatch).not.toBeInTheDocument();
  await page.getByRole('button', { name: 'Create' }).click();
  await expect.element(mismatch).toBeVisible();
  expect(saved).toEqual([]);

  // Changing the password after a match counts as a mismatch too.
  await userEvent.fill(confirmation, 'correct-horse');
  await userEvent.fill(password, 'correct-horses');
  await page.getByRole('button', { name: 'Create' }).click();
  expect(saved).toEqual([]);

  await userEvent.fill(password, 'correct-horse');
  await expect.element(mismatch).not.toBeInTheDocument();
  await page.getByRole('button', { name: 'Create' }).click();
  await expect.poll(() => saved).toEqual(['correct-horse']);
});

test('pressing Enter in an unfinished confirmation shows the mismatch without leaving the field', async () => {
  const saved: string[] = [];
  mount(<NewPassword saved={saved} />);
  const confirmation = page.getByLabelText('Confirm password');

  await userEvent.fill(page.getByLabelText('Password, at least 8 characters'), 'correct-horse');
  await userEvent.fill(confirmation, 'correct');
  await userEvent.keyboard('{Enter}');

  // The field never lost focus, so only the refused submission can have shown the message.
  await expect.element(confirmation).toHaveFocus();
  await expect.element(page.getByText('The passwords do not match.')).toBeVisible();
  expect(saved).toEqual([]);
});
