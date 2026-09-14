"""The managed session after the handshake: what the receive loop does
with what the agent sends, how a session is persisted, and how one is
replaced or ended.

Driven by a scripted pure-Python agent (the C binary's protocol tests live
in test_agent_protocol.py); this is the server half.
"""

import json
import os
import socket
import struct
import threading
import time

import pytest
from test_agentlink_auth import FLAG_METRICS, make_ctx, read_frame, send_frame

from perflens import agentlink
from perflens.sessions import sweep_sessions_dir

FLAG_DATA_RAW = 0
FLAG_CMD_REQUEST = 2
FLAG_CMD_RESPONSE = 3

SCRIPT = (
    'myapp  1234/1234  100.000100: 250000 cycles: \n'
    '\t             401136 hot_function (/usr/bin/myapp)\n'
    '\t             401200 main (/usr/bin/myapp)\n'
    '\n'
)
STAT = (
    '\n### PERF_STAT ###\n'
    " Performance counter stats for process id '1234':\n\n"
    "         1,234,567      cycles\n"
    "              2.00 msec task-clock\n\n"
    "       0.100 seconds time elapsed\n"
)


class ScriptedAgent:
    """A --listen agent that, once paired, sends `script` (a list of
    (flag, bytes) frames) and then answers commands until closed."""

    def __init__(self, script=(), code='pair-me', pid=None):
        self.script = list(script)
        self.code = code
        self.commands = []
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.conn = None
        self.paired = threading.Event()
        self.script_sent = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            self.listener.settimeout(15)
            self.conn, _ = self.listener.accept()
            self.conn.settimeout(15)
            send_frame(self.conn, {'type': 'hello', 'version': 1, 'agent': 'perflens',
                                   'agent_version': '0.12.0', 'auth': 'token',
                                   'platform': {'arch': 'x86_64', 'kernel': 'k'}})
            flag, req = read_frame(self.conn)
            if flag is None or req.get('cmd') != 'auth':
                return
            ok = (req.get('args') or {}).get('token') == self.code
            send_frame(self.conn, {'id': req.get('id'), 'ok': ok,
                                   'error': None if ok else 'auth failed'})
            if not ok:
                return
            self.paired.set()
            for flag, payload in self.script:
                if flag == 'raw':           # pre-framed bytes, sent as-is
                    self.conn.sendall(payload)
                    continue
                self.conn.sendall(struct.pack('!IB', len(payload), flag) + payload)
            self.script_sent.set()
            while True:
                flag, req = read_frame(self.conn)
                if flag is None:
                    return
                self.commands.append(req)
                send_frame(self.conn, {'id': req.get('id'), 'ok': True,
                                       'cmd': req.get('cmd')})
        except (OSError, ValueError):
            pass

    def close(self):
        for s in (self.conn, self.listener):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


@pytest.fixture()
def scripted():
    made = []

    def _make(**kw):
        a = ScriptedAgent(**kw)
        made.append(a)
        return a

    yield _make
    for a in made:
        a.close()


