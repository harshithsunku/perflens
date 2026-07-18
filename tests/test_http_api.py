"""HTTP API (v2) tests via FastAPI's TestClient.

Covers every endpoint's shape, the {"error": {code, message}} envelope
with real status codes, the security regressions (path traversal,
session-id traversal, DELETE traversal, browse confinement), gzip
negotiation, the replay cache, and the SSE stream against live uvicorn.
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
    """Build a fresh AppContext (no workers, no source mapper)."""
    from importlib.resources import files as pkg_files
    from perflens.app import AppContext
    from perflens.config import ServerConfig
    from perflens.state import MetricsState, ProfilingState

    sessions_dir = str(tmp_path / 'sessions')
    os.makedirs(sessions_dir)
    cfg = ServerConfig(
        source_dir=str(tmp_path),
        sessions_dir=sessions_dir,
        browse_root=str(tmp_path),
        ui_dir=os.fspath(pkg_files('perflens') / 'ui'),
    )
    yield AppContext(config=cfg,
                     state=ProfilingState(max_samples=100000),
                     metrics=MetricsState())


@pytest.fixture()
def client(core):
    from perflens import web
    with TestClient(web.create_app(core)) as c:
        yield c


@pytest.fixture()
def session_id(core):
    return materialize_fixture_session(FIXTURE, core.config.sessions_dir)


def assert_error(response, status, code):
    """Every failure is {"error": {"code": ..., "message": ...}}."""
    assert response.status_code == status
    body = response.json()
    assert body['error']['code'] == code
    assert body['error']['message']
    return body['error']


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


def _ui_built():
    from importlib.resources import files as pkg_files
    return (pkg_files('perflens') / 'ui' / 'index.html').is_file()


@pytest.mark.skipif(not _ui_built(),
                    reason='frontend not built (npm --prefix frontend run build)')
def test_static_ui_served(client):
    r = client.get('/')
    assert r.status_code == 200
    assert '<title>PerfLens</title>' in r.text
    # Vite emits hashed bundles under /assets/ — find them via the index
    import re
    js = re.search(r'src="(/assets/[^"]+\.js)"', r.text)
    css = re.search(r'href="(/assets/[^"]+\.css)"', r.text)
    assert js, 'index.html references no JS bundle'
    assert css, 'index.html references no CSS bundle'
    assert client.get(js.group(1)).status_code == 200
    assert client.get(css.group(1)).headers['content-type'].startswith('text/css')


def test_ui_missing_fallback(client, core, tmp_path):
    """A source checkout without built frontend assets serves a friendly
    503 page instead of 404ing, and the API stays functional."""
    from perflens import web
    core.config.ui_dir = str(tmp_path / 'no-ui')
    with TestClient(web.create_app(core)) as c:
        r = c.get('/')
        assert r.status_code == 503
        assert 'UI not built' in r.text
        assert c.get('/api/status').status_code == 200


def test_snapshot_empty_and_404(client):
    r = client.get('/api/snapshot')
    assert r.status_code == 200
    assert r.json() == {'per_event': {},
                        'version': {'chunk_count': 0, 'total_samples': 0}}
    assert_error(client.get('/api/snapshot', params={'event': 'nope'}),
                 404, 'not_found')


def test_agent_info_and_disconnect_without_agent(client):
    assert client.get('/api/agent').json() == {
        'connected': False, 'addr': None, 'hello': None}
    r = client.delete('/api/agent')
    assert r.json() == {'stopped': False, 'reason': 'no agent connected'}


def test_agent_command_without_agent(client):
    r = client.post('/api/agent/command', json={'cmd': 'ping'})
    assert_error(r, 409, 'no_agent')


def test_agent_command_unknown_cmd(client):
    """cmd is enforced against the frozen agent's command set."""
    r = client.post('/api/agent/command', json={'cmd': 'format_disk'})
    assert_error(r, 400, 'validation')


def test_connect_validation(client):
    assert_error(client.post('/api/agent/connect', json={'host': ''}),
                 400, 'validation')
    # Malformed JSON is rejected by request-model validation (rendered
    # through the same error envelope)
    r = client.post('/api/agent/connect', content=b'not json',
                    headers={'content-type': 'application/json'})
    assert_error(r, 400, 'validation')


