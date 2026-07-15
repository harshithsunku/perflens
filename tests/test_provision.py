"""Provisioning tests against a fake release server.

Covers the full download path (fetch, sha256 sidecar verification, safe
extraction, install into ~/.perflens/bin), the refusal paths (checksum
mismatch, unreadable sidecar, incomplete bundle), offline degrade, the
resolve_tool precedence order, and CLI idempotence.
"""

import hashlib
import http.server
import io
import os
import tarfile
import threading

import pytest

ARCH = 'x86_64'
ASSET = f'perflens-tools-linux-{ARCH}.tar.gz'


def make_bundle(tools=('addr2line', 'readelf'), member_prefix=''):
    """Build an in-memory tools tarball like CI's build-tools job."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for name in tools:
            body = f'#!/bin/sh\necho fake-{name}\n'.encode()
            info = tarfile.TarInfo(member_prefix + name)
            info.size = len(body)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    assets = {}

    def do_GET(self):
        name = os.path.basename(self.path)
        if name in self.assets:
            body = self.assets[name]
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


@pytest.fixture()
def release_server():
    """A local HTTP server standing in for GitHub releases. Yields
    (base_url, assets_dict) — tests fill assets_dict."""
    assets = {}
    handler = type('Handler', (_Handler,), {'assets': assets})
    httpd = http.server.HTTPServer(('127.0.0.1', 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}', assets
    httpd.shutdown()
    t.join(timeout=5)


@pytest.fixture()
def provision(perflens_home, release_server, monkeypatch):
    """provision module pointed at the fake release server, isolated
    home, and a pinned bundle arch."""
    from perflens import provision as mod
    base, assets = release_server
    monkeypatch.setattr(mod, 'RELEASE_BASE', base)
    monkeypatch.setattr(mod.platform, 'machine', lambda: ARCH)
    mod._test_assets = assets
    yield mod
    del mod._test_assets


def publish(assets, bundle, checksum=None):
    assets[ASSET] = bundle
    digest = checksum or hashlib.sha256(bundle).hexdigest()
    assets[ASSET + '.sha256'] = f'{digest}  {ASSET}\n'.encode()


def installed_tools(mod):
    d = mod.bin_dir()
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


# ---------------------------------------------------------------------------
# Download path
# ---------------------------------------------------------------------------

def test_fresh_download_installs_tools(provision):
    publish(provision._test_assets, make_bundle())
    assert provision.download_tools_bundle(quiet=True) is True
    assert installed_tools(provision) == ['addr2line', 'readelf']
    for name in ('addr2line', 'readelf'):
        p = os.path.join(provision.bin_dir(), name)
        assert os.access(p, os.X_OK)
        assert provision._cached(name) == p


def test_download_strips_bundle_paths(provision):
    """Members with directory components install flat, basename only."""
    publish(provision._test_assets,
            make_bundle(member_prefix='deep/nested/../path/'))
    assert provision.download_tools_bundle(quiet=True) is True
    assert installed_tools(provision) == ['addr2line', 'readelf']


def test_download_ignores_unexpected_members(provision):
    publish(provision._test_assets,
            make_bundle(tools=('addr2line', 'readelf', 'evil', 'nm')))
    assert provision.download_tools_bundle(quiet=True) is True
    assert installed_tools(provision) == ['addr2line', 'readelf']


# ---------------------------------------------------------------------------
# Refusal paths
# ---------------------------------------------------------------------------

def test_checksum_mismatch_refused(provision):
    publish(provision._test_assets, make_bundle(), checksum='0' * 64)
    assert provision.download_tools_bundle(quiet=True) is False
    assert installed_tools(provision) == []


def test_sidecar_missing_refused(provision):
    provision._test_assets[ASSET] = make_bundle()  # no .sha256
    assert provision.download_tools_bundle(quiet=True) is False
    assert installed_tools(provision) == []


def test_sidecar_empty_refused(provision):
    publish(provision._test_assets, make_bundle())
    provision._test_assets[ASSET + '.sha256'] = b''
    assert provision.download_tools_bundle(quiet=True) is False
    assert installed_tools(provision) == []


def test_incomplete_bundle_refused(provision):
    publish(provision._test_assets, make_bundle(tools=('addr2line',)))
    assert provision.download_tools_bundle(quiet=True) is False
    assert installed_tools(provision) == []


def test_corrupt_tarball_refused(provision):
    bundle = b'this is not a tarball'
    publish(provision._test_assets, bundle)
    assert provision.download_tools_bundle(quiet=True) is False
    assert installed_tools(provision) == []


def test_offline_degrades(provision, monkeypatch):
    monkeypatch.setattr(provision, 'RELEASE_BASE',
                        'http://127.0.0.1:1/does-not-exist')
    assert provision.download_tools_bundle(quiet=True) is False
    assert installed_tools(provision) == []


def test_unsupported_arch_refused(provision, monkeypatch):
    monkeypatch.setattr(provision.platform, 'machine', lambda: 'riscv64')
    publish(provision._test_assets, make_bundle())
    assert provision.bundle_arch() is None
    assert provision.download_tools_bundle(quiet=True) is False


# ---------------------------------------------------------------------------
# resolve_tool precedence
# ---------------------------------------------------------------------------

def test_resolve_explicit_flag_wins(provision):
    path, origin = provision.resolve_tool(
        'addr2line', explicit='/custom/addr2line')
    assert (path, origin) == ('/custom/addr2line', 'flag')


def test_resolve_path_before_cache(provision, tmp_path, monkeypatch):
    fake = tmp_path / 'pathdir' / 'addr2line'
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\n')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(fake.parent))
    path, origin = provision.resolve_tool('addr2line')
    assert (path, origin) == (str(fake), 'path')


def test_resolve_cache_then_download(provision, monkeypatch):
    monkeypatch.setenv('PATH', '/nonexistent')
    # Nothing anywhere, no download requested
    assert provision.resolve_tool('addr2line') == (None, None)

    # download=True triggers the bundle fetch
    publish(provision._test_assets, make_bundle())
    path, origin = provision.resolve_tool('addr2line', download=True)
    assert origin == 'downloaded'
    assert path == os.path.join(provision.bin_dir(), 'addr2line')

    # Now cached: no server needed
    provision._test_assets.clear()
    path, origin = provision.resolve_tool('addr2line')
    assert origin == 'cache'
    assert path == os.path.join(provision.bin_dir(), 'addr2line')


def test_resolve_download_not_offered_for_other_tools(provision, monkeypatch):
    monkeypatch.setenv('PATH', '/nonexistent')
    publish(provision._test_assets, make_bundle())
    assert provision.resolve_tool('perf', download=True) == (None, None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_run_provision_downloads_then_idempotent(provision, monkeypatch,
                                                 capsys):
    monkeypatch.setenv('PATH', '/nonexistent')
    publish(provision._test_assets, make_bundle())

    assert provision.run_provision([]) == 0
    assert installed_tools(provision) == ['addr2line', 'readelf']

    # Second run: cache hit, no download (server cleared to prove it)
    provision._test_assets.clear()
    assert provision.run_provision([]) == 0
    assert 'nothing to do' in capsys.readouterr().out


def test_run_provision_offline_fails(provision, monkeypatch):
    monkeypatch.setenv('PATH', '/nonexistent')
    monkeypatch.setattr(provision, 'RELEASE_BASE',
                        'http://127.0.0.1:1/does-not-exist')
    assert provision.run_provision([]) == 1


def test_run_provision_status(provision, monkeypatch, capsys):
    monkeypatch.setenv('PATH', '/nonexistent')
    assert provision.run_provision(['--status']) == 0
    out = capsys.readouterr().out
    assert 'NOT FOUND' in out
    assert 'perflens provision' in out
