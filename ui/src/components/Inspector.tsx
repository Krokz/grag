import { useState } from 'react';
import type { NodeRecord } from '../types';
import { colorForLabel } from '../graph-utils';
import {Coverage, EvidenceBadges, SourceCitation} from './RecordFacts';

interface Props {
  node: NodeRecord;
  pkMap: Map<string, string>;
  seed: { score: number; match: string } | undefined;
  onClose: () => void;
  onExpand: (node: NodeRecord) => void;
  onInspect: (node: NodeRecord) => void;
}

export function Inspector({ node, pkMap, seed, onClose, onExpand, onInspect }: Props) {
  const [copied, setCopied] = useState(false);
  const pk = pkMap.get(node.label);
  const entries = Object.entries(node.properties).filter(([k]) => !k.startsWith('_') && k !== 'code_coverage');

  const copyId = async () => {
    try {
      await navigator.clipboard.writeText(node.id);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = node.id;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div className="inspector">
      <div className="insp-head">
        <span className="label-chip" style={{ background: colorForLabel(node.label) }}>
          {node.label}
        </span>
        <button className="close" onClick={onClose} title="close">
          ✕
        </button>
      </div>
      <div className="insp-body">
        <div className="insp-id">{node.id}</div>
        {seed && (
          <div className="seed-note">
            search seed — score {seed.score.toFixed(3)} ({seed.match})
          </div>
        )}
        <div className="insp-actions">
          <button onClick={copyId}>{copied ? '✓ copied' : 'Copy node id'}</button>
          <button onClick={() => onExpand(node)} title="double-click a node does the same">
            Expand neighbors
          </button>
          <button onClick={() => onInspect(node)}>Evidence &amp; history</button>
        </div>
        <EvidenceBadges node={node}/>
        <SourceCitation source={node.properties._source} properties={node.properties}/>
        <Coverage value={node.properties.code_coverage}/>
        {entries.length > 0 && (
          <table className="kv">
            <tbody>
              {entries.map(([k, v]) => (
                <tr key={k} className={k === pk ? 'pk' : ''}>
                  <td>{k}</td>
                  <td>{typeof v === 'string' ? v : JSON.stringify(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <details><summary>Raw properties</summary><pre className="raw">{JSON.stringify(node.properties, null, 2)}</pre></details>
      </div>
    </div>
  );
}
