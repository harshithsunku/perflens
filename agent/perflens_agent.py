#!/usr/bin/env python3
"""PerfLens Device Agent — collects perf data, compresses, and streams to server."""

import argparse
import dataclasses
import os
import platform
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time


LOG_PREFIX = "[perflens-agent]"


def log(msg):
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr, flush=True)


def log_warn(msg):
    print(f"{LOG_PREFIX} WARNING: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PlatformInfo:
    arch: str
    endianness: str
    kernel: str
    perf_version: str
    perf_event_paranoid: int


@dataclasses.dataclass
class PerfCapabilities:
    record_events: list          # events suitable for perf record
    stat_only_events: list       # events only for perf stat
    all_events: list             # union of both
    callgraph_method: str        # "fp", "dwarf", "lbr", or "" (none)


@dataclasses.dataclass
class CompressionMethod:
    available: bool
    command: str                 # full path or name of zstd binary, "" if unavailable


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def detect_platform() -> PlatformInfo:
    arch = platform.machine()
    endianness = sys.byteorder
    kernel = platform.release()

    # perf version
    try:
        r = subprocess.run(["perf", "--version"], capture_output=True, text=True, timeout=5)
        perf_version = r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        perf_version = "unknown"

    # perf_event_paranoid
    paranoid = -1
    try:
        with open("/proc/sys/kernel/perf_event_paranoid") as f:
            paranoid = int(f.read().strip())
    except Exception:
        pass

    info = PlatformInfo(
        arch=arch,
        endianness=endianness,
        kernel=kernel,
        perf_version=perf_version,
        perf_event_paranoid=paranoid,
    )

    log(f"Platform: arch={info.arch}, endian={info.endianness}, "
        f"kernel={info.kernel}, perf={info.perf_version}, "
        f"perf_event_paranoid={info.perf_event_paranoid}")

    if paranoid > 1:
        log_warn(f"perf_event_paranoid={paranoid} (>1). "
                 "Some events may be unavailable. "
                 "Consider: sudo sysctl kernel.perf_event_paranoid=1")

    return info


# ---------------------------------------------------------------------------
# Capability probing
# ---------------------------------------------------------------------------

STAT_ONLY_EVENTS = {"page-faults", "context-switches", "cpu-migrations"}

CANDIDATE_EVENTS = [
    "cycles", "instructions", "cache-misses", "cache-references",
    "branch-misses", "branch-instructions", "page-faults",
    "context-switches", "cpu-migrations",
]

CALLGRAPH_METHODS = ["fp", "dwarf", "lbr"]

SKIP_PATTERNS = ["not supported", "invalid event", "unknown"]


def _event_works(event: str, pid: int) -> bool:
    """Probe whether a single perf event works on this system."""
    cmd = ["perf", "stat", "-e", event, "-p", str(pid), "--", "sleep", "1"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False
        stderr_lower = r.stderr.lower()
        for pat in SKIP_PATTERNS:
            if pat in stderr_lower:
                return False
        return True
    except Exception:
        return False


def _callgraph_works(method: str, pid: int) -> bool:
    """Probe whether a call-graph method works by doing a short perf record + script."""
    with tempfile.NamedTemporaryFile(suffix=".data", delete=False) as f:
        tmp = f.name
    try:
        cmd_rec = [
            "perf", "record", "-e", "cycles", "-p", str(pid),
            "--call-graph", method, "-F", "99", "-o", tmp,
            "--", "sleep", "2",
        ]
        r = subprocess.run(cmd_rec, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return False
        cmd_script = ["perf", "script", "-i", tmp]
        r = subprocess.run(cmd_script, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return False
        return len(r.stdout.strip()) > 0
    except Exception:
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def probe_capabilities(pid: int) -> PerfCapabilities:
    log("Probing perf event support...")
    record_events = []
    stat_only_events = []
    for ev in CANDIDATE_EVENTS:
        if _event_works(ev, pid):
            if ev in STAT_ONLY_EVENTS:
                stat_only_events.append(ev)
            else:
                record_events.append(ev)
            log(f"  {ev}: supported")
        else:
            log(f"  {ev}: not available, skipping")

    if not record_events:
        log_warn("No record events available. Profiling may not produce useful data.")

    # Probe call-graph method
    log("Probing call-graph methods...")
    callgraph = ""
    for method in CALLGRAPH_METHODS:
        log(f"  Trying --call-graph {method}...")
        if _callgraph_works(method, pid):
            callgraph = method
            log(f"  Using call-graph method: {method}")
            break
        else:
            log(f"  {method}: failed")

    if not callgraph:
        log_warn("No call-graph method works. Will collect flat profiles (no stacks).")

    caps = PerfCapabilities(
        record_events=record_events,
        stat_only_events=stat_only_events,
        all_events=record_events + stat_only_events,
        callgraph_method=callgraph,
    )
    log(f"Record events: {','.join(caps.record_events) or '(none)'}")
    log(f"Stat-only events: {','.join(caps.stat_only_events) or '(none)'}")
    return caps


# ---------------------------------------------------------------------------
# Compression probing
# ---------------------------------------------------------------------------

def probe_compression() -> CompressionMethod:
    # 1. System zstd
    try:
        r = subprocess.run(["which", "zstd"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            path = r.stdout.strip()
            log(f"Compression: using system zstd at {path}")
            return CompressionMethod(available=True, command=path)
    except Exception:
        pass

    # 2. Bundled binary
    arch = platform.machine()
    bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", arch, "zstd")
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        log(f"Compression: using bundled zstd at {bundled}")
        return CompressionMethod(available=True, command=bundled)

    # 3. No compression
    log_warn("zstd not found. Install with: apt install zstd / yum install zstd. "
             "Sending uncompressed — higher TCP overhead.")
    return CompressionMethod(available=False, command="")


def compress_data(data: bytes, comp: CompressionMethod) -> tuple:
    """Compress data with zstd. Returns (payload_bytes, compression_flag)."""
    if not comp.available:
        return data, 0

    try:
        r = subprocess.run(
            [comp.command, "-1", "-c"],
            input=data, capture_output=True, timeout=30,
        )
        if r.returncode == 0 and len(r.stdout) > 0:
            return r.stdout, 1
    except Exception as e:
        log_warn(f"Compression failed: {e}")

    return data, 0


# ---------------------------------------------------------------------------
# Shutdown coordination
# ---------------------------------------------------------------------------

class ShutdownFlag:
    def __init__(self):
        self._flag = threading.Event()

    def set(self):
        self._flag.set()

    @property
    def is_set(self) -> bool:
        return self._flag.is_set()

    def wait(self, timeout=None) -> bool:
        return self._flag.wait(timeout)


# ---------------------------------------------------------------------------
# TCP sender with reconnect
# ---------------------------------------------------------------------------

class TCPSender:
    def __init__(self, host: str, port: int, shutdown: ShutdownFlag):
        self._host = host
        self._port = port
        self._shutdown = shutdown
        self._sock = None

    def connect(self):
        """Connect with exponential backoff. Loops until connected or shutdown."""
        delay = 1.0
        max_delay = 30.0
        while not self._shutdown.is_set:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(30)
                s.connect((self._host, self._port))
                self._sock = s
                log(f"Connected to {self._host}:{self._port}")
                return
            except OSError as e:
                log(f"Connection failed ({e}), retrying in {delay:.0f}s...")
                s.close()
                self._shutdown.wait(delay)
                delay = min(delay * 2, max_delay)
        raise SystemExit("Shutdown during connect")

    def send(self, payload: bytes, compression_flag: int):
        """Send with 5-byte header. Reconnects on failure and retries once."""
        header = struct.pack("!IB", len(payload), compression_flag)
        data = header + payload
        try:
            self._sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            log(f"Send failed ({e}), reconnecting...")
            self.close()
            self.connect()
            # Retry the send once after reconnect
            self._sock.sendall(data)

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ---------------------------------------------------------------------------
# Perf collector
# ---------------------------------------------------------------------------

class PerfCollector:
    def __init__(self, pid: int, caps: PerfCapabilities, frequency: int,
                 duration: int, shutdown: ShutdownFlag):
        self._pid = pid
        self._caps = caps
        self._frequency = frequency
        self._duration = duration
        self._shutdown = shutdown
        self._lock = threading.Lock()
        self._active_procs = []

    def _track(self, proc: subprocess.Popen):
        with self._lock:
            self._active_procs.append(proc)

    def _untrack(self, proc: subprocess.Popen):
        with self._lock:
            try:
                self._active_procs.remove(proc)
            except ValueError:
                pass

    def terminate_all(self):
        """Terminate all tracked subprocesses."""
        with self._lock:
            procs = list(self._active_procs)
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass

    def collect(self) -> str:
        """Run one collection round. Returns combined perf script + stat text, or None."""
        if not self._caps.record_events:
            return None

        with tempfile.NamedTemporaryFile(suffix=".data", delete=False) as f:
            tmp_path = f.name

        timeout = self._duration + 10

        try:
            # Build perf record command
            record_events_str = ",".join(self._caps.record_events)
            cmd_record = [
                "perf", "record",
                "-e", record_events_str,
                "-p", str(self._pid),
                "-F", str(self._frequency),
                "-o", tmp_path,
            ]
            if self._caps.callgraph_method:
                cmd_record += ["--call-graph", self._caps.callgraph_method]
            cmd_record += ["--", "sleep", str(self._duration)]

            # Build perf stat command
            all_events_str = ",".join(self._caps.all_events) + ",task-clock"
            cmd_stat = [
                "perf", "stat",
                "-e", all_events_str,
                "-p", str(self._pid),
                "--", "sleep", str(self._duration),
            ]

            # Start both concurrently
            proc_record = subprocess.Popen(
                cmd_record, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self._track(proc_record)

            proc_stat = subprocess.Popen(
                cmd_stat, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self._track(proc_stat)

            # Wait for both
            _, rec_stderr = proc_record.communicate(timeout=timeout)
            self._untrack(proc_record)

            _, stat_stderr = proc_stat.communicate(timeout=timeout)
            self._untrack(proc_stat)

            rec_stderr_text = rec_stderr.decode("utf-8", errors="replace")
            stat_stderr_text = stat_stderr.decode("utf-8", errors="replace")

            if proc_record.returncode != 0:
                log(f"perf record failed (rc={proc_record.returncode}): {rec_stderr_text.strip()}")
                return None

            # Run perf script
            cmd_script = ["perf", "script", "-i", tmp_path]
            proc_script = subprocess.Popen(
                cmd_script, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self._track(proc_script)
            script_stdout, script_stderr = proc_script.communicate(timeout=timeout)
            self._untrack(proc_script)

            script_text = script_stdout.decode("utf-8", errors="replace")
            script_err = script_stderr.decode("utf-8", errors="replace")

            if proc_script.returncode != 0:
                log(f"perf script failed (rc={proc_script.returncode}): {script_err.strip()}")
                return None

            # Combine
            combined = script_text
            if stat_stderr_text:
                combined += "\n### PERF_STAT ###\n" + stat_stderr_text

            return combined

        except subprocess.TimeoutExpired:
            log_warn("Collection timed out, terminating subprocesses")
            self.terminate_all()
            return None

        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def install_signal_handlers(shutdown: ShutdownFlag, collector: PerfCollector,
                            sender: TCPSender):
    def handler(signum, frame):
        signame = signal.Signals(signum).name
        log(f"Received {signame}, shutting down...")
        shutdown.set()
        collector.terminate_all()
        sender.close()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="PerfLens Device Agent")
    parser.add_argument("--pid", type=int, required=True,
                        help="PID of process to profile")
    parser.add_argument("--server", type=str, default=None,
                        help="Server IP to stream data to (omit for stdout mode)")
    parser.add_argument("--port", type=int, default=9999,
                        help="Server port (default: 9999)")
    parser.add_argument("--frequency", type=int, default=99,
                        help="Sampling frequency in Hz (default: 99)")
    parser.add_argument("--duration", type=int, default=8,
                        help="Duration of each collection in seconds (default: 8)")
    args = parser.parse_args()

    # Verify the process exists
    try:
        os.kill(args.pid, 0)
    except ProcessLookupError:
        log(f"Error: process {args.pid} not found")
        sys.exit(1)
    except PermissionError:
        pass

    # Platform detection
    detect_platform()

    # Capability probing
    caps = probe_capabilities(args.pid)

    # Stdout mode — single collection, print, exit
    if not args.server:
        log(f"Collecting perf data for PID {args.pid} (stdout mode)")
        shutdown = ShutdownFlag()
        collector = PerfCollector(args.pid, caps, args.frequency, args.duration, shutdown)
        output = collector.collect()
        if output:
            print(output)
            log(f"Done. Output {len(output)} bytes.")
        else:
            log("No data collected.")
            sys.exit(1)
        return

    # --- TCP streaming mode ---

    # Compression
    comp = probe_compression()

    # Shutdown coordination
    shutdown = ShutdownFlag()

    # TCP sender
    sender = TCPSender(args.server, args.port, shutdown)

    # Collector
    collector = PerfCollector(args.pid, caps, args.frequency, args.duration, shutdown)

    # Signal handlers
    install_signal_handlers(shutdown, collector, sender)

    # Connect
    log(f"Connecting to {args.server}:{args.port}...")
    try:
        sender.connect()
    except SystemExit:
        return

    # Collection loop
    round_num = 0
    while not shutdown.is_set:
        round_num += 1

        # Process liveness check
        try:
            os.kill(args.pid, 0)
        except ProcessLookupError:
            log(f"Process {args.pid} has exited")
            break
        except PermissionError:
            pass

        log(f"Round {round_num}: collecting ({args.duration}s)...")
        output = collector.collect()

        if shutdown.is_set:
            break

        if not output:
            log(f"Round {round_num}: no data, retrying...")
            time.sleep(1)
            continue

        raw_bytes = output.encode("utf-8")
        raw_size = len(raw_bytes)

        payload, flag = compress_data(raw_bytes, comp)
        compressed_size = len(payload)

        if flag == 1:
            ratio = raw_size / compressed_size if compressed_size > 0 else 0
            log(f"Round {round_num}: perf script {raw_size} bytes, "
                f"compressed {compressed_size} bytes (ratio {ratio:.1f}x)")
        else:
            log(f"Round {round_num}: perf script {raw_size} bytes (uncompressed)")

        try:
            sender.send(payload, flag)
            log(f"Round {round_num}: sent successfully")
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            log(f"Round {round_num}: send failed after reconnect: {e}")
            if shutdown.is_set:
                break
            time.sleep(1)

    # Cleanup
    log("Shutting down.")
    collector.terminate_all()
    sender.close()


if __name__ == "__main__":
    main()
