import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {memoryApi, toFailure} from '../api';
import {cypherLiteral} from '../graph-utils';
import type {ContextResponse, HistoryEntry, MutationRequest, NodeRecord, SchemaDocument} from '../types';
import {Coverage, EvidenceBadges, SourceCitation, isGeneratedRecord} from './RecordFacts';

export interface RecordIdentity {id:string; label:string; key:unknown}
export type BeginMemorySave = () => (identity:RecordIdentity) => Promise<void>;
interface Props {
  identity: RecordIdentity; schema:SchemaDocument; onExplore:(node:NodeRecord)=>void;
  onSelect:(identity:RecordIdentity)=>void; onChanged:()=>void;
  onBeginSave:BeginMemorySave;
}
type Action = 'edit' | 'accepted' | 'disputed' | 'retract' | 'restore';

export function MemoryDetail({identity, schema, onExplore, onSelect, onChanged, onBeginSave}:Props) {
  const client = useMemo(memoryApi, []);
  const alive = useRef(true);
  const readRun = useRef(0);
  const historyRun = useRef(0);
  const snapshotRun = useRef(0);
  const [node, setNode] = useState<NodeRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [tab, setTab] = useState<'record'|'history'>('record');
  const [related, setRelated] = useState<ContextResponse | null>(null);
  const [relatedError, setRelatedError] = useState('');
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [historyBefore, setHistoryBefore] = useState<number | null>(null);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const [snapshot, setSnapshot] = useState<ContextResponse | null>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [draft, setDraft] = useState<Record<string,string>>({});
  const [source, setSource] = useState('');
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<MutationRequest | null>(null);
  const table = schema.node_tables.find(t=>t.name===identity.label);
  const pk = table?.properties.find(p=>p.is_primary_key)?.name;
  const unsafeKey = typeof identity.key === 'number' && !Number.isSafeInteger(identity.key);
  const managed = node && isGeneratedRecord(node.properties);
  const fields = table?.properties.filter(p=>p.type==='STRING' && !p.is_primary_key && !p.name.startsWith('_') && p.name!=='code_coverage') ?? [];

  const reload = useCallback(async () => {
    const run=++readRun.current;
    ++historyRun.current; ++snapshotRun.current;
    setLoading(true); setError(''); setNode(null);
    setHistoryLoaded(false);setHistoryBusy(false);setHistoryError('');setHistory([]);setSnapshot(null);setRelated(null);setRelatedError('');setTab('record');
    try {
      if (!pk || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(pk) || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(identity.label)) throw new Error('This record has no supported primary key.');
      if (unsafeKey) throw new Error('This numeric key exceeds browser precision. Inspect it through the CLI or MCP to preserve its exact identity.');
      const result = await client.query(`MATCH (n:${identity.label}) WHERE n.${pk}=${cypherLiteral(identity.key)} RETURN n`);
      if (!alive.current || readRun.current!==run) return;
      const found = result.subgraph.nodes.find(n=>n.id===identity.id);
      if (!found) throw new Error('This record no longer exists. Refresh the list.');
      setNode(found);
    } catch (e) {if (alive.current && readRun.current===run) setError(toFailure(e).message);}
    finally {if (alive.current && readRun.current===run) setLoading(false);}
  }, [client, identity.id, identity.key, identity.label, pk, unsafeKey]);

  useEffect(()=>{alive.current=true; void reload(); return ()=>{alive.current=false;};},[reload]);

  const loadRelated = async () => {
    const run=readRun.current;
    setRelatedError('');
    try {
      const result = await client.context({node_ids:[identity.id],hops:1,token_budget:4000});
      if (alive.current && readRun.current===run) setRelated(result);
    } catch(e) {if(alive.current && readRun.current===run) setRelatedError(toFailure(e).message);}
  };
  const loadHistory = async (before?:number) => {
    const run=++historyRun.current;
    setHistoryBusy(true); setHistoryError('');
    try {
      const result = await client.context({node_ids:[identity.id],history:true,...(before==null?{}:{history_before:before})});
      if(!alive.current || historyRun.current!==run) return;
      setHistory(prev=>before==null ? result.history?.entries ?? [] : [...prev,...(result.history?.entries ?? [])]);
      setHistoryBefore(result.history?.next_before ?? null); setHistoryLoaded(true);
    } catch(e) {if(alive.current && historyRun.current===run) setHistoryError(toFailure(e).message);}
    finally {if(alive.current && historyRun.current===run) setHistoryBusy(false);}
  };
  const viewSnapshot = async (sequence:number) => {
    const run=++snapshotRun.current;
    setHistoryError(''); setSnapshot(null);
    try {
      const result = await client.context({node_ids:[identity.id],revision:sequence});
      if(alive.current && snapshotRun.current===run) setSnapshot(result);
    } catch(e) {if(alive.current && snapshotRun.current===run) setHistoryError(toFailure(e).message);}
  };
  const startEdit = (next:Action) => {
    if(!node) return;
    setAction(next); setReason(''); setError(''); setNotice('');
    setSource(String(node.properties._source ?? ''));
    setDraft(Object.fromEntries(fields.map(p=>[p.name,String(node.properties[p.name] ?? '')])));
  };
  const save = async (retry?:MutationRequest) => {
    if(!node || !action || saving) return;
    const properties:Record<string,unknown> = {};
    if(action==='edit') for(const [key,value] of Object.entries(draft)) {
      if(value !== String(node.properties[key] ?? '')) properties[key]=value;
    }
    const evidence:Record<string,unknown> = {actor:'grag UI',reason:reason.trim()};
    if(action==='accepted' || action==='disputed') evidence.review=action;
    if(action==='retract' || action==='restore') Object.assign(evidence,{state:action==='retract'?'retracted':'current',superseded_by:null});
    const payload:MutationRequest = retry ?? {operation_id:crypto.randomUUID(), nodes:[{
      label:node.label,key:identity.key,properties,expected_revision:String(node.properties._revision),evidence,
      ...(source.trim() && source!==String(node.properties._source ?? '') ? {source} : {}),
    }]};
    setSaving(true); setError(''); setPending(payload);
    const updateGraph=onBeginSave();
    try {
      const result = await client.save(payload);
      // The save may finish after the user has returned to Graph. The parent
      // owns cache refresh and its database guard independently of this view.
      await updateGraph(identity);
      if(!alive.current) return;
      setPending(null); setAction(null); setHistoryLoaded(false); setHistory([]); setSnapshot(null); setRelated(null);
      setNotice(result.warnings.length ? `Saved with warnings: ${result.warnings.join('; ')}` : 'Saved. Previous content is retained in history.');
      onChanged(); await reload();
    } catch(e) {
      if(!alive.current) return;
      const failure=toFailure(e);
      if(failure.status>=400 && failure.status<500) {
        setPending(null);
        setError(failure.status===409 ? 'This record changed in another session. Your draft is kept; reload the latest record before reconciling your edit.' : failure.message);
      } else {
        setError(`Save outcome is unconfirmed. Retry the same save to check its receipt. ${failure.message}`);
      }
    } finally {if(alive.current) setSaving(false);}
  };
  const p=node?.properties ?? {};
  const title=String(p.title ?? p.name ?? p.heading ?? identity.id);
  const canEdit=!!node && typeof p._revision==='string' && !managed && !unsafeKey;
  return <article className="memory-detail" aria-label="Memory detail">
    <div className="detail-heading"><span className="eyebrow">{identity.label}</span><h2>{title}</h2><code className="record-id">{identity.id}</code></div>
    {loading && <p role="status">Loading record…</p>}
    {error && <div className="inline-error" role="alert">{error}
      {!pending && <button onClick={()=>{setAction(null); void reload();}}>Reload latest (discard draft)</button>}
    </div>}
    {notice && <p className="save-notice" role="status">{notice}</p>}
    {node && <>
      <EvidenceBadges node={node}/>
      <p className="muted">{managed ? 'Indexed source record. Update its source and re-ingest to change generated content.' : 'Saved project record. Review states reflect a recorded judgment, not an independent fact check.'}</p>
      <div className="detail-actions">
        <button onClick={()=>onExplore(node)}>Explore relationships</button>
        {canEdit && <><button disabled={saving || !!pending} onClick={()=>startEdit('edit')}>Edit memory</button>
          <button disabled={saving || !!pending} onClick={()=>startEdit('accepted')}>Review</button>
          <button disabled={saving || !!pending} onClick={()=>startEdit(p._evidence_state==='retracted' || p._evidence_state==='superseded' ? 'restore' : 'retract')}>
            {p._evidence_state==='retracted' || p._evidence_state==='superseded' ? 'Restore memory' : 'Retire memory'}</button></>}
      </div>
      {action && <form className="edit-memory" onSubmit={e=>{e.preventDefault(); void save(pending ?? undefined);}}>
        <h3>{action==='edit'?'Correct this memory':action==='retract'?'Retire from current answers':action==='restore'?'Restore current lifecycle':'Record a review'}</h3>
        <fieldset disabled={saving || !!pending}>
          {action==='edit' && fields.map(field=><label key={field.name}>{field.name.replace(/_/g,' ')}
            <textarea aria-label={field.name} rows={['body','summary','text','description','rationale','content'].includes(field.name)?5:2} value={draft[field.name] ?? ''} onChange={e=>setDraft({...draft,[field.name]:e.target.value})}/>
          </label>)}
          {(action==='accepted' || action==='disputed') && <label>Review outcome<select value={action} onChange={e=>setAction(e.target.value as Action)}><option value="accepted">Accepted</option><option value="disputed">Disputed</option></select></label>}
          {action==='edit' && <label>Source citation (blank keeps current)<input value={source} onChange={e=>setSource(e.target.value)}/></label>}
          <label>Reason for this change<textarea aria-label="Reason for this change" required maxLength={2048} rows={2} value={reason} onChange={e=>setReason(e.target.value)}/></label>
        </fieldset>
        <p className="muted">{action==='retract'?'Content, relationships and history are kept. This does not mark a task done.':action==='restore'?'Expiry and review state are preserved; expired or disputed evidence still stays out of current answers.':'Only changed fields are saved. History records the edit as “grag UI”.'}</p>
        <button className="primary" disabled={saving || !reason.trim()} type="submit">{saving?'Saving…':pending?'Retry same save':'Save change'}</button>
        <button type="button" disabled={saving || !!pending} onClick={()=>setAction(null)}>Cancel</button>
      </form>}
      <div className="section-tabs" role="tablist" aria-label="Record views">
        <button role="tab" aria-selected={tab==='record'} onClick={()=>setTab('record')}>Record &amp; evidence</button>
        <button role="tab" aria-selected={tab==='history'} onClick={()=>{setTab('history'); if(!historyLoaded) void loadHistory();}}>History</button>
      </div>
      {tab==='record' ? <>
        <section className="record-section"><h3>Source evidence</h3><SourceCitation source={p._source} properties={p}/>
          {p._superseded_by != null && <p>Superseded by <code>{String(p._superseded_by)}</code></p>}
          {p._created_at != null && <p className="muted">Created {String(p._created_at)}</p>}
        </section>
        {Object.entries(p).filter(([key])=>!key.startsWith('_') && key!==pk && !['title','name','heading','code_coverage'].includes(key)).map(([key,value])=>
          <section className="record-section" key={key}><h3>{key.replace(/_/g,' ')}</h3><div className="prose">{typeof value==='string'?value:JSON.stringify(value,null,2)}</div></section>)}
        <Coverage value={p.code_coverage}/>
        <section className="record-section"><h3>Linked records</h3>
          {!related && <button onClick={()=>void loadRelated()}>Load linked evidence</button>}
          {relatedError && <p role="alert" className="inline-error">{relatedError}</p>}
          {related && <>{related.subgraph.nodes.filter(n=>n.id!==identity.id).map(n=>{
            const relatedPk=schema.node_tables.find(t=>t.name===n.label)?.properties.find(prop=>prop.is_primary_key)?.name;
            return <button className="related-record" key={n.id} disabled={!relatedPk || n.properties[relatedPk]==null} onClick={()=>onSelect({id:n.id,label:n.label,key:n.properties[relatedPk!]})}>
              <span className="eyebrow">{n.label}</span>{String(n.properties.title ?? n.properties.name ?? n.id)}
              <small>{related.subgraph.edges.filter(e=>(e.source===identity.id && e.target===n.id)||(e.target===identity.id && e.source===n.id)).map(e=>e.type).join(', ')}</small>
            </button>;
          })}
          {related.subgraph.nodes.length<=1 && <p className="muted">No linked records returned in this read.</p>}
          <p className="muted">{related.truncated || related.expansion_limited ? 'This is a partial, bounded neighborhood. Explore the graph to follow more links.' : 'Links are stored relationships; their presence does not validate a claim.'}</p></>}
        </section>
        <details className="record-section"><summary>Raw properties</summary><pre className="prose">{JSON.stringify(p,null,2)}</pre></details>
      </> : <section className="record-section">
        <p className="muted">History starts when tracking is enabled. Earlier edits, raw writes and source re-ingestion may not be recorded. Attribution is supplied by the writer.</p>
        {historyBusy && <p role="status">Loading history…</p>}
        {historyError && <p className="inline-error" role="alert">{historyError}</p>}
        {historyLoaded && !history.length && <p>No revision history recorded for this node.</p>}
        {history.map(entry=><div className="history-entry" key={entry.sequence}><div><strong>{entry.baseline?'Baseline':`Revision ${entry.sequence}`}</strong> · {new Date(entry.recorded_at).toLocaleString()}</div>
          <p>{entry.reason || 'No reason recorded'}</p><p className="muted">{entry.actor || 'Unknown author'}</p><SourceCitation source={entry.source}/>
          <button onClick={()=>void viewSnapshot(entry.sequence)}>View revision {entry.sequence}</button></div>)}
        {historyBefore!=null && <button disabled={historyBusy} onClick={()=>void loadHistory(historyBefore)}>Older revisions</button>}
        {snapshot && <div className="snapshot"><h3>Recorded snapshot</h3>{snapshot.truncated && <p className="warning">Partial snapshot: the read budget omitted some content. Use MCP/CLI revision text paging for the full text.</p>}<pre className="prose">{snapshot.context}</pre></div>}
      </section>}
    </>}
  </article>;
}
