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

/** Standard 10-color qualitative scheme (Observable 10) — readable on the
 * dark canvas and categorical: adjacent colors stay clearly distinguishable. */
const LABEL_PALETTE = [
  '#4269d0',
  '#efb118',
  '#ff725c',
  '#6cc5b0',
  '#3ca951',
  '#ff8ab7',
  '#a463f2',
  '#97bbf5',
  '#9c6b4e',
  '#9498a0',
];

function hashString(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

// label -> assigned color. The hash picks a stable *preferred* palette slot;
// if another label already holds it, the next free slot is taken so no two
// labels on screen ever share a color (the old raw-hue hash put Chunk and
// Entity 18° apart — both rendered as near-identical magenta).
const labelColors = new Map<string, string>();

export function colorForLabel(label: string): string {
  const cached = labelColors.get(label);
  if (cached) return cached;
  const taken = new Set(labelColors.values());
  let color: string | undefined;
  const start = hashString(label) % LABEL_PALETTE.length;
  for (let i = 0; i < LABEL_PALETTE.length; i++) {
    const candidate = LABEL_PALETTE[(start + i) % LABEL_PALETTE.length];
    if (!taken.has(candidate)) {
      color = candidate;
      break;
    }
  }
  // More labels than palette slots: spread the overflow around the color wheel.
  color ??= `hsl(${hashString(label) % 360}, 62%, 58%)`;
  labelColors.set(label, color);
  return color;
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

// --- Table view model ---------------------------------------------------------

export interface TableCell {
  text: string; // display text (truncated)
  full: string; // full content for the expand modal
  truncated: boolean;
}

export interface TableModel {
  columns: string[];
  rows: TableCell[][];
}

// Mirrors grag.core.engine._HIDDEN_NODE_PROPS / _HIDDEN_REL_PROPS — internal
// identifiers and bulky vector payloads are never shown as table columns.
const HIDDEN_VALUE_KEYS = new Set(['_ID', '_LABEL', '_SRC', '_DST', 'embedding', '_emb_code']);

type GraphValue = Record<string, unknown> & { _LABEL: string };

function isNodeCell(v: unknown): v is GraphValue {
  return (
    typeof v === 'object' &&
    v !== null &&
    '_ID' in v &&
    '_LABEL' in v &&
    !('_SRC' in v)
  );
}

function isRelCell(v: unknown): v is GraphValue {
  return typeof v === 'object' && v !== null && '_SRC' in v && '_DST' in v;
}

/** User properties of a raw node/rel cell: hidden keys and nulls removed.
 * LadybugDB returns the union schema when a MATCH spans labels, so rows carry
 * keys filled with null for every other table — those are pure noise. */
function visibleProps(v: GraphValue): [string, unknown][] {
  return Object.entries(v).filter(
    ([k, val]) => !HIDDEN_VALUE_KEYS.has(k) && val !== null && val !== undefined,
  );
}

function cellText(value: unknown, max = 120): TableCell {
  if (value === null || value === undefined) {
    return { text: '', full: '', truncated: false };
  }
  const full =
    typeof value === 'string'
      ? value
      : typeof value === 'object'
        ? JSON.stringify(value, null, 2)
        : String(value);
  const oneLine = full.replace(/\s+/g, ' ').trim();
  const truncated = oneLine.length > max;
  return { text: truncated ? oneLine.slice(0, max - 1) + '…' : oneLine, full, truncated };
}

/** Reshape raw query rows for display: node/rel cells (RETURN n) are exploded
 * into one column per property actually present on that column's values
 * (`n`, `n.id`, `n.text`, …) instead of a single raw JSON blob. */
export function buildTableModel(columns: string[], rows: unknown[][]): TableModel {
  // First pass: per source column, decide scalar vs graph and collect the
  // property keys observed across rows (first-appearance order).
  const plans = columns.map((alias, j) => {
    let graph = false;
    const propKeys: string[] = [];
    const seen = new Set<string>();
    for (const row of rows) {
      const cell = row[j];
      if (isNodeCell(cell) || isRelCell(cell)) {
        graph = true;
        for (const [k] of visibleProps(cell)) {
          if (!seen.has(k)) {
            seen.add(k);
            propKeys.push(k);
          }
        }
      }
    }
    return { alias, graph, propKeys };
  });

  const outColumns: string[] = [];
  for (const p of plans) {
    if (p.graph) {
      outColumns.push(p.alias, ...p.propKeys.map((k) => `${p.alias}.${k}`));
    } else {
      outColumns.push(p.alias);
    }
  }

  const outRows = rows.map((row) => {
    const out: TableCell[] = [];
    row.forEach((cell, j) => {
      const plan = plans[j];
      if (!plan.graph) {
        out.push(cellText(cell));
        return;
      }
      if (isNodeCell(cell) || isRelCell(cell)) {
        const props = new Map(visibleProps(cell));
        out.push(cellText(cell._LABEL));
        for (const k of plan.propKeys) {
          out.push(props.has(k) ? cellText(props.get(k)) : cellText(null));
        }
      } else {
        // non-graph value in a graph column (e.g. null from OPTIONAL MATCH)
        out.push(cellText(cell));
        for (let i = 0; i < plan.propKeys.length; i++) out.push(cellText(null));
      }
    });
    return out;
  });

  return { columns: outColumns, rows: outRows };
}
