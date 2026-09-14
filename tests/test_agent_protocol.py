"""C-agent protocol tests: the real agent binary against a fake framing
server, with `perf` replaced by a shim on PATH.

Covers the wire framing (5-byte header, flags 0-4), the hello handshake
(incl. --token), the command protocol (ping/status/start/pause/resume/
stop, unknown commands, the start-while-paused regression), the data
path (zstd frames that decompress to perf script output + PERF_STAT
section), health-metrics frames, reconnect-after-disconnect, and the
headless --output mode (multi-round markers).

The shim records how it was run (pid, nice level, LC_ALL, inherited
descriptors) and can be made slow, stuck, chatty or PMU-less through
PERF_SHIM_* environment variables, so the tests near the end cover the
agent's handling of perf itself: chunk sizing, stat coverage, priority,
locale, close-on-exec, a child that ignores SIGTERM, a server that stops
reading, and what a peer may put on the wire.
"""

import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
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

PERF_SHIM_TEMPLATE = r'''#!/usr/bin/env python3
"""Fake `perf` for agent tests. Supports --version / stat / record /
script; rejects events outside SUPPORTED and call-graph methods
other than fp, like a restricted kernel would."""
import os, sys, time

SUPPORTED = %(supported)r
SCRIPT_OUTPUT = %(script_output)r
PIPE_SCRIPT_OUTPUT = %(pipe_script_output)r

args = sys.argv[1:]

def nice_level():
    with open('/proc/self/stat') as f:
        return int(f.read().rsplit(')', 1)[1].split()[16])

def inherited_fds():
    out = []
    for name in sorted(os.listdir('/proc/self/fd'), key=int):
        try:
            out.append('%%s=%%s' %% (name, os.readlink('/proc/self/fd/' + name)))
        except OSError:
            pass
    return ' '.join(out)

log = os.environ.get('PERF_SHIM_LOG')
def shim_log(line):
    if log:
        with open(log, 'a') as f:
            f.write(line + '\n')

# Every invocation records how it was run, so tests can assert on the
# environment perf really got: nice level, locale, inherited descriptors.
env_note = ' [t=%%.6f pid=%%d nice=%%d LC_ALL=%%s fds:%%s]' %% (
    time.monotonic(), os.getpid(), nice_level(),
    os.environ.get('LC_ALL', '<unset>'), inherited_fds())
shim_log(' '.join(args) + env_note)

def spawn_workload(seconds):
    # perf record runs its workload as a child; so does the shim, so tests
    # can check the workload dies with the record it belongs to. Through
    # the interpreter rather than `sleep`, which the perf-outside-PATH
    # tests keep off PATH on purpose.
    import subprocess
    p = subprocess.Popen([sys.executable, '-c',
                          'import time; time.sleep(%%s)' %% seconds])
    shim_log('workload pid=%%d' %% p.pid)
    return p

def opt(name):
    return args[args.index(name) + 1] if name in args else None

sub = args[0] if args else ''

if sub == '--version':
    print('perf version 6.99.shim')
    sys.exit(0)

# Names perf knows. Asking for one it cannot count prints `<not supported>`
# (exit 0); asking for a name it has never heard of aborts the run.
KNOWN = ('cycles', 'instructions', 'cache-misses', 'cache-references',
         'branch-misses', 'branch-instructions', 'page-faults',
         'context-switches', 'cpu-migrations', 'cpu-clock', 'task-clock')

def probe_delay():
    # PERF_SHIM_SLOW_PROBE: seconds every probe step takes, for tests that
    # need a probe to still be running when the next command arrives.
    slow = os.environ.get('PERF_SHIM_SLOW_PROBE')
    if slow:
        time.sleep(float(slow))

if sub == 'stat':
    evs = [e for e in (opt('-e') or '').split(',') if e]
    for ev in evs:
        if ev not in KNOWN:
            sys.stderr.write("event syntax error: '%%s'\n" %% ev)
            sys.exit(1)
    if '-x' in args:
        if os.environ.get('PERF_SHIM_NO_CSV'):
            sys.stderr.write("stat: unknown option -x\n")
            sys.exit(1)
        probe_delay()
        time.sleep(0.05)
        for ev in evs:
            if ev in SUPPORTED or ev == 'task-clock':
                sys.stderr.write('1234567,,%%s,1000000,100.00,,\n' %% ev)
            else:
                sys.stderr.write('<not supported>,,%%s,0,100.00,,\n' %% ev)
        sys.exit(0)
    for ev in evs:
        if ev not in SUPPORTED and ev != 'task-clock':
            sys.stderr.write("event syntax error: '%%s'\n" %% ev)
            sys.exit(1)
    probe_delay()
    # PERF_SHIM_STAT_SLEEP: honour `-- sleep N` up to this many seconds, so
    # a stat round takes as long as a real one (the probes ask for 1 s).
    want = float(args[args.index('sleep') + 1]) if 'sleep' in args else 0.05
    time.sleep(min(want, float(os.environ.get('PERF_SHIM_STAT_SLEEP', '0.05'))))
    first = (opt('-e') or 'cycles').split(',')[0]
    # Plain digits, as perf prints under LC_ALL=C (the agent sets it):
    # a grouped '1,234,567' here hid a parser regression from the suite.
    counter = ("     <not supported>      %%s\n" %% first
               if os.environ.get('PERF_SHIM_STAT_UNSUPPORTED')
               else "           1234567      %%s\n" %% first)
    sys.stderr.write(
        " Performance counter stats for process id '%%s':\n\n"
        "%%s"
        "            234567      instructions\n"
        "                12      page-faults\n"
        "              2.00 msec task-clock\n\n"
        "       0.100 seconds time elapsed\n" %% (opt('-p') or '?', counter))
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
    if out and 'perflens-probe' in out or (out == '-' and 'sleep' in args):
        probe_delay()
    if os.environ.get('PERF_SHIM_IGNORE_TERM'):
        # A perf stuck in uninterruptible I/O: SIGTERM does nothing.
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    workload = None
    # A collection round writes perflens-data-*; probes write perflens-probe-*.
    stuck = os.environ.get('PERF_SHIM_IGNORE_TERM') and out and 'perflens-data' in out
    if 'sleep' in args:
        # Real perf would run the requested `sleep N`; the shim keeps it to
        # 0.2 s so a full probe stays fast, while still being a real child.
        # A stuck record keeps its workload around too, so a test can check
        # the whole process group goes when the agent gives up on it.
        workload = spawn_workload(60 if stuck else 0.2)
    if out == '-':
        if os.environ.get('PERF_SHIM_NO_PIPE'):
            sys.stderr.write('pipe output not supported\n')
            sys.exit(1)
        try:
            if workload is not None:
                # probe: bounded run
                sys.stdout.write('FAKEPERFDATA\n')
                sys.stdout.flush()
                workload.wait()
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
    if stuck:
        time.sleep(60)   # SIGTERM is ignored above; only SIGKILL ends this
    if workload is not None:
        workload.wait()
    else:
        time.sleep(0.2)
    sys.exit(0)

if sub == 'script':
    if opt('-i') == '-':
        if os.environ.get('PERF_SHIM_NO_PIPE'):
            sys.stderr.write('cannot read from pipe\n')
            sys.exit(1)
        # PERF_SHIM_BIG_PIPE: a busy target -- megabytes of script text per
        # record line, so a chunk crosses the size limit within seconds.
        # PERF_SHIM_NOISY_PIPE: random addresses in every frame, so the text
        # does not compress away and the wire actually carries bytes.
        block = PIPE_SCRIPT_OUTPUT * int(os.environ.get('PERF_SHIM_BIG_PIPE', '1'))
        if os.environ.get('PERF_SHIM_NOISY_PIPE'):
            block = ''.join(PIPE_SCRIPT_OUTPUT.replace('401136', os.urandom(8).hex())
                                              .replace('401300', os.urandom(8).hex())
                            for _ in range(int(os.environ.get('PERF_SHIM_BIG_PIPE', '1'))))
        try:
            for _line in sys.stdin:
                sys.stdout.write(block)
                sys.stdout.flush()
        except (BrokenPipeError, IOError):
            pass
        sys.exit(0)
    # PERF_SHIM_SCRIPT_SLEEP: how long symbolizing a round's file takes.
    if 'perflens-data' in (opt('-i') or ''):
        time.sleep(float(os.environ.get('PERF_SHIM_SCRIPT_SLEEP', '0')))
    sys.stdout.write(SCRIPT_OUTPUT)
    sys.exit(0)

sys.stderr.write('shim: unhandled perf invocation: %%r\n' %% args)
sys.exit(1)
'''


