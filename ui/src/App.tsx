import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, hasToken, setDb, setToken, setUnauthorizedHandler, toFailure } from './api';
import type {
  ApiFailure,
  GraphStats,
  HealthResponse,
  NodeRecord,
  QueryResponse,
  SchemaDocument,
  SearchResponse,
  Subgraph,
} from './types';
import { mergeSubgraphs, neighborCypher, pkMapFromSchema } from './graph-utils';
import { SchemaPanel } from './components/SchemaPanel';
import { GraphCanvas, type SeedInfo } from './components/GraphCanvas';
import { Console, type ApplyMode, type ResultView } from './components/Console';
import { SearchBar, SearchPanel } from './components/SearchBar';
import { Inspector } from './components/Inspector';

const EMPTY_GRAPH: Subgraph = { nodes: [], edges: [] };

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [dbs, setDbs] = useState<string[]>([]);
  const [db, setDbState] = useState<string | null>(null);
  const [dbsLoaded, setDbsLoaded] = useState(false);
  const [schema, setSchema] = useState<SchemaDocument | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [graph, setGraph] = useState<Subgraph>(EMPTY_GRAPH);
  const [stats, setStats] = useState<GraphStats | null>(null);
  const [seeds, setSeeds] = useState<Map<string, SeedInfo>>(new Map());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [labelFilter, setLabelFilter] = useState<string | null>(null);

  const [query, setQuery] = useState('MATCH (n) RETURN n LIMIT 25');
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [queryError, setQueryError] = useState<ApiFailure | null>(null);
  const [running, setRunning] = useState(false);
  const [view, setView] = useState<ResultView>('graph');
  const [applyMode, setApplyMode] = useState<ApplyMode>('merge');
  const [appliedNote, setAppliedNote] = useState<string | null>(null);

  const [searching, setSearching] = useState(false);
  const [searchResult, setSearchResult] = useState<SearchResponse | null>(null);
  const [searchError, setSearchError] = useState<ApiFailure | null>(null);

  // Set when any API call comes back 401 (server started with GRAG_API_TOKEN).
  const [needsToken, setNeedsToken] = useState(false);
  const [tokenDraft, setTokenDraft] = useState('');
  const tokenWasSet = hasToken(); // a 401 with a stored token means it was rejected

  const pkMap = useMemo(() => pkMapFromSchema(schema), [schema]);

  const loadSchema = useCallback(async () => {
    setSchemaLoading(true);
    try {
      setSchema(await api.schema());
    } catch {
      setSchema(null);
    } finally {
      setSchemaLoading(false);
    }
  }, []);

  const loadSample = useCallback(async () => {
    try {
      const sample = await api.sample(200);
      setStats(sample.stats);
      setGraph((g) => mergeSubgraphs(g, sample.subgraph));
    } catch {
      // health banner already signals connectivity problems
    }
  }, []);

  // Reset to the initial unfiltered overview: fresh sample, replacing the canvas.
  const resetView = useCallback(async () => {
    setLabelFilter(null);
    setFilter('');
    setSelectedId(null);
    try {
      const sample = await api.sample(200);
      setStats(sample.stats);
      setGraph(sample.subgraph); // replace, not merge — back to the start view
    } catch {
      // connectivity already surfaced via the health banner
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => setNeedsToken(true));
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    api
      .dbs()
      .then((res) => {
        setDbs(res.dbs);
        const initial = res.default ?? res.dbs[0] ?? null;
        setDb(initial);
        setDbState(initial);
      })
      .catch(() => {
        // older server without /api/dbs — stay in single-db mode
      })
      .finally(() => setDbsLoaded(true));
  }, []);

  const selectDb = useCallback((name: string) => {
    setDb(name);
    setDbState(name);
  }, []);

  // (Re)load everything db-scoped; on a db switch the old canvas is stale.
  useEffect(() => {
    if (!dbsLoaded) return;
    setGraph(EMPTY_GRAPH);
    setStats(null);
    setSeeds(new Map());
    setSelectedId(null);
    setResult(null);
    setQueryError(null);
    setSearchResult(null);
    setSearchError(null);
    setLabelFilter(null);
    setFilter('');
    loadSchema();
    loadSample();
  }, [dbsLoaded, db, loadSchema, loadSample]);

  const runQuery = useCallback(
    async (cypher: string, mode?: ApplyMode) => {
      const m = mode ?? applyMode;
      setRunning(true);
      setQueryError(null);
      try {
        const res = await api.query(cypher);
        setResult(res);
        if (res.subgraph.nodes.length > 0) {
          setGraph((g) => (m === 'replace' ? res.subgraph : mergeSubgraphs(g, res.subgraph)));
          setAppliedNote(m === 'replace' ? 'canvas replaced' : 'subgraph merged into canvas');
          setView('graph');
        } else {
          setAppliedNote(null);
          setView('table');
        }
      } catch (e) {
        setResult(null);
        setQueryError(toFailure(e));
      } finally {
        setRunning(false);
      }
    },
    [applyMode],
  );

  const expandNode = useCallback(
    (node: NodeRecord) => {
      setSelectedId(node.id);
      void runQuery(neighborCypher(node, pkMap), 'merge');
    },
    [pkMap, runQuery],
  );

  const selectLabel = useCallback(
    (label: string) => {
      // Nodes of this label PLUS their 1-hop relationships, so the view shows
      // the label *and* how it connects. OPTIONAL MATCH keeps isolated nodes
      // (a plain `MATCH (a)-[r]-(b)` drops them, and a 0-row result never
      // replaces the canvas — the label click looked dead). Undirected so
      // incoming rels count too.
      setLabelFilter(label);
      const q = `MATCH (a:${label}) OPTIONAL MATCH (a)-[r]-(b) RETURN a, r, b LIMIT 100`;
      setQuery(q);
      void runQuery(q, 'replace');
    },
    [runQuery],
  );

  const runSearch = useCallback(async (q: string) => {
    setSearching(true);
    setSearchError(null);
    try {
      const res = await api.search(q);
      setSearchResult(res);
      setGraph((g) => mergeSubgraphs(g, res.subgraph));
      setSeeds(new Map(res.seeds.map((s) => [s.node.id, { score: s.score, match: s.match }])));
    } catch (e) {
      setSearchResult(null);
      setSearchError(toFailure(e));
    } finally {
      setSearching(false);
    }
  }, []);

  const selectedNode = useMemo(
    () => graph.nodes.find((n) => n.id === selectedId) ?? null,
    [graph, selectedId],
  );

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">
          g<span>rag</span> · graph explorer
        </span>
        <SearchBar searching={searching} onSearch={runSearch} />
        {dbs.length > 1 && db != null && (
          <select
            className="db-select"
            value={db}
            onChange={(e) => selectDb(e.target.value)}
            title="database"
          >
            {dbs.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        )}
        <span className="health">
          <span className={health ? 'dot ok' : 'dot'} />
          {health ? `v${health.version}` : 'offline'}
        </span>
      </header>

      {needsToken && (
        <div className="error-banner token-banner">
          <span className="err">
            {tokenWasSet ? 'Stored token was rejected.' : 'This server requires an API token.'}
          </span>
          <div className="hint">
            the server was started with GRAG_API_TOKEN; the token is stored in this browser only
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const t = tokenDraft.trim();
              if (!t) return;
              setToken(t);
              location.reload();
            }}
          >
            <input
              type="password"
              placeholder="API token"
              value={tokenDraft}
              onChange={(e) => setTokenDraft(e.target.value)}
              autoFocus
            />
            <button type="submit">Save &amp; reload</button>
            {tokenWasSet && (
              <button
                type="button"
                onClick={() => {
                  setToken(null);
                  setNeedsToken(false);
                }}
              >
                Clear stored token
              </button>
            )}
          </form>
        </div>
      )}

      {(searchResult || searchError) && (
        <SearchPanel
          result={searchResult}
          error={searchError}
          onClose={() => {
            setSearchResult(null);
            setSearchError(null);
          }}
        />
      )}

      <div className="main">
        <SchemaPanel
          schema={schema}
          loading={schemaLoading}
          onRefresh={loadSchema}
          onSelectLabel={selectLabel}
        />
        <div className="center">
          <GraphCanvas
            subgraph={graph}
            pkMap={pkMap}
            seeds={seeds}
            selectedId={selectedId}
            filter={filter}
            stats={stats}
            onFilterChange={setFilter}
            onSelect={(n) => setSelectedId(n?.id ?? null)}
            onExpand={expandNode}
            onSelectLabel={selectLabel}
            labelFilter={labelFilter}
            onResetView={resetView}
          />
          {selectedNode && (
            <Inspector
              node={selectedNode}
              pkMap={pkMap}
              seed={seeds.get(selectedNode.id)}
              onClose={() => setSelectedId(null)}
              onExpand={expandNode}
            />
          )}
          <Console
            query={query}
            onQueryChange={setQuery}
            onRun={() => runQuery(query)}
            running={running}
            result={result}
            error={queryError}
            view={view}
            onViewChange={setView}
            applyMode={applyMode}
            onApplyModeChange={setApplyMode}
            appliedNote={appliedNote}
          />
        </div>
      </div>
    </div>
  );
}
