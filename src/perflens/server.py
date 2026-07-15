#!/usr/bin/env python3
"""PerfLens Server — receives perf data from agents and serves web UI."""

import collections
import dataclasses
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

from perflens.parser import (parse_perf_script, split_perf_data,
                    get_event_types, parse_perf_stat, merge_perf_stat)
from perflens.aggregator import AggregatorSet, build_per_event_batch
from perflens.source_mapper import SourceMapper, build_annotated_source


# ---------------------------------------------------------------------------
# Server configuration
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ServerConfig:
    source_dir: str = '.'
    binary_path: str = None
    map_file_path: str = None
    addr2line_bin: str = None
    readelf_bin: str = None
    dwarfdump_bin: str = None
    zstd_bin: str = None
    perf_bin: str = None
    path_map: dict = None
    sysroot: str = None
    sessions_dir: str = ''
    max_samples: int = 500000
    tcp_port: int = 9999
    http_port: int = 8080
    http_bind: str = '127.0.0.1'
    browse_root: str = ''
    token: str = None
    ui_dir: str = ''
    inline: bool = True


# ---------------------------------------------------------------------------
# Profiling state
# ---------------------------------------------------------------------------

class ProfilingState:
    """Shared state for streaming data to UI. Thread-safe."""

    def __init__(self, max_samples=500000):
        self.lock = threading.Lock()
        self.all_samples = collections.deque(maxlen=max_samples)
        self.chunk_count = 0
        self.last_update = 0
        self.agent_connected = False
        self.agent_addr = None
        self.agent_conn = None     # active agent socket, for /api/stop
        self.event_types = []
        self.perf_stat = {}
        self.source_mapper = None  # SourceMapper, set at startup
        # Running set — event types only accumulate, never removed
        # (event types don't change mid-session in practice)
        self._event_types_set = set()
        # Incremental per-event aggregation. Chunks queue here and the
        # rebuild worker folds them in off the recv thread; accumulators
        # cover the whole session (the deque above is only the raw window
        # backing thread/source drill-downs).
        self.aggregators = AggregatorSet()
        self._pending_chunks = []
        # Background rebuild state
        self._rebuild_needed = threading.Condition(self.lock)
        self._dirty = False
        self._cached_per_event = {}

    def add_samples(self, new_samples, perf_stat=None):
        """Add samples and return (total_count, event_types_copy)."""
        with self.lock:
            self.all_samples.extend(new_samples)
            self.chunk_count += 1
            self.last_update = time.time()
            self._event_types_set.update(
                s['event_type'] for s in new_samples)
            self.event_types = sorted(self._event_types_set)
            if perf_stat:
                self.perf_stat = merge_perf_stat(self.perf_stat, perf_stat)
            self._pending_chunks.append(new_samples)
            self._dirty = True
            self._rebuild_needed.notify()
            return len(self.all_samples), list(self.event_types)

    def get_snapshot(self):
        with self.lock:
            return {
                'all_samples': list(self.all_samples),
                'event_types': list(self.event_types),
                'perf_stat': dict(self.perf_stat),
            }

    def reset(self):
        with self.lock:
            self.all_samples.clear()
            self.chunk_count = 0
            self.event_types = []
            self._event_types_set.clear()
            self.perf_stat = {}
            self._pending_chunks = []
            self._cached_per_event = {}
            self._dirty = False
        self.aggregators.reset()


