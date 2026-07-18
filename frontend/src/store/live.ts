// Central live-session store. Mirrors the vanilla UI's global `state`
// object; the notify-and-fetch cycle (SSE data_version stamp → pull
// /api/snapshot for the viewed event) lives here.

import { create } from 'zustand';
import { api } from '../api/client';
import type {
  MetricsFrame, PerEventEntry, SessionReplayResponse,
} from '../api/client';

export const METRICS_MAX = 150; // ~5 min at 2s interval

export interface TimeWindow { start: number; end: number }
export interface ThreadCpuInfo { pct: number; state: string }

export interface Baseline {
  label: string;
  perEvent: Record<string, PerEventEntry>;
}

interface LiveState {
  // Connection
  connected: boolean;
  agentAddr: string | null;
  platform: Record<string, unknown>;
  managedAgent: boolean;

  // Profile data
  totalSamples: number;
  chunkCount: number;
  eventTypes: string[];
  selectedEvent: string;
  perEvent: Record<string, PerEventEntry>;
  perfStat: Record<string, { value: number; comment?: string }>;
  lastUpdateTime: number | null;

  // Replay
  isReplayMode: boolean;
  replaySessionId: string | null;
  replayTimestamp: string | null;

  // View filters
  selectedTid: number | null;
  zoomNames: string[];
  currentSourceFile: string | null;
  pendingHighlight: string | null;
  timeWindow: TimeWindow | null;

  // Diff
  baseline: Baseline | null;
  diffEnabled: boolean;

  // Device health
  metricsSystem: MetricsFrame[];
  metricsProcess: MetricsFrame[];
  metricsNetwork: MetricsFrame | null;
  metricsPrevNetwork: MetricsFrame | null;
  metricsDisk: MetricsFrame | null;
  metricsPrevDisk: MetricsFrame | null;
  metricsThreads: MetricsFrame | null;
  metricsPrevThreads: MetricsFrame | null;
  threadLiveCpu: Record<number, ThreadCpuInfo>;
  metricsVisible: boolean;
  metricsCollapseLevel: number;

  // Actions
  fetchPerEvent: (event: string, force?: boolean) => void;
  onDataVersion: (v: { chunk_count?: number; event_types?: string[] | null }) => void;
  selectEvent: (evt: string) => void;
  selectTid: (tid: number | null) => void;
  setZoomNames: (names: string[]) => void;
  setTimeWindow: (w: TimeWindow | null) => void;
  setBaseline: (perEvent: Record<string, PerEventEntry>, label: string) => void;
  clearBaseline: () => void;
  setDiffEnabled: (on: boolean) => void;
  loadReplay: (sessionId: string, data: SessionReplayResponse) => void;
  exitReplay: () => void;
  pushSystemMetrics: (m: MetricsFrame) => void;
  pushProcessMetrics: (m: MetricsFrame) => void;
  pushNetworkMetrics: (m: MetricsFrame) => void;
  pushDiskMetrics: (m: MetricsFrame) => void;
  pushThreadMetrics: (m: MetricsFrame) => void;
}

// Notify-and-fetch bookkeeping (not reactive — no re-render on change)
let dataVersion = 0;
let fetchedVersion = -1;
let perEventFetching = false;

