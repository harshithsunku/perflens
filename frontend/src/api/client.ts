// Typed API client (v2). Response/request shapes come from the generated
// OpenAPI types (types.gen.ts) — regenerate with `npm run typegen`.
//
// Error model: every non-2xx response is
// {"error": {"code": "<slug>", "message": "..."}}.

import type { components } from './types.gen';

export type Schemas = components['schemas'];
export type Status = Schemas['Status'];
export type DataVersion = Schemas['DataVersion'];
export type ErrorDetail = Schemas['ErrorDetail'];
export type FlamegraphNode = Schemas['FlamegraphNode'];
export type FunctionEntry = Schemas['FunctionEntry'];
export type FunctionSummary = Schemas['FunctionSummary'];
export type PerEventEntry = Schemas['PerEventEntry'];
export type SnapshotResponse = Schemas['SnapshotResponse'];
export type SessionMetadata = Schemas['SessionMetadata'];
export type SessionListResponse = Schemas['SessionListResponse'];
export type SessionReplayResponse = Schemas['SessionReplayResponse'];
export type ThreadSummaryResponse = Schemas['ThreadSummaryResponse'];
export type ThreadViewResponse = Schemas['ThreadViewResponse'];
export type TimeWindowResponse = Schemas['TimeWindowResponse'];
export type SourceResponse = Schemas['SourceResponse'];
export type BrowseResponse = Schemas['BrowseResponse'];
export type WizardState = Schemas['WizardState'];
export type ConnectResponse = Schemas['ConnectResponse'];
export type AgentInfo = Schemas['AgentInfo'];
export type MetricsFrame = Schemas['MetricsFrame'];
export type IndexStatus = Schemas['IndexStatus'];
export type ImportResponse = Schemas['ImportResponse'];
export type ConfigState = Schemas['ConfigState'];
export type ConfigUpdate = Schemas['ConfigUpdate'];

/** Agent command responses are loosely shaped (the agent adds fields per
 * command); model the common ones. */
export interface AgentCommandResult {
  ok?: boolean;
  error?: string;
  [key: string]: unknown;
}

/** Error thrown for any non-2xx API response, carrying the v2 envelope. */
export class ApiError extends Error {
  code: string;
  status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

async function unwrap<T>(r: Response): Promise<T> {
  if (r.ok) return r.json() as Promise<T>;
  let code = 'http_error';
  let message = `HTTP ${r.status}`;
  if (r.headers.get('content-type')?.includes('json')) {
    const body = await r.json().catch(() => null) as
      { error?: { code?: string; message?: string } } | null;
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
    }
  }
  throw new ApiError(r.status, code, message);
}

async function getJson<T>(url: string): Promise<T> {
  return unwrap<T>(await fetch(url));
}

async function sendJson<T>(method: string, url: string, body?: unknown): Promise<T> {
  return unwrap<T>(await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }));
}

const q = encodeURIComponent;

export const api = {
  status: () => getJson<Status>('/api/status'),

  snapshot: (event: string) =>
    getJson<SnapshotResponse>(`/api/snapshot?event=${q(event)}`),

  disconnectAgent: () =>
    sendJson<{ stopped: boolean; reason?: string }>('DELETE', '/api/agent'),

  agentInfo: () => getJson<AgentInfo>('/api/agent'),

  sessions: () => getJson<SessionListResponse>('/api/sessions'),

  session: (id: string) =>
    getJson<SessionReplayResponse>(`/api/sessions/${q(id)}`),

  deleteSession: (id: string) =>
    sendJson<{ ok: boolean; session_id: string }>(
      'DELETE', `/api/sessions/${q(id)}`),

  threadSummary: (event: string) =>
    getJson<ThreadSummaryResponse>(`/api/threads?event=${q(event)}`),

  threadView: (event: string, tid: number) =>
    getJson<ThreadViewResponse>(`/api/threads/${tid}?event=${q(event)}`),

  timeWindow: (event: string, start: number, end: number, tid: number | null) =>
    getJson<TimeWindowResponse>(
      `/api/window?event=${q(event)}&start=${start}&end=${end}` +
      (tid !== null ? `&tid=${tid}` : '')),

  source: (file: string, event: string, tid?: number) =>
    getJson<SourceResponse>(
      `/api/source?file=${q(file)}&event=${q(event)}` +
      (tid !== undefined ? `&tid=${tid}` : '')),

  metricsHistory: (type: string) =>
    getJson<MetricsFrame[]>(`/api/metrics/history?type=${q(type)}`),

  indexStatus: () => getJson<IndexStatus>('/api/index/status'),

  browse: (path: string) =>
    getJson<BrowseResponse>(`/api/browse?path=${q(path)}`),

  wizardState: () => getJson<WizardState>('/api/wizard'),
  saveWizardState: (updates: Record<string, unknown>) =>
    sendJson<WizardState>('PUT', '/api/wizard', updates),

  connect: (host: string, port: number) =>
    sendJson<ConnectResponse>('POST', '/api/agent/connect', { host, port }),

  agentCommand: (cmd: string, args: Record<string, unknown> = {}, timeout = 30) =>
    sendJson<AgentCommandResult>('POST', '/api/agent/command',
      { cmd, args, timeout }),

  config: () => getJson<ConfigState>('/api/config'),
  patchConfig: (update: ConfigUpdate) =>
    sendJson<ConfigState>('PATCH', '/api/config', update),

  importPerfData: async (file: File): Promise<ImportResponse> =>
    unwrap<ImportResponse>(
      await fetch('/api/sessions/import', { method: 'POST', body: file })),
};

export const exportUrls = {
  flamegraphSvg: (event: string, sessionId: string) =>
    sessionId === 'live'
      ? `/api/live/export?format=svg&event=${q(event)}`
      : `/api/sessions/${q(sessionId)}/export?format=svg&event=${q(event)}`,
  collapsed: (sessionId: string) =>
    sessionId === 'live'
      ? '/api/live/export?format=collapsed'
      : `/api/sessions/${q(sessionId)}/export?format=collapsed`,
  json: (sessionId: string) =>
    sessionId === 'live'
      ? '/api/live/export?format=json'
      : `/api/sessions/${q(sessionId)}/export?format=json`,
};
