#!/usr/bin/env bash
#
# build_package.sh -- build PerfLens distributables locally.
#
#   Server: Python wheel + sdist (what `uvx perflens` / pip installs).
#   Agent:  static C binary, zero dependencies.
#
# Usage:
#   ./build_package.sh              # build server wheel + C agent
#   ./build_package.sh --server     # server wheel/sdist only
#   ./build_package.sh --agent-c    # C agent only (native static binary)
#
# CI (.github/workflows/build.yml) is the source of truth for release
# artifacts (all agent arches + static tools bundles); this script covers
# the local/native subset.
#
set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

BUILD_SERVER=1
BUILD_AGENT_C=1

for arg in "$@"; do
    case "$arg" in
        --server)    BUILD_SERVER=1; BUILD_AGENT_C=0 ;;
        --agent-c)   BUILD_AGENT_C=1; BUILD_SERVER=0 ;;
        -h|--help)
            sed -n '3,16p' "$0"
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
# Server: Python wheel + sdist
# ---------------------------------------------------------------------------

build_server() {
    info "Building Python wheel + sdist"
    if command -v uv >/dev/null 2>&1; then
        uv build
    elif python3 -c 'import build' 2>/dev/null; then
        python3 -m build
    else
        err "Need either 'uv' or the 'build' module (pip install build)"
        return 1
    fi

    local whl
    whl="$(ls "$DIST_DIR"/perflens-*-py3-none-any.whl 2>/dev/null | head -1)"
    if [ -z "$whl" ]; then
        err "wheel not produced"
        return 1
    fi

    # Contents check: server modules + bundled UI must be inside
    python3 - "$whl" <<'EOF'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
required = ['perflens/server.py', 'perflens/web.py',
            'perflens/provision.py', 'perflens/cli.py']
missing = [r for r in required if r not in names]
for f in ('index.html', 'app.js', 'style.css'):
    if not any(n.endswith(f) and '/ui/' in n for n in names):
        missing.append(f'ui/{f}')
if missing:
    sys.exit(f'wheel is missing: {missing}')
EOF
    ok "Wrote ${whl} (contents verified)"
}

# ---------------------------------------------------------------------------
# C Agent (static binary)
# ---------------------------------------------------------------------------

build_agent_c() {
    if [ ! -f "$REPO_ROOT/agent-c/perflens_agent.c" ]; then
        err "agent-c/perflens_agent.c not found"
        return 1
    fi

    info "Building C agent (native static binary)"
    if ! ( cd "$REPO_ROOT/agent-c" && make clean && make ) 2>&1; then
        err "C agent build failed"
        return 1
    fi

    local pkg_dir="$BUILD_DIR/perflens-agent-c-${VERSION}"
    rm -rf "$pkg_dir"
    mkdir -p "$pkg_dir"

    cp "$REPO_ROOT/agent-c/perflens-agent" "$pkg_dir/perflens-agent"
    chmod +x "$pkg_dir/perflens-agent"
    echo "$VERSION" > "$pkg_dir/VERSION"

    local tarball="$DIST_DIR/perflens-agent-c-${VERSION}.tar.gz"
    ( cd "$BUILD_DIR" && tar -czf "$tarball" "perflens-agent-c-${VERSION}" )
    ok "Wrote ${tarball}"

    # Raw binary with the stable release-asset name (used by
    # install-agent.sh and the agent's --update)
    local arch
    arch="$(uname -m)"
    cp "$REPO_ROOT/agent-c/perflens-agent" "$DIST_DIR/perflens-agent-linux-${arch}"
    ok "Wrote $DIST_DIR/perflens-agent-linux-${arch}"
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if [ "$BUILD_SERVER" -eq 1 ]; then
    build_server
fi

if [ "$BUILD_AGENT_C" -eq 1 ]; then
    build_agent_c
fi

echo
info "Build complete. Artifacts in: $DIST_DIR"
ls -1 "$DIST_DIR" 2>/dev/null | sed 's/^/    /'
echo
cat <<DEPLOY_EOF
Usage
-----
  Server (this machine or any machine with Python 3.10+):
    uvx --from ${DIST_DIR}/perflens-${VERSION}-py3-none-any.whl perflens serve
    # or from PyPI once published:  uvx perflens

  Agent (static C binary, zero deps):
    1. scp ${DIST_DIR}/perflens-agent-linux-\$(uname -m) device:perflens-agent
    2. ssh device './perflens-agent --listen'        # or --server SERVER_IP
    (or on the device:  curl -fsSL .../install-agent.sh | sh)
    (or from this machine:  perflens push-agent user@device)
DEPLOY_EOF
