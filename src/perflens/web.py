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
from typing import TYPE_CHECKING

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from perflens import agentlink, export, sessions
from perflens.api import models
from perflens.api.responses import dumps as _dumps
from perflens.api.responses import json_response as _json
from perflens.config import create_source_mapper
from perflens.parser import (build_flamegraph_data, build_function_summary,
                             filter_samples_by_event, get_event_types)

if TYPE_CHECKING:
    from perflens.app import AppContext

router = APIRouter()


def get_ctx(request: Request) -> 'AppContext':
    return request.app.state.ctx


# FastAPI dependency used by every route
Ctx = Depends(get_ctx)


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
# Core API — status / data / stream / stop
# ---------------------------------------------------------------------------

@router.get('/api/status', response_model=models.Status)
def api_status(ctx=Ctx):
    st = ctx.state
    return _json({
        'status': 'ok',
        'agent_connected': st.agent_connected,
        'agent_addr': st.agent_addr,
        'total_samples': len(st.all_samples),
        'chunk_count': st.chunk_count,
    })


@router.get('/api/per-event', response_model=models.PerEventResponse,
            responses={404: {'model': models.ErrorResponse}})
def api_per_event(request: Request, ctx=Ctx):
    """Pull the cached per-event snapshot (one event, or all). Pairs with
    the 'data_version' SSE notify: browsers fetch only the event they're
    viewing."""
    event = request.query_params.get('event')
    st = ctx.state
    with st.lock:
        per_event = st._cached_per_event
        version = {
            'chunk_count': st.chunk_count,
            'total_samples': len(st.all_samples),
        }
    if event is not None:
        entry = per_event.get(event)
        if entry is None:
            return _json({'error': f'no data for event: {event}',
                          'version': version}, 404)
        return _json({'event': event, 'data': entry, 'version': version},
                     request=request, allow_gzip=True)
    return _json({'per_event': per_event, 'version': version},
                 request=request, allow_gzip=True)


@router.get('/api/stop', response_model=models.StopResponse)
def api_stop(ctx=Ctx):
    """Close the agent connection, triggering normal disconnect flow."""
    return _json(agentlink.stop_agent(ctx))


@router.get('/api/stream')
async def api_stream(request: Request, ctx=Ctx):
    """Server-Sent Events endpoint for real-time updates."""
    hub = request.app.state.sse_hub

    async def gen():
        q = asyncio.Queue(maxsize=256)
        hub.queues.add(q)
        try:
            # Send current state (small events only — the browser pulls
            # the heavy per-event snapshot from /api/per-event when it
            # sees the version stamp)
            st = ctx.state
            with st.lock:
                event_types = list(st.event_types)
                perf_stat = dict(st.perf_stat)
                have_data = bool(st._cached_per_event)
                version = {
                    'chunk_count': st.chunk_count,
                    'total_samples': len(st.all_samples),
                    'event_types': event_types,
                }
            if have_data:
                yield _sse_frame('event_types', event_types)
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
        'Access-Control-Allow-Origin': '*',
        'X-Accel-Buffering': 'no',
    })


# ---------------------------------------------------------------------------
# Sessions — list / replay / export
# ---------------------------------------------------------------------------

@router.get('/api/sessions', response_model=list[models.SessionMetadata])
def api_sessions_list(ctx=Ctx):
    result = []
    if os.path.isdir(ctx.config.sessions_dir):
        for name in sorted(os.listdir(ctx.config.sessions_dir), reverse=True):
            meta_path = os.path.join(ctx.config.sessions_dir, name,
                                     'metadata.json')
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                    result.append(meta)
                except (json.JSONDecodeError, IOError):
                    pass
    return _json(result)


@router.get('/api/sessions/{session_id}',
            response_model=models.SessionReplayResponse)
def api_session_replay(session_id: str, request: Request, ctx=Ctx):
    """Rebuild session data on the fly from raw chunks (cached on disk —
    sessions are immutable once saved)."""
    session_dir = sessions.safe_session_dir(ctx.config, session_id)
    meta_path = (os.path.join(session_dir, 'metadata.json')
                 if session_dir else '')

    if not session_dir or not os.path.isfile(meta_path):
        return _json({'error': 'session not found'})

    with open(meta_path) as f:
        metadata = json.load(f)

    # Replay cache key guards against config changes that alter annotation
    cache_path = os.path.join(session_dir, 'replay_cache.json.gz')
    cache_key = {
        'chunks': len(sessions.session_chunk_files(session_dir)),
        'binary': ctx.config.binary_path,
        'source_dir': ctx.config.source_dir,
        'sysroot': ctx.config.sysroot,
        'inline': ctx.config.inline,
    }
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
        try:
            with gzip.open(cache_path, 'wt') as f:
                json.dump({'key': cache_key, 'per_event': per_event}, f)
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


