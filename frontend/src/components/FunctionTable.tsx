import { useDeferredValue, useMemo, useState } from 'react';
import type { FunctionEntry, FunctionSummary } from '../api/client';
import { useLive } from '../store/live';
import { useUi } from '../store/ui';

/** Map key for a function across snapshots: name and module, joined by a
 * separator no symbol can contain. */
const KEY_SEP = String.fromCharCode(0);
export function fnKey(f: { name: string; module?: string | null }): string {
  return f.name + KEY_SEP + (f.module || '');
}

interface Props {
  data: FunctionSummary | null;
  /** fnKey(entry) -> baseline entry; null = diff off */
  baselineMap: Map<string, FunctionEntry> | null;
  onSelectFunction: (funcName: string) => void;
}

type SortKey = 'self' | 'total';

function fnSelfPct(f: FunctionEntry): number {
  return f.self_percent ?? f.percent ?? 0;
}

function DiffCell({ f, baselineMap }: { f: FunctionEntry;
                    baselineMap: Map<string, FunctionEntry> }) {
  const bf = baselineMap.get(fnKey(f));
  if (!bf) return <td className="diff-col"><span className="diff-new">new</span></td>;
  const d = fnSelfPct(f) - fnSelfPct(bf);
  if (Math.abs(d) < 0.05) {
    return <td className="diff-col"><span className="diff-flat">&plusmn;0.0</span></td>;
  }
  return (
    <td className="diff-col">
      <span className={d > 0 ? 'diff-up' : 'diff-down'}>
        {(d > 0 ? '+' : '') + d.toFixed(1)}pp
      </span>
    </td>
  );
}

