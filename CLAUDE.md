# PerfLens — Project Reference

Real-time Linux performance profiler with a web UI. A device agent uses `perf`
to collect profiling data from a running process on a remote Linux device
(ARM or x86), streams it over TCP to a local Python server, which displays it
in a browser with line-level source annotation, interactive flame graphs, and
function-level breakdowns.

Generic open-source project — no proprietary names, no company-specific
references, no IPs, credentials, or secrets anywhere in the repo or its
history.

---

## Architecture

```
[Target device]                       [Local machine]
   Process (PID)                         Python server (perflens serve)
      |                                      |
   perf record + perf stat                   |
      |                                      |
   Agent (static C binary)                   |
      |                                      |
      +---- TCP (5-byte header + zstd) ----> recv + decompress
      |   (--server: agent connects out)     |
      |   (--listen: server connects in)     |
      |<--- commands (JSON) ----------------+
                                             |
                                        parser.py  (perf script / perf stat)
                                        source_mapper.py  (addr2line + source)
                                             |
                                        SSE  --->  Browser (React SPA)
```

### Wire protocol
- 5-byte header: 4-byte uint32 big-endian payload length + 1-byte flag.
- Flag values:
  - `0` = raw perf data (agent → server)
  - `1` = zstd-compressed perf data (agent → server)
  - `2` = command request JSON (server → agent)
  - `3` = command response JSON (agent → server)
  - `4` = health metrics JSON (agent → server)
- Perf data payload: UTF-8 perf script output, optionally followed by a
  `### PERF_STAT ###` section.
- Agent compresses with in-process zstd (vendored). Server decompresses
  in-process via the `zstandard` package (external `zstd` binary as
  fallback). Typical ratio 20–40×.

### Key design decisions
- The agent is a zero-dependency static C binary. The server is a normal
  Python package with a small, deliberate dependency set (fastapi, uvicorn,
  orjson, zstandard) — everything resolves user-space via `uvx perflens`
  or `pip install --user`; no sudo, no Docker, corporate-machine friendly.
- UI: React 19 + TypeScript + Vite single-page app in `frontend/`.
  The wheel ships the **prebuilt** static assets (Vite output lands in
  `src/perflens/ui/`, which is gitignored) — end users need only
  `uvx perflens` / pip; Node is a dev/CI-build-time dependency only.
  State: zustand (live session) + TanStack Query (fetches). Styling:
  the original CSS custom-property theme (`frontend/src/styles/theme.css`,
  `data-theme` dark/light); Tailwind 4 + Radix are installed for the
  upcoming visual overhaul.
- Type-safety bridge: Pydantic v2 models (`src/perflens/api/models.py`)
  → FastAPI OpenAPI (`/api/openapi.json`, exported to
  `frontend/openapi.json` by `tools/export_openapi.py`) →
  `npm run typegen` generates `frontend/src/api/types.gen.ts`. CI fails
  on schema drift, so keep the exported file in sync when models change.
- No module globals on the server: everything hangs off `AppContext`
  (`app.py`) — HTTP routes get it via `request.app.state.ctx`, worker
  threads receive it at construction.
- HTTP layer (`web.py`): FastAPI on uvicorn. SSE fan-out is an asyncio
  hub; worker threads publish via `loop.call_soon_threadsafe`. Live
  updates use notify-and-fetch: a tiny `data_version` SSE stamp, then the
  browser pulls `/api/snapshot` for the event it is viewing.
- The agent TCP listener, recv loops, and the aggregation rebuild worker
  are plain threads (blocking sockets + subprocess work); uvicorn owns
  only the HTTP side. Heavy request handlers are sync `def` routes that
  run in the threadpool.
- `addr2line -f` (or `-fi` with `--inline`) pipelined in batches of 500
  addresses.
- Session replay is lazy: raw chunks are saved to disk and parsed on demand.
- A single `SourceMapper` is created at server startup and shared across all
  requests — no per-request forking.
- The agent probes supported perf events and call-graph modes (`fp`, `dwarf`,
  `lbr`) on the target before collecting, and uses whichever works.
