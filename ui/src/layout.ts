import {
  forceCenter,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
} from 'd3-force';
import type { EdgeRecord, NodeRecord } from './types';

export interface LaidOutNode extends NodeRecord {
  x?: number;
  y?: number;
}

export interface LaidOutLink extends Omit<EdgeRecord, 'source' | 'target'> {
  source: LaidOutNode | string;
  target: LaidOutNode | string;
}

export interface OffscreenLayout {
  nodes: LaidOutNode[];
  links: LaidOutLink[];
}

/** Run a headless force layout over a whole graph without touching the canvas.
 *
 * Ticks run in ~40ms slices with a yield to the event loop between them, so
 * the button can keep repainting progress while a five-thousand-node graph
 * settles. Resolves to null if `isCancelled()` turns true mid-run.
 */
export async function layoutOffscreen(
  graph: { nodes: NodeRecord[]; edges: EdgeRecord[] },
  onProgress: (fraction: number) => void,
  isCancelled: () => boolean = () => false,
): Promise<OffscreenLayout | null> {
  // Fresh copies: d3 writes x/y/vx/vy onto the objects it is handed and
  // swaps link endpoints for node references.
  const nodes: LaidOutNode[] = graph.nodes.map((n) => ({ ...n }));
  const ids = new Set(nodes.map((n) => n.id));
  const links: LaidOutLink[] = graph.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({ ...e }));

  // Match the on-screen defaults (force-graph: link distance 30, charge -30)
  // and add a weak pull to the origin so disconnected components and
  // singletons stay in frame instead of drifting to infinity.
  const sim = forceSimulation(nodes)
    .force(
      'link',
      forceLink<LaidOutNode, LaidOutLink>(links)
        .id((d) => d.id)
        .distance(30),
    )
    .force('charge', forceManyBody().strength(-30).theta(0.9))
    .force('center', forceCenter(0, 0))
    .force('x', forceX(0).strength(0.02))
    .force('y', forceY(0).strength(0.02))
    .stop();

  // d3's default decay settles in ~300 ticks. Past ten thousand nodes trade
  // some layout quality for time: a hundred-odd ticks already separates the
  // mass into its clusters, which is what a whole-graph poster is for.
  const ticks = nodes.length > 10_000 ? 120 : 300;
  sim.alphaDecay(1 - Math.pow(sim.alphaMin(), 1 / ticks));

  let done = 0;
  onProgress(0);
  while (sim.alpha() > sim.alphaMin()) {
    const sliceStart = performance.now();
    do {
      sim.tick();
      done += 1;
    } while (sim.alpha() > sim.alphaMin() && performance.now() - sliceStart < 40);
    onProgress(Math.min(1, done / ticks));
    await new Promise((resolve) => setTimeout(resolve, 0));
    if (isCancelled()) return null;
  }
  onProgress(1);
  return { nodes, links };
}
