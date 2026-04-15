#!/usr/bin/env python3
"""Parser for perf script output."""

import re
import sys
from collections import defaultdict


PERF_STAT_MARKER = '### PERF_STAT ###'

# Header: "<comm> <pid>[/<tid>] [<cpu>] <timestamp>: [<flags>] <count> <event>:"
# Handles all known perf script output variations:
#   - Kernel 2.6/3.x: no [cpu] field, no flags
#   - Kernel 4.x+:    [cpu] field present by default
#   - Kernel 5.x+:    optional 4-char flags field (e.g. ".... " or "d.b. ")
#   - pid/tid format:  "1234/5678" (optional /tid is discarded)
#   - Event modifiers: "cycles:u:" or "cycles:pp:" (normalized later)
#   - Agent -F output: comm,pid,time,period,event,ip,sym,dso (same shape)
# comm may contain spaces — non-greedy, backtrack finds pid+timestamp
HEADER_RE = re.compile(
    r'^(.+?)\s+(\d+)(?:/(\d+))?\s+'      # comm + pid (optional /tid)
    r'(?:\[\d+\]\s+)?'                    # optional [cpu]
    r'[\d.]+:\s+'                          # timestamp:
    r'(?:[a-zA-Z.]{4}\s+)?'               # optional flags
    r'(\d+)\s+'                            # count
    r'(\S+):\s*$'                          # event:
)

# Same as HEADER_RE but for flat (no call-graph) output where the single
# frame is appended on the same line after the event:
#   comm pid [cpu] ts: count event:  addr func+off (module)
HEADER_INLINE_RE = re.compile(
    r'^(.+?)\s+(\d+)(?:/(\d+))?\s+'      # comm + pid (optional /tid)
    r'(?:\[\d+\]\s+)?'                    # optional [cpu]
    r'[\d.]+:\s+'                          # timestamp:
    r'(?:[a-zA-Z.]{4}\s+)?'               # optional flags
    r'(\d+)\s+'                            # count
    r'(\S+):\s+'                           # event:  (followed by frame)
    r'([0-9a-f]+)\s+'                      # addr
    r'(.+?)\s+'                            # func+offset
    r'\((.+)\)\s*$'                        # (module)
)

# Stack frame with module (last parenthesized group on the line)
# Handles C++ templates with <>, () in function names
FRAME_MODULE_RE = re.compile(
    r'^\s+([0-9a-f]+)\s+(.+)\s+\((.+)\)\s*$'
)

# Stack frame without module
FRAME_BARE_RE = re.compile(
    r'^\s+([0-9a-f]+)\s+(\S+)\s*$'
)


def split_perf_data(text):
    """Split combined perf script + perf stat data.

    Returns (perf_script_text, perf_stat_text).
    """
    if PERF_STAT_MARKER in text:
        parts = text.split(PERF_STAT_MARKER, 1)
        return parts[0], parts[1]
    return text, ''


def _normalize_event(event_type):
    """Normalize event type: strip everything from the first ':' onwards."""
    idx = event_type.find(':')
    return event_type[:idx] if idx >= 0 else event_type


def _parse_frame(line):
    """Parse a stack frame line. Returns frame dict or None."""
    # Try with module first (most common)
    m = FRAME_MODULE_RE.match(line)
    if m:
        func_raw = m.group(2).strip()
        module = m.group(3)
        if '+' in func_raw:
            parts = func_raw.rsplit('+', 1)
            func = parts[0]
            offset = parts[1]
        else:
            func = func_raw
            offset = ''
        return {
            'addr': m.group(1),
            'func': func,
            'offset': offset,
            'module': module,
        }

    # Try without module
    m = FRAME_BARE_RE.match(line)
    if m:
        func_raw = m.group(2)
        if '+' in func_raw:
            parts = func_raw.rsplit('+', 1)
            func = parts[0]
            offset = parts[1]
        else:
            func = func_raw
            offset = ''
        return {
            'addr': m.group(1),
            'func': func,
            'offset': offset,
            'module': '',
        }

    return None


