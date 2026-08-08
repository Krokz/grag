import { useMemo, useState } from 'react';
import CodeMirror from '@uiw/react-codemirror';
import type { ApiFailure, QueryResponse } from '../types';
import { buildTableModel, type TableCell } from '../graph-utils';

export type ApplyMode = 'merge' | 'replace';
export type ResultView = 'graph' | 'table';

interface ExpandedCell {
  header: string;
  cell: TableCell;
}

interface Props {
  query: string;
  onQueryChange: (q: string) => void;
  onRun: () => void;
  running: boolean;
  result: QueryResponse | null;
  error: ApiFailure | null;
  view: ResultView;
  onViewChange: (v: ResultView) => void;
  applyMode: ApplyMode;
  onApplyModeChange: (m: ApplyMode) => void;
  appliedNote: string | null;
}

export function Console({
  query,
  onQueryChange,
  onRun,
  running,
  result,
  error,
  view,
  onViewChange,
  applyMode,
  onApplyModeChange,
  appliedNote,
}: Props) {
  const [expanded, setExpanded] = useState<ExpandedCell | null>(null);
  const table = useMemo(
    () => (result ? buildTableModel(result.columns, result.rows) : null),
    [result],
  );
  return (
    <section className="console">
      <div className="console-main">
        <div className="editor-pane">
          <div
            className="cm-host"
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                e.preventDefault();
                onRun();
              }
            }}
          >
            <CodeMirror
              value={query}
              onChange={(v) => onQueryChange(v)}
              theme="dark"
              height="100%"
              style={{ height: '100%' }}
              basicSetup={{ lineNumbers: true, foldGutter: false, autocompletion: false }}
            />
          </div>
          <div className="editor-actions">
            <button className="primary" onClick={onRun} disabled={running || !query.trim()}>
              {running ? 'running…' : 'Run'}
            </button>
            <span className="radio">
              <span style={{ marginRight: 2 }}>graph:</span>
              <label>
                <input
                  type="radio"
                  checked={applyMode === 'merge'}
                  onChange={() => onApplyModeChange('merge')}
                />{' '}
                merge
              </label>
              <label>
                <input
                  type="radio"
                  checked={applyMode === 'replace'}
                  onChange={() => onApplyModeChange('replace')}
                />{' '}
                replace
              </label>
            </span>
            <span className="spacer" />
            <span>
              <span className="kbd">Ctrl</span>+<span className="kbd">Enter</span> to run
            </span>
          </div>
        </div>

        <div className="results-pane">
          <div className="results-bar">
            <div className="tabs">
              <button
                className={view === 'graph' ? 'active' : ''}
                onClick={() => onViewChange('graph')}
              >
                Graph
              </button>
              <button
                className={view === 'table' ? 'active' : ''}
                onClick={() => onViewChange('table')}
              >
                Table
              </button>
            </div>
            {result && (
              <span>
                {result.row_count} row{result.row_count === 1 ? '' : 's'}
                {result.truncated ? ' · truncated' : ''}
              </span>
            )}
          </div>

          <div className="results-body">
            {error && (
              <div className="error-banner">
                <div className="err">{error.message}</div>
                {error.hint && <div className="hint">{error.hint}</div>}
              </div>
            )}

            {!error && !result && (
              <div className="placeholder">
                Run a read-only Cypher query — e.g. <code>MATCH (n) RETURN n LIMIT 25</code>
              </div>
            )}

            {!error && result && view === 'graph' && (
              <div className="placeholder">
                {result.subgraph.nodes.length > 0 ? (
                  <>
                    {appliedNote ?? 'subgraph applied to canvas'} —{' '}
                    {result.subgraph.nodes.length} nodes, {result.subgraph.edges.length} edges
                    ({applyMode} mode)
                  </>
                ) : (
                  <>query returned no graph elements — check the Table tab</>
                )}
              </div>
            )}

            {!error && result && view === 'table' && table && (
              <>
                {table.columns.length === 0 ? (
                  <div className="placeholder">no columns in result</div>
                ) : (
                  <table className="results">
                    <thead>
                      <tr>
                        {table.columns.map((c) => (
                          <th key={c}>{c}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {table.rows.map((row, i) => (
                        <tr key={i}>
                          {row.map((cell, j) => (
                            <td
                              key={j}
                              className={cell.truncated ? 'cell-truncated' : undefined}
                              title={cell.truncated ? 'click to view full value' : undefined}
                              onClick={
                                cell.truncated
                                  ? () => setExpanded({ header: table.columns[j], cell })
                                  : undefined
                              }
                            >
                              {cell.text}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {result.rows.length === 0 && result.columns.length > 0 && (
                  <div className="placeholder">0 rows</div>
                )}
              </>
            )}
          </div>
        </div>
      </div>

      {expanded && (
        <div className="cell-modal-overlay" onClick={() => setExpanded(null)}>
          <div className="cell-modal" onClick={(e) => e.stopPropagation()}>
            <div className="cell-modal-head">
              <span className="cell-modal-title">{expanded.header}</span>
              <button className="close" onClick={() => setExpanded(null)} title="close">
                ✕
              </button>
            </div>
            <pre className="cell-modal-body">{expanded.cell.full}</pre>
          </div>
        </div>
      )}
    </section>
  );
}
