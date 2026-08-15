"""Shared pytest fixtures and helpers for the PerfLens test suite."""

import functools
import gzip
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
AGENT_BIN = os.path.join(REPO, 'agent-c', 'perflens-agent')

# Allow running from a plain checkout without an installed package
sys.path.insert(0, os.path.join(REPO, 'src'))


@functools.lru_cache(maxsize=1)
def agent_binary_runs():
    """True when AGENT_BIN exists and can actually execute on this host.

    `os.access(AGENT_BIN, os.X_OK)` is not sufficient. A cross-compiled binary
    left in the native path is still marked executable, so the skip guard would
    not fire and every agent test would instead fail with
    `OSError: [Errno 8] Exec format error`. That happened for real: `make
    all-cross` used to build every architecture into this same path, leaving
    whichever one was built last. The Makefile no longer does that, but the
    binary can still be stale or foreign from an older checkout, and a clean
    skip is much easier to read than 15 errors.
    """
    if not os.access(AGENT_BIN, os.X_OK):
        return False
    try:
        r = subprocess.run([AGENT_BIN, '--version'],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and b'perflens-agent' in r.stdout


def fixture_session_names():
    """Names of the device-captured fixture sessions."""
    return sorted(
        d for d in os.listdir(FIXTURES)
        if os.path.isdir(os.path.join(FIXTURES, d)))


# The default fixture session for tests that just need realistic data.
FIXTURE = fixture_session_names()[0]


def load_fixture_chunks(name):
    """Parse a fixture session's gzipped chunks into per-chunk sample lists."""
    from perflens.parser import parse_perf_script, split_perf_data
    d = os.path.join(FIXTURES, name)
    chunks = []
    for fname in sorted(os.listdir(d)):
        if fname.startswith('chunk_') and fname.endswith('.txt.gz'):
            with gzip.open(os.path.join(d, fname), 'rt') as f:
                text = f.read()
            script_text, _ = split_perf_data(text)
            chunks.append(parse_perf_script(script_text))
    return chunks


def materialize_fixture_session(name, sessions_dir, session_id=None):
    """Decompress a fixture into an on-disk session dir the server can
    list and replay. Returns the session id."""
    session_id = session_id or name
    src = os.path.join(FIXTURES, name)
    dest = os.path.join(sessions_dir, session_id)
    os.makedirs(dest, exist_ok=True)
    i = 0
    for fname in sorted(os.listdir(src)):
        if fname.startswith('chunk_') and fname.endswith('.txt.gz'):
            with gzip.open(os.path.join(src, fname), 'rb') as f:
                data = f.read()
            with open(os.path.join(dest, f'chunk_{i:05d}.txt'), 'wb') as f:
                f.write(data)
            i += 1
    # Carry the captured metadata through (perf_stat, platform, totals) so
    # replay renders the counter cards a real session would. Identity fields
    # are forced, so callers can materialize the same fixture under any id.
    # event_types stays empty on purpose: the server's per-event keys are
    # authoritative (store/live.ts falls back to them), so a metadata list
    # that disagreed would offer dead entries in the event dropdown.
    with open(os.path.join(src, 'metadata.json'), encoding='utf-8') as f:
        meta = json.load(f)
    meta.update({
        'session_id': session_id, 'agent': 'fixture',
        'chunks': i, 'event_types': [],
    })
    with open(os.path.join(dest, 'metadata.json'), 'w') as f:
        json.dump(meta, f)
    return session_id


@pytest.fixture()
def core(tmp_path, perflens_home):
    """A fresh AppContext (no workers, no source mapper).

    ui_dir is a stand-in for the built frontend, so the suite always
    exercises the shipped configuration (static assets mounted) whether
    or not this machine has run `npm --prefix frontend run build`. The
    two tests that care about the real assets, or about their absence,
    build their own app.
    """
    from perflens.app import AppContext
    from perflens.config import ServerConfig
    from perflens.state import MetricsState, ProfilingState

    sessions_dir = str(tmp_path / 'sessions')
    os.makedirs(sessions_dir)
    ui_dir = tmp_path / 'ui'
    ui_dir.mkdir()
    (ui_dir / 'index.html').write_text('<!DOCTYPE html><title>stub</title>')
    cfg = ServerConfig(
        source_dir=str(tmp_path),
        sessions_dir=sessions_dir,
        browse_root=str(tmp_path),
        ui_dir=str(ui_dir),
    )
    yield AppContext(config=cfg,
                     state=ProfilingState(max_samples=100000),
                     metrics=MetricsState())


@pytest.fixture()
def client(core):
    from fastapi.testclient import TestClient

    from perflens import web
    with TestClient(web.create_app(core)) as c:
        yield c


@pytest.fixture()
def perflens_home(tmp_path, monkeypatch):
    """Isolated ~/.perflens for the test (caches, sessions, bin)."""
    home = tmp_path / 'perflens-home'
    home.mkdir()
    monkeypatch.setenv('PERFLENS_HOME', str(home))
    return home
