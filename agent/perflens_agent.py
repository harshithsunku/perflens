#!/usr/bin/env python3
"""PerfLens Device Agent -- collects perf data, compresses, and streams to server.

Python 3.5+ compatible. Designed to run on older ARM or x86 Linux targets
where only Python 3.5 may be available.
"""

import argparse
import errno
import json
import os
import platform
import queue
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

# Wire protocol flags (5-byte header: 4-byte length + 1-byte flag)
FLAG_DATA_RAW = 0       # agent -> server: raw perf data
FLAG_DATA_ZSTD = 1      # agent -> server: zstd-compressed perf data
FLAG_CMD_REQUEST = 2    # server -> agent: JSON command
FLAG_CMD_RESPONSE = 3   # agent -> server: JSON response
FLAG_METRICS = 4        # agent -> server: JSON health metrics

# Agent states
AGENT_IDLE = 'idle'
AGENT_PROFILING = 'profiling'
AGENT_PAUSED = 'paused'


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


def recv_exactly(sock, n):
    """Receive exactly n bytes from a socket. Returns None on disconnect."""
    data = b''
    while len(data) < n:
        try:
            chunk = sock.recv(min(65536, n - len(data)))
        except (IOError, OSError):
            return None
        if not chunk:
            return None
        data += chunk
    return data


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
    def __init__(self, record_events, stat_only_events, all_events, callgraph_method,
                 script_fields=""):
        self.record_events = record_events          # events suitable for perf record
        self.stat_only_events = stat_only_events    # events only for perf stat
        self.all_events = all_events                # union of both
        self.callgraph_method = callgraph_method    # 'fp' / 'dwarf' / 'lbr' / '' (none)
        self.script_fields = script_fields          # '-F' fields string or '' if unsupported


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

# Normalized field set for 'perf script -F'.  Ensures consistent output format
# across kernel versions.  Requires perf >= ~3.12 (older versions lack -F).
SCRIPT_FIELDS = "comm,pid,time,period,event,ip,sym,dso"


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


def _script_fields_work(pid, event="cycles"):
    """Probe whether 'perf script -F <fields>' is supported.

    Older perf versions (pre ~3.12) lack -F entirely.  Returns True if
    the normalized field set produces output, False otherwise.
    """
    fd, tmp = tempfile.mkstemp(suffix=".data")
    os.close(fd)
    try:
        cmd_rec = [
            PERF, "record", "-e", event, "-p", str(pid),
            "-F", "99", "-o", tmp,
            "--", "sleep", "1",
        ]
        rc, _, _ = _run_cmd(cmd_rec, timeout=15)
        if rc != 0:
            return False
        cmd_script = [PERF, "script", "-F", SCRIPT_FIELDS, "-i", tmp]
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

    # Probe perf script -F support (normalizes output across kernel versions)
    script_fields = ""
    if record_events:
        log("Probing perf script -F support...")
        if _script_fields_work(pid, event=record_events[0]):
            script_fields = SCRIPT_FIELDS
            log("  perf script -F supported, using: %s" % script_fields)
        else:
            log("  perf script -F not supported, using default output format")

    caps = PerfCapabilities(
        record_events=record_events,
        stat_only_events=stat_only_events,
        all_events=record_events + stat_only_events,
        callgraph_method=callgraph,
        script_fields=script_fields,
    )
    log("Record events: %s" % (",".join(caps.record_events) or "(none)"))
    log("Stat-only events: %s" % (",".join(caps.stat_only_events) or "(none)"))
    return caps


# ---------------------------------------------------------------------------
# Compression probing
# ---------------------------------------------------------------------------