def wait_for(cond, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def data_frame(text):
    return (FLAG_DATA_RAW, text.encode())


def json_frame(flag, obj):
    return (flag, json.dumps(obj).encode())


# ---------------------------------------------------------------------------
# The receive loop
# ---------------------------------------------------------------------------

def test_malformed_frames_do_not_end_the_session(scripted):
    """A metrics frame that is not an object, a response that is not JSON,
    and a stat line the parser cannot read each used to end the session
    through the receive loop's catch-all. Now each is dropped and logged."""
    a = scripted(script=[
        (FLAG_METRICS, b'[1, 2, 3]'),
        (FLAG_CMD_RESPONSE, b'{not json'),
        data_frame('\n### PERF_STAT ###\n   1.2.3 seconds time elapsed\n'),
        json_frame(FLAG_METRICS, {'ts': 1, 'type': 'system', 'cpu': None}),
        data_frame(SCRIPT),
    ])
    ctx = make_ctx()
    session = agentlink.connect_to_agent(ctx, '127.0.0.1', a.port, token='pair-me')
    try:
        assert a.script_sent.wait(10)
        assert wait_for(lambda: len(ctx.state.all_samples) == 1)
        assert session.connected
        assert session.send_command('ping', timeout=10)['ok'] is True
        # The summary over a frame with cpu: null does not raise either
        assert ctx.metrics.get_summary()['snapshots'] == 1
    finally:
        session.close()


def test_stat_only_chunk_keeps_its_counters(scripted):
    """The first chunk or two after start carry only PERF_STAT while perf
    record fills its ring buffer. Their counters used to be dropped with
    the samples they did not have."""
    a = scripted(script=[data_frame(STAT), data_frame(SCRIPT + STAT)])
    ctx = make_ctx()
    session = agentlink.connect_to_agent(ctx, '127.0.0.1', a.port, token='pair-me')
    try:
        assert wait_for(lambda: len(ctx.state.all_samples) == 1)
        assert wait_for(lambda: ctx.state.perf_stat.get('cycles', {}).get('value')
                        == 2 * 1234567)
        assert ctx.state.chunk_count == 1      # the stat-only chunk is not a data chunk
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_session_metadata_is_written_early_and_finalized(scripted):
    """A server that dies mid-session used to leave chunks with no
    metadata.json -- unlisted and unreplayable."""
    a = scripted(script=[data_frame(SCRIPT + STAT), data_frame(SCRIPT)])
    ctx = make_ctx()
    session = agentlink.connect_to_agent(ctx, '127.0.0.1', a.port, token='pair-me')
    sessions_dir = ctx.config.sessions_dir
    try:
        assert wait_for(lambda: len(ctx.state.all_samples) == 2)
        dirs = os.listdir(sessions_dir)
        assert len(dirs) == 1
        meta_path = os.path.join(sessions_dir, dirs[0], 'metadata.json')
        assert wait_for(lambda: os.path.isfile(meta_path) and
                        json.load(open(meta_path))['chunks'] == 2)
        meta = json.load(open(meta_path))
        assert meta['live'] is True
        assert meta['total_samples'] == 2
        assert meta['event_types'] == ['cycles']
        assert meta['platform']['arch'] == 'x86_64'
        assert sorted(f for f in os.listdir(os.path.dirname(meta_path))
                      if f.startswith('chunk_')) == ['chunk_00000.txt', 'chunk_00001.txt']
    finally:
        session.close()
    session.join(timeout=10)
    meta = json.load(open(meta_path))
    assert meta['live'] is False
    assert meta['chunks'] == 2 and meta['total_samples'] == 2
    assert meta['ring_samples'] == 2
    assert meta['perf_stat']['cycles']['value'] == 1234567


def test_a_session_that_received_nothing_leaves_no_directory(scripted):
    a = scripted(script=[])
    ctx = make_ctx()
    session = agentlink.connect_to_agent(ctx, '127.0.0.1', a.port, token='pair-me')
    assert a.paired.wait(10)
    assert wait_for(lambda: len(os.listdir(ctx.config.sessions_dir)) == 1)
    session.close()
    session.join(timeout=10)
    assert os.listdir(ctx.config.sessions_dir) == []


def test_a_failed_chunk_write_is_a_gap_not_an_overwrite(scripted, monkeypatch):
    """ENOSPC mid-write used to leave a truncated file *and* reuse its
    index, so the next chunk overwrote it and the count under-reported."""
    ctx = make_ctx()
    failed = []
    real_replace = os.replace

    def flaky_replace(src, dst):
        if dst.endswith('chunk_00000.txt') and not failed:
            failed.append(dst)
            raise OSError(28, 'No space left on device')
        return real_replace(src, dst)

    monkeypatch.setattr(agentlink.os, 'replace', flaky_replace)
    a = scripted(script=[data_frame(SCRIPT), data_frame(SCRIPT)])
    session = agentlink.connect_to_agent(ctx, '127.0.0.1', a.port, token='pair-me')
    try:
        assert wait_for(lambda: len(ctx.state.all_samples) == 2)
        d = os.path.join(ctx.config.sessions_dir, os.listdir(ctx.config.sessions_dir)[0])
        assert wait_for(lambda: 'chunk_00001.txt' in os.listdir(d))
        names = sorted(os.listdir(d))
        assert 'chunk_00000.txt' not in names
        assert 'chunk_00001.txt' in names
        assert not [n for n in names if n.endswith('.tmp')]
    finally:
        session.close()


def test_sweep_removes_empty_dirs_and_recovers_orphans(tmp_path):
    empty = tmp_path / '20260101_000000_dev:9999'
    empty.mkdir()
    orphan = tmp_path / '20260102_000000_dev:9999'
    orphan.mkdir()
    (orphan / 'chunk_00000.txt').write_text(SCRIPT)
    kept = tmp_path / '20260103_000000_dev:9999'
    kept.mkdir()
    (kept / 'metadata.json').write_text('{}')
    (tmp_path / 'not-a-dir').write_text('x')

    removed, recovered = sweep_sessions_dir(str(tmp_path))
    assert (removed, recovered) == (1, 1)
    assert not empty.exists()
    meta = json.loads((orphan / 'metadata.json').read_text())
    assert meta['recovered'] is True and meta['chunks'] == 1 and meta['live'] is False
    assert (kept / 'metadata.json').read_text() == '{}'


# ---------------------------------------------------------------------------
# Replacing and ending a session
# ---------------------------------------------------------------------------

def test_replacing_agent_finishes_the_old_session_first(scripted):
    """The old receiver may be mid-chunk when the new agent arrives. The
    reset used to run underneath it, so the dying agent's last chunk landed
    in the new session and the old one was saved with the new (empty)
    metrics."""
    ctx = make_ctx()
    old = scripted(script=[json_frame(FLAG_METRICS, {'ts': 1.0, 'type': 'system',
                                                     'cpu': {'overall_pct': 50},
                                                     'mem': {'used_pct': 10}}),
                           data_frame(SCRIPT)])
    s1 = agentlink.connect_to_agent(ctx, '127.0.0.1', old.port, token='pair-me')
    assert wait_for(lambda: len(ctx.state.all_samples) == 1)
    assert wait_for(lambda: ctx.metrics.get_latest().get('system') is not None)
    old_dir = os.path.join(ctx.config.sessions_dir, os.listdir(ctx.config.sessions_dir)[0])

    new = scripted(script=[])
    s2 = agentlink.connect_to_agent(ctx, '127.0.0.1', new.port, token='pair-me')
    try:
        assert not s1.connected
        assert len(ctx.state.all_samples) == 0
        assert ctx.metrics.get_latest() == {}
        s1.join(timeout=10)
        meta = json.load(open(os.path.join(old_dir, 'metadata.json')))
        assert meta['live'] is False and meta['chunks'] == 1
        assert meta['metrics_summary']['snapshots'] == 1
        metrics = json.load(open(os.path.join(old_dir, 'metrics.json')))
        assert metrics['system'][0]['cpu']['overall_pct'] == 50
    finally:
        s2.close()


def test_shutdown_stops_the_agent_and_saves_the_session(scripted):
    a = scripted(script=[data_frame(SCRIPT)])
    ctx = make_ctx()
    agentlink.connect_to_agent(ctx, '127.0.0.1', a.port, token='pair-me')
    assert wait_for(lambda: len(ctx.state.all_samples) == 1)
    d = os.path.join(ctx.config.sessions_dir, os.listdir(ctx.config.sessions_dir)[0])

    agentlink.shutdown_agent(ctx, timeout=10)
    assert ctx.agent.current() is None
    assert [c['cmd'] for c in a.commands] == ['stop']
    meta = json.load(open(os.path.join(d, 'metadata.json')))
    assert meta['live'] is False and meta['chunks'] == 1


def test_command_timeout_is_bounded(client):
    for bad in (0, -1, 601, 10 ** 6):
        r = client.post('/api/agent/command', json={'cmd': 'ping', 'timeout': bad})
        assert r.status_code == 400, (bad, r.text)
        assert r.json()['error']['code'] == 'validation'


# ---------------------------------------------------------------------------
# Bounds on the socket
# ---------------------------------------------------------------------------

def test_session_socket_has_keepalive_and_a_send_bound(scripted):
    a = scripted(script=[])
    ctx = make_ctx()
    session = agentlink.connect_to_agent(ctx, '127.0.0.1', a.port, token='pair-me')
    try:
        s = session.sock
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1
        secs, _ = struct.unpack('ll', s.getsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, 16))
        assert secs == agentlink.SEND_TIMEOUT_SECS
        assert s.gettimeout() is None       # the recv loop blocks indefinitely
    finally:
        session.close()


