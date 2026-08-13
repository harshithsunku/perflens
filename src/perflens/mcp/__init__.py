"""PerfLens MCP server.

Exposes PerfLens profiling data to LLM agents over the Model Context
Protocol. It is a client of the PerfLens HTTP API — the same API the web UI
uses — and holds no profiling state of its own, so a running
`perflens serve` is a prerequisite.

    perflens mcp [--server-url URL] [--read-only]

Tools come in two kinds. Everything that reads a profile is read-only and
safe to expose to an autonomous agent. The agent-control tools reach a real
device and change what it is doing, and `perflens_export` writes a file;
`--read-only` skips registering those.

The API's mutating surface is deliberately *not* exposed at all: no config
patching, no session deletion, no filesystem browsing.
"""

import os

from perflens.mcp.client import DEFAULT_URL, PerfLensClient

SERVER_NAME = 'perflens'


def build_server(server_url=None, read_only=False, client=None):
    """Build the MCP server and its API client.

    Returns (mcp, client); the caller owns closing the client. Passing an
    existing `client` is how the tests drive the tools against a
    TestClient-backed transport instead of a live socket.
    """
    import logging

    from mcp.server import MCPServer

    from perflens import __version__
    from perflens.mcp import analysis, control, export, metrics, threads

    # One INFO line per HTTP call is pure noise here, and on stdio the
    # streams are the protocol — keep them quiet.
    logging.getLogger('httpx').setLevel(logging.WARNING)

    url = server_url or os.environ.get('PERFLENS_MCP_URL') or DEFAULT_URL
    api = client or PerfLensClient(url)

    mcp = MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=(
            'PerfLens profiles Linux processes with perf and symbolizes the '
            'result down to source lines. Start with perflens_status to see '
            'what data exists, then rank functions with perflens_hot_functions '
            'and read the call paths with perflens_hot_stacks before drilling '
            'into perflens_source_hotlines. Profiles are large: every tool '
            'returns a ranked, capped view and tells you how to page for more.'),
    )

    analysis.register(mcp, api)
    threads.register(mcp, api)
    metrics.register(mcp, api)
    if not read_only:
        control.register(mcp, api)
        export.register(mcp, api)

    return mcp, api


def main(argv=None):
    """Entry point for `perflens mcp` (stdio transport)."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog='perflens mcp',
        description='Serve PerfLens profiling data to LLM agents over MCP.')
    parser.add_argument('--server-url', default=None,
                        help=f'PerfLens HTTP API to query (default: '
                             f'$PERFLENS_MCP_URL or {DEFAULT_URL})')
    parser.add_argument('--read-only', action='store_true',
                        help='omit the agent-control and export tools, so the '
                             'agent can analyse but not touch a device or '
                             'write files')
    args = parser.parse_args(argv)

    try:
        mcp, _api = build_server(args.server_url, args.read_only)
    except ImportError:
        # stdout carries the protocol, so diagnostics go to stderr.
        print('error: the MCP SDK is not installed. Install the extra with '
              '`pip install perflens[mcp]` (or `uv tool install '
              "'perflens[mcp]'`).", file=sys.stderr)
        return 2

    mcp.run(transport='stdio')
    return 0
