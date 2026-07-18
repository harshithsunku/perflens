// SSE wiring: /api/stream events → zustand stores. Reconnects with a 3s
// backoff (EventSource's own retry only covers some failure modes).

import { api } from './client';
import type { MetricsFrame } from './client';
import { METRICS_MAX, useLive } from '../store/live';
import { useUi } from '../store/ui';

let source: EventSource | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | undefined;

export function connectSSE(): void {
  if (source) {
    source.close();
    source = null;
  }
  const es = new EventSource('/api/stream');
  source = es;
  const live = () => useLive.getState();

  es.addEventListener('status', (e) => {
    const data = JSON.parse(e.data) as { connected: boolean; agent: string | null };
    useLive.setState({ connected: data.connected, agentAddr: data.agent ?? null });
    if (data.connected) live().exitReplay();
  });

  es.addEventListener('data_version', (e) => {
    // Carries event_types alongside the version stamp (v2)
    live().onDataVersion(JSON.parse(e.data));
  });

  es.addEventListener('perf_stat', (e) => {
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
    useLive.setState({ connected: false, agentAddr: null });
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectSSE, 3000);
  };

  es.onopen = () => {
    // Backfill metrics history on (re)connect
    api.metricsHistory('system').then((h) => {
      if (h.length > 0) {
        useLive.setState({
          metricsSystem: h.slice(-METRICS_MAX) as MetricsFrame[],
          metricsVisible: true,
        });
      }
    }).catch(() => {});
    api.metricsHistory('process').then((h) => {
      if (h.length > 0) {
        useLive.setState({ metricsProcess: h.slice(-METRICS_MAX) as MetricsFrame[] });
      }
    }).catch(() => {});
  };
}

export function disconnectSSE(): void {
  clearTimeout(reconnectTimer);
  if (source) {
    source.close();
    source = null;
  }
}