def test_oversized_hello_is_refused_before_it_is_read():
    """A garbage header can claim 4 GB; the pre-auth reader used to allocate
    it. The hello is capped at 64 KB and the connection dropped above it."""
    lst = socket.socket()
    lst.bind(('127.0.0.1', 0))
    lst.listen(1)
    port = lst.getsockname()[1]
    seen = {}

    def serve():
        conn, _ = lst.accept()
        conn.sendall(struct.pack('!IB', 1 << 30, FLAG_CMD_RESPONSE))
        try:
            seen['peer_closed'] = conn.recv(1) == b''
        except OSError:
            seen['peer_closed'] = True
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        with pytest.raises(RuntimeError, match='oversized frame'):
            agentlink.connect_to_agent(make_ctx(), '127.0.0.1', port, timeout=5)
        t.join(5)
        assert seen.get('peer_closed') is True
    finally:
        lst.close()


def test_oversized_session_frame_ends_the_session(scripted):
    a = scripted(script=[('raw', struct.pack('!IB', agentlink.MAX_FRAME_SIZE + 1,
                                             FLAG_DATA_RAW))])
    ctx = make_ctx()
    session = agentlink.connect_to_agent(ctx, '127.0.0.1', a.port, token='pair-me')
    try:
        assert wait_for(lambda: not session.connected)
        assert ctx.state.agent_connected is False
    finally:
        session.close()


