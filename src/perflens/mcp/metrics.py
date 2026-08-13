"""Device health metric tools.

The agent streams health frames (CPU, memory, temperature, load, process
stats, and — when enabled — disk I/O and per-thread CPU) every couple of
seconds. They matter to a profile reader because they catch the case where
the profile is misleading: a thermally throttled or swapping device
produces a hot-function table that describes the symptom, not the cause.
"""

from mcp.types import ToolAnnotations

from perflens.mcp import format as fmt

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)

# Frames are ~2s apart, so an unbounded history is thousands of records for
# a short session. Summarise instead, and sample evenly across the window.
_MAX_SERIES_POINTS = 40


def _flatten(frame, prefix=''):
    """Flatten a health frame into scalar leaves for compact display."""
    out = {}
    for key, value in sorted((frame or {}).items()):
        if key in ('type', 'timestamp'):
            continue
        name = f'{prefix}{key}'
        if isinstance(value, dict):
            out.update(_flatten(value, f'{name}.'))
        elif isinstance(value, list):
            out[name] = f'[{len(value)} entries]'
        else:
            out[name] = value
    return out


def register(mcp, client):
    """Register the device-health tools on `mcp`."""

    @mcp.tool(
        name='perflens_device_metrics',
        title='Device health now',
        description=(
            'The latest device health frame from the agent: CPU, memory, '
            'temperature, load, process stats, and disk or per-thread CPU when '
            'those opt-in collectors are enabled. Check this when a profile '
            'looks strange — throttling, swapping or a saturated device '
            'explains hot-function tables that otherwise make no sense.'),
        annotations=READ_ONLY,
    )
    async def perflens_device_metrics(kind: str = '',
                                      response_format: str = 'markdown') -> str:
        latest = await client.metrics_current()
        if kind:
            frame = latest.get(kind)
            if frame is None:
                available = ', '.join(sorted(latest)) or '(none)'
                return fmt.respond(
                    {'kinds': sorted(latest)},
                    f'No `{kind}` metrics available. Kinds with data: '
                    f'{available}.', response_format)
            frames = {kind: frame}
        else:
            frames = latest

        data = {'frames': frames}
        if not frames:
            return fmt.respond(
                data,
                '# Device health\n\n_No metrics yet. The agent streams health '
                'frames every couple of seconds once connected — check '
                'perflens_agent_info._', response_format)

        body = ['# Device health', '']
        for name, frame in sorted(frames.items()):
            rows = list(_flatten(frame).items())
            body += [f'## {name}', '', fmt.table(['metric', 'value'], rows), '']
        return fmt.respond(data, '\n'.join(body).rstrip(), response_format)

    @mcp.tool(
        name='perflens_metrics_history',
        title='Device health over time',
        description=(
            'A time series of one device health metric kind, summarised: min, '
            'max and mean of each numeric field plus an evenly sampled set of '
            'points. Use it to see whether the device was degrading during a '
            'profiling run rather than steady.'),
        annotations=READ_ONLY,
    )
    async def perflens_metrics_history(kind: str = 'system', start: float = 0.0,
                                       response_format: str = 'markdown') -> str:
        history = await client.metrics_history(kind, start=start or None)
        if not history:
            return fmt.respond(
                {'kind': kind, 'points': []},
                f'# {kind} history\n\n_No history recorded for `{kind}`._',
                response_format)

        flat = [_flatten(frame) for frame in history]
        numeric_fields = sorted({
            key for record in flat for key, value in record.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)})

        summary = {}
        for field in numeric_fields:
            values = [r[field] for r in flat
                      if isinstance(r.get(field), (int, float))]
            if values:
                summary[field] = {'min': round(min(values), 2),
                                  'max': round(max(values), 2),
                                  'mean': round(sum(values) / len(values), 2)}

        step = max(1, len(history) // _MAX_SERIES_POINTS)
        sampled = history[::step][:_MAX_SERIES_POINTS]
        data = {'kind': kind, 'frames': len(history), 'summary': summary,
                'sampled_points': sampled}

        rows = [(field, values['min'], values['mean'], values['max'])
                for field, values in sorted(summary.items())]
        body = [f'# {kind} history', '',
                f'{len(history)} frames'
                + (f', sampled every {step}' if step > 1 else '') + '.', '',
                fmt.table(['metric', 'min', 'mean', 'max'], rows)]
        return fmt.respond(data, '\n'.join(body), response_format)