export default function FunctionTable({ data, baselineMap, onSelectFunction }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('self');
  const [filter, setFilter] = useState('');
  // The filter follows the deferred value so typing stays responsive on
  // a table of thousands; the input itself keeps what was typed, spaces
  // included (trimming on change made a space impossible to type).
  const deferredFilter = useDeferredValue(filter);
  const term = deferredFilter.trim();
  const [showCount, setShowCount] = useState(200);
  const dark = useUi((s) => s.theme) === 'dark';
  // Reset progressive display on event/thread switches
  const resetKey = useLive((s) => s.selectedEvent) + ':' + useLive((s) => s.selectedTid);
  const [lastResetKey, setLastResetKey] = useState(resetKey);
  if (resetKey !== lastResetKey) {
    setLastResetKey(resetKey);
    setShowCount(200);
  }

  const filterRe = useMemo(() => {
    if (!term) return null;
    try { return new RegExp(term, 'i'); } catch { return undefined; }
  }, [term]);

  const sorted = useMemo(() => {
    if (!data?.functions) return [];
    const s = data.functions.slice().sort((a, b) => {
      if (sortKey === 'total') return (b.total_samples || 0) - (a.total_samples || 0);
      return (b.self_samples ?? b.samples) - (a.self_samples ?? a.samples);
    });
    if (!filterRe) return s;
    return s.filter((f) => filterRe.test(f.name) || filterRe.test(f.module || ''));
  }, [data, sortKey, filterRe]);

  const totalFunctions = data?.functions?.length ?? 0;
  const visible = sorted.slice(0, showCount);
  const remaining = sorted.length - visible.length;
  const maxSelfPct = sorted.reduce((m, f) => Math.max(m, fnSelfPct(f)), 0);
  const maxTotalPct = sorted.reduce((m, f) => Math.max(m, f.total_percent || 0), 0);

  const status = filterRe === undefined ? 'invalid regex'
    : totalFunctions === 0 ? ''
    : sorted.length === totalFunctions
      ? `${totalFunctions} functions`
      : `${sorted.length} of ${totalFunctions} functions`;

  return (
    <>
      <div className="fn-toolbar">
        <input type="text" id="fn-search" placeholder="Filter functions..."
               aria-label="Filter functions (regular expression)"
               value={filter}
               onChange={(e) => { setFilter(e.target.value); setShowCount(200); }} />
        <span id="fn-status" className={'fn-status' + (filterRe === undefined ? ' err' : '')}>
          {status}
        </span>
      </div>
      <table id="function-table" className={baselineMap ? 'diff-mode' : ''}>
        <thead>
          <tr>
            <th>#</th>
            <th>Function</th>
            <th>Module</th>
            <th className={'sortable' + (sortKey === 'self' ? ' active' : '')}
                data-sort="self" aria-sort={sortKey === 'self' ? 'descending' : 'none'}
                onClick={() => { setSortKey('self'); setShowCount(200); }}>
              Self %
            </th>
            <th className={'sortable' + (sortKey === 'total' ? ' active' : '')}
                data-sort="total" aria-sort={sortKey === 'total' ? 'descending' : 'none'}
                onClick={() => { setSortKey('total'); setShowCount(200); }}>
              Total %
            </th>
            <th className="diff-col">&Delta; Self</th>
            <th>Samples</th>
          </tr>
        </thead>
        <tbody id="function-tbody">
          {data === null && (
            // Loading skeleton: no snapshot fetched yet
            Array.from({ length: 8 }, (_, i) => (
              <tr key={'skel' + i} className="skeleton-row" aria-hidden="true">
                <td colSpan={7}><div className="skeleton-bar"
                    style={{ width: `${85 - i * 9}%` }}></div></td>
              </tr>
            ))
          )}
          {data !== null && visible.length === 0 && (
            <tr><td colSpan={7} className="empty">
              {totalFunctions === 0
                ? 'Waiting for samples — they appear a few seconds after collection starts'
                : 'No matching functions'}
            </td></tr>
          )}
          {visible.map((f, i) => {
            const selfPct = fnSelfPct(f);
            const totalPct = f.total_percent || 0;
            const selfSamples = f.self_samples ?? f.samples ?? 0;
            const totalSamples = f.total_samples || 0;
            const selfBarW = Math.max(2, (selfPct / Math.max(maxSelfPct, 1)) * 100);
            const selfHue = Math.max(0, 120 - (selfPct / Math.max(maxSelfPct, 1)) * 120);
            const selfColor = `hsl(${selfHue}, 70%, ${dark ? 45 : 50}%)`;
            const totalBarW = Math.max(2, (totalPct / Math.max(maxTotalPct, 1)) * 100);
            const totalColor = `hsl(210, 50%, ${dark ? 40 : 50}%)`;
            const moduleName = f.module ? f.module.split('/').pop() : '';
            return (
              <tr key={fnKey(f)} data-func={f.name} tabIndex={0}
                  onClick={() => onSelectFunction(f.name)}
                  onKeyDown={(e) => { if (e.key === 'Enter') onSelectFunction(f.name); }}>
                <td>{i + 1}</td>
                <td><strong>{f.name}</strong></td>
                <td title={f.module}>{moduleName}</td>
                <td>
                  <div className="cpu-bar">
                    <div className="cpu-bar-fill"
                         style={{ width: selfBarW + '%', background: selfColor }}></div>
                    <span className="cpu-bar-text">{selfPct.toFixed(1)}%</span>
                  </div>
                </td>
                <td>
                  <div className="cpu-bar total-bar">
                    <div className="cpu-bar-fill"
                         style={{ width: totalBarW + '%', background: totalColor }}></div>
                    <span className="cpu-bar-text">{totalPct.toFixed(1)}%</span>
                  </div>
                </td>
                {baselineMap
                  ? <DiffCell f={f} baselineMap={baselineMap} />
                  : <td className="diff-col"></td>}
                <td title={`self: ${selfSamples} / total: ${totalSamples}`}>{selfSamples}</td>
              </tr>
            );
          })}
          {remaining > 0 && (
            <tr className="fn-show-more-row">
              <td colSpan={7}>
                <button className="fn-show-more-btn" id="fn-show-more"
                        onClick={() => setShowCount(showCount + 200)}>
                  Show {Math.min(remaining, 200)} more ({remaining} remaining)
                </button>
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
