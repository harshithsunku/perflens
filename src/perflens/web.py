"""PerfLens HTTP layer — FastAPI + uvicorn (ASGI).

Division of labor:
- ``perflens.app`` owns the AppContext (config, state, metrics, agent slot).
- ``perflens.agentlink``/``perflens.state`` own the agent TCP protocol and
  aggregation — all on plain threads.
- This module owns HTTP: routing, SSE fan-out, static UI serving.

Threading model: uvicorn's event loop serves HTTP. Handlers that touch
disk, subprocesses, or block on the agent are plain ``def`` routes (or
explicitly pushed to the threadpool) so they never stall the loop. The
agent recv threads and the rebuild worker publish SSE events through
``_SSEHub.publish`` which hops onto the loop via ``call_soon_threadsafe``.

Routes receive the AppContext through the ``Ctx`` dependency
(``request.app.state.ctx``) — no module globals.

Error model (API v2): every error renders as
``{"error": {"code": "<slug>", "message": "..."}}`` with a real status
code — 400 validation, 403 permission, 404 missing, 409 wrong server
state (no agent / no mapper), 413 too large, 502 agent transport.
"""

import asyncio
import gzip
import json
import os
import shutil
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Optional, Union

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from perflens import agentlink, export, sessions
from perflens.api import models
from perflens.api.responses import deflate_segment
from perflens.api.responses import dumps as _dumps
from perflens.api.responses import error_response as _err
from perflens.api.responses import json_response as _json
from perflens.api.responses import spliced_response as _spliced
from perflens.config import create_source_mapper
from perflens.parser import (build_flamegraph_data, build_function_summary,
                             filter_samples_by_event, get_event_types,
                             resolve_event)

if TYPE_CHECKING:
    from perflens.app import AppContext

router = APIRouter()


def get_ctx(request: Request) -> 'AppContext':
    return request.app.state.ctx


# FastAPI dependency used by every route
Ctx = Depends(get_ctx)

# Standard OpenAPI error annotation for routes with failure modes
_ERR = {'model': models.ErrorResponse}

# Python 3.13 renamed 413's reason phrase ("Request Entity Too Large" ->
# "Content Too Large"), and FastAPI falls back to that phrase when a
# response carries no description — which would make the exported schema
# depend on the interpreter version. Pin it.
_ERR_TOO_LARGE = {'model': models.ErrorResponse,
                  'description': 'Content Too Large'}


# ---------------------------------------------------------------------------
# SSE hub — bridges worker threads into asyncio client queues
# ---------------------------------------------------------------------------

class _SSEHub:
    """Per-client asyncio queues; thread-side publishers hop onto the
    event loop via call_soon_threadsafe. Slow clients drop their oldest
    queued message instead of blocking the broadcast."""

    def __init__(self):
        self.loop = None
        self.queues = set()     # touched only on the event loop
        self._lock = threading.Lock()

    def attach(self, loop):
        with self._lock:
            self.loop = loop

    def publish(self, event_type, data):
        """Called from any thread (agent recv loops, rebuild worker)."""
        with self._lock:
            loop = self.loop
        if loop is None or loop.is_closed():
            return
        msg = _sse_frame(event_type, data)
        try:
            loop.call_soon_threadsafe(self._fanout, msg)
        except RuntimeError:
            pass  # loop shut down mid-publish

    def _fanout(self, msg):
        for q in list(self.queues):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)


def _sse_frame(event_type, data):
    return (b'event: ' + event_type.encode('utf-8') +
            b'\ndata: ' + _dumps(data) + b'\n\n')


# ---------------------------------------------------------------------------
# Core API — status / snapshot / stream
# ---------------------------------------------------------------------------

@router.get('/api/status', response_model=models.Status)
def api_status(ctx=Ctx):
    st = ctx.state
    with st.lock:
        version = st.version_locked()
        connected, addr = st.agent_connected, st.agent_addr
    return _json({'status': 'ok', 'agent_connected': connected,
                  'agent_addr': addr, **version})


def _segment(raw):
    return (raw, deflate_segment(raw))


@router.get('/api/snapshot',
            response_model=Union[models.SnapshotResponse,
                                 models.SnapshotAllResponse],
            responses={400: _ERR, 404: _ERR})
def api_snapshot(request: Request, event: Optional[str] = None, ctx=Ctx):
    """Pull the cached per-event snapshot (one event, or all). Pairs with
    the 'data_version' SSE notify: browsers fetch only the event they're
    viewing."""
    st = ctx.state
    with st.lock:
        per_event = st._cached_per_event
        blobs = st._cached_blobs
        version = st.version_locked()
    # The worker serialized (and deflated) each event once when it changed;
    # a request splices those bytes around its own small envelope instead of
    # re-encoding and re-compressing a multi-megabyte profile every time.
    # The dict path remains for state seeded without blobs.
    if event is not None:
        event, error = _resolve_event_name(event, per_event.keys())
        if error:
            return error
        blob = blobs.get(event)
        if blob is None:
            return _json({'event': event, 'data': per_event[event],
                          'version': version},
                         request=request, allow_gzip=True)
        head = (b'{"event":' + _dumps(event) + b',"version":'
                + _dumps(version) + b',"data":')
        return _spliced([_segment(head), blob, _segment(b'}')], request)
    if per_event and set(blobs) == set(per_event):
        parts = [_segment(b'{"version":' + _dumps(version) + b',"per_event":{')]
        for i, evt in enumerate(sorted(blobs)):
            parts.append(_segment((b',' if i else b'') + _dumps(evt) + b':'))
            parts.append(blobs[evt])
        parts.append(_segment(b'}}'))
        return _spliced(parts, request)
    return _json({'per_event': per_event, 'version': version},
                 request=request, allow_gzip=True)


