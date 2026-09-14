// The notify-and-fetch bookkeeping: what a data_version stamp makes the
// store fetch, and what it must not drop.

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/client', () => {
  class ApiError extends Error {
    code: string;
    status: number;
    constructor(status: number, code: string, message: string) {
      super(message);
      this.code = code;
      this.status = status;
    }
  }
  return { api: { snapshot: vi.fn() }, ApiError };
});

import { api, ApiError } from '../api/client';
import { useUi } from './ui';
import { _fetchState, _resetFetchState, useLive } from './live';

const snapshot = api.snapshot as unknown as ReturnType<typeof vi.fn>;

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function entry(total: number) {
  return { function_summary: { total_samples: total, functions: [] },
           flamegraph: { name: 'root', value: total, children: [] },
           source_files: [], threads: [] };
}

function resp(event: string, generation: number, chunk_count: number, total = 1) {
  return { event, data: entry(total),
           version: { generation, chunk_count, total_samples: total } };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  snapshot.mockReset();
  _resetFetchState();
  useLive.setState({
    isReplayMode: false, perEvent: {}, selectedEvent: 'cycles', eventTypes: [],
    generation: 0, chunkCount: 0, totalSamples: 0, snapshotError: null,
  });
  useUi.setState({ error: null, errorSticky: false });
});

describe('fetchPerEvent / onDataVersion', () => {
  it('coalesces stamps that land while a fetch is in flight', async () => {
    const first = deferred<ReturnType<typeof resp>>();
    snapshot.mockReturnValueOnce(first.promise);
    useLive.getState().onDataVersion({ generation: 1, chunk_count: 1, event_types: ['cycles'] });
    expect(snapshot).toHaveBeenCalledTimes(1);

    // Two more chunks arrive; no extra request until the first answers
    useLive.getState().onDataVersion({ generation: 1, chunk_count: 2 });
    useLive.getState().onDataVersion({ generation: 1, chunk_count: 3 });
    expect(snapshot).toHaveBeenCalledTimes(1);

    const second = deferred<ReturnType<typeof resp>>();
    snapshot.mockReturnValueOnce(second.promise);
    first.resolve(resp('cycles', 1, 1));
    await flush();
    // One catch-up fetch, not one per missed stamp
    expect(snapshot).toHaveBeenCalledTimes(2);
    second.resolve(resp('cycles', 1, 3, 7));
    await flush();
    expect(snapshot).toHaveBeenCalledTimes(2);
    expect(useLive.getState().totalSamples).toBe(7);
    expect(_fetchState().fetchedVersion).toBe(_fetchState().dataVersion);
  });

  it('issues an event switch made during a fetch instead of dropping it', async () => {
    const first = deferred<ReturnType<typeof resp>>();
    snapshot.mockReturnValueOnce(first.promise);
    useLive.getState().onDataVersion({ generation: 1, chunk_count: 1,
                                       event_types: ['cycles', 'instructions'] });
    useLive.getState().selectEvent('instructions');
    expect(snapshot).toHaveBeenCalledTimes(1);
    expect(_fetchState().pending).toEqual({ event: 'instructions', force: true });

    snapshot.mockResolvedValueOnce(resp('instructions', 1, 1, 3));
    first.resolve(resp('cycles', 1, 1, 9));
    await flush();
    expect(snapshot).toHaveBeenNthCalledWith(2, 'instructions');
    expect(useLive.getState().perEvent.instructions.function_summary.total_samples).toBe(3);
    expect(useLive.getState().totalSamples).toBe(3);
  });

  it('a new generation drops the old session and refetches', async () => {
    snapshot.mockResolvedValueOnce(resp('cycles', 1, 4, 100));
    useLive.getState().onDataVersion({ generation: 1, chunk_count: 4, event_types: ['cycles'] });
    await flush();
    expect(useLive.getState().perEvent.cycles).toBeDefined();

    // chunk_count restarts at 0 on a reset; the stamp is still newer
    snapshot.mockResolvedValueOnce(resp('cycles', 2, 1, 5));
    useLive.getState().onDataVersion({ generation: 2, chunk_count: 1, event_types: ['cycles'] });
    expect(useLive.getState().generation).toBe(2);
    expect(snapshot).toHaveBeenCalledTimes(2);
    await flush();
    expect(useLive.getState().totalSamples).toBe(5);
  });

  it('picks a concrete PMU event when the server says the name is ambiguous', async () => {
    snapshot.mockRejectedValueOnce(new ApiError(400, 'ambiguous_event',
      "'cycles' matches several events on this device; ask for one of: "
      + 'cpu_atom/cycles/, cpu_core/cycles/'));
    snapshot.mockResolvedValueOnce(resp('cpu_core/cycles/', 1, 1, 2));
    useLive.getState().onDataVersion({ generation: 1, chunk_count: 1 });
    await flush();
    expect(useLive.getState().selectedEvent).toBe('cpu_core/cycles/');
    expect(snapshot).toHaveBeenNthCalledWith(2, 'cpu_core/cycles/');
    expect(useUi.getState().error).toBeNull();
  });

  it('reports a failed fetch instead of latching in-flight forever', async () => {
    snapshot.mockRejectedValueOnce(new ApiError(0, 'network', 'cannot reach the server'));
    useLive.getState().onDataVersion({ generation: 1, chunk_count: 1, event_types: ['cycles'] });
    await flush();
    expect(_fetchState().fetching).toBe(false);
    expect(useLive.getState().snapshotError).toContain('cannot reach');
    expect(useUi.getState().error).toContain('cannot reach the server');
    expect(useUi.getState().errorSticky).toBe(true);
    // The next stamp fetches again
    snapshot.mockResolvedValueOnce(resp('cycles', 1, 2));
    useLive.getState().onDataVersion({ generation: 1, chunk_count: 2 });
    expect(snapshot).toHaveBeenCalledTimes(2);
  });

  it('selects a sensible event when the reported set changes', () => {
    snapshot.mockResolvedValue(resp('cpu_core/cycles/', 1, 1));
    useLive.getState().onDataVersion({ generation: 1, chunk_count: 1,
      event_types: ['cpu_atom/branch-instructions/', 'cpu_atom/cycles/',
                    'cpu_core/branch-instructions/', 'cpu_core/cycles/'] });
    expect(useLive.getState().selectedEvent).toBe('cpu_core/cycles/');
  });

  it('does nothing in replay mode', () => {
    useLive.setState({ isReplayMode: true });
    useLive.getState().onDataVersion({ generation: 1, chunk_count: 1 });
    expect(snapshot).not.toHaveBeenCalled();
  });
});
