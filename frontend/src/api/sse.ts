// SSE wiring: /api/stream events → zustand stores.
//
// Reconnects with exponential backoff (3 s → 30 s). EventSource's own
// retry only covers some failure modes; a server that is down for a
// while used to be hammered every 3 s and reported as "agent
// disconnected", which it is not: the browser lost the *server*. The
// store keeps the two apart (sseState vs connected), and the server's
// opening `status` frame settles the agent question on every (re)connect.

import { api } from './client';
import type { MetricsFrame } from './client';
import { METRICS_MAX, useLive } from '../store/live';
import { useUi } from '../store/ui';

export const BACKOFF_MIN_MS = 3000;
export const BACKOFF_MAX_MS = 30_000;

let source: EventSource | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
let backoffMs = BACKOFF_MIN_MS;

/** Next reconnect delay: doubles from 3 s up to 30 s. Exported for tests. */
export function nextBackoff(current: number): number {
  return Math.min(BACKOFF_MAX_MS, current * 2);
}

export function connectSSE(): void {
  clearTimeout(reconnectTimer);
  if (source) {
    source.close();
    source = null;
  }
  const es = new EventSource('/api/stream');
  source = es;
  const live = () => useLive.getState();

  // The server sends this first on every connection, so the browser
  // learns the agent state without a separate /api/status round trip.
  // Deliberately no exitReplay(): a `--server` agent reconnects every few
  // seconds after a drop, and a status frame on each reconnect used to
  // throw the operator out of the session they were reading. Leaving
  // replay is the operator's action (the banner's button).
  es.addEventListener('status', (e) => {
    const data = JSON.parse(e.data) as { connected: boolean; agent: string | null };
    live().onStatus(data.connected, data.agent ?? null);
  });

  es.addEventListener('data_version', (e) => {
    // Carries event_types alongside the version stamp (v2)
    live().onDataVersion(JSON.parse(e.data));
  });

  es.addEventListener('perf_stat', (e) => {
    if (live().isReplayMode) return;
    useLive.setState({ perfStat: JSON.parse(e.data) });
  });

  // One consolidated 'metrics' event; the payload's own `type` field
  // discriminates system/process/network/disk/threads (v2).
  es.addEventListener('metrics', (e) => {
    const frame = JSON.parse(e.data) as MetricsFrame;
    switch (frame.type) {
      case 'system': live().pushSystemMetrics(frame); break;
      case 'process': live().pushProcessMetrics(frame); break;
      case 'network': live().pushNetworkMetrics(frame); break;
      case 'disk': live().pushDiskMetrics(frame); break;
      case 'threads': live().pushThreadMetrics(frame); break;
    }
  });

  es.addEventListener('agent', (e) => {
    const data = JSON.parse(e.data) as { platform?: Record<string, unknown> };
    useLive.setState({ platform: data.platform ?? {}, managedAgent: true });
    if (useUi.getState().view === 'landing') {
      useUi.getState().showView('profiling');
    }
  });

  es.onerror = () => {
    es.close();
    if (source === es) source = null;
    useLive.setState({ sseState: 'reconnecting' });
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectSSE, backoffMs);
    backoffMs = nextBackoff(backoffMs);
  };

  es.onopen = () => {
    backoffMs = BACKOFF_MIN_MS;
    useLive.setState({ sseState: 'open' });
    // Backfill metrics history on (re)connect. Best-effort: a missing
    // history is not an error worth a banner.
    if (live().isReplayMode) return;
    api.metricsHistory('system').then((h) => {
      if (h.length > 0) {
        useLive.setState({
          metricsSystem: h.slice(-METRICS_MAX) as MetricsFrame[],
          metricsVisible: true,
        });
      }
    }).catch((err) => console.warn('metrics history:', err));
    api.metricsHistory('process').then((h) => {
      if (h.length > 0) {
        useLive.setState({ metricsProcess: h.slice(-METRICS_MAX) as MetricsFrame[] });
      }
    }).catch((err) => console.warn('metrics history:', err));
  };
}

export function disconnectSSE(): void {
  clearTimeout(reconnectTimer);
  backoffMs = BACKOFF_MIN_MS;
  if (source) {
    source.close();
    source = null;
  }
}
