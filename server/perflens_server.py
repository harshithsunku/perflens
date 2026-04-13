#!/usr/bin/env python3
"""PerfLens Server — receives perf data from agents and serves web UI."""

import dataclasses
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http.server import SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from parser import (parse_perf_script, build_function_summary, build_flamegraph_data,
                    split_perf_data, get_event_types, filter_samples_by_event,
                    parse_perf_stat)
from source_mapper import SourceMapper, build_annotated_source


# ---------------------------------------------------------------------------
# Server configuration
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ServerConfig:
    source_dir: str = '.'
    binary_path: str = None
    map_file_path: str = None
    addr2line_bin: str = None
    zstd_bin: str = None
    perf_bin: str = None
    path_map: dict = None
    sessions_dir: str = ''
    max_samples: int = 500000
    tcp_port: int = 9999
    http_port: int = 8080
    ui_dir: str = ''
    inline: bool = True


# ---------------------------------------------------------------------------
# Profiling state
# ---------------------------------------------------------------------------

class ProfilingState:
    """Shared state for streaming data to UI. Thread-safe."""

    def __init__(self, max_samples=500000):
        self.lock = threading.Lock()
        self.all_samples = []
        self.chunk_count = 0
        self.last_update = 0
        self.agent_connected = False
        self.agent_addr = None
        self.sse_clients = set()   # set of (wfile, wlock) tuples
        self.agent_conn = None     # active agent socket, for /api/stop
        self.event_types = []
        self.perf_stat = {}
        self.source_mapper = None  # SourceMapper, set at startup
        self._max_samples = max_samples

    def add_samples(self, new_samples, perf_stat=None):
        """Add samples and return (all_samples_copy, event_types_copy)."""
        with self.lock:
            self.all_samples.extend(new_samples)
            if len(self.all_samples) > self._max_samples:
                excess = len(self.all_samples) - self._max_samples
                self.all_samples = self.all_samples[excess:]
            self.chunk_count += 1
            self.last_update = time.time()
            self.event_types = get_event_types(self.all_samples)
            if perf_stat:
                self.perf_stat = perf_stat
            return list(self.all_samples), list(self.event_types)

    def get_snapshot(self):
        with self.lock:
            return {
                'all_samples': list(self.all_samples),
                'event_types': list(self.event_types),
                'perf_stat': dict(self.perf_stat),
            }

    def reset(self):
        with self.lock:
            self.all_samples = []
            self.chunk_count = 0
            self.event_types = []
            self.perf_stat = {}


# Wire protocol flags (must match agent)
FLAG_DATA_RAW = 0
FLAG_DATA_ZSTD = 1
FLAG_CMD_REQUEST = 2
FLAG_CMD_RESPONSE = 3

# Module-level instances, set in main()
config: ServerConfig = None
state: ProfilingState = None
agent_session: 'AgentSession' = None  # managed agent connection
wizard_state: dict = None              # wizard UI state


# ---------------------------------------------------------------------------
# Startup probing
# ---------------------------------------------------------------------------

