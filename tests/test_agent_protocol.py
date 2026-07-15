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
import socket
import struct
import subprocess
import threading
import time
import uuid

import pytest
import zstandard

from conftest import AGENT_BIN

pytestmark = pytest.mark.skipif(
    not os.access(AGENT_BIN, os.X_OK),
    reason='agent binary not built (run `make -C agent-c`)')

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
    if out:
        with open(out, 'w') as f:
            f.write('FAKEPERFDATA')
    time.sleep(0.2)
    sys.exit(0)

if sub == 'script':
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
    """Fake server end of the wire protocol driving a real agent
    subprocess in --server mode."""

    def __init__(self, shim_dir, tmp_path, agent_args=(), env=None):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(15)
        port = self.listener.getsockname()[1]

        full_env = dict(os.environ)
        full_env['PATH'] = f'{shim_dir}:{full_env["PATH"]}'
        full_env['PERF_SHIM_LOG'] = str(tmp_path / 'perf-shim.log')
        full_env.update(env or {})
        self.shim_log = full_env['PERF_SHIM_LOG']

        self.proc = subprocess.Popen(
            [AGENT_BIN, '--server', '127.0.0.1', '--port', str(port),
             *agent_args],
            env=full_env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

        self.conn = None
        self.frames = None
        self._reader = None
        self.accept()

    def accept(self):
        """(Re-)accept the agent's connection and restart the reader.
        A fresh queue per connection: the previous reader's disconnect
        sentinel must not leak into the new session."""
        self.conn, _ = self.listener.accept()
        self.frames = queue.Queue()
        self.conn.settimeout(30)
        self._reader = threading.Thread(
            target=self._read_loop, args=(self.conn, self.frames),
            daemon=True)
        self._reader.start()

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
    assert 'token' not in hello


def test_hello_with_token(shim_dir, tmp_path):
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        assert h.read_hello()['token'] == 's3cret'
    finally:
        h.close()


def test_hello_token_from_env(shim_dir, tmp_path):
    h = AgentHarness(shim_dir, tmp_path, env={'PERFLENS_TOKEN': 'envtok'})
    try:
        assert h.read_hello()['token'] == 'envtok'
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
