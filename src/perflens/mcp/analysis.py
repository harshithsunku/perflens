"""Orientation and profile-analysis tools.

These are the tools an agent spends most of its turns in: find out what
data exists, rank the functions burning samples, see the call paths that
reach them, drop to the annotated source lines, and compare two runs.
"""

from mcp.types import ToolAnnotations

from perflens.mcp import format as fmt
from perflens.mcp.client import LIVE, PerfLensError, pick_event

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)


async def _resolve_source_path(client, file, event_name):
    """Map a source file an agent named onto the path the profile knows.

    /api/source keys on the compile-time path from DWARF, but agents reach
    for the basename they saw in a function table. Match on basename when it
    is unambiguous; otherwise fail with the candidates rather than a 404.
    """
    _event, entry, _meta = await client.event_entry(LIVE, event_name)
    known = [f.get('path', '') for f in (entry.get('source_files') or [])
             if f.get('path')]
    wanted = file.rsplit('/', 1)[-1]
    matches = [p for p in known if p.rsplit('/', 1)[-1] == wanted]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        listed = ', '.join(sorted(known)[:5]) or '(none)'
        raise PerfLensError(
            f'No annotated source for {file!r} in the live profile. Files '
            f'with annotation include: {listed}. Call '
            f'perflens_list_source_files for the full list.')
    raise PerfLensError(
        f'{wanted!r} is ambiguous — it matches {", ".join(sorted(matches))}. '
        f'Pass the full path.')


