"""Cross-toolchain tool resolution.

`--toolchain-prefix` is the only way to symbolize a binary built for another
architecture, and the flag documents a bare prefix (`armeb-linux-musleabi-`),
not an absolute path. Resolving that bare name is therefore the whole feature.
"""

import os

import pytest

from perflens.config import ServerConfig, config_from_args, probe_tools

PREFIX = 'zz-crossarch-'


@pytest.fixture()
def fake_toolchain(tmp_path, monkeypatch):
    """A directory on PATH holding <prefix>addr2line and <prefix>readelf."""
    binz = tmp_path / 'toolchain-bin'
    binz.mkdir()
    for tool in ('addr2line', 'readelf'):
        p = binz / (PREFIX + tool)
        p.write_text('#!/bin/sh\nexit 0\n')
        p.chmod(0o755)
    monkeypatch.setenv('PATH', str(binz) + os.pathsep + os.environ['PATH'])
    return binz


def _probe(prefix):
    cfg = config_from_args(['--toolchain-prefix', prefix])
    probe_tools(cfg)
    return cfg


def test_bare_toolchain_prefix_resolves_both_tools(fake_toolchain):
    """A bare prefix must select the cross tools, not the host's.

    os.path.isfile() is False for a relative name, so the addr2line branch
    used to fall through and silently overwrite the cross tool with the
    system one — logged as "(system)", with no error. That symbolizes a
    foreign binary with the host's addr2line, which is exactly the case the
    flag exists to prevent.
    """
    cfg = _probe(PREFIX)
    assert cfg.addr2line_bin == str(fake_toolchain / (PREFIX + 'addr2line'))
    assert cfg.readelf_bin == str(fake_toolchain / (PREFIX + 'readelf'))


def test_absolute_toolchain_prefix_still_resolves(fake_toolchain):
    cfg = _probe(str(fake_toolchain) + os.sep + PREFIX)
    assert cfg.addr2line_bin == str(fake_toolchain / (PREFIX + 'addr2line'))
    assert cfg.readelf_bin == str(fake_toolchain / (PREFIX + 'readelf'))


def test_unresolvable_prefix_falls_back_without_claiming_the_toolchain():
    """An unresolvable prefix must not leave the bare name in place.

    Falling back to the host tools is the right behaviour — but the config
    must hold a real path afterwards, not `zz-nonexistent-addr2line`, which
    would fail at the first addr2line call instead of at startup.
    """
    cfg = _probe('zz-nonexistent-toolchain-')
    for got in (cfg.addr2line_bin, cfg.readelf_bin):
        assert got is None or os.path.isabs(got), got
        assert got is None or 'zz-nonexistent-toolchain-' not in got


def test_explicit_addr2line_beats_the_prefix(fake_toolchain, tmp_path):
    """--addr2line is used as-is and is never second-guessed."""
    explicit = tmp_path / 'my-addr2line'
    explicit.write_text('#!/bin/sh\nexit 0\n')
    explicit.chmod(0o755)
    cfg = ServerConfig(addr2line_bin=str(explicit),
                       readelf_bin=PREFIX + 'readelf')
    probe_tools(cfg)
    assert cfg.addr2line_bin == str(explicit)
    assert cfg.readelf_bin == str(fake_toolchain / (PREFIX + 'readelf'))


# ---------------------------------------------------------------------------
# Argument checks
# ---------------------------------------------------------------------------

def test_equal_agent_and_http_ports_are_refused():
    with pytest.raises(SystemExit):
        config_from_args(['--port', '8080', '--http-port', '8080'])


def test_malformed_map_entries_are_reported_and_skipped(capsys):
    cfg = config_from_args(['--path-map', '/build/src=/home/me/src,bogus,=x,',
                            '--module-map', '/opt/app=/tmp/app.sym,nope'])
    assert cfg.path_map == {'/build/src': '/home/me/src'}
    assert cfg.module_map == {'/opt/app': '/tmp/app.sym'}
    err = capsys.readouterr().err
    assert "ignoring --path-map entry 'bogus'" in err
    assert "ignoring --path-map entry '=x'" in err
    assert "ignoring --module-map entry 'nope'" in err
