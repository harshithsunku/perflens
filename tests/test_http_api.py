"""HTTP API tests via FastAPI's TestClient.

Covers every endpoint's shape, the security regressions (path traversal,
session-id traversal, browse confinement), gzip negotiation, the replay
cache, and the SSE stream's initial frames.
"""

import gzip as gzip_mod
import json
import os

import pytest
from fastapi.testclient import TestClient

from conftest import materialize_fixture_session, fixture_session_names

FIXTURE = fixture_session_names()[0]


@pytest.fixture()
def core(tmp_path, perflens_home):
    """Initialize perflens.server module state without running main()."""
    from importlib.resources import files as pkg_files
    from perflens import server as core

    sessions_dir = str(tmp_path / 'sessions')
    os.makedirs(sessions_dir)
    core.config = core.ServerConfig(
        source_dir=str(tmp_path),
        sessions_dir=sessions_dir,
        browse_root=str(tmp_path),
        ui_dir=os.fspath(pkg_files('perflens') / 'ui'),
    )
    core.state = core.ProfilingState(max_samples=100000)
    core.metrics_state = core.MetricsState()
    core.wizard_state = None
    core.agent_session = None
    core._sse_sinks.clear()
    yield core


@pytest.fixture()
def client(core):
    from perflens import web
    with TestClient(web.create_app()) as c:
        yield c


@pytest.fixture()
def session_id(core):
    return materialize_fixture_session(FIXTURE, core.config.sessions_dir)


# ---------------------------------------------------------------------------
# Basic shapes
# ---------------------------------------------------------------------------

def test_status(client):
    r = client.get('/api/status')
    assert r.status_code == 200
    body = r.json()
    assert body['status'] == 'ok'
    assert body['agent_connected'] is False
    assert body['total_samples'] == 0


def test_static_ui_served(client):
    r = client.get('/')
    assert r.status_code == 200
    assert '<title>PerfLens</title>' in r.text
    assert client.get('/app.js').status_code == 200
    assert client.get('/style.css').headers['content-type'].startswith('text/css')


def test_per_event_empty_and_404(client):
    r = client.get('/api/per-event')
    assert r.status_code == 200
    assert r.json() == {'per_event': {},
                        'version': {'chunk_count': 0, 'total_samples': 0}}
    r = client.get('/api/per-event', params={'event': 'nope'})
    assert r.status_code == 404
    assert 'error' in r.json()


def test_stop_without_agent(client):
    r = client.get('/api/stop')
    assert r.json() == {'stopped': False, 'reason': 'no agent connected'}


def test_agent_command_without_agent(client):
    r = client.post('/api/agent/command', json={'cmd': 'ping'})
    assert r.status_code == 400
    assert r.json() == {'error': 'no managed agent connected'}


def test_connect_validation(client):
    assert client.post('/api/connect', json={'host': ''}).status_code == 400
    r = client.post('/api/connect', content=b'not json')
    assert r.status_code == 400
    assert r.json() == {'error': 'invalid JSON'}


def test_import_empty_body(client):
    r = client.post('/api/import')
    assert r.status_code == 400
    assert r.json() == {'error': 'empty request body'}


def test_wizard_roundtrip(client):
    r = client.get('/api/wizard/state')
    assert r.json()['step'] == 0
    r = client.post('/api/wizard/state', json={'step': 3, 'pid': 42})
    assert r.json()['step'] == 3
    assert client.get('/api/wizard/state').json()['pid'] == 42


def test_metrics_endpoints(client, core):
    assert client.get('/api/metrics/current').json() == {}
    core.metrics_state.add('system', {'ts': 1.0, 'cpu': {'overall_pct': 5}})
    assert client.get('/api/metrics/current').json()['system']['ts'] == 1.0
    hist = client.get('/api/metrics/history', params={'type': 'system'}).json()
    assert len(hist) == 1
    r = client.get('/api/metrics/history', params={'type': 'system',
                                                   'start': 'zzz'})
    assert r.status_code == 400


def test_index_endpoints(client):
    r = client.get('/api/index/status')
    assert r.status_code == 200
    assert 'indexing' in r.json()
    r = client.get('/api/index/files', params={'offset': 'x'})
    assert r.status_code == 400
    assert r.json() == {'error': 'bad offset/limit'}


def test_config_binary_bad_path(client):
    r = client.post('/api/config/binary', json={'path': '/nonexistent/xyz'})
    assert r.status_code == 400
    assert r.json()['ok'] is False


def test_config_source_bad_path(client):
    r = client.post('/api/config/source', json={'path': '/nonexistent/xyz'})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Security regressions
# ---------------------------------------------------------------------------

