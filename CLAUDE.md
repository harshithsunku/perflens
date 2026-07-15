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
                                        SSE  --->  Browser (app.js)
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
- Plain HTML + vanilla JS + CSS for the UI. No bundler, no framework.
- HTTP layer (`web.py`): FastAPI on uvicorn. Every handler is a direct
  port of the original stdlib implementation — URL paths and JSON shapes
  are frozen (the UI depends on them). SSE fan-out is an asyncio hub;
  worker threads publish via `loop.call_soon_threadsafe`.
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
- Single agent implementation: a static C binary (~2 MB, vendored zstd,
  zero deps) that cross-compiles for five architectures, installs with one
  curl command (install-agent.sh), and self-updates with --update.
- Bidirectional interactive protocol: agent sends hello + data + metrics,
  server sends commands (start, stop, pause, resume, configure, etc.).
- Two connection patterns: `--server` (agent connects out to server) and
  `--listen` (agent binds port, server/UI connects in via wizard).
- Agent collects device health metrics (CPU, memory, temperature, load,
  process stats, network) every 2s and streams them as JSON frames.
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
│   ├── perflens_agent.c          # C agent (~3200 lines, static binary)
│   ├── Makefile                  # native + cross-compile targets
│   └── vendor/zstd/              # zstd single-file amalgamation
├── pyproject.toml                # pip/uv package (console script: perflens)
├── src/perflens/                 # the server package
│   ├── server.py                 # agent TCP protocol + state + sessions
│   ├── web.py                    # FastAPI/uvicorn HTTP layer + SSE hub
│   ├── cli.py                    # perflens serve / import / push-agent
│   ├── parser.py                 # perf script / perf stat parser
│   ├── aggregator.py             # incremental per-event aggregation
│   ├── source_mapper.py          # addr2line pipeline + path remap
│   ├── symcache.py               # persistent caches (~/.perflens/cache)
│   └── ui/                       # single-page app (ships in the wheel)
├── server/perflens_server.py     # compat shim (one release)
├── docs/
│   ├── hero.svg
│   ├── architecture.svg
│   └── wire-protocol.svg
├── test/
│   ├── sample_workload.c
│   └── Makefile
├── build_package.sh
├── .github/workflows/build.yml
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
| `/api/stream`             | GET    | SSE: `status`, `event_types`, `per_event`, `perf_stat` |
| `/api/sessions`           | GET    | List saved sessions                             |
| `/api/sessions/<id>`      | GET    | Lazy-replay a session from saved chunks         |
| `/api/source?file=&event=&tid=` | GET | Annotated source (optionally per-thread)        |
| `/api/thread-view?event=&tid=`  | GET | Per-thread flamegraph + function summary         |
| `/api/thread-summary?event=`    | GET | Thread overview with sample counts and top funcs |
| `/api/config/toolchain`         | POST| Set toolchain prefix and sysroot at runtime      |
| `/api/per-event?event=`   | GET    | Cached per-event snapshot (gzip); pairs with SSE `data_version` |
| `/api/index/files?offset=&limit=&q=` | GET | Paginated DWARF source-file list            |
| `/api/stop`               | GET    | Disconnect the active agent                     |
| `/*`                      | GET    | Static files from `ui/`                         |

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
--max-samples N       Ring buffer cap             (default 500000)
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

- **Simplicity first.** Stdlib Python, plain HTML/JS/CSS, no frameworks.
- **Defensive parsing.** `perf` output format varies across kernel versions;
  the parser is forgiving.
- **Generic.** No proprietary names, no IPs, no credentials, no company
  references in code, docs, commit messages, or history.
- **No over-engineering.** If a piece of code doesn't earn its complexity,
  it gets cut.
- **Test end-to-end before committing.** Server + agent + browser UI on a
  real Linux target.

---

## Known limitations

- Single agent connection at a time — a new agent replaces the current one.
- `perf_event_paranoid > 1` may restrict the set of usable events (the agent
  warns at startup).
- Call-graph probing adds ~6–12 s to first-connection startup (tests `fp`,
  `dwarf`, `lbr` in sequence).
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
