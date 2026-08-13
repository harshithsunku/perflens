"""MCP server tests.

The MCP server is a client of the PerfLens HTTP API, so these tests drive
the real FastAPI app in-process over an ASGI transport — no sockets, no
mocks of our own endpoints — and call the tools through a real MCP client
session, so tool registration, schemas and error handling are all exercised
the way an agent would hit them.

Skipped entirely when the optional `mcp` extra is not installed.
"""

import asyncio
import json
import os

import pytest

pytest.importorskip('mcp', reason="MCP extra not installed (pip install 'perflens[mcp]')")

import httpx  # noqa: E402
from mcp.client import Client  # noqa: E402

from conftest import (fixture_session_names, load_fixture_chunks,  # noqa: E402
                      materialize_fixture_session)

FIXTURES = fixture_session_names()
FIXTURE = FIXTURES[0]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def core(tmp_path, perflens_home):
    """An AppContext with a stub UI dir (no workers, no source mapper)."""
    from perflens.app import AppContext
    from perflens.config import ServerConfig
    from perflens.state import MetricsState, ProfilingState

    sessions_dir = str(tmp_path / 'sessions')
    os.makedirs(sessions_dir)
    ui_dir = tmp_path / 'ui'
    ui_dir.mkdir()
    (ui_dir / 'index.html').write_text('<!DOCTYPE html><title>stub</title>')
    cfg = ServerConfig(source_dir=str(tmp_path), sessions_dir=sessions_dir,
                       browse_root=str(tmp_path), ui_dir=str(ui_dir))
    return AppContext(config=cfg, state=ProfilingState(max_samples=100000),
                      metrics=MetricsState())


@pytest.fixture()
def session_id(core):
    return materialize_fixture_session(FIXTURE, core.config.sessions_dir)


def feed_live(core, name=FIXTURE, perf_stat=None):
    """Push a fixture's samples into live state.

    The rebuild worker normally folds chunks into the per-event cache off
    the recv thread; tests fold synchronously so the result is
    deterministic.
    """
    chunks = load_fixture_chunks(name)
    for chunk in chunks:
        core.state.add_samples(chunk, perf_stat if chunk is chunks[0] else None)
    with core.state.lock:
        core.state._pending_chunks = []
        core.state._dirty = False
        for chunk in chunks:
            core.state.aggregators.add_chunk(chunk, None)
        core.state._cached_per_event = core.state.aggregators.snapshot_per_event(None)
    return sum(len(c) for c in chunks)


