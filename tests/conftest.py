"""Shared pytest fixtures and helpers for the PerfLens test suite."""

import gzip
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
AGENT_BIN = os.path.join(REPO, 'agent-c', 'perflens-agent')

# Allow running from a plain checkout without an installed package
sys.path.insert(0, os.path.join(REPO, 'src'))


def fixture_session_names():
    """Names of the device-captured fixture sessions."""
    return sorted(
        d for d in os.listdir(FIXTURES)
        if os.path.isdir(os.path.join(FIXTURES, d)))


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
    meta = {
        'version': '0.5.0', 'session_id': session_id, 'agent': 'fixture',
        'timestamp': '2026-07-15T00:00:00', 'total_samples': 0,
        'chunks': i, 'event_types': [], 'perf_stat': {},
    }
    with open(os.path.join(dest, 'metadata.json'), 'w') as f:
        json.dump(meta, f)
    return session_id


@pytest.fixture()
def perflens_home(tmp_path, monkeypatch):
    """Isolated ~/.perflens for the test (caches, sessions, bin)."""
    home = tmp_path / 'perflens-home'
    home.mkdir()
    monkeypatch.setenv('PERFLENS_HOME', str(home))
    return home
