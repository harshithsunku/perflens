#!/usr/bin/env python3
"""PerfLens Device Agent -- collects perf data, compresses, and streams to server.

Python 3.5+ compatible. Runs on embedded ARM switches with Python 3.5.7.
"""

import argparse
import errno
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

# Binary name for perf; kept as a constant to make it easy to override
# for testing and to avoid repeating the string throughout the file.
PERF = 'perf'


def log(msg):
    sys.stderr.write("%s %s\n" % (LOG_PREFIX, msg))
    sys.stderr.flush()


def log_warn(msg):
    sys.stderr.write("%s WARNING: %s\n" % (LOG_PREFIX, msg))
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# subprocess compat helper (Python 3.5+)
# ---------------------------------------------------------------------------

def _run_cmd(cmd, input_data=None, timeout=None):
    """Run a command and return (returncode, stdout_bytes, stderr_bytes).

    Compatibility helper for Python 3.5, which lacks the higher-level
    interfaces added in later versions.
    """
    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_data is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = p.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.wait()
        except Exception:
            pass
        raise
    return p.returncode, stdout, stderr


# ---------------------------------------------------------------------------
# Data classes -- plain classes for 3.5 compat (no decorator-based records)
# ---------------------------------------------------------------------------

class PlatformInfo(object):
    def __init__(self, arch, endianness, kernel, perf_version, perf_event_paranoid):
        self.arch = arch
        self.endianness = endianness
        self.kernel = kernel
        self.perf_version = perf_version
        self.perf_event_paranoid = perf_event_paranoid


class PerfCapabilities(object):
    def __init__(self, record_events, stat_only_events, all_events, callgraph_method):
        self.record_events = record_events          # events suitable for perf record
        self.stat_only_events = stat_only_events    # events only for perf stat
        self.all_events = all_events                # union of both
        self.callgraph_method = callgraph_method    # 'fp' / 'dwarf' / 'lbr' / '' (none)


class CompressionMethod(object):
    def __init__(self, available, command):
        self.available = available
        self.command = command                      # full path/name of zstd, "" if unavailable


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def detect_platform():
    arch = platform.machine()
    endianness = sys.byteorder
    kernel = platform.release()

    # perf version
    try:
        rc, out, _ = _run_cmd([PERF, "--version"], timeout=5)
        perf_version = out.decode("utf-8", "replace").strip() if rc == 0 else "unknown"
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

    log("Platform: arch=%s, endian=%s, kernel=%s, perf=%s, perf_event_paranoid=%s" % (
        info.arch, info.endianness, info.kernel, info.perf_version,
        info.perf_event_paranoid))

    if paranoid > 1:
        log_warn("perf_event_paranoid=%d (>1). "
                 "Some events may be unavailable. "
                 "Consider: sudo sysctl kernel.perf_event_paranoid=1" % paranoid)

    return info


# ---------------------------------------------------------------------------
# Capability probing
# ---------------------------------------------------------------------------

STAT_ONLY_EVENTS = set(["page-faults", "context-switches", "cpu-migrations"])

CANDIDATE_EVENTS = [
    "cycles", "instructions", "cache-misses", "cache-references",
    "branch-misses", "branch-instructions", "page-faults",
    "context-switches", "cpu-migrations",
]

CALLGRAPH_METHODS = ['fp', 'dwarf', 'lbr']

SKIP_PATTERNS = ["not supported", "invalid event", "unknown"]


def _event_works(event, pid):
    """Probe whether a single perf event works on this system."""
    cmd = [PERF, "stat", "-e", event, "-p", str(pid), "--", "sleep", "1"]
    try:
        rc, _, stderr = _run_cmd(cmd, timeout=10)
        if rc != 0:
            return False
        stderr_lower = stderr.decode("utf-8", "replace").lower()
        for pat in SKIP_PATTERNS:
            if pat in stderr_lower:
                return False
        return True
    except Exception:
        return False


def _callgraph_works(method, pid):
    """Probe whether a call-graph method works by doing a short perf record + script."""
    fd, tmp = tempfile.mkstemp(suffix=".data")
    os.close(fd)
    try:
        cmd_rec = [
            PERF, "record", "-e", "cycles", "-p", str(pid),
            "--call-graph", method, "-F", "99", "-o", tmp,
            "--", "sleep", "2",
        ]
        rc, _, _ = _run_cmd(cmd_rec, timeout=15)
        if rc != 0:
            return False
        cmd_script = [PERF, "script", "-i", tmp]
        rc, stdout, _ = _run_cmd(cmd_script, timeout=15)
        if rc != 0:
            return False
        return len(stdout.decode("utf-8", "replace").strip()) > 0
    except Exception:
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def probe_capabilities(pid):
    log("Probing perf event support...")
    record_events = []
    stat_only_events = []
    for ev in CANDIDATE_EVENTS:
        if _event_works(ev, pid):
            if ev in STAT_ONLY_EVENTS:
                stat_only_events.append(ev)
            else:
                record_events.append(ev)
            log("  %s: supported" % ev)
        else:
            log("  %s: not available, skipping" % ev)

    if not record_events:
        log_warn("No record events available. Profiling may not produce useful data.")

    # Probe call-graph method
    log("Probing call-graph methods...")
    callgraph = ""
    for method in CALLGRAPH_METHODS:
        log("  Trying --call-graph %s..." % method)
        if _callgraph_works(method, pid):
            callgraph = method
            log("  Using call-graph method: %s" % method)
            break
        else:
            log("  %s: failed" % method)

    if not callgraph:
        log_warn("No call-graph method works. Will collect flat profiles (no stacks).")

    caps = PerfCapabilities(
        record_events=record_events,
        stat_only_events=stat_only_events,
        all_events=record_events + stat_only_events,
        callgraph_method=callgraph,
    )
    log("Record events: %s" % (",".join(caps.record_events) or "(none)"))
    log("Stat-only events: %s" % (",".join(caps.stat_only_events) or "(none)"))
    return caps


