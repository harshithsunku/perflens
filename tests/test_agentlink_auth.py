"""Server-side pairing-code authentication (agentlink).

Driven by a pure-Python fake agent rather than the C binary on purpose: the
whole of test_agent_protocol.py skips when agent-c has not been built, and the
server half of the handshake is the last thing that should be silently
untested. Covers both entry points — connect_to_agent (the server dials a
--listen agent) and handle_inbound_agent (a --server agent dials in) — because
they must stay in step, and the legacy fallback for pre-0.10.0 agents.
"""

import json
import socket
import struct
import tempfile
import threading

import pytest

from perflens import agentlink
from perflens.app import AppContext
from perflens.config import ServerConfig
from perflens.state import MetricsState, ProfilingState

FLAG_CMD_REQUEST = 2
FLAG_CMD_RESPONSE = 3
FLAG_METRICS = 4


def make_ctx(token=None):
    cfg = ServerConfig(sessions_dir=tempfile.mkdtemp(), token=token)
    return AppContext(config=cfg,
                      state=ProfilingState(max_samples=1000),
                      metrics=MetricsState())


def send_frame(sock, obj, flag=FLAG_CMD_RESPONSE):
    payload = json.dumps(obj).encode()
    sock.sendall(struct.pack('!IB', len(payload), flag) + payload)


def read_frame(sock):
    header = b''
    while len(header) < 5:
        chunk = sock.recv(5 - len(header))
        if not chunk:
            return None, None
        header += chunk
    length, flag = struct.unpack('!IB', header)
    payload = b''
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            return None, None
        payload += chunk
    return flag, json.loads(payload) if payload else None


class FakeAgent:
    """A --listen agent: binds a port, waits for the server to dial in.

    `behaviour` decides how it answers the auth command:
      'accept'  — ok, as a 0.10.0+ agent with a matching code
      'reject'  — auth failed, as one with a different code
      'legacy'  — unknown command, as a pre-0.10.0 agent
    """

    def __init__(self, behaviour='accept', code='pair-me',
                 hello_token=None, metrics_first=False):
        self.behaviour = behaviour
        self.code = code
        self.hello_token = hello_token
        self.metrics_first = metrics_first
        self.presented = None
        self.saw_auth = False
        self.error = None

        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.conn = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _hello(self):
        hello = {'type': 'hello', 'version': 1, 'agent': 'perflens',
                 'agent_version': '0.9.0' if self.behaviour == 'legacy'
                                  else '0.10.0',
                 'platform': {'arch': 'x86_64'}}
        if self.behaviour != 'legacy':
            hello['auth'] = 'token'
        if self.hello_token is not None:
            hello['token'] = self.hello_token
        return hello

    def _serve(self):
        try:
            self.listener.settimeout(15)
            self.conn, _ = self.listener.accept()
            self.conn.settimeout(15)
            send_frame(self.conn, self._hello())

            # A pre-gate agent starts streaming metrics immediately, so a
            # flag-4 frame can land before the auth response.
            if self.metrics_first:
                send_frame(self.conn, {'ts': 1, 'type': 'system'},
                           flag=FLAG_METRICS)

            flag, req = read_frame(self.conn)
            if flag is None:
                return
            if req.get('cmd') != 'auth':
                self.error = f'expected auth, got {req.get("cmd")}'
                return

            self.saw_auth = True
            self.presented = (req.get('args') or {}).get('token')
            cid = req.get('id')

            if self.behaviour == 'legacy':
                send_frame(self.conn, {
                    'id': cid, 'ok': False,
                    'error': 'unknown command: auth'})
            elif self.behaviour == 'accept' and self.presented == self.code:
                send_frame(self.conn, {'id': cid, 'ok': True})
            else:
                send_frame(self.conn, {'id': cid, 'ok': False,
                                       'error': 'auth failed'})
        except (OSError, ValueError) as e:      # pragma: no cover - diagnostic
            self.error = str(e)

    def close(self):
        for s in (self.conn, self.listener):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


@pytest.fixture()
def agent():
    made = []

    def _make(**kwargs):
        a = FakeAgent(**kwargs)
        made.append(a)
        return a

    yield _make
    for a in made:
        a.close()


# ---------------------------------------------------------------------------
# connect_to_agent — the server dials a --listen agent
# ---------------------------------------------------------------------------

def test_correct_code_authenticates(agent):
    a = agent(behaviour='accept', code='pair-me')
    session = agentlink.connect_to_agent(make_ctx(), '127.0.0.1', a.port,
                                         token='pair-me')
    try:
        assert a.presented == 'pair-me'
        assert session.hello['agent_version'] == '0.10.0'
    finally:
        session.close()


