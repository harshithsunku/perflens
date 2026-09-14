import { useEffect, useRef, useState } from 'react';
import { api, exportUrls } from './api/client';
import { connectSSE, disconnectSSE } from './api/sse';
import { downloadExport } from './lib/download';
import { replaySession } from './lib/replay';
import { installShortcuts } from './lib/shortcuts';
import ShortcutsHelp from './components/ShortcutsHelp';
import { useLive } from './store/live';
import { reportError, useUi } from './store/ui';
import { parseHash, replaceHash } from './store/urlHash';
import DocsDrawer from './components/DocsDrawer';
import Landing from './views/Landing';
import ProfilingView from './views/ProfilingView';
import WizardView from './views/WizardView';

function Header() {
  // Selectors, not the whole store: every 2 s metrics frame used to
  // re-render the header, the banners and the whole tree under them.
  const connected = useLive((s) => s.connected);
  const agentAddr = useLive((s) => s.agentAddr);
  const selectedEvent = useLive((s) => s.selectedEvent);
  const replaySessionId = useLive((s) => s.replaySessionId);
  const sseState = useLive((s) => s.sseState);
  const theme = useUi((s) => s.theme);
  const toggleTheme = useUi((s) => s.toggleTheme);
  const setHelp = useUi((s) => s.setHelp);
  const [exportOpen, setExportOpen] = useState(false);
  const [docsOpen, setDocsOpen] = useState(false);

  useEffect(() => {
    if (!exportOpen) return;
    const close = () => setExportOpen(false);
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close(); };
    document.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('click', close);
      document.removeEventListener('keydown', onKey);
    };
  }, [exportOpen]);

  const doExport = (action: string) => {
    setExportOpen(false);
    const sessionId = replaySessionId || 'live';
    const stem = `perflens-${sessionId}`;
    if (action === 'svg') {
      void downloadExport(exportUrls.flamegraphSvg(selectedEvent, sessionId), stem + '.svg');
    } else if (action === 'collapsed') {
      void downloadExport(exportUrls.collapsed(selectedEvent, sessionId), stem + '.collapsed');
    } else if (action === 'json') {
      void downloadExport(exportUrls.json(sessionId), stem + '.json');
    }
  };

  const menuKey = (action: string) => (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); doExport(action); }
  };

  const statusText = sseState === 'reconnecting'
    ? 'Server unreachable — reconnecting…'
    : connected ? 'Agent: ' + agentAddr : 'Agent disconnected';

  return (
    <header>
      <div className="header-left">
        <h1>Perf<span className="logo-accent">Lens</span></h1>
        <span className="header-divider"></span>
        <span className="tagline">Real-time Linux Performance Profiler</span>
      </div>
      <div id="status">
        <span id="status-dot"
              className={'dot ' + (sseState === 'reconnecting' ? 'reconnecting'
                : connected ? 'connected' : 'disconnected')}></span>
        <span id="status-text" data-sse={sseState}>{statusText}</span>
        <button id="stop-btn" className={'stop-btn' + (connected ? '' : ' hidden')}
                title="Disconnect the agent and end this session (a --server agent dials back in)"
                onClick={() => {
                  api.disconnectAgent()
                    .catch((err) => reportError('Disconnect failed', err));
                }}>
          Disconnect
        </button>
        <div className="export-dropdown">
          <button id="export-btn" className="export-btn"
                  aria-haspopup="menu" aria-expanded={exportOpen}
                  onClick={(e) => { e.stopPropagation(); setExportOpen(!exportOpen); }}>
            Export
          </button>
          <div id="export-menu" className={'export-menu' + (exportOpen ? ' visible' : '')}
               role="menu" onClick={(e) => e.stopPropagation()}>
            <div className="export-item" data-action="svg" role="menuitem" tabIndex={0}
                 onClick={() => doExport('svg')} onKeyDown={menuKey('svg')}>
              Download Flamegraph SVG
            </div>
            <div className="export-item" data-action="collapsed" role="menuitem" tabIndex={0}
                 onClick={() => doExport('collapsed')} onKeyDown={menuKey('collapsed')}>
              Download Collapsed Stacks
            </div>
            <div className="export-item" data-action="json" role="menuitem" tabIndex={0}
                 onClick={() => doExport('json')} onKeyDown={menuKey('json')}>
              Download Session JSON
            </div>
          </div>
        </div>
        <button id="docs-btn" className="docs-btn" title="Documentation"
                onClick={() => setDocsOpen(true)}>
          Docs
        </button>
        <button id="help-btn" className="docs-btn help-btn" title="Keyboard shortcuts (?)"
                aria-label="Keyboard shortcuts" onClick={() => setHelp(true)}>
          ?
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
  const error = useUi((s) => s.error);
  const errorSticky = useUi((s) => s.errorSticky);
  const hideError = useUi((s) => s.hideError);
  const isReplayMode = useLive((s) => s.isReplayMode);
  const replaySessionId = useLive((s) => s.replaySessionId);
  const replayTimestamp = useLive((s) => s.replayTimestamp);
  const connected = useLive((s) => s.connected);
  const exitReplay = useLive((s) => s.exitReplay);
  const showView = useUi((s) => s.showView);
  return (
    <>
      <div id="error-banner" role="alert" aria-live="assertive"
           className={(error ? 'visible' : '') + (errorSticky ? ' sticky' : '')}>
        <span id="error-text">{error ?? ''}</span>
        <button id="error-close" aria-label="Dismiss error" onClick={hideError}>&times;</button>
      </div>
      <div id="replay-banner" className={isReplayMode ? 'visible' : ''} data-testid="replay-banner">
        <span id="replay-text">
          {isReplayMode
            ? '⏪ REPLAY MODE — Session: ' + replaySessionId + ' from ' + (replayTimestamp || '')
            : ''}
        </span>
        {isReplayMode && (
          <button id="replay-exit" className="replay-exit" data-testid="replay-exit"
                  title={connected ? 'Back to the live profile' : 'Leave replay mode'}
                  onClick={() => { exitReplay(); if (!connected) showView('landing'); }}>
            {connected ? 'Back to live' : 'Exit replay'}
          </button>
        )}
      </div>
    </>
  );
}

