#!/usr/bin/env python3
"""Parser for perf script output."""

import re
from collections import defaultdict


# Sample header: "sample_workload  38979 38167.748831:          1 cycles:Pu: "
HEADER_RE = re.compile(
    r'^\s*(\S+)\s+(\d+)\s+[\d.]+:\s+(\d+)\s+(\S+):\s*$'
)

# Stack frame: "\t    ffff9de23e3c sinf32x+0x1c (/usr/lib/.../libm.so.6)"
# Or:          "\t    aaaadae50c50 cpu_intensive+0x28 (/tmp/sample_workload)"
# Or:          "\t    ffff9dc4251c [unknown] (/usr/lib/.../libc.so.6)"
FRAME_RE = re.compile(
    r'^\s+([0-9a-f]+)\s+(.+?)\s+\((.+?)\)\s*$'
)


def parse_perf_script(text):
    """Parse perf script text into structured sample data.

    Returns a list of samples, each:
    {
        'comm': str,       # process name
        'pid': int,
        'event_count': int,
        'event_type': str, # e.g. 'cycles:Pu'
        'frames': [        # stack frames, leaf first
            {'addr': str, 'func': str, 'offset': str, 'module': str},
            ...
        ]
    }
    """
    samples = []
    current_sample = None

    for line in text.split('\n'):
        # Try to match sample header
        m = HEADER_RE.match(line)
        if m:
            if current_sample:
                samples.append(current_sample)
            current_sample = {
                'comm': m.group(1),
                'pid': int(m.group(2)),
                'event_count': int(m.group(3)),
                'event_type': m.group(4),
                'frames': []
            }
            continue

        # Try to match stack frame
        m = FRAME_RE.match(line)
        if m and current_sample is not None:
            func_raw = m.group(2)
            # Split "cpu_intensive+0x28" into func and offset
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

    # Don't forget the last sample
    if current_sample:
        samples.append(current_sample)

    return samples


def aggregate_functions(samples):
    """Aggregate sample counts per function.

    Returns dict: {(func, module): sample_count}
    """
    func_counts = defaultdict(int)
    for sample in samples:
        # Count the leaf function (top of stack = index 0)
        if sample['frames']:
            frame = sample['frames'][0]
            key = (frame['func'], frame['module'])
            func_counts[key] += 1
    return dict(func_counts)


def build_function_summary(samples):
    """Build a sorted function summary for display.

    Returns:
    {
        'total_samples': int,
        'functions': [
            {'name': str, 'module': str, 'samples': int, 'percent': float},
            ...
        ]
    }
    """
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
    """Build hierarchical data for flame graph from stack traces.

    Returns a tree: {'name': 'root', 'value': N, 'children': [...]}
    """
    root = {'name': 'root', 'value': 0, 'children': []}

    for sample in samples:
        if not sample['frames']:
            continue
        root['value'] += 1
        # Walk the stack from bottom (callers) to top (leaf)
        node = root
        for frame in reversed(sample['frames']):
            func_name = frame['func']
            # Find or create child
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


if __name__ == '__main__':
    # Quick test: read from stdin
    import sys
    import json

    text = sys.stdin.read()
    samples = parse_perf_script(text)
    print(f"Parsed {len(samples)} samples")

    summary = build_function_summary(samples)
    print(f"\nTotal samples: {summary['total_samples']}")
    print(f"\nTop functions:")
    for f in summary['functions'][:15]:
        print(f"  {f['percent']:6.1f}%  {f['samples']:5d}  {f['name']:<30s}  ({f['module']})")

    fg = build_flamegraph_data(samples)
    print(f"\nFlamegraph root value: {fg['value']}, children: {len(fg['children'])}")
