import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import type { EdgeRecord, GraphStats, NodeRecord, Subgraph } from '../types';
import { colorForLabel, displayName } from '../graph-utils';

export interface SeedInfo {
  score: number;
  match: string;
}

interface Props {
  subgraph: Subgraph;
  pkMap: Map<string, string>;
  seeds: Map<string, SeedInfo>;
  selectedId: string | null;
  filter: string;
  stats: GraphStats | null;
  onFilterChange: (f: string) => void;
  onSelect: (node: NodeRecord | null) => void;
  onExpand: (node: NodeRecord) => void;
  onSelectLabel?: (label: string) => void;
  labelFilter?: string | null;
  onResetView?: () => void;
}

interface FgNode extends NodeRecord {
  x?: number;
  y?: number;
}

/** Past this many visible nodes, per-node labels turn into hover/selection
 * labels only — a wall of overlapping captions is unreadable anyway. */
const LABEL_NODE_LIMIT = 30;

interface LabelRect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

function overlaps(r: LabelRect, others: LabelRect[]): boolean {
  return others.some((o) => r.x0 < o.x1 && r.x1 > o.x0 && r.y0 < o.y1 && r.y1 > o.y0);
}

function useElementSize() {
  const ref = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0].contentRect;
      setSize({ width: Math.floor(r.width), height: Math.floor(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size] as const;
}

export function GraphCanvas({
  subgraph,
  pkMap,
  seeds,
  selectedId,
  filter,
  stats,
  onFilterChange,
  onSelect,
  onExpand,
  onSelectLabel,
  labelFilter,
  onResetView,
}: Props) {
  const [wrapRef, size] = useElementSize();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(null);
  const didFit = useRef(false);
  const lastClick = useRef<{ id: string; t: number }>({ id: '', t: 0 });
  const [hoverId, setHoverId] = useState<string | null>(null);
  // Label bounding boxes already drawn this frame — cleared in
  // onRenderFramePre, filled as labels render, used to skip overlaps.
  const drawnLabels = useRef<LabelRect[]>([]);

  const data = useMemo(() => {
    const f = filter.trim().toLowerCase();
    // labels starting with "_" are grag-internal (e.g. the _grag_tables
    // registry leaks into /api/graph/sample) — never render them
    const visible = (n: NodeRecord) =>
      !n.label.startsWith('_') &&
      (!f ||
        n.id.toLowerCase().includes(f) ||
        JSON.stringify(n.properties).toLowerCase().includes(f));
    const nodes: FgNode[] = subgraph.nodes.filter(visible).map((n) => ({ ...n }));
    const ids = new Set(nodes.map((n) => n.id));
    const links = subgraph.edges
      .filter((e: EdgeRecord) => ids.has(e.source) && ids.has(e.target))
      .map((e) => ({ ...e }));
    return { nodes, links };
  }, [subgraph, filter]);

  useEffect(() => {
    if (!didFit.current && data.nodes.length > 0) {
      didFit.current = true;
      const t = setTimeout(() => fgRef.current?.zoomToFit(600, 48), 400);
      return () => clearTimeout(t);
    }
  }, [data.nodes.length]);

  const handleClick = useCallback(
    (node: object) => {
      const n = node as FgNode;
      const now = Date.now();
      if (lastClick.current.id === n.id && now - lastClick.current.t < 350) {
        lastClick.current = { id: '', t: 0 };
        onExpand(n);
        return;
      }
      lastClick.current = { id: n.id, t: now };
      onSelect(n);
    },
    [onExpand, onSelect],
  );

  const drawNode = useCallback(
    (node: object, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const n = node as FgNode;
      const x = n.x ?? 0;
      const y = n.y ?? 0;
      const seed = seeds.get(n.id);
      const r = seed ? 5 + 3 * Math.min(1, Math.max(0, seed.score)) : 5;

      if (seed) {
        ctx.beginPath();
        ctx.arc(x, y, r + 3.5, 0, 2 * Math.PI);
        ctx.strokeStyle = '#facc15';
        ctx.lineWidth = 1.5 + 2 * Math.min(1, Math.max(0, seed.score));
        ctx.stroke();
      }

      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.fillStyle = colorForLabel(n.label);
      ctx.fill();

      if (n.id === selectedId) {
        ctx.beginPath();
        ctx.arc(x, y, r + 1.8, 0, 2 * Math.PI);
        ctx.strokeStyle = '#e5e7eb';
        ctx.lineWidth = 1.4;
        ctx.stroke();
      }

      // --- label ------------------------------------------------------------
      // Beyond LABEL_NODE_LIMIT only highlighted nodes get captions; below it
      // every node is labeled, but a caption whose box overlaps one already
      // drawn this frame is skipped instead of rendering an unreadable pileup.
      const highlighted = n.id === hoverId || n.id === selectedId || seed != null;
      if (!highlighted && data.nodes.length > LABEL_NODE_LIMIT) return;

      const text = displayName(n, pkMap);
      const fontSize = Math.min(4, Math.max(2.2, 11 / globalScale));
      ctx.font = `${fontSize}px -apple-system, sans-serif`;
      const w = ctx.measureText(text).width;
      const rect: LabelRect = {
        x0: x - w / 2 - 0.6,
        y0: y + r + 1,
        x1: x + w / 2 + 0.6,
        y1: y + r + 1.5 + fontSize + 0.6,
      };
      if (!highlighted && overlaps(rect, drawnLabels.current)) return;
      drawnLabels.current.push(rect);

      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      // dark pill behind the text so captions stay legible over edges
      ctx.fillStyle = 'rgba(11, 15, 20, 0.62)';
      ctx.fillRect(rect.x0, rect.y0, rect.x1 - rect.x0, rect.y1 - rect.y0);
      ctx.fillStyle = highlighted ? '#f3f4f6' : 'rgba(229, 231, 235, 0.85)';
      ctx.fillText(text, x, y + r + 1.5);
    },
    [seeds, selectedId, hoverId, pkMap, data.nodes.length],
  );

  const paintPointerArea = useCallback((node: object, color: string, ctx: CanvasRenderingContext2D) => {
    const n = node as FgNode;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(n.x ?? 0, n.y ?? 0, 9, 0, 2 * Math.PI);
    ctx.fill();
  }, []);

  const labels = useMemo(
    () => [...new Set(subgraph.nodes.map((n) => n.label).filter((l) => !l.startsWith('_')))].sort(),
    [subgraph],
  );

  const empty = stats != null && stats.node_count === 0;

  return (
    <div className="canvas-wrap" ref={wrapRef}>
      <div className="canvas-toolbar">
        <input
          placeholder="Filter nodes…"
          value={filter}
          onChange={(e) => onFilterChange(e.target.value)}
        />
        {labelFilter && (
          <span className="filter-chip" title="A label filter is active">
            label: <strong>{labelFilter}</strong>
            {onResetView && (
              <button
                type="button"
                className="filter-chip-x"
                title="Clear filter and return to the full overview"
                onClick={onResetView}
              >
                ×
              </button>
            )}
          </span>
        )}
        {onResetView && (labelFilter || filter) && (
          <button
            type="button"
            className="reset-view-btn"
            title="Clear all filters and return to the full overview"
            onClick={onResetView}
          >
            Reset view
          </button>
        )}
        <span className="canvas-stats">
          {data.nodes.length} nodes · {data.links.length} edges
          {filter && ` (filtered from ${subgraph.nodes.length})`}
        </span>
      </div>

      {empty && (
        <div className="empty-overlay">
          <div className="empty-card">
            <strong>Empty graph</strong>
            this database has no nodes yet — ingest something first
            <br />
            <code>grag ingest &lt;files&gt;</code> or <code>POST /api/ingest</code>
          </div>
        </div>
      )}

      {labels.length > 0 && !empty && (
        <div className="legend">
          {labels.map((l) =>
            onSelectLabel ? (
              <button
                key={l}
                type="button"
                className={`li li-btn${labelFilter === l ? ' li-active' : ''}`}
                title={
                  labelFilter === l
                    ? `${l} filter active — click to clear`
                    : `Show only ${l} nodes and their relationships`
                }
                onClick={() =>
                  labelFilter === l && onResetView ? onResetView() : onSelectLabel(l)
                }
              >
                <span className="label-dot" style={{ background: colorForLabel(l) }} />
                {l}
                {stats?.labels[l] != null ? ` (${stats.labels[l]})` : ''}
              </button>
            ) : (
              <span key={l} className="li">
                <span className="label-dot" style={{ background: colorForLabel(l) }} />
                {l}
                {stats?.labels[l] != null ? ` (${stats.labels[l]})` : ''}
              </span>
            ),
          )}
        </div>
      )}

      {size.width > 0 && size.height > 0 && (
        <ForceGraph2D
          ref={fgRef}
          width={size.width}
          height={size.height}
          graphData={data}
          nodeId="id"
          nodeCanvasObject={drawNode}
          nodePointerAreaPaint={paintPointerArea}
          linkLabel={(link: object) => (link as EdgeRecord).type}
          linkColor={() => 'rgba(107, 114, 128, 0.55)'}
          linkWidth={1}
          linkDirectionalArrowLength={3.5}
          linkDirectionalArrowRelPos={1}
          onNodeClick={handleClick}
          onNodeHover={(node: object | null) =>
            setHoverId(node ? (node as FgNode).id : null)
          }
          onRenderFramePre={() => {
            drawnLabels.current = [];
          }}
          onBackgroundClick={() => onSelect(null)}
          enableNodeDrag={true}
          cooldownTime={2500}
        />
      )}
    </div>
  );
}
