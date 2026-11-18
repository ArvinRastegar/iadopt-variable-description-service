import { readNdjson } from '@/lib/ndjson';
import { bareOrcid } from '@/lib/orcid';

/*
 * Thin client for the backend API. Request bodies and paths are the ones the previous frontend
 * sent, with these additions: the saved ORCID iD routes (`/auth/me/orcids`, D-035) and the
 * decomposition record of a signed-in account (D-037): the `/records` routes, `client` on
 * decompose and publish, and `record_id` on publish.
 */

export const API_BASE = '/api';

export interface User {
  id?: number;
  username: string;
  display_name?: string;
  email?: string;
  roles?: string[];
  is_active?: boolean;
  auth_provider?: string;
  last_login_at?: string;
}

export interface ProviderOptions {
  label: string;
  default_model_name: string;
  model_names: string[];
}

export interface ModelOptions {
  defaultProvider: string;
  providers: Record<string, ProviderOptions>;
}

export interface PreparationOptions {
  conforms_to_uri: string;
  created_with_label: string;
}

/** Device and screen of the browser, kept with each recorded step (D-037); the server adds IP and browser. */
export interface ClientInfo {
  screen?: { width?: number; height?: number; pixel_ratio?: number };
  device?: { platform?: string; mobile?: boolean; touch_points?: number };
}

export interface DecomposeRequest {
  definition: string;
  model_provider: string;
  model_name: string;
  disable_thinking: boolean;
  client?: ClientInfo;
}

export interface DecomposeResult {
  raw_llm_output: string;
  parsed_json: Record<string, unknown>;
  schema_valid: boolean;
  validation_errors: string[];
  enriched_json: Record<string, unknown>;
  ttl: string;
  /** The record this run started, whose version 0 is `ttl`; null when nothing is recorded. */
  record_id?: string | null;
}

/* ------------------------------ Records (D-037) ---------------------------- */

/** The kinds of change a recorded step can list (the backend's `RecordOperation`). */
export type RecordOperation =
  | 'add'
  | 'remove'
  | 'relabel'
  | 'relink'
  | 'change_shape'
  | 'add_constraint'
  | 'move_constraint'
  | 'confirm'
  | 'unconfirm'
  | 'other';

/** The I-ADOPT role of the component a change touches; empty when it has none (the backend's `RecordRole`). */
export type RecordRole =
  | ''
  | 'variable'
  | 'hasProperty'
  | 'hasObjectOfInterest'
  | 'hasMatrix'
  | 'hasContextObject'
  | 'hasStatisticalModifier'
  | 'hasConstraint';

/** One component change inside a recorded step. */
export interface RecordChange {
  operation: RecordOperation;
  role?: RecordRole;
  /** The component's IRI, or its label when it has no IRI. */
  component?: string;
  before?: string | null;
  after?: string | null;
}

export type RecordEventType = 'edit' | 'metadata' | 'check' | 'uncheck' | 'mark_best';

/** The body of `POST /api/records/{record_id}/events`; the backend checks that the fields fit the type. */
export interface RecordEventBody {
  type: RecordEventType;
  /** The version the step was taken on. */
  version: number;
  occurred_at?: string;
  /** The Turtle after an edit or metadata step; absent otherwise. */
  turtle?: string;
  changes?: RecordChange[];
  note?: string;
  unchecked_components?: number;
  client?: ClientInfo;
}

export interface RecordStarted {
  record_id: string;
  version: number;
}

export interface RecordEventResult {
  record_id: string;
  /** The step's number in the record. */
  event: number;
  /** The version the record is on after the step. */
  version: number;
}

export interface PublishResult {
  nanopub_url: string;
  published_to: string;
  variable_identifier: string;
  variable_uri: string;
}

export interface RetractResult {
  retraction_url: string;
  published_to: string;
  retracted_nanopub_url: string;
}

/** Raised when the backend answers 401: the visitor has to sign in first. */
export class AuthRequiredError extends Error {
  constructor(message = 'Sign in to continue. Your work is still on this page.') {
    super(message);
    this.name = 'AuthRequiredError';
  }
}