def parse_perf_script(text):
    """Parse perf script text into structured sample data.

    Returns a list of samples, each:
    {
        'comm': str,
        'pid': int,
        'event_count': int,
        'event_type': str,  # normalized: 'cycles', 'instructions', etc.
        'frames': [{'addr': str, 'func': str, 'offset': str, 'module': str}]
    }
    """
    samples = []
    current_sample = None
    total_lines = 0
    unrecognized = 0

    for line in text.split('\n'):
        if not line.strip():
            continue
        total_lines += 1

        m = HEADER_RE.match(line)
        if m:
            if current_sample:
                samples.append(current_sample)
            tid_str = m.group(3)
            current_sample = {
                'comm': m.group(1).strip(),
                'pid': int(m.group(2)),
                'tid': int(tid_str) if tid_str else int(m.group(2)),
                'event_count': int(m.group(4)),
                'event_type': _normalize_event(m.group(5)),
                'frames': [],
            }
            continue

        # Flat profile: header + frame on same line (no call-graph)
        m = HEADER_INLINE_RE.match(line)
        if m:
            if current_sample:
                samples.append(current_sample)
            func_raw = m.group(7)
            if '+' in func_raw:
                parts = func_raw.rsplit('+', 1)
                func, offset = parts[0], parts[1]
            else:
                func, offset = func_raw, ''
            tid_str = m.group(3)
            current_sample = {
                'comm': m.group(1).strip(),
                'pid': int(m.group(2)),
                'tid': int(tid_str) if tid_str else int(m.group(2)),
                'event_count': int(m.group(4)),
                'event_type': _normalize_event(m.group(5)),
                'frames': [{
                    'addr': m.group(6),
                    'func': func,
                    'offset': offset,
                    'module': m.group(8),
                }],
            }
            continue

        frame = _parse_frame(line)
        if frame is not None and current_sample is not None:
            current_sample['frames'].append(frame)
            continue

        unrecognized += 1

    if current_sample:
        samples.append(current_sample)

    if total_lines > 0 and unrecognized > total_lines * 0.1:
        print(f"[parser] WARNING: {unrecognized}/{total_lines} "
              f"({100 * unrecognized / total_lines:.0f}%) lines unrecognized",
              file=sys.stderr)

    return samples


def get_event_types(samples):
    """Return sorted list of unique event types in the samples."""
    return sorted(set(s['event_type'] for s in samples))


def filter_samples_by_event(samples, event_type):
    """Filter samples to only those matching the given event type."""
    return [s for s in samples if s['event_type'] == event_type]


def aggregate_functions(samples):
    """Aggregate sample counts per function (leaf frame only)."""
    func_counts = defaultdict(int)
    for sample in samples:
        if sample['frames']:
            frame = sample['frames'][0]
            key = (frame['func'], frame['module'])
            func_counts[key] += 1
    return dict(func_counts)


def build_function_summary(samples):
    """Build a sorted function summary for display.

    Each function gets two counts:
      - self_samples / self_percent: function was the leaf (top of stack)
      - total_samples / total_percent: function appeared anywhere in the stack

    For backward compat, 'samples' and 'percent' are aliases for self.
    """
    total = len(samples)
    if total == 0:
        return {'total_samples': 0, 'functions': []}

    # Self counts (leaf frame only)
    self_counts = aggregate_functions(samples)

    # Total/inclusive counts (function anywhere in the stack)
    total_counts = defaultdict(int)
    for sample in samples:
        seen = set()
        for frame in sample['frames']:
            key = (frame['func'], frame['module'])
            if key not in seen:
                seen.add(key)
                total_counts[key] += 1

    # Merge into function list
    all_keys = set(self_counts.keys()) | set(total_counts.keys())
    func_list = []
    for key in all_keys:
        func, module = key
        sc = self_counts.get(key, 0)
        tc = total_counts.get(key, 0)
        func_list.append({
            'name': func,
            'module': module,
            'samples': sc,
            'percent': round(100.0 * sc / total, 2),
            'self_samples': sc,
            'self_percent': round(100.0 * sc / total, 2),
            'total_samples': tc,
            'total_percent': round(100.0 * tc / total, 2),
        })

    func_list.sort(key=lambda x: x['self_samples'], reverse=True)
    return {'total_samples': total, 'functions': func_list}


def build_flamegraph_data(samples):
    """Build hierarchical data for flame graph from stack traces.

    Uses a dict for children lookup to avoid O(n^2) list scan.
    Stores module info for module-based coloring.
    """
    root = {'name': 'root', 'value': 0, 'children': [], '_cmap': {}}

    for sample in samples:
        if not sample['frames']:
            continue
        root['value'] += 1
        node = root
        for frame in reversed(sample['frames']):
            func_name = frame['func']
            child = node['_cmap'].get(func_name)
            if child is None:
                child = {'name': func_name, 'value': 0, 'children': [],
                         '_cmap': {}, '_inlined': False, '_module': frame.get('module', '')}
                node['children'].append(child)
                node['_cmap'][func_name] = child
            child['value'] += 1
            if frame.get('inlined'):
                child['_inlined'] = True
            node = child

    # Strip internal lookup maps; promote _inlined and _module
    stack = [root]
    while stack:
        n = stack.pop()
        del n['_cmap']
        if n.pop('_inlined', False):
            n['inlined'] = True
        mod = n.pop('_module', '')
        if mod:
            n['module'] = mod
        stack.extend(n['children'])

    return root


