import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { FunctionEntry, FunctionSummary } from '../api/client';
import type { FlameNode } from '../lib/flamegraph/types';
import { fmtClock } from '../lib/format';
import { useLive } from '../store/live';
import { useUi, type Tab } from '../store/ui';
import ControlBar from '../components/ControlBar';
import FlameGraph from '../components/FlameGraph';
import FunctionTable from '../components/FunctionTable';
import MetricsStrip from '../components/MetricsStrip';
import SessionsTab from '../components/SessionsTab';
import SourceView from '../components/SourceView';
import StatBar from '../components/StatBar';
import ThreadsTab from '../components/ThreadsTab';

const TABS: { id: Tab; label: string }[] = [
  { id: 'functions', label: 'Functions' },
  { id: 'source', label: 'Source' },
  { id: 'flamegraph', label: 'Flame Graph' },
  { id: 'threads', label: 'Threads' },
  { id: 'sessions', label: 'Sessions' },
];

function LastUpdate() {
  const lastUpdateTime = useLive((s) => s.lastUpdateTime);
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  if (!lastUpdateTime) return <div id="last-update"></div>;
  const ago = Math.round((Date.now() - lastUpdateTime) / 1000);
  return (
    <div id="last-update">
      {ago < 2 ? 'Updated just now' : 'Updated ' + ago + 's ago'}
    </div>
  );
}

/** Resolve what the Functions table + Flame Graph should show:
 * time-window fetch > thread-view fetch > live per-event snapshot. */
function useCurrentViewData() {
  const selectedEvent = useLive((s) => s.selectedEvent);
  const selectedTid = useLive((s) => s.selectedTid);
  const timeWindow = useLive((s) => s.timeWindow);
  const isReplayMode = useLive((s) => s.isReplayMode);
  const chunkCount = useLive((s) => s.chunkCount);
  const entry = useLive((s) => s.perEvent[s.selectedEvent]);

  const windowQuery = useQuery({
    queryKey: ['time-window', selectedEvent, timeWindow?.start, timeWindow?.end,
               selectedTid, chunkCount],
    queryFn: () => api.timeWindow(selectedEvent, timeWindow!.start, timeWindow!.end,
                                  selectedTid),
    enabled: !!timeWindow && !isReplayMode,
  });

  const threadQuery = useQuery({
    queryKey: ['thread-view', selectedEvent, selectedTid, chunkCount],
    queryFn: () => api.threadView(selectedEvent, selectedTid!),
    enabled: !timeWindow && selectedTid !== null,
  });

  if (timeWindow && !isReplayMode) {
    const d = windowQuery.data;
    return {
      functionSummary: (d?.function_summary ?? null) as FunctionSummary | null,
      flamegraph: (d?.flamegraph ?? null) as FlameNode | null,
      totalSamples: d?.function_summary?.total_samples ?? 0,
      windowSamples: d?.window?.samples,
      allowZoom: false,
    };
  }
  if (selectedTid !== null) {
    const d = threadQuery.data;
    return {
      functionSummary: (d?.function_summary ?? null) as FunctionSummary | null,
      flamegraph: (d?.flamegraph ?? null) as FlameNode | null,
      totalSamples: d?.function_summary?.total_samples ?? 0,
      windowSamples: undefined,
      allowZoom: false,
    };
  }
  return {
    functionSummary: (entry?.function_summary ?? null) as FunctionSummary | null,
    flamegraph: (entry?.flamegraph ?? null) as FlameNode | null,
    totalSamples: entry?.function_summary?.total_samples ?? 0,
    windowSamples: undefined,
    allowZoom: true,
  };
}