/** Raised for any other unsuccessful response, carrying the backend's `detail` message. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function detailOf(data: unknown, fallback: string): string {
  if (data && typeof data === 'object' && 'detail' in data) {
    const detail = (data as { detail: unknown }).detail;
    if (typeof detail === 'string' && detail) return detail;
    if (detail && typeof detail === 'object') return JSON.stringify(detail);
  }
  return fallback;
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text().catch(() => '');
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

/** POST/GET JSON and return the parsed body; 401 becomes AuthRequiredError. */
async function requestJson<T>(path: string, init: RequestInit = {}, fallbackError = 'Request failed.'): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init.headers ?? {}),
    },
  });
  if (response.status === 401) throw new AuthRequiredError();
  const data = await readBody(response);
  if (!response.ok) throw new ApiError(detailOf(data, fallbackError), response.status);
  return data as T;
}

/* ---------------------------------- Auth ---------------------------------- */

export interface Session {
  user: User | null;
  /** False when the deployment has authentication switched off; null when the answer did not say. */
  authEnabled: boolean | null;
}

/** The signed-in user and whether accounts are enforced; an anonymous visitor gets a null user. */
export async function fetchSession(): Promise<Session> {
  const response = await fetch(`${API_BASE}/auth/me`);
  if (response.status === 401) return { user: null, authEnabled: true };
  if (!response.ok) throw new ApiError('Could not check your login.', response.status);
  const data = (await readBody(response)) as { user?: User; auth_enabled?: unknown };
  return { user: data.user ?? null, authEnabled: typeof data.auth_enabled === 'boolean' ? data.auth_enabled : null };
}

/** The signed-in user, or null for an anonymous visitor (401 is not an error here). */
export async function fetchCurrentUser(): Promise<User | null> {
  return (await fetchSession()).user;
}

/**
 * Whether this session's work is recorded (D-037): a signed-in account with an id. The
 * development user of a deployment with authentication switched off has id 0 and is never
 * recorded, as the backend decides too.
 */
export function isRecordedAccount(user: User | null, authEnabled: boolean | null): boolean {
  return Boolean(user?.id) && authEnabled !== false;
}

export async function signIn(username: string, password: string): Promise<User> {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const data = (await readBody(response)) as { user?: User };
  if (!response.ok || !data.user) throw new ApiError(detailOf(data, 'Login failed.'), response.status);
  return data.user;
}

export async function signOut(): Promise<void> {
  const response = await fetch(`${API_BASE}/auth/logout`, { method: 'POST' });
  if (!response.ok && response.status !== 401) throw new ApiError('Could not sign out. Please try again.', response.status);
}

/** One ORCID iD saved to the signed-in account; `orcid` is the canonical https://orcid.org/ URI. */
export interface SavedOrcid {
  orcid: string;
  name: string | null;
  created_at: string;
  last_used_at: string;
}

/** The account's saved ORCID iDs, most recently used first. */
export async function fetchSavedOrcids(): Promise<SavedOrcid[]> {
  const data = await requestJson<{ orcids?: SavedOrcid[] }>('/auth/me/orcids', {}, 'Could not load your saved ORCID iDs.');
  return data.orcids ?? [];
}

/** Save an ORCID iD to the account, or mark a saved one as just used; returns the new list. */
export async function saveOrcid(orcidUri: string): Promise<SavedOrcid[]> {
  const data = await requestJson<{ orcids?: SavedOrcid[] }>(
    '/auth/me/orcids',
    { method: 'POST', body: JSON.stringify({ orcid: orcidUri }) },
    'Could not save the ORCID iD to your account.',
  );
  return data.orcids ?? [];
}

/** Remove a saved ORCID iD from the account; returns the new list. */
export async function removeSavedOrcid(orcidUri: string): Promise<SavedOrcid[]> {
  const data = await requestJson<{ orcids?: SavedOrcid[] }>(
    `/auth/me/orcids/${encodeURIComponent(bareOrcid(orcidUri))}`,
    { method: 'DELETE' },
    'Could not remove the ORCID iD from your account.',
  );
  return data.orcids ?? [];
}

/* ------------------------------ Configuration ----------------------------- */