def test_wrong_code_is_rejected_and_installs_no_session(agent):
    a = agent(behaviour='reject', code='pair-me')
    ctx = make_ctx()
    with pytest.raises(RuntimeError, match='auth failed'):
        agentlink.connect_to_agent(ctx, '127.0.0.1', a.port, token='nope')
    assert ctx.agent.current() is None


def test_server_token_used_when_no_explicit_code(agent):
    """The wizard's per-connection code falls back to the server's --token."""
    a = agent(behaviour='accept', code='from-config')
    session = agentlink.connect_to_agent(
        make_ctx(token='from-config'), '127.0.0.1', a.port)
    try:
        assert a.presented == 'from-config'
    finally:
        session.close()


def test_no_auth_frame_sent_when_nothing_configured(agent):
    """A tokenless server must not send an auth command at all."""
    a = agent(behaviour='accept')
    session = agentlink.connect_to_agent(make_ctx(), '127.0.0.1', a.port)
    try:
        assert a.saw_auth is False
        assert a.presented is None
    finally:
        session.close()


def test_metrics_frame_before_auth_response_is_skipped(agent):
    """Legacy agents stream metrics from the moment they connect. The
    handshake has to step over those rather than mistake one for the reply."""
    a = agent(behaviour='accept', code='pair-me', metrics_first=True)
    session = agentlink.connect_to_agent(make_ctx(), '127.0.0.1', a.port,
                                         token='pair-me')
    try:
        assert a.error is None
        assert session.hello['type'] == 'hello'
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Legacy agents (pre-0.10.0), which answer `unknown command: auth`
# ---------------------------------------------------------------------------

def test_legacy_agent_accepted_on_hello_token(agent):
    a = agent(behaviour='legacy', hello_token='old-secret')
    ctx = make_ctx(token='old-secret')
    session = agentlink.connect_to_agent(ctx, '127.0.0.1', a.port)
    try:
        assert session.hello['agent_version'] == '0.9.0'
        # The hello is served to browsers via GET /api/agent, so the legacy
        # secret must not survive into it.
        assert 'token' not in session.hello
    finally:
        session.close()


def test_legacy_agent_rejected_on_wrong_hello_token(agent):
    a = agent(behaviour='legacy', hello_token='wrong')
    ctx = make_ctx(token='expected')
    with pytest.raises(RuntimeError, match='token mismatch'):
        agentlink.connect_to_agent(ctx, '127.0.0.1', a.port)
    assert ctx.agent.current() is None


# ---------------------------------------------------------------------------
# handle_inbound_agent — a --server agent dials in
# ---------------------------------------------------------------------------

def inbound_pair(ctx, **agent_kwargs):
    """Run handle_inbound_agent against a fake agent over a socketpair-ish
    loopback connection, returning the fake's view."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    result = {}

    def server_side():
        conn, addr = listener.accept()
        agentlink.handle_inbound_agent(ctx, conn, addr)

    t = threading.Thread(target=server_side, daemon=True)
    t.start()

    client = socket.create_connection(('127.0.0.1', port), timeout=10)
    client.settimeout(15)
    behaviour = agent_kwargs.get('behaviour', 'accept')
    code = agent_kwargs.get('code', 'pair-me')
    hello = {'type': 'hello', 'version': 1, 'agent': 'perflens',
             'agent_version': '0.10.0', 'auth': 'token',
             'platform': {'arch': 'x86_64'}}
    send_frame(client, hello)

    try:
        flag, req = read_frame(client)
        if flag is not None and req.get('cmd') == 'auth':
            result['presented'] = (req.get('args') or {}).get('token')
            ok = behaviour == 'accept' and result['presented'] == code
            send_frame(client, {'id': req.get('id'), 'ok': ok,
                                'error': None if ok else 'auth failed'})
    except OSError:
        pass

    t.join(timeout=15)
    listener.close()
    return result, client


def test_inbound_agent_authenticates(agent):
    ctx = make_ctx(token='pair-me')
    result, client = inbound_pair(ctx, behaviour='accept', code='pair-me')
    try:
        assert result.get('presented') == 'pair-me'
        assert ctx.agent.current() is not None
    finally:
        client.close()


def test_inbound_agent_rejected_on_wrong_code(agent):
    ctx = make_ctx(token='expected')
    result, client = inbound_pair(ctx, behaviour='reject', code='expected')
    try:
        assert result.get('presented') == 'expected'
        assert ctx.agent.current() is None
    finally:
        client.close()
