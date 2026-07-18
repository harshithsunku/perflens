"""PerfLens application context and lifecycle.

`AppContext` is the single object every layer hangs off: the HTTP routes
get it via `request.app.state.ctx`, worker threads receive it explicitly
at construction. No module-level globals.
"""

import dataclasses
import os
import sys
import threading

from perflens.agentlink import AgentSlot, run_tcp_server
from perflens.config import (ServerConfig, config_from_args,
                             create_source_mapper, probe_tools)
from perflens.state import MetricsState, ProfilingState, rebuild_worker


def default_wizard_state():
    return {
        'step': 0,
        'agent_host': '',
        'agent_port': 9999,
        'connected': False,
        'perf_verified': False,
        'binary_path': '',
        'source_dir': '',
        'pid': None,
        'process_name': '',
        'frequency': 99,
        'duration': 8,
    }


@dataclasses.dataclass
class AppContext:
    """Everything the server needs, in one place. Built once in main()
    (or by the test fixtures) and shared by the HTTP layer and the
    worker threads."""

    config: ServerConfig
    state: ProfilingState
    metrics: MetricsState
    agent: AgentSlot = dataclasses.field(default_factory=AgentSlot)
    wizard: dict = dataclasses.field(default_factory=default_wizard_state)
    # SSE sinks — the HTTP layer registers its fan-out here at startup.
    # Broadcasts before registration (or with no browsers) are no-ops.
    _sse_sinks: list = dataclasses.field(default_factory=list)

    def register_sse_sink(self, fn):
        """Register a callable(event_type, data) that delivers SSE events.
        Must be safe to call from any thread."""
        self._sse_sinks.append(fn)

    def broadcast(self, event_type, data):
        """Send an SSE event to all connected browsers. Thread-safe."""
        for fn in list(self._sse_sinks):
            try:
                fn(event_type, data)
            except Exception as e:
                print(f"[server] SSE sink error: {e}", file=sys.stderr)

    def update_wizard(self, updates):
        """Merge updates into wizard state."""
        self.wizard.update(updates)
        return self.wizard


def build_context(cfg):
    """Create an AppContext (state + metrics + source mapper) from config."""
    ctx = AppContext(
        config=cfg,
        state=ProfilingState(max_samples=cfg.max_samples),
        metrics=MetricsState(),
    )
    ctx.state.source_mapper = create_source_mapper(cfg)
    return ctx


def main(argv=None):
    cfg = config_from_args(argv)

    os.makedirs(cfg.sessions_dir, exist_ok=True)

    if not os.path.isdir(cfg.ui_dir):
        print(f"[server] Warning: UI directory not found at {cfg.ui_dir}",
              file=sys.stderr)

    # Probe tools, then build the shared context (the SourceMapper it
    # creates lives for the entire server lifetime).
    probe_tools(cfg)
    ctx = build_context(cfg)

    # CLI import: parse perf.data at startup
    if cfg.import_file:
        from perflens.sessions import import_perf_data
        if not os.path.isfile(cfg.import_file):
            print(f"[server] Error: import file not found: {cfg.import_file}",
                  file=sys.stderr)
            sys.exit(1)
        try:
            session_id, samples, metadata = import_perf_data(cfg,
                                                             cfg.import_file)
            # Load into live state so UI shows data immediately
            ctx.state.add_samples(samples)
            print(f"[server] Imported {len(samples)} samples as session "
                  f"{session_id}", file=sys.stderr)
        except RuntimeError as e:
            print(f"[server] Import failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Background rebuild worker (builds per_event data off recv threads)
    threading.Thread(target=rebuild_worker, args=(ctx,), daemon=True).start()

    # Agent TCP server
    threading.Thread(target=run_tcp_server, args=(ctx,), daemon=True).start()

    # Run the HTTP server (FastAPI/uvicorn) in the main thread. Imported
    # here to keep module import light and cycle-free.
    from perflens.web import run_http_server
    run_http_server(ctx)


if __name__ == '__main__':
    main()