@pytest.fixture()
def live_url(core):
    """A real uvicorn instance.

    httpx's ASGI transport buffers a whole response before returning it, so
    it cannot consume the never-ending SSE stream at all. The two tools that
    read the stream head (live event types, live counters) therefore need a
    real socket, exactly as the HTTP suite's SSE tests do.
    """
    import socket
    import threading
    import time

    import uvicorn
    from perflens import web

    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()

    server = uvicorn.Server(uvicorn.Config(
        web.create_app(core), host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, 'uvicorn did not start'
        time.sleep(0.02)
    yield f'http://127.0.0.1:{port}'
    server.should_exit = True
    thread.join(timeout=10)


class Harness:
    """An MCP server wired to a PerfLens app.

    In-process over an ASGI transport by default (fast, no ports); pass
    `url` to talk to a real server instead, which the SSE-reading tools
    need.
    """

    def __init__(self, core, read_only=False, url=None):
        self.core = core
        self.read_only = read_only
        self.url = url

    async def __aenter__(self):
        from perflens import web
        from perflens.mcp import build_server
        from perflens.mcp.client import PerfLensClient

        self._lifespan = None
        if self.url:
            self.api = PerfLensClient(self.url)
        else:
            app = web.create_app(self.core)
            # Run the app's lifespan so the SSE hub is attached to this loop.
            self._lifespan = app.router.lifespan_context(app)
            await self._lifespan.__aenter__()
            self.api = PerfLensClient('http://perflens.test',
                                      transport=httpx.ASGITransport(app=app))
        self.mcp, _ = build_server(read_only=self.read_only, client=self.api)
        self.client = Client(self.mcp)
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)
        await self.api.aclose()
        if self._lifespan is not None:
            await self._lifespan.__aexit__(*exc)

    async def call(self, tool, **kwargs):
        """Call a tool, asserting it succeeded, and return the text."""
        result = await self.client.call_tool(tool, kwargs)
        text = result.content[0].text if result.content else ''
        assert not result.is_error, f'{tool} failed: {text}'
        return text

    async def call_json(self, tool, **kwargs):
        kwargs.setdefault('response_format', 'json')
        return json.loads(await self.call(tool, **kwargs))

    async def call_expect_error(self, tool, **kwargs):
        result = await self.client.call_tool(tool, kwargs)
        assert result.is_error, f'{tool} unexpectedly succeeded'
        return result.content[0].text

    async def tool_names(self):
        listed = await self.client.list_tools()
        return {t.name for t in listed.tools}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_tools_are_registered_with_annotations(core):
    async def go():
        async with Harness(core) as h:
            listed = await h.client.list_tools()
            names = {t.name for t in listed.tools}
            assert 'perflens_status' in names
            assert 'perflens_hot_functions' in names
            assert 'perflens_start_profiling' in names
            # Every tool is namespaced, described, and schema'd.
            for tool in listed.tools:
                assert tool.name.startswith('perflens_')
                assert tool.description and len(tool.description) > 40
                assert (tool.input_schema or {}).get('properties') is not None
            by_name = {t.name: t for t in listed.tools}
            assert by_name['perflens_hot_functions'].annotations.read_only_hint
            assert not by_name['perflens_start_profiling'].annotations.read_only_hint
            assert not by_name['perflens_export'].annotations.read_only_hint
    run(go())


def test_read_only_omits_control_and_export(core):
    async def go():
        async with Harness(core, read_only=True) as h:
            names = await h.tool_names()
            assert 'perflens_hot_functions' in names
            for absent in ('perflens_start_profiling', 'perflens_stop_profiling',
                           'perflens_agent_connect', 'perflens_export',
                           'perflens_collection_pause', 'perflens_list_processes'):
                assert absent not in names
    run(go())


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------

def test_status_reports_empty_server(core):
    async def go():
        async with Harness(core) as h:
            data = await h.call_json('perflens_status')
            assert data['agent_connected'] is False
            assert data['live_samples'] == 0
            assert data['saved_sessions'] == 0
            text = await h.call('perflens_status')
            assert 'PerfLens status' in text
    run(go())


def test_status_counts_sessions_and_live_samples(core, session_id):
    total = feed_live(core)

    async def go():
        async with Harness(core) as h:
            data = await h.call_json('perflens_status')
            assert data['saved_sessions'] == 1
            assert data['live_samples'] == total
    run(go())


def test_status_reads_live_events_from_the_stream(core, live_url):
    """Live event types come from the SSE head, not a full snapshot."""
    feed_live(core)

    async def go():
        async with Harness(core, url=live_url) as h:
            data = await h.call_json('perflens_status')
            assert 'cycles' in data['live_events']
    run(go())


def test_live_perf_stat_comes_from_the_stream(core, live_url):
    feed_live(core, perf_stat={'cycles': {'value': 4000.0, 'comment': ''},
                               'instructions': {'value': 8000.0, 'comment': ''}})

    async def go():
        async with Harness(core, url=live_url) as h:
            data = await h.call_json('perflens_perf_stat')
            assert data['counters'], 'expected counters from the SSE head'
            assert data['derived']['ipc'] == 2.0
    run(go())


def test_list_sessions_lists_the_fixture(core, session_id):
    async def go():
        async with Harness(core) as h:
            data = await h.call_json('perflens_list_sessions')
            ids = [s['session_id'] for s in data['sessions']]
            assert session_id in ids
            assert data['page']['total'] == 1
    run(go())


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def test_hot_functions_ranks_and_reports_coverage(core, session_id):
    async def go():
        async with Harness(core) as h:
            data = await h.call_json('perflens_hot_functions',
                                     source=session_id, limit=5)
            funcs = data['functions']
            assert 1 <= len(funcs) <= 5
            selfs = [f['self_samples'] for f in funcs]
            assert selfs == sorted(selfs, reverse=True)
            assert data['event'] == 'cycles'
            assert 0 < data['coverage_percent'] <= 100
            assert data['total_samples'] > 0
    run(go())


def test_hot_functions_sort_total_differs_from_self(core, session_id):
    async def go():
        async with Harness(core) as h:
            by_self = await h.call_json('perflens_hot_functions',
                                        source=session_id, sort='self', limit=10)
            by_total = await h.call_json('perflens_hot_functions',
                                         source=session_id, sort='total', limit=10)
            totals = [f['total_samples'] for f in by_total['functions']]
            assert totals == sorted(totals, reverse=True)
            # The root of the call tree dominates inclusive time but not self.
            assert by_self['functions'][0] != by_total['functions'][0]
    run(go())


def test_hot_functions_paginates_with_a_next_call(core, session_id):
    async def go():
        async with Harness(core) as h:
            first = await h.call_json('perflens_hot_functions',
                                      source=session_id, limit=3)
            assert first['page']['has_more'] is True
            assert first['page']['next_offset'] == 3
            text = await h.call('perflens_hot_functions',
                                source=session_id, limit=3)
            assert 'offset=3' in text
            second = await h.call_json('perflens_hot_functions',
                                       source=session_id, limit=3, offset=3)
            assert second['functions'][0] not in first['functions']
    run(go())


def test_hot_stacks_fold_the_flamegraph(core, session_id):
    async def go():
        async with Harness(core) as h:
            data = await h.call_json('perflens_hot_stacks', source=session_id,
                                     limit=5, min_percent=0.0)
            stacks = data['stacks']
            assert stacks, 'expected at least one hot stack'
            assert all(isinstance(s['path'], list) and s['path'] for s in stacks)
            shares = [s['self_percent'] for s in stacks]
            assert shares == sorted(shares, reverse=True)
            # Folded self weights can never exceed the profile itself.
            assert sum(s['self_samples'] for s in stacks) <= data['total_samples']
            text = await h.call('perflens_hot_stacks', source=session_id)
            assert '→' in text
    run(go())


def test_min_percent_filters_stacks(core, session_id):
    async def go():
        async with Harness(core) as h:
            everything = await h.call_json('perflens_hot_stacks',
                                           source=session_id, limit=100,
                                           min_percent=0.0)
            filtered = await h.call_json('perflens_hot_stacks',
                                         source=session_id, limit=100,
                                         min_percent=5.0)
            assert len(filtered['stacks']) <= len(everything['stacks'])
            assert all(s['self_percent'] >= 5.0 for s in filtered['stacks'])
    run(go())


def test_list_source_files_for_a_session(core, session_id):
    async def go():
        async with Harness(core) as h:
            data = await h.call_json('perflens_list_source_files',
                                     source=session_id)
            # The device fixtures carry no local sources; the tool must
            # still answer cleanly rather than blowing up.
            assert 'files' in data and 'page' in data
    run(go())


def test_perf_stat_derives_ratios(core, session_id):
    async def go():
        async with Harness(core) as h:
            data = await h.call_json('perflens_perf_stat', source=session_id)
            assert 'counters' in data and 'derived' in data
    run(go())


def test_perf_stat_derivation_math():
    from perflens.mcp.format import derived_counters
    derived = derived_counters({'cycles': 1000, 'instructions': 2500,
                                'cache-references': 200, 'cache-misses': 50,
                                'branches': 400, 'branch-misses': 8})
    assert derived['ipc'] == 2.5
    assert derived['cache_miss_rate_percent'] == 25.0
    assert derived['branch_miss_rate_percent'] == 2.0


def test_compare_two_fixture_sessions(core):
    async def go():
        a = materialize_fixture_session(FIXTURES[0], core.config.sessions_dir,
                                        'baseline')
        b = materialize_fixture_session(FIXTURES[-1], core.config.sessions_dir,
                                        'target')
        async with Harness(core) as h:
            data = await h.call_json('perflens_compare', baseline=a, target=b,
                                     limit=10)
            assert data['baseline'] == 'baseline' and data['target'] == 'target'
            assert data['baseline_samples'] > 0 and data['target_samples'] > 0
            rows = data['functions']
            assert rows, 'expected functions in the diff'
            deltas = [abs(r['delta_percent']) for r in rows]
            assert deltas == sorted(deltas, reverse=True)
            assert {r['status'] for r in rows} <= {'appeared', 'vanished', 'changed'}
    run(go())


# ---------------------------------------------------------------------------
# Threads and metrics
# ---------------------------------------------------------------------------

def test_threads_live_breakdown(core):
    async def go():
        feed_live(core)
        async with Harness(core) as h:
            data = await h.call_json('perflens_threads')
            assert data['threads'], 'fixture has at least one thread'
            first = data['threads'][0]
            assert {'tid', 'comm', 'samples', 'percent'} <= set(first)
            detail = await h.call_json('perflens_thread_detail',
                                       tid=first['tid'], limit=5)
            assert detail['functions']
            assert detail['total_samples'] > 0
    run(go())


def test_threads_for_a_session_explains_the_live_only_limit(core, session_id):
    async def go():
        async with Harness(core) as h:
            text = await h.call('perflens_threads', source=session_id)
            assert 'live' in text.lower()
    run(go())


def test_per_thread_filter_rejects_sessions_with_a_hint(core, session_id):
    async def go():
        async with Harness(core) as h:
            message = await h.call_expect_error('perflens_hot_functions',
                                                source=session_id, tid=1234)
            assert 'live data only' in message
            assert 'perflens_threads' in message
    run(go())


def test_metrics_report_absence_cleanly(core):
    async def go():
        async with Harness(core) as h:
            text = await h.call('perflens_device_metrics')
            assert 'No metrics' in text or 'Device health' in text
    run(go())


def test_metrics_history_summarises(core):
    async def go():
        now = 1000.0
        for i in range(10):
            core.metrics.add('system', {'type': 'system', 'timestamp': now + i,
                                        'cpu_percent': 10.0 + i})
        async with Harness(core) as h:
            data = await h.call_json('perflens_metrics_history', kind='system')
            assert data['frames'] == 10
            summary = data['summary']['cpu_percent']
            assert summary['min'] == 10.0 and summary['max'] == 19.0
    run(go())


# ---------------------------------------------------------------------------
# Control and export
# ---------------------------------------------------------------------------

def test_control_tools_explain_a_missing_agent(core):
    async def go():
        async with Harness(core) as h:
            text = await h.call('perflens_agent_info')
            assert 'Not connected' in text
            message = await h.call_expect_error('perflens_list_processes')
            assert 'perflens_agent_connect' in message
    run(go())


def test_export_writes_a_file(core, session_id, tmp_path):
    async def go():
        out = tmp_path / 'profile'
        async with Harness(core) as h:
            data = await h.call_json('perflens_export', out_path=str(out),
                                     source=session_id, format='collapsed')
            written = data['path']
            assert written.endswith('.collapsed')
            assert os.path.getsize(written) == data['bytes'] > 0
            with open(written) as handle:
                assert ' ' in handle.readline()
    run(go())


def test_export_rejects_unknown_format_and_missing_dir(core, session_id, tmp_path):
    async def go():
        async with Harness(core) as h:
            message = await h.call_expect_error(
                'perflens_export', out_path=str(tmp_path / 'x'),
                source=session_id, format='pdf')
            assert 'collapsed' in message
            message = await h.call_expect_error(
                'perflens_export', out_path=str(tmp_path / 'nope' / 'x'),
                source=session_id)
            assert 'does not exist' in message
    run(go())


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

def test_unknown_session_names_the_fix(core, session_id):
    async def go():
        async with Harness(core) as h:
            message = await h.call_expect_error('perflens_hot_functions',
                                                source='no-such-session')
            assert 'perflens_list_sessions' in message
    run(go())


def test_unknown_event_lists_the_real_ones(core, session_id):
    async def go():
        async with Harness(core) as h:
            message = await h.call_expect_error('perflens_hot_functions',
                                                source=session_id,
                                                event='not-an-event')
            assert 'cycles' in message
    run(go())


def test_no_live_data_suggests_starting_collection(core):
    async def go():
        async with Harness(core) as h:
            message = await h.call_expect_error('perflens_hot_functions')
            assert 'perflens_start_profiling' in message
    run(go())


def test_unreachable_server_names_the_command(core):
    async def go():
        from perflens.mcp.client import PerfLensClient, PerfLensError
        api = PerfLensClient('http://127.0.0.1:1')  # nothing listens here
        try:
            with pytest.raises(PerfLensError) as excinfo:
                await api.status()
            assert 'perflens serve' in str(excinfo.value)
        finally:
            await api.aclose()
    run(go())