@router.get('/api/export/flamegraph')
def api_export_flamegraph(request: Request, ctx=Ctx):
    """Export flamegraph as standalone SVG. Uses session data if provided."""
    event_type = request.query_params.get('event', 'cycles')
    session_id = request.query_params.get('session')
    all_samples = None

    if session_id and session_id != 'live':
        all_samples, _ = sessions.load_session_samples(ctx.config, session_id)

    if not all_samples:
        with ctx.state.lock:
            all_samples = list(ctx.state.all_samples)

    if not all_samples:
        return _json({'error': 'no data available'})

    mapper = ctx.state.source_mapper
    expanded = mapper.expand_inline_frames(all_samples) if mapper else all_samples
    evt_samples = filter_samples_by_event(expanded, event_type)
    if not evt_samples:
        return _json({'error': f'no samples for event {event_type}'})

    fg = build_flamegraph_data(evt_samples)
    svg = export.render_flamegraph_svg(fg, len(evt_samples), event_type)

    fname = f'perflens-flamegraph-{event_type}.svg'
    return Response(content=svg.encode('utf-8'), media_type='image/svg+xml',
                    headers={'Content-Disposition':
                             f'attachment; filename="{fname}"'})


@router.get('/api/export/session/{session_id}')
def api_export_session(session_id: str, request: Request, ctx=Ctx):
    """Export session in collapsed or JSON format."""
    fmt = request.query_params.get('format', 'collapsed')
    all_samples, metadata = sessions.load_session_samples(ctx.config,
                                                          session_id)
    if all_samples is None:
        if session_id == 'live':
            with ctx.state.lock:
                all_samples = list(ctx.state.all_samples)
                perf_stat = dict(ctx.state.perf_stat)
            if not all_samples:
                return _json({'error': 'no live data'})
            event_types = get_event_types(all_samples)
            metadata = {
                'session_id': 'live',
                'total_samples': len(all_samples),
                'event_types': event_types,
                'perf_stat': perf_stat,
            }
        else:
            return _json({'error': 'session not found'})

    if fmt == 'collapsed':
        text = export.export_collapsed(all_samples)
        fname = f'perflens-{session_id}.collapsed'
        return Response(content=text.encode('utf-8'), media_type='text/plain',
                        headers={'Content-Disposition':
                                 f'attachment; filename="{fname}"'})

    if fmt == 'json':
        event_types = get_event_types(all_samples)
        mapper = ctx.state.source_mapper
        per_event = sessions.build_per_event_data(all_samples, event_types,
                                                  mapper)
        body = json.dumps({'metadata': metadata, 'per_event': per_event},
                          indent=2).encode('utf-8')
        fname = f'perflens-{session_id}.json'
        return Response(content=body, media_type='application/json',
                        headers={'Content-Disposition':
                                 f'attachment; filename="{fname}"'})

    return _json({'error': f'unknown format: {fmt}'})


# ---------------------------------------------------------------------------
# Threads / source
# ---------------------------------------------------------------------------

@router.get('/api/thread-view', response_model=models.ThreadViewResponse)
def api_thread_view(request: Request, ctx=Ctx):
    """Per-thread flamegraph + summary + source_files."""
    event_type = request.query_params.get('event', 'cycles')
    tid_str = request.query_params.get('tid')
    if not tid_str:
        return _json({'error': 'tid parameter required'}, 400)
    try:
        tid = int(tid_str)
    except ValueError:
        return _json({'error': 'invalid tid'}, 400)

    with ctx.state.lock:
        all_samples = list(ctx.state.all_samples)

    filtered = filter_samples_by_event(all_samples, event_type)
    filtered = [s for s in filtered
                if s.get('tid', s.get('pid', 0)) == tid]

    if not filtered:
        return _json({'flamegraph': {'name': 'root', 'value': 0,
                                     'children': []},
                      'function_summary': {'total_samples': 0,
                                           'functions': []},
                      'source_files': []})

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
    return _json(result, request=request, allow_gzip=True)


