import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { AgentCommandResult } from '../api/client';
import { useLive } from '../store/live';
import { useUi } from '../store/ui';
import BrowseModal, { type BrowseRequest } from '../components/BrowseModal';

interface WizStatus { text: string; cls: '' | 'info' | 'ok' | 'error' }

interface Capabilities extends AgentCommandResult {
  record_events?: string[];
  stat_only_events?: string[];
  callgraph_method?: string | null;
}

interface ProcRow { pid: number; comm: string; cpu?: number; cmdline?: string }

const STEPS = ['Connect', 'Process', 'Perf', 'Binary', 'Options', 'Start'];

function Status({ s, id }: { s: WizStatus; id?: string }) {
  return <div id={id} className={'wiz-status' + (s.cls ? ' ' + s.cls : '')}>{s.text}</div>;
}

export default function WizardView() {
  const showView = useUi((s) => s.showView);
  const [step, setStep] = useState(1);

  // Connection
  const [host, setHost] = useState('');
  const [port, setPort] = useState('9999');
  const [connected, setConnected] = useState(false);
  const [agentHello, setAgentHello] =
    useState<{ platform?: { arch?: string; kernel?: string } } | null>(null);
  const [connectStatus, setConnectStatus] = useState<WizStatus>({ text: '', cls: '' });
  const [connecting, setConnecting] = useState(false);

  // Process
  const [pid, setPid] = useState('');
  const [processName, setProcessName] = useState('');
  const [procs, setProcs] = useState<ProcRow[] | null>(null);
  const [procsLoading, setProcsLoading] = useState(false);
  const [pidStatus, setPidStatus] = useState<WizStatus>({ text: '', cls: '' });

  // Perf verify + capabilities
  const [perfResult, setPerfResult] = useState<{ html: React.ReactNode } | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [capsError, setCapsError] = useState<string | null>(null);
  const [capsLoading, setCapsLoading] = useState(false);
  const [events, setEvents] = useState<string[] | null>(null); // null = all

  // Binary/source/toolchain
  const [binary, setBinary] = useState('');
  const [sourceDir, setSourceDir] = useState('');
  const [toolchainPrefix, setToolchainPrefix] = useState('');
  const [sysroot, setSysroot] = useState('');
  const [binaryStatus, setBinaryStatus] = useState<WizStatus>({ text: '', cls: '' });
  const [indexStatus, setIndexStatus] = useState<WizStatus | null>(null);
  const [browse, setBrowse] = useState<BrowseRequest | null>(null);

  // Options
  const [frequency, setFrequency] = useState('99');
  const [duration, setDuration] = useState('8');
  const [startStatus, setStartStatus] = useState<WizStatus>({ text: '', cls: '' });
  const [starting, setStarting] = useState(false);

  const verifyRan = useRef(false);

  // Restore persisted wizard state
  useEffect(() => {
    api.wizardState().then((ws) => {
      if (ws.agent_host) setHost(ws.agent_host);
      if (ws.agent_port) setPort(String(ws.agent_port));
      if (ws.binary_path) setBinary(ws.binary_path);
      if (ws.source_dir) setSourceDir(ws.source_dir);
      if (ws.pid) setPid(String(ws.pid));
      if (ws.frequency) setFrequency(String(ws.frequency));
      if (ws.duration) setDuration(String(ws.duration));
      const evts = (ws as Record<string, unknown>).events;
      if (Array.isArray(evts) && evts.length) setEvents(evts as string[]);
    }).catch(() => {});
  }, []);

  const connect = () => {
    const h = host.trim();
    const p = parseInt(port) || 9999;
    if (!h) {
      setConnectStatus({ text: 'Enter host address', cls: 'error' });
      return;
    }
    setConnecting(true);
    setConnectStatus({ text: `Connecting to ${h}:${p}...`, cls: 'info' });
    api.connect(h, p).then((data) => {
      setConnecting(false);
      if (data.ok) {
        setConnected(true);
        setAgentHello(data.hello ?? null);
        const platform = data.hello?.platform as { arch?: string; kernel?: string } | undefined;
        const info = platform ? platform.arch + ' / ' + platform.kernel : 'connected';
        setConnectStatus({ text: 'Connected: ' + info, cls: 'ok' });
      } else {
        setConnectStatus({ text: data.error || 'Connection failed', cls: 'error' });
      }
    }).catch((err) => {
      setConnecting(false);
      setConnectStatus({ text: 'Error: ' + String(err), cls: 'error' });
    });
  };

  const refreshProcs = () => {
    setProcsLoading(true);
    setPidStatus({ text: '', cls: '' });
    api.agentCommand('list_processes', {}, 30).then((data) => {
      setProcsLoading(false);
      if (!data.ok) {
        setProcs([]);
        setPidStatus({ text: (data.error as string) || 'Failed', cls: 'error' });
        return;
      }
      setProcs((data.processes as ProcRow[]) ?? []);
    }).catch((err) => {
      setProcsLoading(false);
      setProcs([]);
      setPidStatus({ text: String(err), cls: 'error' });
    });
  };

  const verifyPerf = () => {
    setPerfResult(null);
    setCaps(null);
    setCapsError(null);
    api.agentCommand('verify_perf').then((data) => {
      if (!data.ok && data.error) {
        setPerfResult({ html: <span className="wiz-err">Error: {String(data.error)}</span> });
        return;
      }
      const paranoid = data.perf_event_paranoid as number | undefined;
      if (data.available) {
        setPerfResult({
          html: (
            <>
              <div className="wiz-ok">&#10003; perf found: {String(data.version || '?')}</div>
              {data.functional
                ? <div className="wiz-ok">&#10003; perf is functional</div>
                : <div className="wiz-err">&#10007; perf stat check failed:{' '}
                    {String(data.error || 'unknown error')}</div>}
              {paranoid != null && paranoid > 1 && (
                <div className="wiz-warn">&#9888; perf_event_paranoid={paranoid} &mdash; some
                  events may be unavailable</div>
              )}
            </>
          ),
        });
        if (data.functional) probeCapabilities();
      } else {
        setPerfResult({
          html: <div className="wiz-err">&#10007; perf not found:{' '}
            {String(data.error || 'not available')}</div>,
        });
      }
    }).catch((err) => {
      setPerfResult({ html: <span className="wiz-err">Command failed: {String(err)}</span> });
    });
  };

  const probeCapabilities = () => {
    setCapsLoading(true);
    api.agentCommand('reprobe', { pid: parseInt(pid) || undefined }, 120).then((data) => {
      setCapsLoading(false);
      if (!data.ok) {
        setCapsError((data.error as string) || 'Probe failed');
        return;
      }
      setCaps(data as Capabilities);
    }).catch((err) => {
      setCapsLoading(false);
      setCapsError('Probe failed: ' + String(err));
    });
  };

  // Auto-verify when arriving on step 3
  useEffect(() => {
    if (step === 3 && connected && !verifyRan.current) {
      verifyRan.current = true;
      verifyPerf();
    }
    if (step !== 3) verifyRan.current = false;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, connected]);

  const pollIndexStatus = (done: () => void) => {
    api.indexStatus().then((data) => {
      if (data.indexing) {
        setIndexStatus({
          text: `Indexing: ${data.symbols_loaded || 0} symbols, ` +
            `${data.source_files_found || 0} source files...`,
          cls: 'info',
        });
        setTimeout(() => pollIndexStatus(done), 500);
      } else {
        setIndexStatus({
          text: `Ready: ${data.symbols_loaded || 0} symbols, ` +
            `${data.source_files_found || 0} source files from binary`,
          cls: 'ok',
        });
        done();
      }
    }).catch(() => {
      setIndexStatus(null);
      done();
    });
  };

  // Apply binary/source/toolchain config when leaving step 4
  const applyStep4 = (done: () => void) => {
    setBinaryStatus({ text: 'Applying...', cls: 'info' });

    // Toolchain first — it sets addr2line/readelf used by binary indexing
    const toolchainStep = (toolchainPrefix.trim() || sysroot.trim())
      ? api.configToolchain({
          ...(toolchainPrefix.trim() ? { prefix: toolchainPrefix.trim() } : {}),
          ...(sysroot.trim() ? { sysroot: sysroot.trim() } : {}),
        }).then((r) => {
          if (!r.ok) throw new Error(r.error || 'Toolchain config failed');
        })
      : Promise.resolve();

    toolchainStep.then(async () => {
      const results: { ok: boolean; error?: string }[] = [];
      if (binary.trim()) results.push(await api.configBinary(binary.trim()));
      if (sourceDir.trim()) results.push(await api.configSource(sourceDir.trim()));

      const errors = results.filter((r) => !r.ok);
      if (errors.length > 0) {
        setBinaryStatus({ text: errors.map((e) => e.error).join('; '), cls: 'error' });
      } else {
        setBinaryStatus({ text: 'Applied', cls: 'ok' });
      }
      if (binary.trim()) {
        setIndexStatus({ text: 'Indexing symbols and source files...', cls: 'info' });
        pollIndexStatus(done);
      } else {
        done();
      }
    }).catch((err) => {
      setBinaryStatus({ text: 'Error: ' + String(err), cls: 'error' });
      done();
    });
  };

  const validateStep = (s: number): boolean => {
    if (s === 1 && !connected) {
      setConnectStatus({ text: 'Connect to agent first', cls: 'error' });
      return false;
    }
    if (s === 2 && !pid) {
      setPidStatus({ text: 'Select or enter a PID', cls: 'error' });
      return false;
    }
    return true;
  };

  const next = () => {
    if (step >= 6 || !validateStep(step)) return;
    if (step === 4) {
      applyStep4(() => setStep(5));
      return;
    }
    setStep(step + 1);
  };

  const start = () => {
    setStarting(true);
    setStartStatus({ text: 'Starting profiling...', cls: 'info' });
    const args: Record<string, unknown> = {
      pid: parseInt(pid),
      frequency: parseInt(frequency) || 99,
      duration: parseInt(duration) || 8,
    };
    if (events?.length) args.events = events;
    api.agentCommand('start', args, 120).then((data) => {
      setStarting(false);
      if (data.ok) {
        setStartStatus({ text: 'Profiling started', cls: 'ok' });
        api.saveWizardState({
          step: 6, pid: parseInt(pid), frequency: parseInt(frequency) || 99,
          duration: parseInt(duration) || 8, events,
        }).catch(() => {});
        useLive.setState({ managedAgent: true });
        showView('profiling');
      } else {
        setStartStatus({ text: (data.error as string) || 'Start failed', cls: 'error' });
      }
    }).catch((err) => {
      setStarting(false);
      setStartStatus({ text: 'Error: ' + String(err), cls: 'error' });
    });
  };

  const toggleEvent = (evt: string, on: boolean) => {
    const all = caps?.record_events ?? [];
    const current = events ?? all;
    const sel = on ? [...new Set([...current, evt])] : current.filter((e) => e !== evt);
    setEvents(sel.length === 0 || sel.length === all.length ? null : sel);
  };

  const platform = agentHello?.platform;

  return (
    <div className="wizard-container">
      <div className="wizard-progress">
        {STEPS.map((label, i) => (
          <div key={label}
               className={'wiz-prog-step' +
                 (i + 1 === step ? ' active' : i + 1 < step ? ' done' : '')}
               data-step={i + 1}>
            <span className="wiz-num">{i + 1}</span> {label}
          </div>
        ))}
      </div>

      <div className="wizard-body">
        {/* Step 1: Connect */}
        <div id="wiz-step-1" className={'wiz-step' + (step === 1 ? ' active' : '')}>
          <h3>Connect to Agent</h3>
          <p>Enter the IP and port of the agent running in <code>--listen</code> mode on your
            target device.</p>
          <div className="wiz-form">
            <div className="wiz-row">
              <label>Host</label>
              <input type="text" id="wiz-host" placeholder="192.168.1.100"
                     value={host} onChange={(e) => setHost(e.target.value)} />
            </div>
            <div className="wiz-row">
              <label>Port</label>
              <input type="number" id="wiz-port" value={port}
                     onChange={(e) => setPort(e.target.value)} />
            </div>
            <button id="wiz-connect-btn" className="wiz-btn wiz-btn-primary"
                    disabled={connecting} onClick={connect}>
              Connect
            </button>
            <Status id="wiz-connect-status" s={connectStatus} />
          </div>
        </div>

        {/* Step 2: Select Process */}
        <div id="wiz-step-2" className={'wiz-step' + (step === 2 ? ' active' : '')}>
          <h3>Select Process</h3>
          <p>Choose the process to profile on the target device.</p>
          <div className="wiz-form">
            <div className="wiz-row">
              <label>PID (or select from list)</label>
              <div className="wiz-input-row">
                <input type="number" id="wiz-pid" placeholder="PID" value={pid}
                       onChange={(e) => setPid(e.target.value)} />
                <button id="wiz-refresh-procs" className="wiz-btn" onClick={refreshProcs}>
                  Refresh
                </button>
              </div>
            </div>
            <Status id="wiz-pid-status" s={pidStatus} />
          </div>
          <div id="wiz-proc-list" className="wiz-proc-list">
            {procsLoading && <div className="wiz-spinner">Loading process list...</div>}
            {!procsLoading && procs === null && (
              <p className="empty">Click Refresh to load process list</p>
            )}
            {!procsLoading && procs !== null && procs.length === 0 && (
              <p className="empty">No processes found</p>
            )}
            {!procsLoading && procs !== null && procs.length > 0 && (
              <table className="wiz-proc-table">
                <thead>
                  <tr><th>PID</th><th>Name</th><th>CPU%</th><th>Command</th></tr>
                </thead>
                <tbody>
                  {procs.map((p) => (
                    <tr key={p.pid} data-pid={p.pid}
                        className={String(p.pid) === pid ? 'selected' : ''}
                        onClick={() => {
                          setPid(String(p.pid));
                          setProcessName(p.comm);
                          setPidStatus({
                            text: `Selected PID ${p.pid} (${p.comm})`, cls: 'ok' });
                        }}>
                      <td>{p.pid}</td>
                      <td>{p.comm}</td>
                      <td>{p.cpu}</td>
                      <td title={p.cmdline}>{(p.cmdline ?? '').substring(0, 80)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        {/* Step 3: Verify Perf */}
        <div id="wiz-step-3" className={'wiz-step' + (step === 3 ? ' active' : '')}>
          <h3>Perf Capabilities</h3>
          <p>Checking that <code>perf</code> is available on the target and probing supported
            events.</p>
          <div id="wiz-perf-result" className="wiz-result">
            {perfResult ? perfResult.html : <div className="wiz-spinner">Checking...</div>}
          </div>
          <div id="wiz-caps-section" className={caps || capsLoading || capsError ? '' : 'hidden'}>
            <h4>Supported Events</h4>
            <div id="wiz-caps-events" className="wiz-caps-grid">
              {capsLoading && <div className="wiz-spinner">Probing supported events...</div>}
              {capsError && <span className="wiz-err">{capsError}</span>}
              {caps && (caps.record_events ?? []).map((evt) => (
                <label key={evt} className="wiz-cap-tag record selectable"
                       title="Click to include/exclude from recording">
                  <input type="checkbox" className="wiz-evt-cb" value={evt}
                         checked={!events || events.includes(evt)}
                         onChange={(e) => toggleEvent(evt, e.target.checked)} /> {evt}
                </label>
              ))}
              {caps && (caps.stat_only_events ?? []).map((evt) => (
                <span key={evt} className="wiz-cap-tag stat"
                      title="perf stat only (always collected)">{evt}</span>
              ))}
              {caps && !(caps.record_events ?? []).length &&
                !(caps.stat_only_events ?? []).length && (
                <span className="wiz-err">No events detected</span>
              )}
            </div>
            <div id="wiz-caps-callgraph" className="wiz-caps-callgraph">
              {caps && (caps.callgraph_method
                ? <>Call-graph mode: <strong>{caps.callgraph_method}</strong></>
                : <span className="wiz-warn">&#9888; No call-graph support detected
                    (flat profile only)</span>)}
            </div>
          </div>
        </div>

        {/* Step 4: Binary & Source */}
        <div id="wiz-step-4" className={'wiz-step' + (step === 4 ? ' active' : '')}>
          <h3>Binary &amp; Source Mapping</h3>
          <p>Provide paths on <strong>this server</strong> to the unstripped binary and source
            directory for line-level annotation. Optional &mdash; skip if not needed.</p>
          <div className="wiz-form">
            <div className="wiz-row">
              <label>Binary (debug symbols)</label>
              <div className="wiz-input-row">
                <input type="text" id="wiz-binary" placeholder="/path/to/binary"
                       value={binary} onChange={(e) => setBinary(e.target.value)} />
                <button className="wiz-browse-btn"
                        onClick={() => setBrowse({
                          startPath: binary.trim() || '/', mode: 'file',
                          onSelect: setBinary })}>
                  Browse
                </button>
              </div>
            </div>
            <div className="wiz-row">
              <label>Source directory</label>
              <div className="wiz-input-row">
                <input type="text" id="wiz-source-dir" placeholder="/path/to/src"
                       value={sourceDir} onChange={(e) => setSourceDir(e.target.value)} />
                <button className="wiz-browse-btn"
                        onClick={() => setBrowse({
                          startPath: sourceDir.trim() || '/', mode: 'dir',
                          onSelect: setSourceDir })}>
                  Browse
                </button>
              </div>
            </div>
            <details className="wiz-advanced">
              <summary>Toolchain &amp; Cross-compilation</summary>
              <div className="wiz-row">
                <label>Toolchain prefix</label>
                <div className="wiz-input-row">
                  <input type="text" id="wiz-toolchain-prefix"
                         placeholder="e.g. arm-linux-gnueabihf- or /opt/toolchain/bin/aarch64-linux-gnu-"
                         value={toolchainPrefix}
                         onChange={(e) => setToolchainPrefix(e.target.value)} />
                </div>
                <div className="wiz-hint">
                  Derives addr2line and readelf from the prefix. For cross-compiled targets.
                </div>
              </div>
              <div className="wiz-row">
                <label>Sysroot</label>
                <div className="wiz-input-row">
                  <input type="text" id="wiz-sysroot"
                         placeholder="/opt/sysroot or /path/to/target/rootfs"
                         value={sysroot} onChange={(e) => setSysroot(e.target.value)} />
                  <button className="wiz-browse-btn"
                          onClick={() => setBrowse({
                            startPath: sysroot.trim() || '/', mode: 'dir',
                            onSelect: setSysroot })}>
                    Browse
                  </button>
                </div>
                <div className="wiz-hint">
                  Target filesystem root for resolving shared libraries and source files
                  (like perf --symfs).
                </div>
              </div>
            </details>
            <Status id="wiz-binary-status" s={binaryStatus} />
            {indexStatus && <Status id="wiz-index-status" s={indexStatus} />}
          </div>
        </div>

        {/* Step 5: Options */}
        <div id="wiz-step-5" className={'wiz-step' + (step === 5 ? ' active' : '')}>
          <h3>Profiling Options</h3>
          <p>Configure sampling parameters. Defaults work well for most cases.</p>
          <div className="wiz-form">
            <div className="wiz-row">
              <label>Sampling frequency (Hz)</label>
              <input type="number" id="wiz-frequency" min={1} max={10000}
                     value={frequency} onChange={(e) => setFrequency(e.target.value)} />
            </div>
            <div className="wiz-row">
              <label>Collection duration (seconds)</label>
              <input type="number" id="wiz-duration" min={1} max={300}
                     value={duration} onChange={(e) => setDuration(e.target.value)} />
            </div>
          </div>
        </div>

        {/* Step 6: Review & Start */}
        <div id="wiz-step-6" className={'wiz-step' + (step === 6 ? ' active' : '')}>
          <h3>Review &amp; Start</h3>
          <div id="wiz-review-summary" className="wiz-review">
            {[
              ['Agent', host + ':' + port],
              ['Platform', platform ? platform.arch + ' / ' + platform.kernel : '?'],
              ['Process', 'PID ' + (pid || '?') + (processName ? ` (${processName})` : '')],
              ['Frequency', frequency + ' Hz'],
              ['Duration', duration + 's per round'],
              ['Events', events ? events.join(', ') : 'all supported'],
              ...(binary ? [['Binary', binary]] : []),
              ...(toolchainPrefix.trim() ? [['Toolchain', toolchainPrefix.trim()]] : []),
              ...(sysroot.trim() ? [['Sysroot', sysroot.trim()]] : []),
            ].map(([label, value]) => (
              <div className="wiz-review-row" key={label}>
                <span className="wiz-review-label">{label}</span>
                <span className="wiz-review-value">{value}</span>
              </div>
            ))}
          </div>
          <button id="wiz-start-btn" className="wiz-btn wiz-btn-primary wiz-btn-large"
                  disabled={starting} onClick={start}>
            Start Profiling
          </button>
          <Status id="wiz-start-status" s={startStatus} />
        </div>
      </div>

      <div className="wizard-footer">
        <button id="wiz-back" className="wiz-btn"
                style={{ display: step > 1 ? undefined : 'none' }}
                onClick={() => setStep(Math.max(1, step - 1))}>
          Back
        </button>
        <div className="wiz-footer-spacer"></div>
        <button id="wiz-skip" className="wiz-btn"
                style={{ display: step === 4 ? undefined : 'none' }}
                onClick={() => setStep(5)}>
          Skip
        </button>
        <button id="wiz-next" className="wiz-btn wiz-btn-primary"
                style={{ display: step < 6 ? undefined : 'none' }}
                onClick={next}>
          Next
        </button>
      </div>

      {browse && <BrowseModal request={browse} onClose={() => setBrowse(null)} />}
    </div>
  );
}
