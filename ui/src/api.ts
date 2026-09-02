import type {
  ApiFailure,
  DbsResponse,
  GraphSample,
  HealthResponse,
  QueryResponse,
  SchemaDocument,
  SearchResponse,
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

function apiUrl(path: string): string {
  if (!currentDb) return path;
  const sep = path.includes('?') ? '&' : '?';
  return `${path}${sep}db=${encodeURIComponent(currentDb)}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (apiToken) headers['Authorization'] = `Bearer ${apiToken}`;
  let res: Response;
  try {
    res = await fetch(apiUrl(path), { headers, ...init });
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

export const api = {
  health: () => request<HealthResponse>('/api/health'),

  dbs: () => request<DbsResponse>('/api/dbs'),

  schema: () => request<SchemaDocument>('/api/schema'),

  sample: (limit = 200, label?: string) =>
    request<GraphSample>(
      `/api/graph/sample?limit=${limit}${label ? `&label=${encodeURIComponent(label)}` : ''}`,
    ),

  // Every user node and edge, unclamped — feeds the whole-database SVG export.
  full: () => request<GraphSample>('/api/graph/full'),

  query: (cypher: string, limit?: number) =>
    request<QueryResponse>('/api/query', {
      method: 'POST',
      body: JSON.stringify(limit != null ? { cypher, limit } : { cypher }),
    }),

  search: (query: string, topK = 8, hops = 1) =>
    request<SearchResponse>('/api/search', {
      method: 'POST',
      body: JSON.stringify({ query, top_k: topK, hops }),
    }),
};