@router.get('/api/time-window', response_model=models.TimeWindowResponse)
def api_time_window(request: Request, ctx=Ctx):
    """Flamegraph + function summary restricted to samples received inside
    [start, end] (unix seconds). Backs the UI's timeline scrubbing: samples
    are stamped with arrival time, so a window on the Device Health
    timeline maps to the profile chunks collected in that window. Bounded
    by the raw-sample ring buffer (--max-samples)."""
    params = request.query_params
    event_type = params.get('event', 'cycles')
    try:
        start = float(params.get('start', ''))
        end = float(params.get('end', ''))
    except ValueError:
        return _json({'error': 'start/end required (unix seconds)'}, 400)
    tid = None
    if params.get('tid'):
        try:
            tid = int(params.get('tid'))
        except ValueError:
            return _json({'error': 'invalid tid'}, 400)

    with ctx.state.lock:
        all_samples = list(ctx.state.all_samples)

    filtered = filter_samples_by_event(all_samples, event_type)
    filtered = [s for s in filtered
                if start <= s.get('recv_ts', 0) <= end]
    if tid is not None:
        filtered = [s for s in filtered
                    if s.get('tid', s.get('pid', 0)) == tid]

    window = {'start': start, 'end': end, 'samples': len(filtered)}
    if not filtered:
        return _json({'flamegraph': {'name': 'root', 'value': 0,
                                     'children': []},
                      'function_summary': {'total_samples': 0,
                                           'functions': []},
                      'window': window})

    mapper = ctx.state.source_mapper
    expanded = mapper.expand_inline_frames(filtered) if mapper else filtered
    return _json({
        'flamegraph': build_flamegraph_data(expanded),
        'function_summary': build_function_summary(expanded),
        'window': window,
    }, request=request, allow_gzip=True)


@router.get('/api/thread-summary', response_model=models.ThreadSummaryResponse)
def api_thread_summary(request: Request, ctx=Ctx):
    """Overview of all threads with CPU breakdown."""
    event_type = request.query_params.get('event', 'cycles')
    with ctx.state.lock:
        all_samples = list(ctx.state.all_samples)

    filtered = filter_samples_by_event(all_samples, event_type)
    total = len(filtered)
    if total == 0:
        return _json({'total_samples': 0, 'threads': []})

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
        func_counts = {}
        for s in expanded:
            if s['frames']:
                fn = s['frames'][0]['func']
                func_counts[fn] = func_counts.get(fn, 0) + 1
        top_func = ''
        top_func_samples = 0
        if func_counts:
            top_func = max(func_counts, key=func_counts.get)
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

    return _json({'total_samples': total, 'threads': threads})


@router.get('/api/source', response_model=models.SourceResponse)
def api_source(request: Request, ctx=Ctx):
    """Return annotated source for a specific file. Optional tid filter."""
    file_path = request.query_params.get('file')
    if not file_path:
        return _json({'error': 'file parameter required'})

    event_type = request.query_params.get('event')
    tid_str = request.query_params.get('tid')

    mapper = ctx.state.source_mapper
    if not mapper:
        return _json({'file': file_path, 'lines': [],
                      'error': 'source mapper not available'})

    with ctx.state.lock:
        all_samples = list(ctx.state.all_samples)

    if event_type:
        all_samples = filter_samples_by_event(all_samples, event_type)

    if tid_str:
        try:
            tid = int(tid_str)
            all_samples = [s for s in all_samples
                           if s.get('tid', s.get('pid', 0)) == tid]
        except ValueError:
            pass

    line_data = mapper.map_samples_to_lines(all_samples)

    if file_path in line_data:
        lines = mapper.annotate_source(file_path, line_data[file_path])
        return _json({'file': file_path, 'lines': lines},
                     request=request, allow_gzip=True)
    return _json({'file': file_path, 'lines': [],
                  'error': 'no data for file'})


# ---------------------------------------------------------------------------
# Index / metrics / browse / wizard state
# ---------------------------------------------------------------------------

@router.get('/api/index/status', response_model=models.IndexStatus)
def api_index_status(ctx=Ctx):
    mapper = ctx.state.source_mapper
    if mapper:
        return _json(mapper.get_index_status())
    return _json({'indexing': False, 'symbols_loaded': 0,
                  'source_files_found': 0})


@router.get('/api/index/files', response_model=models.IndexFilesResponse)
def api_index_files(request: Request, ctx=Ctx):
    mapper = ctx.state.source_mapper
    params = request.query_params
    try:
        offset = int(params.get('offset', '0'))
        limit = int(params.get('limit', '200'))
    except ValueError:
        return _json({'error': 'bad offset/limit'}, 400)
    query = params.get('q', '')
    if mapper:
        return _json(mapper.list_dwarf_files(offset, limit, query),
                     request=request, allow_gzip=True)
    return _json({'total': 0, 'offset': 0, 'limit': limit, 'files': []})


