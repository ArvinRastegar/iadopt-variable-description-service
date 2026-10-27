import * as React from 'react';

import { Label, PasswordInput } from '@/components/ui/fields';
import { confirmationMismatch } from '@/lib/passwords';

/*
 * A new password and its confirmation, for the Create user and Reset password dialogs. Each
 * field has its own eye button. While the two entries differ the confirmation is invalid, so
 * the browser refuses to submit the form, exactly as it does for an empty required field.
 */

const MISMATCH = 'The passwords do not match.';

export function NewPasswordFields({
  idPrefix,
  password,
  onPasswordChange,
  passwordLabel,
  confirmationLabel,
}: {
  /** Id of the password field; the confirmation gets `${idPrefix}-confirm`. */
  idPrefix: string;
  password: string;
  onPasswordChange: (value: string) => void;
  passwordLabel: string;
  confirmationLabel: string;
}) {
  const [confirmation, setConfirmation] = React.useState('');
  // True once the field was left or saving was tried.
  const [finished, setFinished] = React.useState(false);
  const confirmationRef = React.useRef<HTMLInputElement>(null);
  const confirmationId = `${idPrefix}-confirm`;
  const mismatch = confirmationMismatch(password, confirmation, finished);
  // An empty confirmation is left to `required`.
  const differs = confirmation !== '' && confirmation !== password;

  React.useEffect(() => {
    confirmationRef.current?.setCustomValidity(differs ? MISMATCH : '');
  }, [differs]);

  return (
    <>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={idPrefix}>{passwordLabel}</Label>
        <PasswordInput
          id={idPrefix}
          autoComplete="new-password"
          minLength={8}
          required
          value={password}
          onChange={(event) => onPasswordChange(event.target.value)}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={confirmationId}>{confirmationLabel}</Label>
        <PasswordInput
          ref={confirmationRef}
          id={confirmationId}
          autoComplete="new-password"
          required
          value={confirmation}
          aria-invalid={mismatch}
          aria-describedby={`${confirmationId}-message`}
          onChange={(event) => setConfirmation(event.target.value)}
          onBlur={() => setFinished(true)}
          onInvalid={() => setFinished(true)}
        />
        <span id={`${confirmationId}-message`} role="alert" className="min-h-[18px] text-xs leading-[18px] text-destructive-soft-foreground">
          {mismatch ? MISMATCH : ''}
        </span>
      </div>
    </>
  );
}
