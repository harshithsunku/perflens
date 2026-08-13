"""Device agent lifecycle tools.

These are the only tools that change anything: they connect to a device
agent and start or stop collection on it. Two properties of the frozen
agent shape the design here.

First, it is slow in specific places — capability probing on first
connection takes 8-14 seconds, and `list_processes` and `start` are given a
120 second budget server-side. The timeouts below match that rather than
failing early on a device that is merely busy.

Second, collection does not produce samples instantly: in continuous pipe
mode `perf record` flushes its ring buffer in batches, so the first chunk
after `start` may carry only counter data. Tools say this explicitly,
because an agent that queries immediately and sees nothing will otherwise
report "no data" when the right answer is "wait a few seconds".
"""

from mcp.types import ToolAnnotations

from perflens.mcp import format as fmt

# Not read-only: these reach a real device and change what it is doing.
# Not destructive either — nothing is deleted, and starting or stopping a
# profiler is reversible.
CONTROL = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                          idempotent_hint=False, open_world_hint=True)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)

_PROBE_TIMEOUT = 120


def register(mcp, client):
    """Register the agent-control tools on `mcp`."""

    @mcp.tool(
        name='perflens_agent_info',
        title='Agent connection info',
        description=(
            'Details of the connected device agent: address, agent version, '
            'and the platform it probed (architecture, kernel, which perf '
            'events and call-graph modes actually work there). Check this '
            'before starting collection — the usable event set varies by '
            'device and kernel.'),
        annotations=READ_ONLY,
    )
    async def perflens_agent_info(response_format: str = 'markdown') -> str:
        info = await client.agent_info()
        if not info.get('connected'):
            return fmt.respond(
                info,
                '# Device agent\n\n_Not connected._ Use '
                '`perflens_agent_connect(host, port)` to reach an agent '
                'started with `--listen`, or start the agent on the device '
                'with `--server <this host>` so it connects out.',
                response_format)

        hello = info.get('hello') or {}
        platform = hello.get('platform') or {}
        rows = [('address', info.get('addr') or '-'),
                ('agent version', hello.get('version') or '-')]
        rows += [(key, value) for key, value in sorted(platform.items())
                 if not isinstance(value, (dict, list))]
        lists = {key: value for key, value in sorted(platform.items())
                 if isinstance(value, list)}

        body = ['# Device agent', '', fmt.table(['field', 'value'], rows)]
        for key, values in lists.items():
            body += ['', f'**{key}**: {", ".join(str(v) for v in values) or "-"}']
        return fmt.respond(info, '\n'.join(body), response_format)

    @mcp.tool(
        name='perflens_agent_connect',
        title='Connect to a device agent',
        description=(
            'Connect out to a PerfLens agent that was started with --listen on '
            'a device. Takes up to ~15 seconds because the agent probes which '
            'perf events and call-graph modes work there before reporting '
            'ready. Not needed when the agent was started with --server, since '
            'it connects in on its own.'),
        annotations=CONTROL,
    )
    async def perflens_agent_connect(host: str, port: int = 9999,
                                     response_format: str = 'markdown') -> str:
        result = await client.agent_connect(host, port)
        hello = result.get('hello') or {}
        platform = hello.get('platform') or {}
        body = [f'Connected to agent at {result.get("addr") or f"{host}:{port}"}.',
                '',
                f'- agent version: {hello.get("version") or "?"}',
                f'- arch: {platform.get("arch") or "?"}',
                f'- kernel: {platform.get("kernel") or "?"}',
                '',
                '_Next: `perflens_list_processes` to find the pid, then '
                '`perflens_start_profiling(pid)`._']
        return fmt.respond(result, '\n'.join(body), response_format)

    @mcp.tool(
        name='perflens_list_processes',
        title='List device processes',
        description=(
            'List the processes running on the connected device, so you can '
            'pick the pid to profile. Optionally filter by a substring of the '
            'process name. This round-trips to the device and can take up to a '
            'minute on a busy target.'),
        annotations=READ_ONLY,
    )
    async def perflens_list_processes(match: str = '', limit: int = 30,
                                      response_format: str = 'markdown') -> str:
        resp = await client.agent_command('list_processes',
                                          timeout=_PROBE_TIMEOUT)
        processes = resp.get('processes') or resp.get('data') or []
        if match:
            needle = match.lower()
            processes = [p for p in processes
                         if needle in str(p.get('name', '')).lower()
                         or needle in str(p.get('cmdline', '')).lower()]

        window, info = fmt.page(processes, limit, 0)
        data = {'match': match, 'processes': window, 'page': info}
        rows = [(p.get('pid', '?'),
                 p.get('name', '') or '-',
                 p.get('user', '') or '-',
                 str(p.get('cmdline', ''))[:60] or '-')
                for p in window]
        body = ['# Device processes', '',
                fmt.table(['pid', 'name', 'user', 'cmdline'], rows)]
        if not window:
            body += ['', '_No matching processes._']
        return fmt.respond(data, '\n'.join(body), response_format)

    @mcp.tool(
        name='perflens_start_profiling',
        title='Start profiling a process',
        description=(
            'Start perf collection on a pid on the connected device. `events` '
            'may narrow the probed record events (see perflens_agent_info); '
            'frequency and duration override the agent defaults. Samples take '
            'a few seconds to start flowing — the first chunk may carry only '
            'counter data — so wait before analysing rather than concluding '
            'there is no data.'),
        annotations=CONTROL,
    )
    async def perflens_start_profiling(pid: int, events: str = '',
                                       frequency: int = 0, duration: int = 0,
                                       response_format: str = 'markdown') -> str:
        args = {'pid': pid}
        if events:
            args['events'] = [e.strip() for e in events.split(',') if e.strip()]
        if frequency:
            args['frequency'] = frequency
        if duration:
            args['duration'] = duration

        resp = await client.agent_command('start', args, timeout=_PROBE_TIMEOUT)
        body = [f'Started profiling pid {pid}.', '']
        body += [f'- {key}: {value}' for key, value in sorted(args.items())
                 if key != 'pid']
        body += ['',
                 '_Allow a few seconds for samples to accumulate, then use '
                 '`perflens_hot_functions` / `perflens_hot_stacks`. Stop with '
                 '`perflens_stop_profiling`._']
        return fmt.respond(resp, '\n'.join(body), response_format)

    @mcp.tool(
        name='perflens_stop_profiling',
        title='Stop profiling',
        description=(
            'Stop the current collection on the device. Data already streamed '
            'stays available for analysis and is saved as a session.'),
        annotations=CONTROL,
    )
    async def perflens_stop_profiling(response_format: str = 'markdown') -> str:
        resp = await client.agent_command('stop', timeout=60)
        return fmt.respond(
            resp,
            'Stopped collection. Samples already received remain analysable '
            "as `source='live'`, and the run is saved as a session — see "
            '`perflens_list_sessions`.', response_format)

    @mcp.tool(
        name='perflens_collection_pause',
        title='Pause or resume collection',
        description=(
            'Pause the running collection, or resume a paused one, without '
            'tearing down the session. Useful to skip an uninteresting phase '
            'of a workload.'),
        annotations=CONTROL,
    )
    async def perflens_collection_pause(resume: bool = False,
                                        response_format: str = 'markdown') -> str:
        cmd = 'resume' if resume else 'pause'
        resp = await client.agent_command(cmd, timeout=60)
        return fmt.respond(resp, f'Collection {cmd}d.', response_format)
