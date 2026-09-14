import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { AgentCommandResult } from '../api/client';
import { useLive } from '../store/live';
import { reportError, useUi } from '../store/ui';

interface AgentStatus extends AgentCommandResult {
  state?: string;
  pid?: number;
  frequency?: number;
  duration?: number;
  events?: string[];
  agent_version?: string;
  platform?: { perf_version?: string; perf_path?: string };
  capabilities?: {
    pipe_mode?: boolean;
    callgraph_method?: string | null;
    record_events?: string[];
  };
}

interface ProcEntry { pid: number; comm: string; cpu?: number; cmdline?: string }

/** Close a popover on an outside click or Escape. */
function useDismiss(onClose: () => void) {
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
}

/** The `start` arguments that keep an agent's current settings. */
function startArgs(agent: AgentStatus | null, pid: number): Record<string, unknown> {
  const args: Record<string, unknown> = {
    pid,
    frequency: agent?.frequency || 99,
    duration: agent?.duration || 8,
  };
  if (agent?.events?.length) args.events = agent.events;
  return args;
}

function SettingsPop({ agent, onClose, onApplied }:
    { agent: AgentStatus | null; onClose: () => void; onApplied: () => void }) {
  const [freq, setFreq] = useState(String(agent?.frequency || 99));
  const [dur, setDur] = useState(String(agent?.duration || 8));
  const caps = agent?.capabilities ?? {};
  const active = agent?.events ?? [];
  const [checked, setChecked] = useState<Set<string>>(() => new Set(
    (caps.record_events ?? []).filter(
      (e) => active.length === 0 || active.includes(e))));
  const [status, setStatus] = useState<{ text: string; cls: string }>({ text: '', cls: '' });
  const [perf, setPerf] = useState(agent?.platform?.perf_path || 'perf');
  const [perfStatus, setPerfStatus] =
    useState<{ text: string; cls: string }>({ text: '', cls: '' });

  useDismiss(onClose);

  const apply = () => {
    const f = parseInt(freq);
    const d = parseInt(dur);
    if (!(f >= 1 && f <= 10000) || !(d >= 1 && d <= 300)) {
      setStatus({ text: 'Frequency must be 1–10000 Hz and the interval 1–300 s', cls: 'err' });
      return;
    }
    const all = caps.record_events ?? [];
    const events = all.filter((e) => checked.has(e));
    if (all.length && events.length === 0) {
      setStatus({ text: 'Pick at least one record event', cls: 'err' });
      return;
    }
    const current = active.length ? active : all;
    const eventsChanged = events.length > 0 && events.join(',') !== current.join(',');
    const freqChanged = agent != null && f !== agent.frequency;
    const profiling = agent != null &&
      (agent.state === 'profiling' || agent.state === 'paused');

    setStatus({ text: 'Applying...', cls: '' });
    const done = (ok: boolean | undefined, msg?: string) => {
      setStatus({ text: ok ? 'Applied' : (msg || 'Failed'), cls: ok ? 'ok' : 'err' });
      if (ok) onApplied();
    };

    if (profiling && (eventsChanged || freqChanged)) {
      // Frequency/event changes need a restart of collection. The event
      // list is always explicit (one sampling event by default), so the
      // agent never has to guess what "all" means.
      const pid = agent!.pid;
      setStatus({ text: 'Restarting collection (re-probing the process)...', cls: '' });
      api.agentCommand('stop')
        .then(() => {
          const args: Record<string, unknown> = { pid, frequency: f, duration: d };
          if (events.length) args.events = events;
          return api.agentCommand('start', args, 120);
        })
        .then((data) => done(data.ok, data.error))
        .catch((err) => done(false, String(err instanceof Error ? err.message : err)));
    } else {
      api.agentCommand('configure', { frequency: f, duration: d })
        .then((data) => done(data.ok, data.error))
        .catch((err) => done(false, String(err instanceof Error ? err.message : err)));
    }
  };

  // A different perf means different probed capabilities, and the agent will
  // not swap it mid-collection -- so stop, adopt it, and start again on the
  // same pid, which re-probes with the new binary. A rejected path restarts
  // too, so a typo does not leave profiling stopped.
  const usePerf = () => {
    const path = perf.trim();
    if (!path) {
      setPerfStatus({ text: 'Enter a path', cls: 'err' });
      return;
    }
    const restart = agent?.pid != null &&
      (agent.state === 'profiling' || agent.state === 'paused');
    setPerfStatus({ text: restart ? 'Restarting with this perf...' : 'Verifying...', cls: '' });
    const verify = () => api.agentCommand('verify_perf', { perf: path });
    let run: Promise<{ v: AgentCommandResult; s: AgentCommandResult | null }>;
    if (restart) {
      run = api.agentCommand('stop').then(verify).then((v) =>
        api.agentCommand('start', startArgs(agent, agent!.pid!), 180).then((s) => ({ v, s })));
    } else {
      run = verify().then((v) => ({ v, s: null }));
    }
    run.then(({ v, s }) => {
      if (!v.ok || !v.available) {
        setPerfStatus({ text: String(v.error || 'perf not available'), cls: 'err' });
      } else if (s && !s.ok) {
        setPerfStatus({ text: String(s.error || 'Restart failed'), cls: 'err' });
      } else {
        setPerfStatus({ text: 'Using ' + String(v.path || path), cls: 'ok' });
      }
      onApplied();
    }).catch((err) => setPerfStatus({
      text: String(err instanceof Error ? err.message : err), cls: 'err' }));
  };

  return (
    <div id="ctrl-settings-pop" className="metrics-settings-pop" role="dialog"
         aria-label="Profiling settings" onClick={(e) => e.stopPropagation()}>
      <div className="msp-title">Profiling settings</div>
      <div id="csp-info" className="csp-info">
        <div className="csp-info-row">
          <span>Mode</span><strong>{caps.pipe_mode ? 'continuous' : 'rounds'}</strong>
        </div>
        {caps.callgraph_method !== undefined && (
          <div className="csp-info-row">
            <span>Call-graph</span><strong>{caps.callgraph_method || 'none (flat)'}</strong>
          </div>
        )}
        {agent?.agent_version && (
          <div className="csp-info-row">
            <span>Agent</span><strong>v{agent.agent_version}</strong>
          </div>
        )}
      </div>
      <label className="msp-row">Frequency (Hz)
        <input type="number" id="csp-frequency" min={1} max={10000} className="csp-num"
               value={freq} onChange={(e) => setFreq(e.target.value)} />
      </label>
      <label className="msp-row">Interval (s)
        <input type="number" id="csp-duration" min={1} max={300} className="csp-num"
               value={dur} onChange={(e) => setDur(e.target.value)} />
      </label>
      <div className="msp-title">Record events</div>
      <div id="csp-events" className="csp-events">
        {(caps.record_events ?? []).length === 0 ? (
          <span className="msp-hint">No probed events (start profiling first)</span>
        ) : (caps.record_events ?? []).map((evt) => (
          <label className="csp-evt" key={evt}>
            <input type="checkbox" className="csp-evt-cb" value={evt}
                   checked={checked.has(evt)}
                   onChange={(e) => {
                     const next = new Set(checked);
                     if (e.target.checked) next.add(evt);
                     else next.delete(evt);
                     setChecked(next);
                   }} /> {evt}
          </label>
        ))}
      </div>
      <div className="msp-actions">
        <button id="csp-apply" className="wiz-btn wiz-btn-primary" onClick={apply}>Apply</button>
        <span id="csp-status" className={'msp-status ' + status.cls}>{status.text}</span>
      </div>
      <div className="msp-hint">
        Frequency and event changes restart collection; the interval applies from the next chunk.
        Each extra event multiplies the data the device sends.
      </div>
      <div className="msp-title">perf on the device</div>
      <label className="msp-row">Path
        <input type="text" id="csp-perf" className="csp-path"
               title={agent?.platform?.perf_version || undefined}
               value={perf} onChange={(e) => setPerf(e.target.value)} />
      </label>
      <div className="msp-actions">
        <button id="csp-perf-use" className="wiz-btn" onClick={usePerf}>Use</button>
        <span id="csp-perf-status" className={'msp-status ' + perfStatus.cls}>
          {perfStatus.text}
        </span>
      </div>
      <div className="msp-hint">
        For perf installed outside the agent&apos;s PATH. Changing it restarts collection
        and re-probes.
      </div>
    </div>
  );
}

