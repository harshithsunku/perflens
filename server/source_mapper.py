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
from collections import defaultdict


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
                 addr2line_bin=None, path_map=None, inline=False):
        self.source_dir = os.path.abspath(source_dir)
        self.binary_path = binary_path
        self.addr2line_bin = addr2line_bin
        self.path_map = path_map or {}
        self.inline = inline

        # Map file symbols
        self._map_symbols = {}
        if map_file_path:
            parser = MapFileParser(map_file_path)
            self._map_symbols = parser.symbols

        # Cache: binary -> {func_name: vaddr}
        self._symbol_cache = {}
        # Cache: (binary, addr) -> (file, line)
        self._addr2line_cache = {}
        # Persistent addr2line pipes per binary
        self._pipes = {}
        # Inline addr2line pipes per binary (use -i flag)
        self._inline_pipes = {}
        # Inline resolution cache: (binary, addr) -> [(func, file, line), ...] or None
        self._inline_cache = {}
        # Source file index: basename -> [full_paths]
        self._source_index = None
        # Full path cache: reported_path -> actual_path
        self._path_cache = {}

        # Pre-indexing state (populated by pre_index())
        self._indexing = False
        self._dwarf_source_files = []  # list of source file paths from DWARF
        self._index_symbols_loaded = 0
        self._index_source_files_found = 0

        # Probe inline support at startup
        if self.inline:
            if self._probe_inline_support():
                print("[source_mapper] Inline resolution enabled (-i supported)",
                      file=sys.stderr)
            else:
                self.inline = False
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

    def _load_symbols(self, binary):
        """Load symbol table. Priority: map file → readelf.

        Uses streaming parse for readelf output so that very large
        binaries (100-200 MB+) don't require the entire symbol table
        text to be held in memory at once.
        """
        if binary in self._symbol_cache:
            return self._symbol_cache[binary]

        symbols = {}

        # Try readelf first (per-binary, accurate).  Stream output
        # line-by-line so we never hold the full symbol table in RAM.
        if binary and os.path.isfile(binary):
            proc = None
            try:
                proc = subprocess.Popen(
                    ['readelf', '-s', '-W', binary],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                for line in proc.stdout:
                    parts = line.split()
                    if len(parts) >= 8 and parts[3] == 'FUNC':
                        addr = int(parts[1], 16)
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

        # Supplement with map file symbols
        for name, addr in self._map_symbols.items():
            if name not in symbols:
                symbols[name] = addr

        self._symbol_cache[binary] = symbols
        return symbols

    def _compute_vaddr(self, frame, binary):
        """Compute virtual address from function+offset."""
        func = frame['func']
        offset_str = frame.get('offset', '')

        symbols = self._load_symbols(binary)
        if func not in symbols:
            return None

        func_addr = symbols[func]
        if offset_str.startswith('0x'):
            offset = int(offset_str, 16)
        elif offset_str:
            try:
                offset = int(offset_str)
            except ValueError:
                return None
        else:
            offset = 0

        return func_addr + offset

    def _resolve_addrs_batch(self, binary, addrs):
        """Resolve multiple addresses at once using the pipe."""
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
        for addr in uncached:
            if addr in batch_results:
                func, fpath, lineno = batch_results[addr]
                if fpath != '??' and lineno > 0:
                    self._addr2line_cache[(binary, addr)] = (fpath, lineno)
                else:
                    self._addr2line_cache[(binary, addr)] = ('??', 0)
            else:
                self._addr2line_cache[(binary, addr)] = ('??', 0)

    def map_samples_to_lines(self, samples):
        """Map all samples to source lines using batch resolution.

        Returns: {file_path: {line_no: {'samples': int}}}
        """
        # Step 1: Collect all unique addresses per binary
        addrs_per_binary = defaultdict(set)
        frame_addrs = []  # (sample_idx, binary, vaddr)

        for i, sample in enumerate(samples):
            if not sample['frames']:
                continue
            frame = sample['frames'][0]
            binary = self.binary_path or frame.get('module', '')
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
        for i, binary, vaddr in frame_addrs:
            file_path, line_no = self._addr2line_cache.get(
                (binary, vaddr), ('??', 0))
            if file_path != '??' and line_no > 0:
                line_data[file_path][line_no]['samples'] += 1

        return dict(line_data)

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

    def _build_source_index(self):
        """Build an index of source files: basename -> [full_paths]."""
        if self._source_index is not None:
            return
        self._source_index = defaultdict(list)
        for root, dirs, files in os.walk(self.source_dir):
            dirs[:] = [d for d in dirs if not d.startswith('.')
                       and d not in self._SKIP_DIRS]
            for fname in files:
                full = os.path.join(root, fname)
                self._source_index[fname].append(full)

    def _find_source_file(self, file_path):
        """Find a source file: path map → exact path → basename match."""
        if file_path in self._path_cache:
            return self._path_cache[file_path]

        # Apply path mapping first
        mapped = self._apply_path_map(file_path)

        result = None

        # Try exact path
        if os.path.isfile(mapped):
            result = mapped
        else:
            # Try basename matching
            self._build_source_index()
            basename = os.path.basename(mapped)
            candidates = self._source_index.get(basename, [])

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
                    for a, b in zip(reversed(parts), reversed(cand_parts)):
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
            binary = self.binary_path or frame.get('module', '')
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

        # Step 1: Collect unique (binary, vaddr) pairs not yet cached
        to_resolve = defaultdict(list)
        for sample in samples:
            for frame in sample['frames']:
                binary = self.binary_path or frame.get('module', '')
                if not binary:
                    continue
                vaddr = self._compute_vaddr(frame, binary)
                if vaddr is not None and (binary, vaddr) not in self._inline_cache:
                    to_resolve[binary].append(vaddr)

        # Step 2: Resolve via inline pipes
        for binary, addrs in to_resolve.items():
            unique_addrs = list(set(addrs))
            pipe = self._get_inline_pipe(binary)
            if not pipe:
                for addr in unique_addrs:
                    self._inline_cache[(binary, addr)] = None
                continue
            results = pipe.resolve_inline(unique_addrs)
            for addr in unique_addrs:
                chain = results.get(addr)
                if chain and len(chain) > 1:
                    self._inline_cache[(binary, addr)] = chain
                else:
                    self._inline_cache[(binary, addr)] = None

        # Step 3: Expand frames in each sample
        expanded_samples = []
        for sample in samples:
            new_frames = []
            for frame in sample['frames']:
                binary = self.binary_path or frame.get('module', '')
                vaddr = self._compute_vaddr(frame, binary) if binary else None
                chain = self._inline_cache.get((binary, vaddr)) if vaddr else None

                if chain:
                    # chain[0] = innermost (most inlined)
                    # chain[-1] = actual non-inlined function
                    for j, (func, fpath, lineno) in enumerate(chain):
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

        Uses 'readelf --debug-dump=decodedline' to get compilation unit
        file tables.  Returns a sorted list of unique absolute paths
        that appear in the debug info.
        """
        if not binary or not os.path.isfile(binary):
            return []

        files = set()
        proc = None
        try:
            proc = subprocess.Popen(
                ['readelf', '--debug-dump=decodedline', '-W', binary],
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
        """Return current indexing status for the UI."""
        return {
            'indexing': self._indexing,
            'symbols_loaded': self._index_symbols_loaded,
            'source_files_found': self._index_source_files_found,
            'dwarf_source_files': self._dwarf_source_files,
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
        for l in lines:
            if l['samples'] > 0:
                marker = f"[{l['samples']:4d} | {l['percent']:5.1f}%]"
            else:
                marker = "              "
            print(f"{l['line']:4d} {marker}  {l['source']}")

    mapper.close()
