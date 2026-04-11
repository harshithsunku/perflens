#!/usr/bin/env bash
#
# build_package.sh -- produce self-contained tarballs for the PerfLens server
# and agent. No pre-installed dependencies needed on the target machines
# (beyond Python 3.5+ on the agent side).
#
# Usage:
#   ./build_package.sh              # build both, frozen with PyInstaller
#   ./build_package.sh --server     # server package only
#   ./build_package.sh --agent      # agent package only
#   ./build_package.sh --no-freeze  # skip PyInstaller, package scripts only
#
set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

BUILD_SERVER=1
BUILD_AGENT=1
NO_FREEZE=0

for arg in "$@"; do
    case "$arg" in
        --server)    BUILD_SERVER=1; BUILD_AGENT=0 ;;
        --agent)     BUILD_AGENT=1; BUILD_SERVER=0 ;;
        --no-freeze) NO_FREEZE=1 ;;
        -h|--help)
            sed -n '3,15p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

VERSION="dev"
if [ -f "$REPO_ROOT/VERSION" ]; then
    VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"
fi

BUILD_DIR="$REPO_ROOT/build"
DIST_DIR="$REPO_ROOT/dist"

# Colors (only when writing to a TTY)
if [ -t 1 ]; then
    C_GREEN='\033[0;32m'
    C_YELLOW='\033[0;33m'
    C_RED='\033[0;31m'
    C_BLUE='\033[0;34m'
    C_RESET='\033[0m'
else
    C_GREEN=''; C_YELLOW=''; C_RED=''; C_BLUE=''; C_RESET=''
fi

ok()   { printf "${C_GREEN}[OK]${C_RESET} %s\n" "$*"; }
warn() { printf "${C_YELLOW}[!!]${C_RESET} %s\n" "$*"; }
err()  { printf "${C_RED}[XX]${C_RESET} %s\n" "$*" >&2; }
info() { printf "${C_BLUE}[..]${C_RESET} %s\n" "$*"; }

rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

info "PerfLens build -- version ${VERSION}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

write_server_launcher() {
    local dest="$1"
    cat > "$dest" <<'LAUNCHER_EOF'
#!/usr/bin/env bash
set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="$SELF_DIR/bin:$PATH"
if [ -f "$SELF_DIR/perflens-server-bin" ]; then
    exec "$SELF_DIR/perflens-server-bin" "$@"
fi
if [ -d "$SELF_DIR/lib" ] && [ "$(ls -A "$SELF_DIR/lib" 2>/dev/null)" ]; then
    export PYTHONPATH="$SELF_DIR/lib:${PYTHONPATH:-}"
fi
exec python3 "$SELF_DIR/server/perflens_server.py" "$@"
LAUNCHER_EOF
    chmod +x "$dest"
}

write_agent_launcher() {
    local dest="$1"
    cat > "$dest" <<'LAUNCHER_EOF'
#!/usr/bin/env bash
set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ARCH=$(uname -m)
if [ -d "$SELF_DIR/bin/$ARCH" ]; then
    export PATH="$SELF_DIR/bin/$ARCH:$PATH"
fi
if [ "$ARCH" = "aarch64" ] && [ -d "$SELF_DIR/bin/aarch64_be" ]; then
    export PATH="$SELF_DIR/bin/aarch64_be:$PATH"
fi
exec python3 "$SELF_DIR/perflens_agent.py" "$@"
LAUNCHER_EOF
    chmod +x "$dest"
}

write_server_readme() {
    local dest="$1"
    cat > "$dest" <<README_EOF
PerfLens Server ${VERSION}
==========================

Real-time Linux perf profiler server. Receives perf data from agents over
TCP, decompresses it, and serves a web UI for browsing flame graphs,
function-level breakdowns, and annotated source.

Quick start
-----------

    ./perflens-server --source-dir /path/to/sources --binary /path/to/unstripped-binary

Then open http://localhost:8080 in your browser. Run an agent on the target
device that points back at this machine's IP address.

CLI options
-----------

    --port PORT           TCP port for agent connections (default: 9999)
    --http-port PORT      HTTP port for the web UI        (default: 8080)
    --source-dir DIR      Path to source code root
    --binary PATH         Path to unstripped binary with debug symbols
    --map PATH            Path to linker .map file (optional)
    --path-map FROM=TO    Compile-time path prefix mapping
                          (e.g. /build/src=/home/user/src)
    --addr2line PATH      Custom addr2line binary (optional)
    --max-samples N       Max samples retained in memory (default: 500000)

Required binaries (bundled in ./bin/)
-------------------------------------

    zstd       -- decompresses agent payloads (flag 1)
    addr2line  -- resolves addresses to source:line (from binutils)
    readelf    -- reads symbol tables (from binutils)

If these are not present in ./bin/, the launcher will fall back to the
system PATH. When missing entirely, source mapping and compressed streams
will be degraded -- the server still runs.

Directory layout
----------------

    perflens-server       -- launcher (bash)
    perflens-server-bin   -- frozen PyInstaller binary (if built)
    server/               -- script-mode sources (if --no-freeze)
    ui/                   -- bundled web UI (frozen mode only)
    bin/                  -- zstd / addr2line / readelf
    sessions/             -- saved profiling sessions (created on demand)
    VERSION               -- package version
README_EOF
}

