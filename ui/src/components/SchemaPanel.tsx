import { useState } from 'react';
import type { RelTableDoc, SchemaDocument } from '../types';
import { colorForLabel } from '../graph-utils';

interface Props {
  schema: SchemaDocument | null;
  loading: boolean;
  onRefresh: () => void;
  onSelectLabel: (label: string) => void;
}

function PropList({ props }: { props: { name: string; type: string; is_primary_key: boolean }[] }) {
  if (props.length === 0) return <div className="props hint-text">no properties</div>;
  return (
    <div className="props">
      {props.map((p) => (
        <div key={p.name} className={p.is_primary_key ? 'prop pk' : 'prop'}>
          <span className="pname">{p.name}</span>
          <span>{p.type}</span>
        </div>
      ))}
    </div>
  );
}

function RelRow({ rel }: { rel: RelTableDoc }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="schema-item">
      <div className="schema-row" onClick={() => setOpen(!open)} title="toggle properties">
        <span className="chevron">{open ? '▼' : '▶'}</span>
        <span className="rel-arrow">
          {rel.name}: {rel.from_label} → {rel.to_label}
        </span>
        <span className="badge">{rel.row_count}</span>
      </div>
      {open && <PropList props={rel.properties} />}
    </div>
  );
}

export function SchemaPanel({ schema, loading, onRefresh, onSelectLabel }: Props) {
  const [open, setOpen] = useState<Record<string, boolean>>({});

  return (
    <aside className="sidebar">
      <div className="side-head">
        <h2 style={{ margin: 0 }}>Schema</h2>
        <button onClick={onRefresh} disabled={loading} title="reload /api/schema">
          {loading ? '…' : '↻ refresh'}
        </button>
      </div>

      {!schema && !loading && (
        <div className="hint-text">schema unavailable — is the server up?</div>
      )}

      <h2>Node tables</h2>
      {schema?.node_tables.length === 0 && <div className="hint-text">none defined</div>}
      {schema?.node_tables.map((t) => (
        <div key={t.name} className="schema-item">
          <div className="schema-row">
            <span
              className="chevron"
              onClick={() => setOpen((o) => ({ ...o, [t.name]: !o[t.name] }))}
            >
              {open[t.name] ? '▼' : '▶'}
            </span>
            <span className="label-dot" style={{ background: colorForLabel(t.name) }} />
            <span
              className="schema-name"
              onClick={() => onSelectLabel(t.name)}
              title={`MATCH (n:${t.name}) RETURN n LIMIT 50`}
            >
              {t.name}
            </span>
            <span
              className={t.searchable ? 'searchable-dot' : 'searchable-dot off'}
              title={t.searchable ? 'full-text searchable' : 'not searchable'}
            />
            <span className="badge">{t.row_count}</span>
          </div>
          {open[t.name] && <PropList props={t.properties} />}
        </div>
      ))}

      <h2>Rel tables</h2>
      {schema?.rel_tables.length === 0 && <div className="hint-text">none defined</div>}
      {schema?.rel_tables.map((r) => (
        <RelRow key={r.name} rel={r} />
      ))}
    </aside>
  );
}