def register(mcp, client):
    """Register the orientation + analysis tools on `mcp`."""

    @mcp.tool(
        name='perflens_status',
        title='PerfLens status',
        description=(
            'Orientation: what data and hardware are available right now. '
            'Reports whether the server is reachable, whether a device agent '
            'is connected, how many live samples exist, which perf events '
            'have data, whether source mapping is available, and how many '
            'saved sessions there are. Call this first — it prevents guessing '
            'event names or session ids that do not exist.'),
        annotations=READ_ONLY,
    )
    async def perflens_status(response_format: str = 'markdown') -> str:
        status = await client.status()
        sessions = await client.sessions(0, 1)
        index = await client.index_status()

        events = []
        if status.get('total_samples'):
            head = await client.stream_head()
            events = (head.get('data_version') or {}).get('event_types') or []

        data = {
            'server_url': client.base_url,
            'agent_connected': bool(status.get('agent_connected')),
            'agent_addr': status.get('agent_addr'),
            'live_samples': status.get('total_samples', 0),
            'live_chunks': status.get('chunk_count', 0),
            'live_events': events,
            'saved_sessions': sessions.get('total', 0),
            'source_mapping': {
                'symbols_loaded': index.get('symbols_loaded', 0),
                'source_files_found': index.get('source_files_found', 0),
                # symbols_loaded/source_files_found only count the eager
                # pre-index pass, which runs when a binary is configured at
                # runtime -- not when one is passed as --binary at startup.
                # In that case they stay 0 while resolution works lazily, so
                # they cannot be used alone to decide whether source
                # annotation is available.
                'source_index_files': index.get('source_index_files', 0),
                'source_index_ready': bool(index.get('source_index_ready')),
                'indexing': bool(index.get('indexing')),
            },
        }

        agent = (f"connected ({data['agent_addr']})"
                 if data['agent_connected'] else 'not connected')
        lines = [
            '# PerfLens status',
            '',
            f"- Server: {data['server_url']}",
            f'- Device agent: {agent}',
            f"- Live samples: {data['live_samples']:,} "
            f"across {data['live_chunks']} chunks",
            f"- Live events with data: "
            f"{', '.join(events) if events else '(none yet)'}",
            f"- Saved sessions: {data['saved_sessions']}",
            f"- Source mapping: {data['source_mapping']['symbols_loaded']:,} symbols, "
            f"{data['source_mapping']['source_files_found']:,} source files, "
            f"{data['source_mapping']['source_index_files']:,} files in the "
            f"source index",
        ]
        if not data['live_samples'] and data['saved_sessions']:
            lines.append('')
            lines.append('_No live data — analyse a saved session by passing '
                         'its id as `source`._')
        # Only claim source annotation is unavailable when nothing at all is
        # resolvable. Warning on symbols_loaded alone told an agent to skip
        # `perflens_source_hotlines` on servers where it works fine.
        if (not data['source_mapping']['symbols_loaded']
                and not data['source_mapping']['source_index_files']):
            lines.append('')
            lines.append('_No symbols loaded: line-level source annotation '
                         'needs the server started with `--binary` pointing at '
                         'an unstripped build._')
        return fmt.respond(data, '\n'.join(lines), response_format)

    @mcp.tool(
        name='perflens_list_sessions',
        title='List saved profiling sessions',
        description=(
            'List saved profiling sessions, newest first: id, capture time, '
            'sample count, and which perf events each one carries. Use the id '
            'as the `source` argument of the analysis tools.'),
        annotations=READ_ONLY,
    )
    async def perflens_list_sessions(limit: int = 20, offset: int = 0,
                                     response_format: str = 'markdown') -> str:
        payload = await client.sessions(offset=offset, limit=fmt.clamp(limit))
        sessions = payload.get('sessions') or []
        info = {'total': payload.get('total', len(sessions)),
                'shown': len(sessions), 'offset': offset,
                'has_more': offset + len(sessions) < payload.get('total', 0),
                'next_offset': offset + len(sessions)}
        data = {'sessions': sessions, 'page': info}

        rows = [(s.get('session_id', '?'),
                 s.get('timestamp', '')[:19] or '-',
                 f"{s.get('total_samples', 0):,}",
                 ', '.join(s.get('event_types') or []) or '-')
                for s in sessions]
        body = ['# Saved sessions', '',
                fmt.table(['session id', 'captured', 'samples', 'events'], rows)]
        note = fmt.page_note(info, 'perflens_list_sessions')
        return fmt.respond(data, '\n'.join(body) + note, response_format)

    @mcp.tool(
        name='perflens_hot_functions',
        title='Hot functions',
        description=(
            'Rank functions by samples for live or saved profiling data. '
            "sort='self' finds the leaf actually burning cycles; sort='total' "
            'finds the subtree responsible for it — a real diagnosis usually '
            'needs both. Optionally filter to one thread with `tid` (live data '
            'only). Reports what share of the profile the listed functions '
            'cover, so a misleadingly short tail is visible.'),
        annotations=READ_ONLY,
    )
    async def perflens_hot_functions(source: str = LIVE, event: str = '',
                                     sort: str = 'self', tid: int = 0,
                                     limit: int = 20, offset: int = 0,
                                     response_format: str = 'markdown') -> str:
        if tid:
            event_name, summary = await _thread_summary(client, source, event, tid)
            scope = f'thread {tid}'
        else:
            event_name, entry, _meta = await client.event_entry(source, event or None)
            summary = entry.get('function_summary') or {}
            scope = 'all threads'

        functions = fmt.sort_functions(summary.get('functions') or [], sort)
        total = summary.get('total_samples', 0)
        window, info = fmt.page(functions, limit, offset)
        data = {'source': source, 'event': event_name, 'scope': scope,
                'sort': sort, 'total_samples': total,
                'coverage_percent': round(fmt.coverage(window, total), 1),
                'functions': window, 'page': info}

        body = [f'# Hot functions — {source} / {event_name} ({scope})', '',
                f"{total:,} samples, sorted by {sort}. "
                f"These {len(window)} account for "
                f"{fmt.fmt_pct(data['coverage_percent'])} of the profile.",
                '',
                fmt.table(fmt.FUNCTION_HEADERS, fmt.function_rows(window))]
        note = fmt.page_note(info, 'perflens_hot_functions',
                             f"source='{source}'")
        return fmt.respond(data, '\n'.join(body) + note, response_format)

    @mcp.tool(
        name='perflens_hot_stacks',
        title='Hot call stacks',
        description=(
            'The flamegraph as ranked text: the hottest leaf-to-root call '
            'paths with their sample share. This is the bottleneck tool — it '
            'shows not just which function is hot but how it is reached, which '
            'is what tells you where a fix belongs. Optionally filter to one '
            'thread with `tid` (live data only).'),
        annotations=READ_ONLY,
    )
    async def perflens_hot_stacks(source: str = LIVE, event: str = '',
                                  tid: int = 0, limit: int = 10,
                                  min_percent: float = 1.0,
                                  response_format: str = 'markdown') -> str:
        if tid:
            event_name, flamegraph = await _thread_flamegraph(client, source,
                                                              event, tid)
            scope = f'thread {tid}'
        else:
            event_name, entry, _meta = await client.event_entry(source, event or None)
            flamegraph = entry.get('flamegraph') or {}
            scope = 'all threads'

        total = flamegraph.get('value', 0)
        paths = fmt.fold_stacks(flamegraph, limit=limit,
                                min_percent=min_percent, total=total)
        data = {'source': source, 'event': event_name, 'scope': scope,
                'total_samples': total, 'min_percent': min_percent,
                'stacks': paths}
        body = [f'# Hot stacks — {source} / {event_name} ({scope})', '',
                f'{total:,} samples. Paths are root → leaf; the percentage is '
                f'the leaf frame\'s own share.', '',
                fmt.stack_lines(paths)]
        return fmt.respond(data, '\n'.join(body), response_format)

    @mcp.tool(
        name='perflens_perf_stat',
        title='Hardware counters',
        description=(
            'Hardware counter totals with the ratios derived from them — IPC, '
            'cache-miss rate, branch-miss rate. These separate "this code is '
            'slow" from "this workload is starved on memory", which lead to '
            'completely different fixes. Read them before concluding anything '
            'from a function table.'),
        annotations=READ_ONLY,
    )
    async def perflens_perf_stat(source: str = LIVE,
                                 response_format: str = 'markdown') -> str:
        if source == LIVE:
            head = await client.stream_head()
            counters = head.get('perf_stat') or {}
        else:
            _per_event, meta = await client.per_event(source)
            counters = meta.get('perf_stat') or {}

        derived = fmt.derived_counters(counters)
        data = {'source': source, 'counters': counters, 'derived': derived}

        if not counters:
            body = (f'# Hardware counters — {source}\n\n'
                    '_No `perf stat` counters available for this source. '
                    'They arrive with the profiling stream; if collection has '
                    'only just started, try again in a few seconds._')
            return fmt.respond(data, body, response_format)

        rows = []
        for name, value in sorted(counters.items()):
            if isinstance(value, dict):
                value = value.get('value', '')
            rows.append((name, value))
        body = [f'# Hardware counters — {source}', '',
                fmt.table(['counter', 'value'], rows)]
        if derived:
            body += ['', '## Derived', '',
                     fmt.table(['ratio', 'value'],
                               [(k, v) for k, v in sorted(derived.items())])]
        return fmt.respond(data, '\n'.join(body), response_format)

    @mcp.tool(
        name='perflens_list_source_files',
        title='Source files with samples',
        description=(
            'List the source files that carry samples, hottest first. This is '
            'the discovery step before perflens_source_hotlines — it avoids '
            'guessing a path that the profile has no data for.'),
        annotations=READ_ONLY,
    )
    async def perflens_list_source_files(source: str = LIVE, event: str = '',
                                         limit: int = 20, offset: int = 0,
                                         response_format: str = 'markdown') -> str:
        event_name, entry, _meta = await client.event_entry(source, event or None)
        files = sorted(entry.get('source_files') or [],
                       key=lambda f: f.get('total_samples', 0), reverse=True)
        window, info = fmt.page(files, limit, offset)
        data = {'source': source, 'event': event_name,
                'files': window, 'page': info}

        rows = [(f.get('path', '?'),
                 f"{f.get('total_samples', 0):,}",
                 'yes' if f.get('found') else 'no',
                 ', '.join((f.get('functions') or [])[:3]) or '-')
                for f in window]
        body = [f'# Source files with samples — {source} / {event_name}', '',
                fmt.table(['file', 'samples', 'found locally', 'functions'], rows)]
        if window and not any(f.get('found') for f in window):
            body += ['', '_None of these files were found on disk. Point the '
                     'server at the sources with `--source-dir`, or map '
                     'compile-time paths with `--path-map FROM=TO`._']
        note = fmt.page_note(info, 'perflens_list_source_files',
                             f"source='{source}'")
        return fmt.respond(data, '\n'.join(body) + note, response_format)

    @mcp.tool(
        name='perflens_source_hotlines',
        title='Hot source lines',
        description=(
            'The hottest annotated lines of one source file, with the actual '
            'code and a few lines of surrounding context. This is where a '
            'profile becomes actionable. Requires the server to have symbols '
            '(--binary with an unstripped build) and the sources on disk.'),
        annotations=READ_ONLY,
    )
    async def perflens_source_hotlines(file: str, source: str = LIVE,
                                       event: str = '', tid: int = 0,
                                       limit: int = 15, context: int = 2,
                                       response_format: str = 'markdown') -> str:
        if source == LIVE:
            event_name = event or None
            if event_name is None:
                per_event, _meta = await client.per_event(LIVE)
                event_name = pick_event(per_event)
            try:
                payload = await client.source(file, event_name, tid or None)
            except PerfLensError:
                # /api/source keys on the path DWARF recorded, so a bare
                # basename -- the natural thing for an agent to pass, and what
                # the function tables show -- misses. Resolve it, or say which
                # files would work, rather than dead-ending on a 404.
                resolved = await _resolve_source_path(client, file, event_name)
                payload = await client.source(resolved, event_name, tid or None)
                file = resolved
            lines = payload.get('lines') or []
        else:
            event_name, entry, _meta = await client.event_entry(source, event or None)
            annotated = entry.get('source') or {}
            lines = annotated.get(file) or []
            if not lines:
                known = ', '.join(sorted(annotated)[:5]) or '(none)'
                raise PerfLensError(
                    f'Session {source!r} has no annotated source for {file!r}. '
                    f'Files with annotation include: {known}. Call '
                    f'perflens_list_source_files for the full list.')

        # Annotated records are {'line', 'source', 'samples', 'percent'}.
        hot = sorted(range(len(lines)),
                     key=lambda i: lines[i].get('samples', 0), reverse=True)
        hot = [i for i in hot if lines[i].get('samples', 0) > 0]
        hot = hot[:fmt.clamp(limit, default=15)]
        if not hot:
            raise PerfLensError(
                f'No sampled lines in {file!r} for event {event_name!r}.')

        keep = fmt.source_context(lines, hot, max(0, min(int(context), 10)))
        hot_set = set(hot)
        total = sum(line.get('samples', 0) for line in lines)

        kept = [{'line': lines[i].get('line'),
                 'code': lines[i].get('source', ''),
                 'samples': lines[i].get('samples', 0),
                 'percent': lines[i].get('percent', 0.0),
                 'hot': i in hot_set} for i in keep]
        data = {'source': source, 'event': event_name, 'file': file,
                'file_samples': total, 'lines': kept}

        rendered = []
        previous = None
        for record in kept:
            if previous is not None and record['line'] != previous + 1:
                rendered.append('       ...')
            marker = '>>' if record['hot'] else '  '
            share = (f"{record['percent']:5.1f}%" if record['samples']
                     else '      ')
            rendered.append(f"{marker} {record['line']:>5} {share} "
                            f"| {record['code']}")
            previous = record['line']

        body = [f'# Hot lines — {file}', '',
                f'{source} / {event_name}, {total:,} samples in this file. '
                f'`>>` marks the hottest lines.', '',
                '```', '\n'.join(rendered), '```']
        return fmt.respond(data, '\n'.join(body), response_format)

    @mcp.tool(
        name='perflens_compare',
        title='Compare two profiles',
        description=(
            'Diff two profiles by function — a saved session against another '
            'session, or against live data. Reports which functions got hotter '
            'or colder, plus those that appeared or vanished entirely. Use it '
            'to find what a change regressed, rather than eyeballing two '
            'function tables.'),
        annotations=READ_ONLY,
    )
    async def perflens_compare(baseline: str, target: str = LIVE,
                               event: str = '', limit: int = 20,
                               response_format: str = 'markdown') -> str:
        base_event, base_entry, _ = await client.event_entry(baseline, event or None)
        targ_event, targ_entry, _ = await client.event_entry(target, event or None)
        if base_event != targ_event:
            raise PerfLensError(
                f'The two profiles have no event in common to compare: '
                f'baseline resolved to {base_event!r}, target to '
                f'{targ_event!r}. Pass `event` explicitly.')

        def index(entry):
            summary = entry.get('function_summary') or {}
            out = {}
            for func in summary.get('functions') or []:
                out[(func.get('name', '?'), func.get('module', ''))] = func
            return out, summary.get('total_samples', 0)

        base_funcs, base_total = index(base_entry)
        targ_funcs, targ_total = index(targ_entry)

        # Percentages, not raw samples: the two runs almost never have the
        # same sample count, so absolute deltas would be meaningless.
        rows = []
        for key in set(base_funcs) | set(targ_funcs):
            name, module = key
            before = base_funcs.get(key, {}).get('self_percent', 0.0)
            after = targ_funcs.get(key, {}).get('self_percent', 0.0)
            if key not in base_funcs:
                kind = 'appeared'
            elif key not in targ_funcs:
                kind = 'vanished'
            else:
                kind = 'changed'
            rows.append({'name': name, 'module': module,
                         'baseline_percent': before, 'target_percent': after,
                         'delta_percent': round(after - before, 2),
                         'status': kind})

        rows.sort(key=lambda r: abs(r['delta_percent']), reverse=True)
        window, info = fmt.page(rows, limit, 0)
        data = {'baseline': baseline, 'target': target, 'event': base_event,
                'baseline_samples': base_total, 'target_samples': targ_total,
                'functions': window, 'page': info}

        table_rows = [(r['name'], r['module'] or '-',
                       fmt.fmt_pct(r['baseline_percent']),
                       fmt.fmt_pct(r['target_percent']),
                       f"{r['delta_percent']:+.2f}pp",
                       r['status'])
                      for r in window]
        body = [f'# Comparison — {baseline} → {target} ({base_event})', '',
                f'Baseline {base_total:,} samples, target {targ_total:,}. '
                f'Deltas are percentage points of self time, so differing '
                f'sample counts do not distort them.', '',
                fmt.table(['function', 'module', 'baseline', 'target',
                           'delta', 'status'], table_rows)]
        return fmt.respond(data, '\n'.join(body), response_format)


async def _thread_summary(client, source, event, tid):
    """Per-thread function summary — live data only."""
    event_name = await _live_only_event(client, source, event, tid)
    view = await client.thread_view(tid, event_name)
    summary = view.get('function_summary') or {}
    if not summary.get('functions'):
        raise PerfLensError(
            f'No samples for thread {tid} on event {event_name!r}. Call '
            f'perflens_threads to see which thread ids have data.')
    return event_name, summary


async def _thread_flamegraph(client, source, event, tid):
    event_name = await _live_only_event(client, source, event, tid)
    view = await client.thread_view(tid, event_name)
    return event_name, view.get('flamegraph') or {}


async def _live_only_event(client, source, event, tid):
    """Resolve the event for a per-thread view, rejecting saved sessions.

    Per-thread breakdowns are computed from the live sample ring; a saved
    session's replay carries only the thread list, so this is a real
    limitation rather than something to paper over.
    """
    if source != LIVE:
        raise PerfLensError(
            f'Per-thread views ({tid=}) are available for live data only. '
            f'Session {source!r} can be analysed without `tid`, and '
            f'perflens_threads lists the threads it contains.')
    per_event, _meta = await client.per_event(LIVE)
    return pick_event(per_event, event or None)
