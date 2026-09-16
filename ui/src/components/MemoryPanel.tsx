import {useEffect, useMemo, useState} from 'react';
import {memoryApi, toFailure} from '../api';
import type {MemoryList, NodeRecord, SchemaDocument} from '../types';
import {MemoryDetail, type RecordIdentity, type BeginMemorySave} from './MemoryDetail';

interface Props {schema:SchemaDocument | null; schemaError:string; onExplore:(node:NodeRecord)=>void; onRefreshSchema:()=>void; onBeginSave:BeginMemorySave; initial?:RecordIdentity|null}
export function MemoryPanel({schema,schemaError,onExplore,onRefreshSchema,onBeginSave,initial}:Props) {
  const client=useMemo(memoryApi,[]);
  const [mode,setMode]=useState<'browse'|'tasks'|'recent'>('browse');
  const [label,setLabel]=useState('');
  const [draft,setDraft]=useState('');
  const [query,setQuery]=useState('');
  const [inactive,setInactive]=useState(false);
  const [offset,setOffset]=useState(0);
  const [generation,setGeneration]=useState(0);
  const [result,setResult]=useState<MemoryList|null>(null);
  const [loading,setLoading]=useState(false);
  const [error,setError]=useState('');
  const [selected,setSelected]=useState<RecordIdentity|null>(initial ?? null);
  useEffect(()=>{
    if(!schema) return;
    let alive=true;
    setLoading(true); setError(''); setResult(null);
    void client.list({view:mode,label:label||null,query,include_inactive:inactive,offset}).then(value=>{
      if(alive) {
        if(value.offset>0 && !value.items.length) setOffset(Math.max(0,Math.floor((value.total-1)/30)*30));
        else setResult(value);
      }
    }).catch(e=>{if(alive) setError(toFailure(e).message);}).finally(()=>{if(alive) setLoading(false);});
    return ()=>{alive=false;};
  },[client,schema,mode,label,query,inactive,offset,generation]);
  const changeMode=(value:typeof mode)=>{setMode(value);setOffset(0);setLabel('');};
  const refresh=()=>{setGeneration(n=>n+1);onRefreshSchema();};
  return <main className="memory-workspace">
    <div className="workspace-heading"><div><span className="eyebrow">PROJECT CONTEXT</span><h1>What this project remembers</h1><p>Find a decision, inspect its evidence, and keep useful knowledge current.</p></div><button onClick={refresh}>Refresh memories</button></div>
    <div className="memory-toolbar">
      <div className="section-tabs" aria-label="Memory views">
        <button aria-pressed={mode==='browse'} onClick={()=>changeMode('browse')}>Memories</button>
        <button aria-pressed={mode==='tasks'} onClick={()=>changeMode('tasks')}>Open tasks</button>
        <button aria-pressed={mode==='recent'} onClick={()=>changeMode('recent')}>Recent changes</button>
      </div>
      <form className="memory-search" onSubmit={e=>{e.preventDefault();setQuery(draft.trim());setOffset(0);}}>
        <input aria-label="Search memories" placeholder="Search saved text…" value={draft} onChange={e=>setDraft(e.target.value)} maxLength={256}/><button type="submit">Find</button>
      </form>
      <select aria-label="Memory type" value={label} onChange={e=>{setLabel(e.target.value);setOffset(0);}}>
        <option value="">{mode==='tasks'?'Task records':'All memory types'}</option>
        {schema?.node_tables.filter(t=>mode!=='tasks'||t.name.toLowerCase()==='task').map(t=><option key={t.name} value={t.name}>{t.name} ({t.row_count ?? '?'})</option>)}
      </select>
      <label className="checkbox"><input type="checkbox" checked={inactive} onChange={e=>{setInactive(e.target.checked);setOffset(0);}}/> Include inactive evidence</label>
    </div>
    <p className="view-explainer">{mode==='recent'?'Latest recorded change per memory, with creation time for records without history. Raw writes and re-ingestion are not a complete audit trail.':mode==='tasks'?'Tasks marked open, todo, pending, ready, active, in progress or blocked. Other status conventions remain available under Memories → Task.':'All memory types excludes standard code/document labels and managed source records. Choose a type to inspect any project vocabulary.'}</p>
    {schemaError && !schema && <div className="inline-error" role="alert">Schema unavailable: {schemaError}</div>}
    <div className="memory-columns">
      <section className="memory-list" aria-label="Memory results">
        <div className="list-heading"><strong>{loading?'Loading…':result?`${result.total} matching records`:'Records'}</strong><span className="muted">{result && `Code index: ${result.freshness.status}`}</span></div>
        {error && <div className="inline-error" role="alert">{error}<p>Narrow the type or search text if a resource limit was reached.</p></div>}
        {result?.items.length===0 && <div className="memory-empty"><h2>{query||label||mode!=='browse'?'No matching records':'No authored memories found'}</h2><p>{query||label||mode!=='browse'?'Try a different type, status view or search, or include inactive evidence.':'Code and document indexing can exist without saved decisions or concepts. Ask your agent to record useful findings with their sources as you work.'}</p><p className="muted">An empty result does not establish that the project has no decisions.</p></div>}
        {result?.items.map(item=><button className={`memory-card ${selected?.id===item.id?'selected':''}`} key={item.id} onClick={()=>setSelected({id:item.id,label:item.label,key:item.key})}>
          <div className="card-meta"><span className="eyebrow">{item.label}</span><span>{item.status || (item.managed && (!item.review || item.review==='unreviewed') ? 'Indexed source' : item.review || 'Unreviewed')}</span></div>
          <h2>{item.title}</h2><p>{item.preview || 'No text summary recorded.'}</p>
          {item.excluded_reason && <span className="evidence-badge warning">{item.excluded_reason.replace(/_/g,' ')}</span>}
          <small>{mode==='recent' && item.changed_at ? `${item.change_kind==='recorded'?'Changed':'Created'} ${new Date(item.changed_at).toLocaleString()}` : item.source ? 'Source recorded · open to inspect' : 'No source recorded'}</small>
        </button>)}
        {result && <div className="pagination"><button disabled={offset===0||loading} onClick={()=>setOffset(Math.max(0,offset-30))}>Previous</button><span>{result.total?`${offset+1}–${offset+result.items.length} of ${result.total}`:'0 records'}</span><button disabled={result.next_offset==null||loading} onClick={()=>setOffset(result.next_offset!)}>Next</button></div>}
      </section>
      {selected && schema ? <MemoryDetail key={selected.id} identity={selected} schema={schema} onExplore={onExplore} onSelect={setSelected} onBeginSave={onBeginSave} onChanged={()=>setGeneration(n=>n+1)}/> : <div className="detail-placeholder"><span className="eyebrow">EVIDENCE FIRST</span><h2>A memory is useful when you can examine it.</h2><p>Select a record to read its content, follow sources and relationships, or inspect corrections and review history.</p><div className="placeholder-steps"><span>01 · Read the claim</span><span>02 · Check the evidence</span><span>03 · Review or correct</span></div></div>}
    </div>
  </main>;
}
