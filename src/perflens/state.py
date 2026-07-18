"""Profiling and metrics state, plus the background aggregation worker.

All state here is owned by plain threads (agent recv loops feed it, the
rebuild worker folds it); the HTTP layer only reads snapshots under the
lock. Nothing in this module touches sockets or FastAPI.
"""

import collections
import sys
import threading
import time

from perflens.aggregator import AggregatorSet
from perflens.parser import merge_perf_stat


class ProfilingState:
    """Shared state for streaming data to UI. Thread-safe."""

    def __init__(self, max_samples=500000):
        self.lock = threading.Lock()
        self.all_samples = collections.deque(maxlen=max_samples)
        self.chunk_count = 0
        self.last_update = 0
        self.agent_connected = False
        self.agent_addr = None
        self.agent_conn = None     # active agent socket, for /api/stop
        self.event_types = []
        self.perf_stat = {}
        self.source_mapper = None  # SourceMapper, set at startup
        # Running set — event types only accumulate, never removed
        # (event types don't change mid-session in practice)
        self._event_types_set = set()
        # Incremental per-event aggregation. Chunks queue here and the
        # rebuild worker folds them in off the recv thread; accumulators
        # cover the whole session (the deque above is only the raw window
        # backing thread/source drill-downs).
        self.aggregators = AggregatorSet()
        self._pending_chunks = []
        # Background rebuild state
        self._rebuild_needed = threading.Condition(self.lock)
        self._dirty = False
        self._cached_per_event = {}

    def add_samples(self, new_samples, perf_stat=None):
        """Add samples and return (total_count, event_types_copy)."""
        # Stamp arrival time — /api/time-window filters the raw deque by it
        now = time.time()
        for s in new_samples:
            s['recv_ts'] = now
        with self.lock:
            self.all_samples.extend(new_samples)
            self.chunk_count += 1
            self.last_update = time.time()
            self._event_types_set.update(
                s['event_type'] for s in new_samples)
            self.event_types = sorted(self._event_types_set)
            if perf_stat:
                self.perf_stat = merge_perf_stat(self.perf_stat, perf_stat)
            self._pending_chunks.append(new_samples)
            self._dirty = True
            self._rebuild_needed.notify()
            return len(self.all_samples), list(self.event_types)

    def get_snapshot(self):
        with self.lock:
            return {
                'all_samples': list(self.all_samples),
                'event_types': list(self.event_types),
                'perf_stat': dict(self.perf_stat),
            }

    def reset(self):
        with self.lock:
            self.all_samples.clear()
            self.chunk_count = 0
            self.event_types = []
            self._event_types_set.clear()
            self.perf_stat = {}
            self._pending_chunks = []
            self._cached_per_event = {}
            self._dirty = False
            # Swap (don't reset in place): the rebuild worker may be folding
            # already-popped chunks into the old set right now — folding into
            # a discarded object is harmless, folding into a fresh one would
            # leak the previous session's samples into the new session.
            self.aggregators = AggregatorSet()