class MetricsState:
    """Stores device health metrics history in memory. Thread-safe."""

    def __init__(self, max_entries=1800):
        # 1800 entries = 1 hour at 2-second intervals
        self.lock = threading.Lock()
        self.system_history = []
        self.process_history = []
        self.network_history = []
        self._max = max_entries

    def add(self, metrics_type, metrics):
        with self.lock:
            if metrics_type == 'system':
                self._append(self.system_history, metrics)
            elif metrics_type == 'process':
                self._append(self.process_history, metrics)
            elif metrics_type == 'network':
                self._append(self.network_history, metrics)

    def _append(self, history, entry):
        history.append(entry)
        if len(history) > self._max:
            trim = self._max // 10
            del history[:trim]

    def get_history(self, metrics_type, start_ts=None, end_ts=None):
        with self.lock:
            if metrics_type == 'system':
                source = self.system_history
            elif metrics_type == 'process':
                source = self.process_history
            elif metrics_type == 'network':
                source = self.network_history
            else:
                return []
            if start_ts is None and end_ts is None:
                return list(source)
            return [m for m in source
                    if (start_ts is None or m.get('ts', 0) >= start_ts) and
                       (end_ts is None or m.get('ts', 0) <= end_ts)]

    def get_latest(self):
        with self.lock:
            result = {}
            if self.system_history:
                result['system'] = self.system_history[-1]
            if self.process_history:
                result['process'] = self.process_history[-1]
            if self.network_history:
                result['network'] = self.network_history[-1]
            return result

    def get_summary(self):
        """Compute summary stats for session metadata."""
        with self.lock:
            if not self.system_history:
                return None
            cpu_vals = [m['cpu']['overall_pct'] for m in self.system_history
                        if m.get('cpu', {}).get('overall_pct') is not None]
            mem_vals = [m['mem']['used_pct'] for m in self.system_history
                        if m.get('mem', {}).get('used_pct') is not None]
            temp_vals = [m['temp_c'] for m in self.system_history
                         if m.get('temp_c') is not None]
            n = len(self.system_history)
            summary = {'snapshots': n}
            if n >= 2:
                summary['duration_sec'] = round(
                    self.system_history[-1].get('ts', 0) -
                    self.system_history[0].get('ts', 0), 1)
            if cpu_vals:
                summary['avg_cpu_pct'] = round(sum(cpu_vals) / len(cpu_vals), 1)
                summary['max_cpu_pct'] = round(max(cpu_vals), 1)
            if mem_vals:
                summary['avg_mem_pct'] = round(sum(mem_vals) / len(mem_vals), 1)
                summary['max_mem_pct'] = round(max(mem_vals), 1)
            if temp_vals:
                summary['avg_temp_c'] = round(sum(temp_vals) / len(temp_vals))
                summary['max_temp_c'] = max(temp_vals)
            return summary

    def snapshot_for_save(self):
        """Return all metrics for session save."""
        with self.lock:
            return {
                'system': list(self.system_history),
                'process': list(self.process_history),
                'network': list(self.network_history),
            }

    def reset(self):
        with self.lock:
            self.system_history.clear()
            self.process_history.clear()
            self.network_history.clear()


# Wire protocol flags (must match agent)
FLAG_DATA_RAW = 0
FLAG_DATA_ZSTD = 1
FLAG_CMD_REQUEST = 2
FLAG_CMD_RESPONSE = 3
FLAG_METRICS = 4

# Module-level instances, set in main()
config: ServerConfig = None
state: ProfilingState = None
metrics_state: MetricsState = None
agent_session: 'AgentSession' = None  # managed agent connection
agent_session_lock = threading.Lock()  # guards agent_session swaps
wizard_state: dict = None              # wizard UI state


# ---------------------------------------------------------------------------
# Startup probing
# ---------------------------------------------------------------------------

def _perflens_bin_dir():
    from perflens.provision import bin_dir
    return bin_dir()


