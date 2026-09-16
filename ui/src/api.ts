import type {
  ApiFailure,
  DbsResponse,
  FreshnessMode,
  GraphSample,
  HealthResponse,
  QueryResponse,
  SchemaDocument,
  SearchResponse,
  MemoryList, ContextResponse, MutationRequest, MutationResponse, IndexStatus, IndexObservation, JobSummary,
} from './types';

export class ApiError extends Error implements ApiFailure {
  hint: string | null;
  status: number;

  constructor(message: string, hint: string | null, status: number) {
    super(message);
    this.hint = hint;
    this.status = status;
  }
}

// Selected database for multi-db servers; null means "server default".
// App sets this once /api/dbs has been fetched and on every selector change.
let currentDb: string | null = null;
let readMode: FreshnessMode = 'allow_stale';

export function setFreshnessMode(mode: FreshnessMode): void {
  readMode = mode;
}

function withReadPolicy(path: string): string {
  return `${path}${path.includes('?') ? '&' : '?'}freshness=${readMode}&freshness_timeout_ms=5000`;
}

export function setDb(name: string | null): void {
  currentDb = name;
}

// Bearer token for servers started with GRAG_API_TOKEN. Stored in
// localStorage (this browser only); never sent cross-origin because the
// server grants no CORS origins by default.
const TOKEN_KEY = 'grag.api.token';
let apiToken: string | null = localStorage.getItem(TOKEN_KEY);

export function setToken(token: string | null): void {
  apiToken = token;
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export function hasToken(): boolean {
  return apiToken != null;
}

// App-level hook fired on any 401 so it can show the token prompt.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

function apiUrl(path: string, db: string | null = currentDb): string {
  if (!db) return path;
  const sep = path.includes('?') ? '&' : '?';
  return `${path}${sep}db=${encodeURIComponent(db)}`;
}

async function request<T>(path: string, init?: RequestInit, db: string | null = currentDb): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (apiToken) headers['Authorization'] = `Bearer ${apiToken}`;
  let res: Response;
  try {
    res = await fetch(apiUrl(path, db), { headers, ...init });
  } catch {
    throw new ApiError(
      'cannot reach the grag server',
      `is \`grag serve\` running on ${window.location.host || 'this host'}?`,
      0,
    );
  }
  if (res.status === 401) onUnauthorized?.();
  if (!res.ok) {
    let body: { error?: string; hint?: string | null } | null = null;
    try {
      body = await res.json();
    } catch {
      // non-JSON error body — fall through to status text
    }
    throw new ApiError(
      body?.error ?? `${res.status} ${res.statusText}`,
      body?.hint ?? null,
      res.status,
    );
  }
  return (await res.json()) as T;
}

export function toFailure(e: unknown): ApiFailure {
  if (e instanceof ApiError) return e;
  return { message: e instanceof Error ? e.message : String(e), hint: null, status: 0 };
}

/** Freeze database/policy for a mounted view, including ambiguous-write retries. */
export function memoryApi() {
  const db = currentDb;
  const policy = {freshness: readMode, freshness_timeout_ms: 5000};
  const post = <T>(path: string, body: unknown) => request<T>(path, {method:'POST', body:JSON.stringify(body)}, db);
  return {
    list: (options: Record<string, unknown>) => post<MemoryList>('/api/memories', {...policy, ...options}),
    query: (cypher: string) => post<QueryResponse>('/api/query', {...policy, cypher, limit:100}),
    context: (options: Record<string, unknown>) => post<ContextResponse>('/api/context',
      {...policy, token_budget:8192, hops:0, evidence:'all', ...options}),
    save: (body: MutationRequest) => post<MutationResponse>('/api/nodes/upsert', body),
    index: () => request<IndexStatus>(`/api/index/status?freshness=${policy.freshness}&freshness_timeout_ms=5000`, undefined, db),
    observeIndex: (signal: AbortSignal) => request<IndexObservation>('/api/index/status?check=false', {signal}, db),
    jobs: () => request<{jobs: JobSummary[]}>('/api/jobs?limit=20', undefined, db),
  };
}

export const api = {
  health: () => request<HealthResponse>('/api/health'),

  dbs: () => request<DbsResponse>('/api/dbs'),

  schema: () => request<SchemaDocument>(withReadPolicy('/api/schema')),

  sample: (limit = 200, label?: string) =>
    request<GraphSample>(
      withReadPolicy(`/api/graph/sample?limit=${limit}${label ? `&label=${encodeURIComponent(label)}` : ''}`),
    ),

  // Complete topology from a captured download, outside ordinary query limits.
  // Carries keys/labels/endpoints only; full properties are unnecessary for SVG.
  full: () => request<GraphSample>(withReadPolicy('/api/graph/export')),

  query: (cypher: string, limit?: number) =>
    request<QueryResponse>('/api/query', {
      method: 'POST',
      body: JSON.stringify({ cypher, limit, freshness: readMode, freshness_timeout_ms: 5000 }),
    }),

  search: (query: string, topK = 8, hops = 1) =>
    request<SearchResponse>('/api/search', {
      method: 'POST',
      body: JSON.stringify({ query, top_k: topK, hops, freshness: readMode, freshness_timeout_ms: 5000 }),
    }),
};