def _candidate_arch_dirs():
    """Return a list of candidate arch directory names for bundled binaries.

    Some embedded ARM targets report nonstandard machine names; for
    big-endian AArch64 in particular, try several fallbacks.
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
# Metrics collector (reads /proc and /sys, no external tools)
# ---------------------------------------------------------------------------

class MetricsCollector(object):
    """Collects system and per-process health metrics from /proc and /sys.

    CPU percentage requires two consecutive reads to compute a delta.
    The first call returns overall_pct as None (no baseline yet).
    """

    def __init__(self):
        self._pid = None
        self._prev_cpu = None
        self._prev_per_core = None
        self._prev_proc_cpu = None
        self._prev_proc_time = None
        self._prev_net = None
        self._include_network = True
        self._warned = set()
        try:
            self._page_size = os.sysconf("SC_PAGESIZE")
        except (AttributeError, ValueError):
            self._page_size = 4096
        self._clk_tck = 100
        try:
            self._clk_tck = os.sysconf("SC_CLK_TCK")
        except (AttributeError, ValueError):
            pass

    def set_pid(self, pid):
        if pid != self._pid:
            self._pid = pid
            self._prev_proc_cpu = None
            self._prev_proc_time = None

    def set_network(self, enabled):
        self._include_network = enabled

    def _warn_once(self, source, msg):
        if source not in self._warned:
            log_warn("Metrics: %s -- %s (will not warn again)" % (source, msg))
            self._warned.add(source)

    def _read_int(self, path, default=None):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (IOError, OSError, ValueError):
            return default

    def _read_str(self, path, default=""):
        try:
            with open(path) as f:
                return f.read().strip()
        except (IOError, OSError):
            return default

    def collect(self):
        results = []
        ts = time.time()
        sys_m = self._collect_system(ts)
        if sys_m:
            results.append(sys_m)
        if self._pid:
            proc_m = self._collect_process(ts)
            if proc_m:
                results.append(proc_m)
        if self._include_network:
            net_m = self._collect_network(ts)
            if net_m:
                results.append(net_m)
        return results

    def _parse_cpu_line(self, parts):
        """Parse cpu line fields into list of ints."""
        vals = []
        for p in parts:
            try:
                vals.append(int(p))
            except ValueError:
                break
        # Pad to 8 fields (user,nice,sys,idle,iowait,irq,softirq,steal)
        while len(vals) < 8:
            vals.append(0)
        return vals[:8]

    def _calc_cpu_pct(self, prev, curr):
        """Calculate CPU % from two /proc/stat snapshots."""
        p_idle = prev[3] + prev[4]  # idle + iowait
        c_idle = curr[3] + curr[4]
        p_total = sum(prev)
        c_total = sum(curr)
        d_total = c_total - p_total
        d_idle = c_idle - p_idle
        if d_total <= 0:
            return 0.0
        return round(100.0 * (d_total - d_idle) / d_total, 1)

    def _collect_system(self, ts):
        # --- CPU from /proc/stat ---
        cpu_overall = None
        per_core_pct = []
        num_cores = 0
        ctxt = 0
        interrupts = 0
        procs_running = 0
        procs_blocked = 0

        try:
            with open("/proc/stat") as f:
                lines = f.readlines()
        except (IOError, OSError):
            self._warn_once("cpu", "/proc/stat not readable")
            return None

        curr_cpu = None
        curr_per_core = {}
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "cpu":
                curr_cpu = self._parse_cpu_line(parts[1:])
            elif parts[0].startswith("cpu") and parts[0][3:].isdigit():
                core_id = int(parts[0][3:])
                curr_per_core[core_id] = self._parse_cpu_line(parts[1:])
                num_cores = max(num_cores, core_id + 1)
            elif parts[0] == "ctxt" and len(parts) > 1:
                try:
                    ctxt = int(parts[1])
                except ValueError:
                    pass
            elif parts[0] == "intr" and len(parts) > 1:
                try:
                    interrupts = int(parts[1])
                except ValueError:
                    pass
            elif parts[0] == "procs_running" and len(parts) > 1:
                try:
                    procs_running = int(parts[1])
                except ValueError:
                    pass
            elif parts[0] == "procs_blocked" and len(parts) > 1:
                try:
                    procs_blocked = int(parts[1])
                except ValueError:
                    pass

        if curr_cpu and self._prev_cpu:
            cpu_overall = self._calc_cpu_pct(self._prev_cpu, curr_cpu)
        self._prev_cpu = curr_cpu

        if curr_per_core and self._prev_per_core:
            for cid in sorted(curr_per_core.keys()):
                if cid in self._prev_per_core:
                    per_core_pct.append(
                        self._calc_cpu_pct(self._prev_per_core[cid],
                                           curr_per_core[cid]))
        self._prev_per_core = curr_per_core

        # --- CPU frequency ---
        freq_mhz = []
        for i in range(num_cores):
            path = "/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % i
            val = self._read_int(path)
            if val is not None:
                freq_mhz.append(int(val / 1000))
            else:
                break
        if not freq_mhz:
            self._warn_once("freq", "cpufreq not available")

        # --- Memory from /proc/meminfo ---
        mem = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].rstrip(":")
                        try:
                            val = int(parts[1])
                        except ValueError:
                            continue
                        mem[key] = val
        except (IOError, OSError):
            self._warn_once("mem", "/proc/meminfo not readable")

        mem_total = mem.get("MemTotal", 0)
        mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        mem_used = mem_total - mem_avail
        buffers = mem.get("Buffers", 0)
        cached = mem.get("Cached", 0)
        swap_total = mem.get("SwapTotal", 0)
        swap_free = mem.get("SwapFree", 0)
        mem_pct = round(100.0 * mem_used / mem_total, 1) if mem_total > 0 else 0.0

        # --- Load average ---
        load_1m = 0.0
        load_5m = 0.0
        load_15m = 0.0
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().split()
                if len(parts) >= 3:
                    load_1m = float(parts[0])
                    load_5m = float(parts[1])
                    load_15m = float(parts[2])
        except (IOError, OSError, ValueError):
            pass

        # --- Temperature (optional) ---
        temp_raw = self._read_int("/sys/class/thermal/thermal_zone0/temp")
        temp_c = None
        if temp_raw is not None:
            temp_c = int(temp_raw / 1000)
        else:
            self._warn_once("temp", "thermal_zone0 not found")

        # --- Uptime ---
        uptime = 0.0
        try:
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
        except (IOError, OSError, ValueError):
            pass

        # Build result
        result = {
            "ts": round(ts, 3),
            "type": "system",
            "cpu": {
                "overall_pct": cpu_overall,
                "per_core": per_core_pct if per_core_pct else [],
                "num_cores": num_cores,
            },
            "mem": {
                "total_kb": mem_total,
                "used_kb": mem_used,
                "available_kb": mem_avail,
                "buffers_kb": buffers,
                "cached_kb": cached,
                "swap_total_kb": swap_total,
                "swap_used_kb": swap_total - swap_free,
                "used_pct": mem_pct,
            },
            "load": {
                "avg_1m": round(load_1m, 2),
                "avg_5m": round(load_5m, 2),
                "avg_15m": round(load_15m, 2),
            },
            "uptime_sec": int(uptime),
            "context_switches": ctxt,
            "interrupts": interrupts,
            "procs_running": procs_running,
            "procs_blocked": procs_blocked,
        }
        if temp_c is not None:
            result["temp_c"] = temp_c
        if freq_mhz:
            result["cpu"]["freq_mhz"] = freq_mhz
        return result

    def _collect_process(self, ts):
        pid = self._pid
        if not pid:
            return None
        stat_path = "/proc/%d/stat" % pid
        try:
            with open(stat_path) as f:
                raw = f.read()
        except (IOError, OSError):
            return None

        # Parse /proc/<pid>/stat — comm (field 2) may contain parens and spaces.
        # Find last ')' to locate field 3 onwards.
        paren_end = raw.rfind(")")
        if paren_end < 0:
            return None
        # Fields before comm
        paren_start = raw.find("(")
        comm = raw[paren_start + 1:paren_end] if paren_start >= 0 else ""
        # Fields after comm (starting at field 3)
        fields = raw[paren_end + 2:].split()
        if len(fields) < 22:
            return None

        proc_state = fields[0]            # field 3
        minflt = int(fields[7])           # field 10
        majflt = int(fields[9])           # field 12
        utime = int(fields[11])           # field 14
        stime = int(fields[12])           # field 15
        num_threads = int(fields[17])     # field 20
        vsize = int(fields[20])           # field 23
        rss_pages = int(fields[21])       # field 24

        rss_kb = rss_pages * self._page_size // 1024
        vsize_kb = vsize // 1024

        # CPU % delta
        cpu_pct = None
        now = time.time()
        total_ticks = utime + stime
        if self._prev_proc_cpu is not None and self._prev_proc_time is not None:
            dt = now - self._prev_proc_time
            if dt > 0:
                dticks = total_ticks - self._prev_proc_cpu
                cpu_pct = round(100.0 * dticks / (dt * self._clk_tck), 1)
        self._prev_proc_cpu = total_ticks
        self._prev_proc_time = now

        # Voluntary/involuntary context switches from /proc/<pid>/status
        vol_csw = 0
        invol_csw = 0
        try:
            with open("/proc/%d/status" % pid) as f:
                for line in f:
                    if line.startswith("voluntary_ctxt_switches:"):
                        vol_csw = int(line.split()[1])
                    elif line.startswith("nonvoluntary_ctxt_switches:"):
                        invol_csw = int(line.split()[1])
        except (IOError, OSError):
            pass

        # FD count
        fds = 0
        try:
            fds = len(os.listdir("/proc/%d/fd" % pid))
        except (IOError, OSError):
            self._warn_once("proc_fd", "cannot read /proc/%d/fd" % pid)

        # OOM score
        oom = self._read_int("/proc/%d/oom_score" % pid, 0)

        return {
            "ts": round(ts, 3),
            "type": "process",
            "pid": pid,
            "comm": comm,
            "state": proc_state,
            "cpu_pct": cpu_pct,
            "rss_kb": rss_kb,
            "vsize_kb": vsize_kb,
            "threads": num_threads,
            "fds": fds,
            "voluntary_csw": vol_csw,
            "involuntary_csw": invol_csw,
            "minor_faults": minflt,
            "major_faults": majflt,
            "oom_score": oom,
        }

    def _collect_network(self, ts):
        try:
            with open("/proc/net/dev") as f:
                lines = f.readlines()
        except (IOError, OSError):
            self._warn_once("net", "/proc/net/dev not readable")
            return None

        interfaces = {}
        for line in lines[2:]:  # skip header lines
            parts = line.split()
            if len(parts) < 17:
                continue
            iface = parts[0].rstrip(":")
            if iface == "lo":
                continue
            interfaces[iface] = {
                "rx_bytes": int(parts[1]),
                "rx_packets": int(parts[2]),
                "rx_errors": int(parts[3]),
                "rx_drops": int(parts[4]),
                "tx_bytes": int(parts[9]),
                "tx_packets": int(parts[10]),
                "tx_errors": int(parts[11]),
            }

        if not interfaces:
            return None

        return {
            "ts": round(ts, 3),
            "type": "network",
            "interfaces": interfaces,
        }


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

            # Run perf script (use -F for normalized output if supported)
            cmd_script = [PERF, "script", "-i", tmp_path]
            if self._caps.script_fields:
                cmd_script = [PERF, "script", "-F", self._caps.script_fields, "-i", tmp_path]
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
# Helpers
# ---------------------------------------------------------------------------

def _get_local_ips():
    """Get local non-loopback IP addresses for display."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and ip != "0.0.0.0":
                ips.append(ip)
        except Exception:
            pass
        finally:
            s.close()
    except Exception:
        pass
    if not ips:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip and ip != "127.0.0.1":
                ips.append(ip)
        except Exception:
            pass
    return ips or ["127.0.0.1"]


