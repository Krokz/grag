import type { NodeRecord, SchemaDocument, Subgraph } from './types';

/** label -> primary key property name, from schema introspection. */
export function pkMapFromSchema(schema: SchemaDocument | null): Map<string, string> {
  const map = new Map<string, string>();
  for (const t of schema?.node_tables ?? []) {
    const pk = t.properties.find((p) => p.is_primary_key);
    if (pk) map.set(t.name, pk.name);
  }
  return map;
}

/** Stable label -> color hash so colors don't shift between renders/sessions. */
export function colorForLabel(label: string): string {
  let h = 0;
  for (let i = 0; i < label.length; i++) {
    h = (h * 31 + label.charCodeAt(i)) | 0;
  }
  const hue = Math.abs(h) % 360;
  return `hsl(${hue}, 62%, 58%)`;
}

export function splitNodeId(id: string): { label: string; key: string } {
  const i = id.indexOf(':');
  return i < 0 ? { label: id, key: '' } : { label: id.slice(0, i), key: id.slice(i + 1) };
}

/** Canvas caption: pk property value when known, else the raw id. */
export function displayName(node: NodeRecord, pkMap: Map<string, string>): string {
  const pk = pkMap.get(node.label);
  const v = pk ? node.properties[pk] : undefined;
  const s = v != null ? String(v) : node.id;
  return s.length > 28 ? s.slice(0, 27) + '…' : s;
}

export function mergeSubgraphs(a: Subgraph, b: Subgraph): Subgraph {
  const nodes = new Map(a.nodes.map((n) => [n.id, n]));
  for (const n of b.nodes) if (!nodes.has(n.id)) nodes.set(n.id, n);
  const edges = new Map(a.edges.map((e) => [e.id, e]));
  for (const e of b.edges) if (!edges.has(e.id)) edges.set(e.id, e);
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

function escapeCypherString(s: string): string {
  return s.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function cypherLiteral(value: unknown): string {
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return `'${escapeCypherString(String(value))}'`;
}

/** Neighbor-expansion query for a canvas node (double-click). */
export function neighborCypher(node: NodeRecord, pkMap: Map<string, string>): string {
  const pk = pkMap.get(node.label) ?? 'id';
  const raw = node.properties[pk];
  const key = raw != null ? raw : splitNodeId(node.id).key;
  return `MATCH (n:${node.label} {${pk}: ${cypherLiteral(key)}})-[r]-(m) RETURN n, r, m LIMIT 100`;
}

export function truncateCell(value: unknown, max = 120): string {
  const s = typeof value === 'string' ? value : JSON.stringify(value);
  const str = s === undefined ? 'null' : s;
  return str.length > max ? str.slice(0, max - 1) + '…' : str;
}