export const useLive = create<LiveState>((set, get) => ({
  connected: false,
  agentAddr: null,
  platform: {},
  managedAgent: false,

  totalSamples: 0,
  chunkCount: 0,
  eventTypes: [],
  selectedEvent: 'cycles',
  perEvent: {},
  perfStat: {},
  lastUpdateTime: null,

  isReplayMode: false,
  replaySessionId: null,
  replayTimestamp: null,

  selectedTid: null,
  zoomNames: [],
  currentSourceFile: null,
  pendingHighlight: null,
  timeWindow: null,

  baseline: null,
  diffEnabled: false,

  metricsSystem: [],
  metricsProcess: [],
  metricsNetwork: null,
  metricsPrevNetwork: null,
  metricsDisk: null,
  metricsPrevDisk: null,
  metricsThreads: null,
  metricsPrevThreads: null,
  threadLiveCpu: {},
  metricsVisible: false,
  metricsCollapseLevel: 0,

  fetchPerEvent: (event, force = false) => {
    const st = get();
    if (st.isReplayMode || !event) return;
    if (perEventFetching) return;
    if (!force && fetchedVersion >= dataVersion) return;
    perEventFetching = true;
    api.snapshot(event)
      .then((resp) => {
        perEventFetching = false;
        fetchedVersion = resp.version.chunk_count || 0;
        const cur = get();
        const update: Partial<LiveState> = {
          perEvent: { ...cur.perEvent, [resp.event]: resp.data },
          lastUpdateTime: Date.now(),
        };
        if (resp.event === cur.selectedEvent) {
          update.totalSamples = resp.data.function_summary.total_samples;
        }
        set(update);
        // Catch up if more chunks landed while we were fetching
        if (fetchedVersion < dataVersion) get().fetchPerEvent(get().selectedEvent);
      })
      .catch(() => { perEventFetching = false; });
  },

  onDataVersion: (v) => {
    if (get().isReplayMode) return;
    dataVersion = v.chunk_count || 0;
    const update: Partial<LiveState> = { chunkCount: v.chunk_count || 0 };
    if (v.event_types && v.event_types.length) {
      update.eventTypes = v.event_types;
      if (!v.event_types.includes(get().selectedEvent)) {
        update.selectedEvent = v.event_types[0];
      }
    }
    set(update);
    get().fetchPerEvent(get().selectedEvent);
  },

  selectEvent: (evt) => {
    set({ selectedEvent: evt, zoomNames: [], selectedTid: null });
    const st = get();
    const entry = st.perEvent[evt];
    if (entry) set({ totalSamples: entry.function_summary.total_samples });
    if (!st.isReplayMode) st.fetchPerEvent(evt, true);
  },

  selectTid: (tid) => set({ selectedTid: tid, zoomNames: [] }),

  setZoomNames: (names) => set({ zoomNames: names }),

  setTimeWindow: (w) => set({ timeWindow: w, zoomNames: [] }),

  setBaseline: (perEvent, label) =>
    set({ baseline: { label, perEvent: { ...perEvent } }, diffEnabled: true }),

  clearBaseline: () => set({ baseline: null, diffEnabled: false }),

  setDiffEnabled: (on) => set({ diffEnabled: on }),

  loadReplay: (sessionId, data) => {
    // Older sessions may have an empty event_types in metadata — the
    // per-event snapshot keys are authoritative either way
    const eventTypes = data.metadata.event_types?.length
      ? data.metadata.event_types
      : Object.keys(data.per_event ?? {}).sort();
    let selected = get().selectedEvent;
    if (!eventTypes.includes(selected)) selected = eventTypes[0] || 'cycles';
    set({
      perEvent: data.per_event as Record<string, PerEventEntry>,
      eventTypes,
      perfStat: (data.metadata.perf_stat ?? {}) as LiveState['perfStat'],
      selectedEvent: selected,
      timeWindow: null,
      totalSamples: data.metadata.total_samples ?? 0,
      zoomNames: [],
      selectedTid: null,
      lastUpdateTime: Date.now(),
      isReplayMode: true,
      replaySessionId: sessionId,
      replayTimestamp: data.metadata.timestamp ?? null,
    });
    const metrics = data.metrics as Record<string, MetricsFrame[]> | undefined;
    if (metrics) {
      const sys = (metrics.system ?? []).slice(-METRICS_MAX);
      const proc = (metrics.process ?? []).slice(-METRICS_MAX);
      const net = metrics.network ?? [];
      const disk = metrics.disk ?? [];
      set({
        metricsSystem: sys,
        metricsProcess: proc,
        metricsNetwork: net.length ? net[net.length - 1] : null,
        metricsPrevNetwork: net.length > 1 ? net[net.length - 2] : null,
        metricsDisk: disk.length ? disk[disk.length - 1] : null,
        metricsPrevDisk: disk.length > 1 ? disk[disk.length - 2] : null,
        metricsVisible: sys.length > 0 || get().metricsVisible,
      });
    }
  },

  exitReplay: () => {
    if (!get().isReplayMode) return;
    set({ isReplayMode: false, replaySessionId: null, replayTimestamp: null });
    // Force a live refetch — replay clobbered perEvent
    fetchedVersion = -1;
    get().fetchPerEvent(get().selectedEvent, true);
  },

  pushSystemMetrics: (m) => set((s) => ({
    metricsSystem: [...s.metricsSystem, m].slice(-METRICS_MAX),
    metricsVisible: true,
  })),

  pushProcessMetrics: (m) => set((s) => ({
    metricsProcess: [...s.metricsProcess, m].slice(-METRICS_MAX),
  })),

  pushNetworkMetrics: (m) => set((s) => ({
    metricsPrevNetwork: s.metricsNetwork, metricsNetwork: m,
  })),

  pushDiskMetrics: (m) => set((s) => ({
    metricsPrevDisk: s.metricsDisk, metricsDisk: m,
  })),

  pushThreadMetrics: (m) => set((s) => {
    // Cumulative ticks per thread → CPU% from consecutive-snapshot deltas
    const prev = s.metricsThreads as (MetricsFrame & {
      threads?: { tid: number; ticks: number; state?: string }[];
    }) | null;
    const cur = m as MetricsFrame & {
      threads?: { tid: number; ticks: number; state?: string }[];
      clk_tck?: number;
    };
    const map: Record<number, ThreadCpuInfo> = {};
    if (prev && cur.ts > prev.ts) {
      const dt = cur.ts - prev.ts;
      const clk = cur.clk_tck || 100;
      const prevByTid = new Map((prev.threads ?? []).map((t) => [t.tid, t]));
      for (const t of cur.threads ?? []) {
        const p = prevByTid.get(t.tid);
        if (p && t.ticks >= p.ticks) {
          map[t.tid] = {
            pct: ((t.ticks - p.ticks) / (dt * clk)) * 100,
            state: t.state || '',
          };
        }
      }
    }
    return { metricsPrevThreads: s.metricsThreads, metricsThreads: m, threadLiveCpu: map };
  }),
}));

/** Reset the module-level fetch bookkeeping (used by SSE reconnect). */
export function resetFetchVersion(): void {
  fetchedVersion = -1;
}
