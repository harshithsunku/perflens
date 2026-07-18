"""Agent TCP link: wire framing, the bidirectional AgentSession, and the
single-agent slot.

The wire protocol (5-byte header: uint32 BE length + 1-byte flag) is FROZEN
— it must match the C agent exactly. Everything on the server side of the
socket is fair game.

All socket work runs on plain threads with blocking I/O; the HTTP layer
talks to it only through AppContext.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import uuid
from datetime import datetime

from perflens.parser import parse_perf_script, parse_perf_stat, split_perf_data

# Wire protocol flags (must match agent)
FLAG_DATA_RAW = 0
FLAG_DATA_ZSTD = 1
FLAG_CMD_REQUEST = 2
FLAG_CMD_RESPONSE = 3
FLAG_METRICS = 4

# Cap on a single wire frame. The agent bounds its own payloads at 64 MB;
# anything larger is a corrupt stream or a stray client, and allocating it
# blindly (a garbage header can claim 4 GB) is a trivial DoS.
MAX_FRAME_SIZE = 128 * 1024 * 1024

# In-process zstd (the zstandard wheel ships with the package); the
# external `zstd` binary remains as a fallback for source checkouts run
# without installed dependencies.
try:
    import zstandard as _zstd
except ImportError:
    _zstd = None


def recv_exactly(conn, n):
    """Receive exactly n bytes from a socket."""
    buf = bytearray(n)
    view = memoryview(buf)
    pos = 0
    while pos < n:
        nbytes = conn.recv_into(view[pos:], n - pos)
        if nbytes == 0:
            return None
        pos += nbytes
    return bytes(buf)


def decompress_payload(cfg, payload, comp_flag):
    """Decompress payload based on compression flag. Returns text string."""
    if comp_flag == 0:
        return payload.decode('utf-8', errors='replace')

    if comp_flag == 1:
        if _zstd is not None:
            try:
                # Agent frames are single-shot zstd streams
                raw = _zstd.ZstdDecompressor().decompress(
                    payload, max_output_size=1 << 30)
                return raw.decode('utf-8', errors='replace')
            except _zstd.ZstdError as e:
                print(f"[server] zstd decompress error: {e}", file=sys.stderr)
                return None
        if not cfg.zstd_bin:
            print("[server] WARNING: received zstd data but zstd not available",
                  file=sys.stderr)
            return None
        try:
            r = subprocess.run(
                [cfg.zstd_bin, '-d', '-c'],
                input=payload, capture_output=True, timeout=30,
            )
            if r.returncode == 0:
                return r.stdout.decode('utf-8', errors='replace')
            else:
                print(f"[server] zstd decompress failed: {r.stderr.decode(errors='replace')}",
                      file=sys.stderr)
                return None
        except Exception as e:
            print(f"[server] zstd decompress error: {e}", file=sys.stderr)
            return None

    print(f"[server] WARNING: unknown compression flag {comp_flag}",
          file=sys.stderr)
    return None


def check_agent_token(cfg, hello):
    """Validate the hello token against cfg.token (if configured).

    Returns None when accepted, or an error string when rejected.
    """
    if not cfg or not cfg.token:
        return None
    import hmac
    presented = hello.get('token') or ''
    if not hmac.compare_digest(str(presented), cfg.token):
        return 'agent token mismatch'
    return None


class AgentSlot:
    """Holder for THE managed agent session — the single-agent invariant
    lives here. Swaps are serialized by the lock so two near-simultaneous
    connections can't interleave the check-close-replace sequence."""

    def __init__(self):
        self.lock = threading.Lock()
        self.session = None   # AgentSession or None

    def current(self):
        """Return the managed AgentSession (or None). Thread-safe."""
        with self.lock:
            return self.session