def render_shim(supported=SUPPORTED_EVENTS, script_output=SCRIPT_OUTPUT,
                pipe_script_output=None):
    """A perf shim. `script_output` is what `perf script -i FILE` prints,
    `pipe_script_output` what `perf script -i -` prints (defaults to the
    same), which is how an old perf that drops call chains only through a
    pipe is modelled."""
    return PERF_SHIM_TEMPLATE % {
        'supported': tuple(supported),
        'script_output': script_output,
        'pipe_script_output': (pipe_script_output if pipe_script_output is not None
                               else script_output)}


PERF_SHIM = render_shim()


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
                 mode='server', log_path=None, rcvbuf=None):
        self.mode = mode
        self.listener = None
        self.conn = None
        self.frames = None
        self._reader = None
        self.rcvbuf = rcvbuf
        # Cleared to make the reader stop draining the socket: a server that
        # is alive but not reading, which is the case send timeouts exist for.
        self._reading = threading.Event()
        self._reading.set()

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
            if rcvbuf:
                # Accepted sockets inherit it: a tiny window closes fast.
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
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
        self.responses = {}
        self.conn.settimeout(30)
        self._reader = threading.Thread(
            target=self._read_loop, args=(self.conn, self.frames, self._reading),
            daemon=True)
        self._reader.start()

    def pause_reading(self):
        """Stop draining the socket (the agent's sends will block)."""
        self._reading.clear()

    def resume_reading(self):
        self._reading.set()

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
    def _read_loop(conn, frames, reading):
        try:
            while True:
                reading.wait()
                header = recv_exactly(conn, 5)
                length, flag = struct.unpack('>IB', header)
                payload = recv_exactly(conn, length) if length else b''
                frames.put((flag, payload))
        except (ConnectionError, OSError) as e:
            # The sentinel carries the reason, so a test that sees the
            # agent go away says whether it was EOF, a reset, or a timeout.
            frames.put((None, repr(e).encode()))

    def wait_frame(self, flags, timeout=30, pred=None):
        """Next frame whose flag is in `flags` (and matches pred). Other
        frames are discarded -- except command responses, which are kept by
        id for a later wait_response(): a cancelled start is answered
        before the stop that cancelled it."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f'timed out waiting for flags {flags}'
            flag, payload = self.frames.get(timeout=remaining)
            assert flag is not None, f'agent disconnected: {payload.decode()}'
            if flag in flags and (pred is None or pred(payload)):
                return flag, payload
            if flag == FLAG_CMD_RESPONSE:
                try:
                    rid = json.loads(payload).get('id')
                except ValueError:
                    rid = None
                if rid:
                    self.responses[rid] = payload

    def send_command(self, cmd, **kwargs):
        """Send a command frame without waiting; returns its id."""
        cmd_id = uuid.uuid4().hex[:12]
        payload = json.dumps({'cmd': cmd, 'id': cmd_id, **kwargs}).encode()
        self.conn.sendall(struct.pack('>IB', len(payload), FLAG_CMD_REQUEST)
                          + payload)
        return cmd_id

    def wait_response(self, cmd_id, timeout=30):
        kept = self.responses.pop(cmd_id, None)
        if kept is not None:
            return json.loads(kept)
        _, resp = self.wait_frame(
            {FLAG_CMD_RESPONSE}, timeout=timeout,
            pred=lambda p: json.loads(p).get('id') == cmd_id)
        return json.loads(resp)

    def command(self, cmd, timeout=30, **kwargs):
        """Send a command frame, return the matching JSON response."""
        return self.wait_response(self.send_command(cmd, **kwargs), timeout)

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
# perf outside PATH
#
# Some targets install perf under a vendor prefix that is not on PATH. The
# agent takes its path from --perf / PERFLENS_PERF at startup, or from the
# server at runtime via verify_perf {perf}.
# ---------------------------------------------------------------------------

@pytest.fixture()
def offpath(tmp_path, shim_dir):
    """(perf, PATH): a perf shim under a prefix, and a PATH with no perf on it
    at all -- only the python3 the shim itself runs on."""
    prefix = tmp_path / 'vendor' / 'perf-4.4' / 'bin'
    prefix.mkdir(parents=True)
    perf = prefix / 'perf'
    shutil.copy(shim_dir / 'perf', perf)
    perf.chmod(0o755)
    bindir = tmp_path / 'bin-without-perf'
    bindir.mkdir()
    (bindir / 'python3').symlink_to(sys.executable)
    return str(perf), str(bindir)


def test_perf_flag_runs_a_perf_outside_path(shim_dir, tmp_path, offpath,
                                            target_pid):
    perf, path = offpath
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--perf', perf],
                     env={'PATH': path})
    try:
        hello = h.read_hello()
        assert hello['platform']['perf_version'].startswith('perf version 6.99')
        # The hello goes out before authentication; install paths stay out.
        assert perf not in json.dumps(hello)
        assert h.command('status')['platform']['perf_path'] == perf

        resp = h.command('start', args={'pid': target_pid, 'frequency': 99,
                                        'duration': 1}, timeout=60)
        assert resp['ok'] is True, resp
        assert resp['events'] == ['cycles', 'instructions']
        h.wait_frame({FLAG_DATA_RAW, FLAG_DATA_ZSTD}, timeout=30)
        assert h.command('stop')['ok'] is True
    finally:
        h.close()


def test_perf_env_var_is_the_same_as_the_flag(shim_dir, tmp_path, offpath):
    perf, path = offpath
    h = AgentHarness(shim_dir, tmp_path,
                     env={'PATH': path, 'PERFLENS_PERF': perf})
    try:
        hello = h.read_hello()
        assert hello['platform']['perf_version'].startswith('perf version 6.99')
        assert h.command('status')['platform']['perf_path'] == perf
    finally:
        h.close()


def test_missing_perf_is_reported_with_how_to_fix_it(shim_dir, tmp_path,
                                                    offpath):
    _perf, path = offpath
    h = AgentHarness(shim_dir, tmp_path, env={'PATH': path})
    try:
        assert h.read_hello()['platform']['perf_version'] == 'unknown'
        resp = h.command('verify_perf')
        assert resp['available'] is False
        assert resp['path'] == 'perf'
        with open(h.log_path) as f:
            assert '--perf' in f.read()
    finally:
        h.close()


def test_verify_perf_adopts_a_path_at_runtime(shim_dir, tmp_path, offpath,
                                              target_pid):
    """The wizard's route: an agent that cannot find perf is pointed at one
    and probes with it, with no restart."""
    perf, path = offpath
    h = AgentHarness(shim_dir, tmp_path, env={'PATH': path})
    try:
        h.read_hello()
        resp = h.command('verify_perf', args={'perf': perf})
        assert resp['available'] is True, resp
        assert resp['path'] == perf
        assert resp['version'].startswith('perf version 6.99')

        platform = h.command('status')['platform']
        assert platform['perf_path'] == perf
        assert platform['perf_version'].startswith('perf version 6.99')

        probe = h.command('reprobe', args={'pid': target_pid}, timeout=60)
        assert probe['ok'] is True, probe
        assert probe['record_events'] == ['cycles', 'instructions']
    finally:
        h.close()


@pytest.mark.parametrize('candidate,reason', [
    ('vendor/perf', 'not an absolute path'),
    ('/nonexistent/perf-4.4/bin/perf', 'No such file'),
    (shutil.which('true'), 'does not identify as perf'),
])
def test_verify_perf_rejects_what_is_not_perf(shim_dir, tmp_path, offpath,
                                              candidate, reason):
    """A peer chooses this binary over the wire and it then runs for the life
    of the agent, so anything that is not perf is refused -- and the perf
    already in use stays."""
    perf, path = offpath
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--perf', perf],
                     env={'PATH': path})
    try:
        h.read_hello()
        resp = h.command('verify_perf', args={'perf': candidate})
        assert resp['available'] is False
        assert reason in resp['error']
        assert resp['path'] == perf
        assert h.command('status')['platform']['perf_path'] == perf
    finally:
        h.close()


def test_verify_perf_will_not_swap_perf_mid_collection(harness, offpath,
                                                       target_pid):
    perf, _path = offpath
    harness.read_hello()
    resp = harness.command('start', args={'pid': target_pid, 'frequency': 99,
                                          'duration': 1}, timeout=60)
    assert resp['ok'] is True, resp
    resp = harness.command('verify_perf', args={'perf': perf})
    assert resp['ok'] is False
    assert 'stop first' in resp['error']
    assert harness.command('status')['platform']['perf_path'] == 'perf'
    assert harness.command('stop')['ok'] is True


def test_a_bad_perf_flag_fails_at_startup():
    """Rather than as "no perf record events" after a twenty-second probe."""
    r = subprocess.run([AGENT_BIN, '--listen', '--bind', '127.0.0.1',
                        '--port', '0', '--perf', '/nonexistent/perf'],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 1
    assert '/nonexistent/perf' in r.stderr


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
    # Unchanged from before software-event fallback existed: the PMU offers
    # record events here, so cpu-clock/task-clock are never probed at all.
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


# ---------------------------------------------------------------------------
# PMU-less targets
# ---------------------------------------------------------------------------

# What a device with no wired-up PMU offers. Measured on a big-endian ARMv7
# target (armv7b, kernel 4.4): `perf list hw sw` reports software events
# only, and /proc/interrupts carries no arm-pmu line.
PMULESS_EVENTS = ('cpu-clock', 'task-clock', 'page-faults',
                  'context-switches', 'cpu-migrations')


def test_pmuless_target_still_has_a_record_event(tmp_path, target_pid):
    """A target with no PMU must still be profilable.

    Every hardware candidate fails on such a device, and the three software
    events that do survive there are all stat-only — so with only the
    hardware candidates probed, record_events came back empty and `start`
    could never succeed. That is not an exotic configuration: it is most
    embedded hardware.
    """
    d = tmp_path / 'pmuless-shim'
    d.mkdir()
    shim = d / 'perf'
    shim.write_text(render_shim(PMULESS_EVENTS))
    shim.chmod(0o755)

    h = AgentHarness(d, tmp_path)
    try:
        resp = h.command('start', args={'pid': target_pid})
        assert resp['ok'] is True, resp
        assert resp['events'], 'no record events on a PMU-less target'
        assert 'cpu-clock' in resp['events']
        # The call-graph probe records the event the target can actually
        # record. It asked for `-e cycles` unconditionally, which here only
        # ever worked because some perfs fall back to cpu-clock themselves.
        assert resp['callgraph'] == 'fp'

        caps = h.command('status')['capabilities']
        assert 'cpu-clock' in caps['record_events']
        for hw in ('cycles', 'instructions', 'cache-misses'):
            assert hw not in caps['record_events']
        # The stat-only survivors must not be mistaken for record events.
        for so in ('page-faults', 'context-switches', 'cpu-migrations'):
            assert so not in caps['record_events']
    finally:
        h.close()


# A hybrid CPU names every event per PMU in `perf script` output.
HYBRID_SCRIPT_OUTPUT = SCRIPT_OUTPUT.replace(' cycles: ', ' cpu_core/cycles/: ')

# Old perf through a pipe: samples arrive with their ip on the header line and
# no call-chain frames under them.
CHAINLESS_SCRIPT_OUTPUT = (
    'myapp  1234/1234  100.000100: 250000 cycles:   401136 hot_function '
    '(/usr/bin/myapp)\n'
    'myapp  1234/1235  100.000200: 250000 cycles:   401300 worker '
    '(/usr/bin/myapp)\n'
)


@pytest.mark.parametrize('file_output,pipe_output,callgraph,mode', [
    # The check used to count samples by matching "cycles:", which a
    # PMU-qualified name never contains -- so pipe mode was refused on every
    # hybrid x86 machine.
    (HYBRID_SCRIPT_OUTPUT, HYBRID_SCRIPT_OUTPUT, 'fp', 'continuous'),
    # perf 4.4: ~10 frames per sample through a file, one leaf frame each
    # through `record -o - | script -i -`. The call-graph method is real;
    # only pipe mode loses it, so rounds it is.
    (SCRIPT_OUTPUT, CHAINLESS_SCRIPT_OUTPUT, 'fp', 'rounds'),
    # A perf that never prints a stack, file or pipe. The call-graph probe
    # used to accept any non-empty output and report `fp` here; now no
    # method is reported, and a flat profile through the pipe is correct.
    (CHAINLESS_SCRIPT_OUTPUT, CHAINLESS_SCRIPT_OUTPUT, '', 'continuous'),
], ids=['pmu-qualified-names', 'chains-dropped-in-pipe', 'no-chains-at-all'])
def test_pipe_mode_requires_call_chains(tmp_path, target_pid, file_output,
                                        pipe_output, callgraph, mode):
    d = tmp_path / 'pipe-shim'
    d.mkdir()
    shim = d / 'perf'
    shim.write_text(render_shim(script_output=file_output,
                                pipe_script_output=pipe_output))
    shim.chmod(0o755)

    h = AgentHarness(d, tmp_path)
    try:
        resp = h.command('start', args={'pid': target_pid, 'frequency': 99,
                                        'duration': 1}, timeout=60)
        assert resp['ok'] is True, resp
        assert resp['callgraph'] == callgraph
        assert resp['mode'] == mode
    finally:
        h.close()


# ---------------------------------------------------------------------------
# The data path: chunk sizing and perf stat coverage
# ---------------------------------------------------------------------------

def decode_frame(flag, payload, cap=1 << 27):
    if flag == FLAG_DATA_ZSTD:
        payload = zstandard.ZstdDecompressor().decompress(
            payload, max_output_size=cap)
    return payload.decode()


def test_large_intervals_flush_by_size(shim_dir, tmp_path, target_pid):
    """A target producing more than the soft limit inside one interval ships
    several chunks, each cut at a sample boundary.

    The 64 MB hard cap used to be the only limit, and hitting it dropped the
    whole interval: the sticky sink error made the flush skip the send, and
    nothing logged it. Measured at ~3.4 MB/s on an 8-core target, so any
    interval over ~19 s -- the UI offers up to 300 -- lost every chunk.
    """
    h = AgentHarness(shim_dir, tmp_path, env={'PERF_SHIM_BIG_PIPE': '8192'})
    try:
        h.read_hello()
        resp = h.command('start', args={'pid': target_pid, 'duration': 60},
                         timeout=60)
        assert resp['ok'] is True, resp
        assert resp['mode'] == 'continuous'
        sizes = []
        for _ in range(2):
            flag, payload = h.wait_frame({FLAG_DATA_RAW, FLAG_DATA_ZSTD},
                                         timeout=60)
            text = decode_frame(flag, payload)
            # Call-chain output: samples end with a blank line, and a chunk
            # ends with a whole sample (before any stat sections it carries).
            samples = text.split('\n### PERF_STAT ###\n', 1)[0]
            assert samples.endswith('\n\n'), samples[-200:]
            sizes.append(len(samples))
        assert h.command('stop')['ok'] is True
        soft = 16 * 1024 * 1024
        assert all(soft <= n <= soft + (1 << 20) for n in sizes), sizes
    finally:
        h.close()


def test_perf_stat_covers_every_interval(shim_dir, tmp_path, target_pid):
    """Stat rounds run back to back, and each completed round rides the
    next chunk.

    A round used to start only after the previous result had been
    *attached* to a chunk, one interval later -- so with rounds as long as
    the interval, every other interval went uncounted and the totals were
    about half the truth. The shim's stat is made to take the requested
    second so the timing matches a real perf.
    """
    h = AgentHarness(shim_dir, tmp_path, env={'PERF_SHIM_STAT_SLEEP': '1'})
    try:
        h.read_hello()
        resp = h.command('start', args={'pid': target_pid, 'duration': 1},
                         timeout=60)
        assert resp['ok'] is True, resp
        texts = []
        for _ in range(6):
            flag, payload = h.wait_frame({FLAG_DATA_RAW, FLAG_DATA_ZSTD},
                                         timeout=30)
            texts.append(decode_frame(flag, payload))
        assert h.command('stop')['ok'] is True
        # The first chunk may precede the first completed round; after that
        # every chunk carries one. The old cadence gave 2 of these 5.
        counts = [t.count('### PERF_STAT ###') for t in texts]
        assert sum(1 for c in counts[1:] if c >= 1) >= 4, counts
        # And the server-side parser reads what perf printed under the C
        # locale (plain digits): a regression here dropped every counter
        # longer than three digits on real hardware.
        from perflens.parser import parse_perf_stat, split_perf_data
        parsed = [parse_perf_stat(split_perf_data(t)[1])
                  for t in texts if '### PERF_STAT ###' in t]
        assert parsed
        for stat in parsed:
            assert stat['cycles']['value'] % 1234567 == 0 and stat['cycles']['value'] > 0, stat
            assert stat['instructions']['value'] % 234567 == 0, stat
            assert stat['task-clock']['value'] >= 2.0, stat
        with open(h.shim_log) as f:
            rounds = sum(1 for line in f
                         if line.startswith('stat ') and 'task-clock' in line)
        assert rounds >= 4, rounds
    finally:
        h.close()


# ---------------------------------------------------------------------------
# How perf itself is run: priority, locale, descriptors, temp files
# ---------------------------------------------------------------------------

def shim_lines(h):
    with open(h.shim_log) as f:
        return f.read().splitlines()


def test_perf_script_runs_niced_in_every_mode(shim_dir, tmp_path, target_pid):
    """perf script is the CPU-heavy symbolizer; it yields to the workload it
    measures. Continuous mode always niced it; rounds mode -- the fallback
    for the weakest, single-core targets -- ran it at normal priority."""
    for env, mode in (({}, 'continuous'), ({'PERF_SHIM_NO_PIPE': '1'}, 'rounds')):
        h = AgentHarness(shim_dir, tmp_path, env=env)
        try:
            h.read_hello()
            resp = h.command('start', args={'pid': target_pid, 'duration': 1},
                             timeout=60)
            assert resp['ok'] is True, resp
            assert resp['mode'] == mode
            h.wait_frame({FLAG_DATA_RAW, FLAG_DATA_ZSTD}, timeout=30)
            assert h.command('stop')['ok'] is True
            scripts = [line for line in shim_lines(h) if line.startswith('script ')
                       and (' -i - ' in line or 'perflens-data' in line)]
            assert scripts, mode
            assert all('nice=5' in line for line in scripts), (mode, scripts)
            records = [line for line in shim_lines(h) if line.startswith('record ')]
            assert all('nice=0' in line for line in records), records
        finally:
            h.close()


def test_perf_children_run_in_the_c_locale(shim_dir, tmp_path):
    """perf stat prints big numbers with the locale's thousands grouping,
    and the server's parser strips ',' only. A device set to de_DE printed
    1.234.567 and the counters parsed as nonsense."""
    h = AgentHarness(shim_dir, tmp_path,
                     env={'LC_ALL': 'de_DE.UTF-8', 'LANGUAGE': 'de'})
    try:
        h.read_hello()
        lines = [line for line in shim_lines(h) if 'LC_ALL=' in line]
        assert lines
        assert all('LC_ALL=C ' in line for line in lines), lines
    finally:
        h.close()


def test_perf_children_inherit_no_sockets(shim_dir, tmp_path, target_pid):
    """Every descriptor is close-on-exec. A perf child that inherited the
    listening socket kept the port bound after the agent died, and one that
    inherited the session socket hid the disconnect from the server."""
    h = AgentHarness(shim_dir, tmp_path)
    try:
        h.read_hello()
        resp = h.command('start', args={'pid': target_pid, 'duration': 1},
                         timeout=60)
        assert resp['ok'] is True, resp
        h.wait_frame({FLAG_DATA_RAW, FLAG_DATA_ZSTD}, timeout=30)
        assert h.command('stop')['ok'] is True
        for line in shim_lines(h):
            if 'fds:' not in line:
                continue
            assert 'socket:' not in line, line
    finally:
        h.close()


def test_temp_files_honour_tmpdir_and_stale_ones_are_swept(shim_dir, tmp_path,
                                                           target_pid):
    tmpdir = tmp_path / 'scratch'
    tmpdir.mkdir()
    stale = tmpdir / 'perflens-data-stale1'
    stale.write_text('x')
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    fresh = tmpdir / 'perflens-data-fresh1'
    fresh.write_text('x')

    h = AgentHarness(shim_dir, tmp_path, env={'TMPDIR': str(tmpdir),
                                              'PERF_SHIM_NO_PIPE': '1'})
    try:
        h.read_hello()
        assert not stale.exists(), 'stale temp file not swept at startup'
        assert fresh.exists(), 'a recent temp file (another agent) was removed'
        resp = h.command('start', args={'pid': target_pid, 'duration': 1},
                         timeout=60)
        assert resp['ok'] is True, resp
        h.wait_frame({FLAG_DATA_RAW, FLAG_DATA_ZSTD}, timeout=30)
        assert h.command('stop')['ok'] is True
        outputs = [line.split(' -o ')[1].split()[0] for line in shim_lines(h)
                   if line.startswith('record ') and ' -o ' in line]
        assert outputs
        assert all(o.startswith(str(tmpdir) + '/perflens-') for o in outputs
                   if o != '-'), outputs
        assert not [p for p in tmpdir.iterdir()
                    if p.name.startswith('perflens-') and p != fresh], \
            'perf.data temp files left behind'
    finally:
        h.close()


def test_log_lines_carry_a_timestamp(harness):
    harness.read_hello()
    line = harness.read_log().splitlines()[0]
    assert re.match(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} \[perflens-agent\] ',
                    line), line


# ---------------------------------------------------------------------------
# Hangs: a child that will not die, a server that will not read
# ---------------------------------------------------------------------------

def test_stop_does_not_wait_on_a_perf_that_ignores_sigterm(shim_dir, tmp_path,
                                                           target_pid):
    """stop escalates to SIGKILL after a grace period, and the kill covers
    the workload perf spawned (`perf record -- sleep N`).

    A perf stuck in uninterruptible I/O used to block the collection thread
    in waitpid() forever; stop joins that thread, so it hung too, and so did
    session teardown. The SIGKILL paths also left one orphaned `sleep` per
    killed round.
    """
    h = AgentHarness(shim_dir, tmp_path, env={'PERF_SHIM_NO_PIPE': '1',
                                              'PERF_SHIM_IGNORE_TERM': '1'})
    try:
        h.read_hello()
        resp = h.command('start', args={'pid': target_pid, 'duration': 30},
                         timeout=60)
        assert resp['ok'] is True, resp
        assert resp['mode'] == 'rounds'

        # Wait for the round's record (the one writing perflens-data-*) and
        # the workload it spawned.
        record_pid = workload_pid = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and workload_pid is None:
            lines = shim_lines(h)
            for i, line in enumerate(lines):
                if line.startswith('record ') and 'perflens-data' in line:
                    record_pid = int(re.search(r'pid=(\d+)', line).group(1))
                    for later in lines[i + 1:]:
                        if later.startswith('workload pid='):
                            workload_pid = int(later.split('=')[1])
                            break
            time.sleep(0.2)
        assert record_pid and workload_pid, shim_lines(h)

        t0 = time.monotonic()
        assert h.command('stop', timeout=30)['ok'] is True
        elapsed = time.monotonic() - t0
        assert elapsed < 10, f'stop took {elapsed:.1f}s'

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            alive = [p for p in (record_pid, workload_pid) if pid_alive(p)]
            if not alive:
                break
            time.sleep(0.1)
        assert not alive, f'still running after stop: {alive}'
    finally:
        h.close()


def pid_alive(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


def test_send_timeout_ends_a_session_the_server_stops_reading(shim_dir, tmp_path,
                                                              target_pid):
    """A server that is alive but not reading never makes send() fail on
    its own -- keepalive only probes an idle connection, and with a chunk
    in flight Linux retransmits for ~15 minutes. The agent held sock_lock
    through the blocked send, so metrics and every command response froze
    with it, and --server mode never reconnected."""
    h = AgentHarness(shim_dir, tmp_path, rcvbuf=4096,
                     env={'PERFLENS_SEND_TIMEOUT_MS': '2000',
                          'PERF_SHIM_BIG_PIPE': '4096',
                          'PERF_SHIM_NOISY_PIPE': '1'})
    try:
        h.read_hello()
        resp = h.command('start', args={'pid': target_pid, 'duration': 1},
                         timeout=60)
        assert resp['ok'] is True, resp
        h.wait_frame({FLAG_DATA_RAW, FLAG_DATA_ZSTD}, timeout=30)

        h.pause_reading()
        # Either the blocked send times out (SO_SNDTIMEO) or the kernel
        # aborts the connection first (TCP_USER_TIMEOUT); the recv thread
        # then reports the peer gone. Both end the session, which is the
        # point -- it used to sit in send() for the retransmit timeout.
        ended = ('send failed', 'Server disconnected')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(m in h.read_log() for m in ended):
                break
            time.sleep(0.5)
        assert any(m in h.read_log() for m in ended), h.read_log()[-2000:]

        # --server mode: it comes straight back.
        h.resume_reading()
        h.listener.settimeout(30)
        h.accept()
        assert h.read_hello()['type'] == 'hello'
    finally:
        h.close()


# ---------------------------------------------------------------------------
# What a peer can put on the wire
# ---------------------------------------------------------------------------

def test_oversized_command_frame_drops_the_connection(shim_dir, tmp_path):
    """Before authenticating, a peer could make the agent allocate 64 MB per
    frame. Command frames are a few hundred bytes; anything past 64 KB is
    not a server."""
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        h.read_hello()
        payload = b'{"cmd":"ping","id":"x","pad":"' + b'a' * (1 << 20) + b'"}'
        h.conn.sendall(struct.pack('>IB', len(payload), FLAG_CMD_REQUEST) + payload)
        flag, _ = h.frames.get(timeout=15)
        assert flag is None, 'agent should have dropped the connection'
    finally:
        h.close()


def raw_command(h, doc, timeout=15):
    """Send a JSON document as a command frame and return the next response."""
    payload = json.dumps(doc).encode()
    h.conn.sendall(struct.pack('>IB', len(payload), FLAG_CMD_REQUEST) + payload)
    _, resp = h.wait_frame({FLAG_CMD_RESPONSE}, timeout=timeout)
    return json.loads(resp)


def test_command_ids_and_args_are_handled_safely(harness):
    harness.read_hello()

    # A quote or backslash in the id used to be echoed raw, before
    # authentication too, producing invalid JSON.
    resp = raw_command(harness, {'cmd': 'ping', 'id': 'a"b\\c'})
    assert resp['ok'] is True
    assert resp['id'] == ''

    # A `pid` outside `args` is not a pid.
    resp = raw_command(harness, {'cmd': 'verify_pid', 'id': 'k1',
                                 'args': {}, 'pid': os.getpid()})
    assert resp['ok'] is False and 'pid required' in resp['error']

    # An args object may not shadow the command, whichever comes first.
    resp = raw_command(harness, {'args': {'cmd': 'frobnicate'},
                                 'cmd': 'ping', 'id': 'k2'})
    assert resp['ok'] is True and resp['id'] == 'k2'

    # A key that only appears as a string value is not a key.
    resp = raw_command(harness, {'cmd': 'verify_pid', 'id': 'k3',
                                 'args': {'note': 'pid'}})
    assert resp['ok'] is False and 'pid required' in resp['error']


def test_start_validates_frequency_and_duration(harness, target_pid):
    harness.read_hello()
    resp = harness.command('start', args={'pid': target_pid, 'duration': 0})
    assert resp['ok'] is False and 'duration' in resp['error']
    resp = harness.command('start', args={'pid': target_pid, 'duration': 301})
    assert resp['ok'] is False and 'duration' in resp['error']
    resp = harness.command('start', args={'pid': target_pid, 'frequency': 0})
    assert resp['ok'] is False and 'frequency' in resp['error']
    resp = harness.command('start', args={'pid': target_pid,
                                          'frequency': 10 ** 7})
    assert resp['ok'] is False and 'frequency' in resp['error']
    resp = harness.command('configure', args={'duration': 0})
    assert resp['ok'] is False and 'duration' in resp['error']
    assert harness.command('status')['state'] == 'idle'


def test_ping_is_not_delayed_by_nagle(harness):
    """A frame used to go out as three send()s -- length, flag, payload --
    and Nagle held the later segments for the peer's delayed ACK, 40 ms on
    Linux. One writev() per frame and TCP_NODELAY."""
    harness.read_hello()
    harness.command('ping')
    samples = []
    for _ in range(20):
        t0 = time.monotonic()
        assert harness.command('ping')['ok'] is True
        samples.append(time.monotonic() - t0)
    samples.sort()
    assert samples[len(samples) // 2] < 0.03, samples


# ---------------------------------------------------------------------------
# verify_perf's functional check
# ---------------------------------------------------------------------------

def test_verify_perf_functional_check_uses_a_countable_event(tmp_path):
    """The check counted `cycles`. On a PMU-less target perf stat exits 0
    and prints `<not supported>` for it, so the perf was reported
    functional when nothing could be sampled."""
    d = tmp_path / 'pmuless-shim'
    d.mkdir()
    shim = d / 'perf'
    shim.write_text(render_shim(PMULESS_EVENTS))
    shim.chmod(0o755)

    h = AgentHarness(d, tmp_path)
    try:
        h.read_hello()
        resp = h.command('verify_perf')
        assert resp['available'] is True and resp['functional'] is True, resp
        checks = [line for line in shim_lines(h) if line.startswith('stat ') and
                  f'-p {h.proc.pid} ' in line]
        assert checks and all('-e cpu-clock ' in line for line in checks), checks
    finally:
        h.close()

    h = AgentHarness(d, tmp_path, env={'PERF_SHIM_STAT_UNSUPPORTED': '1'})
    try:
        h.read_hello()
        resp = h.command('verify_perf')
        assert resp['available'] is True
        assert resp['functional'] is False, resp
        assert 'not supported' in (resp.get('error') or '')
    finally:
        h.close()


# ---------------------------------------------------------------------------
# Probing: batched, cancellable, and not repeated for a new pid
# ---------------------------------------------------------------------------

def perf_invocations(h):
    return [line for line in shim_lines(h)
            if line.split(' ', 1)[0] in ('stat', 'record', 'script')]


def test_probe_is_batched(harness, target_pid):
    """One stat over every candidate, one record over every survivor, one
    call-graph recording that also serves the -F check, and the pipe probe:
    about seven perf runs where there were ~25 and 24 s of sleep."""
    harness.read_hello()
    resp = harness.command('start', args={'pid': target_pid, 'duration': 1},
                           timeout=60)
    assert resp['ok'] is True, resp
    assert resp['events'] == ['cycles', 'instructions']
    assert resp['callgraph'] == 'fp' and resp['mode'] == 'continuous'
    runs = perf_invocations(harness)
    probe = [r for r in runs if ' -o - ' not in r or ' sleep ' in r]
    probe = [r for r in probe if 'perflens-data' not in r]
    assert len(probe) <= 8, probe
    assert sum(1 for r in probe if r.startswith('stat -x')) == 1, probe
    assert harness.command('status')['capabilities']['stat_only_events'] == ['page-faults']
    assert harness.command('stop')['ok'] is True


def test_probe_falls_back_to_per_event_stat_on_an_old_perf(shim_dir, tmp_path,
                                                            target_pid):
    """A perf whose stat rejects `-x`, or aborts on one unknown name, is
    asked about each event on its own -- and finds the same set."""
    h = AgentHarness(shim_dir, tmp_path, env={'PERF_SHIM_NO_CSV': '1'})
    try:
        h.read_hello()
        resp = h.command('start', args={'pid': target_pid, 'duration': 1},
                         timeout=60)
        assert resp['ok'] is True, resp
        assert resp['events'] == ['cycles', 'instructions']
        caps = h.command('status')['capabilities']
        assert caps['stat_only_events'] == ['page-faults']
        stats = [r for r in perf_invocations(h) if r.startswith('stat ')]
        assert sum(1 for r in stats if '-x' not in r) >= 9, stats
        assert h.command('stop')['ok'] is True
    finally:
        h.close()


def test_commands_are_answered_during_a_probe(shim_dir, tmp_path, target_pid):
    """start probes on the collection thread. Meanwhile ping and status
    answer, status says so, and stop cancels the probe within a couple of
    seconds -- it used to wait out every remaining probe step, and a
    disconnect mid-probe left perf running against the target."""
    h = AgentHarness(shim_dir, tmp_path, env={'PERF_SHIM_SLOW_PROBE': '3'})
    try:
        h.read_hello()
        start_id = h.send_command('start', args={'pid': target_pid})
        time.sleep(1.0)
        t0 = time.monotonic()
        assert h.command('ping', timeout=5)['ok'] is True
        assert time.monotonic() - t0 < 1.0
        assert h.command('status', timeout=5)['state'] == 'probing'

        t0 = time.monotonic()
        assert h.command('stop', timeout=15)['ok'] is True
        assert time.monotonic() - t0 < 5.0, 'stop waited for the probe'
        resp = h.wait_response(start_id, timeout=5)
        assert resp['ok'] is False and 'cancelled' in resp['error']
        assert h.command('status')['state'] == 'idle'

        # The probe's perf child is gone, not left running against the target
        pids = [int(re.search(r'pid=(\d+)', r).group(1))
                for r in perf_invocations(h) if 'pid=' in r]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
            time.sleep(0.1)
        assert not [p for p in pids if pid_alive(p)]

        # And a fresh start works afterwards
        resp = h.command('start', args={'pid': target_pid, 'duration': 1},
                         timeout=90)
        assert resp['ok'] is True, resp
        assert h.command('stop')['ok'] is True
    finally:
        h.close()


def test_disconnect_during_a_probe_stops_the_probe(shim_dir, tmp_path,
                                                   target_pid):
    h = AgentHarness(shim_dir, tmp_path, env={'PERF_SHIM_SLOW_PROBE': '3'})
    try:
        h.read_hello()
        h.send_command('start', args={'pid': target_pid})
        time.sleep(1.0)
        pids = [int(re.search(r'pid=(\d+)', r).group(1))
                for r in perf_invocations(h) if 'pid=' in r]
        assert pids
        h.conn.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
            time.sleep(0.1)
        assert not [p for p in pids if pid_alive(p)], 'probe outlived the session'
        h.accept()     # --server mode reconnects
        assert h.read_hello()['type'] == 'hello'
    finally:
        h.close()


def test_switching_process_does_not_reprobe(harness, target_pid):
    """Capabilities depend on the kernel, perf and permissions, not the
    pid. A start on another pid re-checks that it can be recorded (one
    short record) and keeps everything else."""
    harness.read_hello()
    other = subprocess.Popen(['sleep', '300'])
    try:
        resp = harness.command('start', args={'pid': target_pid, 'duration': 1},
                               timeout=60)
        assert resp['ok'] is True, resp
        assert harness.command('stop')['ok'] is True
        before = len(perf_invocations(harness))

        resp = harness.command('start', args={'pid': other.pid, 'duration': 1},
                               timeout=60)
        assert resp['ok'] is True, resp
        assert resp['events'] == ['cycles', 'instructions']
        assert harness.command('status')['pid'] == other.pid
        assert harness.command('stop')['ok'] is True
        new = perf_invocations(harness)[before:]
        probes = [r for r in new if 'perflens-probe' in r or r.startswith('stat -x')]
        assert len(probes) == 1 and f'-p {other.pid} ' in probes[0], probes
    finally:
        other.kill()
        other.wait()


# ---------------------------------------------------------------------------
# --listen: a silent peer no longer holds the slot
# ---------------------------------------------------------------------------

def test_listen_replaces_an_unauthenticated_peer(shim_dir, tmp_path):
    """One connection that never authenticates used to hold the only slot
    for the whole auth window -- a trivial denial of service from the LAN,
    and what an operator saw after a server crashed without a FIN."""
    h = AgentHarness(shim_dir, tmp_path, mode='listen')
    try:
        code = h.pairing_code()
        h.read_hello()          # this peer stays silent
        silent = h.conn
        silent_frames = h.frames

        h.dial()                # a second peer
        assert h.read_hello()['type'] == 'hello'
        assert h.authenticate(code)['ok'] is True
        assert h.command('ping')['ok'] is True

        flag, _ = silent_frames.get(timeout=10)
        assert flag is None, 'the silent peer should have been dropped'
        silent.close()
    finally:
        h.close()


def test_silent_peer_is_dropped_within_the_auth_window(shim_dir, tmp_path):
    h = AgentHarness(shim_dir, tmp_path, agent_args=['--token', 's3cret'])
    try:
        h.read_hello()
        t0 = time.monotonic()
        flag, _ = h.frames.get(timeout=20)
        assert flag is None
        assert 5 <= time.monotonic() - t0 <= 14
    finally:
        h.close()


# ---------------------------------------------------------------------------
# Rounds mode: no dead time; list_processes: one CPU% convention
# ---------------------------------------------------------------------------

def shim_time(line):
    return float(re.search(r't=([\d.]+)', line).group(1))


def test_rounds_overlap_recording_with_symbolization(shim_dir, tmp_path,
                                                     target_pid):
    """Round N+1's perf record starts before round N's perf script does.
    Before, record, script, record, script ran strictly in sequence, so
    nothing was sampled while perf script ran -- seconds to tens of seconds
    per round on the single-core targets that get rounds mode."""
    h = AgentHarness(shim_dir, tmp_path, env={'PERF_SHIM_NO_PIPE': '1',
                                              'PERF_SHIM_SCRIPT_SLEEP': '0.3'})
    try:
        h.read_hello()
        resp = h.command('start', args={'pid': target_pid, 'duration': 1},
                         timeout=60)
        assert resp['ok'] is True and resp['mode'] == 'rounds', resp
        for _ in range(3):
            h.wait_frame({FLAG_DATA_RAW, FLAG_DATA_ZSTD}, timeout=30)
        assert h.command('stop')['ok'] is True

        records = [shim_time(line) for line in shim_lines(h)
                   if line.startswith('record ') and 'perflens-data' in line]
        scripts = [shim_time(line) for line in shim_lines(h)
                   if line.startswith('script ') and 'perflens-data' in line]
        assert len(records) >= 3 and len(scripts) >= 2, (records, scripts)
        # Record N+1 and script N start together. Sequential rounds started
        # record N+1 only after script N had run -- 0.3 s later here.
        assert abs(records[1] - scripts[0]) < 0.1, (records, scripts)
        assert abs(records[2] - scripts[1]) < 0.1, (records, scripts)
    finally:
        h.close()


def test_process_list_and_process_metrics_agree_on_cpu_percent(harness):
    """CPU% is per core (100 = one busy core) in both places. The process
    list used to divide by the ticks of every core, so one saturated core
    on a 24-core host read 4.2 % there and 100 % in the health strip."""
    harness.read_hello()
    busy = subprocess.Popen([sys.executable, '-c',
                             'while True: pass'])
    try:
        time.sleep(0.5)
        resp = harness.command('list_processes', timeout=30)
        assert resp['ok'] is True
        mine = [p for p in resp['processes'] if p['pid'] == busy.pid]
        assert mine, 'busy process missing from the list'
        assert mine[0]['cpu'] >= 60, mine[0]
        assert mine[0]['comm'].startswith('python')
        assert 'while True' in mine[0]['cmdline']

        resp = harness.command('start', args={'pid': busy.pid, 'duration': 1},
                               timeout=60)
        assert resp['ok'] is True, resp
        # The second process frame carries a delta-based cpu_pct
        seen = 0
        while True:
            _, payload = harness.wait_frame(
                {FLAG_METRICS}, timeout=20,
                pred=lambda p: json.loads(p).get('type') == 'process')
            frame = json.loads(payload)
            seen += 1
            if frame.get('cpu_pct') is not None:
                break
            assert seen < 5
        assert frame['pid'] == busy.pid
        assert frame['cpu_pct'] >= 60, frame
        assert harness.command('stop')['ok'] is True
    finally:
        busy.kill()
        busy.wait()


def test_system_metrics_cover_every_core(harness):
    harness.read_hello()
    _, payload = harness.wait_frame(
        {FLAG_METRICS}, timeout=15,
        pred=lambda p: json.loads(p).get('type') == 'system')
    m = json.loads(payload)
    # num_cores is the highest core id + 1: a cpuset with holes (a container
    # limited to some of the host's CPUs) keeps the index meaningful.
    with open('/proc/stat') as f:
        ids = [int(re.match(r'cpu(\d+)', line).group(1)) for line in f
               if re.match(r'cpu\d', line)]
    assert m['cpu']['num_cores'] == max(ids) + 1
    assert len(m['cpu']['per_core']) == m['cpu']['num_cores']
    if 'freq_mhz' in m['cpu']:
        assert len(m['cpu']['freq_mhz']) == m['cpu']['num_cores']
    assert m['mem']['total_kb'] > 0
    assert 0 <= m['mem']['used_pct'] <= 100


# ---------------------------------------------------------------------------
# Self-update: verified before it runs
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_release(tmp_path):
    """A release directory served over loopback HTTP, with an asset that
    records whether it was ever executed."""
    import hashlib
    import http.server
    import threading as _threading

    root = tmp_path / 'release'
    root.mkdir()
    marker = tmp_path / 'ran.marker'
    asset = root / 'perflens-agent-linux-x86_64'
    asset.write_text('#!/bin/sh\ntouch %s\necho "perflens-agent 9.9.9"\n' % marker)
    (root / 'perflens-agent-linux-x86_64.sha256').write_text(
        hashlib.sha256(asset.read_bytes()).hexdigest() + '  perflens-agent-linux-x86_64\n')

    handler = type('Quiet', (http.server.SimpleHTTPRequestHandler,),
                   {'log_message': lambda *a, **k: None})
    srv = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), lambda *a, **k: handler(*a, directory=str(root), **k))
    t = _threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield root, marker, f'http://127.0.0.1:{srv.server_address[1]}'
    finally:
        srv.shutdown()


def run_update(tmp_path, base_url):
    """Run a private copy of the agent with --update against base_url."""
    copy = tmp_path / 'agent-copy'
    shutil.copy(AGENT_BIN, copy)
    copy.chmod(0o755)
    r = subprocess.run([str(copy), '--update'], capture_output=True, text=True,
                       timeout=120, env={**os.environ, 'PERFLENS_UPDATE_URL': base_url})
    return r, copy


@pytest.mark.skipif(os.uname().machine != 'x86_64',
                    reason='the fake release publishes the x86_64 asset')
def test_update_verifies_the_checksum_before_running_anything(tmp_path,
                                                              fake_release):
    root, marker, url = fake_release
    r, copy = run_update(tmp_path, url)
    assert r.returncode == 0, r.stderr
    assert 'Checksum verified' in r.stderr
    assert 'updated' in r.stderr
    assert marker.exists(), 'the verified binary should have been run for --version'
    assert copy.read_bytes().startswith(b'#!/bin/sh')


@pytest.mark.skipif(os.uname().machine != 'x86_64',
                    reason='the fake release publishes the x86_64 asset')
def test_update_refuses_a_tampered_asset_without_executing_it(tmp_path,
                                                              fake_release):
    """The old order ran `--version` on the download before any check, so
    a rejected update had already executed the attacker's file as the
    agent's user."""
    root, marker, url = fake_release
    asset = root / 'perflens-agent-linux-x86_64'
    asset.write_text(asset.read_text() + '# tampered\n')
    r, copy = run_update(tmp_path, url)
    assert r.returncode == 1
    assert 'checksum mismatch' in r.stderr
    assert not marker.exists(), 'the tampered binary was executed'
    assert not copy.read_bytes().startswith(b'#!/bin/sh'), 'the agent was replaced'
    assert not list(tmp_path.glob('agent-copy.update.*')), 'download left behind'


@pytest.mark.skipif(os.uname().machine != 'x86_64',
                    reason='the fake release publishes the x86_64 asset')
def test_update_without_a_sidecar_proceeds_with_a_warning(tmp_path,
                                                          fake_release):
    root, marker, url = fake_release
    (root / 'perflens-agent-linux-x86_64.sha256').unlink()
    r, copy = run_update(tmp_path, url)
    assert r.returncode == 0, r.stderr
    assert 'not verified' in r.stderr
    assert copy.read_bytes().startswith(b'#!/bin/sh')


def test_update_refuses_plaintext_origins_off_loopback(tmp_path):
    copy = tmp_path / 'agent-copy'
    shutil.copy(AGENT_BIN, copy)
    copy.chmod(0o755)
    r = subprocess.run([str(copy), '--update'], capture_output=True, text=True,
                       timeout=30, env={**os.environ,
                                        'PERFLENS_UPDATE_URL': 'http://mirror.example/dl'})
    assert r.returncode == 1
    assert 'plaintext' in r.stderr
