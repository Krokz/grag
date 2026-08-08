import { useState } from 'react';
import type { NodeRecord } from '../types';
import { colorForLabel } from '../graph-utils';

interface Props {
  node: NodeRecord;
  pkMap: Map<string, string>;
  seed: { score: number; match: string } | undefined;
  onClose: () => void;
  onExpand: (node: NodeRecord) => void;
}

export function Inspector({ node, pkMap, seed, onClose, onExpand }: Props) {
  const [copied, setCopied] = useState(false);
  const pk = pkMap.get(node.label);
  const entries = Object.entries(node.properties).filter(([k]) => !k.startsWith('_'));

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
        </div>
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
        <pre className="raw">{JSON.stringify(node.properties, null, 2)}</pre>
      </div>
    </div>
  );
}