def _find_binary(name, download=False):
    """Find a binary: PATH, then ~/.perflens/bin, then (for addr2line/
    readelf, when download=True) the static tools bundle from the release."""
    from perflens.provision import resolve_tool
    path, _origin = resolve_tool(name, download=download)
    return path


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
        # Prefer llvm-addr2line: GNU-compatible flags, but dramatically
        # faster on GB-scale DWARF (lazy index vs full scan). If neither
        # variant is on PATH or in ~/.perflens/bin, try downloading the
        # static tools bundle from the release (user-space, sha256-checked).
        found = (_find_binary('llvm-addr2line')
                 or _find_binary('addr2line', download=True))
        if found:
            cfg.addr2line_bin = found
            label = ('provisioned' if found.startswith(_perflens_bin_dir())
                     else 'system')
            print(f"[server]   addr2line: {found} ({label})", file=sys.stderr)
        else:
            cfg.addr2line_bin = None
            print("[server]   addr2line: NOT FOUND (source mapping disabled "
                  "— run `perflens provision`, install binutils, or pass "
                  "--addr2line)", file=sys.stderr)

    # llvm-dwarfdump (optional): fast DWARF source-file listing
    found = _find_binary('llvm-dwarfdump')
    if found:
        cfg.dwarfdump_bin = found
        print(f"[server]   llvm-dwarfdump: {found}", file=sys.stderr)

    # readelf
    if cfg.readelf_bin and os.path.isfile(cfg.readelf_bin):
        print(f"[server]   readelf: {cfg.readelf_bin} (user-provided)",
              file=sys.stderr)
    elif cfg.readelf_bin:
        # Toolchain-derived path — check if it exists on PATH
        try:
            r = subprocess.run(['which', cfg.readelf_bin], capture_output=True,
                               text=True, timeout=5)
            if r.returncode == 0:
                cfg.readelf_bin = r.stdout.strip()
                print(f"[server]   readelf: {cfg.readelf_bin} (toolchain)",
                      file=sys.stderr)
            else:
                print(f"[server]   readelf: {cfg.readelf_bin} (NOT FOUND, "
                      f"falling back to system)", file=sys.stderr)
                cfg.readelf_bin = None
        except Exception:
            cfg.readelf_bin = None
    if not cfg.readelf_bin:
        found = _find_binary('readelf', download=True)
        if found:
            cfg.readelf_bin = found
            label = ('provisioned' if found.startswith(_perflens_bin_dir())
                     else 'system')
            print(f"[server]   readelf: {found} ({label})", file=sys.stderr)
        else:
            cfg.readelf_bin = 'readelf'  # fallback, may fail at runtime
            print("[server]   readelf: NOT FOUND (using 'readelf' fallback "
                  "— run `perflens provision` or install binutils)",
                  file=sys.stderr)

    # sysroot
    if cfg.sysroot:
        if os.path.isdir(cfg.sysroot):
            print(f"[server]   sysroot: {cfg.sysroot}", file=sys.stderr)
        else:
            print(f"[server]   sysroot: {cfg.sysroot} (NOT FOUND)",
                  file=sys.stderr)
            cfg.sysroot = None

    # zstd (fallback only — decompression is in-process via zstandard)
    found = _find_binary('zstd')
    if found:
        cfg.zstd_bin = found
        print(f"[server]   zstd: {found}", file=sys.stderr)
    else:
        cfg.zstd_bin = None
        if _zstd is None:
            print("[server]   zstd: NOT FOUND and zstandard module missing "
                  "(compressed payloads will fail)", file=sys.stderr)

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


def _create_source_mapper():
    """Create a SourceMapper from current config. Used at startup and
    reconfiguration. Loads any persisted source index instantly and kicks
    a background refresh — request paths never walk the source tree."""
    mapper = SourceMapper(
        config.source_dir,
        binary_path=config.binary_path,
        map_file_path=config.map_file_path,
        addr2line_bin=config.addr2line_bin,
        readelf_bin=config.readelf_bin,
        path_map=config.path_map or {},
        inline=config.inline,
        sysroot=config.sysroot,
        dwarfdump_bin=config.dwarfdump_bin,
    )
    mapper.start_background_index()
    return mapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# In-process zstd (the zstandard wheel ships with the package); the
# external `zstd` binary remains as a fallback for source checkouts run
# without installed dependencies.
try:
    import zstandard as _zstd
except ImportError:
    _zstd = None