class AgentSession:
    """Manages a bidirectional connection to an agent.

    Works identically regardless of who initiated the TCP connection
    (server connecting out to --listen agent, or --server agent connecting
    in). After the hello handshake, the protocol is the same.
    """

    def __init__(self, ctx, sock, addr):
        self.ctx = ctx
        self.sock = sock
        self.addr = addr          # (host, port) string
        self.lock = threading.Lock()
        self.connected = True
        self.hello = None         # agent hello payload
        self._cmd_lock = threading.Lock()  # guards _pending + _responses
        self._pending = {}        # cmd_id -> threading.Event
        self._responses = {}      # cmd_id -> response dict
        self._recv_thread = None

        # Session persistence for profiling data. Chunks are spooled to
        # disk as they arrive (compressed payloads are written as-received)
        # — nothing is held in RAM for the life of the session.
        self._session_id = None
        self._session_dir = None
        self._chunk_index = 0

    def start(self):
        """Start the receiver thread. Call after reading the hello message."""
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def _spool_chunk(self, payload, flag):
        """Write one received data payload straight to the session dir.

        Compressed payloads (flag 1) are stored as-received (.zst);
        raw payloads (flag 0) as text (.txt). Keeping chunks on disk
        instead of in RAM bounds server memory for long sessions.
        """
        try:
            if flag == FLAG_DATA_ZSTD:
                fname = f'chunk_{self._chunk_index:05d}.zst'
            else:
                fname = f'chunk_{self._chunk_index:05d}.txt'
            with open(os.path.join(self._session_dir, fname), 'wb') as f:
                f.write(payload)
            self._chunk_index += 1
        except OSError as e:
            print(f"[server] Failed to spool chunk: {e}", file=sys.stderr)

    def send_command(self, cmd, args=None, timeout=60):
        """Send a command and wait for the response. Thread-safe.

        Returns the response dict, or {'ok': False, 'error': '...'} on failure.
        """
        cmd_id = uuid.uuid4().hex[:12]
        payload = json.dumps({
            'id': cmd_id,
            'cmd': cmd,
            'args': args or {},
        }).encode('utf-8')

        event = threading.Event()
        with self._cmd_lock:
            self._pending[cmd_id] = event

        try:
            header = struct.pack('!IB', len(payload), FLAG_CMD_REQUEST)
            with self.lock:
                self.sock.sendall(header + payload)
        except (IOError, OSError) as e:
            with self._cmd_lock:
                self._pending.pop(cmd_id, None)
            return {'ok': False, 'error': f'send failed: {e}'}

        # Wait for response
        got_response = event.wait(timeout)
        with self._cmd_lock:
            self._pending.pop(cmd_id, None)
            resp = self._responses.pop(cmd_id, None)
        if resp is not None:
            return resp
        if not got_response:
            return {'ok': False, 'error': 'command timed out'}
        return {'ok': False, 'error': 'no response'}

    def _recv_loop(self):
        """Read messages from agent, dispatch by flag type."""
        ctx = self.ctx
        state = ctx.state

        # Setup session for saving profiling data
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        self._session_id = f'{ts}_{self.addr}'
        self._session_dir = os.path.join(ctx.config.sessions_dir,
                                         self._session_id)
        os.makedirs(self._session_dir, exist_ok=True)

        while self.connected:
            try:
                header = recv_exactly(self.sock, 5)
                if header is None:
                    print(f"[server] Managed agent {self.addr} disconnected",
                          file=sys.stderr)
                    break

                length, flag = struct.unpack('!IB', header)
                if length == 0:
                    continue
                if length > MAX_FRAME_SIZE:
                    print(f"[server] Managed agent {self.addr}: oversized "
                          f"frame ({length} bytes) — disconnecting",
                          file=sys.stderr)
                    break

                payload = recv_exactly(self.sock, length)
                if payload is None:
                    print(f"[server] Managed agent {self.addr} disconnected mid-msg",
                          file=sys.stderr)
                    break

                if flag == FLAG_CMD_RESPONSE:
                    # Command response. Never let a malformed response kill
                    # the connection — log and keep receiving.
                    try:
                        resp = json.loads(payload.decode('utf-8', errors='replace'))
                        cmd_id = resp.get('id', '') if isinstance(resp, dict) else ''
                        with self._cmd_lock:
                            event = self._pending.get(cmd_id)
                            if event is not None:
                                self._responses[cmd_id] = resp
                                event.set()
                            # else: unsolicited (e.g. hello) — ignore
                    except Exception as e:
                        print(f"[server] Bad agent response: {e}",
                              file=sys.stderr)

                elif flag in (FLAG_DATA_RAW, FLAG_DATA_ZSTD):
                    # Profiling data
                    text = decompress_payload(ctx.config, payload, flag)
                    if text is None:
                        continue

                    self._spool_chunk(payload, flag)
                    script_text, stat_text = split_perf_data(text)
                    samples = parse_perf_script(script_text)
                    perf_stat = parse_perf_stat(stat_text) if stat_text else {}

                    if not samples:
                        continue

                    # add_samples sets dirty flag and signals rebuild worker
                    total_count, event_types = state.add_samples(samples, perf_stat)

                    print(f"[server] Managed agent chunk: "
                          f"{len(samples)} new, {total_count} total",
                          file=sys.stderr)

                    # Lightweight SSE: event types + stat pushed immediately.
                    # Heavy per_event rebuild handled by background worker.
                    ctx.broadcast('event_types', event_types)
                    if perf_stat:
                        # Broadcast the accumulated stat, not this round's
                        with state.lock:
                            merged_stat = dict(state.perf_stat)
                        ctx.broadcast('perf_stat', merged_stat)

                elif flag == FLAG_METRICS:
                    # Health metrics snapshot
                    try:
                        metrics = json.loads(payload.decode('utf-8',
                                                            errors='replace'))
                        mtype = metrics.get('type', '')
                        ctx.metrics.add(mtype, metrics)
                        ctx.broadcast('metrics_%s' % mtype, metrics)
                    except (ValueError, KeyError):
                        pass

                else:
                    print(f"[server] Unknown flag {flag} from managed agent",
                          file=sys.stderr)

            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                print(f"[server] Managed agent recv error: {e}", file=sys.stderr)
                break
            except Exception as e:
                import traceback
                print(f"[server] Managed agent recv unexpected error: {e}",
                      file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                break

        self.connected = False
        # Fail fast any in-flight commands instead of letting them time out
        with self._cmd_lock:
            for cmd_id, event in self._pending.items():
                self._responses.setdefault(
                    cmd_id, {'ok': False, 'error': 'agent disconnected'})
                event.set()
            self._pending.clear()
        with state.lock:
            # Only tear down live state if this session still owns it — a
            # replacement agent may already have been installed, in which
            # case the state (and its samples) belong to the new session.
            still_current = state.agent_conn is self.sock
            if still_current:
                state.agent_connected = False
                state.agent_conn = None
                all_samples = list(state.all_samples)
                perf_stat_final = dict(state.perf_stat)
            else:
                all_samples = []
                perf_stat_final = {}
        if still_current:
            ctx.broadcast('status', {'connected': False, 'agent': None})

        # Save session metadata (chunks are already spooled to disk)
        m_snap = ctx.metrics.snapshot_for_save()
        m_summary = ctx.metrics.get_summary()
        if self._chunk_index or any(m_snap.values()):
            from perflens.sessions import save_session
            t = threading.Thread(
                target=save_session,
                args=(self._session_dir, self._session_id,
                      self.addr, self._chunk_index,
                      all_samples, perf_stat_final, self.hello,
                      m_snap, m_summary),
                daemon=True,
            )
            t.start()

    def close(self):
        """Disconnect from agent."""
        self.connected = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def install_agent_session(ctx, session):
    """Register a new AgentSession as THE managed agent (replacing any
    existing one), reset profiling state, and start its receiver."""
    slot = ctx.agent
    with slot.lock:
        if slot.session and slot.session.connected:
            print("[server] Replacing existing agent session", file=sys.stderr)
            slot.session.close()

        ctx.state.reset()
        with ctx.state.lock:
            ctx.state.agent_connected = True
            ctx.state.agent_addr = session.addr
            ctx.state.agent_conn = session.sock
        ctx.metrics.reset()

        slot.session = session
        session.start()

    ctx.broadcast('status', {'connected': True, 'agent': session.addr})
    ctx.broadcast('agent_connected', {
        'agent': session.addr,
        'platform': (session.hello or {}).get('platform', {}),
    })


def stop_agent(ctx):
    """Close the agent connection, triggering the normal disconnect flow.
    Returns the /api/stop response dict."""
    slot = ctx.agent
    with slot.lock:
        session = slot.session
        slot.session = None
    if session and session.connected:
        try:
            session.send_command('stop', timeout=5)
        except Exception:
            pass
        session.close()
        return {'stopped': True}
    return {'stopped': False, 'reason': 'no agent connected'}


def connect_to_agent(ctx, host, port, timeout=10):
    """Connect to a listen-mode agent. Returns AgentSession or raises."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except (IOError, OSError) as e:
        sock.close()
        raise RuntimeError(
            f'Cannot connect to agent at {host}:{port}: {e}') from e

    # Read hello message (flag 3)
    header = recv_exactly(sock, 5)
    if header is None:
        sock.close()
        raise RuntimeError('Agent disconnected before hello')

    length, flag = struct.unpack('!IB', header)
    if flag != FLAG_CMD_RESPONSE:
        sock.close()
        raise RuntimeError(f'Expected hello (flag 3), got flag {flag}')
    if length > MAX_FRAME_SIZE:
        sock.close()
        raise RuntimeError(f'Oversized hello frame ({length} bytes)')

    payload = recv_exactly(sock, length)
    if payload is None:
        sock.close()
        raise RuntimeError('Agent disconnected during hello')

    try:
        hello = json.loads(payload.decode('utf-8', errors='replace'))
    except ValueError as e:
        sock.close()
        raise RuntimeError(f'Invalid hello JSON: {e}') from e

    if hello.get('type') != 'hello':
        sock.close()
        raise RuntimeError(f'Expected hello message, got: {hello.get("type")}')

    token_err = check_agent_token(ctx.config, hello)
    if token_err:
        sock.close()
        raise RuntimeError(token_err)

    # Clear connection timeout — recv loop must block indefinitely
    sock.settimeout(None)

    addr_str = f'{host}:{port}'
    session = AgentSession(ctx, sock, addr_str)
    session.hello = hello
    install_agent_session(ctx, session)

    print(f"[server] Connected to managed agent at {addr_str}: "
          f"platform={hello.get('platform', {}).get('arch', '?')}",
          file=sys.stderr)

    return session


def handle_inbound_agent(ctx, conn, addr):
    """Handle an inbound agent connection (agent using --server mode).

    Reads the hello handshake, creates an AgentSession, and starts the
    bidirectional protocol. Identical to the outbound path (connect_to_agent)
    after the TCP handshake.
    """
    addr_str = f'{addr[0]}:{addr[1]}'
    print(f"[server] Agent connected from {addr_str}", file=sys.stderr)

    # Read hello message (flag 3) — agent always sends hello first
    try:
        conn.settimeout(10)
        header = recv_exactly(conn, 5)
        if header is None:
            print(f"[server] Inbound agent {addr_str} disconnected before hello",
                  file=sys.stderr)
            conn.close()
            return

        length, flag = struct.unpack('!IB', header)
        if flag != FLAG_CMD_RESPONSE:
            print(f"[server] Inbound agent {addr_str}: expected hello (flag 3), "
                  f"got flag {flag}", file=sys.stderr)
            conn.close()
            return
        if length > MAX_FRAME_SIZE:
            print(f"[server] Inbound agent {addr_str}: oversized hello "
                  f"({length} bytes)", file=sys.stderr)
            conn.close()
            return

        payload = recv_exactly(conn, length)
        if payload is None:
            print(f"[server] Inbound agent {addr_str} disconnected during hello",
                  file=sys.stderr)
            conn.close()
            return

        hello = json.loads(payload.decode('utf-8', errors='replace'))
        if hello.get('type') != 'hello':
            print(f"[server] Inbound agent {addr_str}: expected hello message, "
                  f"got type={hello.get('type')}", file=sys.stderr)
            conn.close()
            return

    except (IOError, OSError, ValueError) as e:
        print(f"[server] Inbound agent {addr_str} hello failed: {e}",
              file=sys.stderr)
        try:
            conn.close()
        except OSError:
            pass
        return

    token_err = check_agent_token(ctx.config, hello)
    if token_err:
        print(f"[server] Inbound agent {addr_str} rejected: {token_err}",
              file=sys.stderr)
        try:
            conn.close()
        except OSError:
            pass
        return

    # Clear connection timeout — recv loop must block indefinitely
    conn.settimeout(None)

    session = AgentSession(ctx, conn, addr_str)
    session.hello = hello
    install_agent_session(ctx, session)

    print(f"[server] Inbound agent {addr_str} ready: "
          f"platform={hello.get('platform', {}).get('arch', '?')}",
          file=sys.stderr)


def run_tcp_server(ctx):
    """Run the TCP server that accepts agent connections."""
    port = ctx.config.tcp_port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', port))
    sock.listen(5)
    print(f"[server] TCP server listening on port {port}", file=sys.stderr)

    try:
        while True:
            conn, addr = sock.accept()
            t = threading.Thread(target=handle_inbound_agent,
                                 args=(ctx, conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
