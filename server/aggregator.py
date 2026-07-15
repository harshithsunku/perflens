"""Incremental per-event aggregation.

Replaces the full re-aggregation of every accumulated sample on each chunk
(O(total) every ~8s, the main large-codebase bottleneck) with accumulators
that are updated only with the new chunk's samples:

  - add_chunk() is O(new_samples x stack_depth)
  - snapshot() is O(unique_functions + tree_nodes), cached until new data

Aggregates cover the whole session: unlike the raw sample deque (which is a
ring buffer capped by --max-samples and backs the thread/source drill-down
views), accumulator totals never evict.
"""

import threading
from collections import defaultdict


class EventAccumulator:
    """Running function summary + flamegraph tree for one event type."""

    def __init__(self, event_type):
        self.event_type = event_type
        self.total_samples = 0
        self._self_counts = defaultdict(int)    # (func, module) -> leaf count
        self._total_counts = defaultdict(int)   # (func, module) -> stack count
        self._root = {'name': 'root', 'value': 0, 'children': [], '_cmap': {}}
        self._threads = {}                      # tid -> comm (first seen)
        # Source-file mapping (from unexpanded samples via the mapper)
        self._file_samples = defaultdict(int)   # fpath -> sample count
        self._file_functions = defaultdict(set) # fpath -> set(func names)
        self._file_found = {}                   # fpath -> bool (lazy)
        self._dirty = True
        self._snapshot = None

    # -- ingest ------------------------------------------------------------

    def add_samples(self, expanded_samples):
        """Fold inline-expanded samples of this event into the aggregates."""
        for sample in expanded_samples:
            self.total_samples += 1
            tid = sample.get('tid', sample.get('pid', 0))
            if tid not in self._threads:
                self._threads[tid] = sample.get('comm', '')

            frames = sample['frames']
            if not frames:
                continue

            leaf = frames[0]
            self._self_counts[(leaf['func'], leaf['module'])] += 1

            seen = set()
            for frame in frames:
                key = (frame['func'], frame['module'])
                if key not in seen:
                    seen.add(key)
                    self._total_counts[key] += 1

            # Flamegraph tree — keeps its child map permanently so inserts
            # stay O(depth) per sample
            root = self._root
            root['value'] += 1
            node = root
            for frame in reversed(frames):
                func_name = frame['func']
                child = node['_cmap'].get(func_name)
                if child is None:
                    child = {'name': func_name, 'value': 0, 'children': [],
                             '_cmap': {}, '_inlined': False,
                             '_module': frame.get('module', '')}
                    node['children'].append(child)
                    node['_cmap'][func_name] = child
                child['value'] += 1
                if frame.get('inlined'):
                    child['_inlined'] = True
                node = child

        self._dirty = True

    def add_source_lines(self, line_data, orig_samples, mapper):
        """Merge per-chunk source line data (mapper.map_samples_to_lines
        output for THIS event's unexpanded samples) into the file index."""
        for fpath, lines in line_data.items():
            self._file_samples[fpath] += sum(
                d['samples'] for d in lines.values())

        # Leaf-function names per file, from the mapper's addr2line cache
        # (map_samples_to_lines just resolved these addresses)
        for sample in orig_samples:
            if not sample['frames']:
                continue
            frame = sample['frames'][0]
            binary = (mapper.binary_path
                      or mapper._resolve_module_path(frame.get('module', '')))
            if not binary:
                continue
            vaddr = mapper._compute_vaddr(frame, binary)
            if vaddr is None:
                continue
            fpath, lineno = mapper._addr2line_cache.get(
                (binary, vaddr), ('??', 0))
            if fpath != '??' and lineno > 0:
                self._file_functions[fpath].add(frame['func'])

        self._dirty = True

    # -- snapshot ------------------------------------------------------------

    def snapshot(self, mapper=None):
        """Serializable per-event entry, cached until new samples arrive.

        Shape matches the old batch build_per_event_data() entry exactly:
        {function_summary, flamegraph, source_files, threads}.
        """
        if not self._dirty and self._snapshot is not None:
            return self._snapshot

        total = self.total_samples
        func_list = []
        all_keys = set(self._self_counts) | set(self._total_counts)
        for key in all_keys:
            func, module = key
            sc = self._self_counts.get(key, 0)
            tc = self._total_counts.get(key, 0)
            func_list.append({
                'name': func,
                'module': module,
                'samples': sc,
                'percent': round(100.0 * sc / total, 2) if total else 0,
                'self_samples': sc,
                'self_percent': round(100.0 * sc / total, 2) if total else 0,
                'total_samples': tc,
                'total_percent': round(100.0 * tc / total, 2) if total else 0,
            })
        func_list.sort(key=lambda x: x['self_samples'], reverse=True)

        source_files = []
        for fpath, count in self._file_samples.items():
            found = self._file_found.get(fpath)
            if found is None:
                found = (mapper._find_source_file(fpath) is not None
                         if mapper else False)
                self._file_found[fpath] = found
            source_files.append({
                'path': fpath,
                'found': found,
                'total_samples': count,
                'functions': sorted(self._file_functions.get(fpath, ())),
            })
        source_files.sort(key=lambda x: x['total_samples'], reverse=True)

        self._snapshot = {
            'function_summary': {
                'total_samples': total,
                'functions': func_list,
            },
            'flamegraph': _copy_tree(self._root),
            'source_files': source_files,
            'threads': [{'tid': t, 'comm': c} for t, c in
                        sorted(self._threads.items(), key=lambda x: x[0])],
        }
        self._dirty = False
        return self._snapshot


