import {useEffect, useMemo, useState} from 'react';
import {memoryApi, toFailure} from '../api';
import type {FreshnessReport, IndexObservation} from '../types';

/** Observe the owner without scheduling new checks or relabelling old graph reads. */
export function IndexFreshness({lastRead, readNotice}: {lastRead:FreshnessReport|null; readNotice:string}) {
  const client=useMemo(memoryApi,[]);
  const [observation,setObservation]=useState<IndexObservation|null>(null);
  const [error,setError]=useState('');
  useEffect(()=>{
    const controller=new AbortController();
    let timer:ReturnType<typeof setTimeout>;
    setObservation(null);setError('');
    const observe=async()=>{
      let delay=15000;
      try {
        if(document.hidden) return;
        const value=await client.observeIndex(controller.signal);
        if(controller.signal.aborted) return;
        setObservation(value);setError('');
        if(value.index?.running || ['checking','refreshing'].includes(value.index?.freshness.status ?? '')) delay=1000;
      } catch(e) {
        if(!controller.signal.aborted) {setObservation(null);setError(toFailure(e).message);}
      } finally {
        if(!controller.signal.aborted) timer=setTimeout(()=>void observe(),delay);
      }
    };
    void observe();
    return ()=>{controller.abort();clearTimeout(timer);};
  },[client,lastRead]);
  const current=observation?.index?.freshness;
  const status=error?'unavailable':observation
    ? !observation.loaded?'not loaded':current?.status ?? 'disabled'
    : 'loading status';
  const title=[
    error || `Latest observed code-index status: ${status}; checked ${current?.checked_at ?? 'not yet'}.`,
    observation?.index?.last_error,
    lastRead && `Last graph/schema read: ${lastRead.status}${lastRead.timed_out?' (wait expired)':''}; checked ${lastRead.checked_at ?? 'not yet'}.`,
    readNotice,
    'The displayed graph is not automatically reloaded when verification finishes.',
  ].filter(Boolean).join('\n');
  return <span className="health" aria-label="Code index status" title={title}>index: {status}</span>;
}
