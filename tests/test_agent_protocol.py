"""C-agent protocol tests: the real agent binary against a fake framing
server, with `perf` replaced by a shim on PATH.

Covers the wire framing (5-byte header, flags 0-4), the hello handshake
(incl. --token), the command protocol (ping/status/start/pause/resume/
stop, unknown commands, the start-while-paused regression), the data
path (zstd frames that decompress to perf script output + PERF_STAT
section), health-metrics frames, reconnect-after-disconnect, and the
headless --output mode (multi-round markers).
"""

import json
import os
import queue
import re
import socket
import struct
import subprocess
import threading
import time
import uuid

import pytest
import zstandard

from conftest import AGENT_BIN, agent_binary_runs

pytestmark = pytest.mark.skipif(
    not agent_binary_runs(),
    reason='agent binary missing, stale, or built for another architecture '
           '(run `make -C agent-c`)')

FLAG_DATA_RAW = 0
FLAG_DATA_ZSTD = 1
FLAG_CMD_REQUEST = 2
FLAG_CMD_RESPONSE = 3
FLAG_METRICS = 4

SUPPORTED_EVENTS = ('cycles', 'instructions', 'page-faults')

# What the shim's `perf script` emits (SCRIPT_FIELDS format).
SCRIPT_OUTPUT = (
    'myapp  1234/1234  100.000100: 250000 cycles: \n'
    '\t             401136 hot_function (/usr/bin/myapp)\n'
    '\t             401200 main (/usr/bin/myapp)\n'
    '\n'
    'myapp  1234/1235  100.000200: 250000 cycles: \n'
    '\t             401300 worker (/usr/bin/myapp)\n'
    '\n'
)

PERF_SHIM = r'''#!/usr/bin/env python3
"""Fake `perf` for agent tests. Supports --version / stat / record /
script; rejects events outside SUPPORTED and call-graph methods
other than fp, like a restricted kernel would."""
import os, sys, time

SUPPORTED = %(supported)r
SCRIPT_OUTPUT = %(script_output)r

args = sys.argv[1:]
log = os.environ.get('PERF_SHIM_LOG')
if log:
    with open(log, 'a') as f:
        f.write(' '.join(args) + '\n')

def opt(name):
    return args[args.index(name) + 1] if name in args else None

sub = args[0] if args else ''

if sub == '--version':
    print('perf version 6.99.shim')
    sys.exit(0)

if sub == 'stat':
    for ev in (opt('-e') or '').split(','):
        if ev and ev not in SUPPORTED and ev != 'task-clock':
            sys.stderr.write("event syntax error: '%%s'\n" %% ev)
            sys.exit(1)
    time.sleep(0.05)
    sys.stderr.write(
        " Performance counter stats for process id '%%s':\n\n"
        "         1,234,567      cycles\n"
        "           234,567      instructions\n"
        "                12      page-faults\n"
        "              2.00 msec task-clock\n\n"
        "       0.100 seconds time elapsed\n" %% (opt('-p') or '?'))
    sys.exit(0)

if sub == 'record':
    cg = opt('--call-graph')
    if cg and cg != 'fp':
        sys.stderr.write('callchain: %%s not supported\n' %% cg)
        sys.exit(1)
    for ev in (opt('-e') or '').split(','):
        if ev and ev not in SUPPORTED:
            sys.stderr.write('invalid event: %%s\n' %% ev)
            sys.exit(1)
    out = opt('-o')
    if out == '-':
        if os.environ.get('PERF_SHIM_NO_PIPE'):
            sys.stderr.write('pipe output not supported\n')
            sys.exit(1)
        try:
            if 'sleep' in args:
                # probe: bounded run
                sys.stdout.write('FAKEPERFDATA\n')
                sys.stdout.flush()
                time.sleep(0.2)
            else:
                # continuous: emit until killed
                while True:
                    sys.stdout.write('FAKEPERFDATA\n')
                    sys.stdout.flush()
                    time.sleep(0.2)
        except (BrokenPipeError, IOError):
            pass
        sys.exit(0)
    if out:
        with open(out, 'w') as f:
            f.write('FAKEPERFDATA')
    time.sleep(0.2)
    sys.exit(0)

if sub == 'script':
    if opt('-i') == '-':
        if os.environ.get('PERF_SHIM_NO_PIPE'):
            sys.stderr.write('cannot read from pipe\n')
            sys.exit(1)
        try:
            for _line in sys.stdin:
                sys.stdout.write(SCRIPT_OUTPUT)
                sys.stdout.flush()
        except (BrokenPipeError, IOError):
            pass
        sys.exit(0)
    sys.stdout.write(SCRIPT_OUTPUT)
    sys.exit(0)

sys.stderr.write('shim: unhandled perf invocation: %%r\n' %% args)
sys.exit(1)
''' % {'supported': SUPPORTED_EVENTS, 'script_output': SCRIPT_OUTPUT}