def _read_total_cpu():
    """Read total CPU jiffies from /proc/stat."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = line.split()
        if parts[0] != "cpu":
            return 0
        total = 0
        for x in parts[1:]:
            total += int(x)
        return total
    except (IOError, OSError, ValueError):
        return 0


def list_processes():
    """List running processes with CPU usage from /proc.

    Takes two snapshots 0.5s apart to compute CPU%.
    Returns list of dicts sorted by CPU descending.
    """
    pids = []
    try:
        for entry in os.listdir("/proc"):
            try:
                pids.append(int(entry))
            except ValueError:
                pass
    except OSError:
        return []

    # Snapshot 1
    snap1 = {}
    total1 = _read_total_cpu()
    for pid in pids:
        try:
            with open("/proc/%d/stat" % pid) as f:
                parts = f.read().split()
            snap1[pid] = int(parts[13]) + int(parts[14])
        except (IOError, OSError, IndexError, ValueError):
            pass

    time.sleep(0.5)

    # Snapshot 2
    snap2 = {}
    total2 = _read_total_cpu()
    for pid in list(snap1.keys()):
        try:
            with open("/proc/%d/stat" % pid) as f:
                parts = f.read().split()
            snap2[pid] = int(parts[13]) + int(parts[14])
        except (IOError, OSError, IndexError, ValueError):
            pass

    total_delta = total2 - total1
    if total_delta <= 0:
        total_delta = 1

    result = []
    for pid in snap2:
        if pid not in snap1:
            continue
        cpu_pct = ((snap2[pid] - snap1[pid]) / float(total_delta)) * 100.0

        comm = "?"
        try:
            with open("/proc/%d/comm" % pid) as f:
                comm = f.read().strip()
        except (IOError, OSError):
            pass

        cmdline = ""
        try:
            with open("/proc/%d/cmdline" % pid) as f:
                cmdline = f.read().replace("\x00", " ").strip()[:200]
        except (IOError, OSError):
            pass

        result.append({
            "pid": pid,
            "comm": comm,
            "cmdline": cmdline,
            "cpu": round(cpu_pct, 1),
        })

    result.sort(key=lambda p: p["cpu"], reverse=True)
    return result[:200]


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


# ---------------------------------------------------------------------------
# Interactive agent (unified for --listen and --server modes)
# ---------------------------------------------------------------------------

class InteractiveAgent(object):
    """Bidirectional agent for interactive sessions.

    Handles both --listen (server connects to us) and --server (we connect
    to server) modes. After the TCP handshake, the protocol is identical:
    agent sends hello, then processes commands and streams profiling data.
    """

    def __init__(self, shutdown):
        self._shutdown = shutdown
        self._sock = None
        self._sock_lock = threading.Lock()
        self._cmd_queue = queue.Queue()
        self._state = AGENT_IDLE
        self._lock = threading.Lock()

        # Profiling config (overridable via configure command)
        self._pid = None
        self._frequency = 99
        self._duration = 8

        # Probed state
        self._platform = None
        self._caps = None
        self._comp = None

        # Collection thread state
        self._collector = None
        self._collect_thread = None
        self._collect_stop = threading.Event()

        # Per-session disconnect signal (set by recv loop on disconnect)
        self._session_done = threading.Event()

        # Metrics config
        self._metrics_enabled = True
        self._metrics_interval = 2
        self._metrics_network = True
        self._metrics_thread = None

    # --- Wire protocol ---

    def _send_msg(self, payload_bytes, flag):
        """Send message with 5-byte header. Thread-safe."""
        header = struct.pack("!IB", len(payload_bytes), flag)
        with self._sock_lock:
            self._sock.sendall(header + payload_bytes)

    def _send_response(self, cmd_id, data):
        """Send command response (flag 3)."""
        data["id"] = cmd_id
        payload = json.dumps(data).encode("utf-8")
        self._send_msg(payload, FLAG_CMD_RESPONSE)

    def _send_data(self, payload, comp_flag):
        """Send profiling data (flag 0 or 1)."""
        self._send_msg(payload, comp_flag)

    def _send_metrics(self, metrics_dict):
        """Send one metrics snapshot (flag 4)."""
        payload = json.dumps(metrics_dict, separators=(",", ":")).encode("utf-8")
        self._send_msg(payload, FLAG_METRICS)

    def _recv_loop(self):
        """Receiver thread: reads commands from server, puts on queue.

        Sets _session_done on disconnect (does NOT set _shutdown, so
        daemon modes can re-accept or reconnect).
        """
        while not self._shutdown.is_set and not self._session_done.is_set():
            try:
                header = recv_exactly(self._sock, 5)
                if header is None:
                    log("Server disconnected")
                    self._session_done.set()
                    break
                length, flag = struct.unpack("!IB", header)
                if length == 0:
                    continue
                payload = recv_exactly(self._sock, length)
                if payload is None:
                    log("Server disconnected mid-message")
                    self._session_done.set()
                    break
                if flag == FLAG_CMD_REQUEST:
                    try:
                        cmd = json.loads(payload.decode("utf-8", "replace"))
                        self._cmd_queue.put(cmd)
                    except ValueError as e:
                        log("Bad command JSON: %s" % e)
                else:
                    log("Unexpected flag %d from server" % flag)
            except (IOError, OSError) as e:
                if not self._shutdown.is_set:
                    log("Recv error: %s" % e)
                self._session_done.set()
                break

    # --- Command dispatch ---

    def _handle_command(self, cmd):
        cmd_name = cmd.get("cmd", "")
        cmd_id = cmd.get("id", "")
        args = cmd.get("args") or {}

        handlers = {
            "ping": self._cmd_ping,
            "status": self._cmd_status,
            "list_processes": self._cmd_list_processes,
            "verify_pid": self._cmd_verify_pid,
            "verify_perf": self._cmd_verify_perf,
            "reprobe": self._cmd_reprobe,
            "start": self._cmd_start,
            "stop": self._cmd_stop_profiling,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "configure": self._cmd_configure,
            "configure_metrics": self._cmd_configure_metrics,
        }

        handler = handlers.get(cmd_name)
        if handler:
            try:
                handler(cmd_id, args)
            except Exception as e:
                log("Command '%s' error: %s" % (cmd_name, e))
                try:
                    self._send_response(cmd_id, {"ok": False, "error": str(e)})
                except (IOError, OSError):
                    pass
        else:
            self._send_response(cmd_id, {
                "ok": False, "error": "unknown command: %s" % cmd_name,
            })

    # --- Command handlers ---

    def _cmd_ping(self, cmd_id, args):
        self._send_response(cmd_id, {"ok": True})

    def _cmd_status(self, cmd_id, args):
        with self._lock:
            data = {
                "ok": True,
                "state": self._state,
                "pid": self._pid,
                "frequency": self._frequency,
                "duration": self._duration,
            }
            if self._platform:
                data["platform"] = {
                    "arch": self._platform.arch,
                    "kernel": self._platform.kernel,
                    "perf_version": self._platform.perf_version,
                    "perf_event_paranoid": self._platform.perf_event_paranoid,
                }
            if self._caps:
                data["capabilities"] = {
                    "record_events": self._caps.record_events,
                    "stat_only_events": self._caps.stat_only_events,
                    "callgraph_method": self._caps.callgraph_method,
                }
        self._send_response(cmd_id, data)

    def _cmd_list_processes(self, cmd_id, args):
        procs = list_processes()
        self._send_response(cmd_id, {"ok": True, "processes": procs})

    def _cmd_verify_pid(self, cmd_id, args):
        pid = args.get("pid")
        if pid is None:
            self._send_response(cmd_id, {"ok": False, "error": "pid required"})
            return
        pid = int(pid)
        exists = _process_exists(pid)
        info = {}
        if exists:
            try:
                with open("/proc/%d/comm" % pid) as f:
                    info["comm"] = f.read().strip()
            except (IOError, OSError):
                pass
            try:
                with open("/proc/%d/cmdline" % pid) as f:
                    info["cmdline"] = f.read().replace("\x00", " ").strip()[:200]
            except (IOError, OSError):
                pass
        self._send_response(cmd_id, {
            "ok": True, "exists": exists, "pid": pid, "info": info,
        })

    def _cmd_verify_perf(self, cmd_id, args):
        try:
            rc, out, _ = _run_cmd([PERF, "--version"], timeout=5)
            version = out.decode("utf-8", "replace").strip() if rc == 0 else None
        except Exception:
            version = None

        if not version:
            self._send_response(cmd_id, {
                "ok": True, "available": False,
                "error": "perf not found or not working",
            })
            return

        # Quick functional check against self
        self_pid = os.getpid()
        try:
            rc, _, stderr = _run_cmd(
                [PERF, "stat", "-e", "cycles", "-p", str(self_pid),
                 "--", "sleep", "0"],
                timeout=10,
            )
            functional = (rc == 0)
            err = None if functional else stderr.decode("utf-8", "replace").strip()
        except Exception as e:
            functional = False
            err = str(e)

        paranoid = self._platform.perf_event_paranoid if self._platform else -1
        self._send_response(cmd_id, {
            "ok": True, "available": True, "version": version,
            "functional": functional, "error": err,
            "perf_event_paranoid": paranoid,
        })

    def _cmd_reprobe(self, cmd_id, args):
        pid = args.get("pid", self._pid)
        if pid is None:
            self._send_response(cmd_id, {"ok": False, "error": "pid required"})
            return
        pid = int(pid)
        if not _process_exists(pid):
            self._send_response(cmd_id, {
                "ok": False, "error": "process %d not found" % pid,
            })
            return

        log("Re-probing capabilities for PID %d..." % pid)
        caps = probe_capabilities(pid)
        with self._lock:
            self._caps = caps
            self._pid = pid
        self._send_response(cmd_id, {
            "ok": True,
            "record_events": caps.record_events,
            "stat_only_events": caps.stat_only_events,
            "callgraph_method": caps.callgraph_method,
        })

    def _cmd_start(self, cmd_id, args):
        with self._lock:
            if self._state == AGENT_PROFILING:
                self._send_response(cmd_id, {
                    "ok": False, "error": "already profiling",
                })
                return

        pid = args.get("pid", self._pid)
        if pid is None:
            self._send_response(cmd_id, {"ok": False, "error": "pid required"})
            return
        pid = int(pid)

        if not _process_exists(pid):
            self._send_response(cmd_id, {
                "ok": False, "error": "process %d not found" % pid,
            })
            return

        freq = int(args.get("frequency", self._frequency))
        dur = int(args.get("duration", self._duration))

        # Probe capabilities if needed
        with self._lock:
            if self._caps is None or self._pid != pid:
                self._pid = pid
        caps = probe_capabilities(pid)
        with self._lock:
            self._caps = caps
            self._frequency = freq
            self._duration = dur

        if not caps.record_events:
            self._send_response(cmd_id, {
                "ok": False,
                "error": "no perf record events available for PID %d" % pid,
            })
            return

        # Start collection thread
        self._collect_stop.clear()
        collector = PerfCollector(pid, caps, freq, dur, self._shutdown)
        with self._lock:
            self._collector = collector
            self._state = AGENT_PROFILING

        self._collect_thread = threading.Thread(
            target=self._collection_loop, args=(collector,),
        )
        self._collect_thread.daemon = True
        self._collect_thread.start()

        self._send_response(cmd_id, {
            "ok": True, "pid": pid, "frequency": freq, "duration": dur,
            "events": caps.record_events,
            "callgraph": caps.callgraph_method,
        })

    def _cmd_stop_profiling(self, cmd_id, args):
        with self._lock:
            if self._state not in (AGENT_PROFILING, AGENT_PAUSED):
                self._send_response(cmd_id, {
                    "ok": False, "error": "not profiling",
                })
                return

        self._collect_stop.set()
        if self._collector:
            self._collector.terminate_all()
        if self._collect_thread:
            self._collect_thread.join(timeout=15)
            self._collect_thread = None

        with self._lock:
            self._state = AGENT_IDLE
            self._collector = None

        self._send_response(cmd_id, {"ok": True})

    def _cmd_pause(self, cmd_id, args):
        with self._lock:
            if self._state != AGENT_PROFILING:
                self._send_response(cmd_id, {
                    "ok": False, "error": "not profiling",
                })
                return
            self._state = AGENT_PAUSED
        if self._collector:
            self._collector.terminate_all()
        self._send_response(cmd_id, {"ok": True})

    def _cmd_resume(self, cmd_id, args):
        with self._lock:
            if self._state != AGENT_PAUSED:
                self._send_response(cmd_id, {
                    "ok": False, "error": "not paused",
                })
                return
            self._state = AGENT_PROFILING
        self._send_response(cmd_id, {"ok": True})

    def _cmd_configure(self, cmd_id, args):
        with self._lock:
            if "frequency" in args:
                self._frequency = int(args["frequency"])
            if "duration" in args:
                self._duration = int(args["duration"])
        self._send_response(cmd_id, {
            "ok": True,
            "frequency": self._frequency,
            "duration": self._duration,
        })

    def _cmd_configure_metrics(self, cmd_id, args):
        self._metrics_enabled = args.get("enabled", True)
        self._metrics_interval = int(args.get("interval", 2))
        self._metrics_network = args.get("network", True)
        self._send_response(cmd_id, {
            "ok": True,
            "metrics_enabled": self._metrics_enabled,
            "interval": self._metrics_interval,
            "network": self._metrics_network,
        })

    # --- Metrics loop (runs in separate thread) ---

    def _metrics_loop(self):
        """Background thread: collect and send metrics at configured interval."""
        collector = MetricsCollector()
        while not self._shutdown.is_set and not self._session_done.is_set():
            if not self._metrics_enabled:
                self._session_done.wait(self._metrics_interval)
                continue

            with self._lock:
                pid = self._pid if self._state in (AGENT_PROFILING, AGENT_PAUSED) else None
            collector.set_pid(pid)
            collector.set_network(self._metrics_network)

            try:
                snapshots = collector.collect()
                for snap in snapshots:
                    self._send_metrics(snap)
            except (IOError, OSError):
                break
            except Exception as e:
                log_warn("Metrics error: %s" % e)

            self._session_done.wait(self._metrics_interval)

    # --- Collection loop (runs in separate thread) ---

    def _collection_loop(self, collector):
        comp = self._comp
        round_num = 0
        while (not self._collect_stop.is_set()
               and not self._shutdown.is_set
               and not self._session_done.is_set()):
            with self._lock:
                st = self._state
            if st == AGENT_PAUSED:
                self._collect_stop.wait(1.0)
                continue

            if not _process_exists(self._pid):
                log("Process %d exited" % self._pid)
                with self._lock:
                    self._state = AGENT_IDLE
                break

            round_num += 1
            log("Round %d: collecting (%ds)..." % (round_num, self._duration))
            output = collector.collect()

            if (self._collect_stop.is_set() or self._shutdown.is_set
                    or self._session_done.is_set()):
                break
            if not output:
                log("Round %d: no data" % round_num)
                self._collect_stop.wait(1.0)
                continue

            raw_bytes = output.encode("utf-8")
            payload, flag = compress_data(raw_bytes, comp)

            try:
                self._send_data(payload, flag)
                if flag == FLAG_DATA_ZSTD:
                    ratio = len(raw_bytes) / float(len(payload)) if len(payload) > 0 else 0
                    log("Round %d: sent %d bytes (%.1fx)" % (
                        round_num, len(payload), ratio))
                else:
                    log("Round %d: sent %d bytes" % (round_num, len(payload)))
            except (IOError, OSError) as e:
                log("Round %d: send failed: %s" % (round_num, e))
                break

        with self._lock:
            if self._state in (AGENT_PROFILING, AGENT_PAUSED):
                self._state = AGENT_IDLE
        log("Collection loop ended")

    # --- Interactive session (shared by listen and connect modes) ---

    def _run_interactive(self):
        """Run one interactive session on self._sock.

        Sends hello, starts recv loop, processes commands until the
        connection drops or shutdown is signaled. Returns when session ends.
        """
        # Fresh per-session state
        self._session_done = threading.Event()

        # Send hello handshake (agent always sends hello first)
        hello = {
            "type": "hello",
            "version": 1,
            "agent": "perflens",
            "platform": {
                "arch": self._platform.arch,
                "kernel": self._platform.kernel,
                "perf_version": self._platform.perf_version,
                "perf_event_paranoid": self._platform.perf_event_paranoid,
            },
        }
        try:
            self._send_msg(json.dumps(hello).encode("utf-8"), FLAG_CMD_RESPONSE)
        except (IOError, OSError) as e:
            log("Failed to send hello: %s" % e)
            self._cleanup_session(None)
            return

        # Start receiver thread
        recv_thread = threading.Thread(target=self._recv_loop)
        recv_thread.daemon = True
        recv_thread.start()

        # Start metrics thread
        self._metrics_thread = threading.Thread(target=self._metrics_loop)
        self._metrics_thread.daemon = True
        self._metrics_thread.start()

        # Command processing loop
        while not self._shutdown.is_set and not self._session_done.is_set():
            try:
                cmd = self._cmd_queue.get(timeout=1.0)
                self._handle_command(cmd)
            except queue.Empty:
                continue
            except Exception as e:
                log("Command loop error: %s" % e)

        # Cleanup session
        self._cleanup_session(recv_thread)

    def _cleanup_session(self, recv_thread):
        """Stop collection, close socket, reset state for next session."""
        log("Ending interactive session")

        # Stop any active collection
        self._collect_stop.set()
        if self._collector:
            self._collector.terminate_all()
        if self._collect_thread:
            self._collect_thread.join(timeout=5)

        # Close socket
        try:
            self._sock.close()
        except (IOError, OSError):
            pass

        # Wait for recv and metrics threads to exit
        if recv_thread is not None:
            recv_thread.join(timeout=5)
        if self._metrics_thread is not None:
            self._metrics_thread.join(timeout=5)
            self._metrics_thread = None

        # Reset state for next session
        with self._lock:
            self._state = AGENT_IDLE
            self._collector = None
        self._collect_thread = None
        self._sock = None
        self._collect_stop.clear()

        # Drain command queue
        while True:
            try:
                self._cmd_queue.get_nowait()
            except queue.Empty:
                break

    # --- Entry points ---

    def run_listen(self, port):
        """Listen mode: bind port, accept connections in a loop (daemon).

        After each session ends (server disconnects), the agent goes back
        to accepting new connections until shutdown is signaled.
        """
        self._platform = detect_platform()
        self._comp = probe_compression()
        ips = _get_local_ips()

        # Bind and listen
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen_sock.bind(("0.0.0.0", port))
        listen_sock.listen(1)
        listen_sock.settimeout(2.0)

        log("Listening on port %d" % port)
        log("Waiting for server connection...")
        for ip in ips:
            log("  Connect from server: %s:%d" % (ip, port))

        while not self._shutdown.is_set:
            # Accept loop
            conn = None
            while not self._shutdown.is_set:
                try:
                    conn, addr = listen_sock.accept()
                    log("Server connected from %s:%d" % (addr[0], addr[1]))
                    break
                except socket.timeout:
                    continue
                except (IOError, OSError) as e:
                    if not self._shutdown.is_set:
                        log("Accept error: %s" % e)
                    break

            if conn is None:
                continue

            self._sock = conn
            self._run_interactive()

            if not self._shutdown.is_set:
                log("Session ended, waiting for new connection...")

        try:
            listen_sock.close()
        except (IOError, OSError):
            pass

    def run_connect(self, host, port):
        """Connect mode: connect to server with backoff, reconnect on disconnect.

        After each session ends (server disconnects or network error), the
        agent reconnects with exponential backoff until shutdown is signaled.
        """
        self._platform = detect_platform()
        self._comp = probe_compression()

        while not self._shutdown.is_set:
            # Connect with exponential backoff
            delay = 1.0
            max_delay = 30.0
            sock = None
            while not self._shutdown.is_set:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(30)
                    sock.connect((host, port))
                    log("Connected to %s:%d" % (host, port))
                    break
                except OSError as e:
                    log("Connection failed (%s), retrying in %.0fs..." % (e, delay))
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass
                        sock = None
                    self._shutdown.wait(delay)
                    delay = min(delay * 2, max_delay)

            if sock is None:
                continue

            sock.settimeout(None)
            self._sock = sock
            self._run_interactive()

            if not self._shutdown.is_set:
                log("Session ended, reconnecting...")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    argp = argparse.ArgumentParser(description="PerfLens Device Agent")
    argp.add_argument("--pid", type=int, default=None,
                      help="PID of process to profile (headless mode)")
    argp.add_argument("--server", type=str, default=None,
                      help="Server IP to connect to")
    argp.add_argument("--port", type=int, default=9999,
                      help="Server/listen port (default: 9999)")
    argp.add_argument("--frequency", type=int, default=99,
                      help="Sampling frequency in Hz (default: 99)")
    argp.add_argument("--duration", type=int, default=8,
                      help="Duration of each collection in seconds (default: 8)")
    argp.add_argument("--listen", action="store_true", default=False,
                      help="Listen mode: bind port, wait for server to connect")
    argp.add_argument("--rounds", type=int, default=None,
                      help="Number of collection rounds (headless mode)")
    argp.add_argument("--output", type=str, default=None,
                      help="Output file for headless mode ('-' for stdout)")
    args = argp.parse_args()

    # --- Headless mode: --pid + --output ---
    if args.output is not None:
        if args.pid is None:
            argp.error("--pid is required for headless mode (--output)")
        if not _process_exists(args.pid):
            log("Error: process %d not found" % args.pid)
            sys.exit(1)

        rounds = args.rounds if args.rounds is not None else 1
        detect_platform()
        caps = probe_capabilities(args.pid)
        if not caps.record_events:
            log("Error: no perf record events available for PID %d" % args.pid)
            sys.exit(1)

        shutdown = ShutdownFlag()
        collector = PerfCollector(args.pid, caps, args.frequency, args.duration,
                                 shutdown)
        chunks = []
        for r in range(rounds):
            if shutdown.is_set:
                break
            if not _process_exists(args.pid):
                log("Process %d exited after %d rounds" % (args.pid, r))
                break
            log("Round %d/%d: collecting (%ds)..." % (r + 1, rounds, args.duration))
            output = collector.collect()
            if output:
                chunks.append(output)

        if not chunks:
            log("No data collected.")
            sys.exit(1)

        if args.output == "-":
            for chunk in chunks:
                try:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                except (IOError, OSError):
                    pass
        else:
            with open(args.output, "w") as f:
                for chunk in chunks:
                    f.write(chunk)
            log("Written %d rounds to %s" % (len(chunks), args.output))

        log("Done.")
        return

    # --- Interactive modes: --listen or --server ---
    if not args.listen and not args.server:
        argp.error("Use --listen, --server HOST, or --output FILE")

    shutdown = ShutdownFlag()
    agent = InteractiveAgent(shutdown)

    def _sig_handler(signum, frame):
        try:
            signame = signal.Signals(signum).name
        except Exception:
            signame = str(signum)
        log("Received %s, shutting down..." % signame)
        shutdown.set()
        if agent._collector:
            agent._collector.terminate_all()
        try:
            if agent._sock:
                agent._sock.close()
        except (IOError, OSError):
            pass

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    if args.listen:
        agent.run_listen(args.port)
    else:
        agent.run_connect(args.server, args.port)


if __name__ == "__main__":
    main()