@router.get('/api/stream')
async def api_stream(request: Request, ctx=Ctx):
    """Server-Sent Events endpoint for real-time updates.

    Events: `status`, `agent`, `data_version` (carries event_types),
    `perf_stat`, `metrics` (discriminated by its `type` field).
    """
    hub = request.app.state.sse_hub

    async def gen():
        q = asyncio.Queue(maxsize=256)
        hub.queues.add(q)
        try:
            # Send current state (small events only — the browser pulls
            # the heavy per-event snapshot from /api/snapshot when it
            # sees the version stamp). `status` and `agent` first: a
            # browser attaching to a running session used to learn the
            # data version but never that an agent was connected.
            st = ctx.state
            session = ctx.agent.current()
            with st.lock:
                perf_stat = dict(st.perf_stat)
                have_data = bool(st._cached_per_event)
                version = st.version_locked(with_events=True)
                connected, addr = st.agent_connected, st.agent_addr
            yield _sse_frame('status', {'connected': connected, 'agent': addr})
            if connected and session is not None and session.connected:
                yield _sse_frame('agent', {
                    'agent': session.addr,
                    'platform': (session.hello or {}).get('platform', {}),
                })
            if have_data:
                yield _sse_frame('data_version', version)
                yield _sse_frame('perf_stat', perf_stat)

            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=1.0)
                    yield msg
                except asyncio.TimeoutError:
                    yield b': keepalive\n\n'
        finally:
            hub.queues.discard(q)

    return StreamingResponse(gen(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
    })


# ---------------------------------------------------------------------------
# Sessions — list / replay / delete / export / import
# ---------------------------------------------------------------------------

