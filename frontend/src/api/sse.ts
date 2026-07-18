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

  es.addEventListener('event_types', (e) => {
    const types = JSON.parse(e.data) as string[];
    useLive.setState((s) => {
      const update: Record<string, unknown> = { eventTypes: types };
      if (!types.includes(s.selectedEvent) && types.length) {
        update.selectedEvent = types[0];
      }
      return update;
    });
  });

  es.addEventListener('data_version', (e) => {
    live().onDataVersion(JSON.parse(e.data));
  });

  es.addEventListener('perf_stat', (e) => {
    useLive.setState({ perfStat: JSON.parse(e.data) });
  });

  es.addEventListener('metrics_system', (e) => {
    live().pushSystemMetrics(JSON.parse(e.data));
  });
  es.addEventListener('metrics_process', (e) => {
    live().pushProcessMetrics(JSON.parse(e.data));
  });
  es.addEventListener('metrics_network', (e) => {
    live().pushNetworkMetrics(JSON.parse(e.data));
  });
  es.addEventListener('metrics_disk', (e) => {
    live().pushDiskMetrics(JSON.parse(e.data));
  });
  es.addEventListener('metrics_threads', (e) => {
    live().pushThreadMetrics(JSON.parse(e.data));
  });

  es.addEventListener('agent_connected', (e) => {
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