def _find_binary(name, bundled_subdir='bin'):
    """Find a binary: check server/bin/ first, then system PATH."""
    bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           bundled_subdir, name)
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    try:
        r = subprocess.run(['which', name], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def probe_tools(cfg):
    """Probe available tools at startup and log capability status."""
    print("[server] === Startup Capability Check ===", file=sys.stderr)

    # Source directory
    if os.path.isdir(cfg.source_dir):
        print(f"[server]   source-dir: {cfg.source_dir}", file=sys.stderr)
    else:
        print(f"[server]   source-dir: {cfg.source_dir} (NOT FOUND)", file=sys.stderr)

    # Binary
    if cfg.binary_path and os.path.isfile(cfg.binary_path):
        print(f"[server]   binary: {cfg.binary_path}", file=sys.stderr)
    elif cfg.binary_path:
        print(f"[server]   binary: {cfg.binary_path} (NOT FOUND)", file=sys.stderr)
        cfg.binary_path = None
    else:
        print("[server]   binary: not provided (source mapping limited)",
              file=sys.stderr)

    # Map file
    if cfg.map_file_path and os.path.isfile(cfg.map_file_path):
        print(f"[server]   map file: {cfg.map_file_path}", file=sys.stderr)
    elif cfg.map_file_path:
        print(f"[server]   map file: {cfg.map_file_path} (NOT FOUND)",
              file=sys.stderr)
        cfg.map_file_path = None
    else:
        print("[server]   map file: not provided", file=sys.stderr)

    # addr2line
    if cfg.addr2line_bin and os.path.isfile(cfg.addr2line_bin):
        print(f"[server]   addr2line: {cfg.addr2line_bin} (user-provided)",
              file=sys.stderr)
    else:
        found = _find_binary('addr2line')
        if found:
            cfg.addr2line_bin = found
            label = 'bundled' if 'server/bin' in found else 'system'
            print(f"[server]   addr2line: {found} ({label})", file=sys.stderr)
        else:
            cfg.addr2line_bin = None
            print("[server]   addr2line: NOT FOUND (source mapping disabled)",
                  file=sys.stderr)

    # zstd
    found = _find_binary('zstd')
    if found:
        cfg.zstd_bin = found
        label = 'bundled' if 'server/bin' in found else 'system'
        print(f"[server]   zstd: {found} ({label})", file=sys.stderr)
    else:
        cfg.zstd_bin = None
        print("[server]   zstd: NOT FOUND (compressed payloads will fail)",
              file=sys.stderr)

    # Path map
    if cfg.path_map:
        for k, v in cfg.path_map.items():
            print(f"[server]   path-map: {k} → {v}", file=sys.stderr)

    # perf
    found = _find_binary('perf')
    if found:
        cfg.perf_bin = found
        print(f"[server]   perf: {found}", file=sys.stderr)
    else:
        cfg.perf_bin = None
        print("[server]   perf: NOT FOUND (perf.data import disabled)",
              file=sys.stderr)

    # Inline resolution
    if cfg.inline:
        print("[server]   inline: enabled (will probe at mapper init)",
              file=sys.stderr)
    else:
        print("[server]   inline: disabled (--no-inline)", file=sys.stderr)

    print("[server] ================================", file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def recv_exactly(conn, n):
    """Receive exactly n bytes from a socket."""
    data = b''
    while len(data) < n:
        chunk = conn.recv(min(65536, n - len(data)))
        if not chunk:
            return None
        data += chunk
    return data


def decompress_payload(payload, comp_flag):
    """Decompress payload based on compression flag. Returns text string."""
    if comp_flag == 0:
        return payload.decode('utf-8', errors='replace')

    if comp_flag == 1:
        if not config.zstd_bin:
            print("[server] WARNING: received zstd data but zstd not available",
                  file=sys.stderr)
            return None
        try:
            r = subprocess.run(
                [config.zstd_bin, '-d', '-c'],
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


# ---------------------------------------------------------------------------
# Managed agent session (bidirectional protocol)
# ---------------------------------------------------------------------------

class AgentSession:
    """Manages a bidirectional connection to an agent.

    Works identically regardless of who initiated the TCP connection
    (server connecting out to --listen agent, or --server agent connecting
    in). After the hello handshake, the protocol is the same.
    """

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr          # (host, port) string
        self.lock = threading.Lock()
        self.connected = True
        self.hello = None         # agent hello payload
        self._pending = {}        # cmd_id -> threading.Event
        self._responses = {}      # cmd_id -> response dict
        self._recv_thread = None

        # Session persistence for profiling data
        self._session_id = None
        self._session_dir = None
        self._raw_chunks = []

    def start(self):
        """Start the receiver thread. Call after reading the hello message."""
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

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
        self._pending[cmd_id] = event

        try:
            header = struct.pack('!IB', len(payload), FLAG_CMD_REQUEST)
            with self.lock:
                self.sock.sendall(header + payload)
        except (IOError, OSError) as e:
            self._pending.pop(cmd_id, None)
            return {'ok': False, 'error': f'send failed: {e}'}

        # Wait for response
        if not event.wait(timeout):
            self._pending.pop(cmd_id, None)
            return {'ok': False, 'error': 'command timed out'}

        return self._responses.pop(cmd_id, {'ok': False, 'error': 'no response'})

    def _recv_loop(self):
        """Read messages from agent, dispatch by flag type."""
        # Setup session for saving profiling data
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        self._session_id = f'{ts}_{self.addr}'
        self._session_dir = os.path.join(config.sessions_dir, self._session_id)
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

                payload = recv_exactly(self.sock, length)
                if payload is None:
                    print(f"[server] Managed agent {self.addr} disconnected mid-msg",
                          file=sys.stderr)
                    break

                if flag == FLAG_CMD_RESPONSE:
                    # Command response
                    try:
                        resp = json.loads(payload.decode('utf-8', errors='replace'))
                        cmd_id = resp.get('id', '')
                        if cmd_id in self._pending:
                            self._responses[cmd_id] = resp
                            self._pending[cmd_id].set()
                        else:
                            # Unsolicited response (e.g. hello) — ignore
                            pass
                    except (ValueError, KeyError) as e:
                        print(f"[server] Bad agent response JSON: {e}",
                              file=sys.stderr)

                elif flag in (FLAG_DATA_RAW, FLAG_DATA_ZSTD):
                    # Profiling data
                    text = decompress_payload(payload, flag)
                    if text is None:
                        continue

                    self._raw_chunks.append(text)
                    script_text, stat_text = split_perf_data(text)
                    samples = parse_perf_script(script_text)
                    perf_stat = parse_perf_stat(stat_text) if stat_text else {}

                    if not samples:
                        continue

                    all_samples, event_types = state.add_samples(samples, perf_stat)

                    print(f"[server] Managed agent chunk: "
                          f"{len(samples)} new, {len(all_samples)} total",
                          file=sys.stderr)

                    per_event = build_per_event_data(
                        all_samples, event_types, state.source_mapper)
                    broadcast_sse('event_types', event_types)
                    broadcast_sse('per_event', per_event)
                    if perf_stat:
                        broadcast_sse('perf_stat', perf_stat)

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
        with state.lock:
            state.agent_connected = False
            state.agent_conn = None
            all_samples = list(state.all_samples)
            perf_stat_final = dict(state.perf_stat)
        broadcast_sse('status', {'connected': False, 'agent': None})

        # Save session
        if self._raw_chunks:
            t = threading.Thread(
                target=_save_session,
                args=(self._session_dir, self._session_id,
                      self.addr, self._raw_chunks,
                      all_samples, perf_stat_final, self.hello),
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


def connect_to_agent(host, port, timeout=10):
    """Connect to a listen-mode agent. Returns AgentSession or raises."""
    global agent_session

    # Close existing session if any
    if agent_session and agent_session.connected:
        agent_session.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except (IOError, OSError) as e:
        sock.close()
        raise RuntimeError(f'Cannot connect to agent at {host}:{port}: {e}')

    # Read hello message (flag 3)
    header = recv_exactly(sock, 5)
    if header is None:
        sock.close()
        raise RuntimeError('Agent disconnected before hello')

    length, flag = struct.unpack('!IB', header)
    if flag != FLAG_CMD_RESPONSE:
        sock.close()
        raise RuntimeError(f'Expected hello (flag 3), got flag {flag}')

    payload = recv_exactly(sock, length)
    if payload is None:
        sock.close()
        raise RuntimeError('Agent disconnected during hello')

    try:
        hello = json.loads(payload.decode('utf-8', errors='replace'))
    except ValueError as e:
        sock.close()
        raise RuntimeError(f'Invalid hello JSON: {e}')

    if hello.get('type') != 'hello':
        sock.close()
        raise RuntimeError(f'Expected hello message, got: {hello.get("type")}')

    # Clear connection timeout — recv loop must block indefinitely
    sock.settimeout(None)

    addr_str = f'{host}:{port}'
    session = AgentSession(sock, addr_str)
    session.hello = hello

    # Update global state
    with state.lock:
        state.agent_connected = True
        state.agent_addr = addr_str
        state.agent_conn = sock
        state.all_samples = []
        state.chunk_count = 0

    broadcast_sse('status', {'connected': True, 'agent': addr_str})
    broadcast_sse('agent_connected', {
        'agent': addr_str,
        'platform': hello.get('platform', {}),
    })

    session.start()
    agent_session = session

    print(f"[server] Connected to managed agent at {addr_str}: "
          f"platform={hello.get('platform', {}).get('arch', '?')}",
          file=sys.stderr)

    return session


# ---------------------------------------------------------------------------
# Wizard state management
# ---------------------------------------------------------------------------

def get_wizard_state():
    """Get current wizard state (server-side, survives page refreshes)."""
    global wizard_state
    if wizard_state is None:
        wizard_state = {
            'step': 0,
            'agent_host': '',
            'agent_port': 9999,
            'connected': False,
            'perf_verified': False,
            'binary_path': '',
            'source_dir': '',
            'pid': None,
            'process_name': '',
            'frequency': 99,
            'duration': 8,
        }
    return wizard_state


def update_wizard_state(updates):
    """Merge updates into wizard state."""
    ws = get_wizard_state()
    ws.update(updates)
    return ws


# ---------------------------------------------------------------------------
# SSE broadcasting
# ---------------------------------------------------------------------------

def broadcast_sse(event_type, data):
    """Send an SSE event to all connected browsers."""
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    encoded = msg.encode('utf-8')

    dead = set()
    with state.lock:
        clients = set(state.sse_clients)

    for client in clients:
        wfile, wlock = client
        try:
            with wlock:
                wfile.write(encoded)
                wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            dead.add(client)

    if dead:
        with state.lock:
            state.sse_clients -= dead


# ---------------------------------------------------------------------------
# Agent connection handler
# ---------------------------------------------------------------------------

def handle_inbound_agent(conn, addr):
    """Handle an inbound agent connection (agent using --server mode).

    Reads the hello handshake, creates an AgentSession, and starts the
    bidirectional protocol. Identical to the outbound path (connect_to_agent)
    after the TCP handshake.
    """
    global agent_session

    addr_str = f'{addr[0]}:{addr[1]}'
    print(f"[server] Agent connected from {addr_str}", file=sys.stderr)

    # Close existing session if any
    if agent_session and agent_session.connected:
        print("[server] Replacing existing agent session", file=sys.stderr)
        agent_session.close()

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

    # Clear connection timeout — recv loop must block indefinitely
    conn.settimeout(None)

    session = AgentSession(conn, addr_str)
    session.hello = hello

    # Update global state
    with state.lock:
        state.agent_connected = True
        state.agent_addr = addr_str
        state.agent_conn = conn
        state.all_samples = []
        state.chunk_count = 0

    broadcast_sse('status', {'connected': True, 'agent': addr_str})
    broadcast_sse('agent_connected', {
        'agent': addr_str,
        'platform': hello.get('platform', {}),
    })

    session.start()
    agent_session = session

    print(f"[server] Inbound agent {addr_str} ready: "
          f"platform={hello.get('platform', {}).get('arch', '?')}",
          file=sys.stderr)


def _save_session(session_dir, session_id, agent_addr, raw_chunks,
                  all_samples, perf_stat, hello=None):
    """Save profiling session to disk (raw chunks + metadata).

    Metadata schema version 0.4.0: includes platform info from agent hello.
    """
    try:
        for i, chunk in enumerate(raw_chunks):
            with open(os.path.join(session_dir, f'chunk_{i:03d}.txt'), 'w') as f:
                f.write(chunk)

        event_types = get_event_types(all_samples)
        metadata = {
            'version': '0.4.0',
            'session_id': session_id,
            'agent': agent_addr,
            'timestamp': datetime.now().isoformat(),
            'total_samples': len(all_samples),
            'chunks': len(raw_chunks),
            'event_types': event_types,
            'perf_stat': perf_stat,
        }
        if hello and hello.get('platform'):
            metadata['platform'] = hello['platform']

        with open(os.path.join(session_dir, 'metadata.json'), 'w') as f:
            json.dump(metadata, f, indent=2)

        print(f"[server] Session saved: {session_id} ({len(all_samples)} samples)",
              file=sys.stderr)
    except Exception as e:
        print(f"[server] Error saving session: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# perf.data import
# ---------------------------------------------------------------------------

MAX_IMPORT_SIZE = 500 * 1024 * 1024  # 500 MB

def _run_perf_script(perf_data_path):
    """Run perf script on a perf.data file. Returns perf script text or raises."""
    if not config.perf_bin:
        raise RuntimeError('perf not found on server — cannot import perf.data')

    # Try with -F first (structured output, matches agent behavior)
    try:
        r = subprocess.run(
            [config.perf_bin, 'script', '-F', 'comm,pid,time,period,event,ip,sym,dso',
             '-i', perf_data_path],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError('perf script timed out (file too large?)')
    except Exception:
        pass

    # Fallback to plain perf script
    try:
        r = subprocess.run(
            [config.perf_bin, 'script', '-i', perf_data_path],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
        stderr = r.stderr.strip() if r.stderr else 'unknown error'
        raise RuntimeError(f'perf script failed: {stderr}')
    except subprocess.TimeoutExpired:
        raise RuntimeError('perf script timed out (file too large?)')


def import_perf_data(perf_data_path):
    """Import a perf.data file: run perf script, parse, save as session.

    Returns (session_id, all_samples, metadata) on success.
    Raises RuntimeError on failure.
    """
    if not os.path.isfile(perf_data_path):
        raise RuntimeError(f'file not found: {perf_data_path}')

    print(f"[server] Importing perf.data: {perf_data_path}", file=sys.stderr)
    script_text = _run_perf_script(perf_data_path)

    # Parse
    samples = parse_perf_script(script_text)
    if not samples:
        raise RuntimeError('perf script produced no samples '
                           '(file may be empty or corrupt)')

    event_types = get_event_types(samples)
    session_id = (datetime.now().strftime('%Y%m%d_%H%M%S')
                  + f'_{os.getpid():04x}_import')

    # Save as session (single chunk)
    session_dir = os.path.join(config.sessions_dir, session_id)
    os.makedirs(session_dir, exist_ok=True)

    with open(os.path.join(session_dir, 'chunk_000.txt'), 'w') as f:
        f.write(script_text)

    metadata = {
        'version': '0.4.0',
        'session_id': session_id,
        'agent': 'import',
        'timestamp': datetime.now().isoformat(),
        'total_samples': len(samples),
        'chunks': 1,
        'event_types': event_types,
        'perf_stat': {},
    }
    with open(os.path.join(session_dir, 'metadata.json'), 'w') as f:
        json.dump(metadata, f, indent=2)

    print(f"[server] Import complete: {session_id} "
          f"({len(samples)} samples, events: {event_types})",
          file=sys.stderr)

    return session_id, samples, metadata


# ---------------------------------------------------------------------------
# Per-event data builder (inline expansion + summaries)
# ---------------------------------------------------------------------------

def build_per_event_data(all_samples, event_types, mapper, source=False):
    """Build per-event data dict for UI consumption.

    Expands inline frames (if mapper has inline enabled) before building
    flamegraph trees and function summaries.

    Args:
        all_samples: raw sample list
        event_types: list of event type strings
        mapper: SourceMapper instance (or None)
        source: if True, include annotated source in the output
    """
    expanded = mapper.expand_inline_frames(all_samples) if mapper else all_samples

    per_event = {}
    for evt in event_types:
        evt_expanded = filter_samples_by_event(expanded, evt)
        evt_orig = filter_samples_by_event(all_samples, evt)
        entry = {
            'function_summary': build_function_summary(evt_expanded),
            'flamegraph': build_flamegraph_data(evt_expanded),
            'source_files': mapper.get_files_with_samples(evt_orig)
                            if mapper else [],
        }
        if source:
            if mapper:
                entry['source'] = build_annotated_source(mapper, evt_orig)
            else:
                entry['source'] = {}
        per_event[evt] = entry
    return per_event


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def _load_session_samples(session_id):
    """Load all samples from a saved session. Returns (samples, metadata) or (None, None)."""
    session_dir = os.path.join(config.sessions_dir, session_id)
    meta_path = os.path.join(session_dir, 'metadata.json')
    if not os.path.isfile(meta_path):
        return None, None

    with open(meta_path) as f:
        metadata = json.load(f)

    all_samples = []
    chunk_files = sorted(
        f for f in os.listdir(session_dir)
        if f.startswith('chunk_') and f.endswith('.txt')
    )
    for fname in chunk_files:
        fpath = os.path.join(session_dir, fname)
        try:
            with open(fpath) as f:
                text = f.read()
            script_text, _ = split_perf_data(text)
            samples = parse_perf_script(script_text)
            all_samples.extend(samples)
        except (IOError, OSError):
            pass

    return all_samples, metadata


def _export_collapsed(samples):
    """Export samples in Brendan Gregg collapsed stack format.

    Each line: semicolon-separated stack (bottom to top) followed by space
    and sample count. Compatible with flamegraph.pl, speedscope, Perfetto.
    """
    stacks = {}
    for sample in samples:
        if not sample['frames']:
            continue
        # Build stack bottom-to-top (reversed frames, since frames[0] is leaf)
        funcs = [f['func'] for f in reversed(sample['frames'])]
        key = ';'.join(funcs)
        stacks[key] = stacks.get(key, 0) + 1

    lines = []
    for stack, count in sorted(stacks.items()):
        lines.append(f'{stack} {count}')
    return '\n'.join(lines) + '\n' if lines else ''


def _render_flamegraph_svg(fg_root, total_samples, event_type):
    """Render flamegraph tree as standalone SVG with embedded styles."""
    width = 1200
    row_height = 18
    font_size = 11
    margin_top = 50  # space for title

    # Flatten tree
    rects = []
    _flatten_for_svg(fg_root, 0, 0, width, rects, total_samples)
    max_depth = max((r['depth'] for r in rects), default=0)
    height = margin_top + (max_depth + 1) * row_height + 4

    # Build SVG
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"'
        f' viewBox="0 0 {width} {height}" font-family="monospace">',
        '<style>',
        '  rect:hover { stroke: #fff; stroke-width: 1; }',
        '  text { pointer-events: none; fill: #fff; }',
        '  .title { font-size: 16px; fill: #333; font-weight: bold; }',
        '  .subtitle { font-size: 12px; fill: #666; }',
        '</style>',
        f'<rect width="{width}" height="{height}" fill="#f8f8f0"/>',
        f'<text x="10" y="20" class="title">PerfLens Flamegraph — {_svg_escape(event_type)}</text>',
        f'<text x="10" y="38" class="subtitle">{total_samples} samples</text>',
    ]

    for r in rects:
        inlined = r.get('inlined', False)
        hue = 30 + (_hash_code(r['name']) % 30)
        sat = 50 + (_hash_code(r['name'] + 'x') % 15) if inlined \
            else 80 + (_hash_code(r['name'] + 'x') % 20)
        light = 45 + (_hash_code(r['name'] + 'y') % 15)
        color = f'hsl({hue}, {sat}%, {light}%)'
        y = height - (r['depth'] + 1) * row_height
        rw = max(r['w'] - 1, 1)

        inlined_tag = ' (inlined)' if inlined else ''
        pct = f"{r['percent']:.1f}"
        title = f"{_svg_escape(r['name'])}{inlined_tag} ({r['value']} samples, {pct}%)"
        stroke = ' stroke-dasharray="3 2" stroke="rgba(0,0,0,0.3)" stroke-width="1"' \
            if inlined else ''
        lines.append(f'<g>')
        lines.append(f'  <rect x="{r["x"]:.1f}" y="{y}" width="{rw:.1f}"'
                     f' height="{row_height - 1}" fill="{color}" rx="1"{stroke}>'
                     f'<title>{title}</title></rect>')
        if r['w'] > 40:
            max_chars = int(r['w'] / 7)
            label = r['name'][:max_chars] + '..' if len(r['name']) > max_chars else r['name']
            lines.append(f'  <text x="{r["x"] + 3:.1f}" y="{y + 13}"'
                         f' font-size="{font_size}">{_svg_escape(label)}</text>')
        lines.append('</g>')

    lines.append('</svg>')
    return '\n'.join(lines)


def _flatten_for_svg(node, depth, x, width, rects, total_samples):
    """Flatten flamegraph tree into list of rects for SVG export."""
    pct = (node['value'] / total_samples * 100) if total_samples > 0 else 0
    entry = {
        'name': node['name'], 'value': node['value'], 'percent': pct,
        'depth': depth, 'x': x, 'w': width,
    }
    if node.get('inlined'):
        entry['inlined'] = True
    rects.append(entry)
    child_x = x
    for child in (node.get('children') or []):
        cw = (child['value'] / node['value']) * width if node['value'] > 0 else 0
        if cw >= 1:
            _flatten_for_svg(child, depth + 1, child_x, cw, rects, total_samples)
        child_x += cw


def _hash_code(s):
    """Simple string hash matching the JS hashCode function."""
    h = 0
    for c in s:
        h = ((h << 5) - h) + ord(c)
        h &= 0xFFFFFFFF
    return h


def _svg_escape(s):
    """Escape text for SVG/XML."""
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))


# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------

def run_tcp_server(port):
    """Run the TCP server that accepts agent connections."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', port))
    sock.listen(5)
    print(f"[server] TCP server listening on port {port}", file=sys.stderr)

    try:
        while True:
            conn, addr = sock.accept()
            t = threading.Thread(target=handle_inbound_agent,
                                 args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class PerfLensHTTPHandler(SimpleHTTPRequestHandler):
    """HTTP handler for serving UI and API endpoints."""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/status':
            self._send_json({
                'status': 'ok',
                'agent_connected': state.agent_connected,
                'agent_addr': state.agent_addr,
                'total_samples': len(state.all_samples),
                'chunk_count': state.chunk_count,
            })
        elif path == '/api/stop':
            self._handle_stop()
        elif path == '/api/stream':
            self._handle_sse()
        elif path == '/api/sessions':
            self._handle_sessions_list()
        elif path.startswith('/api/sessions/'):
            session_id = path.split('/api/sessions/')[1].rstrip('/')
            self._handle_session_replay(session_id)
        elif path == '/api/export/flamegraph':
            params = parse_qs(parsed.query)
            event = params.get('event', ['cycles'])[0]
            session_id = params.get('session', [None])[0]
            self._handle_export_flamegraph(event, session_id)
        elif path.startswith('/api/export/session/'):
            session_id = path.split('/api/export/session/')[1].rstrip('/')
            params = parse_qs(parsed.query)
            fmt = params.get('format', ['collapsed'])[0]
            self._handle_export_session(session_id, fmt)
        elif path == '/api/source':
            params = parse_qs(parsed.query)
            file_path = params.get('file', [None])[0]
            self._handle_source_request(file_path)
        elif path == '/api/wizard/state':
            self._send_json(get_wizard_state())
        elif path == '/api/browse':
            params = parse_qs(parsed.query)
            browse_path = params.get('path', ['/'])[0]
            self._handle_browse(browse_path)
        else:
            # Serve static files from UI directory
            if path == '/':
                path = '/index.html'
            file_path = os.path.join(config.ui_dir, path.lstrip('/'))
            if os.path.isfile(file_path):
                self._serve_file(file_path)
            else:
                self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/api/import':
            self._handle_import()
        elif path == '/api/connect':
            self._handle_connect()
        elif path == '/api/agent/command':
            self._handle_agent_command()
        elif path == '/api/wizard/state':
            self._handle_wizard_state_update()
        elif path == '/api/config/binary':
            self._handle_config_binary()
        elif path == '/api/config/source':
            self._handle_config_source()
        elif path == '/api/config/pathmap':
            self._handle_config_pathmap()
        else:
            self.send_error(404)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _handle_stop(self):
        """Close the agent connection, triggering normal disconnect flow."""
        global agent_session
        if agent_session and agent_session.connected:
            try:
                agent_session.send_command('stop', timeout=5)
            except Exception:
                pass
            agent_session.close()
            agent_session = None
            self._send_json({'stopped': True})
        else:
            self._send_json({'stopped': False, 'reason': 'no agent connected'})

    def _read_json_body(self):
        """Read and parse JSON POST body."""
        length = int(self.headers.get('Content-Length', 0))
        if length <= 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body.decode('utf-8', errors='replace'))

    def _handle_connect(self):
        """POST /api/connect — connect to a listen-mode agent."""
        try:
            body = self._read_json_body()
        except (ValueError, KeyError):
            self._send_json({'error': 'invalid JSON'}, 400)
            return

        host = body.get('host', '').strip()
        port = int(body.get('port', 9999))

        if not host:
            self._send_json({'error': 'host required'}, 400)
            return

        try:
            session = connect_to_agent(host, port)
            update_wizard_state({
                'agent_host': host,
                'agent_port': port,
                'connected': True,
            })
            self._send_json({
                'ok': True,
                'hello': session.hello,
                'addr': session.addr,
            })
        except RuntimeError as e:
            self._send_json({'ok': False, 'error': str(e)}, 500)

    def _handle_agent_command(self):
        """POST /api/agent/command — relay command to managed agent."""
        if not agent_session or not agent_session.connected:
            self._send_json({'error': 'no managed agent connected'}, 400)
            return

        try:
            body = self._read_json_body()
        except (ValueError, KeyError):
            self._send_json({'error': 'invalid JSON'}, 400)
            return

        cmd = body.get('cmd', '')
        args = body.get('args') or {}
        timeout = int(body.get('timeout', 60))

        if not cmd:
            self._send_json({'error': 'cmd required'}, 400)
            return

        # list_processes and reprobe can take longer
        if cmd in ('list_processes', 'reprobe', 'start'):
            timeout = max(timeout, 120)

        resp = agent_session.send_command(cmd, args, timeout=timeout)
        self._send_json(resp)

    def _handle_wizard_state_update(self):
        """POST /api/wizard/state — update wizard state."""
        try:
            body = self._read_json_body()
        except (ValueError, KeyError):
            self._send_json({'error': 'invalid JSON'}, 400)
            return
        ws = update_wizard_state(body)
        self._send_json(ws)

    def _handle_config_binary(self):
        """POST /api/config/binary — set binary path for addr2line."""
        try:
            body = self._read_json_body()
        except (ValueError, KeyError):
            self._send_json({'error': 'invalid JSON'}, 400)
            return

        path = body.get('path', '').strip()
        if not path:
            config.binary_path = None
            self._send_json({'ok': True, 'path': None})
            return

        path = os.path.abspath(path)
        if not os.path.isfile(path):
            self._send_json({'ok': False, 'error': f'file not found: {path}'}, 400)
            return

        config.binary_path = path
        # Recreate source mapper with new binary
        mapper = SourceMapper(
            config.source_dir,
            binary_path=config.binary_path,
            map_file_path=config.map_file_path,
            addr2line_bin=config.addr2line_bin,
            path_map=config.path_map or {},
            inline=config.inline,
        )
        state.source_mapper = mapper
        update_wizard_state({'binary_path': path})
        self._send_json({'ok': True, 'path': path})

    def _handle_config_source(self):
        """POST /api/config/source — set source directory."""
        try:
            body = self._read_json_body()
        except (ValueError, KeyError):
            self._send_json({'error': 'invalid JSON'}, 400)
            return

        path = body.get('path', '').strip()
        if not path:
            self._send_json({'ok': False, 'error': 'path required'}, 400)
            return

        path = os.path.abspath(path)
        if not os.path.isdir(path):
            self._send_json({'ok': False, 'error': f'directory not found: {path}'}, 400)
            return

        config.source_dir = path
        # Recreate source mapper
        mapper = SourceMapper(
            config.source_dir,
            binary_path=config.binary_path,
            map_file_path=config.map_file_path,
            addr2line_bin=config.addr2line_bin,
            path_map=config.path_map or {},
            inline=config.inline,
        )
        state.source_mapper = mapper
        update_wizard_state({'source_dir': path})
        self._send_json({'ok': True, 'path': path})

    def _handle_config_pathmap(self):
        """POST /api/config/pathmap — set path mapping."""
        try:
            body = self._read_json_body()
        except (ValueError, KeyError):
            self._send_json({'error': 'invalid JSON'}, 400)
            return

        path_map = body.get('path_map', {})
        config.path_map = path_map if path_map else None
        # Recreate source mapper
        mapper = SourceMapper(
            config.source_dir,
            binary_path=config.binary_path,
            map_file_path=config.map_file_path,
            addr2line_bin=config.addr2line_bin,
            path_map=config.path_map or {},
            inline=config.inline,
        )
        state.source_mapper = mapper
        self._send_json({'ok': True, 'path_map': path_map})

    def _handle_browse(self, browse_path):
        """GET /api/browse?path=/ — browse server filesystem for files."""
        browse_path = os.path.abspath(browse_path)
        if not os.path.isdir(browse_path):
            self._send_json({'error': f'not a directory: {browse_path}'}, 400)
            return

        entries = []
        try:
            for name in sorted(os.listdir(browse_path)):
                full = os.path.join(browse_path, name)
                is_dir = os.path.isdir(full)
                entry = {'name': name, 'path': full, 'is_dir': is_dir}
                if not is_dir:
                    try:
                        entry['size'] = os.path.getsize(full)
                    except OSError:
                        entry['size'] = 0
                entries.append(entry)
        except PermissionError:
            self._send_json({'error': f'permission denied: {browse_path}'}, 403)
            return

        self._send_json({
            'path': browse_path,
            'parent': os.path.dirname(browse_path),
            'entries': entries[:500],  # cap at 500 entries
        })

    def _serve_file(self, file_path):
        ext = os.path.splitext(file_path)[1]
        content_types = {
            '.html': 'text/html',
            '.css': 'text/css',
            '.js': 'application/javascript',
            '.json': 'application/json',
            '.png': 'image/png',
            '.svg': 'image/svg+xml',
        }
        ct = content_types.get(ext, 'application/octet-stream')
        with open(file_path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _handle_sse(self):
        """Server-Sent Events endpoint for real-time updates."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        wlock = threading.Lock()
        client = (self.wfile, wlock)
        with state.lock:
            state.sse_clients.add(client)

        # Send current state immediately
        snapshot = state.get_snapshot()
        all_samples = snapshot['all_samples']
        event_types = snapshot['event_types']
        perf_stat = snapshot['perf_stat']

        if all_samples:
            mapper = state.source_mapper
            per_event = build_per_event_data(all_samples, event_types, mapper)

            for sse_event, data in [
                ('event_types', event_types),
                ('per_event', per_event),
                ('perf_stat', perf_stat),
            ]:
                msg = f"event: {sse_event}\ndata: {json.dumps(data)}\n\n"
                try:
                    with wlock:
                        self.wfile.write(msg.encode('utf-8'))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    with state.lock:
                        state.sse_clients.discard(client)
                    return

        # Keep connection open
        try:
            while True:
                time.sleep(1)
                try:
                    with wlock:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            with state.lock:
                state.sse_clients.discard(client)

    def _handle_source_request(self, file_path):
        """Return annotated source for a specific file."""
        if not file_path:
            self._send_json({'error': 'file parameter required'})
            return

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        event_type = params.get('event', [None])[0]

        mapper = state.source_mapper
        if not mapper:
            self._send_json({'file': file_path, 'lines': [],
                             'error': 'source mapper not available'})
            return

        with state.lock:
            all_samples = list(state.all_samples)

        if event_type:
            all_samples = filter_samples_by_event(all_samples, event_type)

        line_data = mapper.map_samples_to_lines(all_samples)

        if file_path in line_data:
            lines = mapper.annotate_source(file_path, line_data[file_path])
            self._send_json({'file': file_path, 'lines': lines})
        else:
            self._send_json({'file': file_path, 'lines': [],
                             'error': 'no data for file'})

    def _handle_export_flamegraph(self, event_type, session_id=None):
        """Export flamegraph as standalone SVG. Uses session data if provided."""
        all_samples = None

        # Try session first (saved or live)
        if session_id and session_id != 'live':
            all_samples, _ = _load_session_samples(session_id)

        # Fall back to live data
        if not all_samples:
            with state.lock:
                all_samples = list(state.all_samples)

        if not all_samples:
            self._send_json({'error': 'no data available'})
            return

        # Expand inline frames before building flamegraph
        mapper = state.source_mapper
        expanded = mapper.expand_inline_frames(all_samples) if mapper else all_samples
        evt_samples = filter_samples_by_event(expanded, event_type)
        if not evt_samples:
            self._send_json({'error': f'no samples for event {event_type}'})
            return

        fg = build_flamegraph_data(evt_samples)
        total = len(evt_samples)
        svg = _render_flamegraph_svg(fg, total, event_type)

        body = svg.encode('utf-8')
        fname = f'perflens-flamegraph-{event_type}.svg'
        self.send_response(200)
        self.send_header('Content-Type', 'image/svg+xml')
        self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _handle_export_session(self, session_id, fmt):
        """Export session in collapsed or JSON format."""
        # Load session samples
        all_samples, metadata = _load_session_samples(session_id)
        if all_samples is None:
            # Try live data if session_id is 'live'
            if session_id == 'live':
                with state.lock:
                    all_samples = list(state.all_samples)
                    perf_stat = dict(state.perf_stat)
                if not all_samples:
                    self._send_json({'error': 'no live data'})
                    return
                event_types = get_event_types(all_samples)
                metadata = {
                    'session_id': 'live',
                    'total_samples': len(all_samples),
                    'event_types': event_types,
                    'perf_stat': perf_stat,
                }
            else:
                self._send_json({'error': 'session not found'})
                return

        if fmt == 'collapsed':
            text = _export_collapsed(all_samples)
            body = text.encode('utf-8')
            fname = f'perflens-{session_id}.collapsed'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Disposition',
                             f'attachment; filename="{fname}"')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)

        elif fmt == 'json':
            event_types = get_event_types(all_samples)
            mapper = state.source_mapper
            per_event = build_per_event_data(all_samples, event_types, mapper)
            export_data = {
                'metadata': metadata,
                'per_event': per_event,
            }
            body = json.dumps(export_data, indent=2).encode('utf-8')
            fname = f'perflens-{session_id}.json'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Disposition',
                             f'attachment; filename="{fname}"')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({'error': f'unknown format: {fmt}'})

    def _handle_import(self):
        """Handle POST /api/import — import a perf.data file."""
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length <= 0:
            self._send_json({'error': 'empty request body'}, 400)
            return
        if content_length > MAX_IMPORT_SIZE:
            self._send_json({
                'error': f'file too large ({content_length} bytes, '
                         f'max {MAX_IMPORT_SIZE // 1024 // 1024} MB)'
            }, 413)
            return
        if not config.perf_bin:
            self._send_json({
                'error': 'perf not found on server — cannot import perf.data'
            }, 500)
            return

        # Read upload into temp file
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(suffix='.data', delete=False)
            remaining = content_length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                tmp.write(chunk)
                remaining -= len(chunk)
            tmp.close()

            session_id, samples, metadata = import_perf_data(tmp.name)
            self._send_json({
                'session_id': session_id,
                'total_samples': len(samples),
                'event_types': metadata['event_types'],
            })
        except RuntimeError as e:
            self._send_json({'error': str(e)}, 500)
        except Exception as e:
            self._send_json({'error': f'import failed: {e}'}, 500)
        finally:
            if tmp and os.path.isfile(tmp.name):
                os.unlink(tmp.name)

    def _handle_sessions_list(self):
        """List all saved sessions."""
        sessions = []
        if os.path.isdir(config.sessions_dir):
            for name in sorted(os.listdir(config.sessions_dir), reverse=True):
                meta_path = os.path.join(config.sessions_dir, name,
                                         'metadata.json')
                if os.path.isfile(meta_path):
                    try:
                        with open(meta_path) as f:
                            meta = json.load(f)
                        sessions.append(meta)
                    except (json.JSONDecodeError, IOError):
                        pass
        self._send_json(sessions)

    def _handle_session_replay(self, session_id):
        """Rebuild session data on the fly from raw chunks."""
        session_dir = os.path.join(config.sessions_dir, session_id)
        meta_path = os.path.join(session_dir, 'metadata.json')

        if not os.path.isfile(meta_path):
            self._send_json({'error': 'session not found'})
            return

        with open(meta_path) as f:
            metadata = json.load(f)

        # Lazy rebuild: load raw chunks, parse, build per-event data
        all_samples = []
        chunk_files = sorted(
            f for f in os.listdir(session_dir)
            if f.startswith('chunk_') and f.endswith('.txt')
        )
        for fname in chunk_files:
            fpath = os.path.join(session_dir, fname)
            try:
                with open(fpath) as f:
                    text = f.read()
                script_text, _ = split_perf_data(text)
                samples = parse_perf_script(script_text)
                all_samples.extend(samples)
            except (IOError, OSError):
                pass

        event_types = get_event_types(all_samples)
        mapper = state.source_mapper
        per_event = build_per_event_data(all_samples, event_types, mapper,
                                         source=True)

        self._send_json({'metadata': metadata, 'per_event': per_event})

    def log_message(self, format, *args):
        if '/api/stream' not in str(args):
            super().log_message(format, *args)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def run_http_server(port):
    """Run the HTTP server for the web UI."""
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(('0.0.0.0', port), PerfLensHTTPHandler)
    print(f"[server] HTTP server on http://0.0.0.0:{port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global config, state, agent_session, wizard_state

    import argparse
    parser = argparse.ArgumentParser(description='PerfLens Server')
    parser.add_argument('--port', type=int, default=9999,
                        help='TCP port for agent connections (default: 9999)')
    parser.add_argument('--http-port', type=int, default=8080,
                        help='HTTP port for web UI (default: 8080)')
    parser.add_argument('--source-dir', type=str, default='.',
                        help='Path to source code directory')
    parser.add_argument('--binary', type=str, default=None,
                        help='Path to unstripped binary or .debug file')
    parser.add_argument('--map', type=str, default=None,
                        help='Path to linker map file')
    parser.add_argument('--path-map', type=str, default=None,
                        help='Compile-time path prefix mapping '
                             '(e.g., /build/src=/home/user/src)')
    parser.add_argument('--addr2line', type=str, default=None,
                        help='Path to custom addr2line binary')
    parser.add_argument('--max-samples', type=int, default=500000,
                        help='Max accumulated samples before oldest are dropped '
                             '(default: 500000)')
    parser.add_argument('--inline', action='store_true', default=True,
                        dest='inline',
                        help='Enable inline function resolution via '
                             'addr2line -i (default)')
    parser.add_argument('--no-inline', action='store_false', dest='inline',
                        help='Disable inline function resolution')
    parser.add_argument('--import', type=str, default=None, dest='import_file',
                        metavar='FILE',
                        help='Import a perf.data file at startup and make it '
                             'available as a session')
    args = parser.parse_args()

    # Parse path-map
    path_map = {}
    if args.path_map:
        for mapping in args.path_map.split(','):
            if '=' in mapping:
                src, dst = mapping.split('=', 1)
                path_map[src] = dst

    # Build config.
    #
    # Path resolution differs between development runs (script mode) and
    # PyInstaller frozen builds:
    #   - In frozen mode, `sys._MEIPASS` is the temporary directory where
    #     PyInstaller extracts bundled data (ui/, VERSION, etc.). The actual
    #     executable lives next to the sessions directory we want to write.
    #   - In script mode, __file__ is server/perflens_server.py and the ui/
    #     and sessions/ dirs are siblings of server/.
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        bundle_dir = sys._MEIPASS
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        bundle_dir = None
        base_dir = os.path.dirname(os.path.abspath(__file__))

    # Resolve the UI directory: PyInstaller bundle first, then alongside the
    # executable (extracted package), then the development layout.
    ui_candidates = []
    if bundle_dir:
        ui_candidates.append(os.path.join(bundle_dir, 'ui'))
    ui_candidates.append(os.path.join(base_dir, 'ui'))
    ui_candidates.append(os.path.join(base_dir, '..', 'ui'))

    ui_dir = None
    for candidate in ui_candidates:
        if os.path.isdir(candidate):
            ui_dir = os.path.abspath(candidate)
            break
    if ui_dir is None:
        ui_dir = os.path.abspath(ui_candidates[-1])

    # Sessions directory: next to the executable in frozen mode, sibling of
    # server/ in script mode.
    if bundle_dir:
        sessions_dir = os.path.abspath(os.path.join(base_dir, 'sessions'))
    else:
        sessions_dir = os.path.abspath(os.path.join(base_dir, '..', 'sessions'))

    config = ServerConfig(
        source_dir=os.path.abspath(args.source_dir),
        binary_path=os.path.abspath(args.binary) if args.binary else None,
        map_file_path=os.path.abspath(args.map) if args.map else None,
        addr2line_bin=args.addr2line,
        path_map=path_map or None,
        sessions_dir=sessions_dir,
        max_samples=args.max_samples,
        tcp_port=args.port,
        http_port=args.http_port,
        ui_dir=ui_dir,
        inline=args.inline,
    )

    os.makedirs(config.sessions_dir, exist_ok=True)

    if not os.path.isdir(config.ui_dir):
        print(f"[server] Warning: UI directory not found at {config.ui_dir}",
              file=sys.stderr)

    # Probe tools
    probe_tools(config)

    # Create shared SourceMapper (lives for the entire server lifetime)
    mapper = SourceMapper(
        config.source_dir,
        binary_path=config.binary_path,
        map_file_path=config.map_file_path,
        addr2line_bin=config.addr2line_bin,
        path_map=config.path_map or {},
        inline=config.inline,
    )

    # Create shared state
    state = ProfilingState(max_samples=config.max_samples)
    state.source_mapper = mapper

    # CLI import: parse perf.data at startup
    if args.import_file:
        import_path = os.path.abspath(args.import_file)
        if not os.path.isfile(import_path):
            print(f"[server] Error: import file not found: {import_path}",
                  file=sys.stderr)
            sys.exit(1)
        try:
            session_id, samples, metadata = import_perf_data(import_path)
            # Load into live state so UI shows data immediately
            state.add_samples(samples)
            print(f"[server] Imported {len(samples)} samples as session "
                  f"{session_id}", file=sys.stderr)
        except RuntimeError as e:
            print(f"[server] Import failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Start TCP server in a thread
    tcp_thread = threading.Thread(target=run_tcp_server,
                                  args=(config.tcp_port,), daemon=True)
    tcp_thread.start()

    # Run HTTP server in main thread
    run_http_server(config.http_port)


if __name__ == '__main__':
    main()
