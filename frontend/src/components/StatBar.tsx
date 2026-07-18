import { useLive } from '../store/live';
import { formatNumber, formatStatValue } from '../lib/format';

const STAT_ORDER = [
  'ipc', 'cycles', 'instructions',
  'cache-misses', 'cache-references', 'cache_miss_rate',
  'branch-misses', 'branch-instructions', 'branch_miss_rate',
  'page-faults', 'context-switches', 'cpu-migrations', 'task-clock',
];

const STAT_LABELS: Record<string, string> = {
  ipc: 'IPC', cycles: 'Cycles', instructions: 'Instructions',
  'cache-misses': 'Cache Miss', 'cache-references': 'Cache Refs',
  cache_miss_rate: 'Cache Miss %',
  'branch-misses': 'Branch Miss', 'branch-instructions': 'Branches',
  branch_miss_rate: 'Br Miss %', 'page-faults': 'Page Faults',
  'context-switches': 'Ctx Switch', 'cpu-migrations': 'CPU Migr',
  'task-clock': 'Task Clock',
};

export default function StatBar() {
  const totalSamples = useLive((s) => s.totalSamples);
  const perfStat = useLive((s) => s.perfStat);
  const agentAddr = useLive((s) => s.agentAddr);
  const connected = useLive((s) => s.connected);

  const entries = Object.entries(perfStat)
    .filter(([k]) => k !== 'time_elapsed')
    .sort(([a], [b]) => {
      const ia = STAT_ORDER.indexOf(a);
      const ib = STAT_ORDER.indexOf(b);
      return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib);
    });

  return (
    <div id="perf-stat-bar">
      <div className="stat-card" id="stat-card-samples">
        <div className="stat-value" id="stat-samples">{formatNumber(totalSamples)}</div>
        <div className="stat-label">Samples</div>
      </div>
      {entries.map(([key, data]) => (
        <div className="stat-card stat-card-dynamic" key={key}>
          <div className="stat-value" title={data.comment || ''}>
            {formatStatValue(key, data.value)}
          </div>
          <div className="stat-label">{STAT_LABELS[key] || key}</div>
        </div>
      ))}
      <div className="stat-card" id="stat-card-agent">
        <div className="stat-value" id="stat-agent">{connected ? agentAddr : '--'}</div>
        <div className="stat-label">Agent</div>
      </div>
    </div>
  );
}
