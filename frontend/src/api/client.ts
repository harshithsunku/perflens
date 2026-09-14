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
export type SymbolizationStatus = Schemas['SymbolizationStatus'];
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

export async function unwrap<T>(r: Response): Promise<T> {
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

/** Every request is bounded: a half-open connection used to leave a fetch
 * pending forever, and the snapshot bookkeeping latched on it. Relayed agent
 * commands get the server's own timeout plus headroom. */
export const REQUEST_TIMEOUT_MS = 30_000;

/** Network and timeout failures as ApiError too, so callers see one shape. */
async function guarded(run: () => Promise<Response>): Promise<Response> {
  try {
    return await run();
  } catch (err) {
    if (err instanceof DOMException && err.name === 'TimeoutError') {
      throw new ApiError(0, 'timeout', 'the server did not answer in time');
    }
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError(0, 'aborted', 'request aborted');
    }
    throw new ApiError(0, 'network', 'cannot reach the server');
  }
}

async function getJson<T>(url: string, timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
  return unwrap<T>(await guarded(() =>
    fetch(url, { signal: AbortSignal.timeout(timeoutMs) })));
}

async function sendJson<T>(method: string, url: string, body?: unknown,
                           timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
  return unwrap<T>(await guarded(() => fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  })));
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

  connect: (host: string, port: number, token?: string) =>
    sendJson<ConnectResponse>('POST', '/api/agent/connect', { host, port, token }),

  /** `timeout` is the server-side bound (seconds, 1..600); the request itself
   * waits that long plus headroom, and the server stretches start/reprobe/
   * list_processes to at least 120 s on its own. */
  agentCommand: (cmd: string, args: Record<string, unknown> = {}, timeout = 30) => {
    const serverSecs = ['start', 'reprobe', 'list_processes'].includes(cmd)
      ? Math.max(timeout, 120) : timeout;
    return sendJson<AgentCommandResult>('POST', '/api/agent/command',
      { cmd, args, timeout }, (serverSecs + 15) * 1000);
  },

  config: () => getJson<ConfigState>('/api/config'),
  patchConfig: (update: ConfigUpdate) =>
    sendJson<ConfigState>('PATCH', '/api/config', update),

  // No timeout: a 500 MB upload through perf script takes as long as it
  // takes, and the server bounds it (413 / its own 300 s perf timeout).
  importPerfData: async (file: File): Promise<ImportResponse> =>
    unwrap<ImportResponse>(await guarded(() =>
      fetch('/api/sessions/import', { method: 'POST', body: file }))),
};

export const exportUrls = {
  flamegraphSvg: (event: string, sessionId: string) =>
    sessionId === 'live'
      ? `/api/live/export?format=svg&event=${q(event)}`
      : `/api/sessions/${q(sessionId)}/export?format=svg&event=${q(event)}`,
  // One event, like the SVG: the server refuses to merge several.
  collapsed: (event: string, sessionId: string) =>
    sessionId === 'live'
      ? `/api/live/export?format=collapsed&event=${q(event)}`
      : `/api/sessions/${q(sessionId)}/export?format=collapsed&event=${q(event)}`,
  json: (sessionId: string) =>
    sessionId === 'live'
      ? '/api/live/export?format=json'
      : `/api/sessions/${q(sessionId)}/export?format=json`,
};