# ---------------------------------------------------------------------------
# Compression probing
# ---------------------------------------------------------------------------

def _candidate_arch_dirs():
    """Return a list of candidate arch directory names for bundled binaries.

    Embedded targets may report nonstandard machine names; for big-endian
    AArch64 switches in particular, try several fallbacks.
    """
    mach = platform.machine()
    candidates = [mach]
    # Fallbacks for big-endian ARM and 32-bit ARM variants
    for alt in ("aarch64_be", "arm64", "armv7l", "armv7", "arm"):
        if alt not in candidates:
            candidates.append(alt)
    return candidates


def probe_compression():
    # 1. System zstd
    try:
        rc, out, _ = _run_cmd(["which", "zstd"], timeout=5)
        if rc == 0:
            path = out.decode("utf-8", "replace").strip()
            if path:
                log("Compression: using system zstd at %s" % path)
                return CompressionMethod(available=True, command=path)
    except Exception:
        pass

    # 2. Bundled binary -- try multiple arch directory names
    base_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
    for arch_dir in _candidate_arch_dirs():
        bundled = os.path.join(base_bin, arch_dir, "zstd")
        if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
            log("Compression: using bundled zstd at %s" % bundled)
            return CompressionMethod(available=True, command=bundled)

    # 3. No compression
    log_warn("zstd not found. Install with: apt install zstd / yum install zstd. "
             "Sending uncompressed -- higher TCP overhead.")
    return CompressionMethod(available=False, command="")


def compress_data(data, comp):
    """Compress data with zstd. Returns (payload_bytes, compression_flag)."""
    if not comp.available:
        return data, 0

    try:
        rc, stdout, _ = _run_cmd([comp.command, "-1", "-c"], input_data=data, timeout=30)
        if rc == 0 and len(stdout) > 0:
            return stdout, 1
    except Exception as e:
        log_warn("Compression failed: %s" % e)

    return data, 0


# ---------------------------------------------------------------------------
# Shutdown coordination
# ---------------------------------------------------------------------------

class ShutdownFlag(object):
    def __init__(self):
        self._flag = threading.Event()

    def set(self):
        self._flag.set()

    @property
    def is_set(self):
        return self._flag.is_set()

    def wait(self, timeout=None):
        return self._flag.wait(timeout)


# ---------------------------------------------------------------------------
# TCP sender with reconnect
# ---------------------------------------------------------------------------

class TCPSender(object):
    def __init__(self, host, port, shutdown):
        self._host = host
        self._port = port
        self._shutdown = shutdown
        self._sock = None

    def connect(self):
        """Connect with exponential backoff. Loops until connected or shutdown."""
        delay = 1.0
        max_delay = 30.0
        while not self._shutdown.is_set:
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(30)
                s.connect((self._host, self._port))
                self._sock = s
                log("Connected to %s:%d" % (self._host, self._port))
                return
            except OSError as e:
                log("Connection failed (%s), retrying in %.0fs..." % (e, delay))
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
                self._shutdown.wait(delay)
                delay = min(delay * 2, max_delay)
        raise SystemExit("Shutdown during connect")

    def send(self, payload, compression_flag):
        """Send with 5-byte header. Reconnects on failure and retries once."""
        header = struct.pack("!IB", len(payload), compression_flag)
        data = header + payload
        try:
            self._sock.sendall(data)
        except (IOError, OSError) as e:
            log("Send failed (%s), reconnecting..." % e)
            self.close()
            self.connect()
            try:
                self._sock.sendall(data)
            except (IOError, OSError) as e2:
                log("Retry send also failed (%s)" % e2)
                self.close()
                raise

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