def test_import_empty_body(client):
    assert_error(client.post('/api/sessions/import'), 400, 'empty_body')


def test_wizard_roundtrip(client):
    r = client.get('/api/wizard')
    assert r.json()['step'] == 0
    r = client.put('/api/wizard', json={'step': 3, 'pid': 42})
    assert r.json()['step'] == 3
    assert client.get('/api/wizard').json()['pid'] == 42


def test_metrics_endpoints(client, core):
    assert client.get('/api/metrics/current').json() == {}
    core.metrics.add('system', {'ts': 1.0, 'cpu': {'overall_pct': 5}})
    assert client.get('/api/metrics/current').json()['system']['ts'] == 1.0
    hist = client.get('/api/metrics/history', params={'type': 'system'}).json()
    assert len(hist) == 1
    r = client.get('/api/metrics/history', params={'type': 'system',
                                                   'start': 'zzz'})
    assert_error(r, 400, 'validation')


def test_index_endpoints(client):
    r = client.get('/api/index/status')
    assert r.status_code == 200
    assert 'indexing' in r.json()
    assert_error(client.get('/api/index/files', params={'offset': 'x'}),
                 400, 'validation')


def test_config_get_state(client, core, tmp_path):
    body = client.get('/api/config').json()
    assert body['binary'] is None
    assert body['source_dir'] == str(tmp_path)
    assert body['inline'] is True


def test_config_patch_binary_and_source(client, core, tmp_path):
    assert_error(client.patch('/api/config',
                              json={'binary': '/nonexistent/xyz'}),
                 400, 'bad_path')
    assert_error(client.patch('/api/config',
                              json={'source_dir': '/nonexistent/xyz'}),
                 400, 'bad_path')
    # Unknown fields are rejected (extra='forbid')
    assert_error(client.patch('/api/config', json={'bogus': 1}),
                 400, 'validation')

    # A valid patch mutates config and reports the new state
    src = tmp_path / 'src'
    src.mkdir()
    body = client.patch('/api/config',
                        json={'source_dir': str(src)}).json()
    assert body['source_dir'] == str(src)
    assert core.config.source_dir == str(src)
    # Clearing the binary is allowed; the wizard state tracks source_dir
    assert client.patch('/api/config', json={'binary': ''}).status_code == 200
    assert core.wizard['source_dir'] == str(src)


def test_config_patch_pathmap(client, core):
    body = client.patch('/api/config',
                        json={'path_map': {'/build': '/src'}}).json()
    assert body['path_map'] == {'/build': '/src'}
    assert core.config.path_map == {'/build': '/src'}
    assert client.patch('/api/config',
                        json={'path_map': {}}).json()['path_map'] is None


def test_config_patch_toolchain(client, core, tmp_path):
    assert_error(client.patch('/api/config',
                              json={'toolchain_prefix': str(tmp_path) + '/no-such-'}),
                 400, 'bad_path')
    assert_error(client.patch('/api/config',
                              json={'sysroot': '/nonexistent/sysroot'}),
                 400, 'bad_path')
    sysroot = tmp_path / 'sysroot'
    sysroot.mkdir()
    assert client.patch('/api/config',
                        json={'sysroot': str(sysroot)}).json()['sysroot'] == str(sysroot)
    # Explicit empty sysroot clears it
    assert client.patch('/api/config',
                        json={'sysroot': ''}).json()['sysroot'] is None


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
    assert r.status_code == 404
    assert 'secret' not in r.text


def test_session_delete_traversal_blocked(client, core, tmp_path):
    """DELETE must refuse ids that resolve outside sessions_dir."""
    outside = tmp_path / 'precious'
    outside.mkdir()
    (outside / 'metadata.json').write_text('{}')
    # Encoded-slash ids never reach the route (404/405 from routing or the
    # static mount); ids that do reach it are checked by safe_session_dir.
    r = client.delete('/api/sessions/..%2Fprecious')
    assert r.status_code in (404, 405)
    assert outside.is_dir(), 'directory outside sessions_dir was deleted'


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
# Sessions: list / replay / delete / export
# ---------------------------------------------------------------------------