write_pyinstaller_spec() {
    local spec_path="$1"
    local src_dir="$2"
    cat > "$spec_path" <<SPEC_EOF
# -*- mode: python ; coding: utf-8 -*-
# Generated by build_package.sh -- edit build_package.sh, not this file.

import os

block_cipher = None

SRC = r"${src_dir}"

datas = [
    (os.path.join(SRC, 'ui', 'index.html'), 'ui'),
    (os.path.join(SRC, 'ui', 'app.js'),     'ui'),
    (os.path.join(SRC, 'ui', 'style.css'),  'ui'),
    (os.path.join(SRC, 'VERSION'),          '.'),
]

a = Analysis(
    [os.path.join(SRC, 'server', 'perflens_server.py')],
    pathex=[os.path.join(SRC, 'server')],
    binaries=[],
    datas=datas,
    hiddenimports=['parser', 'source_mapper'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='perflens-server-bin',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
SPEC_EOF
}

# ---------------------------------------------------------------------------
# Server package
# ---------------------------------------------------------------------------

build_server_frozen() {
    local stage="$BUILD_DIR/server-stage"
    local pkg_dir="$BUILD_DIR/perflens-server-${VERSION}"
    rm -rf "$stage" "$pkg_dir"
    mkdir -p "$stage"

    info "Staging server sources"
    mkdir -p "$stage/server" "$stage/ui"
    cp server/perflens_server.py "$stage/server/"
    cp server/parser.py           "$stage/server/"
    cp server/source_mapper.py    "$stage/server/"
    cp ui/index.html ui/app.js ui/style.css "$stage/ui/"
    cp VERSION "$stage/VERSION" 2>/dev/null || echo "$VERSION" > "$stage/VERSION"

    local spec="$stage/perflens_server.spec"
    write_pyinstaller_spec "$spec" "$stage"

    info "Running PyInstaller"
    local pyi_ok=0
    if command -v pyinstaller >/dev/null 2>&1; then
        if ( cd "$stage" && pyinstaller --clean --noconfirm --distpath "$stage/_dist" \
                --workpath "$stage/_work" "$spec" ) >/dev/null 2>&1; then
            pyi_ok=1
        fi
    fi
    if [ "$pyi_ok" -ne 1 ]; then
        warn "PyInstaller failed or not available -- falling back to script mode"
        build_server_scripts
        return
    fi

    mkdir -p "$pkg_dir/bin" "$pkg_dir/sessions"
    cp "$stage/_dist/perflens-server-bin" "$pkg_dir/perflens-server-bin"
    chmod +x "$pkg_dir/perflens-server-bin"
    write_server_launcher "$pkg_dir/perflens-server"
    echo "$VERSION" > "$pkg_dir/VERSION"
    write_server_readme "$pkg_dir/README.txt"

    copy_server_bins "$pkg_dir/bin"

    local tarball="$DIST_DIR/perflens-server-${VERSION}.tar.gz"
    ( cd "$BUILD_DIR" && tar -czf "$tarball" "perflens-server-${VERSION}" )
    ok "Wrote ${tarball}"
}

build_server_scripts() {
    local pkg_dir="$BUILD_DIR/perflens-server-${VERSION}"
    rm -rf "$pkg_dir"
    mkdir -p "$pkg_dir/server" "$pkg_dir/ui" "$pkg_dir/bin" "$pkg_dir/lib" "$pkg_dir/sessions"

    cp server/perflens_server.py "$pkg_dir/server/"
    cp server/parser.py           "$pkg_dir/server/"
    cp server/source_mapper.py    "$pkg_dir/server/"
    cp ui/index.html ui/app.js ui/style.css "$pkg_dir/ui/"

    write_server_launcher "$pkg_dir/perflens-server"
    echo "$VERSION" > "$pkg_dir/VERSION"
    write_server_readme "$pkg_dir/README.txt"

    # Vendored pip packages (optional -- requirements-server.txt may be empty)
    if [ -f "$REPO_ROOT/requirements-server.txt" ] \
            && [ -s "$REPO_ROOT/requirements-server.txt" ]; then
        info "Vendoring pip dependencies into lib/"
        if command -v pip3 >/dev/null 2>&1; then
            pip3 install --quiet --target "$pkg_dir/lib" \
                -r "$REPO_ROOT/requirements-server.txt" --no-deps || \
                warn "pip3 install failed -- lib/ may be incomplete"
        else
            warn "pip3 not found -- skipping vendored dependencies"
        fi
    fi

    copy_server_bins "$pkg_dir/bin"

    local tarball="$DIST_DIR/perflens-server-${VERSION}.tar.gz"
    ( cd "$BUILD_DIR" && tar -czf "$tarball" "perflens-server-${VERSION}" )
    ok "Wrote ${tarball}"
}

copy_server_bins() {
    local dest="$1"
    mkdir -p "$dest"
    local any=0
    for name in zstd addr2line readelf; do
        if [ -f "server/bin/$name" ]; then
            cp "server/bin/$name" "$dest/"
            chmod +x "$dest/$name"
            any=1
        fi
    done
    if [ "$any" -eq 0 ]; then
        warn "No binaries found in server/bin/ -- drop zstd/addr2line/readelf there before distributing"
    fi
}

# ---------------------------------------------------------------------------
# Agent package
# ---------------------------------------------------------------------------

build_agent() {
    local pkg_dir="$BUILD_DIR/perflens-agent-${VERSION}"
    rm -rf "$pkg_dir"
    mkdir -p "$pkg_dir/bin/aarch64" "$pkg_dir/bin/aarch64_be" "$pkg_dir/bin/armv7l"

    cp agent/perflens_agent.py "$pkg_dir/perflens_agent.py"
    write_agent_launcher "$pkg_dir/perflens-agent"
    echo "$VERSION" > "$pkg_dir/VERSION"

    # Opportunistically copy cross-compiled zstd binaries if they exist
    local any=0
    for arch in aarch64 aarch64_be armv7l; do
        if [ -f "agent/bin/$arch/zstd" ]; then
            cp "agent/bin/$arch/zstd" "$pkg_dir/bin/$arch/zstd"
            chmod +x "$pkg_dir/bin/$arch/zstd"
            any=1
        fi
    done
    if [ "$any" -eq 0 ]; then
        warn "No cross-compiled zstd binaries found in agent/bin/{aarch64,aarch64_be,armv7l}/ -- agent will fall back to system zstd"
    fi

    # Quick sanity: make sure the agent still parses
    if ! python3 -c "import ast; ast.parse(open('$pkg_dir/perflens_agent.py').read())" 2>/dev/null; then
        err "Staged agent failed AST parse -- aborting"
        exit 1
    fi

    local tarball="$DIST_DIR/perflens-agent-${VERSION}.tar.gz"
    ( cd "$BUILD_DIR" && tar -czf "$tarball" "perflens-agent-${VERSION}" )
    ok "Wrote ${tarball}"
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if [ "$BUILD_SERVER" -eq 1 ]; then
    if [ "$NO_FREEZE" -eq 1 ]; then
        build_server_scripts
    else
        build_server_frozen
    fi
fi

if [ "$BUILD_AGENT" -eq 1 ]; then
    build_agent
fi

echo
info "Build complete. Artifacts in: $DIST_DIR"
ls -1 "$DIST_DIR" 2>/dev/null | sed 's/^/    /'
echo
cat <<DEPLOY_EOF
Deployment
----------
  Server:
    1. scp ${DIST_DIR}/perflens-server-${VERSION}.tar.gz host:
    2. ssh host 'tar xf perflens-server-${VERSION}.tar.gz'
    3. ssh host './perflens-server-${VERSION}/perflens-server --source-dir ... --binary ...'
    4. Browse to http://host:8080

  Agent:
    1. scp ${DIST_DIR}/perflens-agent-${VERSION}.tar.gz device:
    2. ssh device 'tar xf perflens-agent-${VERSION}.tar.gz'
    3. ssh device './perflens-agent-${VERSION}/perflens-agent --pid PID --server SERVER_IP'
DEPLOY_EOF
