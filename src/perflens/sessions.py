"""Saved-session persistence: chunk spools on disk, lazy replay, metadata,
and perf.data import."""

import json
import os
import subprocess
import sys
from datetime import datetime

from perflens.agentlink import FLAG_DATA_ZSTD, decompress_payload
from perflens.aggregator import build_per_event_batch
from perflens.parser import get_event_types, parse_perf_script, split_perf_data
from perflens.source_mapper import build_annotated_source

MAX_IMPORT_SIZE = 500 * 1024 * 1024  # 500 MB


def safe_session_dir(cfg, session_id):
    """Resolve a session id (from a URL) to its directory, or None if the
    id would escape sessions_dir."""
    root = os.path.realpath(cfg.sessions_dir)
    session_dir = os.path.realpath(os.path.join(root, session_id))
    if session_dir == root or not session_dir.startswith(root + os.sep):
        return None
    return session_dir


def session_chunk_files(session_dir):
    """List a session's chunk files (.txt or .zst) in chunk order.

    Sorted numerically by chunk index so old 3-digit and new 5-digit
    names both order correctly.
    """
    def chunk_key(fname):
        stem = fname[len('chunk_'):].split('.', 1)[0]
        try:
            return int(stem)
        except ValueError:
            return 0

    return sorted(
        (f for f in os.listdir(session_dir)
         if f.startswith('chunk_') and (f.endswith('.txt')
                                        or f.endswith('.zst'))),
        key=chunk_key)


def _read_session_chunk(cfg, fpath):
    """Read one chunk file, decompressing .zst spools. Returns text or None."""
    try:
        with open(fpath, 'rb') as f:
            payload = f.read()
    except (IOError, OSError):
        return None
    if fpath.endswith('.zst'):
        return decompress_payload(cfg, payload, FLAG_DATA_ZSTD)
    return payload.decode('utf-8', errors='replace')


def load_session_chunks(cfg, session_dir):
    """Parse every chunk of a session into one sample list."""
    all_samples = []
    for fname in session_chunk_files(session_dir):
        text = _read_session_chunk(cfg, os.path.join(session_dir, fname))
        if text is None:
            continue
        script_text, _ = split_perf_data(text)
        all_samples.extend(parse_perf_script(script_text))
    return all_samples


def load_session_samples(cfg, session_id):
    """Load all samples from a saved session. Returns (samples, metadata)
    or (None, None)."""
    session_dir = safe_session_dir(cfg, session_id)
    if session_dir is None:
        return None, None
    meta_path = os.path.join(session_dir, 'metadata.json')
    if not os.path.isfile(meta_path):
        return None, None

    with open(meta_path) as f:
        metadata = json.load(f)

    return load_session_chunks(cfg, session_dir), metadata


def save_session(session_dir, session_id, agent_addr, chunk_count,
                 all_samples, perf_stat, hello=None,
                 metrics_snapshot=None, metrics_summary=None):
    """Save session metadata + metrics (chunks are spooled at receive time)."""
    try:
        event_types = get_event_types(all_samples)
        metadata = {
            'version': '0.5.0',
            'session_id': session_id,
            'agent': agent_addr,
            'timestamp': datetime.now().isoformat(),
            'total_samples': len(all_samples),
            'chunks': chunk_count,
            'event_types': event_types,
            'perf_stat': perf_stat,
        }
        if hello and hello.get('platform'):
            metadata['platform'] = hello['platform']
        if metrics_summary:
            metadata['metrics_summary'] = metrics_summary

        with open(os.path.join(session_dir, 'metadata.json'), 'w') as f:
            json.dump(metadata, f, indent=2)

        # Save metrics history
        if metrics_snapshot:
            with open(os.path.join(session_dir, 'metrics.json'), 'w') as f:
                json.dump(metrics_snapshot, f)

        print(f"[server] Session saved: {session_id} ({len(all_samples)} samples)",
              file=sys.stderr)
    except Exception as e:
        print(f"[server] Error saving session: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# perf.data import
# ---------------------------------------------------------------------------

def _run_perf_script(cfg, perf_data_path):
    """Run perf script on a perf.data file. Returns perf script text or raises."""
    if not cfg.perf_bin:
        raise RuntimeError('perf not found on server — cannot import perf.data')

    # Try with -F first (structured output, matches agent behavior)
    try:
        r = subprocess.run(
            [cfg.perf_bin, 'script', '-F', 'comm,pid,time,period,event,ip,sym,dso',
             '-i', perf_data_path],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            'perf script timed out (file too large?)') from None
    except Exception:
        pass

    # Fallback to plain perf script
    try:
        r = subprocess.run(
            [cfg.perf_bin, 'script', '-i', perf_data_path],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
        stderr = r.stderr.strip() if r.stderr else 'unknown error'
        raise RuntimeError(f'perf script failed: {stderr}')
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            'perf script timed out (file too large?)') from None


def import_perf_data(cfg, perf_data_path):
    """Import a perf.data file: run perf script, parse, save as session.

    Returns (session_id, all_samples, metadata) on success.
    Raises RuntimeError on failure.
    """
    if not os.path.isfile(perf_data_path):
        raise RuntimeError(f'file not found: {perf_data_path}')

    print(f"[server] Importing perf.data: {perf_data_path}", file=sys.stderr)
    script_text = _run_perf_script(cfg, perf_data_path)

    # Parse
    samples = parse_perf_script(script_text)
    if not samples:
        raise RuntimeError('perf script produced no samples '
                           '(file may be empty or corrupt)')

    event_types = get_event_types(samples)
    session_id = (datetime.now().strftime('%Y%m%d_%H%M%S')
                  + f'_{os.getpid():04x}_import')

    # Save as session (single chunk)
    session_dir = os.path.join(cfg.sessions_dir, session_id)
    os.makedirs(session_dir, exist_ok=True)

    with open(os.path.join(session_dir, 'chunk_000.txt'), 'w') as f:
        f.write(script_text)

    metadata = {
        'version': '0.4.0',
        'session_id': session_id,
        'agent': 'import',
        'timestamp': datetime.now().isoformat(),
        'total_samples': len(samples),
        'chunks': 1,
        'event_types': event_types,
        'perf_stat': {},
    }
    with open(os.path.join(session_dir, 'metadata.json'), 'w') as f:
        json.dump(metadata, f, indent=2)

    print(f"[server] Import complete: {session_id} "
          f"({len(samples)} samples, events: {event_types})",
          file=sys.stderr)

    return session_id, samples, metadata


# ---------------------------------------------------------------------------
# Per-event data builder (inline expansion + summaries)
# ---------------------------------------------------------------------------

def build_per_event_data(all_samples, event_types, mapper, source=False):
    """Build per-event data dict for UI consumption (batch: replay/import).

    Runs through the same AggregatorSet code path as live streaming, fed in
    one shot. `event_types` is accepted for backward compatibility; the
    events are derived from the samples themselves.
    """
    source_builder = None
    if source:
        if mapper:
            def source_builder(evt_orig):
                return build_annotated_source(mapper, evt_orig)
        else:
            def source_builder(evt_orig):
                return {}
    return build_per_event_batch(all_samples, mapper,
                                 source_builder=source_builder)
