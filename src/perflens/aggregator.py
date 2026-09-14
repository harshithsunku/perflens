"""Incremental per-event aggregation.

Replaces the full re-aggregation of every accumulated sample on each chunk
(O(total) every ~8s, the main large-codebase bottleneck) with accumulators
that are updated only with the new chunk's samples:

  - add_chunk() is O(new_samples x stack_depth)
  - snapshot() is O(unique_functions) when dirty, O(1) otherwise; the
    flamegraph tree is served in place, never copied
  - blobs() serializes a dirty event once; /api/snapshot splices the bytes

Aggregates cover the whole session: unlike the raw sample deque (which is a
ring buffer capped by --max-samples and backs the thread/source drill-down
views), accumulator totals never evict.
"""

import threading
from collections import defaultdict

from perflens.api.responses import deflate_segment, dumps
from perflens.parser import MAX_FLAMEGRAPH_DEPTH


class EventAccumulator:
    """Running function summary + flamegraph tree for one event type."""

    def __init__(self, event_type):
        self.event_type = event_type
        self.total_samples = 0
        self._self_counts = defaultdict(int)    # (func, module) -> leaf count
        self._total_counts = defaultdict(int)   # (func, module) -> stack count
        # The tree is kept in its serializable shape: {name, value, children,
        # [module], [inlined], [truncated]}. The per-node child lookup maps
        # live beside it, keyed by node identity, so a snapshot never has to
        # copy the tree to strip them -- which it did, for every event, on
        # every chunk, and which grew with the session rather than the chunk.
        self._root = {'name': 'root', 'value': 0, 'children': []}
        self._cmaps = {id(self._root): {}}      # id(node) -> {func: child}
        self._threads = {}                      # tid -> comm (first seen)
        # Source-file mapping (from unexpanded samples via the mapper)
        self._file_samples = defaultdict(int)   # fpath -> sample count
        self._file_functions = defaultdict(set) # fpath -> set(func names)
        self._file_found = {}                   # fpath -> bool (lazy)
        self._dirty = True
        self._snapshot = None
        self._blob = None                       # (json bytes, deflate segment)

    # -- ingest ------------------------------------------------------------

    def add_samples(self, expanded_samples):
        """Fold inline-expanded samples of this event into the aggregates."""
        cmaps = self._cmaps
        root = self._root
        root_cmap = cmaps[id(root)]
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

            # Flamegraph tree -- inserts stay O(depth) per sample. Depth is
            # capped here, at insert time: orjson cannot encode past a fixed
            # nesting depth, so an uncapped deep tree made /api/snapshot
            # return 500 and blanked the whole UI. See MAX_FLAMEGRAPH_DEPTH.
            root['value'] += 1
            node = root
            cmap = root_cmap
            depth = 0
            for frame in reversed(frames):
                if depth >= MAX_FLAMEGRAPH_DEPTH:
                    node['truncated'] = True
                    break
                func_name = frame['func']
                child = cmap.get(func_name)
                if child is None:
                    child = {'name': func_name, 'value': 0, 'children': []}
                    module = frame.get('module', '')
                    if module:
                        child['module'] = module
                    node['children'].append(child)
                    cmap[func_name] = child
                    cmaps[id(child)] = {}
                child['value'] += 1
                if frame.get('inlined'):
                    child['inlined'] = True
                node = child
                cmap = cmaps[id(child)]
                depth += 1

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
            # Must match what map_samples_to_lines used, or the
            # _addr2line_cache lookup below misses: it keys on (binary,
            # vaddr), and _binary_for_frame is what filled it.
            binary = mapper._binary_for_frame(frame)
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
        """Serializable per-event entry, cached until new samples arrive:
        {function_summary, flamegraph, source_files, threads}.

        The flamegraph is the live tree, not a copy. Callers read it or
        serialize it; they do not mutate it, and they do not serialize it
        while add_samples may be running (the rebuild worker holds the set's
        lock for both, and /api/snapshot serves the bytes from blob()).
        """
        if not self._dirty and self._snapshot is not None:
            return self._snapshot

        total = self.total_samples
        func_list = []
        # Deterministic union (dict preserves insertion order) — a set here
        # would hash-randomize the order of equal-count functions per process
        all_keys = dict.fromkeys(self._total_counts)
        all_keys.update(dict.fromkeys(self._self_counts))
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
            'flamegraph': self._root,
            'source_files': source_files,
            'threads': [{'tid': t, 'comm': c} for t, c in
                        sorted(self._threads.items(), key=lambda x: x[0])],
        }
        self._dirty = False
        self._blob = None
        return self._snapshot

    def blob(self, mapper=None):
        """(json bytes, raw-deflate segment) of snapshot(), serialized once
        per change. The deflate segment ends with a full flush so
        responses.gzip_join can splice it into a gzip body without
        recompressing a multi-megabyte profile per request."""
        snap = self.snapshot(mapper)
        if self._blob is None:
            raw = dumps(snap)
            self._blob = (raw, deflate_segment(raw))
        return self._blob


class AggregatorSet:
    """All per-event accumulators + chunk routing. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._accs = {}   # event_type -> EventAccumulator

    def add_chunk(self, samples, mapper, count_symbolization=True):
        """Fold one chunk (all events mixed, unexpanded) into the
        accumulators. Inline expansion and addr2line resolution happen here,
        once, for the new samples only.

        `count_symbolization=False` for replay and export, whose frames are
        not the live capture's and must not show up in its tally."""
        if not samples:
            return

        if mapper:
            # Load-base recovery scans the chunk once, here, rather than in
            # each of the three resolution passes below.
            mapper.prime_module_bases(samples)
            # Name what the target's perf could not, before anything keys
            # off frame['func'] -- the accumulators copy the name into dict
            # keys and tree nodes by value, so a later rewrite would leave a
            # stale '[unknown]' bucket and split one function across two
            # names.
            mapper.resolve_unknown_frames(samples, count=count_symbolization,
                                          primed=True)
            expanded = mapper.expand_inline_frames(samples, primed=True)
        else:
            expanded = samples

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
                    line_data = mapper.map_samples_to_lines(orig_group,
                                                            primed=True)
                    acc.add_source_lines(line_data, orig_group, mapper)

    def snapshot_per_event(self, mapper=None):
        with self._lock:
            return {evt: acc.snapshot(mapper)
                    for evt, acc in sorted(self._accs.items())}

    def snapshot_blobs(self, mapper=None):
        """(per_event dicts, {event: (json bytes, deflate segment)}), built
        under the lock so the bytes are of a consistent tree."""
        with self._lock:
            per_event = {}
            blobs = {}
            for evt, acc in sorted(self._accs.items()):
                per_event[evt] = acc.snapshot(mapper)
                blobs[evt] = acc.blob(mapper)
            return per_event, blobs

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
    aggs.add_chunk(all_samples, mapper, count_symbolization=False)
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