export default function App() {
  const view = useUi((s) => s.view);
  const booted = useRef(false);

  // Boot: apply URL hash, check status, open SSE. Only the one-shot
  // actions are guarded against StrictMode's double invocation; the SSE
  // connection and the shortcuts are set up and torn down symmetrically,
  // so the second pass (dev only) gets them back after the first cleanup.
  useEffect(() => {
    if (!booted.current) {
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
          if (!useLive.getState().isReplayMode) useUi.getState().showView('profiling');
        }
      }).catch((err) => console.warn('status:', err));
    }

    connectSSE();
    const uninstall = installShortcuts();
    return () => { disconnectSSE(); uninstall(); };
  }, []);

  // Keep the URL hash in sync with the shareable view state
  const activeTab = useUi((s) => s.activeTab);
  const selectedEvent = useLive((s) => s.selectedEvent);
  const selectedTid = useLive((s) => s.selectedTid);
  const zoomNames = useLive((s) => s.zoomNames);
  const isReplayMode = useLive((s) => s.isReplayMode);
  const replaySessionId = useLive((s) => s.replaySessionId);
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
        {/* Banners live outside the per-view containers: an error raised
            on the landing page or in the wizard used to render inside the
            hidden profiling view, where nobody could see it. */}
        <Banners />
        <div id="view-landing" className={'view' + (view === 'landing' ? ' active' : '')}>
          {view === 'landing' && <Landing />}
        </div>
        <div id="view-wizard" className={'view' + (view === 'wizard' ? ' active' : '')}>
          {view === 'wizard' && <WizardView />}
        </div>
        <div id="view-profiling" className={'view' + (view === 'profiling' ? ' active' : '')}>
          {view === 'profiling' && <ProfilingView active />}
        </div>
      </main>
      <ShortcutsHelp />
    </>
  );
}
