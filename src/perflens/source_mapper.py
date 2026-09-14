#!/usr/bin/env python3
"""Maps perf data to source code lines using debug symbols.

Supports:
- Batch addr2line via persistent pipe (-f flag, 2-line output format)
- Map file symbol resolution as fallback
- Path prefix mapping for cross-compiled binaries
- Per-module (shared library) resolution
- Caching across chunks
"""

import bisect
import os
import re
import select
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict

from perflens import symcache
from perflens.parser import UNKNOWN_FUNC

# Load-base recovery (see SourceMapper._prime_module_bases). Bases are
# page-aligned by the loader, which is what makes a single frame enough to
# pin one down; the vote thresholds only guard against a stray symbol-name
# collision, and the cap keeps priming O(1) on long sessions.
PAGE_SIZE = 0x1000
BASE_VOTE_CAP = 512
BASE_MIN_VOTES = 4
BASE_MIN_SHARE = 0.75
BASE_SCAN_SAMPLES = 20000
LAST_SYMBOL_SPAN = 1 << 20

# A tool that stops answering. addr2line answers a batch in milliseconds
# and readelf streams a symbol table continuously, so thirty seconds
# without a byte is a hang, not a slow binary. The worker used to block
# in readline() for good, which silently froze every UI update.
TOOL_READ_TIMEOUT = 30.0
# Consecutive deaths (crash, hang, failed start) before a binary's
# addr2line is given up on for this server run.
PIPE_MAX_FAILURES = 3


class ToolTimeout(Exception):
    """A child tool produced nothing for TOOL_READ_TIMEOUT seconds."""


def _stream_lines(proc, timeout=TOOL_READ_TIMEOUT):
    """Yield decoded lines from proc.stdout (a binary pipe), raising
    ToolTimeout when the tool goes quiet. Iterating `proc.stdout` directly
    has no timeout at all."""
    fd = proc.stdout.fileno()
    buf = b''
    while True:
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            raise ToolTimeout(f'no output for {timeout:.0f}s')
        data = os.read(fd, 1 << 16)
        if not data:
            if buf:
                yield buf.decode('utf-8', errors='replace')
            return
        buf += data
        start = 0
        while True:
            nl = buf.find(b'\n', start)
            if nl < 0:
                break
            yield buf[start:nl].decode('utf-8', errors='replace')
            start = nl + 1
        buf = buf[start:]


def _parse_addr2line_pair(func_line, file_line):
    """(func, file, line) from addr2line's two output lines."""
    file_line = re.sub(r'\s*\(discriminator \d+\)', '', file_line)
    if func_line == '??':
        return ('??', '??', 0)
    if file_line.startswith('??'):
        # A real symbol with no line info — ordinary for hand-written
        # assembly, which is exactly where a soft-float target spends its
        # time. The name is still good, and resolve_unknown_frames wants
        # it. Line consumers already gate on `lineno > 0`.
        return (func_line, '??', 0)
    # file:line  (rfind: Windows paths carry colons)
    idx = file_line.rfind(':')
    if idx > 0:
        try:
            return (func_line, file_line[:idx], int(file_line[idx + 1:]))
        except ValueError:
            pass
    return (func_line, '??', 0)


class MapFileParser:
    """Parse a GNU ld linker map file to extract symbol addresses.

    Handles formats:
        0x00000000004011a0    cpu_intensive
                0x00000000004011a0                cpu_intensive
    """

    def __init__(self, map_file_path):
        self.symbols = {}  # func_name -> vaddr
        self._parse(map_file_path)

    def _parse(self, path):
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, 'r', errors='replace') as f:
                for line in f:
                    # Match: optional whitespace, hex address, whitespace, identifier
                    m = re.match(
                        r'\s*(0x[0-9a-fA-F]+)\s+([A-Za-z_]\S*)', line
                    )
                    if m:
                        addr = int(m.group(1), 16)
                        name = m.group(2)
                        if addr > 0:
                            self.symbols[name] = addr
        except (IOError, OSError) as e:
            print(f"[source_mapper] WARNING: cannot read map file: {e}",
                  file=sys.stderr)

        if self.symbols:
            print(f"[source_mapper] Map file: loaded {len(self.symbols)} symbols",
                  file=sys.stderr)


