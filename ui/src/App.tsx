import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, toFailure } from './api';
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
  const [schema, setSchema] = useState<SchemaDocument | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [graph, setGraph] = useState<Subgraph>(EMPTY_GRAPH);
  const [stats, setStats] = useState<GraphStats | null>(null);
  const [seeds, setSeeds] = useState<Map<string, SeedInfo>>(new Map());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filter, setFilter] = useState('');

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

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    loadSchema();
    loadSample();
  }, [loadSchema, loadSample]);

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
      const q = `MATCH (n:${label}) RETURN n LIMIT 50`;
      setQuery(q);
      void runQuery(q);
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
        <span className="health">
          <span className={health ? 'dot ok' : 'dot'} />
          {health ? `v${health.version}` : 'offline'}
        </span>
      </header>

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
