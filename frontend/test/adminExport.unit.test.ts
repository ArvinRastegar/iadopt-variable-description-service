import { afterEach, describe, expect, test, vi } from 'vitest';

import { adminApi, ApiError, AuthRequiredError } from '../src/shared/api';

/*
 * The admin download of every decomposition record (D-040): the file keeps the name the server
 * gives it, and a refusal becomes an error the admin page can show or a sign-in prompt.
 */

afterEach(() => {
  vi.unstubAllGlobals();
});

function respond(body: BodyInit | null, init: ResponseInit) {
  const fetchMock = vi.fn(async () => new Response(body, init));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('adminApi.exportRecords', () => {
  test('returns the ZIP under the file name the server gives it', async () => {
    const fetchMock = respond(new Blob(['PK']), {
      status: 200,
      headers: { 'Content-Type': 'application/zip', 'Content-Disposition': 'attachment; filename="iadopt-records-20261118T101500Z.zip"' },
    });
    const { blob, fileName } = await adminApi.exportRecords();
    expect(fetchMock).toHaveBeenCalledWith('/api/admin/records/export');
    expect(fileName).toBe('iadopt-records-20261118T101500Z.zip');
    expect(await blob.text()).toBe('PK');
  });

  test('falls back to a plain name when the server gives none', async () => {
    respond(new Blob(['PK']), { status: 200, headers: { 'Content-Type': 'application/zip' } });
    expect((await adminApi.exportRecords()).fileName).toBe('iadopt-records.zip');
  });

  test('a lost session asks for a new sign-in', async () => {
    respond(null, { status: 401 });
    await expect(adminApi.exportRecords()).rejects.toBeInstanceOf(AuthRequiredError);
  });

  test('a refusal keeps the server reason', async () => {
    respond(JSON.stringify({ detail: 'Admin access required.' }), { status: 403, headers: { 'Content-Type': 'application/json' } });
    const error = await adminApi.exportRecords().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ message: 'Admin access required.', status: 403 });
  });
});
