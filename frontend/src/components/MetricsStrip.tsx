import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { MetricsFrame } from '../api/client';
import { formatBytes, formatCount, formatKB, formatRate, formatUptime } from '../lib/format';
import { useLive } from '../store/live';
import { themeColor, useUi } from '../store/ui';

// Loose agent frame shapes (vary by platform)
interface SysFrame extends MetricsFrame {
  cpu?: { overall_pct?: number; per_core?: number[]; freq_mhz?: number | number[];
          num_cores?: number };
  mem?: { used_pct?: number; total_kb?: number; used_kb?: number; cached_kb?: number;
          buffers_kb?: number; swap_total_kb?: number; swap_used_kb?: number };
  load?: { avg_1m?: number; avg_5m?: number; avg_15m?: number };
  temp_c?: number | null;
  uptime_sec?: number;
  context_switches?: number;
  interrupts?: number;
  procs_running?: number;
  procs_blocked?: number;
}

interface ProcFrame extends MetricsFrame {
  pid?: number; comm?: string; cpu_pct?: number; rss_kb?: number; state?: string;
  threads?: number; fds?: number; vsize_kb?: number;
  minor_faults?: number; major_faults?: number;
  voluntary_csw?: number; involuntary_csw?: number; oom_score?: number;
}

interface NetFrame extends MetricsFrame {
  interfaces?: Record<string, {
    rx_bytes: number; tx_bytes: number; rx_packets: number; tx_packets: number;
    rx_drops?: number; rx_errors?: number; tx_drops?: number; tx_errors?: number;
  }>;
}

interface DiskFrame extends MetricsFrame {
  devices?: Record<string, {
    read_bytes: number; write_bytes: number; reads: number; writes: number;
  }>;
  proc?: { read_bytes: number; write_bytes: number };
}

function severity(key: string, value: number | null | undefined): '' | 'severity-warning' | 'severity-critical' {
  const t: Record<string, { w: number; c: number }> = {
    cpu_pct: { w: 80, c: 95 }, mem_pct: { w: 85, c: 95 },
    temp_c: { w: 80, c: 95 }, proc_cpu: { w: 80, c: 95 },
    oom_score: { w: 500, c: 800 },
  };
  const th = t[key];
  if (!th || value == null) return '';
  if (value >= th.c) return 'severity-critical';
  if (value >= th.w) return 'severity-warning';
  return '';
}