def _session_meta_cached(ctx, meta_path):
    """metadata.json as a dict, re-read only when its mtime changes (the
    list used to parse every session's metadata on every call), or None
    when missing or unreadable."""
    try:
        mtime = os.stat(meta_path).st_mtime_ns
    except OSError:
        return None
    hit = ctx.session_meta_cache.get(meta_path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (ValueError, OSError):
        return None
    if not isinstance(meta, dict):
        return None
    ctx.session_meta_cache[meta_path] = (mtime, meta)
    return meta


@router.get('/api/sessions', response_model=models.SessionListResponse)
def api_sessions_list(offset: int = 0, limit: int = 100, ctx=Ctx):
    metas = []
    seen = set()
    if os.path.isdir(ctx.config.sessions_dir):
        for name in sorted(os.listdir(ctx.config.sessions_dir), reverse=True):
            meta_path = os.path.join(ctx.config.sessions_dir, name,
                                     'metadata.json')
            seen.add(meta_path)
            meta = _session_meta_cached(ctx, meta_path)
            if meta is not None:
                metas.append(meta)
    for stale in [k for k in ctx.session_meta_cache if k not in seen]:
        ctx.session_meta_cache.pop(stale, None)
    offset = max(offset, 0)
    limit = max(0, min(limit, 1000))
    return _json({
        'sessions': metas[offset:offset + limit],
        'total': len(metas),
        'offset': offset,
        'limit': limit,
    })


@router.get('/api/sessions/{session_id}',
            response_model=models.SessionReplayResponse,
            responses={404: _ERR})
def api_session_replay(session_id: str, request: Request, ctx=Ctx):
    """Rebuild session data on the fly from raw chunks (cached on disk —
    sessions are immutable once saved)."""
    session_dir = sessions.safe_session_dir(ctx.config, session_id)
    meta_path = (os.path.join(session_dir, 'metadata.json')
                 if session_dir else '')

    if not session_dir or not os.path.isfile(meta_path):
        return _err('not_found', 'session not found', 404)

    try:
        metadata = sessions.read_metadata(session_dir)
    except ValueError as e:
        # A truncated metadata.json used to surface as a traceback
        return _err('bad_metadata', f'session metadata unreadable: {e}', 500)

    # Replay cache key guards against config changes that alter annotation.
    # 'schema' bumps whenever the replay response shape changes, so caches
    # written by older servers regenerate instead of serving stale shapes.
    cache_path = os.path.join(session_dir, 'replay_cache.json.gz')
    cache_key = {
        # 3: frames the target's perf left as [unknown] are now named
        # server-side, which changes the body without changing any input
        # below -- so the bump is what stops stale blobs being served.
        'schema': 3,
        'chunks': len(sessions.session_chunk_files(session_dir)),
        'binary': ctx.config.binary_path,
        'source_dir': ctx.config.source_dir,
        'sysroot': ctx.config.sysroot,
        'inline': ctx.config.inline,
    }
    # One build per session at a time: two tabs replaying the same session
    # used to build it twice and race on the cache file.
    with ctx.replay_lock(session_id):
        per_event = None
        if os.path.isfile(cache_path):
            try:
                with gzip.open(cache_path, 'rt') as f:
                    cached = json.load(f)
                if cached.get('key') == cache_key:
                    per_event = cached.get('per_event')
            except (OSError, ValueError):
                pass

        if per_event is None:
            all_samples = sessions.load_session_chunks(ctx.config, session_dir)
            event_types = get_event_types(all_samples)
            mapper = ctx.state.source_mapper
            per_event = sessions.build_per_event_data(all_samples, event_types,
                                                      mapper, source=True)
            tmp = cache_path + '.tmp'
            try:
                with gzip.open(tmp, 'wt') as f:
                    json.dump({'key': cache_key, 'per_event': per_event}, f)
                os.replace(tmp, cache_path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    result = {'metadata': metadata, 'per_event': per_event}

    metrics_path = os.path.join(session_dir, 'metrics.json')
    if os.path.isfile(metrics_path):
        try:
            with open(metrics_path) as f:
                result['metrics'] = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    return _json(result, request=request, allow_gzip=True)


@router.delete('/api/sessions/{session_id}',
               response_model=models.SessionDeleteResponse,
               responses={404: _ERR, 500: _ERR})
def api_session_delete(session_id: str, ctx=Ctx):
    session_dir = sessions.safe_session_dir(ctx.config, session_id)
    meta_path = os.path.join(session_dir, 'metadata.json') if session_dir else ''
    if not session_dir or not os.path.isfile(meta_path):
        return _err('not_found', 'session not found', 404)
    ctx.session_meta_cache.pop(meta_path, None)
    try:
        shutil.rmtree(session_dir)
    except OSError as e:
        # It used to answer ok after ignore_errors=True left the directory
        return _err('delete_failed', f'could not delete session: {e}', 500)
    return _json({'ok': True, 'session_id': session_id})


def _resolve_event_name(event, available):
    """Resolve a requested event onto one name actually present.

    Returns (name, None) or (None, error response). A hybrid CPU reports
    'cpu_core/cycles/' and 'cpu_atom/cycles/' but never a bare 'cycles', so a
    base name resolves when it is unambiguous; when it is not, the error names
    the candidates rather than leaving the caller to guess.
    """
    matches = resolve_event(event, available)
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, _err('ambiguous_event',
                          f'{event!r} matches several events on this '
                          f'device; ask for one of: '
                          f'{", ".join(sorted(matches))}', 400)
    have = ', '.join(sorted(available)) or 'none yet'
    return None, _err('not_found',
                      f'no data for event: {event} (have: {have})', 404)


def _pick_event(event, available, what):
    """Resolve `event` for a view that renders a single event.

    Named: as `_resolve_event_name`. Omitted: the only event present, or 400
    naming the choices -- summing cycles with cache-misses renders nothing
    meaningful, and quietly picking one is how a view goes plausibly wrong on
    a hybrid CPU.
    """
    if event:
        return _resolve_event_name(event, available)
    if not available:
        return None, _err('no_data', 'no samples yet', 404)
    if len(available) > 1:
        return None, _err('ambiguous_event',
                          f'{what} covers one event and this profile has '
                          f'{len(available)}; ask for one of: '
                          f'{", ".join(available)}', 400)
    return available[0], None


def _attachment(body, media_type, filename):
    return Response(content=body, media_type=media_type,
                    headers={'Content-Disposition':
                             f'attachment; filename="{filename}"'})


def _export_response(ctx, all_samples, metadata, fmt, event, name):
    """Render an export (collapsed / json / svg) for live or session data.

    Every format honours `event`, resolved as /api/snapshot resolves it.
    Collapsed stacks and SVG are one event each — summing cycles with
    cache-misses yields a flame graph of nothing — so when the data holds
    several events the caller must name one. JSON is keyed per event, so
    without `event` it carries them all.
    """
    if fmt not in ('collapsed', 'json', 'svg'):
        return _err('bad_format', f'unknown format: {fmt}', 400)

    available = get_event_types(all_samples)
    if event or fmt != 'json':
        event, error = _pick_event(event, available, f'format={fmt}')
        if error:
            return error

    stem = f'perflens-{name}'
    if event:
        stem += '-' + event.strip('/').replace('/', '-')
    mapper = ctx.state.source_mapper

    if fmt == 'collapsed':
        text = export.export_collapsed(
            filter_samples_by_event(all_samples, event))
        return _attachment(text.encode('utf-8'), 'text/plain',
                           f'{stem}.collapsed')

    if fmt == 'json':
        # build_per_event_data derives the events from the samples it is
        # given and ignores its event list, so narrow the samples instead.
        chosen = (filter_samples_by_event(all_samples, event) if event
                  else all_samples)
        per_event = sessions.build_per_event_data(
            chosen, [event] if event else available, mapper)
        body = json.dumps({'metadata': metadata, 'per_event': per_event},
                          indent=2).encode('utf-8')
        return _attachment(body, 'application/json', f'{stem}.json')

    expanded = (mapper.expand_inline_frames(all_samples)
                if mapper else all_samples)
    evt_samples = filter_samples_by_event(expanded, event)
    fg = build_flamegraph_data(evt_samples)
    svg = export.render_flamegraph_svg(fg, len(evt_samples), event)
    return _attachment(svg.encode('utf-8'), 'image/svg+xml', f'{stem}.svg')


@router.get('/api/sessions/{session_id}/export',
            responses={400: _ERR, 404: _ERR})
def api_session_export(session_id: str, format: str = 'collapsed',
                       event: Optional[str] = None, ctx=Ctx):
    """Export a saved session: collapsed stacks or an SVG flamegraph for one
    event, or JSON for every event unless `event` names one."""
    try:
        all_samples, metadata = sessions.load_session_samples(ctx.config,
                                                              session_id)
    except ValueError as e:
        return _err('bad_metadata', f'session metadata unreadable: {e}', 500)
    if all_samples is None:
        return _err('not_found', 'session not found', 404)
    return _export_response(ctx, all_samples, metadata, format, event,
                            session_id)


@router.get('/api/live/export', responses={400: _ERR, 404: _ERR})
def api_live_export(format: str = 'collapsed', event: Optional[str] = None,
                    ctx=Ctx):
    """Export the live in-memory profile (bounded by --max-samples), in the
    same formats and with the same `event` rules as a session export."""
    with ctx.state.lock:
        all_samples = list(ctx.state.all_samples)
        perf_stat = dict(ctx.state.perf_stat)
    if not all_samples:
        return _err('no_data', 'no live data', 404)
    metadata = {
        'session_id': 'live',
        'total_samples': len(all_samples),
        'event_types': get_event_types(all_samples),
        'perf_stat': perf_stat,
    }
    return _export_response(ctx, all_samples, metadata, format, event, 'live')


@router.post('/api/sessions/import', response_model=models.ImportResponse,
             responses={400: _ERR, 413: _ERR_TOO_LARGE, 500: _ERR})
async def api_sessions_import(request: Request, ctx=Ctx):
    """Import an uploaded perf.data file as a saved session."""
    try:
        content_length = int(request.headers.get('content-length', 0))
    except ValueError:
        content_length = 0
    if content_length <= 0:
        return _err('empty_body', 'empty request body', 400)
    if content_length > sessions.MAX_IMPORT_SIZE:
        return _err('too_large',
                    f'file too large ({content_length} bytes, '
                    f'max {sessions.MAX_IMPORT_SIZE // 1024 // 1024} MB)',
                    413)
    if not ctx.config.perf_bin:
        return _err('no_perf',
                    'perf not found on server — cannot import perf.data', 500)

    tmp = tempfile.NamedTemporaryFile(suffix='.data', delete=False)
    try:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > sessions.MAX_IMPORT_SIZE:
                return _err('too_large', 'file too large', 413)
            # Disk writes off the event loop: up to 500 MB used to be
            # written on it, stalling SSE for every browser meanwhile
            await run_in_threadpool(tmp.write, chunk)
        tmp.close()

        session_id, samples, metadata = await run_in_threadpool(
            sessions.import_perf_data, ctx.config, tmp.name)
        return _json({
            'session_id': session_id,
            'total_samples': len(samples),
            'event_types': metadata['event_types'],
        })
    except RuntimeError as e:
        return _err('import_failed', str(e), 500)
    except Exception as e:
        # Broad on purpose: this is the outermost handler for an
        # arbitrary uploaded perf.data, parsed by a forgiving parser and
        # an external perf binary. Anything that escapes becomes a 500
        # with the reason, rather than a stack trace to the browser.
        return _err('import_failed', f'import failed: {e}', 500)
    finally:
        try:
            tmp.close()
        except OSError:
            pass
        if os.path.isfile(tmp.name):
            os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# Threads / time window / source
# ---------------------------------------------------------------------------

def _cached_view(ctx, key, build):
    """Memoize a view derived from the raw ring.

    These views copy and rescan the whole ring (up to --max-samples
    entries, with inline expansion allocating a dict per sample), and the
    UI refetches them on every chunk and every timeline drag. The ring
    only changes when a chunk lands or the session resets, so the result
    is keyed by (generation, chunk_count) on top of the view's own
    parameters. `build` returns a dict to cache, or an error response,
    which is not cached.
    """
    with ctx.state.lock:
        stamp = (ctx.state.generation, ctx.state.chunk_count)
    full_key = key + stamp
    hit = ctx.views.get(full_key)
    if hit is not None:
        return hit
    result = build()
    if isinstance(result, dict):
        ctx.views.put(full_key, result)
    return result


@router.get('/api/threads', response_model=models.ThreadSummaryResponse,
            responses={400: _ERR, 404: _ERR})
def api_threads(event: Optional[str] = None, ctx=Ctx):
    """Overview of all threads with CPU breakdown, for one event."""
    result = _cached_view(ctx, ('threads', event),
                          lambda: _threads_view(ctx, event))
    return result if isinstance(result, Response) else _json(result)


def _threads_view(ctx, event):
    with ctx.state.lock:
        all_samples = list(ctx.state.all_samples)
    if not all_samples:
        return {'total_samples': 0, 'threads': []}

    event, error = _pick_event(event, get_event_types(all_samples),
                               'the thread overview')
    if error:
        return error
    filtered = filter_samples_by_event(all_samples, event)
    total = len(filtered)

    by_tid = {}
    for s in filtered:
        tid = s.get('tid', s.get('pid', 0))
        if tid not in by_tid:
            by_tid[tid] = {'comm': s.get('comm', ''), 'samples': []}
        by_tid[tid]['samples'].append(s)

    mapper = ctx.state.source_mapper
    threads = []
    for tid, info in sorted(by_tid.items(),
                            key=lambda x: len(x[1]['samples']), reverse=True):
        count = len(info['samples'])
        expanded = (mapper.expand_inline_frames(info['samples'])
                    if mapper else info['samples'])
        func_counts: dict[str, int] = {}
        for s in expanded:
            if s['frames']:
                fn = s['frames'][0]['func']
                func_counts[fn] = func_counts.get(fn, 0) + 1
        top_func = ''
        top_func_samples = 0
        if func_counts:
            top_func = max(func_counts, key=lambda k: func_counts[k])
            top_func_samples = func_counts[top_func]

        top_funcs = sorted(func_counts.items(),
                           key=lambda x: x[1], reverse=True)[:5]
        top_functions = [{'name': fn, 'samples': c,
                          'percent': round(100.0 * c / count, 1)}
                         for fn, c in top_funcs]

        threads.append({
            'tid': tid,
            'comm': info['comm'],
            'samples': count,
            'percent': round(100.0 * count / total, 1),
            'top_function': top_func,
            'top_function_samples': top_func_samples,
            'top_functions': top_functions,
        })

    return {'total_samples': total, 'threads': threads}


@router.get('/api/threads/{tid}', response_model=models.ThreadViewResponse,
            responses={400: _ERR, 404: _ERR})
def api_thread_view(tid: int, request: Request, event: Optional[str] = None,
                    ctx=Ctx):
    """Per-thread flamegraph + summary + source_files, for one event."""
    result = _cached_view(ctx, ('thread', event, tid),
                          lambda: _thread_view(ctx, tid, event))
    if isinstance(result, Response):
        return result
    return _json(result, request=request, allow_gzip=True)


def _thread_view(ctx, tid, event):
    with ctx.state.lock:
        all_samples = list(ctx.state.all_samples)

    filtered = []
    if all_samples:
        event, error = _pick_event(event, get_event_types(all_samples),
                                   'the thread view')
        if error:
            return error
        filtered = [s for s in filter_samples_by_event(all_samples, event)
                    if s.get('tid', s.get('pid', 0)) == tid]

    if not filtered:
        return {'flamegraph': {'name': 'root', 'value': 0, 'children': []},
                'function_summary': {'total_samples': 0, 'functions': []},
                'source_files': []}

    mapper = ctx.state.source_mapper
    expanded = mapper.expand_inline_frames(filtered) if mapper else filtered
    result = {
        'flamegraph': build_flamegraph_data(expanded),
        'function_summary': build_function_summary(expanded),
    }
    if mapper:
        result['source_files'] = mapper.get_files_with_samples(filtered)
    else:
        result['source_files'] = []
    return result


@router.get('/api/window', response_model=models.TimeWindowResponse,
            responses={400: _ERR, 404: _ERR})
def api_window(request: Request, start: float, end: float,
               event: Optional[str] = None, tid: Optional[int] = None,
               ctx=Ctx):
    """Flamegraph + function summary restricted to samples received inside
    [start, end] (unix seconds), for one event. Backs the UI's timeline
    scrubbing: samples are stamped with arrival time, so a window on the
    Device Health timeline maps to the profile chunks collected in that
    window. Bounded by the raw-sample ring buffer (--max-samples)."""
    result = _cached_view(ctx, ('window', event, tid, start, end),
                          lambda: _window_view(ctx, start, end, event, tid))
    if isinstance(result, Response):
        return result
    return _json(result, request=request, allow_gzip=True)


def _window_view(ctx, start, end, event, tid):
    with ctx.state.lock:
        all_samples = list(ctx.state.all_samples)

    filtered = []
    if all_samples:
        event, error = _pick_event(event, get_event_types(all_samples),
                                   'a time window')
        if error:
            return error
        filtered = [s for s in filter_samples_by_event(all_samples, event)
                    if start <= s.get('recv_ts', 0) <= end]
    if tid is not None:
        filtered = [s for s in filtered
                    if s.get('tid', s.get('pid', 0)) == tid]

    window = {'start': start, 'end': end, 'samples': len(filtered)}
    if not filtered:
        return {'flamegraph': {'name': 'root', 'value': 0, 'children': []},
                'function_summary': {'total_samples': 0, 'functions': []},
                'window': window}

    mapper = ctx.state.source_mapper
    expanded = mapper.expand_inline_frames(filtered) if mapper else filtered
    return {
        'flamegraph': build_flamegraph_data(expanded),
        'function_summary': build_function_summary(expanded),
        'window': window,
    }


@router.get('/api/source', response_model=models.SourceResponse,
            responses={400: _ERR, 404: _ERR, 409: _ERR})
def api_source(request: Request, file: str, event: Optional[str] = None,
               tid: Optional[int] = None, ctx=Ctx):
    """Return annotated source for a specific file. Optional tid filter."""
    mapper = ctx.state.source_mapper
    if not mapper:
        return _err('no_mapper', 'source mapper not available', 409)
    result = _cached_view(ctx, ('source', file, event, tid),
                          lambda: _source_view(ctx, mapper, file, event, tid))
    if isinstance(result, Response):
        return result
    return _json(result, request=request, allow_gzip=True)


def _source_view(ctx, mapper, file, event, tid):
    with ctx.state.lock:
        all_samples = list(ctx.state.all_samples)

    if all_samples:
        event, error = _pick_event(event, get_event_types(all_samples),
                                   'source annotation')
        if error:
            return error
        all_samples = filter_samples_by_event(all_samples, event)

    if tid is not None:
        all_samples = [s for s in all_samples
                       if s.get('tid', s.get('pid', 0)) == tid]

    line_data = mapper.map_samples_to_lines(all_samples)

    if file in line_data:
        lines = mapper.annotate_source(file, line_data[file])
        return {'file': file, 'lines': lines}
    return _err('not_found', f'no data for file: {file}', 404)


# ---------------------------------------------------------------------------
# Index / metrics / browse / wizard
# ---------------------------------------------------------------------------

def _symbolization_status(ctx):
    """Frame-naming outcome for /api/index/status.

    Counted after server-side resolution, so `unknown_frames` is what is
    still unnamed once the server has done what it can.
    """
    mapper = ctx.state.source_mapper
    st = (mapper.symbolization_stats() if mapper else
          {'userspace_frames': 0, 'unknown_frames': 0, 'resolved_frames': 0})
    total = st['userspace_frames']
    unknown = st['unknown_frames']
    resolved = st['resolved_frames']
    named = total - unknown

    if not total:
        mode, detail = 'idle', ''
    elif unknown == 0:
        mode = 'server' if resolved else 'device'
        detail = (f'{resolved:,} frames named here from their address'
                  if resolved else '')
    elif named == 0:
        mode = 'degraded'
        detail = ("This target's perf cannot resolve userspace symbols. "
                  "Point --binary at the matching unstripped build "
                  "(--module-map for shared objects) to name them here.")
    else:
        mode = 'server' if resolved else 'degraded'
        detail = (f'{unknown:,} of {total:,} userspace frames unnamed '
                  f'— supply the matching unstripped binary to resolve them.')

    return {
        'userspace_frames': total,
        'unknown_frames': unknown,
        'resolved_frames': resolved,
        'named_pct': round(named * 100.0 / total, 1) if total else 100.0,
        'mode': mode,
        'detail': detail,
    }


@router.get('/api/index/status', response_model=models.IndexStatus)
def api_index_status(ctx=Ctx):
    mapper = ctx.state.source_mapper
    if mapper:
        status = mapper.get_index_status()
        status['symbolization'] = _symbolization_status(ctx)
        return _json(status)
    # Same keys as get_index_status(), so a client never has to tell a
    # missing mapper apart from an unindexed one by which fields exist.
    return _json({'indexing': False, 'symbols_loaded': 0,
                  'source_files_found': 0, 'source_index_ready': False,
                  'source_index_files': 0, 'dwarf_total': 0,
                  'dwarf_source_files': [], 'dwarf_truncated': False,
                  'symbolization': _symbolization_status(ctx)})


@router.get('/api/index/files', response_model=models.IndexFilesResponse)
def api_index_files(request: Request, offset: int = 0, limit: int = 200,
                    q: str = '', ctx=Ctx):
    mapper = ctx.state.source_mapper
    if mapper:
        return _json(mapper.list_dwarf_files(offset, limit, q),
                     request=request, allow_gzip=True)
    return _json({'total': 0, 'offset': 0, 'limit': limit, 'files': []})


@router.get('/api/metrics/current',
            response_model=dict[str, models.MetricsFrame])
def api_metrics_current(ctx=Ctx):
    return _json(ctx.metrics.get_latest())


@router.get('/api/metrics/history',
            response_model=list[models.MetricsFrame])
def api_metrics_history(type: str = 'system', start: Optional[float] = None,
                        end: Optional[float] = None, ctx=Ctx):
    return _json(ctx.metrics.get_history(type, start, end))


@router.get('/api/browse', response_model=models.BrowseResponse,
            responses={400: _ERR, 403: _ERR})
def api_browse(path: str = '/', ctx=Ctx):
    """Browse the server filesystem for the wizard's binary/source pickers.
    Confined to config.browse_root."""
    root = os.path.realpath(ctx.config.browse_root or os.path.expanduser('~'))
    browse_path = os.path.realpath(path or root)
    if browse_path != root and not browse_path.startswith(root + os.sep):
        # Outside the allowed root — start the picker at the root
        # instead of erroring, so the UI stays usable.
        browse_path = root
    if not os.path.isdir(browse_path):
        return _err('not_directory', f'not a directory: {browse_path}', 400)

    entries = []
    try:
        for name in sorted(os.listdir(browse_path)):
            full = os.path.join(browse_path, name)
            is_dir = os.path.isdir(full)
            entry = {'name': name, 'path': full, 'is_dir': is_dir}
            if not is_dir:
                try:
                    entry['size'] = os.path.getsize(full)
                except OSError:
                    entry['size'] = 0
            entries.append(entry)
    except PermissionError:
        return _err('forbidden', f'permission denied: {browse_path}', 403)

    return _json({
        'path': browse_path,
        'parent': os.path.dirname(browse_path),
        'entries': entries[:500],  # cap at 500 entries
    })


@router.get('/api/wizard', response_model=models.WizardState)
def api_wizard_get(ctx=Ctx):
    return _json(ctx.wizard)


@router.put('/api/wizard', response_model=models.WizardState)
async def api_wizard_put(body: dict, ctx=Ctx):
    return _json(ctx.update_wizard(body))


# ---------------------------------------------------------------------------
# Agent control
# ---------------------------------------------------------------------------

@router.get('/api/agent', response_model=models.AgentInfo)
def api_agent_info(ctx=Ctx):
    """Current agent connection: address + hello (platform, version)."""
    session = ctx.agent.current()
    if not session or not session.connected:
        return _json({'connected': False, 'addr': None, 'hello': None})
    return _json({'connected': True, 'addr': session.addr,
                  'hello': session.hello})


@router.delete('/api/agent', response_model=models.StopResponse)
def api_agent_disconnect(ctx=Ctx):
    """Close the agent connection, triggering normal disconnect flow.

    This ends the current session and saves it. It is not a way to keep
    an agent away: one started with `--server` dials back in within
    seconds by design, so for that deployment this reads as a session
    reset. Stop the agent process on the device to make it stick.
    """
    return _json(agentlink.stop_agent(ctx))


@router.post('/api/agent/connect', response_model=models.ConnectResponse,
             responses={400: _ERR, 502: _ERR})
async def api_agent_connect(body: models.ConnectRequest, ctx=Ctx):
    """Connect to a listen-mode agent."""
    host = body.host.strip()
    if not host:
        return _err('validation', 'host required', 400)

    try:
        session = await run_in_threadpool(agentlink.connect_to_agent,
                                          ctx, host, body.port, 10, body.token)
        # The pairing code is deliberately not persisted into the wizard
        # state: it is written to disk and served back over the API, and the
        # code rotates on every agent restart anyway.
        ctx.update_wizard({
            'agent_host': host,
            'agent_port': body.port,
            'connected': True,
        })
        return _json({
            'ok': True,
            'hello': session.hello,
            'addr': session.addr,
        })
    except RuntimeError as e:
        return _err('agent_unreachable', str(e), 502)


# Transport-level failures from AgentSession.send_command — everything
# else in its response came from the agent itself and passes through.
_TRANSPORT_ERRORS = ('send failed', 'command timed out', 'no response',
                     'agent disconnected')


@router.post('/api/agent/command',
             response_model=models.AgentCommandResponse,
             responses={409: _ERR, 502: _ERR})
async def api_agent_command(body: models.AgentCommandRequest, ctx=Ctx):
    """Relay a command to the managed agent."""
    session = ctx.agent.current()
    if not session or not session.connected:
        return _err('no_agent', 'no managed agent connected', 409)

    # list_processes and reprobe can take longer
    timeout = body.timeout
    if body.cmd in ('list_processes', 'reprobe', 'start'):
        timeout = max(timeout, 120)

    resp = await run_in_threadpool(session.send_command, body.cmd,
                                   body.args, timeout=timeout)
    error = resp.get('error') or ''
    if not resp.get('ok', True) and error.startswith(_TRANSPORT_ERRORS):
        return _err('agent_transport', error, 502)
    if (body.cmd == 'verify_perf' and resp.get('available')
            and resp.get('version')):
        # A perf chosen at runtime changes what the hello reported at connect
        # time, and /api/agent and the device strip both read the hello.
        hello = session.hello or {}
        platform = hello.get('platform')
        if isinstance(platform, dict):
            # A new dict: the old one is being serialized by other threads
            platform = {**platform, 'perf_version': resp['version']}
            session.hello = {**hello, 'platform': platform}
            ctx.broadcast('agent', {'agent': session.addr,
                                    'platform': platform})
    return _json(resp)


# ---------------------------------------------------------------------------
# Config — unified GET + PATCH
# ---------------------------------------------------------------------------

def _reload_mapper(ctx, pre_index=False):
    """Swap in a fresh SourceMapper built from the (mutated) config, and
    close the old one -- its addr2line children, sqlite handle and index
    thread used to leak on every PATCH."""
    old = ctx.state.source_mapper
    mapper = create_source_mapper(ctx.config)
    ctx.state.source_mapper = mapper
    if old is not None:
        old.close()
    if pre_index:
        # Pre-index symbols and DWARF source files in background
        threading.Thread(target=mapper.pre_index, daemon=True).start()
    return mapper


def _config_state(ctx):
    cfg = ctx.config
    return {
        'binary': cfg.binary_path,
        'source_dir': cfg.source_dir,
        'path_map': cfg.path_map,
        'module_map': cfg.module_map,
        'addr2line': cfg.addr2line_bin,
        'readelf': cfg.readelf_bin,
        'sysroot': cfg.sysroot,
        'inline': cfg.inline,
    }


def _config_patch_impl(ctx, body: models.ConfigUpdate):
    cfg = ctx.config
    pre_index = False

    if body.binary is not None:
        path = body.binary.strip()
        if not path:
            cfg.binary_path = None
        else:
            path = os.path.abspath(path)
            if not os.path.isfile(path):
                return _err('bad_path', f'file not found: {path}', 400)
            cfg.binary_path = path
            ctx.update_wizard({'binary_path': path})
            pre_index = True

    if body.source_dir is not None:
        path = body.source_dir.strip()
        if not path:
            return _err('bad_path', 'source_dir required', 400)
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            return _err('bad_path', f'directory not found: {path}', 400)
        cfg.source_dir = path
        ctx.update_wizard({'source_dir': path})
        pre_index = True

    if body.path_map is not None:
        cfg.path_map = body.path_map or None

    if body.module_map is not None:
        cfg.module_map = body.module_map or None

    if body.toolchain_prefix is not None:
        prefix = body.toolchain_prefix.strip()
        if prefix:
            a2l = prefix + 'addr2line'
            rel = prefix + 'readelf'
            # Verify at least addr2line exists
            found = os.path.isfile(a2l)
            if not found:
                resolved = shutil.which(a2l)
                if resolved:
                    found = True
                    a2l = resolved
                    rel = shutil.which(rel) or rel
            if not found:
                return _err('bad_path', f'addr2line not found: {a2l}', 400)
            cfg.addr2line_bin = a2l
            cfg.readelf_bin = rel
            pre_index = pre_index or bool(cfg.binary_path)

    if body.sysroot is not None:
        sysroot = body.sysroot.strip()
        if sysroot:
            sysroot = os.path.abspath(sysroot)
            if not os.path.isdir(sysroot):
                return _err('bad_path', f'sysroot not found: {sysroot}', 400)
            cfg.sysroot = sysroot
        else:
            cfg.sysroot = None

    _reload_mapper(ctx, pre_index=pre_index)
    return _json(_config_state(ctx))


@router.get('/api/config', response_model=models.ConfigState)
def api_config_get(ctx=Ctx):
    return _json(_config_state(ctx))


@router.patch('/api/config', response_model=models.ConfigState,
              responses={400: _ERR})
async def api_config_patch(body: models.ConfigUpdate, ctx=Ctx):
    """Update binary / source dir / path map / module map / toolchain /
    sysroot in one request; the source mapper rebuilds once."""
    # Mapper recreation touches disk (persisted index) — threadpool; and
    # one at a time, so two PATCHes cannot each swap in a mapper.
    def patch():
        with ctx.config_lock:
            return _config_patch_impl(ctx, body)
    return await run_in_threadpool(patch)


# ---------------------------------------------------------------------------
# Error envelope — every failure renders as {"error": {code, message}}
# ---------------------------------------------------------------------------

async def _validation_exc_handler(request, exc: RequestValidationError):
    errs = exc.errors()
    if errs:
        e = errs[0]
        loc = '.'.join(str(x) for x in e.get('loc', ())
                       if x not in ('body', 'query', 'path'))
        message = (f"{loc}: {e.get('msg', 'invalid request')}"
                   if loc else e.get('msg', 'invalid request'))
    else:
        message = 'invalid request'
    return _err('validation', message, 400)


async def _http_exc_handler(request, exc: StarletteHTTPException):
    code = {403: 'forbidden', 404: 'not_found',
            405: 'method_not_allowed'}.get(exc.status_code, 'http_error')
    return _err(code, str(exc.detail), exc.status_code)


# ---------------------------------------------------------------------------
# App factory + runner
# ---------------------------------------------------------------------------

_UI_MISSING_PAGE = """<!DOCTYPE html>
<html><head><title>PerfLens — UI not built</title></head>
<body style="font-family: system-ui; max-width: 40em; margin: 4em auto;">
<h1>PerfLens server is running</h1>
<p>The web UI assets were not found. If you are running from a source
checkout, build them first:</p>
<pre>npm --prefix frontend ci
npm --prefix frontend run build</pre>
<p>Installed wheels ship the UI prebuilt — <code>pip install perflens</code>
or <code>uvx perflens</code> need no extra step.</p>
<p>The HTTP API is fully functional: try <a href="/api/status">/api/status</a>.</p>
</body></html>"""


@asynccontextmanager
async def _lifespan(app):
    app.state.sse_hub.attach(asyncio.get_running_loop())
    app.state.ctx.register_sse_sink(app.state.sse_hub.publish)
    yield
    # Shutdown: end the agent session and wait for its metadata to be
    # written. The receive and save threads are daemons, so a Ctrl-C mid-
    # capture used to leave the session's directory without metadata --
    # unlisted, unreplayable, and never cleaned up.
    await run_in_threadpool(agentlink.shutdown_agent, app.state.ctx)
    mapper = app.state.ctx.state.source_mapper
    if mapper is not None:
        await run_in_threadpool(mapper.close)


def create_app(ctx):
    """Build the ASGI app around an AppContext."""
    from perflens import __version__
    app = FastAPI(title='PerfLens', version=__version__, lifespan=_lifespan,
                  docs_url=None, redoc_url=None,
                  openapi_url='/api/openapi.json')
    app.state.ctx = ctx
    app.state.sse_hub = _SSEHub()
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, _validation_exc_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exc_handler)

    # Static UI last — API routes take precedence. StaticFiles owns path
    # normalization/traversal safety.
    ui_dir = ctx.config.ui_dir
    if ui_dir and os.path.isfile(os.path.join(ui_dir, 'index.html')):
        app.mount('/', StaticFiles(directory=ui_dir, html=True), name='ui')
    else:
        # Source checkout without built frontend assets — say what's
        # missing at /, and let every other path 404 exactly as the static
        # mount would (the SPA deep-links via URL hash, not paths). Out of
        # the schema: a dev-only placeholder isn't API surface, and hiding
        # it keeps the export identical to the assets-present build.
        @app.get('/', include_in_schema=False)
        def ui_missing():
            return HTMLResponse(_UI_MISSING_PAGE, status_code=503)

    return app


def run_http_server(ctx):
    """Run the HTTP server for the web UI (blocks until shutdown)."""
    app = create_app(ctx)
    bind = ctx.config.http_bind
    port = ctx.config.http_port
    print(f"[server] HTTP server on http://{bind}:{port}", file=sys.stderr)
    if bind not in ('127.0.0.1', 'localhost', '::1'):
        print("[server] WARNING: web UI is exposed beyond localhost "
              f"(bound to {bind}) and has no authentication — anyone who "
              "can reach it can browse files under --browse-root and "
              "control the agent", file=sys.stderr)
    # uvicorn logs access on response completion, so the never-ending
    # /api/stream SSE response is naturally excluded (matching the old
    # handler's log filter).
    uvicorn.run(app, host=bind, port=port, log_level='info')
