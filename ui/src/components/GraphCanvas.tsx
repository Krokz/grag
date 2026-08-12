import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import type { EdgeRecord, GraphStats, NodeRecord, Subgraph } from '../types';
import { colorForLabel, displayName } from '../graph-utils';

export interface SeedInfo {
  score: number;
  match: string;
}

export interface FocusRequest {
  nodeId: string;
  revision: number;
  /** Keep this node fixed while newly-added neighbors settle around it. */
  keepStable: boolean;
}

interface Props {
  subgraph: Subgraph;
  pkMap: Map<string, string>;
  seeds: Map<string, SeedInfo>;
  selectedId: string | null;
  focusRequest: FocusRequest | null;
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
  fx?: number;
  fy?: number;
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
  focusRequest,
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
  const pinnedNode = useRef<FgNode | null>(null);
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
    // ForceGraph writes simulation coordinates onto each node. Preserve those
    // object identities across graph merges so adding neighbors does not reset
    // every existing node (especially the selected one) to a random position.
    const nodes = subgraph.nodes.filter(visible) as FgNode[];
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

  useEffect(() => {
    if (!focusRequest) return;

    let cancelled = false;
    let pinnedByThisEffect: FgNode | null = null;
    const timers: number[] = [];

    const focusWhenReady = (attempt = 0) => {
      if (cancelled) return;
      const node = data.nodes.find((candidate) => candidate.id === focusRequest.nodeId);
      const fg = fgRef.current;
      if (!node || !fg || !Number.isFinite(node.x) || !Number.isFinite(node.y)) {
        if (attempt < 12) {
          timers.push(window.setTimeout(() => focusWhenReady(attempt + 1), 50));
        }
        return;
      }

      if (focusRequest.keepStable) {
        // Expansion reheats the simulation. Anchor the selected node until the
        // graph's 2.5s cooldown completes, while its new neighbors arrange
        // themselves around the existing point of interest.
        if (pinnedNode.current && pinnedNode.current !== node) {
          delete pinnedNode.current.fx;
          delete pinnedNode.current.fy;
        }
        node.fx = node.x;
        node.fy = node.y;
        pinnedNode.current = node;
        pinnedByThisEffect = node;
        timers.push(
          window.setTimeout(() => {
            if (pinnedNode.current === node) {
              delete node.fx;
              delete node.fy;
              pinnedNode.current = null;
            }
          }, 2700),
        );
      }

      fg.centerAt(node.x, node.y, 450);
      if (!focusRequest.keepStable && fg.zoom() < 2.5) {
        fg.zoom(2.5, 450);
      }
    };

    timers.push(window.setTimeout(() => focusWhenReady(), 0));
    return () => {
      cancelled = true;
      timers.forEach(window.clearTimeout);
      if (pinnedByThisEffect && pinnedNode.current === pinnedByThisEffect) {
        delete pinnedByThisEffect.fx;
        delete pinnedByThisEffect.fy;
        pinnedNode.current = null;
      }
    };
  }, [focusRequest]);

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

  // IDs of edges touching the selected node — used to highlight + label them.
  const selectedEdgeIds = useMemo(() => {
    if (!selectedId) return new Set<string>();
    return new Set(
      data.links
        .filter((e) => {
          const src = typeof e.source === 'object' ? (e.source as FgNode).id : e.source;
          const tgt = typeof e.target === 'object' ? (e.target as FgNode).id : e.target;
          return src === selectedId || tgt === selectedId;
        })
        .map((e) => e.id),
    );
  }, [selectedId, data.links]);

  const linkColor = useCallback(
    (link: object) => {
      if (!selectedId) return 'rgba(107, 114, 128, 0.55)';
      return selectedEdgeIds.has((link as EdgeRecord).id)
        ? 'rgba(56, 189, 248, 0.85)'   // accent — highlighted edge
        : 'rgba(107, 114, 128, 0.18)'; // dimmed
    },
    [selectedId, selectedEdgeIds],
  );

  const linkWidth = useCallback(
    (link: object) =>
      selectedId && selectedEdgeIds.has((link as EdgeRecord).id) ? 2 : 1,
    [selectedId, selectedEdgeIds],
  );

  const drawLink = useCallback(
    (link: object, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const e = link as EdgeRecord & { source: FgNode; target: FgNode };
      if (!selectedEdgeIds.has(e.id)) return;
      const sx = e.source.x ?? 0;
      const sy = e.source.y ?? 0;
      const tx = e.target.x ?? 0;
      const ty = e.target.y ?? 0;
      const mx = (sx + tx) / 2;
      const my = (sy + ty) / 2;

      const fontSize = Math.min(3.5, Math.max(2, 10 / globalScale));
      ctx.font = `600 ${fontSize}px -apple-system, sans-serif`;
      const text = e.type;
      const w = ctx.measureText(text).width;
      const pad = 1.2;

      // pill background
      ctx.fillStyle = 'rgba(14, 116, 144, 0.82)';
      const rx = 1.5;
      const bx = mx - w / 2 - pad;
      const by = my - fontSize / 2 - pad;
      const bw = w + pad * 2;
      const bh = fontSize + pad * 2;
      ctx.beginPath();
      ctx.moveTo(bx + rx, by);
      ctx.lineTo(bx + bw - rx, by);
      ctx.quadraticCurveTo(bx + bw, by, bx + bw, by + rx);
      ctx.lineTo(bx + bw, by + bh - rx);
      ctx.quadraticCurveTo(bx + bw, by + bh, bx + bw - rx, by + bh);
      ctx.lineTo(bx + rx, by + bh);
      ctx.quadraticCurveTo(bx, by + bh, bx, by + bh - rx);
      ctx.lineTo(bx, by + rx);
      ctx.quadraticCurveTo(bx, by, bx + rx, by);
      ctx.closePath();
      ctx.fill();

      ctx.fillStyle = '#e0f2fe';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, mx, my);
    },
    [selectedEdgeIds],
  );

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
          linkColor={linkColor}
          linkWidth={linkWidth}
          linkDirectionalArrowLength={3.5}
          linkDirectionalArrowRelPos={1}
          linkCanvasObjectMode={() => 'after'}
          linkCanvasObject={drawLink}
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