@router.get('/api/metrics/current',
            response_model=dict[str, models.MetricsFrame])
def api_metrics_current(ctx=Ctx):
    return _json(ctx.metrics.get_latest())


@router.get('/api/metrics/history',
            response_model=list[models.MetricsFrame])
def api_metrics_history(request: Request, ctx=Ctx):
    params = request.query_params
    mtype = params.get('type', 'system')
    start = params.get('start')
    end = params.get('end')
    try:
        start_ts = float(start) if start else None
        end_ts = float(end) if end else None
    except ValueError:
        return _json({'error': 'bad start/end'}, 400)
    return _json(ctx.metrics.get_history(mtype, start_ts, end_ts))


@router.get('/api/browse', response_model=models.BrowseResponse)
def api_browse(request: Request, ctx=Ctx):
    """Browse the server filesystem for the wizard's binary/source pickers.
    Confined to config.browse_root."""
    browse_path = request.query_params.get('path', '/')
    root = os.path.realpath(ctx.config.browse_root or os.path.expanduser('~'))
    browse_path = os.path.realpath(browse_path or root)
    if browse_path != root and not browse_path.startswith(root + os.sep):
        # Outside the allowed root — start the picker at the root
        # instead of erroring, so the UI stays usable.
        browse_path = root
    if not os.path.isdir(browse_path):
        return _json({'error': f'not a directory: {browse_path}'}, 400)

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
        return _json({'error': f'permission denied: {browse_path}'}, 403)

    return _json({
        'path': browse_path,
        'parent': os.path.dirname(browse_path),
        'entries': entries[:500],  # cap at 500 entries
    })


@router.get('/api/wizard/state', response_model=models.WizardState)
def api_wizard_state(ctx=Ctx):
    return _json(ctx.wizard)


@router.post('/api/wizard/state', response_model=models.WizardState)
async def api_wizard_state_update(body: dict, ctx=Ctx):
    return _json(ctx.update_wizard(body))


# ---------------------------------------------------------------------------
# Agent control
# ---------------------------------------------------------------------------

@router.post('/api/connect', response_model=models.ConnectResponse)
async def api_connect(body: models.ConnectRequest, ctx=Ctx):
    """Connect to a listen-mode agent."""
    host = body.host.strip()
    if not host:
        return _json({'error': 'host required'}, 400)

    try:
        session = await run_in_threadpool(agentlink.connect_to_agent,
                                          ctx, host, body.port)
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
        return _json({'ok': False, 'error': str(e)}, 500)


@router.post('/api/agent/command',
             response_model=models.AgentCommandResponse)
async def api_agent_command(body: models.AgentCommandRequest, ctx=Ctx):
    """Relay a command to the managed agent."""
    session = ctx.agent.current()
    if not session or not session.connected:
        return _json({'error': 'no managed agent connected'}, 400)

    if not body.cmd:
        return _json({'error': 'cmd required'}, 400)

    # list_processes and reprobe can take longer
    timeout = body.timeout
    if body.cmd in ('list_processes', 'reprobe', 'start'):
        timeout = max(timeout, 120)

    resp = await run_in_threadpool(session.send_command, body.cmd,
                                   body.args, timeout=timeout)
    return _json(resp)


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

def _reload_mapper(ctx, pre_index=False):
    """Swap in a fresh SourceMapper built from the (mutated) config."""
    mapper = create_source_mapper(ctx.config)
    ctx.state.source_mapper = mapper
    if pre_index:
        # Pre-index symbols and DWARF source files in background
        threading.Thread(target=mapper.pre_index, daemon=True).start()
    return mapper


def _config_binary_impl(ctx, body: models.PathConfigRequest):
    path = body.path.strip()
    if not path:
        ctx.config.binary_path = None
        return _json({'ok': True, 'path': None})

    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return _json({'ok': False, 'error': f'file not found: {path}'}, 400)

    ctx.config.binary_path = path
    _reload_mapper(ctx, pre_index=True)
    ctx.update_wizard({'binary_path': path})
    return _json({'ok': True, 'path': path})


def _config_source_impl(ctx, body: models.PathConfigRequest):
    path = body.path.strip()
    if not path:
        return _json({'ok': False, 'error': 'path required'}, 400)

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return _json({'ok': False,
                      'error': f'directory not found: {path}'}, 400)

    ctx.config.source_dir = path
    _reload_mapper(ctx, pre_index=True)
    ctx.update_wizard({'source_dir': path})
    return _json({'ok': True, 'path': path})


