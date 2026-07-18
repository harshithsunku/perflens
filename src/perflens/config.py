"""Server configuration: dataclass, CLI parsing, and startup tool probing."""

import dataclasses
import os
import sys

try:
    import zstandard as _zstd
except ImportError:
    _zstd = None


@dataclasses.dataclass
class ServerConfig:
    source_dir: str = '.'
    binary_path: str = None
    map_file_path: str = None
    addr2line_bin: str = None
    readelf_bin: str = None
    dwarfdump_bin: str = None
    zstd_bin: str = None
    perf_bin: str = None
    path_map: dict = None
    sysroot: str = None
    sessions_dir: str = ''
    max_samples: int = 500000
    tcp_port: int = 9999
    http_port: int = 8080
    http_bind: str = '127.0.0.1'
    browse_root: str = ''
    token: str = None
    ui_dir: str = ''
    inline: bool = True
    import_file: str = None


def _perflens_bin_dir():
    from perflens.provision import bin_dir
    return bin_dir()


def _find_binary(name, download=False):
    """Find a binary: PATH, then ~/.perflens/bin, then (for addr2line/
    readelf, when download=True) the static tools bundle from the release."""
    from perflens.provision import resolve_tool
    path, _origin = resolve_tool(name, download=download)
    return path


def probe_tools(cfg):
    """Probe available tools at startup and log capability status."""
    print("[server] === Startup Capability Check ===", file=sys.stderr)

    # Source directory
    if os.path.isdir(cfg.source_dir):
        print(f"[server]   source-dir: {cfg.source_dir}", file=sys.stderr)
    else:
        print(f"[server]   source-dir: {cfg.source_dir} (NOT FOUND)", file=sys.stderr)

    # Binary
    if cfg.binary_path and os.path.isfile(cfg.binary_path):
        print(f"[server]   binary: {cfg.binary_path}", file=sys.stderr)
    elif cfg.binary_path:
        print(f"[server]   binary: {cfg.binary_path} (NOT FOUND)", file=sys.stderr)
        cfg.binary_path = None
    else:
        print("[server]   binary: not provided (source mapping limited)",
              file=sys.stderr)

    # Map file
    if cfg.map_file_path and os.path.isfile(cfg.map_file_path):
        print(f"[server]   map file: {cfg.map_file_path}", file=sys.stderr)
    elif cfg.map_file_path:
        print(f"[server]   map file: {cfg.map_file_path} (NOT FOUND)",
              file=sys.stderr)
        cfg.map_file_path = None
    else:
        print("[server]   map file: not provided", file=sys.stderr)

    # addr2line
    if cfg.addr2line_bin and os.path.isfile(cfg.addr2line_bin):
        print(f"[server]   addr2line: {cfg.addr2line_bin} (user-provided)",
              file=sys.stderr)
    else:
        # Prefer llvm-addr2line: GNU-compatible flags, but dramatically
        # faster on GB-scale DWARF (lazy index vs full scan). If neither
        # variant is on PATH or in ~/.perflens/bin, try downloading the
        # static tools bundle from the release (user-space, sha256-checked).
        found = (_find_binary('llvm-addr2line')
                 or _find_binary('addr2line', download=True))
        if found:
            cfg.addr2line_bin = found
            label = ('provisioned' if found.startswith(_perflens_bin_dir())
                     else 'system')
            print(f"[server]   addr2line: {found} ({label})", file=sys.stderr)
        else:
            cfg.addr2line_bin = None
            print("[server]   addr2line: NOT FOUND (source mapping disabled "
                  "— run `perflens provision`, install binutils, or pass "
                  "--addr2line)", file=sys.stderr)

    # llvm-dwarfdump (optional): fast DWARF source-file listing
    found = _find_binary('llvm-dwarfdump')
    if found:
        cfg.dwarfdump_bin = found
        print(f"[server]   llvm-dwarfdump: {found}", file=sys.stderr)

    # readelf
    if cfg.readelf_bin and os.path.isfile(cfg.readelf_bin):
        print(f"[server]   readelf: {cfg.readelf_bin} (user-provided)",
              file=sys.stderr)
    elif cfg.readelf_bin:
        # Toolchain-derived name — resolve it on PATH
        import shutil
        found = shutil.which(cfg.readelf_bin)
        if found:
            cfg.readelf_bin = found
            print(f"[server]   readelf: {cfg.readelf_bin} (toolchain)",
                  file=sys.stderr)
        else:
            print(f"[server]   readelf: {cfg.readelf_bin} (NOT FOUND, "
                  f"falling back to system)", file=sys.stderr)
            cfg.readelf_bin = None
    if not cfg.readelf_bin:
        found = _find_binary('readelf', download=True)
        if found:
            cfg.readelf_bin = found
            label = ('provisioned' if found.startswith(_perflens_bin_dir())
                     else 'system')
            print(f"[server]   readelf: {found} ({label})", file=sys.stderr)
        else:
            cfg.readelf_bin = 'readelf'  # fallback, may fail at runtime
            print("[server]   readelf: NOT FOUND (using 'readelf' fallback "
                  "— run `perflens provision` or install binutils)",
                  file=sys.stderr)

    # sysroot
    if cfg.sysroot:
        if os.path.isdir(cfg.sysroot):
            print(f"[server]   sysroot: {cfg.sysroot}", file=sys.stderr)
        else:
            print(f"[server]   sysroot: {cfg.sysroot} (NOT FOUND)",
                  file=sys.stderr)
            cfg.sysroot = None

    # zstd (fallback only — decompression is in-process via zstandard)
    found = _find_binary('zstd')
    if found:
        cfg.zstd_bin = found
        print(f"[server]   zstd: {found}", file=sys.stderr)
    else:
        cfg.zstd_bin = None
        if _zstd is None:
            print("[server]   zstd: NOT FOUND and zstandard module missing "
                  "(compressed payloads will fail)", file=sys.stderr)

    # Path map
    if cfg.path_map:
        for k, v in cfg.path_map.items():
            print(f"[server]   path-map: {k} → {v}", file=sys.stderr)

    # perf
    found = _find_binary('perf')
    if found:
        cfg.perf_bin = found
        print(f"[server]   perf: {found}", file=sys.stderr)
    else:
        cfg.perf_bin = None
        print("[server]   perf: NOT FOUND (perf.data import disabled)",
              file=sys.stderr)

    # Inline resolution
    if cfg.inline:
        print("[server]   inline: enabled (will probe at mapper init)",
              file=sys.stderr)
    else:
        print("[server]   inline: disabled (--no-inline)", file=sys.stderr)

    print("[server] ================================", file=sys.stderr)


