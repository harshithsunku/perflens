"""Compatibility shim (kept for one release).

``perflens.server`` was split into focused modules:

- ``perflens.app``       — AppContext + lifecycle + main()
- ``perflens.config``    — ServerConfig, CLI parsing, tool probing
- ``perflens.state``     — ProfilingState, MetricsState, rebuild worker
- ``perflens.agentlink`` — agent TCP wire protocol + AgentSession
- ``perflens.sessions``  — session persistence, replay, perf.data import
- ``perflens.export``    — collapsed-stack and SVG exports

Import from those modules directly.
"""

from perflens.app import AppContext, build_context, main  # noqa: F401
from perflens.config import ServerConfig  # noqa: F401
from perflens.state import MetricsState, ProfilingState  # noqa: F401

if __name__ == '__main__':
    main()