def _config_pathmap_impl(ctx, body: models.PathMapConfigRequest):
    path_map = body.path_map
    ctx.config.path_map = path_map if path_map else None
    _reload_mapper(ctx)
    return _json({'ok': True, 'path_map': path_map})


def _config_toolchain_impl(ctx, body: models.ToolchainConfigRequest):
    prefix = body.prefix.strip()
    sysroot = (body.sysroot or '').strip()

    result = {'ok': True}

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
            return _json({'ok': False,
                          'error': f'addr2line not found: {a2l}'}, 400)
        ctx.config.addr2line_bin = a2l
        ctx.config.readelf_bin = rel
        result['addr2line'] = a2l
        result['readelf'] = rel

    if sysroot:
        sysroot = os.path.abspath(sysroot)
        if not os.path.isdir(sysroot):
            return _json({'ok': False,
                          'error': f'sysroot not found: {sysroot}'}, 400)
        ctx.config.sysroot = sysroot
        result['sysroot'] = sysroot
    elif body.sysroot is not None:
        # Explicit empty sysroot clears it; absent leaves it unchanged
        ctx.config.sysroot = None

    _reload_mapper(ctx, pre_index=bool(ctx.config.binary_path))
    return _json(result)


def _make_config_route(path, impl, body_model):
    @router.post(path, response_model=models.ConfigResult)
    async def config_route(body: body_model, ctx=Ctx):
        # Mapper recreation touches disk (persisted index) — threadpool
        return await run_in_threadpool(impl, ctx, body)


_make_config_route('/api/config/binary', _config_binary_impl,
                   models.PathConfigRequest)
_make_config_route('/api/config/source', _config_source_impl,
                   models.PathConfigRequest)
_make_config_route('/api/config/pathmap', _config_pathmap_impl,
                   models.PathMapConfigRequest)
_make_config_route('/api/config/toolchain', _config_toolchain_impl,
                   models.ToolchainConfigRequest)


# ---------------------------------------------------------------------------
# perf.data import
# ---------------------------------------------------------------------------

@router.post('/api/import', response_model=models.ImportResponse)
async def api_import(request: Request, ctx=Ctx):
    """Import an uploaded perf.data file."""
    try:
        content_length = int(request.headers.get('content-length', 0))
    except ValueError:
        content_length = 0
    if content_length <= 0:
        return _json({'error': 'empty request body'}, 400)
    if content_length > sessions.MAX_IMPORT_SIZE:
        return _json({
            'error': f'file too large ({content_length} bytes, '
                     f'max {sessions.MAX_IMPORT_SIZE // 1024 // 1024} MB)'
        }, 413)
    if not ctx.config.perf_bin:
        return _json({
            'error': 'perf not found on server — cannot import perf.data'
        }, 500)

    tmp = tempfile.NamedTemporaryFile(suffix='.data', delete=False)
    try:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > sessions.MAX_IMPORT_SIZE:
                return _json({'error': 'file too large'}, 413)
            tmp.write(chunk)
        tmp.close()

        session_id, samples, metadata = await run_in_threadpool(
            sessions.import_perf_data, ctx.config, tmp.name)
        return _json({
            'session_id': session_id,
            'total_samples': len(samples),
            'event_types': metadata['event_types'],
        })
    except RuntimeError as e:
        return _json({'error': str(e)}, 500)
    except Exception as e:
        return _json({'error': f'import failed: {e}'}, 500)
    finally:
        try:
            tmp.close()
        except OSError:
            pass
        if os.path.isfile(tmp.name):
            os.unlink(tmp.name)


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


def create_app(ctx):
    """Build the ASGI app around an AppContext."""
    from perflens import __version__
    app = FastAPI(title='PerfLens', version=__version__, lifespan=_lifespan,
                  docs_url=None, redoc_url=None,
                  openapi_url='/api/openapi.json')
    app.state.ctx = ctx
    app.state.sse_hub = _SSEHub()
    app.include_router(router)

    # Static UI last — API routes take precedence. StaticFiles owns path
    # normalization/traversal safety.
    ui_dir = ctx.config.ui_dir
    if ui_dir and os.path.isfile(os.path.join(ui_dir, 'index.html')):
        app.mount('/', StaticFiles(directory=ui_dir, html=True), name='ui')
    else:
        # Source checkout without built frontend assets — keep the API
        # usable and say what's missing instead of 404ing everything.
        @app.get('/{path:path}')
        def ui_missing(path: str):
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