function SwitchPop({ agent, onClose, onSwitched }:
    { agent: AgentStatus | null; onClose: () => void; onSwitched: () => void }) {
  const [procs, setProcs] = useState<ProcEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<{ text: string; cls: string }>({ text: '', cls: '' });
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    api.agentCommand('list_processes', {}, 30).then((data) => {
      if (cancelled) return;
      if (!data.ok) setError((data.error as string) || 'Failed');
      else setProcs(((data.processes as ProcEntry[]) ?? []).slice(0, 30));
    }).catch((err) => { if (!cancelled) setError(String(err instanceof Error ? err.message : err)); });
    return () => { cancelled = true; clearTimeout(closeTimer.current); };
  }, []);

  useDismiss(onClose);

  const doSwitch = (pid: number) => {
    setStatus({ text: `Switching to PID ${pid} (checking the process)...`, cls: '' });
    // Keep the frequency, interval and events the agent is running with;
    // a switch used to reset them to the defaults.
    api.agentCommand('stop')
      .then(() => api.agentCommand('start', startArgs(agent, pid), 180))
      .then((data) => {
        if (data.ok) {
          setStatus({ text: 'Now profiling PID ' + pid, cls: 'ok' });
          onSwitched();
          closeTimer.current = setTimeout(onClose, 1200);
        } else {
          setStatus({ text: (data.error as string) || 'Start failed', cls: 'err' });
          onSwitched();
        }
      })
      .catch((err) => {
        setStatus({ text: String(err instanceof Error ? err.message : err), cls: 'err' });
        onSwitched();
      });
  };

  return (
    <div id="ctrl-switch-pop" className="metrics-settings-pop" role="dialog"
         aria-label="Switch process" onClick={(e) => e.stopPropagation()}>
      <div className="msp-title">Switch process</div>
      <div id="swp-list" className="swp-list">
        {error && <span className="msp-hint">{error}</span>}
        {!error && !procs && <div className="wiz-spinner">Loading processes...</div>}
        {procs && procs.length === 0 && <span className="msp-hint">No processes</span>}
        {procs?.map((p) => (
          <div className="swp-row" key={p.pid} data-pid={p.pid} role="button" tabIndex={0}
               onClick={() => doSwitch(p.pid)}
               onKeyDown={(e) => { if (e.key === 'Enter') doSwitch(p.pid); }}>
            <span className="swp-pid">{p.pid}</span>
            <span className="swp-comm">{p.comm}</span>
            <span className="swp-cpu">{(p.cpu || 0).toFixed(1)}%</span>
          </div>
        ))}
      </div>
      <div className="msp-actions">
        <span id="swp-status" className={'msp-status ' + status.cls}>{status.text}</span>
      </div>
    </div>
  );
}