def test_static_path_traversal_blocked(client):
    for probe in ('/../etc/passwd', '/%2e%2e/%2e%2e/etc/passwd',
                  '/..%2f..%2fetc%2fpasswd'):
        r = client.get(probe)
        assert r.status_code in (400, 404), probe
        assert 'root:' not in r.text


def test_session_id_traversal_blocked(client, core, tmp_path):
    # A metadata.json outside sessions_dir must not be reachable
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'metadata.json').write_text('{"secret": true}')
    r = client.get('/api/sessions/..%2Foutside')
    assert r.status_code in (200, 404)
    assert 'secret' not in r.text


def test_browse_confined_to_root(client, core, tmp_path):
    r = client.get('/api/browse', params={'path': '/etc'})
    body = r.json()
    # Outside the browse root -> snapped back to the root, not an error
    assert body['path'] == os.path.realpath(str(tmp_path))


def test_browse_lists_entries(client, core, tmp_path):
    (tmp_path / 'somefile.txt').write_text('x')
    r = client.get('/api/browse', params={'path': str(tmp_path)})
    names = [e['name'] for e in r.json()['entries']]
    assert 'somefile.txt' in names


# ---------------------------------------------------------------------------
# Sessions: list / replay / export
# ---------------------------------------------------------------------------

def test_sessions_list(client, session_id):
    ids = [s['session_id'] for s in client.get('/api/sessions').json()]
    assert session_id in ids


def test_session_replay_and_cache(client, core, session_id):
    r = client.get(f'/api/sessions/{session_id}')
    assert r.status_code == 200
    body = r.json()
    per_event = body['per_event']
    assert per_event, 'replay produced no events'
    evt = next(iter(per_event))
    entry = per_event[evt]
    assert entry['function_summary']['total_samples'] > 0
    assert entry['flamegraph']['value'] > 0
    assert 'threads' in entry

    # Replay cache written; second call returns the same payload
    cache = os.path.join(core.config.sessions_dir, session_id,
                         'replay_cache.json.gz')
    assert os.path.isfile(cache)
    assert client.get(f'/api/sessions/{session_id}').json() == body


def test_session_replay_not_found(client):
    assert client.get('/api/sessions/nope').json() == {
        'error': 'session not found'}


def test_export_collapsed(client, session_id):
    r = client.get(f'/api/export/session/{session_id}',
                   params={'format': 'collapsed'})
    assert r.status_code == 200
    assert 'attachment' in r.headers['content-disposition']
    line = r.text.strip().split('\n')[0]
    stack, count = line.rsplit(' ', 1)
    assert int(count) > 0 and stack


def test_export_json(client, session_id):
    r = client.get(f'/api/export/session/{session_id}',
                   params={'format': 'json'})
    body = r.json()
    assert body['metadata']['session_id'] == session_id
    assert body['per_event']


def test_export_unknown_format(client, session_id):
    r = client.get(f'/api/export/session/{session_id}',
                   params={'format': 'xml'})
    assert r.json() == {'error': 'unknown format: xml'}


def test_export_flamegraph_svg(client, session_id):
    r = client.get('/api/export/flamegraph',
                   params={'event': 'cycles', 'session': session_id})
    assert r.headers['content-type'].startswith('image/svg')
    assert r.text.startswith('<svg')


def test_export_flamegraph_no_data(client):
    r = client.get('/api/export/flamegraph', params={'event': 'cycles'})
    assert r.json() == {'error': 'no data available'}


# ---------------------------------------------------------------------------
# Live-state endpoints (seeded through state.add_samples)
# ---------------------------------------------------------------------------

def _seed_live_state(core):
    from conftest import load_fixture_chunks
    chunks = load_fixture_chunks(FIXTURE)
    for chunk in chunks:
        core.state.add_samples(chunk)
    all_samples = [s for c in chunks for s in c]
    return all_samples


def test_thread_summary_and_view(client, core):
    samples = _seed_live_state(core)
    evt = samples[0]['event_type']

    r = client.get('/api/thread-summary', params={'event': evt})
    body = r.json()
    assert body['total_samples'] > 0
    assert body['threads']
    tid = body['threads'][0]['tid']

    r = client.get('/api/thread-view', params={'event': evt, 'tid': tid})
    view = r.json()
    assert view['function_summary']['total_samples'] > 0
    assert view['flamegraph']['value'] > 0

    assert client.get('/api/thread-view',
                      params={'event': evt}).status_code == 400
    assert client.get('/api/thread-view',
                      params={'event': evt, 'tid': 'zz'}).status_code == 400