def test_sessions_list_and_pagination(client, session_id):
    body = client.get('/api/sessions').json()
    assert body['total'] == 1
    assert [s['session_id'] for s in body['sessions']] == [session_id]

    page = client.get('/api/sessions', params={'offset': 1, 'limit': 5}).json()
    assert page == {'sessions': [], 'total': 1, 'offset': 1, 'limit': 5}
    assert client.get('/api/sessions',
                      params={'limit': 0}).json()['sessions'] == []


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

    # Replay cache written with the schema-2 key; second call returns the
    # same payload
    cache = os.path.join(core.config.sessions_dir, session_id,
                         'replay_cache.json.gz')
    assert os.path.isfile(cache)
    with gzip_mod.open(cache, 'rt') as f:
        assert json.load(f)['key']['schema'] == 2
    assert client.get(f'/api/sessions/{session_id}').json() == body


def test_session_replay_stale_cache_regenerates(client, core, session_id):
    """A v1-era cache (no schema field) is ignored and rewritten."""
    cache = os.path.join(core.config.sessions_dir, session_id,
                         'replay_cache.json.gz')
    with gzip_mod.open(cache, 'wt') as f:
        json.dump({'key': {'chunks': 1}, 'per_event': {'bogus': {}}}, f)
    body = client.get(f'/api/sessions/{session_id}').json()
    assert 'bogus' not in body['per_event']
    with gzip_mod.open(cache, 'rt') as f:
        assert json.load(f)['key']['schema'] == 2


def test_session_replay_not_found(client):
    assert_error(client.get('/api/sessions/nope'), 404, 'not_found')


def test_session_delete(client, core, session_id):
    assert client.delete(f'/api/sessions/{session_id}').json() == {
        'ok': True, 'session_id': session_id}
    assert not os.path.isdir(os.path.join(core.config.sessions_dir,
                                          session_id))
    assert_error(client.delete(f'/api/sessions/{session_id}'),
                 404, 'not_found')
    assert client.get('/api/sessions').json()['total'] == 0


def test_export_collapsed(client, session_id):
    r = client.get(f'/api/sessions/{session_id}/export',
                   params={'format': 'collapsed'})
    assert r.status_code == 200
    assert 'attachment' in r.headers['content-disposition']
    line = r.text.strip().split('\n')[0]
    stack, count = line.rsplit(' ', 1)
    assert int(count) > 0 and stack


def test_export_json(client, session_id):
    r = client.get(f'/api/sessions/{session_id}/export',
                   params={'format': 'json'})
    body = r.json()
    assert body['metadata']['session_id'] == session_id
    assert body['per_event']


def test_export_svg(client, session_id):
    r = client.get(f'/api/sessions/{session_id}/export',
                   params={'format': 'svg', 'event': 'cycles'})
    assert r.headers['content-type'].startswith('image/svg')
    assert r.text.startswith('<svg')
    assert_error(client.get(f'/api/sessions/{session_id}/export',
                            params={'format': 'svg', 'event': 'nope'}),
                 404, 'not_found')


def test_export_unknown_format(client, session_id):
    assert_error(client.get(f'/api/sessions/{session_id}/export',
                            params={'format': 'xml'}),
                 400, 'bad_format')


def test_export_session_not_found(client):
    assert_error(client.get('/api/sessions/nope/export'), 404, 'not_found')


def test_live_export_no_data(client):
    assert_error(client.get('/api/live/export'), 404, 'no_data')


def test_live_export(client, core):
    samples = _seed_live_state(core)
    r = client.get('/api/live/export', params={'format': 'collapsed'})
    assert r.status_code == 200
    assert 'attachment' in r.headers['content-disposition']

    r = client.get('/api/live/export', params={'format': 'json'})
    assert r.json()['metadata']['session_id'] == 'live'

    evt = samples[0]['event_type']
    r = client.get('/api/live/export', params={'format': 'svg',
                                               'event': evt})
    assert r.text.startswith('<svg')


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


def test_threads_summary_and_view(client, core):
    samples = _seed_live_state(core)
    evt = samples[0]['event_type']

    r = client.get('/api/threads', params={'event': evt})
    body = r.json()
    assert body['total_samples'] > 0
    assert body['threads']
    tid = body['threads'][0]['tid']

    r = client.get(f'/api/threads/{tid}', params={'event': evt})
    view = r.json()
    assert view['function_summary']['total_samples'] > 0
    assert view['flamegraph']['value'] > 0

    # Non-integer tid is a path-validation failure
    assert_error(client.get('/api/threads/zz', params={'event': evt}),
                 400, 'validation')
    # Unknown tid is empty but well-formed
    empty = client.get('/api/threads/999999999',
                       params={'event': evt}).json()
    assert empty['function_summary']['total_samples'] == 0