const STATE_LABEL: Record<string, string> = {
  profiling: 'Profiling', paused: 'Paused', probing: 'Probing', idle: 'Stopped',
};

export default function ControlBar() {
  const managedAgent = useLive((s) => s.managedAgent);
  const connected = useLive((s) => s.connected);
  const showError = useUi((s) => s.showError);
  const [agent, setAgent] = useState<AgentStatus | null>(null);
  const [visible, setVisible] = useState(false);
  // A start or switch this bar issued and is waiting on
  const [busy, setBusy] = useState<string | null>(null);
  const [pop, setPop] = useState<'settings' | 'switch' | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const probingSince = useRef<number | null>(null);

  // Sync with the agent's actual state (page reload, or a --server agent
  // already profiling when the UI attached). The label always comes from
  // the agent's own `status` answer rather than from local flags.
  const refresh = useCallback(() => {
    api.agentCommand('status', {}, 10).then((data: AgentStatus) => {
      if (!data.ok || !data.state) return;
      setAgent(data);
      setVisible(data.state !== 'idle' || data.pid != null);
      if (data.state === 'probing') probingSince.current ??= Date.now();
      else probingSince.current = null;
    }).catch((err) => console.warn('agent status:', err));
  }, []);

  // Re-sync whenever the view opens (this bar mounts with it) and the
  // connection flags change; a wizard start otherwise never showed the
  // bar until a reload.
  const view = useUi((s) => s.view);
  useEffect(() => {
    if (connected || managedAgent) refresh();
    else setVisible(false);
  }, [connected, managedAgent, view, refresh]);

  // While the agent is probing a process (a start or a switch), poll so
  // the elapsed time and the eventual state land without a reload.
  const probing = agent?.state === 'probing' || busy !== null;
  useEffect(() => {
    if (!probing) return;
    const t = setInterval(() => { setNow(Date.now()); refresh(); }, 1000);
    return () => clearInterval(t);
  }, [probing, refresh]);

  if (!visible) return null;

  const caps = agent?.capabilities ?? {};
  const state = agent?.state ?? 'idle';
  const since = probingSince.current;
  const stateText = busy ?? (state === 'probing' && since
    ? `Probing… (${Math.max(0, Math.round((now - since) / 1000))}s)`
    : STATE_LABEL[state] ?? state);
  const canPause = state === 'profiling';
  const canResume = state === 'paused';
  const canStop = state === 'profiling' || state === 'paused';
  const canStart = state === 'idle' && agent?.pid != null && busy === null;

  const command = (cmd: string, label: string) => {
    api.agentCommand(cmd).then((d) => {
      if (!d.ok) showError(`${label} failed: ${d.error || 'agent refused'}`);
      refresh();
    }).catch((err) => reportError(`${label} failed`, err));
  };

  const start = () => {
    if (agent?.pid == null) return;
    setBusy('Starting…');
    api.agentCommand('start', startArgs(agent, agent.pid), 180).then((d) => {
      setBusy(null);
      if (!d.ok) showError(`Start failed: ${d.error || 'agent refused'}`);
      refresh();
    }).catch((err) => { setBusy(null); reportError('Start failed', err); });
  };

  return (
    <div id="control-bar" data-state={state}>
      <div className="ctrl-group">
        <button id="ctrl-pause" className={'ctrl-btn' + (canPause ? '' : ' hidden')} title="Pause"
                aria-label="Pause profiling" disabled={probing}
                onClick={() => command('pause', 'Pause')}>
          &#9208;
        </button>
        <button id="ctrl-resume" className={'ctrl-btn' + (canResume ? '' : ' hidden')} title="Resume"
                aria-label="Resume profiling"
                onClick={() => command('resume', 'Resume')}>
          &#9654;
        </button>
        <button id="ctrl-start" className={'ctrl-btn' + (canStart ? '' : ' hidden')}
                title="Start profiling the same process again"
                aria-label="Start profiling" onClick={start}>
          &#9654;
        </button>
        <button id="ctrl-stop" className={'ctrl-btn' + (canStop ? '' : ' hidden')}
                title="Stop collection (the agent stays connected)"
                aria-label="Stop profiling" disabled={probing}
                onClick={() => command('stop', 'Stop')}>
          &#9632;
        </button>
        <button id="ctrl-disconnect" className="ctrl-btn ctrl-btn-danger"
                title="Disconnect the agent and end this session"
                aria-label="Disconnect agent"
                onClick={() => {
                  api.disconnectAgent().then((d) => {
                    if (d.stopped) useLive.setState({ managedAgent: false });
                    else showError('Disconnect: ' + (d.reason || 'no agent connected'));
                  }).catch((err) => reportError('Disconnect failed', err));
                }}>
          &#9167;
        </button>
      </div>
      <div className="ctrl-status">
        <span id="ctrl-state"
              className={state === 'paused' || state === 'idle' ? 'paused'
                : state === 'probing' || busy ? 'probing' : ''}>
          {stateText}
        </span>
        <span id="ctrl-pid">
          {agent?.pid != null ? 'PID ' + agent.pid : ''}
        </span>
      </div>
      <div className="ctrl-group">
        <span id="ctrl-mode" className="ctrl-mode"
              title={'Collection mode · sampling frequency · events' +
                (agent?.agent_version ? ' · agent v' + agent.agent_version : '')}>
          {(caps.pipe_mode ? 'continuous' : 'rounds') + ' · ' + (agent?.frequency || '?') + ' Hz'
            + (agent?.events?.length ? ' · ' + agent.events.join(', ') : '')}
        </span>
        <button id="ctrl-switch" className="ctrl-btn" title="Switch process"
                aria-label="Switch process" aria-expanded={pop === 'switch'}
                disabled={probing}
                onClick={(e) => { e.stopPropagation(); setPop(pop === 'switch' ? null : 'switch'); }}>
          &#8646;
        </button>
        <button id="ctrl-settings" className="ctrl-btn" title="Profiling settings"
                aria-label="Profiling settings" aria-expanded={pop === 'settings'}
                onClick={(e) => {
                  e.stopPropagation();
                  if (pop === 'settings') { setPop(null); return; }
                  refresh();
                  setPop('settings');
                }}>
          &#9881;
        </button>
      </div>
      {pop === 'settings' && (
        <SettingsPop agent={agent} onClose={() => setPop(null)} onApplied={refresh} />
      )}
      {pop === 'switch' && (
        <SwitchPop agent={agent} onClose={() => setPop(null)} onSwitched={refresh} />
      )}
    </div>
  );
}