def parse_perf_stat(text):
    """Parse perf stat output into structured metrics.

    Returns dict: {'metric_name': {'value': int|float, 'comment': str}}
    """
    metrics = {}
    for line in text.strip().split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('Performance') or stripped.startswith('#'):
            continue
        # Skip special values
        if '<not counted>' in stripped or '<not supported>' in stripped:
            continue

        # Float with msec unit (task-clock on some systems):
        #   "2,950.76 msec task-clock  # 0.983 CPUs utilized"
        m = re.match(r'^\s*([\d,.]+)\s+msec\s+(\S+)', line)
        if m:
            try:
                value = float(m.group(1).replace(',', ''))
                name = m.group(2).split(':')[0]
                comment = _extract_stat_comment(line)
                metrics[name] = {'value': value, 'comment': comment}
            except ValueError:
                pass
            continue

        # Integer counter:
        #   "9,310,933,573      cycles:u  # 3.155 GHz  (85.56%)"
        m = re.match(r'^\s*([\d,]+)\s+(\S+)', line)
        if m:
            try:
                value = int(m.group(1).replace(',', ''))
                name = m.group(2).split(':')[0]
                comment = _extract_stat_comment(line)
                metrics[name] = {'value': value, 'comment': comment}
            except ValueError:
                pass
            continue

        # Time elapsed: "3.002210655 seconds time elapsed"
        m = re.match(r'^\s*([\d.]+)\s+seconds\s+time\s+elapsed', line)
        if m:
            metrics['time_elapsed'] = {
                'value': float(m.group(1)),
                'comment': 'seconds',
            }

    # Compute derived metrics
    cycles = metrics.get('cycles', {}).get('value', 0)
    instructions = metrics.get('instructions', {}).get('value', 0)
    if cycles > 0 and instructions > 0:
        metrics['ipc'] = {
            'value': round(instructions / cycles, 2),
            'comment': 'instructions per cycle',
        }

    cache_refs = metrics.get('cache-references', {}).get('value', 0)
    cache_misses = metrics.get('cache-misses', {}).get('value', 0)
    if cache_refs > 0 and cache_misses > 0:
        metrics['cache_miss_rate'] = {
            'value': round(100.0 * cache_misses / cache_refs, 2),
            'comment': '% cache miss rate',
        }

    branches = 0
    for k in metrics:
        if 'branches' in k and 'misses' not in k:
            branches = metrics[k].get('value', 0)
            break
    branch_misses = metrics.get('branch-misses', {}).get('value', 0)
    if branches > 0 and branch_misses > 0:
        metrics['branch_miss_rate'] = {
            'value': round(100.0 * branch_misses / branches, 2),
            'comment': '% branch miss rate',
        }

    return metrics


def _extract_stat_comment(line):
    """Extract comment after # in a perf stat line, stripping trailing (pct%)."""
    m = re.search(r'#\s*(.*?)(?:\s*\(\d+\.\d+%\))?\s*$', line)
    if m:
        return m.group(1).strip()
    return ''


if __name__ == '__main__':
    import json

    text = sys.stdin.read()
    script_text, stat_text = split_perf_data(text)

    samples = parse_perf_script(script_text)
    event_types = get_event_types(samples)
    print(f"Parsed {len(samples)} samples, events: {event_types}")

    for evt in event_types:
        filtered = filter_samples_by_event(samples, evt)
        summary = build_function_summary(filtered)
        print(f"\n=== {evt} ({summary['total_samples']} samples) ===")
        for f in summary['functions'][:10]:
            print(f"  self:{f['self_percent']:5.1f}%  total:{f['total_percent']:5.1f}%  {f['self_samples']:5d}/{f['total_samples']:5d}  {f['name']:<30s}  ({f['module']})")

    if stat_text:
        print("\n=== Perf Stat ===")
        stats = parse_perf_stat(stat_text)
        for name, data in stats.items():
            print(f"  {name}: {data['value']} ({data['comment']})")