export default function ProfilingView({ active }: { active: boolean }) {
  const activeTab = useUi((s) => s.activeTab);
  const switchTab = useUi((s) => s.switchTab);
  const showError = useUi((s) => s.showError);

  const eventTypes = useLive((s) => s.eventTypes);
  const selectedEvent = useLive((s) => s.selectedEvent);
  const selectEvent = useLive((s) => s.selectEvent);
  const selectedTid = useLive((s) => s.selectedTid);
  const selectTid = useLive((s) => s.selectTid);
  const timeWindow = useLive((s) => s.timeWindow);
  const setTimeWindow = useLive((s) => s.setTimeWindow);
  const baseline = useLive((s) => s.baseline);
  const diffEnabled = useLive((s) => s.diffEnabled);
  const isReplayMode = useLive((s) => s.isReplayMode);
  const replaySessionId = useLive((s) => s.replaySessionId);
  const entry = useLive((s) => s.perEvent[s.selectedEvent]);
  const perEvent = useLive((s) => s.perEvent);

  const view = useCurrentViewData();

  // Diff active only on the full, unfiltered view
  const diffActive = diffEnabled && !!baseline && selectedTid === null &&
    !timeWindow && !!baseline.perEvent[selectedEvent];

  const baselineMap = useMemo(() => {
    if (!diffActive || !baseline) return null;
    const base = baseline.perEvent[selectedEvent];
    if (!base?.function_summary) return null;
    const map = new Map<string, FunctionEntry>();
    for (const f of base.function_summary.functions ?? []) {
      map.set(f.name + '\u0000' + (f.module || ''), f);
    }
    return map;
  }, [diffActive, baseline, selectedEvent]);

  const threads = entry?.threads ?? [];
  const sourceFiles = entry?.source_files ?? [];
  const hasSource = sourceFiles.some((f) => f.found);

  const showSourceForFunction = (funcName: string) => {
    const embedded = entry?.source as
      Record<string, { samples: number }[]> | undefined;
    if (embedded && Object.keys(embedded).length > 0) {
      // Replay: pick the embedded file with the most samples
      let bestFile: string | null = null;
      let bestSamples = -1;
      for (const [filePath, lines] of Object.entries(embedded)) {
        const total = lines.reduce((s, l) => s + l.samples, 0);
        if (total > bestSamples) { bestSamples = total; bestFile = filePath; }
      }
      if (bestFile) {
        useLive.setState({ currentSourceFile: bestFile });
        switchTab('source');
        return;
      }
    }
    const target =
      sourceFiles.find((f) => f.found && f.functions?.includes(funcName)) ||
      sourceFiles.find((f) => f.found) ||
      sourceFiles[0];
    useLive.setState({
      currentSourceFile: target?.found ? target.path : null,
    });
    switchTab('source');
  };

  return (
    <>
      <ControlBar />
      <StatBar />
      <LastUpdate />
      <MetricsStrip />

      <div id="event-selector">
        <label>Event: </label>
        <select id="event-select" value={selectedEvent}
                onChange={(e) => selectEvent(e.target.value)}>
          {(eventTypes.length ? eventTypes : [selectedEvent]).map((evt) => (
            <option key={evt} value={evt}>{evt}</option>
          ))}
        </select>
        {threads.length > 1 && (
          <>
            <label id="thread-filter-label" className="thread-filter-label">Thread: </label>
            <select id="thread-filter" value={selectedTid != null ? String(selectedTid) : ''}
                    onChange={(e) => selectTid(e.target.value ? parseInt(e.target.value) : null)}>
              <option value="">All threads ({threads.length})</option>
              {threads.map((t) => (
                <option key={t.tid} value={t.tid}>{t.comm} ({t.tid})</option>
              ))}
            </select>
          </>
        )}
        {timeWindow && (
          <span id="time-window-chip" className="tw-chip">
            <span id="time-window-text">
              {fmtClock(timeWindow.start)}-{fmtClock(timeWindow.end)}
              {view.windowSamples !== undefined ? ` · ${view.windowSamples} samples` : ''}
            </span>
            <button id="time-window-clear" title="Back to full session"
                    onClick={() => setTimeWindow(null)}>&times;</button>
          </span>
        )}
        <span className="diff-controls">
          <button id="diff-set-btn" className="diff-btn"
                  title="Snapshot the current profile as a comparison baseline"
                  onClick={() => {
                    if (Object.keys(perEvent).length === 0) {
                      showError('No profile data to baseline yet');
                      return;
                    }
                    const label = isReplayMode && replaySessionId
                      ? replaySessionId
                      : 'snapshot ' + new Date().toLocaleTimeString();
                    useLive.getState().setBaseline(perEvent, label);
                  }}>
            {baseline ? 'Re-baseline' : 'Set Baseline'}
          </button>
          {baseline && (
            <>
              <label id="diff-toggle-wrap" className="diff-toggle-wrap">
                <input type="checkbox" id="diff-toggle" checked={diffEnabled}
                       onChange={(e) => useLive.getState().setDiffEnabled(e.target.checked)} />
                {' '}Diff vs <span id="diff-label">{baseline.label}</span>
              </label>
              <button id="diff-clear-btn" className="diff-btn" title="Clear baseline"
                      onClick={() => useLive.getState().clearBaseline()}>
                &times;
              </button>
            </>
          )}
        </span>
      </div>

      <div id="source-banner" className={entry && !hasSource ? 'visible' : ''}>
        Source view unavailable &mdash; start server with <code>--binary</code> to enable
      </div>

      <div id="tabs">
        {TABS.map((t) => (
          <button key={t.id} className={'tab' + (activeTab === t.id ? ' active' : '')}
                  data-tab={t.id} onClick={() => switchTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>

      <div id="tab-functions"
           className={'tab-content' + (activeTab === 'functions' ? ' active' : '')}>
        <FunctionTable data={view.functionSummary} baselineMap={baselineMap}
                       onSelectFunction={showSourceForFunction} />
      </div>

      <div id="tab-source" className={'tab-content' + (activeTab === 'source' ? ' active' : '')}>
        {activeTab === 'source' && <SourceView />}
      </div>

      <div id="tab-flamegraph"
           className={'tab-content' + (activeTab === 'flamegraph' ? ' active' : '')}>
        {activeTab === 'flamegraph' && (
          <FlameGraph tree={view.flamegraph} totalSamples={view.totalSamples}
                      allowZoom={view.allowZoom} onShowSource={showSourceForFunction} />
        )}
      </div>

      <div id="tab-threads"
           className={'tab-content' + (activeTab === 'threads' ? ' active' : '')}>
        {activeTab === 'threads' && <ThreadsTab active={active && activeTab === 'threads'} />}
      </div>

      <div id="tab-sessions"
           className={'tab-content' + (activeTab === 'sessions' ? ' active' : '')}>
        {activeTab === 'sessions' && <SessionsTab />}
      </div>
    </>
  );
}
