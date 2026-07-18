import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { AgentCommandResult } from '../api/client';
import { useLive } from '../store/live';

interface AgentStatus extends AgentCommandResult {
  state?: string;
  pid?: number;
  frequency?: number;
  duration?: number;
  events?: string[];
  agent_version?: string;
  capabilities?: {
    pipe_mode?: boolean;
    callgraph_method?: string | null;
    record_events?: string[];
  };
}

interface ProcEntry { pid: number; comm: string; cpu?: number; cmdline?: string }

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

  useEffect(() => {
    const close = () => onClose();
    document.addEventListener('click', close);
    return () => document.removeEventListener('click', close);
  }, [onClose]);

  const apply = () => {
    const f = parseInt(freq);
    const d = parseInt(dur);
    if (!(f >= 1 && f <= 10000) || !(d >= 1 && d <= 300)) {
      setStatus({ text: 'Invalid values', cls: 'err' });
      return;
    }
    const events = [...checked];
    const all = caps.record_events ?? [];
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
      // Frequency/event changes need a restart of collection
      const pid = agent!.pid;
      api.agentCommand('stop')
        .then(() => {
          const args: Record<string, unknown> = { pid, frequency: f, duration: d };
          if (eventsChanged || events.length < all.length) args.events = events;
          return api.agentCommand('start', args, 120);
        })
        .then((data) => done(data.ok, data.error))
        .catch((err) => done(false, String(err)));
    } else {
      api.agentCommand('configure', { frequency: f, duration: d })
        .then((data) => done(data.ok, data.error))
        .catch((err) => done(false, String(err)));
    }
  };

  return (
    <div id="ctrl-settings-pop" className="metrics-settings-pop"
         onClick={(e) => e.stopPropagation()}>
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
      </div>
    </div>
  );
}

function SwitchPop({ onClose, onSwitched }:
    { onClose: () => void; onSwitched: (pid: number, comm: string) => void }) {
  const [procs, setProcs] = useState<ProcEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<{ text: string; cls: string }>({ text: '', cls: '' });

  useEffect(() => {
    api.agentCommand('list_processes', {}, 30).then((data) => {
      if (!data.ok) setError((data.error as string) || 'Failed');
      else setProcs(((data.processes as ProcEntry[]) ?? []).slice(0, 30));
    }).catch((err) => setError(String(err)));
  }, []);

  useEffect(() => {
    const close = () => onClose();
    document.addEventListener('click', close);
    return () => document.removeEventListener('click', close);
  }, [onClose]);

  const doSwitch = (pid: number, comm: string) => {
    setStatus({ text: `Switching to PID ${pid} (re-probing, may take a moment)...`, cls: '' });
    api.agentCommand('stop')
      .then(() => api.agentCommand('start', { pid, frequency: 99, duration: 8 }, 180))
      .then((data) => {
        if (data.ok) {
          setStatus({ text: 'Now profiling PID ' + pid, cls: 'ok' });
          onSwitched(pid, comm);
          setTimeout(onClose, 1200);
        } else {
          setStatus({ text: (data.error as string) || 'Start failed', cls: 'err' });
        }
      })
      .catch((err) => setStatus({ text: String(err), cls: 'err' }));
  };

  return (
    <div id="ctrl-switch-pop" className="metrics-settings-pop"
         onClick={(e) => e.stopPropagation()}>
      <div className="msp-title">Switch process</div>
      <div id="swp-list" className="swp-list">
        {error && <span className="msp-hint">{error}</span>}
        {!error && !procs && <div className="wiz-spinner">Loading processes...</div>}
        {procs && procs.length === 0 && <span className="msp-hint">No processes</span>}
        {procs?.map((p) => (
          <div className="swp-row" key={p.pid} data-pid={p.pid}
               onClick={() => doSwitch(p.pid, p.comm)}>
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

export default function ControlBar() {
  const managedAgent = useLive((s) => s.managedAgent);
  const connected = useLive((s) => s.connected);
  const [agent, setAgent] = useState<AgentStatus | null>(null);
  const [visible, setVisible] = useState(false);
  const [paused, setPaused] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [pop, setPop] = useState<'settings' | 'switch' | null>(null);

  // Sync with the agent's actual state (page reload, or a --server agent
  // already profiling when the UI attached)
  const refresh = useCallback(() => {
    api.agentCommand('status', {}, 10).then((data: AgentStatus) => {
      if (!data.ok || !data.state) return;
      setAgent(data);
      if (data.state === 'profiling' || data.state === 'paused') {
        setVisible(true);
        setStopped(false);
        setPaused(data.state === 'paused');
      } else {
        setVisible(false);
      }
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (connected || managedAgent) refresh();
    else setVisible(false);
  }, [connected, managedAgent, refresh]);

  if (!visible) return null;

  const caps = agent?.capabilities ?? {};
  const stateText = stopped ? 'Stopped' : paused ? 'Paused' : 'Profiling';

  return (
    <div id="control-bar">
      <div className="ctrl-group">
        <button id="ctrl-pause" className={'ctrl-btn' + (paused ? ' hidden' : '')} title="Pause"
                aria-label="Pause profiling"
                onClick={() => {
                  api.agentCommand('pause').then((d) => { if (d.ok) setPaused(true); })
                    .catch(() => {});
                }}>
          &#9208;
        </button>
        <button id="ctrl-resume" className={'ctrl-btn' + (paused ? '' : ' hidden')} title="Resume"
                aria-label="Resume profiling"
                onClick={() => {
                  api.agentCommand('resume').then((d) => { if (d.ok) setPaused(false); })
                    .catch(() => {});
                }}>
          &#9654;
        </button>
        <button id="ctrl-stop" className="ctrl-btn ctrl-btn-danger" title="Stop"
                aria-label="Stop profiling and disconnect"
                onClick={() => {
                  api.disconnectAgent().then((d) => {
                    if (d.stopped) {
                      setStopped(true);
                      useLive.setState({ managedAgent: false });
                    }
                  }).catch(() => {});
                }}>
          &#9632;
        </button>
      </div>
      <div className="ctrl-status">
        <span id="ctrl-state" className={stopped || paused ? 'paused' : ''}>{stateText}</span>
        <span id="ctrl-pid">
          {agent?.pid != null ? 'PID ' + agent.pid : ''}
        </span>
      </div>
      <div className="ctrl-group">
        <span id="ctrl-mode" className="ctrl-mode"
              title={'Collection mode · sampling frequency' +
                (agent?.agent_version ? ' · agent v' + agent.agent_version : '')}>
          {(caps.pipe_mode ? 'continuous' : 'rounds') + ' · ' + (agent?.frequency || '?') + ' Hz'}
        </span>
        <button id="ctrl-switch" className="ctrl-btn" title="Switch process"
                onClick={(e) => { e.stopPropagation(); setPop(pop === 'switch' ? null : 'switch'); }}>
          &#8646;
        </button>
        <button id="ctrl-settings" className="ctrl-btn" title="Profiling settings"
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
        <SwitchPop onClose={() => setPop(null)}
                   onSwitched={() => { refresh(); }} />
      )}
    </div>
  );
}
