"""Export tool — writes profiling data to a file.

Exports are deliberately never returned inline. A collapsed-stack dump or a
full JSON profile is orders of magnitude larger than anything an agent
should be reading into context; what an agent actually wants is a path it
can hand to another tool, attach to a report, or open with a flamegraph
viewer.
"""

import os

from mcp.types import ToolAnnotations

from perflens.mcp import format as fmt
from perflens.mcp.client import LIVE, PerfLensError

# Writes a file, so not read-only — but it only ever creates the path it is
# given, never deletes or overwrites something it did not just produce.
WRITES_FILE = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                              idempotent_hint=True, open_world_hint=False)

_EXTENSIONS = {'collapsed': '.collapsed', 'json': '.json', 'svg': '.svg'}


def register(mcp, client):
    """Register the export tool on `mcp`."""

    @mcp.tool(
        name='perflens_export',
        title='Export a profile to a file',
        description=(
            'Write a profile to disk: `collapsed` folded stacks (the input '
            'format for external flamegraph tools), `json` for the full '
            'per-event data, or `svg` for a standalone flamegraph image. '
            'Returns the path and size — the content is never returned inline '
            'because these files are far too large to read into context.'),
        annotations=WRITES_FILE,
    )
    async def perflens_export(out_path: str, source: str = LIVE,
                              format: str = 'collapsed', event: str = '',
                              response_format: str = 'markdown') -> str:
        if format not in _EXTENSIONS:
            raise PerfLensError(
                f'Unknown export format {format!r}. Use one of: '
                f'{", ".join(sorted(_EXTENSIONS))}.')

        # SVG renders one event, so resolve a sensible default rather than
        # letting the server fall back to a `cycles` that may not exist.
        event_name = event
        if format == 'svg' and not event_name:
            event_name, _entry, _meta = await client.event_entry(source, None)

        content = await client.export(source, format, event_name or None)

        path = os.path.abspath(os.path.expanduser(out_path))
        if not os.path.splitext(path)[1]:
            path += _EXTENSIONS[format]
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            raise PerfLensError(
                f'Cannot write there: the directory {parent!r} does not exist.')
        with open(path, 'wb') as handle:
            handle.write(content)

        data = {'path': path, 'bytes': len(content), 'format': format,
                'source': source, 'event': event_name or None}
        size = len(content)
        human = (f'{size / 1024 / 1024:.1f} MB' if size >= 1024 * 1024
                 else f'{size / 1024:.1f} KB')
        body = [f'Wrote {human} to `{path}`.', '',
                f'- source: {source}', f'- format: {format}']
        if event_name:
            body.append(f'- event: {event_name}')
        return fmt.respond(data, '\n'.join(body), response_format)
