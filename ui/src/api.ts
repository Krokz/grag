import type {
  ApiFailure,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    });
  } catch {
    throw new ApiError(
      'cannot reach the grag server',
      'is `grag serve` running on port 8471?',
      0,
    );
  }
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

  schema: () => request<SchemaDocument>('/api/schema'),

  sample: (limit = 200, label?: string) =>
    request<GraphSample>(
      `/api/graph/sample?limit=${limit}${label ? `&label=${encodeURIComponent(label)}` : ''}`,
    ),

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