def test_time_window(client, core):
    """Samples are stamped with arrival time; the window endpoint filters
    the raw deque by it (timeline scrubbing)."""
    import time as time_mod
    samples = _seed_live_state(core)
    evt = samples[0]['event_type']
    now = time_mod.time()

    # A window covering "now" holds everything just seeded
    r = client.get('/api/time-window', params={
        'event': evt, 'start': now - 60, 'end': now + 60})
    assert r.status_code == 200
    body = r.json()
    assert body['window']['samples'] > 0
    assert body['function_summary']['total_samples'] == body['window']['samples']
    assert body['flamegraph']['value'] > 0

    # A window in the past is empty but well-formed
    r = client.get('/api/time-window', params={
        'event': evt, 'start': now - 120, 'end': now - 60})
    body = r.json()
    assert body['window']['samples'] == 0
    assert body['function_summary']['total_samples'] == 0

    # Validation
    assert client.get('/api/time-window',
                      params={'event': evt}).status_code == 400
    assert client.get('/api/time-window',
                      params={'event': evt, 'start': now - 60, 'end': now + 60,
                              'tid': 'zz'}).status_code == 400

    # tid filter composes with the window
    tids = {s.get('tid', s.get('pid', 0)) for s in samples
            if s['event_type'] == evt}
    tid = next(iter(tids))
    r = client.get('/api/time-window', params={
        'event': evt, 'start': now - 60, 'end': now + 60, 'tid': tid})
    assert r.json()['window']['samples'] > 0


def test_source_endpoint_no_mapper(client, core):
    _seed_live_state(core)
    r = client.get('/api/source', params={'file': 'x.c'})
    assert r.json()['error'] == 'source mapper not available'
    r = client.get('/api/source')
    assert r.json()['error'] == 'file parameter required'


def test_per_event_gzip_negotiation(client, core):
    """Payloads >8KB gzip when the client accepts it."""
    big = {'function_summary': {'total_samples': 1,
                                'functions': [{'name': f'f{i}' * 20,
                                               'module': 'm'}
                                              for i in range(2000)]},
           'flamegraph': {'name': 'root', 'value': 1, 'children': []},
           'threads': [], 'source_files': []}
    with core.state.lock:
        core.state._cached_per_event = {'cycles': big}
    r = client.get('/api/per-event', params={'event': 'cycles'},
                   headers={'Accept-Encoding': 'gzip'})
    assert r.headers.get('content-encoding') == 'gzip'
    assert r.json()['event'] == 'cycles'  # httpx transparently decompresses
    # Without Accept-Encoding: no compression
    r = client.get('/api/per-event', params={'event': 'cycles'},
                   headers={'Accept-Encoding': 'identity'})
    assert 'content-encoding' not in r.headers


# ---------------------------------------------------------------------------
# SSE — against a real uvicorn instance (TestClient cannot consume
# never-ending streaming responses incrementally)
# ---------------------------------------------------------------------------

@pytest.fixture()
def live_server(core):
    import socket
    import threading
    import time

    import uvicorn
    from perflens import web

    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()

    server = uvicorn.Server(uvicorn.Config(
        web.create_app(), host='127.0.0.1', port=port, log_level='error'))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, 'uvicorn did not start'
        time.sleep(0.02)
    yield f'http://127.0.0.1:{port}'
    server.should_exit = True
    t.join(timeout=5)


def _read_sse(url, want, on_first_line=None, max_lines=60):
    """Collect SSE frames from url until all `want` event names are seen."""
    import httpx
    got = {}
    with httpx.stream('GET', url + '/api/stream', timeout=10) as r:
        assert r.headers['content-type'].startswith('text/event-stream')
        current = None
        for i, line in enumerate(r.iter_lines()):
            assert i < max_lines, f'frames not seen; got: {sorted(got)}'
            if on_first_line:
                on_first_line()
                on_first_line = None
            if line.startswith('event: '):
                current = line[len('event: '):]
            elif line.startswith('data: ') and current:
                got[current] = json.loads(line[len('data: '):])
                current = None
            if want <= set(got):
                break
    return got


def test_sse_initial_frames(core, live_server):
    """With cached data present, a new SSE client immediately receives
    event_types, data_version, and perf_stat frames."""
    with core.state.lock:
        core.state._cached_per_event = {'cycles': {}}
        core.state._event_types_set.add('cycles')
        core.state.event_types = ['cycles']
        core.state.perf_stat = {'cycles': {'value': 1, 'comment': ''}}

    got = _read_sse(live_server, {'event_types', 'data_version', 'perf_stat'})
    assert got['event_types'] == ['cycles']
    assert got['data_version']['event_types'] == ['cycles']
    assert got['perf_stat']['cycles']['value'] == 1


def test_sse_broadcast_reaches_client(core, live_server):
    """A broadcast_sse() from a worker thread lands on connected clients.
    The broadcast fires after the first received line, which proves the
    client's queue is registered."""
    got = _read_sse(
        live_server, {'status'},
        on_first_line=lambda: core.broadcast_sse(
            'status', {'connected': True, 'agent': 'x:1'}))
    assert got['status'] == {'connected': True, 'agent': 'x:1'}
