#!/usr/bin/env python3
"""Maps perf data to source code lines using debug symbols."""

import os
import re
import subprocess
from collections import defaultdict


class SourceMapper:
    """Maps function+offset from perf data to source file and line."""

    def __init__(self, source_dir, binary_path=None):
        self.source_dir = os.path.abspath(source_dir)
        self.binary_path = binary_path
        # Cache: binary_path -> {func_name: vaddr}
        self._symbol_cache = {}
        # Cache: (binary, addr) -> (file, line)
        self._addr2line_cache = {}

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
                capture_output=True, text=True, timeout=10
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

    def _addr2line(self, binary, addr):
        """Resolve an address to file:line using addr2line."""
        key = (binary, addr)
        if key in self._addr2line_cache:
            return self._addr2line_cache[key]

        result = ('??', 0)
        if not os.path.isfile(binary):
            self._addr2line_cache[key] = result
            return result

        try:
            proc = subprocess.run(
                ['addr2line', '-e', binary, '-f', hex(addr)],
                capture_output=True, text=True, timeout=5
            )
            lines = proc.stdout.strip().split('\n')
            if len(lines) >= 2:
                location = lines[1]
                # Remove discriminator info
                location = re.sub(r'\s*\(discriminator \d+\)', '', location)
                if ':' in location and location != '??:0':
                    file_path, line_no = location.rsplit(':', 1)
                    try:
                        result = (file_path, int(line_no))
                    except ValueError:
                        pass
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        self._addr2line_cache[key] = result
        return result

    def resolve_frame(self, frame):
        """Resolve a single stack frame to source file and line.

        Args:
            frame: dict with 'func', 'offset', 'module' keys

        Returns:
            (source_file, line_number) or ('??', 0) if unresolved
        """
        binary = self.binary_path or frame.get('module', '')
        if not os.path.isfile(binary):
            return ('??', 0)

        func = frame['func']
        offset_str = frame.get('offset', '')

        symbols = self._load_symbols(binary)
        if func not in symbols:
            return ('??', 0)

        func_addr = symbols[func]
        if offset_str.startswith('0x'):
            offset = int(offset_str, 16)
        elif offset_str:
            try:
                offset = int(offset_str)
            except ValueError:
                offset = 0
        else:
            offset = 0

        vaddr = func_addr + offset
        return self._addr2line(binary, vaddr)

    def map_samples_to_lines(self, samples):
        """Map all samples to source lines.

        Returns:
        {
            file_path: {
                line_no: {'samples': int, 'source': str}
            }
        }
        """
        line_data = defaultdict(lambda: defaultdict(lambda: {'samples': 0, 'source': ''}))

        for sample in samples:
            if not sample['frames']:
                continue
            # Resolve the leaf frame (where CPU time is spent)
            frame = sample['frames'][0]
            file_path, line_no = self.resolve_frame(frame)
            if file_path != '??' and line_no > 0:
                line_data[file_path][line_no]['samples'] += 1

        return dict(line_data)

    def annotate_source(self, file_path, line_samples):
        """Read a source file and annotate it with sample data.

        Args:
            file_path: path to source file (as reported by addr2line)
            line_samples: {line_no: {'samples': int}}

        Returns:
            list of {'line': int, 'source': str, 'samples': int, 'percent': float}
        """
        # Try to find the file relative to source_dir
        actual_path = self._find_source_file(file_path)
        if actual_path is None:
            return []

        total_samples = sum(d['samples'] for d in line_samples.values())

        result = []
        try:
            with open(actual_path, 'r') as f:
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

    def _find_source_file(self, file_path):
        """Find a source file, trying various paths."""
        # Try exact path
        if os.path.isfile(file_path):
            return file_path

        # Try relative to source_dir
        basename = os.path.basename(file_path)
        for root, dirs, files in os.walk(self.source_dir):
            if basename in files:
                return os.path.join(root, basename)

        return None


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
