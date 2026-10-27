/**
 * Whether to show "The passwords do not match" under a confirmation field. While the person is
 * still typing, it only complains once the typed part already differs from the password; once
 * they left the field or tried to save, any difference counts.
 */
export function confirmationMismatch(password: string, confirmation: string, finished: boolean): boolean {
  if (!confirmation || confirmation === password) return false;
  return finished || !password.startsWith(confirmation);
}
