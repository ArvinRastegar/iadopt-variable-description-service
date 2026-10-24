import { readNdjson } from '@/lib/ndjson';

/*
 * Thin client for the backend API. Request bodies and paths are exactly the ones the previous
 * frontend sent, so the backend and its audit log see no difference.
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

export interface DecomposeRequest {
  definition: string;
  model_provider: string;
  model_name: string;
  disable_thinking: boolean;
}

export interface DecomposeResult {
  raw_llm_output: string;
  parsed_json: Record<string, unknown>;
  schema_valid: boolean;
  validation_errors: string[];
  enriched_json: Record<string, unknown>;
  ttl: string;
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

/** The signed-in user, or null for an anonymous visitor (401 is not an error here). */
export async function fetchCurrentUser(): Promise<User | null> {
  const response = await fetch(`${API_BASE}/auth/me`);
  if (response.status === 401) return null;
  if (!response.ok) throw new ApiError('Could not check your login.', response.status);
  const data = (await readBody(response)) as { user?: User };
  return data.user ?? null;
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

/* ------------------------------ Nanopublications -------------------------- */

export function publishNanopub(ttl: string, creatorOrcidUri: string): Promise<PublishResult> {
  return requestJson<PublishResult>(
    '/nanopub/publish',
    { method: 'POST', body: JSON.stringify({ ttl, creator_orcid_id: creatorOrcidUri }) },
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
};
