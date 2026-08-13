"""Session persistence: what gets saved, what deliberately does not, and
the metadata contract the session list and replay both read.
"""

import json
import os

from perflens import __version__
from perflens.sessions import save_session


def make_samples(n=3):
    return [{'comm': 'w', 'pid': 1, 'tid': 1, 'event_count': 1,
             'event_type': 'cycles',
             'frames': [{'addr': '0', 'func': 'main', 'offset': '0x0',
                         'module': '/bin/w'}]}] * n


def test_session_with_samples_is_saved(tmp_path):
    session_dir = tmp_path / 'sess'
    session_dir.mkdir()
    save_session(str(session_dir), 'sess', 'device:9999', 2,
                 make_samples(), {})

    meta_path = session_dir / 'metadata.json'
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text())
    assert meta['total_samples'] == 3
    assert meta['event_types'] == ['cycles']


def test_metadata_records_the_running_version(tmp_path):
    """It used to be hardcoded, and had read '0.5.0' since that release."""
    session_dir = tmp_path / 'sess'
    session_dir.mkdir()
    save_session(str(session_dir), 'sess', 'device:9999', 1,
                 make_samples(), {})

    meta = json.loads((session_dir / 'metadata.json').read_text())
    assert meta['version'] == __version__


def test_empty_session_is_not_saved(tmp_path):
    """An agent started with --server reconnects with backoff, so a
    disconnect produces a connect/disconnect cycle every few seconds.
    Persisting each one filled the session list with rows replaying to
    nothing."""
    session_dir = tmp_path / 'empty'
    session_dir.mkdir()
    save_session(str(session_dir), 'empty', 'device:9999', 0, [], {})

    assert not (session_dir / 'metadata.json').exists(), \
        'an empty session was persisted'
    assert not session_dir.exists(), 'the empty session directory was left behind'


def test_empty_session_directory_with_chunks_is_kept(tmp_path):
    """Chunks on disk mean data arrived even if nothing parsed out of it --
    PERF_STAT-only chunks are normal at the start of a run. Keep those."""
    session_dir = tmp_path / 'statonly'
    session_dir.mkdir()
    (session_dir / 'chunk_00000.zst').write_bytes(b'x')
    save_session(str(session_dir), 'statonly', 'device:9999', 1, [], {})

    assert (session_dir / 'metadata.json').is_file()
    meta = json.loads((session_dir / 'metadata.json').read_text())
    assert meta['total_samples'] == 0
    assert meta['chunks'] == 1


def test_save_session_survives_an_unwritable_directory(tmp_path):
    """Saving is best-effort: it must never take the recv thread down."""
    missing = os.path.join(str(tmp_path), 'does-not-exist')
    save_session(missing, 'sess', 'device:9999', 1, make_samples(), {})
    assert not os.path.exists(missing)