def create_source_mapper(cfg):
    """Create a SourceMapper from a config. Used at startup and runtime
    reconfiguration. Loads any persisted source index instantly and kicks
    a background refresh — request paths never walk the source tree."""
    from perflens.source_mapper import SourceMapper
    mapper = SourceMapper(
        cfg.source_dir,
        binary_path=cfg.binary_path,
        map_file_path=cfg.map_file_path,
        addr2line_bin=cfg.addr2line_bin,
        readelf_bin=cfg.readelf_bin,
        path_map=cfg.path_map or {},
        inline=cfg.inline,
        sysroot=cfg.sysroot,
        dwarfdump_bin=cfg.dwarfdump_bin,
    )
    mapper.start_background_index()
    return mapper


def config_from_args(argv=None):
    """Parse server CLI flags into a ServerConfig."""
    import argparse
    parser = argparse.ArgumentParser(description='PerfLens Server')
    parser.add_argument('--port', type=int, default=9999,
                        help='TCP port for agent connections (default: 9999)')
    parser.add_argument('--http-port', type=int, default=8080,
                        help='HTTP port for web UI (default: 8080)')
    parser.add_argument('--source-dir', type=str, default='.',
                        help='Path to source code directory')
    parser.add_argument('--binary', type=str, default=None,
                        help='Path to unstripped binary or .debug file')
    parser.add_argument('--map', type=str, default=None,
                        help='Path to linker map file')
    parser.add_argument('--path-map', type=str, default=None,
                        help='Compile-time path prefix mapping '
                             '(e.g., /build/src=/home/user/src)')
    parser.add_argument('--addr2line', type=str, default=None,
                        help='Path to custom addr2line binary')
    parser.add_argument('--readelf', type=str, default=None,
                        help='Path to custom readelf binary')
    parser.add_argument('--toolchain-prefix', type=str, default=None,
                        help='Cross-toolchain prefix '
                             '(e.g., arm-linux-gnueabihf- or '
                             '/opt/toolchain/bin/aarch64-linux-gnu-). '
                             'Derives addr2line and readelf from prefix.')
    parser.add_argument('--sysroot', type=str, default=None,
                        help='Target sysroot directory for resolving '
                             'shared libraries and source files '
                             '(like perf --symfs)')
    parser.add_argument('--max-samples', type=int, default=500000,
                        help='Max accumulated samples before oldest are dropped '
                             '(default: 500000)')
    parser.add_argument('--inline', action='store_true', default=True,
                        dest='inline',
                        help='Enable inline function resolution via '
                             'addr2line -i (default)')
    parser.add_argument('--no-inline', action='store_false', dest='inline',
                        help='Disable inline function resolution')
    parser.add_argument('--import', type=str, default=None, dest='import_file',
                        metavar='FILE',
                        help='Import a perf.data file at startup and make it '
                             'available as a session')
    parser.add_argument('--http-bind', type=str, default='127.0.0.1',
                        metavar='ADDR',
                        help='Bind address for the web UI (default: '
                             '127.0.0.1; use 0.0.0.0 to expose it — the UI '
                             'has no authentication)')
    parser.add_argument('--browse-root', type=str, default=None,
                        metavar='DIR',
                        help='Directory the /api/browse file picker is '
                             'confined to (default: your home directory)')
    parser.add_argument('--token', type=str,
                        default=os.environ.get('PERFLENS_TOKEN'),
                        help='Shared secret agents must present in their '
                             'hello (agents pass --token / PERFLENS_TOKEN); '
                             'connections without it are rejected')
    parser.add_argument('--sessions-dir', type=str, default=None,
                        metavar='DIR',
                        help='Where to save profiling sessions '
                             '(default: ~/.perflens/sessions)')
    args = parser.parse_args(argv)

    # Parse path-map
    path_map = {}
    if args.path_map:
        for mapping in args.path_map.split(','):
            if '=' in mapping:
                src, dst = mapping.split('=', 1)
                path_map[src] = dst

    # Toolchain prefix: derive addr2line and readelf from prefix
    if args.toolchain_prefix:
        prefix = args.toolchain_prefix
        if not args.addr2line:
            args.addr2line = prefix + 'addr2line'
        if not args.readelf:
            args.readelf = prefix + 'readelf'
        print(f"[server] Toolchain prefix: {prefix}", file=sys.stderr)
    elif args.addr2line and not args.readelf:
        # Infer readelf from addr2line path (same directory, same prefix)
        a2l = args.addr2line
        if 'addr2line' in os.path.basename(a2l):
            inferred = a2l.replace('addr2line', 'readelf')
            if os.path.isfile(inferred):
                args.readelf = inferred

    # UI ships inside the package (src/perflens/ui) — resolve it via
    # importlib.resources so both `pip install` and repo checkouts work.
    from importlib.resources import files as _pkg_files
    ui_dir = os.fspath(_pkg_files('perflens') / 'ui')

    # Sessions live under ~/.perflens (override root with PERFLENS_HOME,
    # or the directory itself with --sessions-dir).
    if args.sessions_dir:
        sessions_dir = os.path.abspath(args.sessions_dir)
    else:
        from perflens.symcache import perflens_home
        sessions_dir = os.path.join(perflens_home(), 'sessions')

    return ServerConfig(
        source_dir=os.path.abspath(args.source_dir),
        binary_path=os.path.abspath(args.binary) if args.binary else None,
        map_file_path=os.path.abspath(args.map) if args.map else None,
        addr2line_bin=args.addr2line,
        readelf_bin=args.readelf,
        path_map=path_map or None,
        sysroot=os.path.abspath(args.sysroot) if args.sysroot else None,
        sessions_dir=sessions_dir,
        max_samples=args.max_samples,
        tcp_port=args.port,
        http_port=args.http_port,
        http_bind=args.http_bind,
        browse_root=os.path.abspath(args.browse_root)
                    if args.browse_root else os.path.expanduser('~'),
        token=args.token,
        ui_dir=ui_dir,
        inline=args.inline,
        import_file=os.path.abspath(args.import_file)
                    if args.import_file else None,
    )