class Addr2LinePipe:
    """Persistent addr2line process for batch address resolution.

    Uses -f flag only (no -i, no -p):
      Input:  one hex address per line
      Output: exactly 2 lines per address (function name, then file:line)

    This makes batch processing predictable — N addresses in → 2N lines out
    — which is also why every exchange runs under the pipe's lock: two
    threads interleaving their writes would each read the other's answers,
    and the pipe would stay desynchronized for the rest of its life.

    Reads time out (TOOL_READ_TIMEOUT); a hung or dead addr2line is killed
    and restarted on the next call, and after PIPE_MAX_FAILURES in a row
    the pipe is disabled with one log line. Addresses an exchange did not
    answer are simply absent from the result — never reported as '??'.
    """

    read_timeout = TOOL_READ_TIMEOUT

    def __init__(self, binary, addr2line_bin='addr2line', inline=False):
        self.binary = binary
        self.addr2line_bin = addr2line_bin
        self.inline = inline
        self._proc = None
        self._buf = b''
        self._lock = threading.Lock()
        self.failures = 0           # consecutive
        self.disabled = False

    def _ensure_started(self):
        if self.disabled:
            return False
        if self._proc is not None and self._proc.poll() is None:
            return True
        flags = ['-f', '-i'] if self.inline else ['-f']
        cmd = [self.addr2line_bin, '-e', self.binary] + flags
        self._buf = b''
        for argv in (['stdbuf', '-oL'] + cmd, cmd):
            try:
                self._proc = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0)
                return True
            except FileNotFoundError:
                continue        # no stdbuf, or no addr2line
            except OSError as e:
                self._fail(f'cannot start: {e}')
                return False
        self._proc = None
        self._fail(f'{self.addr2line_bin}: not found')
        return False

    def _readline(self):
        """One stdout line (stripped), or None at EOF. Raises ToolTimeout."""
        fd = self._proc.stdout.fileno()
        deadline = time.monotonic() + self.read_timeout
        while True:
            nl = self._buf.find(b'\n')
            if nl >= 0:
                line = self._buf[:nl]
                self._buf = self._buf[nl + 1:]
                return line.decode('utf-8', errors='replace').strip()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise ToolTimeout(f'no output for {self.read_timeout:.0f}s')
            data = os.read(fd, 1 << 16)
            if not data:
                return None
            self._buf += data

    def _fail(self, reason):
        """Kill the child (if any) and count the failure."""
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass    # already dead, or refusing to die in 2s
            self._proc = None
        self._buf = b''
        self.failures += 1
        name = os.path.basename(self.binary or '?')
        if self.failures >= PIPE_MAX_FAILURES:
            self.disabled = True
            print(f"[source_mapper] addr2line for {name} failed "
                  f"{self.failures} times in a row ({reason}); giving up on "
                  f"it for this server run — frames will stay unresolved",
                  file=sys.stderr)
        else:
            print(f"[source_mapper] addr2line for {name}: {reason}; "
                  f"restarting", file=sys.stderr)

    def _send(self, text):
        self._proc.stdin.write(text.encode('ascii'))
        self._proc.stdin.flush()

    def resolve_batch(self, addrs):
        """Resolve a list of addresses via the persistent pipe.

        Returns {addr: (func, file, line)} for the addresses answered.
        Processes in chunks to avoid pipe buffer deadlock.
        """
        if not addrs:
            return {}
        with self._lock:
            return self._resolve_batch_locked(list(addrs))

    def _resolve_batch_locked(self, addrs):
        results = {}
        CHUNK = 500  # safe for 64KB pipe buffer (~100 bytes output per addr)
        for i in range(0, len(addrs), CHUNK):
            chunk = addrs[i:i + CHUNK]
            if not self._ensure_started():
                return results
            try:
                self._send(''.join(f'{hex(a)}\n' for a in chunk))
                for addr in chunk:
                    func_line = self._readline()
                    file_line = self._readline() if func_line is not None else None
                    if func_line is None or file_line is None:
                        raise EOFError('exited mid-batch')
                    results[addr] = _parse_addr2line_pair(func_line, file_line)
            except (BrokenPipeError, OSError, EOFError, ToolTimeout) as e:
                self._fail(str(e))
                return results
        self.failures = 0
        return results

    def resolve_inline(self, addrs):
        """Resolve addresses with inline expansion via sentinel protocol.

        Returns {addr: [(func, file, line), ...]} where index 0 is innermost,
        for the addresses answered. Processes one address at a time with a
        0x0 sentinel to delimit output.
        """
        if not addrs:
            return {}
        with self._lock:
            return self._resolve_inline_locked(list(addrs))

    def _resolve_inline_locked(self, addrs):
        results = {}
        if not self._ensure_started():
            return results
        try:
            for addr in addrs:
                self._send(f'{hex(addr)}\n0x0\n')
                chain = []
                while True:
                    func_line = self._readline()
                    file_line = self._readline() if func_line is not None else None
                    if func_line is None or file_line is None:
                        raise EOFError('exited mid-batch')
                    # Sentinel detection: 0x0 produces ?? / ??:0
                    if func_line == '??' and file_line.startswith('??'):
                        if not chain:
                            # ?? was from the real address; sentinel pending
                            if self._readline() is None or self._readline() is None:
                                raise EOFError('exited mid-batch')
                        break
                    func, fpath, lineno = _parse_addr2line_pair(func_line, file_line)
                    chain.append((func, fpath, lineno))
                results[addr] = chain if chain else [('??', '??', 0)]
        except (BrokenPipeError, OSError, EOFError, ToolTimeout) as e:
            self._fail(str(e))
            return results
        self.failures = 0
        return results

    def close(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.stdin.close()
                    self._proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    self._proc.kill()
            self._proc = None
            self._buf = b''


class SourceMapper:
    """Maps function+offset from perf data to source file and line.

    Created once at server startup and shared across all requests.
    """

    def __init__(self, source_dir, binary_path=None, map_file_path=None,
                 addr2line_bin=None, readelf_bin=None, path_map=None,
                 inline=False, sysroot=None, dwarfdump_bin=None,
                 sym_cache=None, module_map=None):
        self.source_dir = os.path.abspath(source_dir)
        self.binary_path = binary_path
        self.addr2line_bin = addr2line_bin
        self.readelf_bin = readelf_bin or 'readelf'
        self.dwarfdump_bin = dwarfdump_bin  # llvm-dwarfdump, when available
        self.path_map = path_map or {}
        self.inline = inline
        self.sysroot = sysroot
        # Device module path -> local file. --sysroot covers a whole tree;
        # this covers the single path that does not live under one, which is
        # the normal case for a firmware image.
        self.module_map = module_map or {}

        # One mapper is shared by the rebuild worker and every request
        # thread. The addr2line pipes have their own locks; this one covers
        # the cache-mutating phases (symbol loads, base recovery, the
        # address caches), which are not safe to interleave either.
        # Reentrant: the public methods call each other.
        self._lock = threading.RLock()
        self._closed = False

        # Persistent cross-restart cache (~/.perflens/cache)
        self._owns_cache = sym_cache is None
        self._sym_cache = sym_cache if sym_cache is not None \
            else symcache.SymbolCache()
        self._bkeys = {}            # binary path -> identity key (memoized)
        self._a2l_loaded = set()    # binaries whose addr2line rows are loaded
        self._inline_loaded = set() # binaries whose inline rows are loaded

        # Map file symbols
        self._map_symbols = {}
        if map_file_path:
            parser = MapFileParser(map_file_path)
            self._map_symbols = parser.symbols

        # Cache: binary -> {func_name: vaddr}
        self._symbol_cache = {}
        # Cache: (binary, addr) -> (file, line)
        self._addr2line_cache = {}
        # Load-base recovery for perf output without `symoff` (see
        # _prime_module_bases): binary -> page-aligned base, and the set of
        # binaries we already tried and could not pin down.
        self._load_base = {}
        self._base_unresolvable = set()
        # binary -> {symbol start: end}, memoized (see _symbol_spans)
        self._sym_spans = {}
        # binary -> sorted symbol starts, memoized alongside _sym_spans so a
        # reverse (address -> symbol) lookup can bisect them
        self._sym_starts = {}
        # Frame-naming tally, for /api/index/status. Lives here rather than
        # on an accumulator because replay and import each build their own
        # AggregatorSet, while the mapper is shared by every path.
        self._sym_seen = 0          # userspace frames examined
        self._sym_unknown_in = 0    # ... that arrived as [unknown]
        self._sym_named = 0         # ... that we managed to name
        # (binary, vaddr) -> function name, or None when we could not name it
        # (negative entries matter: the same unnameable address recurs in
        # every chunk). Deliberately not _vaddr_cache's key, which already
        # holds negative entries under (binary, '[unknown]', '').
        self._symname_cache = {}
        # Persistent addr2line pipes per binary
        self._pipes = {}
        # Inline addr2line pipes per binary (use -i flag)
        self._inline_pipes = {}
        # Inline resolution cache: (binary, addr) -> [(func, file, line), ...] or None
        self._inline_cache = {}
        # vaddr cache: (binary, func, offset_str) -> vaddr or None
        self._vaddr_cache = {}
        # Source file index: basename -> [full_paths]. Loaded instantly
        # from the persistent cache when available; (re)built by a
        # background thread — request paths NEVER trigger a tree walk.
        self._source_index = None
        self._index_build_lock = threading.Lock()
        self._index_building = False
        # Full path cache: reported_path -> actual_path
        self._path_cache = {}

        # Pre-indexing state (populated by pre_index())
        self._indexing = False
        self._dwarf_source_files = []  # list of source file paths from DWARF
        self._index_symbols_loaded = 0
        self._index_source_files_found = 0

        # Instant (possibly stale) index from a previous run
        cached_index = symcache.load_source_index(self.source_dir)
        if cached_index:
            self._source_index = defaultdict(list, cached_index)
            print("[source_mapper] Source index loaded from cache "
                  f"({sum(len(v) for v in cached_index.values())} files); "
                  "refreshing in background", file=sys.stderr)

        # Probe inline support at startup
        if self.inline:
            if self._probe_inline_support():
                print("[source_mapper] Inline resolution enabled (-i supported)",
                      file=sys.stderr)
            else:
                self.inline = False
                if not self.binary_path:
                    print("[source_mapper] Inline resolution off "
                          "(no --binary configured yet)", file=sys.stderr)
                else:
                    print("[source_mapper] Inline resolution disabled "
                          "(-i not supported by addr2line)", file=sys.stderr)

    def _get_pipe(self, binary):
        """Get or create an addr2line pipe for a binary. None when there
        is nothing to run it on, or when the pipe has been given up on."""
        pipe = self._pipes.get(binary)
        if pipe is None:
            if self._closed or not (binary and os.path.isfile(binary)
                                    and self.addr2line_bin):
                return None
            pipe = self._pipes[binary] = Addr2LinePipe(binary, self.addr2line_bin)
        return None if pipe.disabled else pipe

    def _get_inline_pipe(self, binary):
        """Get or create an inline addr2line pipe for a binary."""
        pipe = self._inline_pipes.get(binary)
        if pipe is None:
            if self._closed or not (binary and os.path.isfile(binary)
                                    and self.addr2line_bin):
                return None
            pipe = self._inline_pipes[binary] = Addr2LinePipe(
                binary, self.addr2line_bin, inline=True)
        return None if pipe.disabled else pipe

    def _probe_inline_support(self):
        """Check if addr2line supports the -i (inline) flag."""
        binary = self.binary_path
        if not binary or not self.addr2line_bin:
            return False
        if not os.path.isfile(binary):
            return False
        try:
            r = subprocess.run(
                [self.addr2line_bin, '-e', binary, '-f', '-i'],
                input='0x0\n',
                capture_output=True, text=True, timeout=5
            )
            return r.returncode == 0 and '??' in r.stdout
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return False

    def _bkey(self, binary):
        """Persistent-cache identity for a binary (memoized)."""
        if binary not in self._bkeys:
            self._bkeys[binary] = symcache.binary_key(binary) if binary else None
        return self._bkeys[binary]

    def _load_symbols(self, binary):
        """Load symbol table. Priority: persistent cache → readelf → map file.

        Uses streaming parse for readelf output so that very large
        binaries (100-200 MB+) don't require the entire symbol table
        text to be held in memory at once. Results persist in
        ~/.perflens/cache/symbols.db so restarts skip readelf entirely.
        """
        if binary in self._symbol_cache:
            return self._symbol_cache[binary]
        with self._lock:
            return self._load_symbols_locked(binary)

    def _load_symbols_locked(self, binary):
        if binary in self._symbol_cache:      # loaded while we waited
            return self._symbol_cache[binary]

        bkey = self._bkey(binary)
        cached = self._sym_cache.load_symtab(bkey)
        if cached is not None:
            for name, addr in self._map_symbols.items():
                if name not in cached:
                    cached[name] = addr
            self._symbol_cache[binary] = cached
            print(f"[source_mapper] Symbols from cache: {len(cached)} "
                  f"({os.path.basename(binary or '?')})", file=sys.stderr)
            return cached

        symbols = {}
        complete = False

        # Try readelf first (per-binary, accurate).  Stream output
        # line-by-line so we never hold the full symbol table in RAM.
        if binary and os.path.isfile(binary):
            proc = None
            try:
                proc = subprocess.Popen(
                    [self.readelf_bin, '-s', '-W', binary],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                for line in _stream_lines(proc):
                    parts = line.split()
                    if len(parts) >= 8 and parts[3] == 'FUNC':
                        try:
                            addr = int(parts[1], 16)
                        except ValueError:
                            continue  # readelf formats vary; skip odd lines
                        name = parts[7]
                        if addr > 0:
                            symbols[name] = addr
                proc.wait(timeout=300)
                complete = proc.returncode == 0
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
                    ToolTimeout) as e:
                print(f"[source_mapper] readelf -s on "
                      f"{os.path.basename(binary)}: {e}", file=sys.stderr)
            finally:
                if proc and proc.poll() is None:
                    proc.kill()
                    proc.wait()

        # Persist before merging map-file symbols (which belong to the map
        # file, not to this binary's identity) — but only a table readelf
        # finished, or a hang would be remembered as a binary with few
        # symbols for as long as the cache lives.
        if complete:
            self._sym_cache.store_symtab(bkey, symbols)

        # Supplement with map file symbols
        for name, addr in self._map_symbols.items():
            if name not in symbols:
                symbols[name] = addr

        self._symbol_cache[binary] = symbols
        return symbols

    def _symbol_spans(self, binary):
        """{symbol start -> end} for a binary, memoized.

        readelf gives us addresses but the symbol table we persist carries no
        sizes, so a symbol's end is taken as the next symbol's start. That is
        an approximation, but it only ever has to be good enough to bound the
        load base in _base_candidate().
        """
        spans = self._sym_spans.get(binary)
        if spans is not None:
            return spans
        with self._lock:
            return self._symbol_spans_locked(binary)

    def _symbol_spans_locked(self, binary):
        spans = self._sym_spans.get(binary)
        if spans is not None:
            return spans
        addrs = sorted(set(self._load_symbols(binary).values()))
        spans = {}
        for i, addr in enumerate(addrs):
            # The last symbol has no successor to bound it; be generous, since
            # this bound only gates _base_candidate (which ignores spans wider
            # than a page anyway) and the sanity check in _compute_vaddr.
            nxt = addrs[i + 1] if i + 1 < len(addrs) else addr + LAST_SYMBOL_SPAN
            spans[addr] = nxt
        self._sym_spans[binary] = spans
        self._sym_starts[binary] = addrs
        return spans

    def _base_candidate(self, frame, binary):
        """The page-aligned load base implied by a single frame, or None.

        perf reports the raw instruction pointer, which for a PIE binary is
        `base + file_vaddr`. The base is page-aligned, and the file_vaddr must
        lie inside the frame's symbol, so

            base == floor_to_page(ip - symbol_start)

        is the only solution whenever the symbol spans at most one page. We
        only vote with those frames; larger symbols admit several candidates
        and would just add noise.
        """
        addr_str = frame.get('addr') or ''
        try:
            ip = int(addr_str, 16)
        except (TypeError, ValueError):
            return None

        symbols = self._load_symbols(binary)
        start = symbols.get(frame.get('func') or '')
        if start is None or start <= 0:
            return None

        end = self._symbol_spans(binary).get(start, start + PAGE_SIZE)
        if end - start > PAGE_SIZE:
            return None

        base = (ip - start) & ~(PAGE_SIZE - 1)
        # base must also satisfy file_vaddr < end, i.e. base > ip - end
        if base <= ip - end or base < 0:
            return None
        return base

    def _prime_module_bases(self, samples):
        """Work out where each module was loaded, so frames without a
        `symoff` can still be resolved to a real line.

        `perf script -F ...,sym,dso` prints a bare symbol name; only the
        `symoff` field adds `+0x<offset>`. Agents built before that field was
        requested — and every session already saved to disk — therefore carry
        no offset, and _compute_vaddr used to fall back to the symbol address,
        which resolves every sample in a function to its declaration line.

        The instruction pointer is in the data either way, so recovering the
        load base recovers real line-level annotation. Votes are taken across
        the corpus rather than trusting one frame, because a leaf symbol name
        can collide with an unrelated module's when --binary overrides the
        module path.
        """
        # Called once per chunk, so it must not walk a large corpus after
        # there is nothing left to learn. With --binary there is exactly one
        # module, and once it is decided this is a single dict lookup.
        if self.binary_path and (self.binary_path in self._load_base
                                 or self.binary_path in self._base_unresolvable):
            return

        votes = defaultdict(Counter)
        for sample in samples[:BASE_SCAN_SAMPLES]:
            for frame in sample['frames']:
                if frame.get('offset'):
                    continue  # exact path, no base needed
                binary = (self.binary_path
                          or self._resolve_module_path(frame.get('module', '')))
                if (not binary or binary in self._load_base
                        or binary in self._base_unresolvable):
                    continue
                tally = votes[binary]
                if sum(tally.values()) >= BASE_VOTE_CAP:
                    continue
                candidate = self._base_candidate(frame, binary)
                if candidate is not None:
                    tally[candidate] += 1

        if not votes:
            return  # nothing to decide; try again on the next chunk

        for binary, tally in votes.items():
            total = sum(tally.values())
            if not total:
                self._base_unresolvable.add(binary)
                continue
            base, hits = tally.most_common(1)[0]
            if hits >= BASE_MIN_VOTES and hits >= total * BASE_MIN_SHARE:
                self._load_base[binary] = base
                print(f"[source_mapper] Recovered load base 0x{base:x} for "
                      f"{os.path.basename(binary or '?')} "
                      f"({hits}/{total} frames agree) — perf output carries "
                      f"no symoff, resolving line numbers from ip",
                      file=sys.stderr)
            else:
                self._base_unresolvable.add(binary)

    def _vaddr_from_ip(self, frame, binary, func):
        """File virtual address recovered from the raw ip, or None.

        The result must land inside the very symbol perf named, which is what
        makes this safe when --binary overrides the module path: frames from
        libc or the kernel are then attributed to the main binary, and
        subtracting its base would otherwise yield an address belonging to
        nothing (or one too large for the cache to store).
        """
        base = self._load_base.get(binary)
        if base is None:
            return None
        start = self._load_symbols(binary).get(func)
        if start is None:
            return None
        try:
            vaddr = int(frame['addr'], 16) - base
        except (KeyError, TypeError, ValueError):
            return None
        end = self._symbol_spans(binary).get(start, start + PAGE_SIZE)
        if start <= vaddr < end:
            return vaddr
        return None

    def _compute_vaddr(self, frame, binary):
        """Compute the file virtual address for a frame.

        Two routes, in order of precision:

        1. `func+0x<offset>`, which `perf script` prints when asked for the
           `symoff` field. Exact and independent of where the module landed.
        2. The raw ip minus the module's load base, for output that has no
           offset (see _prime_module_bases).

        The symbol address alone is the last resort: it collapses every
        sample in a function onto that function's declaration line.
        """
        func = frame['func']
        offset_str = frame.get('offset', '')

        if not offset_str:
            vaddr = self._vaddr_from_ip(frame, binary, func)
            if vaddr is not None:
                return vaddr

        cache_key = (binary, func, offset_str)
        cached = self._vaddr_cache.get(cache_key)
        if cached is not None:
            return cached
        # Distinguish "not cached" from "cached as None"
        if cache_key in self._vaddr_cache:
            return None

        symbols = self._load_symbols(binary)
        if func not in symbols:
            self._vaddr_cache[cache_key] = None
            return None

        func_addr = symbols[func]
        if offset_str.startswith('0x'):
            offset = int(offset_str, 16)
        elif offset_str:
            try:
                offset = int(offset_str)
            except ValueError:
                self._vaddr_cache[cache_key] = None
                return None
        else:
            offset = 0

        result = func_addr + offset
        self._vaddr_cache[cache_key] = result
        return result

    def _resolve_addrs_batch(self, binary, addrs):
        """Resolve multiple addresses at once using the pipe.

        First touch of a binary bulk-loads its previously resolved
        addresses from the persistent cache; anything newly resolved is
        written back, so restarts against the same binary skip addr2line.
        """
        if binary not in self._a2l_loaded:
            self._a2l_loaded.add(binary)
            persisted = self._sym_cache.load_addr2line(self._bkey(binary))
            for vaddr, (fpath, lineno) in persisted.items():
                self._addr2line_cache.setdefault((binary, vaddr),
                                                 (fpath, lineno))
            if persisted:
                print(f"[source_mapper] addr2line cache: {len(persisted)} "
                      f"addrs ({os.path.basename(binary or '?')})",
                      file=sys.stderr)

        uncached = [a for a in addrs
                    if (binary, a) not in self._addr2line_cache]
        if not uncached:
            return

        pipe = self._get_pipe(binary)
        if not pipe:
            for addr in uncached:
                self._addr2line_cache[(binary, addr)] = ('??', 0)
            return

        batch_results = pipe.resolve_batch(uncached)
        new_entries = {}
        # An address the pipe did not answer (it died or hung mid-batch)
        # is left uncached, so the next chunk asks again. Caching it as
        # '??' — let alone persisting that — turned one addr2line hiccup
        # into a permanently blank line for that address.
        for addr, (_func, fpath, lineno) in batch_results.items():
            entry = (fpath, lineno) if fpath != '??' and lineno > 0 else ('??', 0)
            self._addr2line_cache[(binary, addr)] = entry
            new_entries[addr] = entry
        self._sym_cache.store_addr2line(self._bkey(binary), new_entries)

    def map_samples_to_lines(self, samples, primed=False):
        """Map all samples to source lines using batch resolution.

        Returns: {file_path: {line_no: {'samples': int}}}

        `primed`: the caller already ran _prime_module_bases over these
        samples this chunk (AggregatorSet.add_chunk does, once, instead of
        each of its three mapper calls scanning the chunk again).
        """
        with self._lock:
            return self._map_samples_to_lines_locked(samples, primed)

    def _map_samples_to_lines_locked(self, samples, primed):
        # Step 0: pin down where each module was loaded, so frames without a
        # symoff resolve to a real line instead of the function's first one.
        if not primed:
            self._prime_module_bases(samples)

        # Step 1: Collect all unique addresses per binary
        addrs_per_binary = defaultdict(set)
        frame_addrs = []  # (sample_idx, binary, vaddr)

        for i, sample in enumerate(samples):
            if not sample['frames']:
                continue
            frame = sample['frames'][0]
            binary = self._binary_for_frame(frame)
            if not binary:
                continue
            vaddr = self._compute_vaddr(frame, binary)
            if vaddr is not None:
                addrs_per_binary[binary].add(vaddr)
                frame_addrs.append((i, binary, vaddr))

        # Step 2: Batch resolve all addresses
        for binary, addrs in addrs_per_binary.items():
            self._resolve_addrs_batch(binary, list(addrs))

        # Step 3: Build line data from cached results
        line_data = defaultdict(lambda: defaultdict(lambda: {'samples': 0}))
        for _i, binary, vaddr in frame_addrs:
            file_path, line_no = self._addr2line_cache.get(
                (binary, vaddr), ('??', 0))
            if file_path != '??' and line_no > 0:
                line_data[file_path][line_no]['samples'] += 1

        return dict(line_data)

    # Modules that are certainly not the executable named by --binary:
    # shared objects, and perf's bracketed pseudo-modules
    # ([kernel.kallsyms], [unknown], [vdso], ...).
    _SO_SUFFIX = re.compile(r'\.so(\.\d+)*$')

    def _is_main_binary(self, module):
        """Whether `module` plausibly refers to the --binary executable.

        Deliberately conservative. It cannot be a basename comparison: the
        documented cross-compilation workflow points --binary at a separate
        unstripped build (matrixlab.sym) whose name does not match the path
        perf reports for the running process (/opt/matrixlab). So instead of
        proving a match, rule out the cases that are certainly not it.
        """
        if not module:
            return True             # nothing better to try
        if module.startswith('['):  # [kernel.kallsyms], [unknown], [vdso]
            return False
        return not self._SO_SUFFIX.search(module)

    def _binary_for_frame(self, frame):
        """The binary whose symbols should resolve this frame.

        --binary names the unstripped build of the profiled executable, and
        applying it to every frame asks addr2line for addresses that are not
        in that file. Usually the lookup simply fails — but it is also why
        ip-based line recovery needs its span check, because a libc ip minus
        the main binary's load base can land inside a real function there and
        yield a confidently wrong source line. On a typical capture most
        frames are libc/libm/kernel, so this is the common case, not an edge
        one.
        """
        module = frame.get('module', '') or ''
        if self.binary_path and self._is_main_binary(module):
            return self.binary_path
        return self._resolve_module_path(module)

    def _resolve_module_path(self, module):
        """Resolve a module path from perf output to a local binary.

        An explicit --module-map entry wins. Otherwise, if sysroot is set,
        prepend it to absolute paths (e.g. /usr/lib/libc.so ->
        /opt/sysroot/usr/lib/libc.so).
        """
        if not module:
            return module
        mapped = self.module_map.get(module)
        if mapped:
            return mapped
        if self.sysroot and module.startswith('/'):
            candidate = os.path.join(self.sysroot, module.lstrip('/'))
            if os.path.isfile(candidate):
                return candidate
        return module

    def _apply_path_map(self, file_path):
        """Apply compile-time path prefix mappings."""
        for compile_prefix, server_prefix in self.path_map.items():
            if file_path.startswith(compile_prefix):
                return file_path.replace(compile_prefix, server_prefix, 1)
        return file_path

    # Directories that are never useful for source mapping.
    _SKIP_DIRS = frozenset((
        'node_modules', '__pycache__', '.git', '.svn', '.hg',
        'build', 'cmake-build', '_build', 'obj', 'out', 'output',
        'third_party', 'external', 'deps', 'vendor',
    ))

    def _scan_source_tree(self):
        """Walk the source tree (os.scandir, iterative) and return a fresh
        basename -> [full_paths] index. Safe to run in a background thread."""
        index = defaultdict(list)
        stack = [self.source_dir]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        name = entry.name
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if (not name.startswith('.')
                                        and name not in self._SKIP_DIRS):
                                    stack.append(entry.path)
                            else:
                                index[name].append(entry.path)
                        except OSError:
                            continue
            except OSError:
                continue
        return index

    def _build_source_index(self, force=False):
        """(Re)build the source index synchronously and persist it.
        Called from background threads and pre_index() — never from a
        request path."""
        if self._source_index is not None and not force:
            return
        index = self._scan_source_tree()
        self._source_index = index  # atomic swap
        # Retry-able negatives may now resolve
        self._path_cache = {k: v for k, v in self._path_cache.items()
                            if v is not None}
        symcache.save_source_index(self.source_dir, dict(index))

    def start_background_index(self):
        """Kick a background (re)build of the source index. The request
        path keeps serving from the cached/stale index (or exact-path
        checks only) until the fresh one atomically swaps in."""
        with self._index_build_lock:
            if self._index_building:
                return
            self._index_building = True

        def worker():
            try:
                self._build_source_index(force=True)
                total = sum(len(v) for v in (self._source_index or {}).values())
                print(f"[source_mapper] Source index ready: {total} files "
                      f"in {self.source_dir}", file=sys.stderr)
            finally:
                with self._index_build_lock:
                    self._index_building = False

        threading.Thread(target=worker, daemon=True,
                         name='source-index').start()

    def _find_source_file(self, file_path):
        """Find a source file: path map → exact path → basename match.

        Never triggers a tree walk: if the index isn't ready yet the
        basename fallback is skipped (and the miss is NOT cached, so the
        lookup retries once the background index lands)."""
        if file_path in self._path_cache:
            return self._path_cache[file_path]

        # Apply path mapping first
        mapped = self._apply_path_map(file_path)

        result = None

        # Try exact path
        if os.path.isfile(mapped):
            result = mapped
        elif self.sysroot and mapped.startswith('/'):
            # Try sysroot-prefixed path (cross-compilation)
            sysroot_path = os.path.join(self.sysroot, mapped.lstrip('/'))
            if os.path.isfile(sysroot_path):
                result = sysroot_path

        index = self._source_index
        if result is None:
            if index is None:
                # Index not built yet — don't cache the miss
                return None
            # Try basename matching
            basename = os.path.basename(mapped)
            candidates = index.get(basename, [])

            if len(candidates) == 1:
                result = candidates[0]
            elif len(candidates) > 1:
                # Match longest common suffix
                parts = mapped.replace('\\', '/').split('/')
                best_match = None
                best_score = 0
                for cand in candidates:
                    cand_parts = cand.replace('\\', '/').split('/')
                    score = 0
                    for a, b in zip(reversed(parts), reversed(cand_parts),
                                    strict=False):
                        if a == b:
                            score += 1
                        else:
                            break
                    if score > best_score:
                        best_score = score
                        best_match = cand
                result = best_match

        self._path_cache[file_path] = result
        return result

    # Hard cap on annotated-source lines returned.  Source files larger
    # than this are truncated to keep JSON responses manageable.  The UI
    # already caps rendering at ~2000 lines, so this avoids sending huge
    # payloads for auto-generated code.
    MAX_SOURCE_LINES = 15000

    def annotate_source(self, file_path, line_samples):
        """Read a source file and annotate it with sample data.

        Args:
            file_path: path to source file (as reported by addr2line)
            line_samples: {line_no: {'samples': int}}

        Returns:
            list of {'line': int, 'source': str, 'samples': int, 'percent': float}
        """
        actual_path = self._find_source_file(file_path)
        if actual_path is None:
            return []

        total_samples = sum(d['samples'] for d in line_samples.values())

        # Find the hottest line so we guarantee it is within the window.
        hottest_line = 0
        hottest_samples = 0
        for ln, d in line_samples.items():
            if d['samples'] > hottest_samples:
                hottest_samples = d['samples']
                hottest_line = ln

        result = []
        try:
            with open(actual_path, 'r', errors='replace') as f:
                for i, source_line in enumerate(f, 1):
                    samples = line_samples.get(i, {}).get('samples', 0)
                    pct = round(100.0 * samples / total_samples, 2) if total_samples > 0 else 0.0
                    result.append({
                        'line': i,
                        'source': source_line.rstrip(),
                        'samples': samples,
                        'percent': pct
                    })
        except (FileNotFoundError, PermissionError):
            pass

        # Truncate when the file is very large, keeping lines around
        # the hottest region so the most relevant code is always visible.
        if len(result) > self.MAX_SOURCE_LINES:
            keep_start = max(0, hottest_line - self.MAX_SOURCE_LINES // 2)
            keep_end = keep_start + self.MAX_SOURCE_LINES
            if keep_end > len(result):
                keep_end = len(result)
                keep_start = max(0, keep_end - self.MAX_SOURCE_LINES)
            result = result[keep_start:keep_end]

        return result

    def get_files_with_samples(self, samples):
        """Return list of source files that have samples, with sample counts."""
        with self._lock:
            return self._get_files_with_samples_locked(samples)

    def _get_files_with_samples_locked(self, samples):
        line_data = self._map_samples_to_lines_locked(samples, False)

        # Build function-to-file mapping from cached results
        file_functions = defaultdict(set)
        for sample in samples:
            if not sample['frames']:
                continue
            frame = sample['frames'][0]
            binary = self._binary_for_frame(frame)
            vaddr = self._compute_vaddr(frame, binary)
            if vaddr is not None:
                fpath, lineno = self._addr2line_cache.get(
                    (binary, vaddr), ('??', 0))
                if fpath != '??' and lineno > 0:
                    file_functions[fpath].add(frame['func'])

        file_list = []
        for fpath, lines in line_data.items():
            total = sum(d['samples'] for d in lines.values())
            actual = self._find_source_file(fpath)
            file_list.append({
                'path': fpath,
                'found': actual is not None,
                'total_samples': total,
                'functions': sorted(file_functions.get(fpath, [])),
            })
        file_list.sort(key=lambda x: x['total_samples'], reverse=True)
        return file_list

    def symbolization_stats(self):
        """Userspace frame naming so far. See resolve_unknown_frames."""
        return {
            'userspace_frames': self._sym_seen,
            'unknown_frames': self._sym_unknown_in - self._sym_named,
            'resolved_frames': self._sym_named,
        }

    def _symbol_starts(self, binary):
        """Sorted symbol start addresses, for address -> symbol lookups."""
        if binary not in self._sym_starts:
            self._symbol_spans(binary)      # fills both maps
        return self._sym_starts.get(binary, [])

    def _unknown_vaddr(self, frame, binary):
        """File vaddr for a frame perf could not name, or None.

        perf prints the ip in whichever form it managed: a file-relative
        offset when it worked out the module's load base, the raw runtime
        address when it did not. Both shapes were observed for the same
        capture, so try each and keep only a candidate that lands inside a
        real symbol.

        That containment check is the guard against inventing names. It is
        not sufficient on its own — spans are approximated as the next
        symbol's start, so inter-function padding reads as part of the
        preceding symbol — which is why addr2line has to agree as well.
        """
        try:
            ip = int(frame.get('addr') or '', 16)
        except ValueError:
            return None

        candidates = []
        base = self._load_base.get(binary)
        if base is not None and ip >= base:
            candidates.append(ip - base)
        candidates.append(ip)

        spans = self._symbol_spans(binary)
        if not spans:
            return None
        starts = self._symbol_starts(binary)
        for vaddr in candidates:
            i = bisect.bisect_right(starts, vaddr) - 1
            if i < 0:
                continue
            start = starts[i]
            if start <= vaddr < spans.get(start, start):
                return vaddr
        return None

    def _resolve_symnames_batch(self, binary, addrs):
        """Fill _symname_cache for these addresses, negative entries included."""
        pipe = self._get_pipe(binary)
        if pipe is None:
            for addr in addrs:
                self._symname_cache[(binary, addr)] = None
            return
        # Only what the pipe answered is cached; an address it did not
        # reach (the pipe died) is asked again next chunk.
        for addr, (func, _fpath, _lineno) in pipe.resolve_batch(addrs).items():
            self._symname_cache[(binary, addr)] = (
                None if not func or func == '??' else func)

    def prime_module_bases(self, samples):
        """Public entry for the per-chunk load-base pass (see
        _prime_module_bases); the three resolution methods take
        `primed=True` afterwards."""
        with self._lock:
            self._prime_module_bases(samples)

    def resolve_unknown_frames(self, samples, count=True, primed=False):
        """Name frames the target's own perf could not, in place.

        A perf built without libelf still reads /proc/kallsyms, so kernel
        frames arrive named while every userspace frame arrives as
        '[unknown]' — measured at 99.6% of samples on one device. The
        address is in the data regardless, and the server already holds the
        unstripped binary and a matching cross toolchain, so the name is
        recoverable here even though the device could never produce it.

        Additive by construction. A frame is renamed only when the address
        lands inside a known symbol *and* addr2line independently agrees;
        anything less certain keeps '[unknown]', because a confidently
        wrong name is worse than an honest blank.

        Frames are rewritten in place. The sample dicts are the same objects
        held by the live ring, so /api/threads, /api/window, /api/source and
        the collapsed/SVG exports see the resolved names too.

        `count=False` leaves the symbolization tally alone: replay and
        export run the same pass over saved samples, and counting those
        made /api/index/status report a live capture's frames twice.

        Returns the number of frames named.
        """
        if not samples:
            return 0
        with self._lock:
            return self._resolve_unknown_frames_locked(samples, count, primed)

    def _resolve_unknown_frames_locked(self, samples, count, primed):
        # Unknown frames cannot vote for a load base (_base_candidate needs
        # a symbol name), so the bases must come from the named frames
        # first. expand_inline_frames primes them too, but it runs after
        # this and is skipped outright under --no-inline.
        if not primed:
            self._prime_module_bases(samples)

        # A hot loop resamples the same few addresses: 5,577 unknown frames
        # in one measured chunk collapsed to 195 distinct (binary, vaddr)
        # pairs, so ask addr2line once per pair, not once per frame.
        wanted = defaultdict(set)
        targets = []
        seen = unknown_in = 0
        for sample in samples:
            for frame in sample['frames']:
                if (frame.get('module') or '').startswith('['):
                    continue        # [kernel.kallsyms], [vdso], ...
                seen += 1
                if frame.get('func') != UNKNOWN_FUNC:
                    continue
                unknown_in += 1
                binary = self._binary_for_frame(frame)
                if not binary or not os.path.isfile(binary):
                    continue                    # nothing local to ask
                vaddr = self._unknown_vaddr(frame, binary)
                if vaddr is None:
                    continue
                targets.append((frame, binary, vaddr))
                if (binary, vaddr) not in self._symname_cache:
                    wanted[binary].add(vaddr)

        for binary, addrs in wanted.items():
            self._resolve_symnames_batch(binary, sorted(addrs))

        named = 0
        for frame, binary, vaddr in targets:
            func = self._symname_cache.get((binary, vaddr))
            if func:
                frame['func'] = sys.intern(func)
                named += 1
        if count:
            self._sym_seen += seen
            self._sym_unknown_in += unknown_in
            self._sym_named += named
        return named

    def expand_inline_frames(self, samples, primed=False):
        """Expand inline frames in sample data using addr2line -i.

        Returns a new sample list where each frame may be expanded into
        multiple frames. Inlined frames have 'inlined': True.
        Original samples are not modified.
        """
        if not self.inline:
            return samples
        with self._lock:
            return self._expand_inline_frames_locked(samples, primed)

    def _expand_inline_frames_locked(self, samples, primed):
        # Inline expansion can run before any line mapping, so it needs the
        # load bases primed too — otherwise every frame in a function shares
        # one address and collapses to a single inline chain.
        if not primed:
            self._prime_module_bases(samples)

        # Step 1: Collect unique (binary, vaddr) pairs not yet cached.
        # First touch of a binary bulk-loads its persisted inline chains.
        to_resolve = defaultdict(list)
        for sample in samples:
            for frame in sample['frames']:
                binary = self._binary_for_frame(frame)
                if not binary:
                    continue
                if binary not in self._inline_loaded:
                    self._inline_loaded.add(binary)
                    persisted = self._sym_cache.load_inline(self._bkey(binary))
                    for vaddr, chain in persisted.items():
                        self._inline_cache.setdefault((binary, vaddr), chain)
                vaddr = self._compute_vaddr(frame, binary)
                if vaddr is not None and (binary, vaddr) not in self._inline_cache:
                    to_resolve[binary].append(vaddr)

        # Step 2: Resolve via inline pipes; persist what we learned
        for binary, addrs in to_resolve.items():
            unique_addrs = list(set(addrs))
            new_entries = {}
            pipe = self._get_inline_pipe(binary)
            if not pipe:
                for addr in unique_addrs:
                    self._inline_cache[(binary, addr)] = None
                    new_entries[addr] = None
            else:
                # Unanswered addresses (pipe died mid-batch) stay uncached
                # and are asked again next chunk, rather than remembered
                # — and persisted — as "no inline chain".
                for addr, chain in pipe.resolve_inline(unique_addrs).items():
                    if chain and len(chain) > 1:
                        self._inline_cache[(binary, addr)] = chain
                        new_entries[addr] = chain
                    else:
                        self._inline_cache[(binary, addr)] = None
                        new_entries[addr] = None
            self._sym_cache.store_inline(self._bkey(binary), new_entries)

        # Step 3: Expand frames in each sample
        expanded_samples = []
        for sample in samples:
            new_frames = []
            for frame in sample['frames']:
                binary = self._binary_for_frame(frame)
                vaddr = self._compute_vaddr(frame, binary) if binary else None
                chain = self._inline_cache.get((binary, vaddr)) if vaddr else None

                if chain:
                    # chain[0] = innermost (most inlined)
                    # chain[-1] = actual non-inlined function
                    for j, (func, _fpath, _lineno) in enumerate(chain):
                        new_frame = {
                            'addr': frame['addr'],
                            'func': func,
                            'offset': frame['offset'] if j == len(chain) - 1 else '',
                            'module': frame['module'],
                        }
                        if j < len(chain) - 1:
                            new_frame['inlined'] = True
                        new_frames.append(new_frame)
                else:
                    new_frames.append(frame)

            expanded_sample = dict(sample)
            expanded_sample['frames'] = new_frames
            expanded_samples.append(expanded_sample)

        return expanded_samples

    def close(self):
        """Clean up addr2line processes and the persistent cache
        connection (when this mapper opened it). Called when a mapper is
        replaced by PATCH /api/config and at server shutdown; it used to
        have no production caller, so every reconfiguration leaked a set
        of addr2line children and a sqlite handle."""
        with self._lock:
            self._closed = True
            for pipe in self._pipes.values():
                pipe.close()
            self._pipes.clear()
            for pipe in self._inline_pipes.values():
                pipe.close()
            self._inline_pipes.clear()
            if self._owns_cache:
                self._sym_cache.close()

    # ------------------------------------------------------------------
    # Pre-indexing: eagerly load symbols and DWARF source file paths
    # ------------------------------------------------------------------

    def pre_index(self):
        """Eagerly load symbol table and extract DWARF source files.

        Called in a background thread when the user configures a binary.
        Populates caches so the first profiling chunk is instant.
        """
        self._indexing = True
        self._index_symbols_loaded = 0
        self._index_source_files_found = 0
        self._dwarf_source_files = []

        try:
            if self.binary_path:
                # 1. Load symbol table (populates _symbol_cache)
                symbols = self._load_symbols(self.binary_path)
                self._index_symbols_loaded = len(symbols)
                print(f"[source_mapper] Pre-indexed {len(symbols)} symbols",
                      file=sys.stderr)

                # 2. Extract DWARF compilation unit source files
                dwarf_files = self._extract_dwarf_source_files(self.binary_path)
                self._dwarf_source_files = dwarf_files
                self._index_source_files_found = len(dwarf_files)
                print(f"[source_mapper] DWARF: {len(dwarf_files)} source files",
                      file=sys.stderr)

            # 3. Build source directory index
            self._build_source_index()
            if self._source_index:
                total = sum(len(v) for v in self._source_index.values())
                print(f"[source_mapper] Source index: {total} files "
                      f"in {self.source_dir}", file=sys.stderr)
        finally:
            self._indexing = False

    def _extract_dwarf_source_files(self, binary):
        """Extract source file paths from DWARF debug info.

        Cached persistently by binary identity. Prefers
        'llvm-dwarfdump --show-sources' (emits exactly the file list —
        dramatically faster on GB-scale debug info), falling back to
        'readelf --debug-dump=decodedline'.
        """
        if not binary or not os.path.isfile(binary):
            return []

        bkey = self._bkey(binary)
        cached = self._sym_cache.load_dwarf_files(bkey)
        if cached is not None:
            print(f"[source_mapper] DWARF file list from cache "
                  f"({len(cached)} files)", file=sys.stderr)
            return cached

        result = self._dwarf_files_llvm(binary)
        if result is None:
            result = self._dwarf_files_readelf(binary)
        self._sym_cache.store_dwarf_files(bkey, result)
        return result

    def _dwarf_files_llvm(self, binary):
        """File list via llvm-dwarfdump --show-sources, or None when the
        tool is unavailable/fails."""
        if not self.dwarfdump_bin:
            return None
        try:
            r = subprocess.run(
                [self.dwarfdump_bin, '--show-sources', binary],
                capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                return None
            files = {line.strip() for line in r.stdout.splitlines()
                     if line.strip() and '/' in line}
            return sorted(files)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None

    def _dwarf_files_readelf(self, binary):
        files = set()
        proc = None
        try:
            proc = subprocess.Popen(
                [self.readelf_bin, '--debug-dump=decodedline', '-W', binary],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            # The decoded line table has lines like:
            #   /full/path/to/file.c                          42       0x401234  ...
            # or CU header lines like:
            #   CU: /full/path/to/file.c:
            for line in _stream_lines(proc):
                line = line.strip()
                if not line or line.startswith('Decoded'):
                    continue
                # CU header: "CU: path/to/file.c:"
                if line.startswith('CU:'):
                    cu_path = line[3:].strip().rstrip(':')
                    if cu_path and cu_path != '.' and '/' in cu_path:
                        files.add(cu_path)
                    continue
                # Decoded line entry: path  line  addr  [flags]
                # The path has no spaces (or is the first space-delimited token)
                parts = line.split()
                if len(parts) >= 3 and '/' in parts[0]:
                    # Validate: second field should be a line number
                    try:
                        int(parts[1])
                        files.add(parts[0])
                    except ValueError:
                        pass
            proc.wait(timeout=300)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired,
                ToolTimeout) as e:
            print(f"[source_mapper] readelf --debug-dump on "
                  f"{os.path.basename(binary)}: {e}", file=sys.stderr)
        finally:
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait()

        return sorted(files)

    def get_index_status(self):
        """Return current indexing status for the UI.

        The DWARF file list is truncated — on millions-of-LOC binaries the
        full list is multi-MB; use list_dwarf_files() for pagination.
        """
        index = self._source_index
        return {
            'indexing': self._indexing or self._index_building,
            'symbols_loaded': self._index_symbols_loaded,
            'source_files_found': self._index_source_files_found,
            'source_index_ready': index is not None,
            'source_index_files': (sum(len(v) for v in index.values())
                                   if index is not None else 0),
            'dwarf_total': len(self._dwarf_source_files),
            'dwarf_source_files': self._dwarf_source_files[:200],
            'dwarf_truncated': len(self._dwarf_source_files) > 200,
        }

    def list_dwarf_files(self, offset=0, limit=200, query=''):
        """Paginated (optionally filtered) DWARF source-file list."""
        files = self._dwarf_source_files
        if query:
            q = query.lower()
            files = [f for f in files if q in f.lower()]
        total = len(files)
        offset = max(0, offset)
        limit = max(1, min(limit, 1000))
        return {
            'total': total,
            'offset': offset,
            'limit': limit,
            'files': files[offset:offset + limit],
        }


def build_annotated_source(mapper, samples):
    """Build source annotation from samples.

    Returns dict of {file_path: [annotated lines]}
    """
    line_data = mapper.map_samples_to_lines(samples)
    annotated = {}
    for file_path, line_samples in line_data.items():
        lines = mapper.annotate_source(file_path, line_samples)
        if lines:
            annotated[file_path] = lines
    return annotated


if __name__ == '__main__':
    import argparse as ap

    p = ap.ArgumentParser(description='Test source mapper')
    p.add_argument('--binary', default=None, help='Path to binary with debug info')
    p.add_argument('--map', default=None, help='Path to linker map file')
    p.add_argument('--source-dir', default='.', help='Source code directory')
    p.add_argument('--addr2line', default=None, help='Path to addr2line binary')
    p.add_argument('--path-map', default=None, help='Path prefix mapping (from=to)')
    args = p.parse_args()

    path_map = {}
    if args.path_map and '=' in args.path_map:
        src, dst = args.path_map.split('=', 1)
        path_map[src] = dst

    from parser import parse_perf_script

    text = sys.stdin.read()
    samples = parse_perf_script(text)
    print(f"Parsed {len(samples)} samples")

    mapper = SourceMapper(
        args.source_dir,
        binary_path=args.binary,
        map_file_path=args.map,
        addr2line_bin=args.addr2line or 'addr2line',
        path_map=path_map,
    )
    annotated = build_annotated_source(mapper, samples)

    for file_path, lines in annotated.items():
        print(f"\n=== {file_path} ===")
        for ln in lines:
            if ln['samples'] > 0:
                marker = f"[{ln['samples']:4d} | {ln['percent']:5.1f}%]"
            else:
                marker = "              "
            print(f"{ln['line']:4d} {marker}  {ln['source']}")

    mapper.close()