def test_accept_loop_survives_an_accept_error(monkeypatch):
    """One EMFILE or ECONNABORTED used to end the listener thread for good,
    with nothing in the API saying so."""
    events = []

    class FakeListener:
        def __init__(self, *a, **k):
            self.calls = 0

        def setsockopt(self, *a):
            pass

        def bind(self, addr):
            events.append(('bind', addr))

        def listen(self, n):
            pass

        def accept(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError(24, 'Too many open files')
            if self.calls == 2:
                return FakeConn(), ('10.0.0.2', 5555)
            raise KeyboardInterrupt

        def close(self):
            events.append(('close',))

    class FakeConn:
        def settimeout(self, t):
            pass

        def close(self):
            events.append(('conn_closed',))

    monkeypatch.setattr(agentlink.socket, 'socket', FakeListener)
    monkeypatch.setattr(agentlink.time, 'sleep', lambda s: events.append(('sleep', s)))
    monkeypatch.setattr(agentlink, 'handle_inbound_agent',
                        lambda ctx, conn, addr: events.append(('inbound', addr)))
    agentlink.run_tcp_server(make_ctx())
    assert wait_for(lambda: ('inbound', ('10.0.0.2', 5555)) in events)
    assert ('sleep', 1) in events
    assert events[-1] == ('close',)