def _copy_tree(root):
    """Copy the mutable flamegraph tree into the serializable shape
    (drop _cmap, promote _inlined/_module), iteratively — perf stacks plus
    inline expansion can exceed Python's recursion limit."""
    out_root = {'name': root['name'], 'value': root['value'], 'children': []}
    stack = [(root, out_root)]
    while stack:
        src, dst = stack.pop()
        for child in src['children']:
            out = {'name': child['name'], 'value': child['value'],
                   'children': []}
            if child.get('_inlined'):
                out['inlined'] = True
            if child.get('_module'):
                out['module'] = child['_module']
            dst['children'].append(out)
            stack.append((child, out))
    return out_root


class AggregatorSet:
    """All per-event accumulators + chunk routing. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._accs = {}   # event_type -> EventAccumulator

    def add_chunk(self, samples, mapper):
        """Fold one chunk (all events mixed, unexpanded) into the
        accumulators. Inline expansion and addr2line resolution happen here,
        once, for the new samples only."""
        if not samples:
            return
        expanded = (mapper.expand_inline_frames(samples)
                    if mapper else samples)

        by_event_exp = defaultdict(list)
        for s in expanded:
            by_event_exp[s['event_type']].append(s)
        by_event_orig = defaultdict(list)
        for s in samples:
            by_event_orig[s['event_type']].append(s)

        with self._lock:
            for evt, group in by_event_exp.items():
                acc = self._accs.get(evt)
                if acc is None:
                    acc = self._accs[evt] = EventAccumulator(evt)
                acc.add_samples(group)
                if mapper:
                    orig_group = by_event_orig.get(evt, [])
                    line_data = mapper.map_samples_to_lines(orig_group)
                    acc.add_source_lines(line_data, orig_group, mapper)

    def snapshot_per_event(self, mapper=None):
        with self._lock:
            return {evt: acc.snapshot(mapper)
                    for evt, acc in sorted(self._accs.items())}

    def event_types(self):
        with self._lock:
            return sorted(self._accs)

    def reset(self):
        with self._lock:
            self._accs = {}


def build_per_event_batch(all_samples, mapper, source_builder=None):
    """One-shot batch aggregation (replay / import) through the same code
    path as live streaming.

    source_builder: optional callable(event_orig_samples) returning the
    annotated-source dict to attach as entry['source'].
    """
    aggs = AggregatorSet()
    aggs.add_chunk(all_samples, mapper)
    per_event = aggs.snapshot_per_event(mapper)

    if source_builder is not None:
        by_event = defaultdict(list)
        for s in all_samples:
            by_event[s['event_type']].append(s)
        for evt, entry in per_event.items():
            entry = dict(entry)
            entry['source'] = source_builder(by_event.get(evt, []))
            per_event[evt] = entry

    return per_event
