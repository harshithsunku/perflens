"""Aggregator tests.

The differential test pins the core Phase-2 invariant: the incremental
EventAccumulator path, fed chunk by chunk, must produce aggregates
identical to a full batch rebuild — on real device-captured sessions.
"""

import os
import subprocess
import sys

import pytest

from perflens.parser import (MAX_FLAMEGRAPH_DEPTH, build_flamegraph_data,
                             build_function_summary,
                             filter_samples_by_event, get_event_types)
from perflens.aggregator import AggregatorSet

from conftest import REPO, fixture_session_names, load_fixture_chunks


def canon_functions(funcs):
    """Order-independent canonical form of a function summary list."""
    return sorted(tuple(sorted(f.items())) for f in funcs)


def canon_tree(node):
    """Canonical form of a flamegraph tree (children sorted by name)."""
    return (
        node['name'],
        node['value'],
        node.get('inlined', False),
        node.get('module', ''),
        tuple(sorted(canon_tree(c) for c in node['children'])),
    )


@pytest.mark.parametrize('name', fixture_session_names())
def test_incremental_matches_batch(name):
    chunks = load_fixture_chunks(name)
    assert chunks, f'no chunks found for {name}'
    all_samples = [s for chunk in chunks for s in chunk]

    # Incremental: feed chunk by chunk (mapper=None: no inline expansion —
    # equivalence of the aggregation itself is what's under test)
    aggs = AggregatorSet()
    for chunk in chunks:
        aggs.add_chunk(chunk, None)
    per_event = aggs.snapshot_per_event(None)

    events = get_event_types(all_samples)
    assert sorted(per_event.keys()) == events

    for evt in events:
        evt_samples = filter_samples_by_event(all_samples, evt)
        batch_summary = build_function_summary(evt_samples)
        batch_tree = build_flamegraph_data(evt_samples)
        inc = per_event[evt]

        assert (inc['function_summary']['total_samples']
                == batch_summary['total_samples']), f'{name}/{evt}'
        assert (canon_functions(inc['function_summary']['functions'])
                == canon_functions(batch_summary['functions'])), f'{name}/{evt}'
        assert canon_tree(inc['flamegraph']) == canon_tree(batch_tree), \
            f'{name}/{evt}'

        # Sorting contract: functions ordered by self_samples desc
        selfs = [f['self_samples']
                 for f in inc['function_summary']['functions']]
        assert selfs == sorted(selfs, reverse=True), f'{name}/{evt}'

        # Threads present and sorted by tid
        tids = [t['tid'] for t in inc['threads']]
        assert tids == sorted(tids), f'{name}/{evt}'


_DET_SNIPPET = r'''
import hashlib, json, os, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from conftest import load_fixture_chunks, fixture_session_names
from perflens.aggregator import build_per_event_batch
name = fixture_session_names()[0]
samples = [s for c in load_fixture_chunks(name) for s in c]
pe = build_per_event_batch(samples, None)
names = {e: [f['name'] for f in pe[e]['function_summary']['functions']]
         for e in sorted(pe)}
print(hashlib.sha1(json.dumps(names, sort_keys=True).encode()).hexdigest())
'''


def test_function_order_stable_across_hash_seeds():
    """Regression for the set-union bug: equal-count functions used to be
    ordered by randomized string hash, differing between processes."""
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    digests = set()
    for seed in ('0', '1', '42'):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run(
            [sys.executable, '-c', _DET_SNIPPET,
             os.path.join(REPO, 'src'), tests_dir],
            capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0, r.stderr
        digests.add(r.stdout.strip())
    assert len(digests) == 1, 'function ordering depends on the hash seed'


def test_reset_clears_state():
    chunks = load_fixture_chunks(fixture_session_names()[0])
    aggs = AggregatorSet()
    aggs.add_chunk(chunks[0], None)
    assert aggs.snapshot_per_event(None)
    aggs.reset()
    assert aggs.snapshot_per_event(None) == {}


def _deep_sample(depth, event='cycles'):
    """One sample whose stack is `depth` frames deep."""
    return {
        'event_type': event, 'event_count': 1, 'pid': 1, 'tid': 1,
        'comm': 'deep',
        'frames': [{'func': f'recurse_{i}', 'module': 'deep',
                    'addr': hex(i), 'offset': ''}
                   for i in range(depth)],
    }


@pytest.mark.parametrize('builder', ['aggregator', 'parser'])
def test_deep_stacks_stay_serializable(builder):
    """Regression: a deeply recursive workload made /api/snapshot 500.

    orjson refuses to encode past a fixed nesting depth (254 containers).
    A flamegraph level costs two of them — the node dict and its children
    list — so a stack deeper than ~126 frames could not be serialized at
    all, and the whole UI went blank instead of one deep stack rendering
    short. Found by profiling a 25-thread local workload.
    """
    orjson = pytest.importorskip('orjson')
    samples = [_deep_sample(400)]

    if builder == 'aggregator':
        aggs = AggregatorSet()
        aggs.add_chunk(samples, None)
        tree = aggs.snapshot_per_event(None)['cycles']['flamegraph']
    else:
        tree = build_flamegraph_data(samples)

    # The actual failure: this raised TypeError('Recursion limit reached').
    orjson.dumps(tree, option=orjson.OPT_NON_STR_KEYS)

    depth, node = 0, tree
    while node.get('children'):
        node = node['children'][0]
        depth += 1
    assert depth <= MAX_FLAMEGRAPH_DEPTH
    assert node.get('truncated') is True, 'the cut point must be marked'


def test_shallow_stacks_are_not_marked_truncated():
    """The cap must not touch stacks of ordinary depth."""
    aggs = AggregatorSet()
    aggs.add_chunk([_deep_sample(12)], None)
    tree = aggs.snapshot_per_event(None)['cycles']['flamegraph']
    stack = [tree]
    while stack:
        n = stack.pop()
        assert 'truncated' not in n
        stack.extend(n.get('children') or [])
