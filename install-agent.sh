#!/bin/sh
# PerfLens agent installer — POSIX sh, zero sudo, user-space only.
#
#   curl -fsSL https://raw.githubusercontent.com/harshithsunku/perflens/master/install-agent.sh | sh
#
# Detects the machine architecture, downloads the matching static binary
# from the latest GitHub release into ~/.perflens/bin (override with
# PERFLENS_INSTALL_DIR), and prints usage. The same asset naming is used
# by the agent's built-in `--update`.
#
# Environment overrides:
#   PERFLENS_INSTALL_DIR   install directory  (default: ~/.perflens/bin)
#   PERFLENS_UPDATE_URL    asset base URL     (default: GitHub latest release)

set -e

REPO="harshithsunku/perflens"
BASE_URL="${PERFLENS_UPDATE_URL:-https://github.com/${REPO}/releases/latest/download}"
INSTALL_DIR="${PERFLENS_INSTALL_DIR:-${HOME}/.perflens/bin}"

say()  { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- Detect architecture (matches agent-c release asset naming) ------------
machine="$(uname -m)"

# Endianness: od -tx2 reads the two bytes 01 00 as one 16-bit word —
# a little-endian machine sees 0x0001, a big-endian machine 0x0100.
if printf '\1\0' | od -An -tx2 | grep -q '0001'; then
    endian=little
else
    endian=big
fi

case "$machine" in
    x86_64)   arch=x86_64 ;;
    aarch64)  [ "$endian" = big ] && arch=aarch64_be || arch=aarch64 ;;
    aarch64_be) arch=aarch64_be ;;
    armeb*)   arch=armeb ;;
    arm*)     [ "$endian" = big ] && arch=armeb || arch=armv7 ;;
    *) fail "unsupported architecture: $machine" ;;
esac

asset="perflens-agent-linux-${arch}"
url="${BASE_URL}/${asset}"
dest="${INSTALL_DIR}/perflens-agent"

say "PerfLens agent installer"
say "  arch:    ${arch} (${machine}, ${endian}-endian)"
say "  from:    ${url}"
say "  to:      ${dest}"

mkdir -p "$INSTALL_DIR"
tmp="${dest}.download.$$"
trap 'rm -f "$tmp"' EXIT

# --- Download (curl preferred, wget fallback) -------------------------------
fetch() {  # fetch <url> <dest>  — quiet, returns non-zero on failure
    if command -v curl >/dev/null 2>&1; then
        curl -fSL --connect-timeout 20 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 20 -O "$2" "$1"
    else
        fail "neither curl nor wget found"
    fi
}

fetch "$url" "$tmp" || fail "download failed: $url"

# --- Verify the published checksum ------------------------------------------
# Every release publishes a <asset>.sha256 sidecar. This catches truncation,
# corruption and a poisoned single object. It does NOT authenticate the
# release: an attacker who controls the origin controls both files. See
# SECURITY.md.
sumfile="${tmp}.sha256"
if command -v sha256sum >/dev/null 2>&1; then
    if fetch "${url}.sha256" "$sumfile" 2>/dev/null; then
        expected="$(cut -d' ' -f1 < "$sumfile")"
        actual="$(sha256sum "$tmp" | cut -d' ' -f1)"
        rm -f "$sumfile"
        [ "$expected" = "$actual" ] || fail \
            "checksum mismatch for ${url}
  expected: ${expected}
  actual:   ${actual}
Refusing to install. Retry, or report this if it persists."
        say "  sha256:  verified"
    else
        rm -f "$sumfile"
        say "  sha256:  no sidecar published for this asset — skipping"
    fi
else
    say "  sha256:  sha256sum not available — skipping checksum verification"
fi

chmod 0755 "$tmp"

# --- Verify it runs before installing ---------------------------------------
if ! "$tmp" --version 2>/dev/null | grep -q perflens-agent; then
    fail "downloaded binary failed verification (wrong arch or corrupt download?)"
fi
version="$("$tmp" --version)"

mv "$tmp" "$dest"
trap - EXIT

say ""
say "Installed: ${version}"
say ""
say "Run it:"
say "  ${dest} --listen                 # wait for the server to connect"
say "  ${dest} --server <SERVER_IP>     # connect out to the server"
say ""
say "Update later with:  ${dest} --update"

case ":${PATH}:" in
    *":${INSTALL_DIR}:"*) ;;
    *) say ""
       say "Tip: add it to your PATH:"
       say "  export PATH=\"${INSTALL_DIR}:\$PATH\"" ;;
esac
