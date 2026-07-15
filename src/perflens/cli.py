"""perflens console entry point.

    perflens                       # serve (default)
    perflens serve [server flags]  # explicit form
    perflens import FILE           # import a perf.data file and serve it
    perflens push-agent USER@HOST  # install the agent binary on a device
    perflens version
"""

import os
import subprocess
import sys

from perflens import __version__

_USAGE = """\
usage: perflens [command] [options]

Commands:
  serve         Start the server + web UI (default; takes the server flags)
  import FILE   Import a perf.data file at startup, then serve it
  push-agent USER@HOST [PORT]
                Detect the device arch over ssh, download the matching
                static agent binary from the latest GitHub release, and
                scp it to the device
  version       Print version and exit

Run `perflens serve --help` for the full server flag reference.
"""

AGENT_RELEASE_BASE = (os.environ.get('PERFLENS_UPDATE_URL')
                      or 'https://github.com/harshithsunku/perflens'
                         '/releases/latest/download')

# uname -m on the device -> release asset arch suffix
_ARCH_MAP = {
    'x86_64': 'x86_64',
    'aarch64': 'aarch64',
    'aarch64_be': 'aarch64_be',
    'armv7l': 'armv7',
    'armv6l': 'armv7',
    'armeb': 'armeb',
}


def _run_serve(argv):
    from perflens.server import main as serve_main
    serve_main(argv)


def _run_import(argv):
    if not argv or argv[0].startswith('-'):
        print('usage: perflens import FILE [server flags]', file=sys.stderr)
        return 2
    path = argv[0]
    if not os.path.isfile(path):
        print(f'error: file not found: {path}', file=sys.stderr)
        return 2
    _run_serve(['--import', path] + argv[1:])
    return 0


def _agent_cache_dir():
    from perflens.symcache import perflens_home
    d = os.path.join(perflens_home(), 'bin', 'agents')
    os.makedirs(d, exist_ok=True)
    return d


def _download(url, dest):
    """Download url to dest with urllib (stdlib — no curl dependency)."""
    import urllib.request
    tmp = dest + '.download.%d' % os.getpid()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, \
                open(tmp, 'wb') as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, dest)
        return True
    except Exception as e:
        print(f'error: download failed: {e}\n  {url}', file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _run_push_agent(argv):
    if not argv:
        print('usage: perflens push-agent USER@HOST [PORT]', file=sys.stderr)
        return 2
    host = argv[0]
    ssh_port = argv[1] if len(argv) > 1 else '22'

    print(f'[push-agent] Detecting architecture on {host} ...')
    try:
        r = subprocess.run(
            ['ssh', '-p', ssh_port, '-o', 'ConnectTimeout=15', host,
             'uname -m'],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f'error: ssh failed: {e}', file=sys.stderr)
        return 1
    if r.returncode != 0:
        print(f'error: ssh failed: {r.stderr.strip()}', file=sys.stderr)
        return 1

    machine = r.stdout.strip()
    arch = _ARCH_MAP.get(machine)
    if not arch:
        print(f'error: unsupported device architecture: {machine}',
              file=sys.stderr)
        return 1
    asset = f'perflens-agent-linux-{arch}'
    print(f'[push-agent] Device is {machine} -> asset {asset}')

    cached = os.path.join(_agent_cache_dir(), asset)
    if not os.path.isfile(cached):
        url = f'{AGENT_RELEASE_BASE}/{asset}'
        print(f'[push-agent] Downloading {url} ...')
        if not _download(url, cached):
            print('Tip: build it yourself with '
                  '`make -C agent-c CROSS=<prefix>` and scp the binary.',
                  file=sys.stderr)
            return 1
        os.chmod(cached, 0o755)
    else:
        print(f'[push-agent] Using cached binary: {cached}')

    dest = '~/.perflens/bin/perflens-agent'
    print(f'[push-agent] Installing to {host}:{dest} ...')
    mkdir = subprocess.run(
        ['ssh', '-p', ssh_port, host, 'mkdir -p ~/.perflens/bin'],
        capture_output=True, text=True, timeout=30)
    if mkdir.returncode != 0:
        print(f'error: {mkdir.stderr.strip()}', file=sys.stderr)
        return 1
    scp = subprocess.run(
        ['scp', '-P', ssh_port, cached, f'{host}:{dest}'],
        capture_output=True, text=True, timeout=120)
    if scp.returncode != 0:
        print(f'error: scp failed: {scp.stderr.strip()}', file=sys.stderr)
        return 1
    subprocess.run(['ssh', '-p', ssh_port, host, f'chmod +x {dest}'],
                   capture_output=True, timeout=30)

    ver = subprocess.run(
        ['ssh', '-p', ssh_port, host, f'{dest} --version'],
        capture_output=True, text=True, timeout=30)
    print(f'[push-agent] Installed: {ver.stdout.strip() or "(version check failed)"}')
    print()
    print('Run it on the device:')
    print(f'  ssh {host} "~/.perflens/bin/perflens-agent --listen"')
    print('  # or: --server <your-ip> to connect out to this machine')
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ('version', '--version', '-V'):
        print(f'perflens {__version__}')
        return 0
    if argv and argv[0] in ('help', '--help', '-h'):
        print(_USAGE)
        return 0

    if argv and argv[0] == 'serve':
        _run_serve(argv[1:])
        return 0
    if argv and argv[0] == 'import':
        return _run_import(argv[1:])
    if argv and argv[0] == 'push-agent':
        return _run_push_agent(argv[1:])

    # Default: everything is server flags
    _run_serve(argv)
    return 0


if __name__ == '__main__':
    sys.exit(main())