function CardSparkline({ data, colorVar, min, max }:
    { data: (number | null | undefined)[]; colorVar: string;
      min: number | null; max: number | null }) {
  const vals = data.filter((v): v is number => v != null);
  if (vals.length < 2) return null;
  const w = 120, h = 24;
  const mn = min ?? Math.min(...vals);
  const mx = max ?? Math.max(...vals);
  const range = mx - mn || 1;
  const pts = vals.map((v, i) => {
    const x = (i / (vals.length - 1)) * w;
    const y = h - ((v - mn) / range) * (h - 2) - 1;
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke={`var(${colorVar})`} strokeWidth={1.5} />
    </svg>
  );
}

interface PanelSpec {
  id: string;
  label: string;
  ts: (number | null)[];
  data: (number | null)[];
  colorVar: string;
  min: number | null;
  max: number | null;
  thresholds?: { value: number; colorVar: string }[];
}

/** Labeled sparkline with hover readout + drag-to-select time window. */
function SparkPanel({ spec }: { spec: PanelSpec }) {
  const chartRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState('');
  const [drag, setDrag] = useState<{ x0: number; x1: number } | null>(null);
  const isReplayMode = useLive((s) => s.isReplayMode);
  const timeWindow = useLive((s) => s.timeWindow);
  const setTimeWindow = useLive((s) => s.setTimeWindow);
  const theme = useUi((s) => s.theme);   // re-render sparkline colors on toggle
  void theme;

  // Align values and timestamps, dropping nulls
  const vals: number[] = [];
  const tss: (number | null)[] = [];
  spec.data.forEach((v, i) => {
    if (v != null) { vals.push(v); tss.push(spec.ts[i] ?? null); }
  });
  if (vals.length < 2) return null;

  const w = 300, h = 50;
  const mn = spec.min ?? Math.min(...vals);
  const mx = spec.max ?? Math.max(...vals);
  const range = mx - mn || 1;
  const pts = vals.map((v, i) => {
    const x = (i / (vals.length - 1)) * w;
    const y = h - 2 - ((v - mn) / range) * (h - 4);
    return x.toFixed(1) + ',' + y.toFixed(1);
  });
  const color = themeColor(spec.colorVar) || '#4ade80';

  const idxAtX = (clientX: number, rect: DOMRect) => {
    const idx = Math.round(((clientX - rect.left) / rect.width) * (vals.length - 1));
    return Math.max(0, Math.min(vals.length - 1, idx));
  };

  // Persistent overlay of the active window
  let winOverlay: { left: string; width: string } | null = null;
  if (timeWindow && tss.length >= 2 && tss[0] != null && tss[tss.length - 1] != null) {
    const t0 = tss[0]!;
    const t1 = tss[tss.length - 1]!;
    if (t1 > t0) {
      const a = Math.max(0, (timeWindow.start - t0) / (t1 - t0));
      const b = Math.min(1, (timeWindow.end - t0) / (t1 - t0));
      if (b > 0 && a < 1 && b > a) {
        winOverlay = { left: a * 100 + '%', width: (b - a) * 100 + '%' };
      }
    }
  }

  return (
    <div className="spark-panel" id={spec.id}>
      <div className="sp-label">{spec.label}</div>
      <div className="sp-chart" ref={chartRef}
           onMouseMove={(ev) => {
             const r = chartRef.current?.getBoundingClientRect();
             if (!r || r.width <= 0) return;
             const idx = idxAtX(ev.clientX, r);
             const v = vals[idx];
             let ago = '';
             const last = tss[tss.length - 1];
             if (tss[idx] != null && last != null) {
               const agoSec = Math.round(last - tss[idx]!);
               ago = agoSec > 0 ? `  (${agoSec}s ago)` : '  (now)';
             }
             setHover(v.toFixed(1) + ago);
             if (drag) setDrag({ ...drag, x1: ev.clientX });
           }}
           onMouseDown={(ev) => {
             if (isReplayMode) return;
             setDrag({ x0: ev.clientX, x1: ev.clientX });
             ev.preventDefault();
           }}
           onMouseUp={(ev) => {
             if (!drag) return;
             const x0 = Math.min(drag.x0, ev.clientX);
             const x1 = Math.max(drag.x0, ev.clientX);
             setDrag(null);
             if (x1 - x0 < 8) return;  // click, not a drag
             const r = chartRef.current?.getBoundingClientRect();
             if (!r || tss.length < 2 || tss[0] == null) return;
             const t0 = tss[idxAtX(x0, r)];
             const t1 = tss[idxAtX(x1, r)];
             if (t0 != null && t1 != null && t1 > t0) {
               setTimeWindow({ start: t0, end: t1 });
             }
           }}
           onMouseLeave={() => { setHover(''); setDrag(null); }}>
        <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
          {(spec.thresholds ?? []).map((t, i) => {
            const ty = h - 2 - ((t.value - mn) / range) * (h - 4);
            if (ty <= 0 || ty >= h) return null;
            return <rect key={i} x={0} y={0} width={w} height={ty}
                         fill={themeColor(t.colorVar)} />;
          })}
          <polygon points={`0,${h} ${pts.join(' ')} ${w},${h}`} fill={color + '15'} />
          <polyline points={pts.join(' ')} fill="none" stroke={color} strokeWidth={1.5} />
        </svg>
        {winOverlay && <div className="sp-select" style={winOverlay}></div>}
        {drag && chartRef.current && (
          <div className="sp-select sp-select-temp" style={{
            left: Math.max(0, Math.min(drag.x0, drag.x1) -
                           chartRef.current.getBoundingClientRect().left) + 'px',
            width: Math.abs(drag.x1 - drag.x0) + 'px',
          }}></div>
        )}
      </div>
      <div className="sp-hover">{hover}</div>
    </div>
  );
}

function MetricsSettingsPop({ onClose }: { onClose: () => void }) {
  const [enabled, setEnabled] = useState(true);
  const [network, setNetwork] = useState(true);
  const [disk, setDisk] = useState(false);
  const [threads, setThreads] = useState(false);
  const [interval, setIntervalSec] = useState('2');
  const [status, setStatus] = useState<{ text: string; cls: string }>({ text: '', cls: '' });

  useEffect(() => {
    // An argless configure_metrics reads current settings without changes
    api.agentCommand('configure_metrics').then((data) => {
      if (!data.ok) return;
      if (data.metrics_enabled !== undefined) setEnabled(!!data.metrics_enabled);
      if (data.network !== undefined) setNetwork(!!data.network);
      if (data.disk !== undefined) setDisk(!!data.disk);
      if (data.threads !== undefined) setThreads(!!data.threads);
      if (data.interval) setIntervalSec(String(data.interval));
    }).catch(() => {});
  }, []);

  useEffect(() => {
    const close = () => onClose();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('click', close);
      document.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  const apply = () => {
    setStatus({ text: 'Applying...', cls: '' });
    api.agentCommand('configure_metrics', {
      enabled, network, disk, threads, interval: parseInt(interval) || 2,
    }).then((data) => {
      if (data.ok) {
        setStatus({ text: 'Applied', cls: 'ok' });
        // Older agents ignore unknown args — reflect what came back
        if (data.disk === false || data.disk === undefined) {
          useLive.setState({ metricsDisk: null, metricsPrevDisk: null });
        }
        if (data.network === false) {
          useLive.setState({ metricsNetwork: null, metricsPrevNetwork: null });
        }
        if (data.threads === false || data.threads === undefined) {
          useLive.setState({ metricsThreads: null, metricsPrevThreads: null,
                             threadLiveCpu: {} });
        }
      } else {
        setStatus({ text: data.error || 'No agent connected', cls: 'error' });
      }
    }).catch(() => setStatus({ text: 'No agent connected', cls: 'error' }));
  };

  return (
    <div id="metrics-settings-pop" className="metrics-settings-pop"
         onClick={(e) => e.stopPropagation()}>
      <div className="msp-title">Agent metrics collection</div>
      <label className="msp-row">
        <input type="checkbox" id="msp-enabled" checked={enabled}
               onChange={(e) => setEnabled(e.target.checked)} /> Metrics collection{' '}
        <span className="msp-hint">(master)</span>
      </label>
      <label className="msp-row">
        <input type="checkbox" id="msp-network" checked={network}
               onChange={(e) => setNetwork(e.target.checked)} /> Network stats
      </label>
      <label className="msp-row">
        <input type="checkbox" id="msp-disk" checked={disk}
               onChange={(e) => setDisk(e.target.checked)} /> Disk I/O{' '}
        <span className="msp-hint">(off by default)</span>
      </label>
      <label className="msp-row">
        <input type="checkbox" id="msp-threads" checked={threads}
               onChange={(e) => setThreads(e.target.checked)} /> Per-thread CPU{' '}
        <span className="msp-hint">(off by default)</span>
      </label>
      <label className="msp-row">Interval{' '}
        <select id="msp-interval" value={interval}
                onChange={(e) => setIntervalSec(e.target.value)}>
          <option value="1">1s</option>
          <option value="2">2s</option>
          <option value="5">5s</option>
          <option value="10">10s</option>
        </select>
      </label>
      <div className="msp-actions">
        <button id="msp-apply" className="wiz-btn wiz-btn-primary" onClick={apply}>Apply</button>
        <span id="msp-status" className={'msp-status ' + status.cls}>{status.text}</span>
      </div>
    </div>
  );
}

export default function MetricsStrip() {
  const metricsVisible = useLive((s) => s.metricsVisible);
  const metricsSystem = useLive((s) => s.metricsSystem) as SysFrame[];
  const metricsProcess = useLive((s) => s.metricsProcess) as ProcFrame[];
  const metricsNetwork = useLive((s) => s.metricsNetwork) as NetFrame | null;
  const metricsPrevNetwork = useLive((s) => s.metricsPrevNetwork) as NetFrame | null;
  const metricsDisk = useLive((s) => s.metricsDisk) as DiskFrame | null;
  const metricsPrevDisk = useLive((s) => s.metricsPrevDisk) as DiskFrame | null;
  const platform = useLive((s) => s.platform) as {
    arch?: string; kernel?: string; perf_version?: string;
  };
  const collapseLevel = useLive((s) => s.metricsCollapseLevel);
  const [settingsOpen, setSettingsOpen] = useState(false);

  if (!metricsVisible) return null;

  const sys = metricsSystem.length ? metricsSystem[metricsSystem.length - 1] : null;
  const proc = metricsProcess.length ? metricsProcess[metricsProcess.length - 1] : null;

  const cpuPct = sys?.cpu?.overall_pct;
  const memPct = sys?.mem?.used_pct;
  const sysTs = metricsSystem.map((s) => s.ts ?? null);
  const procTs = metricsProcess.map((p) => p.ts ?? null);

  const panels: PanelSpec[] = [
    { id: 'sp-cpu', label: 'CPU %', ts: sysTs,
      data: metricsSystem.map((s) => s.cpu?.overall_pct ?? 0),
      colorVar: '--spark-cpu', min: 0, max: 100,
      thresholds: [{ value: 80, colorVar: '--spark-warn-bg' },
                   { value: 95, colorVar: '--spark-crit-bg' }] },
    { id: 'sp-mem', label: 'Memory %', ts: sysTs,
      data: metricsSystem.map((s) => s.mem?.used_pct ?? 0),
      colorVar: '--spark-mem', min: 0, max: 100,
      thresholds: [{ value: 85, colorVar: '--spark-warn-bg' },
                   { value: 95, colorVar: '--spark-crit-bg' }] },
    { id: 'sp-temp', label: 'Temperature', ts: sysTs,
      data: metricsSystem.map((s) => s.temp_c ?? 0),
      colorVar: '--spark-temp', min: 20, max: 110,
      thresholds: [{ value: 80, colorVar: '--spark-warn-bg' },
                   { value: 95, colorVar: '--spark-crit-bg' }] },
  ];
  if (metricsProcess.length > 1) {
    panels.push({ id: 'sp-proc-cpu', label: 'Process CPU %', ts: procTs,
      data: metricsProcess.map((p) => p.cpu_pct ?? 0),
      colorVar: '--spark-proc-cpu', min: 0, max: 100,
      thresholds: [{ value: 80, colorVar: '--spark-warn-bg' }] });
    panels.push({ id: 'sp-proc-rss', label: 'Process RSS (MB)', ts: procTs,
      data: metricsProcess.map((p) => (p.rss_kb ?? 0) / 1024),
      colorVar: '--spark-proc-rss', min: 0, max: null });
  }

  const platformText = [platform.arch, platform.kernel, platform.perf_version]
    .filter(Boolean).join(' │ ');

  const netIfaces = metricsNetwork?.interfaces ?? {};
  const showDetail = collapseLevel === 0;

  return (
    <div id="metrics-strip"
         className={'metrics-strip' +
           (collapseLevel === 1 ? ' compact' : collapseLevel === 2 ? ' minimal' : '')}>
      <div className="metrics-header">
        <span className="metrics-title">Device Health</span>
        <span className="metrics-platform" id="metrics-platform">{platformText}</span>
        <button id="metrics-settings-btn" className="metrics-collapse" title="Metrics settings"
                onClick={(e) => { e.stopPropagation(); setSettingsOpen(!settingsOpen); }}>
          &#9881;
        </button>
        <button id="metrics-collapse-btn" className="metrics-collapse"
                onClick={() => useLive.setState({
                  metricsCollapseLevel: (collapseLevel + 1) % 3 })}>
          {collapseLevel === 1 ? '▶' : collapseLevel === 2 ? '▲' : '▼'}
        </button>
      </div>
      {settingsOpen && <MetricsSettingsPop onClose={() => setSettingsOpen(false)} />}
      <div className="metrics-cards" id="metrics-cards">
        <div className={'metric-card ' + severity('cpu_pct', cpuPct)} id="mc-cpu">
          <div className="metric-value" id="mv-cpu">
            {cpuPct != null ? cpuPct.toFixed(1) + '%' : '--'}
          </div>
          <div className="metric-label">CPU</div>
          <div className="metric-spark" id="ms-cpu">
            <CardSparkline data={metricsSystem.map((s) => s.cpu?.overall_pct)}
                           colorVar="--spark-cpu" min={0} max={100} />
          </div>
        </div>
        <div className={'metric-card ' + severity('mem_pct', memPct)} id="mc-mem">
          <div className="metric-value" id="mv-mem">
            {memPct != null ? memPct.toFixed(1) + '%' : '--'}
          </div>
          <div className="metric-label">MEM</div>
          <div className="metric-spark" id="ms-mem">
            <CardSparkline data={metricsSystem.map((s) => s.mem?.used_pct)}
                           colorVar="--spark-mem" min={0} max={100} />
          </div>
        </div>
        <div className={'metric-card ' + severity('temp_c', sys?.temp_c)} id="mc-temp">
          <div className="metric-value" id="mv-temp">
            {sys?.temp_c != null ? sys.temp_c + '°C' : '--'}
          </div>
          <div className="metric-label">Temp</div>
          <div className="metric-spark" id="ms-temp">
            <CardSparkline data={metricsSystem.map((s) => s.temp_c)}
                           colorVar="--spark-temp" min={20} max={110} />
          </div>
        </div>
        <div className="metric-card" id="mc-load">
          <div className="metric-value" id="mv-load">
            {sys?.load?.avg_1m != null ? sys.load.avg_1m.toFixed(2) : '--'}
          </div>
          <div className="metric-label">Load</div>
          <div className="metric-spark" id="ms-load">
            <CardSparkline data={metricsSystem.map((s) => s.load?.avg_1m)}
                           colorVar="--spark-load" min={0} max={null} />
          </div>
        </div>
        <div className={'metric-card ' + severity('proc_cpu', proc?.cpu_pct)} id="mc-proc">
          <div className="metric-value" id="mv-proc">
            {proc ? [
              proc.cpu_pct != null ? 'CPU:' + proc.cpu_pct.toFixed(1) + '%' : '',
              proc.rss_kb ? 'RSS:' + formatKB(proc.rss_kb) : '',
            ].filter(Boolean).join(' ') || '--' : '--'}
          </div>
          <div className="metric-label" id="ml-proc">
            {proc?.comm ? proc.comm + ' (' + proc.pid + ')' : 'Process'}
          </div>
          <div className="metric-spark" id="ms-proc-cpu">
            <CardSparkline data={metricsProcess.map((p) => p.cpu_pct)}
                           colorVar="--spark-proc-cpu" min={0} max={100} />
          </div>
        </div>
      </div>
      <div className="metrics-detail" id="metrics-detail">
        <div className="metrics-sparklines" id="metrics-sparklines">
          {showDetail && panels.map((p) => <SparkPanel key={p.id} spec={p} />)}
        </div>
        <div className="metrics-extras" id="metrics-extras">
          <div className="metrics-sys-detail" id="metrics-sys-detail">
            {showDetail && sys && <SystemDetails sys={sys} />}
          </div>
          <div className="metrics-proc-detail" id="metrics-proc-detail">
            {showDetail && proc && <ProcessDetails proc={proc} />}
          </div>
        </div>
        <div className="metrics-network" id="metrics-network">
          {showDetail && Object.entries(netIfaces).map(([name, c]) => {
            let rateStr = '';
            const prev = metricsPrevNetwork;
            if (prev?.interfaces?.[name] && metricsNetwork) {
              const p = prev.interfaces[name];
              const dt = metricsNetwork.ts - prev.ts;
              if (dt > 0) {
                const rx = Math.round((c.rx_bytes - p.rx_bytes) / dt);
                const tx = Math.round((c.tx_bytes - p.tx_bytes) / dt);
                rateStr = ` (${formatRate(rx)} in, ${formatRate(tx)} out)`;
              }
            }
            const rxExtra = [
              (c.rx_drops ?? 0) > 0 ? c.rx_drops + ' drops' : '',
              (c.rx_errors ?? 0) > 0 ? c.rx_errors + ' errs' : '',
            ].filter(Boolean).join(', ');
            const txExtra = [
              (c.tx_drops ?? 0) > 0 ? c.tx_drops + ' drops' : '',
              (c.tx_errors ?? 0) > 0 ? c.tx_errors + ' errs' : '',
            ].filter(Boolean).join(', ');
            return (
              <div className="net-iface" key={name}>
                <span className="net-label">{name}</span>: RX {formatBytes(c.rx_bytes)}{' '}
                ({c.rx_packets} pkts{rxExtra ? ', ' + rxExtra : ''}){' '}
                TX {formatBytes(c.tx_bytes)} ({c.tx_packets} pkts
                {txExtra ? ', ' + txExtra : ''}){rateStr}
              </div>
            );
          })}
        </div>
        <div className="metrics-network metrics-disk" id="metrics-disk">
          {showDetail && metricsDisk && <DiskPanel cur={metricsDisk} prev={metricsPrevDisk} />}
        </div>
      </div>
    </div>
  );
}

function DiskPanel({ cur, prev }: { cur: DiskFrame; prev: DiskFrame | null }) {
  const devices = cur.devices ?? {};
  const dt = prev ? cur.ts - prev.ts : 0;
  return (
    <>
      {Object.entries(devices).map(([name, c]) => {
        let rateStr = '';
        if (dt > 0 && prev?.devices?.[name]) {
          const p = prev.devices[name];
          const rd = Math.max(0, Math.round((c.read_bytes - p.read_bytes) / dt));
          const wr = Math.max(0, Math.round((c.write_bytes - p.write_bytes) / dt));
          const iops = Math.max(0,
            Math.round(((c.reads - p.reads) + (c.writes - p.writes)) / dt));
          rateStr = ` (${formatRate(rd)} read, ${formatRate(wr)} write, ${iops} IOPS)`;
        }
        return (
          <div className="net-iface" key={name}>
            <span className="net-label">{name}</span>: read {formatBytes(c.read_bytes)} /
            write {formatBytes(c.write_bytes)}{rateStr}
          </div>
        );
      })}
      {cur.proc && (() => {
        let procRate = '';
        if (dt > 0 && prev?.proc) {
          procRate = ` (${formatRate(Math.max(0,
            Math.round((cur.proc.read_bytes - prev.proc.read_bytes) / dt)))} read, ` +
            `${formatRate(Math.max(0,
            Math.round((cur.proc.write_bytes - prev.proc.write_bytes) / dt)))} write)`;
        }
        return (
          <div className="net-iface">
            <span className="net-label">process</span>: read {formatBytes(cur.proc.read_bytes)} /
            write {formatBytes(cur.proc.write_bytes)}{procRate}
          </div>
        );
      })()}
    </>
  );
}

function SystemDetails({ sys }: { sys: SysFrame }) {
  const cpu = sys.cpu ?? {};
  const mem = sys.mem ?? {};
  const load = sys.load ?? {};
  let freq = cpu.freq_mhz;
  if (Array.isArray(freq)) freq = freq.reduce((a, b) => a + b, 0) / freq.length;
  return (
    <>
      {cpu.per_core && cpu.per_core.length > 0 && (
        <div className="detail-section">
          <span className="detail-label">Per-Core CPU</span>
          <div className="core-bars">
            {cpu.per_core.map((pct, i) => (
              <div className="core-bar" key={i} title={`Core ${i}: ${pct.toFixed(0)}%`}>
                <div className={'core-fill core-' +
                       (pct >= 95 ? 'crit' : pct >= 80 ? 'warn' : 'ok')}
                     style={{ height: Math.max(pct, 2) + '%' }}></div>
              </div>
            ))}
          </div>
          {freq != null && <span className="detail-note">{(freq / 1000).toFixed(2)} GHz</span>}
          {cpu.num_cores != null && <span className="detail-note">{cpu.num_cores} cores</span>}
        </div>
      )}
      {mem.total_kb != null && (() => {
        const used = mem.used_kb ?? 0;
        const cached = mem.cached_kb ?? 0;
        const buffers = mem.buffers_kb ?? 0;
        const total = mem.total_kb!;
        return (
          <div className="detail-section">
            <span className="detail-label">Memory</span>
            <div className="mem-bar-wrap">
              <div className="mem-bar">
                <div className="mem-seg mem-used"
                     style={{ width: ((used / total) * 100).toFixed(0) + '%' }}
                     title={'Used: ' + formatKB(used)}></div>
                <div className="mem-seg mem-cached"
                     style={{ width: ((cached / total) * 100).toFixed(0) + '%' }}
                     title={'Cache: ' + formatKB(cached)}></div>
                <div className="mem-seg mem-buffers"
                     style={{ width: ((buffers / total) * 100).toFixed(0) + '%' }}
                     title={'Buffers: ' + formatKB(buffers)}></div>
              </div>
            </div>
            <span className="detail-note">{formatKB(used)} used</span>
            <span className="detail-note">{formatKB(cached)} cache</span>
            <span className="detail-note">{formatKB(buffers)} buf</span>
            <span className="detail-note">{formatKB(total)} total</span>
            {(mem.swap_total_kb ?? 0) > 0 && (
              <span className="detail-note">
                Swap: {formatKB(mem.swap_used_kb ?? 0)} / {formatKB(mem.swap_total_kb!)}{' '}
                ({(mem.swap_used_kb ? (mem.swap_used_kb / mem.swap_total_kb!) * 100 : 0).toFixed(0)}%)
              </span>
            )}
          </div>
        );
      })()}
      {load.avg_1m != null && (
        <div className="detail-section">
          <span className="detail-label">Load</span>
          <span className="detail-val">{load.avg_1m.toFixed(2)}</span>
          <span className="detail-val">{load.avg_5m != null ? load.avg_5m.toFixed(2) : '--'}</span>
          <span className="detail-val">{load.avg_15m != null ? load.avg_15m.toFixed(2) : '--'}</span>
          <span className="detail-note">1m / 5m / 15m</span>
          {sys.uptime_sec != null && (
            <span className="detail-note">Up: {formatUptime(sys.uptime_sec)}</span>
          )}
        </div>
      )}
      {(sys.context_switches != null || sys.interrupts != null) && (
        <div className="detail-section">
          <span className="detail-label">Scheduling</span>
          {sys.context_switches != null && (
            <span className="detail-note">Ctx: {formatCount(sys.context_switches)}</span>
          )}
          {sys.interrupts != null && (
            <span className="detail-note">IRQ: {formatCount(sys.interrupts)}</span>
          )}
          {sys.procs_running != null && (
            <span className="detail-note">Run: {sys.procs_running}</span>
          )}
          {(sys.procs_blocked ?? 0) > 0 && (
            <span className="detail-note sev-warn">Blocked: {sys.procs_blocked}</span>
          )}
        </div>
      )}
    </>
  );
}

function ProcessDetails({ proc }: { proc: ProcFrame }) {
  const oomCls = severity('oom_score', proc.oom_score) === 'severity-critical'
    ? ' sev-crit'
    : severity('oom_score', proc.oom_score) === 'severity-warning' ? ' sev-warn' : '';
  return (
    <>
      <div className="detail-section">
        <span className="detail-label">Process</span>
        {proc.comm && <span className="detail-val">{proc.comm} ({proc.pid})</span>}
        {proc.state && <span className="detail-note">State: {proc.state}</span>}
      </div>
      {(proc.threads != null || proc.fds != null || proc.vsize_kb != null) && (
        <div className="detail-section">
          <span className="detail-label">Resources</span>
          {proc.threads != null && <span className="detail-note">Threads: {proc.threads}</span>}
          {proc.fds != null && <span className="detail-note">FDs: {proc.fds}</span>}
          {proc.vsize_kb != null && (
            <span className="detail-note">VSize: {formatKB(proc.vsize_kb)}</span>
          )}
          {proc.rss_kb != null && (
            <span className="detail-note">RSS: {formatKB(proc.rss_kb)}</span>
          )}
        </div>
      )}
      {(proc.minor_faults != null || proc.major_faults != null) && (
        <div className="detail-section">
          <span className="detail-label">Page Faults</span>
          {proc.minor_faults != null && (
            <span className="detail-note">Minor: {formatCount(proc.minor_faults)}</span>
          )}
          {proc.major_faults != null && (
            <span className={'detail-note' + (proc.major_faults > 0 ? ' sev-warn' : '')}>
              Major: {formatCount(proc.major_faults)}
            </span>
          )}
        </div>
      )}
      {(proc.voluntary_csw != null || proc.involuntary_csw != null) && (
        <div className="detail-section">
          <span className="detail-label">Ctx Switches</span>
          {proc.voluntary_csw != null && (
            <span className="detail-note">Vol: {formatCount(proc.voluntary_csw)}</span>
          )}
          {proc.involuntary_csw != null && (
            <span className="detail-note">Invol: {formatCount(proc.involuntary_csw)}</span>
          )}
        </div>
      )}
      {proc.oom_score != null && (
        <div className="detail-section">
          <span className="detail-label">OOM</span>
          <span className={'detail-note' + oomCls}>Score: {proc.oom_score}</span>
        </div>
      )}
    </>
  );
}