class PerfCollector(object):
    def __init__(self, pid, caps, frequency, duration, shutdown):
        self._pid = pid
        self._caps = caps
        self._frequency = frequency
        self._duration = duration
        self._shutdown = shutdown
        self._lock = threading.Lock()
        self._active_procs = []

    def _track(self, proc):
        with self._lock:
            self._active_procs.append(proc)

    def _untrack(self, proc):
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

    def collect(self):
        """Run one collection round. Returns combined perf script + stat text, or None."""
        if not self._caps.record_events:
            return None

        fd, tmp_path = tempfile.mkstemp(suffix=".data")
        os.close(fd)

        timeout = self._duration + 10

        try:
            # Build perf record command
            record_events_str = ",".join(self._caps.record_events)
            cmd_record = [
                PERF, "record",
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
                PERF, "stat",
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

            # Wait for both -- if record times out, also clean up stat
            try:
                _, rec_stderr = proc_record.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc_record.kill()
                proc_stat.kill()
                proc_record.wait()
                proc_stat.wait()
                self._untrack(proc_record)
                self._untrack(proc_stat)
                log_warn("Collection timed out, killed subprocesses")
                return None
            self._untrack(proc_record)

            _, stat_stderr = proc_stat.communicate(timeout=timeout)
            self._untrack(proc_stat)

            rec_stderr_text = rec_stderr.decode("utf-8", "replace")
            stat_stderr_text = stat_stderr.decode("utf-8", "replace")

            if proc_record.returncode != 0:
                log("perf record failed (rc=%d): %s" % (
                    proc_record.returncode, rec_stderr_text.strip()))
                return None

            # Run perf script
            cmd_script = [PERF, "script", "-i", tmp_path]
            proc_script = subprocess.Popen(
                cmd_script, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self._track(proc_script)
            script_stdout, script_stderr = proc_script.communicate(timeout=timeout)
            self._untrack(proc_script)

            script_text = script_stdout.decode("utf-8", "replace")
            script_err = script_stderr.decode("utf-8", "replace")

            if proc_script.returncode != 0:
                log("perf script failed (rc=%d): %s" % (
                    proc_script.returncode, script_err.strip()))
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

def install_signal_handlers(shutdown, collector, sender):
    def handler(signum, frame):
        try:
            signame = signal.Signals(signum).name
        except Exception:
            signame = str(signum)
        log("Received %s, shutting down..." % signame)
        shutdown.set()
        collector.terminate_all()
        sender.close()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _process_exists(pid):
    """Return True if the PID exists and we can see it. Raises on unknown error."""
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        # EPERM means the process exists but we lack permission -- treat as alive
        if e.errno == errno.EPERM:
            return True
        raise


def main():
    argp = argparse.ArgumentParser(description="PerfLens Device Agent")
    argp.add_argument("--pid", type=int, required=True,
                      help="PID of process to profile")
    argp.add_argument("--server", type=str, default=None,
                      help="Server IP to stream data to (omit for stdout mode)")
    argp.add_argument("--port", type=int, default=9999,
                      help="Server port (default: 9999)")
    argp.add_argument("--frequency", type=int, default=99,
                      help="Sampling frequency in Hz (default: 99)")
    argp.add_argument("--duration", type=int, default=8,
                      help="Duration of each collection in seconds (default: 8)")
    args = argp.parse_args()

    # Verify the process exists
    if not _process_exists(args.pid):
        log("Error: process %d not found" % args.pid)
        sys.exit(1)

    # Platform detection
    detect_platform()

    # Capability probing
    caps = probe_capabilities(args.pid)

    # Stdout mode -- single collection, print, exit
    if not args.server:
        log("Collecting perf data for PID %d (stdout mode)" % args.pid)
        shutdown = ShutdownFlag()
        collector = PerfCollector(args.pid, caps, args.frequency, args.duration, shutdown)
        output = collector.collect()
        if output:
            try:
                sys.stdout.write(output)
                sys.stdout.flush()
            except (IOError, OSError):
                pass
            log("Done. Output %d bytes." % len(output))
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
    log("Connecting to %s:%d..." % (args.server, args.port))
    try:
        sender.connect()
    except SystemExit:
        return

    # Collection loop
    round_num = 0
    while not shutdown.is_set:
        round_num += 1

        # Process liveness check
        if not _process_exists(args.pid):
            log("Process %d has exited" % args.pid)
            break

        log("Round %d: collecting (%ds)..." % (round_num, args.duration))
        output = collector.collect()

        if shutdown.is_set:
            break

        if not output:
            log("Round %d: no data, retrying..." % round_num)
            time.sleep(1)
            continue

        raw_bytes = output.encode("utf-8")
        raw_size = len(raw_bytes)

        payload, flag = compress_data(raw_bytes, comp)
        compressed_size = len(payload)

        if flag == 1:
            ratio = (raw_size / compressed_size) if compressed_size > 0 else 0
            log("Round %d: perf script %d bytes, compressed %d bytes (ratio %.1fx)" % (
                round_num, raw_size, compressed_size, ratio))
        else:
            log("Round %d: perf script %d bytes (uncompressed)" % (round_num, raw_size))

        try:
            sender.send(payload, flag)
            log("Round %d: sent successfully" % round_num)
        except (IOError, OSError) as e:
            log("Round %d: send failed after reconnect: %s" % (round_num, e))
            if shutdown.is_set:
                break
            time.sleep(1)

    # Cleanup
    log("Shutting down.")
    collector.terminate_all()
    sender.close()


if __name__ == "__main__":
    main()