- Collection prefers continuous pipe mode (`perf record -o - | perf script
  -i -`, probed at startup): one long-lived pipeline with no sampling dead
  time, symbol tables parsed once, output cut into chunks every `duration`
  seconds at sample boundaries and streamed through in-process zstd. Falls
  back to discrete record/script rounds when pipe mode is unavailable.
  `perf script` runs at `nice 5` so the profiler yields to the workload.
- Single agent implementation: a static C binary (~2 MB, vendored zstd,
  zero deps) that cross-compiles for five architectures, installs with one
  curl command (install-agent.sh), and self-updates with --update.
- Bidirectional interactive protocol: agent sends hello + data + metrics,
  server sends commands (start, stop, pause, resume, configure, etc.).
  `start` accepts an optional `events` subset of the probed record events;
  the UI's control-bar popovers expose live profiling settings (frequency,
  interval, events — restarting collection transparently when needed),
  process switching, and metrics toggles.
- Two connection patterns: `--server` (agent connects out to server) and
  `--listen` (agent binds port, server/UI connects in via wizard).
- Agent collects device health metrics (CPU, memory, temperature, load,
  process stats, network) every 2s and streams them as JSON frames.
  Disk I/O metrics (per-device + per-process) and per-thread CPU metrics
  are opt-in — off by default to stay light on embedded targets, enabled
  at runtime by the server via `configure_metrics {"disk": true,
  "threads": true}` (UI: gear on the Device Health strip).
- Cross-compilation support: `--toolchain-prefix` derives both addr2line
  and readelf from a single prefix; `--sysroot` resolves module paths and
  source files under a sysroot tree (like `perf --symfs`).
- Per-thread profiling: the parser extracts `pid/tid` and `comm` from
  `perf script` output; the UI can filter flamegraphs, function tables,
  and source annotations by thread ID.

---

## File layout

```
perflens/
├── install-agent.sh              # curl-able agent installer (no sudo)
├── agent-c/
│   ├── src/                      # C agent modules (agent.h + 10 .c files)
│   │   ├── agent.h               # shared types, constants, cross-module API
│   │   ├── main.c                # agent state, session loop, run modes, CLI
│   │   ├── collect.c             # round + continuous collection loops
│   │   ├── commands.c            # command handlers + dispatch
│   │   ├── metrics.c             # device health metrics
│   │   ├── probe.c               # platform + perf capability probing
│   │   ├── subproc.c             # signals, child tracking, fork/exec helpers
│   │   ├── wire.c                # TCP framing + streaming zstd sink
│   │   └── util.c, procs.c, update.c
│   ├── Makefile                  # native + cross-compile targets
│   └── vendor/zstd/              # zstd single-file amalgamation
├── pyproject.toml                # pip/uv package (console script: perflens)
├── src/perflens/                 # the server package
│   ├── app.py                    # AppContext + lifecycle + main()
│   ├── config.py                 # ServerConfig, CLI parsing, tool probing
│   ├── state.py                  # ProfilingState, MetricsState, rebuild worker
│   ├── agentlink.py              # agent TCP wire protocol + AgentSession
│   ├── sessions.py               # session persistence, replay, perf.data import
│   ├── export.py                 # collapsed-stack + SVG flamegraph export
│   ├── web.py                    # FastAPI/uvicorn HTTP layer + SSE hub
│   ├── api/                      # Pydantic v2 schemas + response helpers
│   ├── server.py                 # compat shim (one release)
│   ├── cli.py                    # perflens serve/import/push-agent/provision
│   ├── parser.py                 # perf script / perf stat parser
│   ├── aggregator.py             # incremental per-event aggregation
│   ├── source_mapper.py          # addr2line pipeline + path remap
│   ├── symcache.py               # persistent caches (~/.perflens/cache)
│   ├── provision.py              # static addr2line/readelf download (~/.perflens/bin)
│   └── ui/                       # BUILT React app (gitignored Vite output;
│                                 #   ships in the wheel/sdist via hatch artifacts)
├── frontend/                     # React 19 + TypeScript + Vite SPA source
│   ├── src/api/                  # typed client, SSE wiring, types.gen.ts
│   ├── src/store/                # zustand stores + URL-hash deep links
│   ├── src/lib/flamegraph/       # pure layout/zoom/diff/color modules
│   ├── src/components/, src/views/
│   ├── e2e/                      # Playwright browser E2E (self-contained)
│   └── openapi.json              # committed schema (CI drift-checked)
├── server/perflens_server.py     # compat shim (one release)
├── tools/export_openapi.py       # dump OpenAPI schema for TS typegen
├── docs/
│   ├── hero.svg
│   ├── architecture.svg
│   └── wire-protocol.svg
├── tests/
│   ├── conftest.py               # shared pytest fixtures
│   ├── test_*.py                 # pytest suite (parser, aggregator, http, agent, ...)
│   ├── fixtures/                 # device-captured perf sessions (gzipped)
│   ├── sample_workload.c
│   └── Makefile
├── build_package.sh              # builds frontend (if needed) + wheel + agent
├── .github/workflows/test.yml    # pytest matrix + vitest + playwright e2e
├── .github/workflows/build.yml   # frontend build + wheel + release
├── VERSION
├── LICENSE (MIT)
├── README.md
└── CLAUDE.md                     # this file
```

