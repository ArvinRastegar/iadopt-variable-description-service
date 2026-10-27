import { expect, test } from 'vitest';

import { confirmationMismatch } from '../src/lib/passwords';

test('a confirmation only counts as wrong once it can no longer match', () => {
  // Nothing typed yet, or the same password: never a mismatch.
  expect(confirmationMismatch('correct-horse', '', true)).toBe(false);
  expect(confirmationMismatch('correct-horse', 'correct-horse', true)).toBe(false);
  // While typing, the start of the password is fine, and a different character is not.
  expect(confirmationMismatch('correct-horse', 'correct', false)).toBe(false);
  expect(confirmationMismatch('correct-horse', 'correcd', false)).toBe(true);
  expect(confirmationMismatch('correct-horse', 'correct-horse-battery', false)).toBe(true);
  // Once the field was left or saving was tried, an unfinished confirmation is wrong too.
  expect(confirmationMismatch('correct-horse', 'correct', true)).toBe(true);
});
