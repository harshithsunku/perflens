#!/usr/bin/env python3
"""Parser for perf script output."""

import re
from collections import defaultdict


# Sample header: "sample_workload  38979 38167.748831:          1 cycles:Pu: "
# Multi-event:   "sample_workload  39200 38635.633417:          1        cycles:u: "
HEADER_RE = re.compile(
    r'^\s*(\S+)\s+(\d+)\s+[\d.]+:\s+(\d+)\s+(\S+):\s*$'
)

# Stack frame: "\t    ffff9de23e3c sinf32x+0x1c (/usr/lib/.../libm.so.6)"
FRAME_RE = re.compile(
    r'^\s+([0-9a-f]+)\s+(.+?)\s+\((.+?)\)\s*$'
)

# Perf stat line: "    14916038591      instructions:u    #    1.89  insn per cycle"
STAT_RE = re.compile(
    r'^\s+([\d,]+)\s+(\S+?)(?::u)?\s+#?\s*(.*?)$'
)

PERF_STAT_MARKER = '### PERF_STAT ###'


def split_perf_data(text):
    """Split combined perf script + perf stat data.

    Returns (perf_script_text, perf_stat_text).
    """
    if PERF_STAT_MARKER in text:
        parts = text.split(PERF_STAT_MARKER, 1)
        return parts[0], parts[1]
    return text, ''


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

    for line in text.split('\n'):
        m = HEADER_RE.match(line)
        if m:
            if current_sample:
                samples.append(current_sample)
            event_type = m.group(4)
            # Normalize event type: "cycles:Pu" -> "cycles", "cycles:u" -> "cycles"
            event_base = event_type.split(':')[0] if ':' in event_type else event_type
            current_sample = {
                'comm': m.group(1),
                'pid': int(m.group(2)),
                'event_count': int(m.group(3)),
                'event_type': event_base,
                'frames': []
            }
            continue

        m = FRAME_RE.match(line)
        if m and current_sample is not None:
            func_raw = m.group(2)
            if '+' in func_raw:
                parts = func_raw.rsplit('+', 1)
                func = parts[0]
                offset = parts[1]
            else:
                func = func_raw
                offset = ''
            current_sample['frames'].append({
                'addr': m.group(1),
                'func': func,
                'offset': offset,
                'module': m.group(3)
            })

    if current_sample:
        samples.append(current_sample)

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
    """Build a sorted function summary for display."""
    func_counts = aggregate_functions(samples)
    total = len(samples)
    if total == 0:
        return {'total_samples': 0, 'functions': []}

    func_list = []
    for (func, module), count in func_counts.items():
        func_list.append({
            'name': func,
            'module': module,
            'samples': count,
            'percent': round(100.0 * count / total, 2)
        })

    func_list.sort(key=lambda x: x['samples'], reverse=True)
    return {'total_samples': total, 'functions': func_list}


def build_flamegraph_data(samples):
    """Build hierarchical data for flame graph from stack traces."""
    root = {'name': 'root', 'value': 0, 'children': []}

    for sample in samples:
        if not sample['frames']:
            continue
        root['value'] += 1
        node = root
        for frame in reversed(sample['frames']):
            func_name = frame['func']
            child = None
            for c in node['children']:
                if c['name'] == func_name:
                    child = c
                    break
            if child is None:
                child = {'name': func_name, 'value': 0, 'children': []}
                node['children'].append(child)
            child['value'] += 1
            node = child

    return root


def parse_perf_stat(text):
    """Parse perf stat output into structured metrics.

    Returns dict: {'metric_name': {'value': int, 'unit': str, 'comment': str}}
    """
    metrics = {}
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('Performance') or line.startswith('#'):
            continue

        # Try to parse: "14916038591      instructions:u   #  1.89  insn per cycle"
        # Also: "3.002210655 seconds time elapsed"
        m = re.match(r'^\s*([\d,]+)\s+(\S+?)(?::u)?\s*(#\s*(.*))?$', line)
        if m:
            value_str = m.group(1).replace(',', '')
            name = m.group(2)
            comment = (m.group(4) or '').strip()
            try:
                value = int(value_str)
                metrics[name] = {
                    'value': value,
                    'comment': comment,
                }
            except ValueError:
                pass
            continue

        # Try: "3.002210655 seconds time elapsed"
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


if __name__ == '__main__':
    import sys
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
            print(f"  {f['percent']:6.1f}%  {f['samples']:5d}  {f['name']:<30s}  ({f['module']})")

    if stat_text:
        print("\n=== Perf Stat ===")
        stats = parse_perf_stat(stat_text)
        for name, data in stats.items():
            print(f"  {name}: {data['value']} ({data['comment']})")
