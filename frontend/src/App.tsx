import { useEffect, useRef, useState } from 'react';
import { api, exportUrls } from './api/client';
import { connectSSE, disconnectSSE } from './api/sse';
import { replaySession } from './lib/replay';
import { useLive } from './store/live';
import { useUi } from './store/ui';
import { parseHash, replaceHash } from './store/urlHash';
import DocsDrawer from './components/DocsDrawer';
import Landing from './views/Landing';
import ProfilingView from './views/ProfilingView';
import WizardView from './views/WizardView';

function Header() {
  const { connected, agentAddr, selectedEvent, replaySessionId } = useLive();
  const { theme, toggleTheme } = useUi();
  const [exportOpen, setExportOpen] = useState(false);
  const [docsOpen, setDocsOpen] = useState(false);

  useEffect(() => {
    if (!exportOpen) return;
    const close = () => setExportOpen(false);
    document.addEventListener('click', close);
    return () => document.removeEventListener('click', close);
  }, [exportOpen]);

  const doExport = (action: string) => {
    setExportOpen(false);
    const sessionId = replaySessionId || 'live';
    if (action === 'svg') window.open(exportUrls.flamegraphSvg(selectedEvent, sessionId), '_blank');
    else if (action === 'collapsed') window.open(exportUrls.collapsed(sessionId), '_blank');
    else if (action === 'json') window.open(exportUrls.json(sessionId), '_blank');
  };

  return (
    <header>
      <div className="header-left">
        <h1>Perf<span className="logo-accent">Lens</span></h1>
        <span className="header-divider"></span>
        <span className="tagline">Real-time Linux Performance Profiler</span>
      </div>
      <div id="status">
        <span id="status-dot" className={'dot ' + (connected ? 'connected' : 'disconnected')}></span>
        <span id="status-text">
          {connected ? 'Agent: ' + agentAddr : 'Agent disconnected'}
        </span>
        <button id="stop-btn" className={'stop-btn' + (connected ? '' : ' hidden')}
                title="Stop profiling"
                onClick={() => {
                  api.stop().then((d) => {
                    if (d.error) useUi.getState().showError(d.error);
                  }).catch(() => useUi.getState().showError('Stop not available'));
                }}>
          Stop
        </button>
        <div className="export-dropdown">
          <button id="export-btn" className="export-btn"
                  onClick={(e) => { e.stopPropagation(); setExportOpen(!exportOpen); }}>
            Export
          </button>
          <div id="export-menu" className={'export-menu' + (exportOpen ? ' visible' : '')}
               onClick={(e) => e.stopPropagation()}>
            <div className="export-item" data-action="svg" onClick={() => doExport('svg')}>
              Download Flamegraph SVG
            </div>
            <div className="export-item" data-action="collapsed" onClick={() => doExport('collapsed')}>
              Download Collapsed Stacks
            </div>
            <div className="export-item" data-action="json" onClick={() => doExport('json')}>
              Download Session JSON
            </div>
          </div>
        </div>
        <button id="docs-btn" className="docs-btn" title="Documentation"
                onClick={() => setDocsOpen(true)}>
          Docs
        </button>
        <span className="theme-label" id="theme-label">{theme === 'dark' ? 'Dark' : 'Light'}</span>
        <button id="theme-toggle" className="theme-toggle" title="Toggle light/dark mode"
                aria-label="Toggle theme" onClick={toggleTheme}></button>
      </div>
      <DocsDrawer open={docsOpen} onClose={() => setDocsOpen(false)} />
    </header>
  );
}

function Banners() {
  const { error, hideError } = useUi();
  const { isReplayMode, replaySessionId, replayTimestamp } = useLive();
  return (
    <>
      <div id="error-banner" className={error ? 'visible' : ''}>
        <span id="error-text">{error ?? ''}</span>
        <button id="error-close" onClick={hideError}>&times;</button>
      </div>
      <div id="replay-banner" className={isReplayMode ? 'visible' : ''} data-testid="replay-banner">
        <span id="replay-text">
          {isReplayMode
            ? '⏪ REPLAY MODE — Session: ' + replaySessionId + ' from ' + (replayTimestamp || '')
            : ''}
        </span>
      </div>
    </>
  );
}

export default function App() {
  const view = useUi((s) => s.view);
  const booted = useRef(false);

  // Boot: apply URL hash, check status, open SSE
  useEffect(() => {
    if (booted.current) return;   // StrictMode double-invoke guard
    booted.current = true;

    const p = parseHash(location.hash);
    if (p.event) useLive.setState({ selectedEvent: p.event });
    if (p.tid != null) useLive.setState({ selectedTid: p.tid });
    if (p.zoom) useLive.setState({ zoomNames: p.zoom });
    if (p.tab || p.session) {
      useUi.getState().showView('profiling');
      if (p.tab) useUi.getState().switchTab(p.tab);
    }
    if (p.session) void replaySession(p.session, !!p.tab);

    api.status().then((data) => {
      if (data.agent_connected) {
        useLive.setState({ connected: true, agentAddr: data.agent_addr ?? null });
        useUi.getState().showView('profiling');
      }
    }).catch(() => {});

    connectSSE();
    return () => disconnectSSE();
  }, []);

  // Keep the URL hash in sync with the shareable view state
  const activeTab = useUi((s) => s.activeTab);
  const { selectedEvent, selectedTid, zoomNames, isReplayMode, replaySessionId } = useLive();
  useEffect(() => {
    replaceHash({
      tab: activeTab,
      event: selectedEvent,
      tid: selectedTid ?? undefined,
      zoom: zoomNames,
      session: isReplayMode && replaySessionId ? replaySessionId : undefined,
    });
  }, [activeTab, selectedEvent, selectedTid, zoomNames, isReplayMode, replaySessionId]);

  return (
    <>
      <Header />
      <main>
        <div id="view-landing" className={'view' + (view === 'landing' ? ' active' : '')}>
          {view === 'landing' && <Landing />}
        </div>
        <div id="view-wizard" className={'view' + (view === 'wizard' ? ' active' : '')}>
          {view === 'wizard' && <WizardView />}
        </div>
        <div id="view-profiling" className={'view' + (view === 'profiling' ? ' active' : '')}>
          <Banners />
          <ProfilingView active={view === 'profiling'} />
        </div>
      </main>
    </>
  );
}