def test_window(client, core):
    """Samples are stamped with arrival time; the window endpoint filters
    the raw deque by it (timeline scrubbing)."""
    import time as time_mod
    samples = _seed_live_state(core)
    evt = samples[0]['event_type']
    now = time_mod.time()

    # A window covering "now" holds everything just seeded
    r = client.get('/api/window', params={
        'event': evt, 'start': now - 60, 'end': now + 60})
    assert r.status_code == 200
    body = r.json()
    assert body['window']['samples'] > 0
    assert body['function_summary']['total_samples'] == body['window']['samples']
    assert body['flamegraph']['value'] > 0

    # A window in the past is empty but well-formed
    r = client.get('/api/window', params={
        'event': evt, 'start': now - 120, 'end': now - 60})
    body = r.json()
    assert body['window']['samples'] == 0
    assert body['function_summary']['total_samples'] == 0

    # Validation
    assert_error(client.get('/api/window', params={'event': evt}),
                 400, 'validation')
    assert_error(client.get('/api/window',
                            params={'event': evt, 'start': now - 60,
                                    'end': now + 60, 'tid': 'zz'}),
                 400, 'validation')

    # tid filter composes with the window
    tids = {s.get('tid', s.get('pid', 0)) for s in samples
            if s['event_type'] == evt}
    tid = next(iter(tids))
    r = client.get('/api/window', params={
        'event': evt, 'start': now - 60, 'end': now + 60, 'tid': tid})
    assert r.json()['window']['samples'] > 0


def test_source_endpoint_errors(client, core):
    _seed_live_state(core)
    # No source mapper configured → 409, not a fake-success body
    assert_error(client.get('/api/source', params={'file': 'x.c'}),
                 409, 'no_mapper')
    # Missing required file param → validation
    assert_error(client.get('/api/source'), 400, 'validation')


def test_snapshot_gzip_negotiation(client, core):
    """Payloads >8KB gzip when the client accepts it."""
    big = {'function_summary': {'total_samples': 1,
                                'functions': [{'name': f'f{i}' * 20,
                                               'module': 'm'}
                                              for i in range(2000)]},
           'flamegraph': {'name': 'root', 'value': 1, 'children': []},
           'threads': [], 'source_files': []}
    with core.state.lock:
        core.state._cached_per_event = {'cycles': big}
    r = client.get('/api/snapshot', params={'event': 'cycles'},
                   headers={'Accept-Encoding': 'gzip'})
    assert r.headers.get('content-encoding') == 'gzip'
    assert r.json()['event'] == 'cycles'  # httpx transparently decompresses
    # Without Accept-Encoding: no compression
    r = client.get('/api/snapshot', params={'event': 'cycles'},
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
        web.create_app(core), host='127.0.0.1', port=port, log_level='error'))
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
    data_version (carrying event_types) and perf_stat frames."""
    with core.state.lock:
        core.state._cached_per_event = {'cycles': {}}
        core.state._event_types_set.add('cycles')
        core.state.event_types = ['cycles']
        core.state.perf_stat = {'cycles': {'value': 1, 'comment': ''}}

    got = _read_sse(live_server, {'data_version', 'perf_stat'})
    assert got['data_version']['event_types'] == ['cycles']
    assert got['perf_stat']['cycles']['value'] == 1


def test_sse_broadcast_reaches_client(core, live_server):
    """A broadcast() from a worker thread lands on connected clients.
    The broadcast fires after the first received line, which proves the
    client's queue is registered. The consolidated 'metrics' event carries
    its type in the payload."""
    def fire():
        core.broadcast('status', {'connected': True, 'agent': 'x:1'})
        core.broadcast('metrics', {'type': 'system', 'ts': 2.0})

    got = _read_sse(live_server, {'status', 'metrics'}, on_first_line=fire)
    assert got['status'] == {'connected': True, 'agent': 'x:1'}
    assert got['metrics']['type'] == 'system'
