import { useState } from 'react';
import type { ApiFailure, NodeRecord, SearchResponse } from '../types';

interface BarProps {
  searching: boolean;
  onSearch: (query: string) => void;
}

export function SearchBar({ searching, onSearch }: BarProps) {
  const [value, setValue] = useState('');

  const submit = () => {
    const q = value.trim();
    if (q) onSearch(q);
  };

  return (
    <div className="searchbox">
      <input
        placeholder="Search knowledge… (FTS + vector, expands 1 hop)"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') submit();
        }}
      />
      <button className="primary" onClick={submit} disabled={searching || !value.trim()}>
        {searching ? 'searching…' : 'Search'}
      </button>
    </div>
  );
}

interface PanelProps {
  result: SearchResponse | null;
  error: ApiFailure | null;
  selectedId: string | null;
  onSelectNode: (node: NodeRecord) => void;
  onClose: () => void;
}

/** Collapsible LLM-grounding context returned by /api/search. */
export function SearchPanel({ result, error, selectedId, onSelectNode, onClose }: PanelProps) {
  const [open, setOpen] = useState(true);

  return (
    <details
      className="context-panel"
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary>
        grounding context
        {result ? ` — ${result.seeds.length} seeds, ${result.context.length} chars` : ''}
        {'  '}(click to {open ? 'collapse' : 'expand'})
        <button
          style={{ float: 'right', border: 'none', background: 'none', color: 'var(--text-dim)' }}
          onClick={(e) => {
            e.preventDefault();
            onClose();
          }}
        >
          ✕ dismiss
        </button>
      </summary>
      {error && (
        <div className="error-banner">
          <div className="err">{error.message}</div>
          {error.hint && <div className="hint">{error.hint}</div>}
        </div>
      )}
      {result && (
        <>
          <div className="seed-list">
            {result.seeds.map((s) => (
              <button
                key={s.node.id}
                type="button"
                className={`seed${selectedId === s.node.id ? ' seed-active' : ''}`}
                title={`Show ${s.node.id} in the graph`}
                aria-label={`Show ${s.node.id} in the graph`}
                aria-pressed={selectedId === s.node.id}
                onClick={() => onSelectNode(s.node)}
              >
                <span className="score">{s.score.toFixed(3)}</span>
                <span className="match">{s.match}</span>
                <span className="seed-id">{s.node.id}</span>
                <span className="seed-jump" aria-hidden="true">⌖</span>
              </button>
            ))}
            {result.seeds.length === 0 && <div className="hint-text">no matching nodes</div>}
          </div>
          {result.context && <pre className="raw context-text">{result.context}</pre>}
        </>
      )}
    </details>
  );
}
