// Mirrors grag.core.types — keep in sync with the frozen backend contracts.

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
  node_tables: NodeTableDoc[];
  rel_tables: RelTableDoc[];
  text: string;
}

export interface QueryResponse {
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
  seeds: ScoredNode[];
  subgraph: Subgraph;
  context: string;
}

export interface GraphStats {
  node_count: number;
  edge_count: number;
  labels: Record<string, number>;
}

export interface GraphSample {
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
