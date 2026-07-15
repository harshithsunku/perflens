#!/usr/bin/env python3
"""Differential test: incremental EventAccumulator vs the original batch
builders (build_function_summary / build_flamegraph_data), fed the same
real device-captured session fixtures chunk by chunk.

The incremental path must produce identical aggregates to a full batch
rebuild — this pins the Phase 2a "no behavior change" requirement.

Run: python3 test/test_aggregator_diff.py
"""

import gzip
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))

from parser import (parse_perf_script, split_perf_data,
                    build_function_summary, build_flamegraph_data,
                    filter_samples_by_event, get_event_types)
from aggregator import AggregatorSet

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def load_fixture_chunks(name):
    d = os.path.join(FIXTURES, name)
    chunks = []
    for fname in sorted(os.listdir(d)):
        if fname.startswith('chunk_') and fname.endswith('.txt.gz'):
            with gzip.open(os.path.join(d, fname), 'rt') as f:
                text = f.read()
            script_text, _ = split_perf_data(text)
            chunks.append(parse_perf_script(script_text))
    return chunks


def canon_functions(funcs):
    """Order-independent canonical form of a function summary list."""
    return sorted(
        (tuple(sorted(f.items())) for f in funcs),
    )


def canon_tree(node):
    """Canonical form of a flamegraph tree (children sorted by name)."""
    return (
        node['name'],
        node['value'],
        node.get('inlined', False),
        node.get('module', ''),
        tuple(sorted(canon_tree(c) for c in node['children'])),
    )


def check_fixture(name):
    chunks = load_fixture_chunks(name)
    assert chunks, 'no chunks found for %s' % name
    all_samples = [s for chunk in chunks for s in chunk]

    # Incremental: feed chunk by chunk (mapper=None: no inline expansion —
    # equivalence of the aggregation itself is what's under test)
    aggs = AggregatorSet()
    for chunk in chunks:
        aggs.add_chunk(chunk, None)
    per_event = aggs.snapshot_per_event(None)

    events_batch = get_event_types(all_samples)
    assert sorted(per_event.keys()) == sorted(events_batch), (
        'event sets differ: %s vs %s' % (sorted(per_event), events_batch))

    failures = 0
    for evt in events_batch:
        evt_samples = filter_samples_by_event(all_samples, evt)
        batch_summary = build_function_summary(evt_samples)
        batch_tree = build_flamegraph_data(evt_samples)
        inc = per_event[evt]

        ok = True
        if inc['function_summary']['total_samples'] != batch_summary['total_samples']:
            print('FAIL %s/%s: total_samples %d != %d' % (
                name, evt, inc['function_summary']['total_samples'],
                batch_summary['total_samples']))
            ok = False
        if canon_functions(inc['function_summary']['functions']) != \
                canon_functions(batch_summary['functions']):
            print('FAIL %s/%s: function summaries differ' % (name, evt))
            ok = False
        if canon_tree(inc['flamegraph']) != canon_tree(batch_tree):
            print('FAIL %s/%s: flamegraph trees differ' % (name, evt))
            ok = False

        # Sorting contract: functions ordered by self_samples desc
        selfs = [f['self_samples'] for f in inc['function_summary']['functions']]
        if selfs != sorted(selfs, reverse=True):
            print('FAIL %s/%s: function list not sorted' % (name, evt))
            ok = False

        # Threads present and sorted by tid
        tids = [t['tid'] for t in inc['threads']]
        if tids != sorted(tids):
            print('FAIL %s/%s: threads not sorted' % (name, evt))
            ok = False

        if ok:
            print('PASS %s/%-22s samples=%-6d functions=%-4d tree_root=%d' % (
                name, evt, batch_summary['total_samples'],
                len(batch_summary['functions']), batch_tree['value']))
        else:
            failures += 1
    return failures


def main():
    total_failures = 0
    for name in sorted(os.listdir(FIXTURES)):
        if os.path.isdir(os.path.join(FIXTURES, name)):
            total_failures += check_fixture(name)
    print()
    if total_failures:
        print('%d FAILURES' % total_failures)
        sys.exit(1)
    print('ALL DIFFERENTIAL TESTS PASSED')


if __name__ == '__main__':
    main()