const FALLBACK_PROVIDERS: Record<string, ProviderOptions> = {
  psnc: {
    label: 'PSNC',
    default_model_name: 'Qwen3.8-27B',
    model_names: ['Qwen3.8-27B'],
  },
};

/** Normalise `/api/model-options` the way the previous frontend did, with a safe fallback. */
export function normalizeModelOptions(data: unknown): ModelOptions {
  const raw = (data && typeof data === 'object' ? data : {}) as {
    providers?: Record<string, Partial<ProviderOptions>>;
    default_model_provider?: string;
    default_model_name?: string;
    model_names?: string[];
  };
  const rawProviders =
    raw.providers && typeof raw.providers === 'object'
      ? raw.providers
      : { psnc: { label: 'PSNC', default_model_name: raw.default_model_name, model_names: raw.model_names } };

  const providers: Record<string, ProviderOptions> = {};
  for (const [id, provider] of Object.entries(rawProviders)) {
    if (!id || !provider || typeof provider !== 'object') continue;
    const fallback = FALLBACK_PROVIDERS[id];
    const names = [...new Set((provider.model_names ?? fallback?.model_names ?? []).filter(Boolean))];
    if (!names.length) continue;
    const preferred = provider.default_model_name ?? fallback?.default_model_name ?? names[0];
    providers[id] = {
      label: provider.label ?? fallback?.label ?? id,
      default_model_name: names.includes(preferred) ? preferred : names[0],
      model_names: names,
    };
  }

  const finalProviders = Object.keys(providers).length ? providers : FALLBACK_PROVIDERS;
  const ids = Object.keys(finalProviders);
  const defaultProvider = raw.default_model_provider && ids.includes(raw.default_model_provider)
    ? raw.default_model_provider
    : ids[0];
  return { defaultProvider, providers: finalProviders };
}

export async function fetchModelOptions(): Promise<ModelOptions> {
  try {
    const response = await fetch(`${API_BASE}/model-options`);
    const data = await readBody(response);
    if (!response.ok) throw new Error(detailOf(data, 'Could not load model options.'));
    return normalizeModelOptions(data);
  } catch (error) {
    console.error(error);
    // Keep the composer usable even if the configuration endpoint is unavailable.
    return normalizeModelOptions({});
  }
}

export async function fetchPreparationOptions(): Promise<PreparationOptions> {
  const response = await fetch(`${API_BASE}/nanopub/preparation-options`);
  const data = await readBody(response);
  if (!response.ok) throw new Error(detailOf(data, 'Could not load nanopublication preparation settings.'));
  return data as PreparationOptions;
}

/* ------------------------------ Decomposition ----------------------------- */

interface StreamEvent {
  type?: string;
  delta?: string;
  detail?: string;
  data?: DecomposeResult;
}

/**
 * Run `/api/decompose/stream`, calling `onDelta` for every raw output chunk, and resolve with
 * the final payload. A 401 before the stream starts becomes AuthRequiredError.
 */
