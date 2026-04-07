#!/usr/bin/env python3
"""PerfLens Device Agent - collects perf data and streams to server."""

import argparse
import subprocess
import sys
import tempfile
import os


def collect_perf_data(pid, frequency=99, duration=3):
    """Run perf record + perf script and return the text output."""
    with tempfile.NamedTemporaryFile(suffix='.data', delete=False) as f:
        perf_data_path = f.name

    try:
        # Run perf record
        cmd_record = [
            'perf', 'record',
            '-o', perf_data_path,
            '-p', str(pid),
            '-g',
            '-F', str(frequency),
            '--', 'sleep', str(duration)
        ]
        print(f"[agent] Running: {' '.join(cmd_record)}", file=sys.stderr)
        result = subprocess.run(cmd_record, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[agent] perf record failed: {result.stderr}", file=sys.stderr)
            return None
        print(f"[agent] perf record done: {result.stderr.strip()}", file=sys.stderr)

        # Run perf script to get text output
        cmd_script = ['perf', 'script', '-i', perf_data_path]
        result = subprocess.run(cmd_script, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[agent] perf script failed: {result.stderr}", file=sys.stderr)
            return None

        return result.stdout
    finally:
        # Clean up perf.data file
        try:
            os.unlink(perf_data_path)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description='PerfLens Device Agent')
    parser.add_argument('--pid', type=int, required=True,
                        help='PID of process to profile')
    parser.add_argument('--frequency', type=int, default=99,
                        help='Sampling frequency in Hz (default: 99)')
    parser.add_argument('--duration', type=int, default=3,
                        help='Duration of each collection in seconds (default: 3)')
    args = parser.parse_args()

    # Verify the process exists
    try:
        os.kill(args.pid, 0)
    except ProcessLookupError:
        print(f"[agent] Error: process {args.pid} not found", file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        pass  # Process exists but we can't signal it — that's fine

    print(f"[agent] Collecting perf data for PID {args.pid}", file=sys.stderr)
    output = collect_perf_data(args.pid, args.frequency, args.duration)
    if output:
        print(output)
        print(f"[agent] Done. Output {len(output)} bytes.", file=sys.stderr)
    else:
        print("[agent] No data collected.", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
