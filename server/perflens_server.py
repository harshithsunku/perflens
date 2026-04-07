#!/usr/bin/env python3
"""PerfLens Server - receives perf data from agents and serves web UI."""

import argparse
import socket
import struct
import sys
import threading

from parser import parse_perf_script, build_function_summary


def recv_exactly(conn, n):
    """Receive exactly n bytes from a socket."""
    data = b''
    while len(data) < n:
        chunk = conn.recv(min(65536, n - len(data)))
        if not chunk:
            return None
        data += chunk
    return data


def handle_agent_connection(conn, addr):
    """Handle a single agent connection, reading length-prefixed messages."""
    print(f"[server] Agent connected from {addr}")
    chunk_num = 0
    try:
        while True:
            # Read 4-byte length header
            header = recv_exactly(conn, 4)
            if header is None:
                print(f"[server] Agent {addr} disconnected")
                break

            length = struct.unpack('!I', header)[0]
            if length == 0:
                continue

            # Read the payload
            payload = recv_exactly(conn, length)
            if payload is None:
                print(f"[server] Agent {addr} disconnected mid-message")
                break

            text = payload.decode('utf-8')
            chunk_num += 1

            # Parse and display summary
            samples = parse_perf_script(text)
            summary = build_function_summary(samples)
            print(f"\n[server] === Chunk {chunk_num} from {addr}: "
                  f"{length} bytes, {summary['total_samples']} samples ===")
            print(f"Top 10 functions:")
            for f in summary['functions'][:10]:
                print(f"  {f['percent']:6.1f}%  {f['samples']:5d}  "
                      f"{f['name']:<30s}  ({f['module']})")

    except ConnectionResetError:
        print(f"[server] Agent {addr} connection reset")
    finally:
        conn.close()


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
        print("\n[server] Shutting down")
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description='PerfLens Server')
    parser.add_argument('--port', type=int, default=9999,
                        help='TCP port for agent connections (default: 9999)')
    parser.add_argument('--source-dir', type=str, default='.',
                        help='Path to source code directory')
    args = parser.parse_args()

    run_tcp_server(args.port)


if __name__ == '__main__':
    main()