@pytest.fixture(scope='module')
def shim_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp('perf-shim')
    shim = d / 'perf'
    shim.write_text(PERF_SHIM)
    shim.chmod(0o755)
    return d


@pytest.fixture()
def target_pid():
    """A real process for the agent to 'profile'."""
    proc = subprocess.Popen(['sleep', '300'])
    yield proc.pid
    proc.kill()
    proc.wait()


def recv_exactly(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('agent disconnected')
        buf += chunk
    return buf


class AgentHarness:
    """Fake server end of the wire protocol driving a real agent subprocess.

    mode='server' (default) — the agent dials us; we listen.
    mode='listen'           — the agent listens; we dial it.

    The two differ only in how the socket is obtained. Everything after that
    goes through _attach, which mirrors the server's own two entry points
    being identical after TCP setup.
    """

    def __init__(self, shim_dir, tmp_path, agent_args=(), env=None,
                 mode='server', log_path=None):
        self.mode = mode
        self.listener = None
        self.conn = None
        self.frames = None
        self._reader = None

        full_env = dict(os.environ)
        full_env['PATH'] = f'{shim_dir}:{full_env["PATH"]}'
        full_env['PERF_SHIM_LOG'] = str(tmp_path / 'perf-shim.log')
        full_env.update(env or {})
        self.shim_log = full_env['PERF_SHIM_LOG']

        # The agent's own log, so tests can read a generated pairing code the
        # way an operator does.
        self.log_path = log_path or str(tmp_path / 'agent.log')
        self._log_fh = open(self.log_path, 'wb')

        if mode == 'server':
            self.listener = socket.socket()
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind(('127.0.0.1', 0))
            self.listener.listen(1)
            self.listener.settimeout(15)
            self.port = self.listener.getsockname()[1]
            argv = [AGENT_BIN, '--server', '127.0.0.1',
                    '--port', str(self.port), *agent_args]
        else:
            # Reserve a port by binding and releasing. That leaves a race with
            # the agent's own bind, which the connect retry loop below absorbs.
            probe = socket.socket()
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
            probe.close()
            argv = [AGENT_BIN, '--listen', '--bind', '127.0.0.1',
                    '--port', str(self.port), *agent_args]

        self.proc = subprocess.Popen(
            argv, env=full_env, stdout=self._log_fh, stderr=subprocess.STDOUT)

        if mode == 'server':
            self.accept()
        else:
            self.dial()

    def accept(self):
        """(Re-)accept the agent's connection and restart the reader."""
        conn, _ = self.listener.accept()
        self._attach(conn)

    def dial(self, timeout=30):
        """Connect to a --listen agent, retrying until its socket is up."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                self._attach(socket.create_connection(
                    ('127.0.0.1', self.port), timeout=10))
                return
            except (ConnectionError, OSError) as e:
                last = e
                time.sleep(0.25)
        raise AssertionError(f'could not connect to --listen agent: {last}')

    def _attach(self, conn):
        """Take ownership of a connected socket and start reading frames.

        A fresh queue per connection: the previous reader's disconnect
        sentinel must not leak into the new session."""
        self.conn = conn
        self.frames = queue.Queue()
        self.conn.settimeout(30)
        self._reader = threading.Thread(
            target=self._read_loop, args=(self.conn, self.frames),
            daemon=True)
        self._reader.start()

    def read_log(self):
        self._log_fh.flush()
        with open(self.log_path) as f:
            return f.read()

    def pairing_code(self, timeout=30):
        """The generated code, read from the agent's log as an operator would."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = re.search(r'Pairing code: (\w+)', self.read_log())
            if m:
                return m.group(1)
            time.sleep(0.25)
        raise AssertionError(
            f'no pairing code in agent log:\n{self.read_log()}')

    def authenticate(self, token, timeout=30):
        """Complete the pairing handshake; returns the agent's response."""
        return self.command('auth', timeout=timeout, args={'token': token})

    @staticmethod
    def _read_loop(conn, frames):
        try:
            while True:
                header = recv_exactly(conn, 5)
                length, flag = struct.unpack('>IB', header)
                payload = recv_exactly(conn, length) if length else b''
                frames.put((flag, payload))
        except (ConnectionError, OSError):
            frames.put((None, b''))

    def wait_frame(self, flags, timeout=30, pred=None):
        """Next frame whose flag is in `flags` (and matches pred);
        other frames are discarded."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f'timed out waiting for flags {flags}'
            flag, payload = self.frames.get(timeout=remaining)
            assert flag is not None, 'agent disconnected'
            if flag in flags and (pred is None or pred(payload)):
                return flag, payload

    def command(self, cmd, timeout=30, **kwargs):
        """Send a command frame, return the matching JSON response."""
        cmd_id = uuid.uuid4().hex[:12]
        payload = json.dumps({'cmd': cmd, 'id': cmd_id, **kwargs}).encode()
        self.conn.sendall(struct.pack('>IB', len(payload), FLAG_CMD_REQUEST)
                          + payload)
        _, resp = self.wait_frame(
            {FLAG_CMD_RESPONSE}, timeout=timeout,
            pred=lambda p: json.loads(p).get('id') == cmd_id)
        return json.loads(resp)

    def read_hello(self):
        _, payload = self.wait_frame(
            {FLAG_CMD_RESPONSE}, timeout=15,
            pred=lambda p: json.loads(p).get('type') == 'hello')
        return json.loads(payload)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for s in (self.conn, self.listener):
            if s is not None:
                s.close()
        self._log_fh.close()


@pytest.fixture()
def harness(shim_dir, tmp_path):
    h = AgentHarness(shim_dir, tmp_path)
    yield h
    h.close()


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------

def test_hello(harness):
    hello = harness.read_hello()
    assert hello['agent'] == 'perflens'
    assert hello['version'] == 1
    with open(os.path.join(os.path.dirname(__file__), '..', 'VERSION')) as f:
        assert hello['agent_version'] == f.read().strip()
    assert hello['platform']['perf_version'].startswith('perf version 6.99')
    assert 'arch' in hello['platform']
    assert hello['auth'] == 'token'
    assert 'token' not in hello


# ---------------------------------------------------------------------------
# Pairing-code authentication
#
# The hello goes to whoever completed the TCP handshake, before that peer has
# proved anything. Everything here exists to keep secrets out of it and to
# keep commands behind the gate.
# ---------------------------------------------------------------------------

def test_hello_never_carries_an_explicit_token(shim_dir, tmp_path):
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        hello = h.read_hello()
        assert 'token' not in hello
        assert 's3cret' not in json.dumps(hello)
    finally:
        h.close()


def test_hello_never_carries_an_env_token(shim_dir, tmp_path):
    h = AgentHarness(shim_dir, tmp_path, env={'PERFLENS_TOKEN': 'envtok'})
    try:
        hello = h.read_hello()
        assert 'token' not in hello
        assert 'envtok' not in json.dumps(hello)
    finally:
        h.close()


@pytest.mark.parametrize('cmd', ['ping', 'status', 'list_processes',
                                 'start', 'reprobe', 'update'])
def test_commands_rejected_before_auth(shim_dir, tmp_path, cmd):
    """The gate sits in dispatch_command, so it covers the whole table."""
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        h.read_hello()
        resp = h.command(cmd)
        assert resp['ok'] is False
        assert resp['error'] == 'unauthenticated'
    finally:
        h.close()


def test_auth_then_commands_accepted(shim_dir, tmp_path):
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        h.read_hello()
        assert h.authenticate('s3cret')['ok'] is True
        assert h.command('ping')['ok'] is True
    finally:
        h.close()


def test_auth_wrong_code_leaves_session_locked(shim_dir, tmp_path):
    """A failed attempt must not leave a half-open state."""
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        h.read_hello()
        resp = h.authenticate('wrong')
        assert resp['ok'] is False
        assert resp['error'] == 'auth failed'
        assert h.command('ping')['error'] == 'unauthenticated'
        # ...and the right code still works afterwards.
        assert h.authenticate('s3cret')['ok'] is True
        assert h.command('ping')['ok'] is True
    finally:
        h.close()


def test_auth_failure_cap_closes_session(shim_dir, tmp_path):
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        h.read_hello()
        for _ in range(3):
            assert h.authenticate('wrong')['ok'] is False
        # The agent drops the peer rather than letting it guess forever.
        flag, _ = h.frames.get(timeout=15)
        assert flag is None, 'expected the agent to close the session'
    finally:
        h.close()


def test_no_metrics_before_auth(shim_dir, tmp_path):
    """Metrics carry CPU/memory/temperature and per-process detail. They must
    not stream to a peer that has not proved itself — this is the regression
    test for the thread being started before the gate."""
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        h.read_hello()
        deadline = time.monotonic() + 5      # metrics interval is 2s
        while time.monotonic() < deadline:
            try:
                flag, _ = h.frames.get(timeout=0.5)
            except queue.Empty:
                continue
            assert flag != FLAG_METRICS, 'metrics leaked before authentication'

        assert h.authenticate('s3cret')['ok'] is True
        h.wait_frame({FLAG_METRICS}, timeout=15)
    finally:
        h.close()


def test_tokenless_server_mode_needs_no_auth(harness):
    """--server mode dials an operator-chosen address and exposes no
    listening socket, so a secret stays optional there."""
    harness.read_hello()
    assert harness.command('ping')['ok'] is True


def test_update_refused_without_a_pairing_code(harness):
    """The one command that fetches and executes new code."""
    harness.read_hello()
    resp = harness.command('update')
    assert resp['ok'] is False
    assert 'pairing code' in resp['error']


# ---------------------------------------------------------------------------
# --listen mode — the direction that had no coverage at all
# ---------------------------------------------------------------------------

def test_listen_mode_generates_and_logs_a_pairing_code(shim_dir, tmp_path):
    h = AgentHarness(shim_dir, tmp_path, mode='listen')
    try:
        code = h.pairing_code()
        assert len(code) == 32 and all(c in '0123456789abcdef' for c in code)

        hello = h.read_hello()
        assert 'token' not in hello
        assert code not in json.dumps(hello), 'code leaked in the hello'

        assert h.command('ping')['error'] == 'unauthenticated'
        assert h.authenticate(code)['ok'] is True
        assert h.command('ping')['ok'] is True
    finally:
        h.close()


def test_listen_mode_honours_an_explicit_token(shim_dir, tmp_path):
    h = AgentHarness(shim_dir, tmp_path, mode='listen',
                     agent_args=['--token', 'explicit-code'])
    try:
        h.read_hello()
        assert 'Pairing code:' not in h.read_log(), \
            'should not generate a code when one was supplied'
        assert h.authenticate('explicit-code')['ok'] is True
    finally:
        h.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def test_ping(harness):
    harness.read_hello()
    assert harness.command('ping')['ok'] is True


def test_unknown_command(harness):
    harness.read_hello()
    resp = harness.command('frobnicate')
    assert resp['ok'] is False
    assert 'unknown command' in resp['error']


def test_status_idle(harness):
    harness.read_hello()
    resp = harness.command('status')
    assert resp['ok'] is True
    assert resp['state'] == 'idle'
    assert 'platform' in resp


def test_start_requires_valid_pid(harness):
    harness.read_hello()
    resp = harness.command('start', args={'pid': 999999999})
    assert resp['ok'] is False
    assert 'not found' in resp['error']


# ---------------------------------------------------------------------------
# Full lifecycle: probe, collect, pause/resume, stop
# ---------------------------------------------------------------------------

def test_lifecycle_and_data_frames(harness, target_pid):
    harness.read_hello()

    resp = harness.command('start',
                           args={'pid': target_pid, 'frequency': 99,
                                 'duration': 1},
                           timeout=60)
    assert resp['ok'] is True, resp
    # Probe found exactly what the shim supports
    assert resp['events'] == ['cycles', 'instructions']
    assert resp['callgraph'] == 'fp'
    # Shim supports pipe mode, so continuous collection is used
    assert resp['mode'] == 'continuous'

    # Data frames flow; zstd payload decompresses to the shim's script
    # output plus the appended PERF_STAT section
    flag, payload = harness.wait_frame({FLAG_DATA_RAW, FLAG_DATA_ZSTD},
                                       timeout=30)
    if flag == FLAG_DATA_ZSTD:
        payload = zstandard.ZstdDecompressor().decompress(
            payload, max_output_size=1 << 20)
    text = payload.decode()
    assert 'hot_function' in text
    assert '### PERF_STAT ###' in text
    assert 'task-clock' in text

    status = harness.command('status')
    assert status['state'] == 'profiling'
    assert status['pid'] == target_pid
    assert status['capabilities']['record_events'] == [
        'cycles', 'instructions']
    assert status['capabilities']['stat_only_events'] == ['page-faults']
    assert status['capabilities']['pipe_mode'] is True

    # Double-start rejected
    resp = harness.command('start', args={'pid': target_pid})
    assert resp['ok'] is False
    assert 'already profiling' in resp['error']

    # Pause; start-while-paused rejected (phase-1a regression)
    assert harness.command('pause')['ok'] is True
    assert harness.command('status')['state'] == 'paused'
    resp = harness.command('start', args={'pid': target_pid})
    assert resp['ok'] is False
    assert 'paused' in resp['error']

    assert harness.command('resume')['ok'] is True
    assert harness.command('status')['state'] == 'profiling'

    assert harness.command('stop')['ok'] is True
    assert harness.command('status')['state'] == 'idle'

    # The shim really was driven like perf would be
    with open(harness.shim_log) as f:
        log = f.read()
    assert 'record' in log and 'stat' in log and 'script' in log


def test_pause_resume_require_profiling(harness):
    harness.read_hello()
    assert harness.command('pause')['ok'] is False
    assert harness.command('resume')['ok'] is False


def test_start_with_event_subset(harness, target_pid):
    """start accepts args.events to record a subset of probed events;
    unknown names are dropped, and status reports the selection."""
    harness.read_hello()
    resp = harness.command('start',
                           args={'pid': target_pid, 'duration': 1,
                                 'events': ['cycles', 'bogus-event']},
                           timeout=60)
    assert resp['ok'] is True, resp
    assert resp['events'] == ['cycles']

    status = harness.command('status')
    assert status['events'] == ['cycles']
    assert status['capabilities']['record_events'] == [
        'cycles', 'instructions']

    assert harness.command('stop')['ok'] is True

    # A start without events resets to all probed events
    resp = harness.command('start', args={'pid': target_pid, 'duration': 1},
                           timeout=60)
    assert resp['ok'] is True, resp
    assert resp['events'] == ['cycles', 'instructions']
    assert harness.command('stop')['ok'] is True


def test_round_mode_fallback(shim_dir, tmp_path, target_pid):
    """When pipe mode is unavailable (old perf), the agent falls back to
    per-round collection and still produces valid data frames."""
    h = AgentHarness(shim_dir, tmp_path, env={'PERF_SHIM_NO_PIPE': '1'})
    try:
        h.read_hello()
        resp = h.command('start',
                         args={'pid': target_pid, 'frequency': 99,
                               'duration': 1},
                         timeout=60)
        assert resp['ok'] is True, resp
        assert resp['mode'] == 'rounds'

        flag, payload = h.wait_frame({FLAG_DATA_RAW, FLAG_DATA_ZSTD},
                                     timeout=30)
        if flag == FLAG_DATA_ZSTD:
            payload = zstandard.ZstdDecompressor().decompress(
                payload, max_output_size=1 << 20)
        text = payload.decode()
        assert 'hot_function' in text
        assert '### PERF_STAT ###' in text

        assert h.command('status')['capabilities']['pipe_mode'] is False
        assert h.command('stop')['ok'] is True
    finally:
        h.close()


# ---------------------------------------------------------------------------
# Metrics stream
# ---------------------------------------------------------------------------

def test_metrics_frames(harness):
    harness.read_hello()
    _, payload = harness.wait_frame(
        {FLAG_METRICS}, timeout=15,
        pred=lambda p: json.loads(p).get('type') == 'system')
    metrics = json.loads(payload)
    assert metrics['ts'] > 0
    assert 'cpu' in metrics


def test_configure_metrics(harness):
    harness.read_hello()
    resp = harness.command('configure_metrics', args={'interval': 5})
    assert resp['ok'] is True


# ---------------------------------------------------------------------------
# Reconnect behavior
# ---------------------------------------------------------------------------

def test_reconnects_after_disconnect(harness):
    harness.read_hello()
    assert harness.command('ping')['ok'] is True

    harness.conn.close()
    harness.accept()  # --server mode reconnects on its own

    hello = harness.read_hello()
    assert hello['type'] == 'hello'
    assert harness.command('ping')['ok'] is True


def test_reconnect_requires_authenticating_again(shim_dir, tmp_path):
    """Authentication is per-session state.

    Both run modes loop over sessions, so an `authed` flag that survived
    teardown would let one authenticated peer authorize whoever connected
    next — which, in --listen mode, is anyone.
    """
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        h.read_hello()
        assert h.authenticate('s3cret')['ok'] is True
        assert h.command('ping')['ok'] is True

        h.conn.close()
        h.accept()
        h.read_hello()

        assert h.command('ping')['error'] == 'unauthenticated'
        assert h.authenticate('s3cret')['ok'] is True
        assert h.command('ping')['ok'] is True
    finally:
        h.close()


# ---------------------------------------------------------------------------
# Headless --output mode
# ---------------------------------------------------------------------------

def test_output_mode_multi_round(shim_dir, tmp_path, target_pid):
    out = tmp_path / 'capture.txt'
    env = dict(os.environ)
    env['PATH'] = f'{shim_dir}:{env["PATH"]}'
    proc = subprocess.run(
        [AGENT_BIN, '--output', str(out), '--pid', str(target_pid),
         '--rounds', '2', '--duration', '1'],
        env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    text = out.read_text()
    assert 'hot_function' in text
    # One PERF_STAT section per round — the multi-round marker layout
    # split_perf_data must handle (phase-1c regression)
    assert text.count('### PERF_STAT ###') == 2


def test_failed_auth_backs_off_instead_of_spinning(shim_dir, tmp_path):
    """A --server agent whose session ends unauthenticated must back off.

    The TCP connect succeeds every time here — only the auth fails — so the
    connect loop's own backoff never engages. Without a separate wait the
    agent reconnects about once a second, per device, indefinitely.

    Found on hardware, not here: the first version of the fix incremented the
    delay but nothing ever slept on it, and still produced 90 connections in
    90 seconds.

    Each session is ended with three wrong codes, which trips the failure cap
    and makes the agent close from its own side — deterministic, and much
    faster than waiting out the 30s auth deadline.
    """
    def burn_session(h):
        """Fail auth until the agent drops us."""
        h.read_hello()
        for _ in range(3):
            try:
                h.authenticate('wrong', timeout=10)
            except (AssertionError, ConnectionError, OSError):
                break

    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        burn_session(h)

        stamps = []
        for _ in range(3):
            try:
                h.listener.settimeout(20)
                conn, _ = h.listener.accept()
            except (TimeoutError, socket.timeout, OSError):
                break
            stamps.append(time.monotonic())
            h._attach(conn)
            burn_session(h)

        assert len(stamps) >= 3, (
            f'expected the agent to keep reconnecting, got {len(stamps)}')

        gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
        # 1s, then 2s, then 4s... A spin would give three gaps under a second.
        assert gaps[-1] > gaps[0], f'delays are not increasing: {gaps}'
        assert gaps[-1] >= 1.5, f'no meaningful backoff between retries: {gaps}'
    finally:
        h.close()
