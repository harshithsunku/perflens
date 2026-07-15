"""User-space provisioning of external binaries.

PerfLens shells out to a few tools (addr2line/readelf for source mapping,
optionally their llvm- variants, zstd as a decompression fallback, perf
for .data imports). On developer boxes these are usually installed; on
locked-down corporate machines they often are not, and installing them
needs privileges the user doesn't have.

Resolution order for every tool (all user-space, zero sudo):

  1. explicit path from a CLI flag (used as-is, never second-guessed)
  2. system PATH
  3. ``~/.perflens/bin`` (previously provisioned)
  4. for addr2line/readelf only: download the static tools bundle
     ``perflens-tools-linux-<arch>.tar.gz`` from the GitHub release,
     verify it against its ``.sha256`` sidecar, extract into
     ``~/.perflens/bin``

Any failure degrades gracefully: the caller gets ``None``, profiling
still works, source mapping is disabled, and the user gets printed
instructions (``perflens provision`` retries the download explicitly).
"""

import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request

from perflens.symcache import perflens_home

RELEASE_BASE = (os.environ.get('PERFLENS_UPDATE_URL')
                or 'https://github.com/harshithsunku/perflens'
                   '/releases/latest/download')

# Tools shipped in the downloadable static bundle (built in CI).
BUNDLE_TOOLS = ('addr2line', 'readelf')
# Architectures the bundle is published for.
BUNDLE_ARCHES = ('x86_64', 'aarch64')

_DOWNLOAD_TIMEOUT = 30


def bin_dir():
    """The user-space binary cache: ~/.perflens/bin."""
    return os.path.join(perflens_home(), 'bin')


def _cached(name):
    p = os.path.join(bin_dir(), name)
    if os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    return None


def resolve_tool(name, explicit=None, download=False):
    """Resolve one external tool.

    Returns (path, origin) where origin is one of 'flag', 'path',
    'cache', 'downloaded' — or (None, None) if unavailable.
    """
    if explicit:
        return explicit, 'flag'
    p = shutil.which(name)
    if p:
        return p, 'path'
    p = _cached(name)
    if p:
        return p, 'cache'
    if download and name in BUNDLE_TOOLS:
        if download_tools_bundle():
            p = _cached(name)
            if p:
                return p, 'downloaded'
    return None, None


def bundle_arch():
    """Release-asset arch suffix for this machine, or None if the tools
    bundle isn't published for it."""
    m = platform.machine()
    return m if m in BUNDLE_ARCHES else None


def _fetch(url, dest, timeout=_DOWNLOAD_TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as resp, \
            open(dest, 'wb') as f:
        shutil.copyfileobj(resp, f, 1 << 16)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def download_tools_bundle(quiet=False):
    """Download + verify + install the static tools bundle into
    ~/.perflens/bin. Returns True on success, False on any failure
    (callers degrade; failure details go to stderr unless quiet)."""

    def say(msg):
        if not quiet:
            print(f'[provision] {msg}', file=sys.stderr)

    arch = bundle_arch()
    if not arch:
        say(f'no static tools bundle published for {platform.machine()}')
        return False

    asset = f'perflens-tools-linux-{arch}.tar.gz'
    url = f'{RELEASE_BASE}/{asset}'
    say(f'downloading {url} ...')

    with tempfile.TemporaryDirectory(prefix='perflens-tools-') as td:
        tarball = os.path.join(td, asset)
        try:
            _fetch(url, tarball)
            _fetch(url + '.sha256', tarball + '.sha256')
        except Exception as e:
            say(f'download failed: {e}')
            say('to fix: retry with `perflens provision`, install binutils '
                'via your package manager, or pass --addr2line/--readelf')
            return False

        # Sidecar format: "<hex>  <filename>" (sha256sum output)
        try:
            with open(tarball + '.sha256') as f:
                expected = f.read().split()[0].strip().lower()
        except (OSError, IndexError):
            say('checksum sidecar unreadable — refusing to install')
            return False
        actual = _sha256(tarball)
        if actual != expected:
            say(f'checksum mismatch (expected {expected[:16]}..., '
                f'got {actual[:16]}...) — refusing to install')
            return False

        # Extract only the expected flat tool files — nothing else, no
        # paths, no links.
        extracted = {}
        try:
            with tarfile.open(tarball, 'r:gz') as tf:
                for member in tf.getmembers():
                    base = os.path.basename(member.name)
                    if base not in BUNDLE_TOOLS or not member.isfile():
                        continue
                    src = tf.extractfile(member)
                    dest = os.path.join(td, base)
                    with open(dest, 'wb') as out:
                        shutil.copyfileobj(src, out)
                    extracted[base] = dest
        except (tarfile.TarError, OSError) as e:
            say(f'bundle extract failed: {e}')
            return False

        missing = [t for t in BUNDLE_TOOLS if t not in extracted]
        if missing:
            say(f'bundle is missing tools: {missing} — refusing to install')
            return False

        os.makedirs(bin_dir(), exist_ok=True)
        for name, src in extracted.items():
            dest = os.path.join(bin_dir(), name)
            os.chmod(src, 0o755)
            os.replace(src, dest)
        say(f'installed {", ".join(sorted(extracted))} into {bin_dir()}')
        return True


# Tools reported by `perflens provision --status`. The two bundle tools
# are what the download can fix; the rest are informational.
STATUS_TOOLS = ('addr2line', 'readelf', 'llvm-addr2line', 'llvm-dwarfdump',
                'zstd', 'perf')


def provision_status():
    """Resolution table for the standard tool set (no downloads)."""
    rows = []
    for name in STATUS_TOOLS:
        path, origin = resolve_tool(name)
        rows.append({'tool': name, 'path': path, 'origin': origin})
    return rows


def run_provision(argv):
    """CLI: `perflens provision [--status]`."""
    if '--status' in argv:
        rows = provision_status()
        width = max(len(r['tool']) for r in rows)
        for r in rows:
            if r['path']:
                print(f"  {r['tool']:<{width}}  {r['path']}  ({r['origin']})")
            else:
                print(f"  {r['tool']:<{width}}  NOT FOUND")
        if not any(r['path'] for r in rows
                   if r['tool'] in BUNDLE_TOOLS):
            print()
            print('Run `perflens provision` to download the static '
                  'addr2line/readelf bundle.')
        return 0

    have = {t: resolve_tool(t)[0] for t in BUNDLE_TOOLS}
    missing = [t for t, p in have.items() if not p]
    if not missing:
        for t, p in have.items():
            print(f'  {t}: {p}')
        print('All bundle tools already available — nothing to do.')
        return 0
    ok = download_tools_bundle()
    if ok:
        for t in BUNDLE_TOOLS:
            path, origin = resolve_tool(t)
            print(f'  {t}: {path} ({origin})')
        return 0
    return 1
