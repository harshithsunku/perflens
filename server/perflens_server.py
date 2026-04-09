#!/usr/bin/env python3
"""PerfLens Server — receives perf data from agents and serves web UI."""

import dataclasses
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
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
    path_map: dict = None
    sessions_dir: str = ''
    max_samples: int = 500000
    tcp_port: int = 9999
    http_port: int = 8080
    ui_dir: str = ''


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


# Module-level instances, set in main()
config: ServerConfig = None
state: ProfilingState = None


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

def handle_agent_connection(conn, addr):
    """Handle a single agent connection, reading 5-byte header messages."""
    print(f"[server] Agent connected from {addr}", file=sys.stderr)

    with state.lock:
        state.agent_connected = True
        state.agent_addr = f"{addr[0]}:{addr[1]}"
        state.all_samples = []
        state.chunk_count = 0

    broadcast_sse('status', {'connected': True, 'agent': f"{addr[0]}:{addr[1]}"})

    mapper = state.source_mapper

    # Create session directory
    session_id = datetime.now().strftime('%Y%m%d_%H%M%S') + f'_{addr[0]}'
    session_dir = os.path.join(config.sessions_dir, session_id)
    os.makedirs(session_dir, exist_ok=True)
    raw_chunks = []

    try:
        while True:
            # 5-byte header: 4 bytes length + 1 byte compression flag
            header = recv_exactly(conn, 5)
            if header is None:
                print(f"[server] Agent {addr} disconnected", file=sys.stderr)
                break

            length, comp_flag = struct.unpack('!IB', header)
            if length == 0:
                continue

            payload = recv_exactly(conn, length)
            if payload is None:
                print(f"[server] Agent {addr} disconnected mid-message",
                      file=sys.stderr)
                break

            # Decompress if needed
            text = decompress_payload(payload, comp_flag)
            if text is None:
                continue

            raw_chunks.append(text)

            # Split perf script and perf stat data
            script_text, stat_text = split_perf_data(text)

            # Parse this chunk
            samples = parse_perf_script(script_text)
            perf_stat = parse_perf_stat(stat_text) if stat_text else {}

            # Skip empty chunks
            if not samples:
                print(f"[server] WARNING: chunk parsed to 0 samples, skipping",
                      file=sys.stderr)
                continue

            all_samples, event_types = state.add_samples(samples, perf_stat)

            print(f"[server] Chunk {state.chunk_count}: "
                  f"{len(samples)} new samples, {len(all_samples)} total, "
                  f"events: {event_types}", file=sys.stderr)

            # Build per-event summaries
            per_event = {}
            for evt in event_types:
                evt_samples = filter_samples_by_event(all_samples, evt)
                per_event[evt] = {
                    'function_summary': build_function_summary(evt_samples),
                    'flamegraph': build_flamegraph_data(evt_samples),
                    'source_files': mapper.get_files_with_samples(evt_samples)
                                    if mapper else [],
                }

            # Broadcast to UI
            broadcast_sse('event_types', event_types)
            broadcast_sse('per_event', per_event)
            if perf_stat:
                broadcast_sse('perf_stat', perf_stat)

    except ConnectionResetError:
        print(f"[server] Agent {addr} connection reset", file=sys.stderr)
    finally:
        conn.close()
        with state.lock:
            state.agent_connected = False
            all_samples = list(state.all_samples)
            perf_stat_final = dict(state.perf_stat)
        broadcast_sse('status', {'connected': False, 'agent': None})

        # Save session in background thread
        t = threading.Thread(
            target=_save_session,
            args=(session_dir, session_id, addr, raw_chunks,
                  all_samples, perf_stat_final),
            daemon=True,
        )
        t.start()


