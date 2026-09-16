import {useEffect,useMemo,useState} from 'react';
import {memoryApi,toFailure} from '../api';
import type {IndexStatus,JobSummary} from '../types';

export function HealthPanel() {
  const client=useMemo(memoryApi,[]);
  const [index,setIndex]=useState<IndexStatus|null>(null);
  const [jobs,setJobs]=useState<JobSummary[]>([]);
  const [error,setError]=useState('');
  const [generation,setGeneration]=useState(0);
  const [loading,setLoading]=useState(false);
  useEffect(()=>{
    let alive=true;setLoading(true);setError('');
    void Promise.all([client.index(),client.jobs()]).then(([status,result])=>{if(alive){setIndex(status);setJobs(result.jobs);}})
      .catch(e=>{if(alive){setIndex(null);setJobs([]);setError(toFailure(e).message);}}).finally(()=>{if(alive)setLoading(false);});
    return ()=>{alive=false;};
  },[client,generation]);
  return <main className="health-workspace"><div className="workspace-heading"><div><span className="eyebrow">SELECTED DATABASE</span><h1>Index &amp; ingestion health</h1><p>Inspect the current owner's state. Code freshness does not certify saved memories.</p></div><button onClick={()=>setGeneration(n=>n+1)} disabled={loading}>{loading?'Checking…':'Refresh health'}</button></div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {index && <><div className="health-cards"><section><h2>Code index</h2><strong>{index.freshness.status}</strong><p>{index.running?'Verification or refresh is running.':'No source check currently running.'}</p><p className="muted">Last checked: {index.freshness.checked_at || 'Not verified'}</p></section>
      <section><h2>Optional embeddings</h2><strong>{index.embedding ? index.embedding.last_error?'Error':index.embedding.running?'Working':'Idle':'Not running'}</strong><p>{index.embedding?.last_error || (index.embedding ? `${index.embedding.embedded_total ?? 0} embedded by this worker.` : 'No background embedding worker is active. Keyword search does not require a model.')}</p></section>
      <section><h2>Owner scope</h2><strong className="scope-id">{index.database_id}</strong><p>These observations belong to the selected database.</p></section></div>
      <section className="health-section"><h2>Registered code roots</h2>{!index.roots?.length && <p>No code roots are registered. This does not imply an empty database.</p>}{index.roots?.map(root=><div className="root-row" key={root.path}><code>{root.path}</code><span className={root.error?'warning':'muted'}>{root.error || (root.unknown?'Scope needs verification':'Registered')}</span></div>)}</section></>}
    <section className="health-section"><h2>Recent ingestion jobs</h2><p className="muted">Up to 20 jobs retained by this server process. Restarting the owner clears this list.</p>{!jobs.length && !loading && !error && <p>No retained jobs.</p>}{jobs.map(job=><div className="root-row" key={job.id}><div><strong>{job.kind}</strong><small>{job.created_at}</small></div><span className={job.status==='failed'?'warning':''}>{job.status}</span>{job.error && <p className="inline-error">{job.error}</p>}</div>)}</section>
  </main>;
}