---

## HTTP API

| Endpoint                  | Method | Description                                     |
|---------------------------|--------|-------------------------------------------------|
| `/api/status`             | GET    | Server + agent connection state, sample totals  |
| `/api/stream`             | GET    | SSE: `status`, `agent`, `data_version` (carries event types), `perf_stat`, `metrics` (typed by payload) |
| `/api/snapshot?event=`    | GET    | Cached per-event snapshot (gzip); pairs with SSE `data_version` |
| `/api/sessions?offset=&limit=` | GET | List saved sessions (paginated)               |
| `/api/sessions/<id>`      | GET    | Lazy-replay a session from saved chunks (cached); 404 when missing |
| `/api/sessions/<id>`      | DELETE | Delete a saved session                          |
| `/api/sessions/<id>/export?format=&event=` | GET | Export: `collapsed`, `json`, or `svg` |
| `/api/sessions/import`    | POST   | Import an uploaded `perf.data` as a session     |
| `/api/live/export?format=&event=` | GET | Export the live in-memory profile          |
| `/api/source?file=&event=&tid=` | GET | Annotated source (optionally per-thread); 404 when no data |
| `/api/threads?event=`     | GET    | Thread overview with sample counts and top funcs |
| `/api/threads/<tid>?event=` | GET  | Per-thread flamegraph + function summary        |
| `/api/window?event=&start=&end=&tid=` | GET | Flamegraph + functions for a received-time range (timeline scrubbing) |
| `/api/index/status`       | GET    | Source-index / DWARF list state (truncated)     |
| `/api/index/files?offset=&limit=&q=` | GET | Paginated DWARF source-file list            |
| `/api/metrics/current`    | GET    | Latest device health metrics per type           |
| `/api/metrics/history?type=&start=` | GET | Health metrics time series               |
| `/api/agent`              | GET    | Agent connection info (addr + hello/platform)   |
| `/api/agent`              | DELETE | Disconnect the active agent                     |
| `/api/agent/connect`      | POST   | Connect out to a `--listen` agent               |
| `/api/agent/command`      | POST   | Send a command to the connected agent (cmd names enforced) |
| `/api/wizard`             | GET/PUT | Persisted Live Debug wizard state              |
| `/api/browse?path=`       | GET    | File picker (confined to `--browse-root`)      |
| `/api/config`             | GET/PATCH | Runtime binary/source/pathmap/toolchain config (one typed model) |
| `/*`                      | GET    | Static files from `ui/`                         |

Error model: every failure is `{"error": {"code": "<slug>", "message":
"..."}}` with a real status code (400 validation, 403 permission, 404
missing, 409 wrong server state, 413 too large, 502 agent transport).

---

## Server CLI