def _save_session(session_dir, session_id, addr, raw_chunks,
                  all_samples, perf_stat):
    """Save profiling session to disk (raw chunks + metadata only)."""
    try:
        for i, chunk in enumerate(raw_chunks):
            with open(os.path.join(session_dir, f'chunk_{i:03d}.txt'), 'w') as f:
                f.write(chunk)

        event_types = get_event_types(all_samples)
        metadata = {
            'session_id': session_id,
            'agent': f"{addr[0]}:{addr[1]}",
            'timestamp': datetime.now().isoformat(),
            'total_samples': len(all_samples),
            'chunks': len(raw_chunks),
            'event_types': event_types,
            'perf_stat': perf_stat,
        }
        with open(os.path.join(session_dir, 'metadata.json'), 'w') as f:
            json.dump(metadata, f, indent=2)

        print(f"[server] Session saved: {session_id} ({len(all_samples)} samples)",
              file=sys.stderr)
    except Exception as e:
        print(f"[server] Error saving session: {e}", file=sys.stderr)


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
            t = threading.Thread(target=handle_agent_connection,
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
        elif path == '/api/stream':
            self._handle_sse()
        elif path == '/api/sessions':
            self._handle_sessions_list()
        elif path.startswith('/api/sessions/'):
            session_id = path.split('/api/sessions/')[1].rstrip('/')
            self._handle_session_replay(session_id)
        elif path == '/api/source':
            params = parse_qs(parsed.query)
            file_path = params.get('file', [None])[0]
            self._handle_source_request(file_path)
        else:
            # Serve static files from UI directory
            if path == '/':
                path = '/index.html'
            file_path = os.path.join(config.ui_dir, path.lstrip('/'))
            if os.path.isfile(file_path):
                self._serve_file(file_path)
            else:
                self.send_error(404)

    def _send_json(self, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

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
            per_event = {}
            for evt in event_types:
                evt_samples = filter_samples_by_event(all_samples, evt)
                per_event[evt] = {
                    'function_summary': build_function_summary(evt_samples),
                    'flamegraph': build_flamegraph_data(evt_samples),
                    'source_files': mapper.get_files_with_samples(evt_samples)
                                    if mapper else [],
                }

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

        # Check for legacy pre-computed per_event.json
        legacy_path = os.path.join(session_dir, 'per_event.json')
        if os.path.isfile(legacy_path):
            with open(legacy_path) as f:
                per_event = json.load(f)
            self._send_json({'metadata': metadata, 'per_event': per_event})
            return

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

        per_event = {}
        for evt in event_types:
            evt_samples = filter_samples_by_event(all_samples, evt)
            entry = {
                'function_summary': build_function_summary(evt_samples),
                'flamegraph': build_flamegraph_data(evt_samples),
            }
            if mapper:
                entry['source_files'] = mapper.get_files_with_samples(evt_samples)
                entry['source'] = build_annotated_source(mapper, evt_samples)
            else:
                entry['source_files'] = []
                entry['source'] = {}
            per_event[evt] = entry

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
    global config, state

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
    args = parser.parse_args()

    # Parse path-map
    path_map = {}
    if args.path_map:
        for mapping in args.path_map.split(','):
            if '=' in mapping:
                src, dst = mapping.split('=', 1)
                path_map[src] = dst

    # Build config
    base_dir = os.path.dirname(os.path.abspath(__file__))

    config = ServerConfig(
        source_dir=os.path.abspath(args.source_dir),
        binary_path=os.path.abspath(args.binary) if args.binary else None,
        map_file_path=os.path.abspath(args.map) if args.map else None,
        addr2line_bin=args.addr2line,
        path_map=path_map or None,
        sessions_dir=os.path.abspath(os.path.join(base_dir, '..', 'sessions')),
        max_samples=args.max_samples,
        tcp_port=args.port,
        http_port=args.http_port,
        ui_dir=os.path.abspath(os.path.join(base_dir, '..', 'ui')),
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
    )

    # Create shared state
    state = ProfilingState(max_samples=config.max_samples)
    state.source_mapper = mapper

    # Start TCP server in a thread
    tcp_thread = threading.Thread(target=run_tcp_server,
                                  args=(config.tcp_port,), daemon=True)
    tcp_thread.start()

    # Run HTTP server in main thread
    run_http_server(config.http_port)


if __name__ == '__main__':
    main()
