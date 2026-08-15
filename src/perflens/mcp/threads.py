"""Per-thread (task) breakdown tools.

On Linux a thread is a task, and on a multi-threaded target the question
"where does the time go" usually has to be answered per task before the
function tables mean anything — one busy worker among ten idle ones looks
like a mild hotspot in the aggregate view.

Both tools read the live sample ring: `/api/threads` and `/api/threads/{tid}`
filter `ctx.state.all_samples`, and a saved session's replay carries only
the thread list, not per-thread aggregates. The tools say so rather than
silently returning the global numbers.
"""

from mcp.types import ToolAnnotations

from perflens.mcp import format as fmt
from perflens.mcp.client import LIVE, PerfLensError, pick_event

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)


def register(mcp, client):
    """Register the thread/task tools on `mcp`."""

    @mcp.tool(
        name='perflens_threads',
        title='Thread breakdown',
        description=(
            'Break the profile down by thread (task): which tids burn the '
            'samples, what each is named, and its top functions. Run this '
            'before drawing conclusions from whole-process function tables on '
            'a multi-threaded target. Live data only — for a saved session it '
            'reports the thread list the session recorded.'),
        annotations=READ_ONLY,
    )
    async def perflens_threads(source: str = LIVE, event: str = '',
                               limit: int = 20, offset: int = 0,
                               response_format: str = 'markdown') -> str:
        if source != LIVE:
            event_name, entry, _meta = await client.event_entry(source, event or None)
            threads = entry.get('threads') or []
            window, info = fmt.page(threads, limit, offset)
            data = {'source': source, 'event': event_name,
                    'threads': window, 'page': info,
                    'note': 'per-thread sample counts are live-only'}
            # Widened because the live branch below builds wider rows and
            # this assignment is what fixes the inferred element type.
            rows: list = [(t.get('tid', '?'), t.get('comm', '') or '-')
                          for t in window]
            body = [f'# Threads in session {source} ({event_name})', '',
                    fmt.table(['tid', 'comm'], rows), '',
                    '_Saved sessions record which threads were present, but '
                    'per-thread sample counts and function tables are computed '
                    'from live data only._']
            return fmt.respond(data, '\n'.join(body) + fmt.page_note(
                info, 'perflens_threads', f"source='{source}'"), response_format)

        per_event, _meta = await client.per_event(LIVE)
        event_name = pick_event(per_event, event or None)
        payload = await client.threads(event_name)
        threads = payload.get('threads') or []
        total = payload.get('total_samples', 0)
        window, info = fmt.page(threads, limit, offset)
        data = {'source': LIVE, 'event': event_name, 'total_samples': total,
                'threads': window, 'page': info}

        rows = [(t.get('tid', '?'),
                 t.get('comm', '') or '-',
                 f"{t.get('samples', 0):,}",
                 fmt.fmt_pct(t.get('percent', 0.0)),
                 t.get('top_function', '') or '-')
                for t in window]
        body = [f'# Threads — live / {event_name}', '',
                f'{total:,} samples across {info["total"]} threads.', '',
                fmt.table(['tid', 'comm', 'samples', 'share', 'top function'],
                          rows), '',
                '_Drill into one with perflens_thread_detail(tid=…)._']
        note = fmt.page_note(info, 'perflens_threads')
        return fmt.respond(data, '\n'.join(body) + note, response_format)

    @mcp.tool(
        name='perflens_thread_detail',
        title='Thread detail',
        description=(
            'One thread\'s own hot functions and hot call stacks. Use after '
            'perflens_threads has identified which task is worth the attention. '
            'Live data only.'),
        annotations=READ_ONLY,
    )
    async def perflens_thread_detail(tid: int, event: str = '',
                                     limit: int = 15,
                                     response_format: str = 'markdown') -> str:
        per_event, _meta = await client.per_event(LIVE)
        event_name = pick_event(per_event, event or None)
        view = await client.thread_view(tid, event_name)
        summary = view.get('function_summary') or {}
        functions = summary.get('functions') or []
        if not functions:
            raise PerfLensError(
                f'No samples for thread {tid} on event {event_name!r}. Call '
                f'perflens_threads to see which tids have data.')

        total = summary.get('total_samples', 0)
        window, info = fmt.page(functions, limit, 0)
        stacks = fmt.fold_stacks(view.get('flamegraph') or {}, limit=5,
                                 min_percent=1.0)
        data = {'tid': tid, 'event': event_name, 'total_samples': total,
                'functions': window, 'stacks': stacks, 'page': info}

        body = [f'# Thread {tid} — live / {event_name}', '',
                f'{total:,} samples in this thread.', '',
                '## Hot functions', '',
                fmt.table(fmt.FUNCTION_HEADERS, fmt.function_rows(window)), '',
                '## Hot stacks', '',
                fmt.stack_lines(stacks)]
        return fmt.respond(data, '\n'.join(body), response_format)