class MetricsState:
    """Stores device health metrics history in memory. Thread-safe."""

    def __init__(self, max_entries=1800):
        # 1800 entries = 1 hour at 2-second intervals
        self.lock = threading.Lock()
        self.system_history = []
        self.process_history = []
        self.network_history = []
        self.disk_history = []
        self.threads_history = []
        self._max = max_entries

    def _history_for(self, metrics_type):
        return {
            'system': self.system_history,
            'process': self.process_history,
            'network': self.network_history,
            'disk': self.disk_history,
            'threads': self.threads_history,
        }.get(metrics_type)

    def add(self, metrics_type, metrics):
        with self.lock:
            history = self._history_for(metrics_type)
            if history is not None:
                self._append(history, metrics)

    def _append(self, history, entry):
        history.append(entry)
        if len(history) > self._max:
            trim = self._max // 10
            del history[:trim]

    def get_history(self, metrics_type, start_ts=None, end_ts=None):
        with self.lock:
            source = self._history_for(metrics_type)
            if source is None:
                return []
            if start_ts is None and end_ts is None:
                return list(source)
            return [m for m in source
                    if (start_ts is None or m.get('ts', 0) >= start_ts) and
                       (end_ts is None or m.get('ts', 0) <= end_ts)]

    def get_latest(self):
        with self.lock:
            result = {}
            if self.system_history:
                result['system'] = self.system_history[-1]
            if self.process_history:
                result['process'] = self.process_history[-1]
            if self.network_history:
                result['network'] = self.network_history[-1]
            if self.disk_history:
                result['disk'] = self.disk_history[-1]
            if self.threads_history:
                result['threads'] = self.threads_history[-1]
            return result

    def get_summary(self):
        """Compute summary stats for session metadata."""
        with self.lock:
            if not self.system_history:
                return None
            cpu_vals = [m['cpu']['overall_pct'] for m in self.system_history
                        if m.get('cpu', {}).get('overall_pct') is not None]
            mem_vals = [m['mem']['used_pct'] for m in self.system_history
                        if m.get('mem', {}).get('used_pct') is not None]
            temp_vals = [m['temp_c'] for m in self.system_history
                         if m.get('temp_c') is not None]
            n = len(self.system_history)
            summary = {'snapshots': n}
            if n >= 2:
                summary['duration_sec'] = round(
                    self.system_history[-1].get('ts', 0) -
                    self.system_history[0].get('ts', 0), 1)
            if cpu_vals:
                summary['avg_cpu_pct'] = round(sum(cpu_vals) / len(cpu_vals), 1)
                summary['max_cpu_pct'] = round(max(cpu_vals), 1)
            if mem_vals:
                summary['avg_mem_pct'] = round(sum(mem_vals) / len(mem_vals), 1)
                summary['max_mem_pct'] = round(max(mem_vals), 1)
            if temp_vals:
                summary['avg_temp_c'] = round(sum(temp_vals) / len(temp_vals))
                summary['max_temp_c'] = max(temp_vals)
            return summary

    def snapshot_for_save(self):
        """Return all metrics for session save."""
        with self.lock:
            snap = {
                'system': list(self.system_history),
                'process': list(self.process_history),
                'network': list(self.network_history),
            }
            if self.disk_history:
                snap['disk'] = list(self.disk_history)
            if self.threads_history:
                snap['threads'] = list(self.threads_history)
            return snap

    def reset(self):
        with self.lock:
            self.system_history.clear()
            self.process_history.clear()
            self.network_history.clear()
            self.disk_history.clear()
            self.threads_history.clear()


def rebuild_worker(ctx):
    """Background thread: folds newly arrived chunks into the incremental
    per-event accumulators and broadcasts fresh snapshots.

    Only the NEW chunks are processed (inline expansion + addr2line included)
    — cost per wakeup is O(new samples), not O(all samples). Coalesces rapid
    updates: chunks that arrive while folding are picked up next loop.
    """
    state = ctx.state
    while True:
        with state.lock:
            while not state._dirty:
                state._rebuild_needed.wait()
            state._dirty = False
            pending = state._pending_chunks
            state._pending_chunks = []
            aggs = state.aggregators

        if not pending:
            continue

        mapper = state.source_mapper
        try:
            for chunk in pending:
                aggs.add_chunk(chunk, mapper)
            per_event = aggs.snapshot_per_event(mapper)
        except Exception as e:
            # Never let one bad chunk (or a mapper hiccup) kill the worker —
            # that would silently freeze all UI updates for the session.
            import traceback
            print(f"[server] Rebuild worker error: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            continue

        with state.lock:
            if state.aggregators is not aggs:
                continue  # session was reset mid-fold — discard stale build
            state._cached_per_event = per_event
            version = {
                'chunk_count': state.chunk_count,
                'total_samples': len(state.all_samples),
                'event_types': list(state.event_types),
            }

        # Notify-and-fetch: browsers get a tiny version stamp and pull the
        # event they're actually viewing from /api/per-event — the full
        # per-event blob (multi-MB on big profiles) is never broadcast.
        ctx.broadcast('data_version', version)