export async function streamDecomposition(
  body: DecomposeRequest,
  handlers: { onStart?: () => void; onDelta: (delta: string) => void },
): Promise<DecomposeResult> {
  const response = await fetch(`${API_BASE}/decompose/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (response.status === 401) throw new AuthRequiredError();
  if (!response.ok) {
    const data = await readBody(response);
    throw new ApiError(detailOf(data, 'Backend request failed.'), response.status);
  }
  if (!response.body) throw new Error('The backend did not return a readable stream.');
  // Only now replace the previous result, so an expired session never wipes existing work.
  handlers.onStart?.();
  const onDelta = handlers.onDelta;

  let finalPayload: DecomposeResult | null = null;
  for await (const value of readNdjson(response.body)) {
    const event = (value ?? {}) as StreamEvent;
    if (event.type === 'raw_delta') onDelta(event.delta ?? '');
    else if (event.type === 'error') throw new Error(event.detail || 'Streaming backend error.');
    else if (event.type === 'final') finalPayload = event.data ?? null;
  }
  if (!finalPayload) throw new Error('The backend stream ended before returning the final decomposition payload.');
  return finalPayload;
}

/* --------------------------------- Records -------------------------------- */

/** Start a record from Turtle the person opened or pasted; it becomes version 0. */
export function startRecord(body: { turtle: string; occurred_at?: string; client?: ClientInfo }): Promise<RecordStarted> {
  return requestJson<RecordStarted>(
    '/records',
    { method: 'POST', body: JSON.stringify(body) },
    'Could not start the record of this work.',
  );
}

/** Add one step to the signed-in account's own record. */
export function postRecordEvent(recordId: string, body: RecordEventBody): Promise<RecordEventResult> {
  return requestJson<RecordEventResult>(
    `/records/${encodeURIComponent(recordId)}/events`,
    { method: 'POST', body: JSON.stringify(body) },
    'Could not record this step.',
  );
}

/* ------------------------------ Nanopublications -------------------------- */

/**
 * Publish the visible Turtle. Publishing needs the record whose version was marked as best
 * (D-037); the backend answers 409 without an account, without `record_id` or when the Turtle
 * is not the marked version.
 */
export function publishNanopub(
  ttl: string,
  creatorOrcidUri: string,
  record: { recordId: string | null; client?: ClientInfo } = { recordId: null },
): Promise<PublishResult> {
  return requestJson<PublishResult>(
    '/nanopub/publish',
    {
      method: 'POST',
      body: JSON.stringify({
        ttl,
        creator_orcid_id: creatorOrcidUri,
        ...(record.recordId ? { record_id: record.recordId } : {}),
        ...(record.client ? { client: record.client } : {}),
      }),
    },
    'Nanopub publish failed.',
  );
}

export function retractNanopub(nanopubUri: string, creatorOrcidUri: string): Promise<RetractResult> {
  return requestJson<RetractResult>(
    '/nanopub/retract',
    { method: 'POST', body: JSON.stringify({ nanopub_uri: nanopubUri, creator_orcid_id: creatorOrcidUri }) },
    'Nanopub retract failed.',
  );
}

/* --------------------------------- Events --------------------------------- */

export function postEvent(action: string, payload: Record<string, unknown> = {}, metadata: Record<string, unknown> = {}) {
  return fetch(`${API_BASE}/events`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, payload, metadata }),
  });
}

/* ---------------------------------- Admin --------------------------------- */

export interface AuditEvent {
  id: number;
  created_at: string;
  user_id: number | null;
  username: string;
  action: string;
  method: string;
  path: string;
  status_code: number | null;
  latency_ms: number | null;
  ip_address: string;
  user_agent: string;
  request_payload: string | null;
  response_payload: string | null;
  metadata: string | null;
  metadata_json: Record<string, unknown>;
  error: string | null;
}

export interface AdminStats {
  auth_enabled: boolean;
  total_users: number;
  active_users: number;
  active_usernames_30d: string[];
  event_count_30d: number;
  failures_30d: number;
  average_latency_ms_30d: number;
  events_by_action_30d: Record<string, number>;
  model_usage_30d: Record<string, number>;
  readiness: { status: string; checks: Record<string, boolean> };
}

export const adminApi = {
  stats: () => requestJson<AdminStats>('/admin/stats'),
  users: () => requestJson<{ users: User[] }>('/admin/users'),
  audit: (limit = 100) => requestJson<{ events: AuditEvent[] }>(`/admin/audit?limit=${limit}`),
  createUser: (body: { username: string; password: string; display_name: string; email: string; roles: string[]; is_active: boolean }) =>
    requestJson<{ user: User }>('/admin/users', { method: 'POST', body: JSON.stringify(body) }),
  updateUser: (id: number, updates: Record<string, unknown>) =>
    requestJson<{ user: User }>(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify(updates) }),
  /** Every decomposition record as a ZIP with summary.csv, edits.csv and records.jsonld (D-041), named as the server names it. */
  exportRecords: async (): Promise<{ blob: Blob; fileName: string }> => {
    const response = await fetch(`${API_BASE}/admin/records/export`);
    if (response.status === 401) throw new AuthRequiredError();
    if (!response.ok) throw new ApiError(detailOf(await readBody(response), 'Could not export the records.'), response.status);
    const fileName = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') ?? '')?.[1] ?? 'iadopt-records.zip';
    return { blob: await response.blob(), fileName };
  },
};
