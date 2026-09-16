import type {NodeRecord} from '../types';

export function isGeneratedRecord(properties:Record<string,unknown>):boolean {
  return ['_source_state','_ingest_hash','_document_owner','_document_state','_document_identity','_document_file']
    .some(key=>properties[key]!=null);
}

export function SourceCitation({source, properties = {}}: {source: unknown; properties?: Record<string, unknown>}) {
  if (!source) return <span className="muted">No source recorded</span>;
  const citation = String(source) + (properties.line_start != null ? `:${properties.line_start}${properties.line_end ? `–${properties.line_end}` : ''}` : '');
  const web = /^https?:\/\//i.test(String(source));
  return <div className="citation">
    {web ? <a href={String(source)} target="_blank" rel="noopener noreferrer">{citation}</a> : <code>{citation}</code>}
    <button type="button" onClick={(e) => {
      const button = e.currentTarget;
      if (!navigator.clipboard) {button.textContent = 'Select text to copy'; return;}
      void navigator.clipboard.writeText(citation).then(() => {button.textContent = 'Copied';})
        .catch(() => {button.textContent = 'Select text to copy';});
    }}>Copy citation</button>
  </div>;
}

export function Coverage({value}: {value: unknown}) {
  if (!value) return null;
  let raw: unknown;
  try {raw = typeof value === 'string' ? JSON.parse(value) : value;} catch {
    return <details><summary>Coverage details (unparsed)</summary><pre className="prose">{String(value)}</pre></details>;
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return <p className="muted">No structured coverage report.</p>;
  const record = raw as Record<string,unknown>;
  const counts = record.counts && typeof record.counts==='object' && !Array.isArray(record.counts) ? record.counts as Record<string,unknown> : {};
  const examples = Array.isArray(record.examples) ? record.examples.filter(item=>item && typeof item==='object') as Record<string,unknown>[] : [];
  const mode = typeof record.mode==='string' ? record.mode.replace(/_/g,' ') : 'Recorded parser coverage';
  return <section className="record-section coverage">
    <h3>Analysis coverage</h3><p className="muted">{typeof record.language==='string' ? record.language : 'Code'} · {mode}</p>
    <div className="fact-grid">{Object.entries(counts).map(([key, count]) =>
      <div key={key}><strong>{String(count)}</strong><span>{key.replace(/_/g, ' ')}</span></div>)}</div>
    <details><summary>Unresolved sites and limits</summary>
      <ul>{examples.map((site, i) =>
        <li key={i}>{String(site.kind ?? '')} · line {String(site.line ?? '?')}: {String(site.reason ?? '')}</li>)}</ul>
      <p className="prose">{typeof record.limits==='string' ? record.limits : ''}</p>
      <p className="muted">These are recorded parser diagnostics. Missing relationships do not prove absence.</p>
    </details>
  </section>;
}

export function EvidenceBadges({node}: {node:NodeRecord}) {
  const p = node.properties;
  const state = p._evidence_state ?? 'untracked';
  const review = p._review_state ?? 'unreviewed';
  const generated=isGeneratedRecord(p);
  return <div className="badges">
    {generated ? <span className="evidence-badge">Indexed source</span> : <span className="evidence-badge">{String(state)}</span>}
    {generated && (state==='retracted' || state==='superseded') && <span className="evidence-badge warning">{String(state)}</span>}
    {(!generated || review!=='unreviewed') && <span className={`evidence-badge ${review === 'disputed' ? 'warning' : ''}`}>{String(review)}</span>}
    {p.status != null && <span className="evidence-badge">{String(p.status)}</span>}
    {(p._source_state === 'obsolete' || p._document_state === 'obsolete') && <span className="evidence-badge warning">Obsolete source</span>}
    {p._expires_at != null && <span className="evidence-badge warning">Expiry: {String(p._expires_at)}</span>}
  </div>;
}