def decompress_payload(payload, comp_flag):
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
                mode = 'wb'
            else:
                fname = f'chunk_{self._chunk_index:05d}.txt'
                mode = 'wb'
            with open(os.path.join(self._session_dir, fname), mode) as f:
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
                    text = decompress_payload(payload, flag)
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
                    broadcast_sse('event_types', event_types)
                    if perf_stat:
                        # Broadcast the accumulated stat, not this round's
                        with state.lock:
                            merged_stat = dict(state.perf_stat)
                        broadcast_sse('perf_stat', merged_stat)

                elif flag == FLAG_METRICS:
                    # Health metrics snapshot
                    try:
                        metrics = json.loads(payload.decode('utf-8',
                                                            errors='replace'))
                        mtype = metrics.get('type', '')
                        metrics_state.add(mtype, metrics)
                        broadcast_sse('metrics_%s' % mtype, metrics)
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
            state.agent_connected = False
            state.agent_conn = None
            all_samples = list(state.all_samples)
            perf_stat_final = dict(state.perf_stat)
        broadcast_sse('status', {'connected': False, 'agent': None})

        # Save session metadata (chunks are already spooled to disk)
        m_snap = metrics_state.snapshot_for_save()
        m_summary = metrics_state.get_summary()
        if self._chunk_index or any(m_snap.values()):
            t = threading.Thread(
                target=_save_session,
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


def _check_agent_token(hello):
    """Validate the hello token against config.token (if configured).

    Returns None when accepted, or an error string when rejected.
    """
    if not config or not config.token:
        return None
    import hmac
    presented = hello.get('token') or ''
    if not hmac.compare_digest(str(presented), config.token):
        return 'agent token mismatch'
    return None


def _install_agent_session(session):
    """Register a new AgentSession as THE managed agent (replacing any
    existing one), reset profiling state, and start its receiver.

    Serialized by agent_session_lock so two near-simultaneous connections
    can't interleave the check-close-replace sequence.
    """
    global agent_session

    with agent_session_lock:
        if agent_session and agent_session.connected:
            print("[server] Replacing existing agent session", file=sys.stderr)
            agent_session.close()

        with state.lock:
            state.agent_connected = True
            state.agent_addr = session.addr
            state.agent_conn = session.sock
            state.all_samples.clear()
            state.chunk_count = 0
            state._event_types_set.clear()
            state.event_types = []
            state.perf_stat = {}
            state._pending_chunks = []
            state._cached_per_event = {}
        state.aggregators.reset()
        metrics_state.reset()

        agent_session = session
        session.start()

    broadcast_sse('status', {'connected': True, 'agent': session.addr})
    broadcast_sse('agent_connected', {
        'agent': session.addr,
        'platform': (session.hello or {}).get('platform', {}),
    })


def current_agent_session():
    """Return the managed AgentSession (or None). Thread-safe."""
    with agent_session_lock:
        return agent_session


def stop_agent():
    """Close the agent connection, triggering the normal disconnect flow.
    Returns the /api/stop response dict."""
    global agent_session
    with agent_session_lock:
        session = agent_session
        agent_session = None
    if session and session.connected:
        try:
            session.send_command('stop', timeout=5)
        except Exception:
            pass
        session.close()
        return {'stopped': True}
    return {'stopped': False, 'reason': 'no agent connected'}


def connect_to_agent(host, port, timeout=10):
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

    token_err = _check_agent_token(hello)
    if token_err:
        sock.close()
        raise RuntimeError(token_err)

    # Clear connection timeout — recv loop must block indefinitely
    sock.settimeout(None)

    addr_str = f'{host}:{port}'
    session = AgentSession(sock, addr_str)
    session.hello = hello
    _install_agent_session(session)

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

# The HTTP layer (perflens.web) registers its fan-out here at startup.
# Broadcasts before registration (or with no browsers connected) are no-ops.
_sse_sinks = []


def register_sse_sink(fn):
    """Register a callable(event_type, data) that delivers SSE events.
    Must be safe to call from any thread."""
    _sse_sinks.append(fn)


def broadcast_sse(event_type, data):
    """Send an SSE event to all connected browsers. Thread-safe."""
    for fn in list(_sse_sinks):
        try:
            fn(event_type, data)
        except Exception as e:
            print(f"[server] SSE sink error: {e}", file=sys.stderr)


def _rebuild_worker():
    """Background thread: folds newly arrived chunks into the incremental
    per-event accumulators and broadcasts fresh snapshots.

    Only the NEW chunks are processed (inline expansion + addr2line included)
    — cost per wakeup is O(new samples), not O(all samples). Coalesces rapid
    updates: chunks that arrive while folding are picked up next loop.
    """
    while True:
        with state.lock:
            while not state._dirty:
                state._rebuild_needed.wait()
            state._dirty = False
            pending = state._pending_chunks
            state._pending_chunks = []

        if not pending:
            continue

        mapper = state.source_mapper
        for chunk in pending:
            state.aggregators.add_chunk(chunk, mapper)
        per_event = state.aggregators.snapshot_per_event(mapper)

        with state.lock:
            state._cached_per_event = per_event
            version = {
                'chunk_count': state.chunk_count,
                'total_samples': len(state.all_samples),
                'event_types': list(state.event_types),
            }

        # Notify-and-fetch: browsers get a tiny version stamp and pull the
        # event they're actually viewing from /api/per-event — the full
        # per-event blob (multi-MB on big profiles) is never broadcast.
        broadcast_sse('data_version', version)


# ---------------------------------------------------------------------------
# Agent connection handler
# ---------------------------------------------------------------------------

def handle_inbound_agent(conn, addr):
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

    token_err = _check_agent_token(hello)
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

    session = AgentSession(conn, addr_str)
    session.hello = hello
    _install_agent_session(session)

    print(f"[server] Inbound agent {addr_str} ready: "
          f"platform={hello.get('platform', {}).get('arch', '?')}",
          file=sys.stderr)


def _save_session(session_dir, session_id, agent_addr, chunk_count,
                  all_samples, perf_stat, hello=None,
                  metrics_snapshot=None, metrics_summary=None):
    """Save session metadata + metrics (chunks are spooled at receive time)."""
    try:
        event_types = get_event_types(all_samples)
        metadata = {
            'version': '0.5.0',
            'session_id': session_id,
            'agent': agent_addr,
            'timestamp': datetime.now().isoformat(),
            'total_samples': len(all_samples),
            'chunks': chunk_count,
            'event_types': event_types,
            'perf_stat': perf_stat,
        }
        if hello and hello.get('platform'):
            metadata['platform'] = hello['platform']
        if metrics_summary:
            metadata['metrics_summary'] = metrics_summary

        with open(os.path.join(session_dir, 'metadata.json'), 'w') as f:
            json.dump(metadata, f, indent=2)

        # Save metrics history
        if metrics_snapshot:
            with open(os.path.join(session_dir, 'metrics.json'), 'w') as f:
                json.dump(metrics_snapshot, f)

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
        raise RuntimeError(
            'perf script timed out (file too large?)') from None
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
        raise RuntimeError(
            'perf script timed out (file too large?)') from None


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
    """Build per-event data dict for UI consumption (batch: replay/import).

    Runs through the same AggregatorSet code path as live streaming, fed in
    one shot. `event_types` is accepted for backward compatibility; the
    events are derived from the samples themselves.
    """
    source_builder = None
    if source:
        if mapper:
            def source_builder(evt_orig):
                return build_annotated_source(mapper, evt_orig)
        else:
            def source_builder(evt_orig):
                return {}
    return build_per_event_batch(all_samples, mapper,
                                 source_builder=source_builder)


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def _safe_session_dir(session_id):
    """Resolve a session id (from a URL) to its directory, or None if the
    id would escape sessions_dir."""
    root = os.path.realpath(config.sessions_dir)
    session_dir = os.path.realpath(os.path.join(root, session_id))
    if session_dir == root or not session_dir.startswith(root + os.sep):
        return None
    return session_dir


def _session_chunk_files(session_dir):
    """List a session's chunk files (.txt or .zst) in chunk order.

    Sorted numerically by chunk index so old 3-digit and new 5-digit
    names both order correctly.
    """
    def chunk_key(fname):
        stem = fname[len('chunk_'):].split('.', 1)[0]
        try:
            return int(stem)
        except ValueError:
            return 0

    return sorted(
        (f for f in os.listdir(session_dir)
         if f.startswith('chunk_') and (f.endswith('.txt')
                                        or f.endswith('.zst'))),
        key=chunk_key)


def _read_session_chunk(fpath):
    """Read one chunk file, decompressing .zst spools. Returns text or None."""
    try:
        with open(fpath, 'rb') as f:
            payload = f.read()
    except (IOError, OSError):
        return None
    if fpath.endswith('.zst'):
        return decompress_payload(payload, FLAG_DATA_ZSTD)
    return payload.decode('utf-8', errors='replace')


def _load_session_chunks(session_dir):
    """Parse every chunk of a session into one sample list."""
    all_samples = []
    for fname in _session_chunk_files(session_dir):
        text = _read_session_chunk(os.path.join(session_dir, fname))
        if text is None:
            continue
        script_text, _ = split_perf_data(text)
        all_samples.extend(parse_perf_script(script_text))
    return all_samples


def _load_session_samples(session_id):
    """Load all samples from a saved session. Returns (samples, metadata) or (None, None)."""
    session_dir = _safe_session_dir(session_id)
    if session_dir is None:
        return None, None
    meta_path = os.path.join(session_dir, 'metadata.json')
    if not os.path.isfile(meta_path):
        return None, None

    with open(meta_path) as f:
        metadata = json.load(f)

    return _load_session_chunks(session_dir), metadata


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
        lines.append('<g>')
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
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    global config, state, metrics_state, agent_session, wizard_state

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
    parser.add_argument('--readelf', type=str, default=None,
                        help='Path to custom readelf binary')
    parser.add_argument('--toolchain-prefix', type=str, default=None,
                        help='Cross-toolchain prefix '
                             '(e.g., arm-linux-gnueabihf- or '
                             '/opt/toolchain/bin/aarch64-linux-gnu-). '
                             'Derives addr2line and readelf from prefix.')
    parser.add_argument('--sysroot', type=str, default=None,
                        help='Target sysroot directory for resolving '
                             'shared libraries and source files '
                             '(like perf --symfs)')
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
    parser.add_argument('--http-bind', type=str, default='127.0.0.1',
                        metavar='ADDR',
                        help='Bind address for the web UI (default: '
                             '127.0.0.1; use 0.0.0.0 to expose it — the UI '
                             'has no authentication)')
    parser.add_argument('--browse-root', type=str, default=None,
                        metavar='DIR',
                        help='Directory the /api/browse file picker is '
                             'confined to (default: your home directory)')
    parser.add_argument('--token', type=str,
                        default=os.environ.get('PERFLENS_TOKEN'),
                        help='Shared secret agents must present in their '
                             'hello (agents pass --token / PERFLENS_TOKEN); '
                             'connections without it are rejected')
    parser.add_argument('--sessions-dir', type=str, default=None,
                        metavar='DIR',
                        help='Where to save profiling sessions '
                             '(default: ~/.perflens/sessions)')
    args = parser.parse_args(argv)

    # Parse path-map
    path_map = {}
    if args.path_map:
        for mapping in args.path_map.split(','):
            if '=' in mapping:
                src, dst = mapping.split('=', 1)
                path_map[src] = dst

    # Toolchain prefix: derive addr2line and readelf from prefix
    if args.toolchain_prefix:
        prefix = args.toolchain_prefix
        if not args.addr2line:
            args.addr2line = prefix + 'addr2line'
        if not args.readelf:
            args.readelf = prefix + 'readelf'
        print(f"[server] Toolchain prefix: {prefix}", file=sys.stderr)
    elif args.addr2line and not args.readelf:
        # Infer readelf from addr2line path (same directory, same prefix)
        a2l = args.addr2line
        if 'addr2line' in os.path.basename(a2l):
            inferred = a2l.replace('addr2line', 'readelf')
            if os.path.isfile(inferred):
                args.readelf = inferred

    # UI ships inside the package (src/perflens/ui) — resolve it via
    # importlib.resources so both `pip install` and repo checkouts work.
    from importlib.resources import files as _pkg_files
    ui_dir = os.fspath(_pkg_files('perflens') / 'ui')

    # Sessions live under ~/.perflens (override root with PERFLENS_HOME,
    # or the directory itself with --sessions-dir).
    if args.sessions_dir:
        sessions_dir = os.path.abspath(args.sessions_dir)
    else:
        from perflens.symcache import perflens_home
        sessions_dir = os.path.join(perflens_home(), 'sessions')

    config = ServerConfig(
        source_dir=os.path.abspath(args.source_dir),
        binary_path=os.path.abspath(args.binary) if args.binary else None,
        map_file_path=os.path.abspath(args.map) if args.map else None,
        addr2line_bin=args.addr2line,
        readelf_bin=args.readelf,
        path_map=path_map or None,
        sysroot=os.path.abspath(args.sysroot) if args.sysroot else None,
        sessions_dir=sessions_dir,
        max_samples=args.max_samples,
        tcp_port=args.port,
        http_port=args.http_port,
        http_bind=args.http_bind,
        browse_root=os.path.abspath(args.browse_root)
                    if args.browse_root else os.path.expanduser('~'),
        token=args.token,
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
    mapper = _create_source_mapper()

    # Create shared state
    state = ProfilingState(max_samples=config.max_samples)
    state.source_mapper = mapper
    metrics_state = MetricsState()

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

    # Start background rebuild worker (builds per_event data off recv thread)
    rebuild_thread = threading.Thread(target=_rebuild_worker, daemon=True)
    rebuild_thread.start()

    # Start TCP server in a thread
    tcp_thread = threading.Thread(target=run_tcp_server,
                                  args=(config.tcp_port,), daemon=True)
    tcp_thread.start()

    # Run HTTP server in main thread
    # Run the HTTP server (FastAPI/uvicorn) in the main thread. Imported
    # here so config/state are fully initialized before the app is built.
    from perflens.web import run_http_server
    run_http_server(config.http_port, config.http_bind)


if __name__ == '__main__':
    main()
