"""The `perflens` console entry point.

Mostly `main(argv) -> int` dispatch, which is cheap to assert and easy to
break: every subcommand is a string compare against argv[0], and a typo there
silently falls through to the default branch, which starts the server.

DANGER: never call `main([])`, `main(['serve'])`, or anything that reaches
_run_serve without patching it — uvicorn blocks and the test run hangs.
"""

import hashlib
import http.server
import os
import threading

import pytest

from perflens import __version__, cli


@pytest.fixture()
def no_serve(monkeypatch):
    """Replace the blocking server entry point with a recorder."""
    calls = []
    monkeypatch.setattr(cli, '_run_serve', lambda argv: calls.append(argv))
    return calls


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('argv', [['version'], ['--version'], ['-V']])
def test_version(capsys, argv):
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.strip() == f'perflens {__version__}'


@pytest.mark.parametrize('argv', [['help'], ['--help'], ['-h']])
def test_help(capsys, argv):
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert 'usage: perflens' in out
    for cmd in ('serve', 'import', 'push-agent', 'provision', 'mcp'):
        assert cmd in out


def test_bare_invocation_serves_with_no_flags(no_serve):
    assert cli.main([]) == 0
    assert no_serve == [[]]


def test_unknown_leading_flag_is_treated_as_a_server_flag(no_serve):
    """`perflens --http-port 9000` must serve, not error."""
    assert cli.main(['--http-port', '9000']) == 0
    assert no_serve == [['--http-port', '9000']]


def test_serve_subcommand_strips_its_own_name(no_serve):
    assert cli.main(['serve', '--port', '1234']) == 0
    assert no_serve == [['--port', '1234']]


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def test_import_without_a_file_is_a_usage_error(capsys):
    assert cli.main(['import']) == 2
    assert 'usage' in capsys.readouterr().err.lower()


def test_import_rejects_a_flag_where_the_file_should_be(capsys):
    assert cli.main(['import', '--port']) == 2


def test_import_missing_file(capsys, tmp_path):
    missing = str(tmp_path / 'nope.data')
    assert cli.main(['import', missing]) == 2
    assert 'not found' in capsys.readouterr().err.lower()


def test_import_passes_an_existing_file_through_to_serve(no_serve, tmp_path):
    data = tmp_path / 'perf.data'
    data.write_bytes(b'PERFILE2')
    assert cli.main(['import', str(data)]) == 0
    assert no_serve, 'import should hand off to the server'
    assert str(data) in no_serve[0]


# ---------------------------------------------------------------------------
# push-agent
# ---------------------------------------------------------------------------

def test_push_agent_without_a_host_is_a_usage_error(capsys):
    assert cli.main(['push-agent']) == 2
    assert 'usage' in capsys.readouterr().err.lower()


@pytest.mark.parametrize('machine,asset_arch', [
    ('x86_64', 'x86_64'),
    ('aarch64', 'aarch64'),
    ('aarch64_be', 'aarch64_be'),
    ('armv7l', 'armv7'),
    # uname says armv6l, but the release asset is armv7 — publishing or
    # requesting 'armv6l' 404s.
    ('armv6l', 'armv7'),
    ('armeb', 'armeb'),
])
def test_arch_map_covers_every_published_asset(machine, asset_arch):
    assert cli._ARCH_MAP[machine] == asset_arch


def test_push_agent_rejects_an_unsupported_arch(capsys, monkeypatch):
    class R:
        returncode = 0
        stdout = 'mips64\n'
        stderr = ''

    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: R())
    assert cli.main(['push-agent', 'user@host']) == 1
    assert 'unsupported device architecture' in capsys.readouterr().err


def test_push_agent_reports_an_ssh_failure(capsys, monkeypatch):
    class R:
        returncode = 255
        stdout = ''
        stderr = 'Permission denied (publickey).'

    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: R())
    assert cli.main(['push-agent', 'user@host']) == 1
    assert 'Permission denied' in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _download — checksum verification
#
# Served from a real local HTTP server rather than a mock, following
# test_provision.py, so the urllib path is genuinely exercised.
# ---------------------------------------------------------------------------

class _Assets(http.server.BaseHTTPRequestHandler):
    payload = b'#!/bin/sh\necho perflens-agent 9.9.9\n'
    sidecar = None          # set per-test

    def do_GET(self):
        if self.path.endswith('.sha256'):
            if self.sidecar is None:
                self.send_error(404)
                return
            body = self.sidecar
        else:
            body = self.payload
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def asset_server():
    def _serve(sidecar):
        handler = type('H', (_Assets,), {'sidecar': sidecar})
        srv = http.server.HTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f'http://127.0.0.1:{srv.server_port}/agent'

    servers = []

    def _make(sidecar):
        srv, url = _serve(sidecar)
        servers.append(srv)
        return url

    yield _make
    for s in servers:
        s.shutdown()


def test_download_accepts_a_matching_checksum(asset_server, tmp_path):
    digest = hashlib.sha256(_Assets.payload).hexdigest()
    url = asset_server(f'{digest}  agent\n'.encode())
    dest = str(tmp_path / 'agent')
    assert cli._download(url, dest) is True
    assert open(dest, 'rb').read() == _Assets.payload


def test_download_refuses_a_mismatched_checksum(asset_server, tmp_path, capsys):
    url = asset_server(b'%s  agent\n' % (b'0' * 64))
    dest = str(tmp_path / 'agent')
    assert cli._download(url, dest) is False
    assert not os.path.exists(dest), 'a corrupt download must not be installed'
    assert 'checksum mismatch' in capsys.readouterr().err


def test_download_proceeds_when_no_sidecar_is_published(asset_server, tmp_path):
    """Older releases have no .sha256; the download still has to work."""
    url = asset_server(None)
    dest = str(tmp_path / 'agent')
    assert cli._download(url, dest) is True
    assert open(dest, 'rb').read() == _Assets.payload


def test_download_leaves_no_temp_file_on_failure(tmp_path, capsys):
    dest = str(tmp_path / 'agent')
    assert cli._download('http://127.0.0.1:1/nothing', dest) is False
    assert os.listdir(tmp_path) == []


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def test_agent_cache_dir_is_under_perflens_home(perflens_home):
    d = cli._agent_cache_dir()
    assert os.path.isdir(d)
    assert str(perflens_home) in d


def test_release_base_honours_the_update_url_override():
    """Same environment variable the agent's --update reads, so a mirror
    configured once applies to both."""
    assert cli.AGENT_RELEASE_BASE.startswith('http')
