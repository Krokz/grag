// Mirrors grag.core.types — keep in sync with the frozen backend contracts.

export type FreshnessMode = 'allow_stale' | 'wait' | 'require';
export interface FreshnessReport {
  status: 'fresh' | 'checking' | 'refreshing' | 'stale' | 'error' | 'unknown' | 'disabled';
  checked_at: string | null;
  timed_out: boolean;
}

export interface NodeRecord {
  id: string; // "Label:key"
  label: string;
  properties: Record<string, unknown>;
}

export interface EdgeRecord {
  id: string; // "TYPE:source->target"
  type: string;
  source: string;
  target: string;
  properties: Record<string, unknown>;
}

export interface Subgraph {
  nodes: NodeRecord[];
  edges: EdgeRecord[];
}

export interface PropertyDoc {
  name: string;
  type: string;
  is_primary_key: boolean;
}

export interface NodeTableDoc {
  name: string;
  properties: PropertyDoc[];
  row_count: number;
  sample_keys: string[];
  searchable: boolean;
}

export interface RelTableDoc {
  name: string;
  from_label: string;
  to_label: string;
  properties: PropertyDoc[];
  row_count: number;
}

export interface SchemaDocument {
  freshness: FreshnessReport;
  node_tables: NodeTableDoc[];
  rel_tables: RelTableDoc[];
  text: string;
}

export interface QueryResponse {
  freshness: FreshnessReport;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  subgraph: Subgraph;
}

export interface ScoredNode {
  node: NodeRecord;
  score: number;
  match: 'fts' | 'vector' | 'graph';
}

export interface SearchResponse {
  freshness: FreshnessReport;
  seeds: ScoredNode[];
  subgraph: Subgraph;
  context: string;
  token_estimate: number;
  response_token_estimate: number;
  included_node_ids: string[];
  truncated: boolean;
  omitted_nodes: number;
  omitted_edges: number;
  omitted_properties: number;
  expansion_limited: boolean;
}

export interface GraphStats {
  node_count: number;
  edge_count: number;
  labels: Record<string, number>;
}

export interface GraphSample {
  freshness: FreshnessReport;
  subgraph: Subgraph;
  stats: GraphStats;
}

export interface HealthResponse {
  status: string;
  version: string;
  database_id: string | null;
}

export interface DbsResponse {
  dbs: string[];
  default: string | null;
}

export interface ApiFailure {
  message: string;
  hint: string | null;
  status: number;
}
