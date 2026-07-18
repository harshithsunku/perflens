// Typed API client. Response/request shapes come from the generated
// OpenAPI types (types.gen.ts) — regenerate with `npm run typegen`.

import type { components } from './types.gen';

export type Schemas = components['schemas'];
export type Status = Schemas['Status'];
export type DataVersion = Schemas['DataVersion'];
export type FlamegraphNode = Schemas['FlamegraphNode'];
export type FunctionEntry = Schemas['FunctionEntry'];
export type FunctionSummary = Schemas['FunctionSummary'];
export type PerEventEntry = Schemas['PerEventEntry'];
export type PerEventResponse = Schemas['PerEventResponse'];
export type SessionMetadata = Schemas['SessionMetadata'];
export type SessionReplayResponse = Schemas['SessionReplayResponse'];
export type ThreadSummaryResponse = Schemas['ThreadSummaryResponse'];
export type ThreadViewResponse = Schemas['ThreadViewResponse'];
export type TimeWindowResponse = Schemas['TimeWindowResponse'];
export type SourceResponse = Schemas['SourceResponse'];
export type BrowseResponse = Schemas['BrowseResponse'];
export type WizardState = Schemas['WizardState'];
export type ConnectResponse = Schemas['ConnectResponse'];
export type MetricsFrame = Schemas['MetricsFrame'];
export type IndexStatus = Schemas['IndexStatus'];
export type ImportResponse = Schemas['ImportResponse'];

/** Agent command responses are loosely shaped (the agent adds fields per
 * command); model the common ones. */
export interface AgentCommandResult {
  ok?: boolean;
  error?: string;
  [key: string]: unknown;
}

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok && r.headers.get('content-type')?.includes('json')) {
    const body = await r.json().catch(() => null) as { error?: string } | null;
    throw new Error(body?.error ?? `HTTP ${r.status}`);
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json() as Promise<T>;
}

const q = encodeURIComponent;

export const api = {
  status: () => getJson<Status>('/api/status'),

  perEvent: (event: string) =>
    getJson<PerEventResponse>(`/api/per-event?event=${q(event)}`),

  stop: () => getJson<{ stopped: boolean; error?: string }>('/api/stop'),

  sessions: () => getJson<SessionMetadata[]>('/api/sessions'),

  session: (id: string) =>
    getJson<SessionReplayResponse & { error?: string }>(`/api/sessions/${q(id)}`),

  threadSummary: (event: string) =>
    getJson<ThreadSummaryResponse>(`/api/thread-summary?event=${q(event)}`),

  threadView: (event: string, tid: number) =>
    getJson<ThreadViewResponse>(`/api/thread-view?event=${q(event)}&tid=${tid}`),

  timeWindow: (event: string, start: number, end: number, tid: number | null) =>
    getJson<TimeWindowResponse & { error?: string }>(
      `/api/time-window?event=${q(event)}&start=${start}&end=${end}` +
      (tid !== null ? `&tid=${tid}` : '')),

  source: (file: string, event: string, tid?: number) =>
    getJson<SourceResponse>(
      `/api/source?file=${q(file)}&event=${q(event)}` +
      (tid !== undefined ? `&tid=${tid}` : '')),

  metricsHistory: (type: string) =>
    getJson<MetricsFrame[]>(`/api/metrics/history?type=${q(type)}`),

  indexStatus: () => getJson<IndexStatus>('/api/index/status'),

  browse: (path: string) =>
    getJson<BrowseResponse & { error?: string }>(`/api/browse?path=${q(path)}`),

  wizardState: () => getJson<WizardState>('/api/wizard/state'),
  saveWizardState: (updates: Record<string, unknown>) =>
    postJson<WizardState>('/api/wizard/state', updates),

  connect: (host: string, port: number) =>
    postJson<ConnectResponse>('/api/connect', { host, port }),

  agentCommand: (cmd: string, args: Record<string, unknown> = {}, timeout = 30) =>
    postJson<AgentCommandResult>('/api/agent/command', { cmd, args, timeout }),

  configBinary: (path: string) =>
    postJson<{ ok: boolean; error?: string }>('/api/config/binary', { path }),
  configSource: (path: string) =>
    postJson<{ ok: boolean; error?: string }>('/api/config/source', { path }),
  configToolchain: (body: { prefix?: string; sysroot?: string }) =>
    postJson<{ ok: boolean; error?: string }>('/api/config/toolchain', body),

  importPerfData: async (file: File): Promise<ImportResponse & { error?: string }> => {
    const r = await fetch('/api/import', { method: 'POST', body: file });
    return r.json();
  },
};

export const exportUrls = {
  flamegraphSvg: (event: string, sessionId: string) =>
    `/api/export/flamegraph?event=${q(event)}&session=${q(sessionId)}`,
  collapsed: (sessionId: string) =>
    `/api/export/session/${q(sessionId)}?format=collapsed`,
  json: (sessionId: string) =>
    `/api/export/session/${q(sessionId)}?format=json`,
};
