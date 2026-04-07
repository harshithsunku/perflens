#!/usr/bin/env python3
"""PerfLens Server - receives perf data from agents and serves web UI."""

import argparse
import json
import os
import socket
import struct
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from parser import parse_perf_script, build_function_summary, build_flamegraph_data
from source_mapper import SourceMapper, build_annotated_source

# Shared state for streaming data to UI
profiling_state = {
    'lock': threading.Lock(),
    'all_samples': [],       # accumulated samples across chunks
    'chunk_count': 0,
    'last_update': 0,
    'agent_connected': False,
    'agent_addr': None,
    'sse_clients': [],       # list of (wfile, lock) for SSE
}

server_config = {
    'source_dir': '.',
    'binary_path': None,
}


def recv_exactly(conn, n):
    """Receive exactly n bytes from a socket."""
    data = b''
    while len(data) < n:
        chunk = conn.recv(min(65536, n - len(data)))
        if not chunk:
            return None
        data += chunk
    return data


def broadcast_sse(event_type, data):
    """Send an SSE event to all connected browsers."""
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    encoded = msg.encode('utf-8')
    state = profiling_state

    dead_clients = []
    with state['lock']:
        clients = list(state['sse_clients'])

    for i, (wfile, wlock) in enumerate(clients):
        try:
            with wlock:
                wfile.write(encoded)
                wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            dead_clients.append(i)

    if dead_clients:
        with state['lock']:
            for i in sorted(dead_clients, reverse=True):
                if i < len(state['sse_clients']):
                    state['sse_clients'].pop(i)


def handle_agent_connection(conn, addr):
    """Handle a single agent connection, reading length-prefixed messages."""
    state = profiling_state
    print(f"[server] Agent connected from {addr}")

    with state['lock']:
        state['agent_connected'] = True
        state['agent_addr'] = f"{addr[0]}:{addr[1]}"
        state['all_samples'] = []
        state['chunk_count'] = 0

    broadcast_sse('status', {'connected': True, 'agent': f"{addr[0]}:{addr[1]}"})

    mapper = SourceMapper(
        server_config['source_dir'],
        server_config.get('binary_path')
    )

    try:
        while True:
            header = recv_exactly(conn, 4)
            if header is None:
                print(f"[server] Agent {addr} disconnected")
                break

            length = struct.unpack('!I', header)[0]
            if length == 0:
                continue

            payload = recv_exactly(conn, length)
            if payload is None:
                print(f"[server] Agent {addr} disconnected mid-message")
                break

            text = payload.decode('utf-8')

            # Parse this chunk
            samples = parse_perf_script(text)
            with state['lock']:
                state['all_samples'].extend(samples)
                state['chunk_count'] += 1
                state['last_update'] = time.time()
                all_samples = list(state['all_samples'])

            # Build summaries from accumulated data
            summary = build_function_summary(all_samples)
            flamegraph = build_flamegraph_data(all_samples)
            annotated = build_annotated_source(mapper, all_samples)

            # Build source data for UI
            source_data = {}
            for fpath, lines in annotated.items():
                source_data[fpath] = lines

            print(f"[server] Chunk {state['chunk_count']}: "
                  f"{len(samples)} new samples, {len(all_samples)} total")

            # Broadcast to UI
            broadcast_sse('function_summary', summary)
            broadcast_sse('flamegraph', flamegraph)
            broadcast_sse('source', source_data)

    except ConnectionResetError:
        print(f"[server] Agent {addr} connection reset")
    finally:
        conn.close()
        with state['lock']:
            state['agent_connected'] = False
        broadcast_sse('status', {'connected': False, 'agent': None})


