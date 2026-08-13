#!/usr/bin/env python3
"""Maps perf data to source code lines using debug symbols.

Supports:
- Batch addr2line via persistent pipe (-f flag, 2-line output format)
- Map file symbol resolution as fallback
- Path prefix mapping for cross-compiled binaries
- Per-module (shared library) resolution
- Caching across chunks
"""

import os
import re
import subprocess
import sys
import threading
from collections import Counter, defaultdict

from perflens import symcache

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

    This makes batch processing predictable — N addresses in → 2N lines out.
    """

    def __init__(self, binary, addr2line_bin='addr2line', inline=False):
        self.binary = binary
        self.addr2line_bin = addr2line_bin
        self.inline = inline
        self._proc = None

    def _ensure_started(self):
        if self._proc is None or self._proc.poll() is not None:
            flags = ['-f', '-i'] if self.inline else ['-f']
            cmd = [self.addr2line_bin, '-e', self.binary] + flags
            try:
                self._proc = subprocess.Popen(
                    ['stdbuf', '-oL'] + cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError:
                # stdbuf not available, try without
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )

    def resolve_batch(self, addrs):
        """Resolve a list of addresses via the persistent pipe.

        Returns {addr: (func, file, line)}.
        Processes in chunks to avoid pipe buffer deadlock.
        """
        if not addrs:
            return {}
        self._ensure_started()

        results = {}
        CHUNK = 500  # safe for 64KB pipe buffer (~100 bytes output per addr)

        try:
            for i in range(0, len(addrs), CHUNK):
                chunk = addrs[i:i + CHUNK]

                for addr in chunk:
                    self._proc.stdin.write(hex(addr) + '\n')
                self._proc.stdin.flush()

                for addr in chunk:
                    func_line = self._proc.stdout.readline().strip()
                    file_line = self._proc.stdout.readline().strip()

                    if not func_line or not file_line:
                        results[addr] = ('??', '??', 0)
                        continue

                    # Strip discriminator
                    file_line = re.sub(r'\s*\(discriminator \d+\)', '', file_line)

                    if func_line == '??' or file_line.startswith('??'):
                        results[addr] = ('??', '??', 0)
                        continue

                    # Parse file:line  (use rfind to handle Windows paths with colons)
                    idx = file_line.rfind(':')
                    if idx > 0:
                        fpath = file_line[:idx]
                        try:
                            lineno = int(file_line[idx + 1:])
                            results[addr] = (func_line, fpath, lineno)
                        except ValueError:
                            results[addr] = ('??', '??', 0)
                    else:
                        results[addr] = ('??', '??', 0)
        except (BrokenPipeError, OSError):
            if self._proc:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
            self._proc = None

        return results

    def resolve_inline(self, addrs):
        """Resolve addresses with inline expansion via sentinel protocol.

        Returns {addr: [(func, file, line), ...]} where index 0 is innermost.
        Processes one address at a time with a 0x0 sentinel to delimit output.
        """
        if not addrs:
            return {}
        self._ensure_started()

        results = {}
        try:
            for addr in addrs:
                self._proc.stdin.write(hex(addr) + '\n')
                self._proc.stdin.write('0x0\n')
                self._proc.stdin.flush()

                chain = []
                while True:
                    func_line = self._proc.stdout.readline().strip()
                    file_line = self._proc.stdout.readline().strip()

                    if not func_line or not file_line:
                        break

                    file_line = re.sub(r'\s*\(discriminator \d+\)', '', file_line)

                    # Sentinel detection: 0x0 produces ?? / ??:0
                    if func_line == '??' and file_line.startswith('??'):
                        if not chain:
                            # ?? was from the real address; sentinel still pending
                            self._proc.stdout.readline()
                            self._proc.stdout.readline()
                        break

                    idx = file_line.rfind(':')
                    if idx > 0:
                        fpath = file_line[:idx]
                        try:
                            lineno = int(file_line[idx + 1:])
                            chain.append((func_line, fpath, lineno))
                        except ValueError:
                            chain.append((func_line, '??', 0))
                    else:
                        chain.append((func_line, '??', 0))

                results[addr] = chain if chain else [('??', '??', 0)]
        except (BrokenPipeError, OSError):
            if self._proc:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
            self._proc = None

        return results

    def close(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
            self._proc = None


class SourceMapper:
    """Maps function+offset from perf data to source file and line.

    Created once at server startup and shared across all requests.
    """

    def __init__(self, source_dir, binary_path=None, map_file_path=None,
                 addr2line_bin=None, readelf_bin=None, path_map=None,
                 inline=False, sysroot=None, dwarfdump_bin=None,
                 sym_cache=None):
        self.source_dir = os.path.abspath(source_dir)
        self.binary_path = binary_path
        self.addr2line_bin = addr2line_bin
        self.readelf_bin = readelf_bin or 'readelf'
        self.dwarfdump_bin = dwarfdump_bin  # llvm-dwarfdump, when available
        self.path_map = path_map or {}
        self.inline = inline
        self.sysroot = sysroot

        # Persistent cross-restart cache (~/.perflens/cache)
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
        """Get or create an addr2line pipe for a binary."""
        if binary not in self._pipes:
            if binary and os.path.isfile(binary) and self.addr2line_bin:
                self._pipes[binary] = Addr2LinePipe(binary, self.addr2line_bin)
            else:
                return None
        return self._pipes[binary]

    def _get_inline_pipe(self, binary):
        """Get or create an inline addr2line pipe for a binary."""
        if binary not in self._inline_pipes:
            if binary and os.path.isfile(binary) and self.addr2line_bin:
                self._inline_pipes[binary] = Addr2LinePipe(
                    binary, self.addr2line_bin, inline=True)
            else:
                return None
        return self._inline_pipes[binary]

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

        # Try readelf first (per-binary, accurate).  Stream output
        # line-by-line so we never hold the full symbol table in RAM.
        if binary and os.path.isfile(binary):
            proc = None
            try:
                proc = subprocess.Popen(
                    [self.readelf_bin, '-s', '-W', binary],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                for line in proc.stdout:
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
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                if proc:
                    proc.kill()
                    proc.wait()
            finally:
                if proc and proc.poll() is None:
                    proc.kill()
                    proc.wait()

        # Persist before merging map-file symbols (which belong to the map
        # file, not to this binary's identity)
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
        addrs = sorted(set(self._load_symbols(binary).values()))
        spans = {}
        for i, addr in enumerate(addrs):
            # The last symbol has no successor to bound it; be generous, since
            # this bound only gates _base_candidate (which ignores spans wider
            # than a page anyway) and the sanity check in _compute_vaddr.
            nxt = addrs[i + 1] if i + 1 < len(addrs) else addr + LAST_SYMBOL_SPAN
            spans[addr] = nxt
        self._sym_spans[binary] = spans
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
        for addr in uncached:
            if addr in batch_results:
                func, fpath, lineno = batch_results[addr]
                if fpath != '??' and lineno > 0:
                    self._addr2line_cache[(binary, addr)] = (fpath, lineno)
                    new_entries[addr] = (fpath, lineno)
                else:
                    self._addr2line_cache[(binary, addr)] = ('??', 0)
                    new_entries[addr] = ('??', 0)
            else:
                self._addr2line_cache[(binary, addr)] = ('??', 0)
        self._sym_cache.store_addr2line(self._bkey(binary), new_entries)

    def map_samples_to_lines(self, samples):
        """Map all samples to source lines using batch resolution.

        Returns: {file_path: {line_no: {'samples': int}}}
        """
        # Step 0: pin down where each module was loaded, so frames without a
        # symoff resolve to a real line instead of the function's first one.
        self._prime_module_bases(samples)

        # Step 1: Collect all unique addresses per binary
        addrs_per_binary = defaultdict(set)
        frame_addrs = []  # (sample_idx, binary, vaddr)

        for i, sample in enumerate(samples):
            if not sample['frames']:
                continue
            frame = sample['frames'][0]
            binary = self.binary_path or self._resolve_module_path(frame.get('module', ''))
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

    def _resolve_module_path(self, module):
        """Resolve a module path from perf output to a local binary.

        If sysroot is set, prepends it to absolute paths (e.g.
        /usr/lib/libc.so -> /opt/sysroot/usr/lib/libc.so).
        """
        if not module:
            return module
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
        line_data = self.map_samples_to_lines(samples)

        # Build function-to-file mapping from cached results
        file_functions = defaultdict(set)
        for sample in samples:
            if not sample['frames']:
                continue
            frame = sample['frames'][0]
            binary = self.binary_path or self._resolve_module_path(frame.get('module', ''))
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

    def expand_inline_frames(self, samples):
        """Expand inline frames in sample data using addr2line -i.

        Returns a new sample list where each frame may be expanded into
        multiple frames. Inlined frames have 'inlined': True.
        Original samples are not modified.
        """
        if not self.inline:
            return samples

        # Inline expansion can run before any line mapping, so it needs the
        # load bases primed too — otherwise every frame in a function shares
        # one address and collapses to a single inline chain.
        self._prime_module_bases(samples)

        # Step 1: Collect unique (binary, vaddr) pairs not yet cached.
        # First touch of a binary bulk-loads its persisted inline chains.
        to_resolve = defaultdict(list)
        for sample in samples:
            for frame in sample['frames']:
                binary = self.binary_path or self._resolve_module_path(frame.get('module', ''))
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
                results = pipe.resolve_inline(unique_addrs)
                for addr in unique_addrs:
                    chain = results.get(addr)
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
                binary = self.binary_path or self._resolve_module_path(frame.get('module', ''))
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
        """Clean up addr2line processes."""
        for pipe in self._pipes.values():
            pipe.close()
        self._pipes.clear()
        for pipe in self._inline_pipes.values():
            pipe.close()
        self._inline_pipes.clear()

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
                text=True,
                bufsize=1,
            )
            # The decoded line table has lines like:
            #   /full/path/to/file.c                          42       0x401234  ...
            # or CU header lines like:
            #   CU: /full/path/to/file.c:
            for line in proc.stdout:
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
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            if proc:
                proc.kill()
                proc.wait()
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
