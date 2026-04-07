#!/usr/bin/env python3
"""Maps perf data to source code lines using debug symbols.

Designed for large binaries compiled with -O2:
- Batch addr2line via persistent pipe (handles 100K+ addresses efficiently)
- Inline expansion (-i flag) to unwind inlined functions
- Full-path source file matching (no basename collisions)
- Per-module (shared library) resolution
- Caching across chunks to avoid redundant work
"""

import os
import re
import subprocess
from collections import defaultdict


class Addr2LinePipe:
    """Persistent addr2line process for batch address resolution.

    Instead of spawning one subprocess per address, keeps a single addr2line
    process running and pipes addresses to it. 100-1000x faster for large binaries.
    """

    def __init__(self, binary):
        self.binary = binary
        self._proc = None

    def _ensure_started(self):
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(
                ['addr2line', '-e', self.binary, '-f', '-i', '-p'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,  # line-buffered
            )

    def resolve(self, addr):
        """Resolve a single address. Returns list of (func, file, line) tuples.

        With -i, inlined functions produce multiple results (innermost last).
        """
        self._ensure_started()
        try:
            self._proc.stdin.write(hex(addr) + '\n')
            self._proc.stdin.flush()
            results = []
            while True:
                line = self._proc.stdout.readline().strip()
                if not line:
                    break
                # Format with -p: "func at file:line" or "func at file:line (inlined by) ..."
                # Or with -i -p: multiple lines, continuations start with " (inlined by)"
                parsed = self._parse_line(line)
                if parsed:
                    results.append(parsed)
                # Check if next line is a continuation (inlined by)
                # addr2line -f -i -p outputs one line per result, no continuation
                # But we only get one response per address query
                break
            return results if results else [('??', '??', 0)]
        except (BrokenPipeError, OSError):
            self._proc = None
            return [('??', '??', 0)]

    def resolve_batch(self, addrs):
        """Resolve a list of addresses efficiently.

        Returns dict: {addr: [(func, file, line), ...]}
        """
        if not addrs:
            return {}

        results = {}
        self._ensure_started()

        try:
            for addr in addrs:
                self._proc.stdin.write(hex(addr) + '\n')
            self._proc.stdin.flush()

            for addr in addrs:
                line = self._proc.stdout.readline().strip()
                parsed = self._parse_line(line)
                results[addr] = [parsed] if parsed else [('??', '??', 0)]
        except (BrokenPipeError, OSError):
            self._proc = None
            # Fill remaining with unknowns
            for addr in addrs:
                if addr not in results:
                    results[addr] = [('??', '??', 0)]

        return results

    def _parse_line(self, line):
        """Parse addr2line -f -i -p output line.

        Formats:
          "cpu_intensive at /path/sample_workload.c:11"
          "cpu_intensive at /path/sample_workload.c:11 (discriminator 1)"
          "?? at ??:0"
          "?? at :?"
        """
        # Remove discriminator
        line = re.sub(r'\s*\(discriminator \d+\)', '', line)

        m = re.match(r'^(.+?)\s+at\s+(.+):(\d+)\s*$', line)
        if m:
            func = m.group(1).strip()
            fpath = m.group(2).strip()
            lineno = int(m.group(3))
            if fpath == '??' or lineno == 0:
                return None
            return (func, fpath, lineno)
        return None

    def close(self):
        if self._proc and self._proc.poll() is None:
            self._proc.stdin.close()
            self._proc.wait(timeout=5)
            self._proc = None


class SourceMapper:
    """Maps function+offset from perf data to source file and line.

    Supports:
    - Large binaries with thousands of functions
    - Multiple modules (main binary + shared libraries)
    - Inlined functions (-O2 builds)
    - Efficient batch resolution
    """

    def __init__(self, source_dir, binary_path=None):
        self.source_dir = os.path.abspath(source_dir)
        self.binary_path = binary_path
        # Cache: binary_path -> {func_name: vaddr}
        self._symbol_cache = {}
        # Cache: (binary, addr) -> (file, line)
        self._addr2line_cache = {}
        # Persistent addr2line pipes per binary
        self._pipes = {}
        # Source file index: basename -> [full_paths]
        self._source_index = None
        # Full path cache: reported_path -> actual_path
        self._path_cache = {}

    def _get_pipe(self, binary):
        """Get or create an addr2line pipe for a binary."""
        if binary not in self._pipes:
            if os.path.isfile(binary):
                self._pipes[binary] = Addr2LinePipe(binary)
            else:
                return None
        return self._pipes[binary]

    def _load_symbols(self, binary):
        """Load symbol table from a binary using readelf."""
        if binary in self._symbol_cache:
            return self._symbol_cache[binary]

        symbols = {}
        if not os.path.isfile(binary):
            self._symbol_cache[binary] = symbols
            return symbols

        try:
            result = subprocess.run(
                ['readelf', '-s', '-W', binary],
                capture_output=True, text=True, timeout=60
            )
            for line in result.stdout.split('\n'):
                parts = line.split()
                if len(parts) >= 8 and parts[3] == 'FUNC':
                    addr = int(parts[1], 16)
                    name = parts[7]
                    if addr > 0:
                        symbols[name] = addr
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        self._symbol_cache[binary] = symbols
        return symbols

    def _resolve_addr(self, binary, addr):
        """Resolve an address using the persistent pipe (with cache)."""
        key = (binary, addr)
        if key in self._addr2line_cache:
            return self._addr2line_cache[key]

        pipe = self._get_pipe(binary)
        if pipe is None:
            result = ('??', 0)
            self._addr2line_cache[key] = result
            return result

        resolved = pipe.resolve(addr)
        if resolved and resolved[0][1] != '??':
            # Use the innermost (last) result for inlined functions
            _, fpath, lineno = resolved[-1]
            result = (fpath, lineno)
        else:
            result = ('??', 0)

        self._addr2line_cache[key] = result
        return result

    def _resolve_addrs_batch(self, binary, addrs):
        """Resolve multiple addresses at once using the pipe."""
        # Filter out already-cached addresses
        uncached = []
        for addr in addrs:
            key = (binary, addr)
            if key not in self._addr2line_cache:
                uncached.append(addr)

        if uncached:
            pipe = self._get_pipe(binary)
            if pipe:
                batch_results = pipe.resolve_batch(uncached)
                for addr, resolved in batch_results.items():
                    if resolved and resolved[0][1] != '??':
                        _, fpath, lineno = resolved[-1]
                        self._addr2line_cache[(binary, addr)] = (fpath, lineno)
                    else:
                        self._addr2line_cache[(binary, addr)] = ('??', 0)

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

    def resolve_frame(self, frame):
        """Resolve a single stack frame to source file and line."""
        binary = self.binary_path or frame.get('module', '')
        if not os.path.isfile(binary):
            return ('??', 0)

        vaddr = self._compute_vaddr(frame, binary)
        if vaddr is None:
            return ('??', 0)

        return self._resolve_addr(binary, vaddr)

    def map_samples_to_lines(self, samples):
        """Map all samples to source lines using batch resolution.

        Returns:
        {
            file_path: {
                line_no: {'samples': int}
            }
        }
        """
        # Step 1: Collect all unique addresses per binary
        addrs_per_binary = defaultdict(set)
        frame_addrs = []  # (sample_idx, binary, vaddr)

        for i, sample in enumerate(samples):
            if not sample['frames']:
                continue
            frame = sample['frames'][0]
            binary = self.binary_path or frame.get('module', '')
            if not os.path.isfile(binary):
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
            file_path, line_no = self._addr2line_cache.get((binary, vaddr), ('??', 0))
            if file_path != '??' and line_no > 0:
                line_data[file_path][line_no]['samples'] += 1

        return dict(line_data)

    def _build_source_index(self):
        """Build an index of source files: basename -> [full_paths]."""
        if self._source_index is not None:
            return
        self._source_index = defaultdict(list)
        for root, dirs, files in os.walk(self.source_dir):
            # Skip hidden dirs and common non-source dirs
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                       ('node_modules', '__pycache__', '.git', 'build', 'cmake-build')]
            for fname in files:
                full = os.path.join(root, fname)
                self._source_index[fname].append(full)

    def _find_source_file(self, file_path):
        """Find a source file using full path matching, falling back to basename."""
        if file_path in self._path_cache:
            return self._path_cache[file_path]

        result = None

        # Try exact path first
        if os.path.isfile(file_path):
            result = file_path
        else:
            # Try joining with source_dir using the relative path
            # addr2line may report /home/user/project/src/foo.c
            # but our source is at /root/perflens/test/foo.c
            # Try matching the tail of the path
            self._build_source_index()
            basename = os.path.basename(file_path)
            candidates = self._source_index.get(basename, [])

            if len(candidates) == 1:
                result = candidates[0]
            elif len(candidates) > 1:
                # Multiple matches — try to match the longest common suffix
                parts = file_path.replace('\\', '/').split('/')
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

        return result

    def get_files_with_samples(self, samples):
        """Return list of source files that have samples, with sample counts.

        This is lightweight — doesn't read source files, just returns file paths.
        """
        line_data = self.map_samples_to_lines(samples)
        file_list = []
        for fpath, lines in line_data.items():
            total = sum(d['samples'] for d in lines.values())
            actual = self._find_source_file(fpath)
            file_list.append({
                'path': fpath,
                'found': actual is not None,
                'total_samples': total,
            })
        file_list.sort(key=lambda x: x['total_samples'], reverse=True)
        return file_list

    def close(self):
        """Clean up addr2line processes."""
        for pipe in self._pipes.values():
            pipe.close()
        self._pipes.clear()


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
    import sys
    import argparse as ap

    p = ap.ArgumentParser(description='Test source mapper')
    p.add_argument('--binary', required=True, help='Path to binary with debug info')
    p.add_argument('--source-dir', default='.', help='Source code directory')
    args = p.parse_args()

    from parser import parse_perf_script

    text = sys.stdin.read()
    samples = parse_perf_script(text)
    print(f"Parsed {len(samples)} samples")

    mapper = SourceMapper(args.source_dir, args.binary)
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