def run_tcp_server(port):
    """Run the TCP server that accepts agent connections."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', port))
    sock.listen(5)
    print(f"[server] TCP server listening on port {port}")

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


class PerfLensHTTPHandler(SimpleHTTPRequestHandler):
    """HTTP handler for serving UI and API endpoints."""

    ui_dir = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/status':
            self._send_json({
                'status': 'ok',
                'agent_connected': profiling_state['agent_connected'],
                'agent_addr': profiling_state['agent_addr'],
                'total_samples': len(profiling_state['all_samples']),
                'chunk_count': profiling_state['chunk_count'],
            })
        elif path == '/api/stream':
            self._handle_sse()
        elif path == '/api/source':
            params = parse_qs(parsed.query)
            file_path = params.get('file', [None])[0]
            self._handle_source_request(file_path)
        else:
            # Serve static files from UI directory
            if path == '/':
                path = '/index.html'
            file_path = os.path.join(self.ui_dir, path.lstrip('/'))
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
        with profiling_state['lock']:
            profiling_state['sse_clients'].append(client)

        # Send current state immediately
        state = profiling_state
        with state['lock']:
            all_samples = list(state['all_samples'])

        if all_samples:
            mapper = SourceMapper(
                server_config['source_dir'],
                server_config.get('binary_path')
            )
            summary = build_function_summary(all_samples)
            flamegraph = build_flamegraph_data(all_samples)
            annotated = build_annotated_source(mapper, all_samples)

            for event_type, data in [
                ('function_summary', summary),
                ('flamegraph', flamegraph),
                ('source', annotated),
            ]:
                msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
                try:
                    with wlock:
                        self.wfile.write(msg.encode('utf-8'))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

        # Keep connection open
        try:
            while True:
                time.sleep(1)
                # Send keepalive
                try:
                    with wlock:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            with state['lock']:
                try:
                    state['sse_clients'].remove(client)
                except ValueError:
                    pass

    def _handle_source_request(self, file_path):
        """Return annotated source for a specific file."""
        if not file_path:
            self._send_json({'error': 'file parameter required'})
            return

        mapper = SourceMapper(
            server_config['source_dir'],
            server_config.get('binary_path')
        )
        with profiling_state['lock']:
            all_samples = list(profiling_state['all_samples'])

        line_data = mapper.map_samples_to_lines(all_samples)
        if file_path in line_data:
            lines = mapper.annotate_source(file_path, line_data[file_path])
            self._send_json({'file': file_path, 'lines': lines})
        else:
            self._send_json({'file': file_path, 'lines': [], 'error': 'no data for file'})

    def log_message(self, format, *args):
        # Suppress access logs for SSE keepalives
        if '/api/stream' not in str(args):
            super().log_message(format, *args)


def run_http_server(port, ui_dir):
    """Run the HTTP server for the web UI."""
    PerfLensHTTPHandler.ui_dir = ui_dir
    httpd = HTTPServer(('0.0.0.0', port), PerfLensHTTPHandler)
    print(f"[server] HTTP server on http://0.0.0.0:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description='PerfLens Server')
    parser.add_argument('--port', type=int, default=9999,
                        help='TCP port for agent connections (default: 9999)')
    parser.add_argument('--http-port', type=int, default=8080,
                        help='HTTP port for web UI (default: 8080)')
    parser.add_argument('--source-dir', type=str, default='.',
                        help='Path to source code directory')
    parser.add_argument('--binary', type=str, default=None,
                        help='Path to binary with debug symbols')
    args = parser.parse_args()

    server_config['source_dir'] = os.path.abspath(args.source_dir)
    server_config['binary_path'] = args.binary

    ui_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ui')
    ui_dir = os.path.abspath(ui_dir)

    if not os.path.isdir(ui_dir):
        print(f"[server] Warning: UI directory not found at {ui_dir}")

    # Start TCP server in a thread
    tcp_thread = threading.Thread(target=run_tcp_server, args=(args.port,), daemon=True)
    tcp_thread.start()

    # Run HTTP server in main thread
    run_http_server(args.http_port, ui_dir)


if __name__ == '__main__':
    main()