```
--port PORT           TCP port for agent          (default 9999)
--http-port PORT      HTTP port for web UI        (default 8080)
--source-dir DIR      Root of source tree         (default .)
--binary PATH         Unstripped binary for addr2line
--map PATH            GNU ld linker map file (optional symbol fallback)
--path-map FROM=TO    Rewrite compile-time paths to local paths
--addr2line PATH      Custom addr2line binary
--readelf PATH        Custom readelf binary
--toolchain-prefix P  Cross-compilation prefix (e.g. arm-linux-gnueabihf-)
--sysroot DIR         Sysroot for shared library and source resolution
--max-samples N       Raw-sample ring buffer cap  (default 500000)
--sessions-dir DIR    Saved-session location      (default ~/.perflens/sessions)
--http-bind ADDR      Web UI bind address         (default 127.0.0.1)
--browse-root DIR     File-picker confinement root (default: home dir)
--token SECRET        Shared secret agents must present (or PERFLENS_TOKEN)
--inline / --no-inline  Enable/disable inline function resolution (default: on)
--import FILE         Import a perf.data file at startup as a session
```

## Agent CLI

Three run modes (must pick one):

```
--listen              Daemon: bind port, wait for server to connect in
--server HOST         Daemon: connect out to server (reconnects with backoff)
--output FILE         Headless: collect once, write to FILE ('-' for stdout)
```

Options:

```
--pid PID             Process to profile (required for --output)
--port PORT           TCP port                    (default 9999)
--frequency HZ        perf record -F              (default 99)
--duration SECS       Length of each round         (default 8)
--rounds N            Number of rounds (--output mode only)
--token SECRET        Shared secret sent in hello (or PERFLENS_TOKEN env)
--update              Self-update from latest GitHub release, then exit
--version             Print version and exit
```

---

## Development rules

- **The agent is FROZEN.** Do not change `agent-c/` or the TCP wire
  protocol. Everything server-side of the socket is fair game.
- **Simplicity first.** A small, deliberate server dependency set
  (fastapi, uvicorn, orjson, zstandard, pydantic — all user-space).
  The UI is React + TS + Vite, but Node is dev/CI-only: the wheel ships
  prebuilt assets, so `uvx perflens` needs no npm at runtime; the agent
  stays zero-dependency static C.
- **Frontend dev workflow.** `perflens serve` on 8080 + `npm --prefix
  frontend run dev` (Vite on 5173, proxies `/api` incl. SSE). Build with
  `npm --prefix frontend run build` (emits into `src/perflens/ui/`).
  After changing `api/models.py` or routes: `python tools/export_openapi.py
  && npm --prefix frontend run typegen` (CI diff-checks both).
- **Defensive parsing.** `perf` output format varies across kernel versions;
  the parser is forgiving.
- **Generic.** No proprietary names, no IPs, no credentials, no company
  references in code, docs, commit messages, or history.
- **No over-engineering.** If a piece of code doesn't earn its complexity,
  it gets cut.
- **Test end-to-end before committing.** `pytest tests/`, `npm --prefix
  frontend run test` (vitest), and `npm --prefix frontend run e2e`
  (Playwright, self-contained: starts its own server + fixture session).
  For agent changes (rare, requires explicit approval): a real Linux
  target.

---

## Known limitations

- Single agent connection at a time — a new agent replaces the current one.
- `perf_event_paranoid > 1` may restrict the set of usable events (the agent
  warns at startup).
- Capability probing adds ~8–14 s to first-connection startup (events,
  call-graph modes `fp`/`dwarf`/`lbr`, script fields, pipe mode).
- In continuous mode, `perf record` flushes its ring buffer in batches, so
  the first chunk or two after `start` may carry only PERF_STAT data before
  samples begin flowing.
- Some container environments don't support `perf record -p <pid>`. The
  agent's per-PID mode returns empty in that case; a system-wide
  `perf record -a` usually works as a fallback.
- `addr2line` source mapping requires a binary compiled with `-g` debug
  symbols and not stripped.
- The source view renders up to ~2000 lines (or hottest line ± 100,
  whichever is larger).
- The parser handles `perf script` output from all kernel versions (2.6
  through 6.x), including optional `[cpu]`, `pid/tid`, and flags fields.
  The agent normalizes output with `perf script -F` when supported (perf
  >= ~3.12) and falls back to default format on older versions.
