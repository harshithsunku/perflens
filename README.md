<p align="center">
  <img src="docs/hero.svg" alt="PerfLens — real-time Linux perf profiling in your browser" width="100%"/>
</p>

<p align="center">
  <a href="https://harshithsunku.github.io/perflens/"><img alt="docs site" src="https://img.shields.io/badge/docs-online-38bdf8?style=flat-square&logo=readthedocs&logoColor=white"/></a>
  <a href="https://github.com/harshithsunku/perflens/actions/workflows/build.yml"><img alt="build" src="https://github.com/harshithsunku/perflens/actions/workflows/build.yml/badge.svg?branch=master"/></a>
  <a href="https://pypi.org/project/perflens/"><img alt="PyPI" src="https://img.shields.io/pypi/v/perflens?style=flat-square&color=3775a9&logo=pypi&logoColor=white"/></a>
  <a href="https://github.com/harshithsunku/perflens/releases/latest"><img alt="release" src="https://img.shields.io/github/v/release/harshithsunku/perflens?style=flat-square&color=blue"/></a>
  <a href="https://github.com/harshithsunku/perflens/stargazers"><img alt="stars" src="https://img.shields.io/github/stars/harshithsunku/perflens?style=flat-square&color=fbbf24"/></a>
  <a href="#quick-start"><img alt="quick start" src="https://img.shields.io/badge/quick_start-60s-3fb950?style=flat-square"/></a>
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square"/>
  <img alt="agent" src="https://img.shields.io/badge/agent-static_C_binary-58a6ff?style=flat-square"/>
  <img alt="arch" src="https://img.shields.io/badge/arch-x86__64_%7C_aarch64-c084fc?style=flat-square"/>
  <img alt="wire" src="https://img.shields.io/badge/wire-zstd_%7C_5--byte_header-d29922?style=flat-square"/>
  <img alt="install" src="https://img.shields.io/badge/install-uvx_perflens-f85149?style=flat-square"/>
</p>

<p align="center">
  <strong>📖 <a href="https://harshithsunku.github.io/perflens/">Read the documentation site →</a></strong><br>
  <sub>Hosted on GitHub Pages — features, architecture deep-dive, CLI &amp; HTTP API reference, live UI tour.</sub>
</p>

<p align="center">
  <img src="docs/demo.gif" alt="Live demo: function table updating in real time as perf samples stream in, then flame graph, then source view" width="100%"/>
  <br><sub><em>Sample counts climb live as <code>perf record</code> rounds stream in. Flip to flame graph, click a function, drop into source with line-level heat. Zero polling — Server-Sent Events.</em></sub>
</p>

# PerfLens

**PerfLens** is a remote Linux performance profiler with a real-time web UI. Drop the agent on any Linux device (ARM or x86), point it at a PID, and watch flame graphs, function tables, `perf stat` metrics, and line-level annotated source update live in your browser.

No Docker, no sudo. A modern React + TypeScript UI shipped **prebuilt** inside the Python wheel (end users never need Node), and a single static C agent binary (~2 MB) with zero runtime dependencies — it runs on anything from bare-metal embedded boards to servers, installs with one curl command, and updates itself with `--update`.

---

## Highlights

- **Real-time streaming** — `perf record` runs in ~8s rounds; each round is compressed with zstd and streamed over a 5-byte framed TCP protocol
- **Live web UI** — Server-Sent Events push parsed function tables, flame graphs, and `perf stat` panels to the browser as new data arrives
- **Source-level annotation** — `addr2line` maps samples back to source lines; the UI heat-colors hot lines red/amber/green
- **Differential profiling** — snapshot a baseline (or pick a saved session) and the flame graph recolors by change (red grew, blue shrank) while the function table shows per-function Δ; did-my-fix-help in one glance
- **Timeline scrubbing** — drag across a Device Health sparkline to rebuild the flame graph and function table from only the samples collected in that window (e.g. select a CPU spike)
- **Per-thread profiling** — filter flame graphs, function tables, and source annotations by thread; dedicated thread analysis view with per-thread CPU breakdown, plus an optional real-time Live CPU column fed by the agent
- **Device health strip** — live CPU, memory, temperature, load, and network sparklines; opt-in disk I/O and per-thread CPU collectors (off by default to stay light on embedded targets) toggled from the UI at runtime
- **Interactive SVG flame graphs** — hand-rolled layout engine (no d3); ancestry zoom, regex search, diff coloring, hover details
- **Shareable URLs** — tab, event, thread filter, flame-graph zoom, and replayed session live in the URL hash; refresh or paste a link and land on the same view
- **Cross-compilation toolchain support** — `--toolchain-prefix` derives addr2line and readelf from a single prefix; `--sysroot` resolves shared libraries and source files under a sysroot tree
- **ARM + x86** — same agent code runs on aarch64, aarch64_be, armv7, armeb, x86_64
- **Session save / replay** — raw chunks saved to disk, replayed lazily on demand via the UI's session list
- **Static C agent** — single binary with vendored zstd, no runtime dependencies; cross-compiles to aarch64, aarch64_be, armv7, armeb, x86_64; one-line curl install and built-in self-update
- **Zero-friction server install** — `uvx perflens` (or `pipx` / `pip install --user`); everything resolves user-space, no sudo, corporate-machine friendly. Missing binutils? `perflens provision` downloads static addr2line/readelf into `~/.perflens/bin`
- **Capability probing** — the agent discovers which perf events and call-graph modes (`fp` / `dwarf` / `lbr`) actually work on the target before collecting
- **Zstd compression** — typical perf script payloads compress 20–40× before hitting the wire

---

## Architecture

<p align="center">
  <img src="docs/architecture.svg" alt="PerfLens architecture" width="100%"/>
</p>

The pipeline in one sentence: **`perf record` → agent → TCP+zstd → server → parser → source mapper → SSE → browser.**

### Target device

- The agent probes the kernel's `perf_event_paranoid`, enumerates candidate events (`cycles`, `instructions`, `cache-*`, `branch-*`, `page-faults`, `context-switches`, `cpu-migrations`), tries call-graph modes in order (`fp`, `dwarf`, `lbr`), and picks the first that produces non-empty stacks
- Each collection round runs `perf record` and `perf stat` in parallel for N seconds, then `perf script` to flatten the output
- The combined text is compressed with in-process zstd (level 1) and framed with a 5-byte header
- Reconnects with exponential backoff if the server drops
- Single static binary — **no Python, no libc, no zstd needed on the target**. Suitable for old or minimal ARM/x86 Linux devices.

### Local machine

- `perflens serve` runs a FastAPI/uvicorn HTTP layer (`web.py`) in front of a plain-threads agent side (`agentlink.py`: TCP listener and recv loops; `state.py`: aggregation worker); one agent at a time, any number of SSE browser clients
- `parser.py` parses `perf script` and `perf stat` text into per-event sample lists; `aggregator.py` folds each new chunk incrementally into function summaries and flame graph trees (O(new samples) per chunk, not O(total))
- `source_mapper.py` pipelines addresses through `addr2line` in batches of 500, applies compile-time path prefix rewrites, and builds annotated source views; `symcache.py` persists resolutions and source-file indexes under `~/.perflens/cache` so warm restarts skip the work
- A single `SourceMapper` is created at startup and shared across requests — no per-request forking
- Sessions are spooled to disk as compressed chunks while streaming and replayed lazily on demand (with a config-keyed replay cache) when the user opens them from the UI

---

## Wire protocol

<p align="center">
  <img src="docs/wire-protocol.svg" alt="PerfLens wire protocol: 5-byte header + payload" width="100%"/>
</p>

Every message is a 5-byte header followed by a payload of exactly `LEN` bytes:

```python
header = struct.pack('!IB', len(payload), flag)
sock.sendall(header + payload)
```

| Field | Size | Meaning |
|-------|------|---------|
| `LEN` | 4 bytes (uint32, big-endian) | Payload length in bytes |
| `FLAG` | 1 byte (uint8) | Frame type (see below) |
| `PAYLOAD` | `LEN` bytes | Perf data, JSON command/response, or JSON metrics |

The protocol is bidirectional — data and health metrics flow agent → server, commands flow server → agent over the same socket:

| Flag | Direction | Payload |
|------|-----------|---------|
| `0` | agent → server | Raw `perf script` text, optionally followed by a `### PERF_STAT ###` section |
| `1` | agent → server | Same, zstd-compressed |
| `2` | server → agent | Command request (JSON: `start`, `stop`, `pause`, `resume`, `configure`, ...) |
| `3` | agent → server | Command response / `hello` handshake (JSON) |
| `4` | agent → server | Device health metrics (JSON, every 2s: CPU, memory, temperature, network, per-process stats; opt-in disk I/O and per-thread CPU via `configure_metrics`) |

The server reads the 5 header bytes first, then exactly `LEN` more. Compression is in-process zstd on both ends (vendored in the agent, the `zstandard` package on the server, external `zstd` binary as a fallback). Typical ratio on real `perf script` output is **20–40×**.

---

## Quick Start

### Option A — install with uv/pip (recommended)

```bash
# On the machine where you want to view profiles (Python 3.10+, no sudo):
uvx perflens serve \
    --source-dir /path/to/sources \
    --binary     /path/to/unstripped-binary
# → http://localhost:8080

# Equivalent alternatives:
#   pipx install perflens          then: perflens serve ...
#   pip install --user perflens    then: perflens serve ...
```

```bash
# On the target Linux device — one-line install (no sudo, ~/.perflens/bin):
curl -fsSL https://raw.githubusercontent.com/harshithsunku/perflens/master/install-agent.sh | sh

# Option 1: agent connects to server
~/.perflens/bin/perflens-agent --server <server-ip>

# Option 2: agent listens, server connects to agent
~/.perflens/bin/perflens-agent --listen
# Then use the Live Debug wizard in the UI to connect to <device-ip>:9999

# Update later (downloads, verifies, atomically replaces itself):
~/.perflens/bin/perflens-agent --update

# Or push the agent to the device from your machine (ssh arch-detect):
perflens push-agent user@device
```

Release assets published on every tagged release:

| Asset | What it is |
|-------|------------|
| `perflens-<ver>-py3-none-any.whl` | Server — Python wheel (`uvx --from ...`) |
| `perflens-agent-linux-x86_64` | Agent — static binary, Linux x86_64 |
| `perflens-agent-linux-aarch64` | Agent — static binary, Linux aarch64 |
| `perflens-agent-linux-aarch64_be` | Agent — static binary, Linux aarch64 BE |
| `perflens-agent-linux-armv7` | Agent — static binary, Linux armv7 (32-bit LE) |
| `perflens-agent-linux-armeb` | Agent — static binary, Linux armv7 BE |

The asset suffix is the normalized arch, not `uname -m`: a device reporting `armv7l` fetches `perflens-agent-linux-armv7`.
| `perflens-tools-linux-{x86_64,aarch64}.tar.gz` | Static addr2line+readelf for `perflens provision` |

### Option B — build the agent yourself

```bash
# Build (on your build machine)
cd agent-c
make                                    # native x86_64
make CROSS=aarch64-linux-gnu-           # ARM64 little-endian
make CROSS=aarch64_be-linux-musl-       # ARM64 big-endian
make CROSS=arm-linux-gnueabihf-         # ARMv7 little-endian
make CROSS=armeb-linux-musleabihf-      # ARMv7 big-endian

# Deploy (single file, no dependencies)
scp perflens-agent user@device:/tmp/
ssh user@device
/tmp/perflens-agent --server <server-ip>        # connects to server
/tmp/perflens-agent --listen                     # or: wait for server to connect in
```

The agent is a single static binary (~2 MB) with zstd built in.

### Option C — from source (dev / contributors)

```bash
# Server (editable install pulls fastapi/uvicorn/orjson/zstandard)
uv venv && uv pip install -e .
.venv/bin/perflens serve \
    --source-dir /path/to/source \
    --binary     /path/to/myprogram \
    --port       9999 \
    --http-port  8080

# Agent (on the target device — build once, copy the binary)
cd agent-c && make && scp perflens-agent user@device:/tmp/
ssh user@device
/tmp/perflens-agent --server <server-ip>   # connects to server
/tmp/perflens-agent --listen                # or: wait for server
```

Then browse to `http://<server-ip>:8080`.

### Prerequisites

| Component | Needs |
|-----------|-------|
| **Target device** | Linux and `perf` — nothing else (the static agent has zstd built in) |
| **Local machine** | Python 3.10+ and `uv`/`pip`. `addr2line`/`readelf` from binutils for source mapping — if missing, `perflens provision` downloads static builds into `~/.perflens/bin` (no sudo). For cross-compiled targets: a matching toolchain with `<prefix>addr2line` and `<prefix>readelf` |
| **Binary** | Compiled with `-g` (debug symbols), not stripped |
| **Source** | A checkout of the source tree readable from the server machine |

---

## Configuration

### Server CLI

| Option | Default | Description |
|---|---|---|
| `--port PORT` | `9999` | TCP port the agent connects to |
| `--http-port PORT` | `8080` | HTTP port for the web UI |
| `--source-dir DIR` | `.` | Root of the source tree for line annotation |
| `--binary PATH` | — | Unstripped binary (enables `addr2line`) |
| `--map PATH` | — | GNU ld linker map file (optional symbol fallback) |
| `--path-map FROM=TO` | — | Rewrite compile-time paths to local paths (e.g. `/build/src=/home/user/src`) |
| `--addr2line PATH` | — | Custom `addr2line` binary (overrides `bin/` and PATH) |
| `--readelf PATH` | — | Custom `readelf` binary |
| `--toolchain-prefix PREFIX` | — | Cross-compilation prefix (e.g. `arm-linux-gnueabihf-`); derives addr2line and readelf |
| `--sysroot DIR` | — | Sysroot for resolving shared library modules and source files |
| `--max-samples N` | `500000` | Raw-sample ring buffer cap (aggregates always cover the full session). Costs ~1.7 KB RSS per sample — the default plateaus near 1.1 GB on a busy target |
| `--sessions-dir DIR` | `~/.perflens/sessions` | Where saved sessions are stored (`PERFLENS_HOME` moves the whole `~/.perflens` root) |
| `--http-bind ADDR` | `127.0.0.1` | Web UI bind address (`0.0.0.0` to expose — the UI has no auth) |
| `--browse-root DIR` | `~` | Directory the wizard's file picker is confined to |
| `--token SECRET` | — | Shared secret agents must present (or `PERFLENS_TOKEN`) |
| `--inline` / `--no-inline` | on | Enable/disable inline function resolution via `addr2line -i` |
| `--import FILE` | — | Import a `perf.data` file at startup and make it available as a session |

### Agent CLI

Three run modes (must pick one):

| Mode | Description |
|---|---|
| `--listen` | Daemon: bind `--port`, wait for server to connect in |
| `--server HOST` | Daemon: connect out to server (reconnects with exponential backoff) |
| `--output FILE` | Headless: collect once, write to file (`-` for stdout). Requires `--pid`. |

Options:

| Option | Default | Description |
|---|---|---|
| `--pid PID` | — | PID of process to profile (required for `--output`; set via UI wizard in daemon modes) |
| `--port PORT` | `9999` | TCP port (listen or connect) |
| `--frequency HZ` | `99` | `perf record -F` sampling frequency |
| `--duration SECS` | `8` | Length of each collection round |
| `--rounds N` | `1` | Number of collection rounds (`--output` mode only) |
| `--token SECRET` | — | Shared secret sent to the server in the hello (or `PERFLENS_TOKEN`) |
| `--update` | — | Self-update from the latest GitHub release, then exit |
| `--version` | — | Print version and exit |

---

## HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Server + agent connection state, sample totals |
| `/api/stream` | GET | Server-Sent Events: `status`, `agent`, `data_version` (per chunk, carries event types), `perf_stat`, `metrics` |
| `/api/snapshot?event=<evt>` | GET | Cached per-event snapshot (gzip); clients fetch it when SSE `data_version` bumps |
| `/api/sessions?offset=&limit=` | GET | List saved sessions (metadata only, paginated) |
| `/api/sessions/<id>` | GET | Lazy-replay a session (parses raw chunks on demand, cached) |
| `/api/sessions/<id>` | DELETE | Delete a saved session from disk |
| `/api/sessions/<id>/export?format=` | GET | Export a session: `collapsed` (FlameGraph stacks), `json`, or `svg` flame graph (`&event=`) |
| `/api/sessions/import` | POST | Import an uploaded `perf.data` file as a session (needs `perf` on the server) |
| `/api/live/export?format=` | GET | Export the live in-memory profile (same formats) |
| `/api/source?file=<path>&event=<evt>&tid=<tid>` | GET | Annotated source for a single file (optionally filtered by thread) |
| `/api/threads?event=<evt>` | GET | Thread overview: all threads with sample counts and top functions |
| `/api/threads/<tid>?event=<evt>` | GET | Per-thread flamegraph and function summary |
| `/api/window?event=&start=&end=` | GET | Flame graph + function summary for samples received in a time range (timeline scrubbing) |
| `/api/index/status` | GET | Source-index / DWARF file-list state (truncated preview) |
| `/api/index/files?offset=&limit=&q=` | GET | Paginated DWARF source-file list |
| `/api/metrics/current` | GET | Latest device health metrics per type |
| `/api/metrics/history?type=&start=` | GET | Health metrics time series |
| `/api/agent` | GET | Agent connection info (address, hello/platform) |
| `/api/agent` | DELETE | Disconnect the active agent (triggers normal session save) |
| `/api/agent/connect` | POST | Connect out to a `--listen` agent (`{"host": ..., "port": ...}`) |
| `/api/agent/command` | POST | Send a command to the connected agent (`start`, `stop`, `pause`, ...) |
| `/api/wizard` | GET/PUT | Persisted Live Debug wizard state |
| `/api/browse?path=` | GET | File picker listing (confined to `--browse-root`) |
| `/api/config` | GET/PATCH | Runtime binary/source/path-map/toolchain configuration (one typed model) |
| `/*` | GET | Static files from `ui/` |

Errors are uniform: every failure responds
`{"error": {"code": "<slug>", "message": "..."}}` with a real status code
(400 validation, 403 permission, 404 missing, 409 wrong server state,
413 too large, 502 agent transport).

---

## MCP server — profiling data for LLM agents

`perflens mcp` exposes the profiling data to any MCP client (Claude Code,
Claude Desktop, …) so an agent can answer "why is this slow" against real
`perf` data — and, when asked, drive a live run on a device.

It is a client of the HTTP API above, so a `perflens serve` must be
running. The MCP SDK is an optional dependency:

```bash
pip install 'perflens[mcp]'      # or: uv tool install 'perflens[mcp]'
```

Register it with your client — for Claude Code:

```bash
claude mcp add perflens -- perflens mcp
```

or in an MCP client config file:

```json
{
  "mcpServers": {
    "perflens": { "command": "perflens", "args": ["mcp"] }
  }
}
```

| Flag | Meaning |
|------|---------|
| `--server-url URL` | PerfLens HTTP API to query (default `$PERFLENS_MCP_URL` or `http://127.0.0.1:8080`) |
| `--read-only` | Omit the agent-control and export tools, so the agent can analyse but cannot touch a device or write files |

**Tools.** Analysis (read-only): `perflens_status`, `perflens_list_sessions`,
`perflens_hot_functions`, `perflens_hot_stacks`, `perflens_perf_stat`,
`perflens_list_source_files`, `perflens_source_hotlines`,
`perflens_compare`, `perflens_threads`, `perflens_thread_detail`,
`perflens_device_metrics`, `perflens_metrics_history`. Device control:
`perflens_agent_info`, `perflens_agent_connect`, `perflens_list_processes`,
`perflens_start_profiling`, `perflens_stop_profiling`,
`perflens_collection_pause`. Plus `perflens_export`, which writes collapsed
stacks / JSON / SVG to a file.

Profiles are large — a single event's snapshot runs from kilobytes to
megabytes — so every tool returns a ranked, capped view and tells the agent
exactly how to page for more. Config mutation, session deletion and the
filesystem browser are deliberately **not** exposed.

**Companion skill.** [`skills/perflens-profiling/`](skills/perflens-profiling/SKILL.md)
teaches an agent the method these tools support — orient, pick the right
event, read self *and* total, drill to hot source lines, corroborate with
IPC and device health, and the pitfalls that produce confidently wrong
answers. Install it wherever you profile:

```bash
cp -r skills/perflens-profiling ~/.claude/skills/
```

---

## Supported perf events

| Event | Typical use | Mode |
|-------|-------------|------|
| `cycles` | CPU time / hot paths | record + stat |
| `instructions` | IPC, retired instruction count | record + stat |
| `cache-misses` | Last-level cache misses | record + stat |
| `cache-references` | LLC accesses | record + stat |
| `branch-misses` | Branch prediction misses | record + stat |
| `branch-instructions` | Total branches | record + stat |
| `page-faults` | Minor/major page faults | stat only |
| `context-switches` | Scheduling pressure | stat only |
| `cpu-migrations` | Inter-CPU movement | stat only |

The agent probes each event before use and only emits the ones the kernel actually supports.

---

## Building release packages

```bash
./build_package.sh              # server wheel/sdist + native C agent
./build_package.sh --server     # Python wheel + sdist only
./build_package.sh --agent-c    # C agent only (native static binary)
```

Output lands in `dist/`:

```
dist/
├── perflens-<ver>-py3-none-any.whl     # server (uvx / pipx / pip)
├── perflens-<ver>.tar.gz               # server sdist
├── perflens-agent-c-<ver>.tar.gz       # agent tarball
└── perflens-agent-linux-<arch>         # agent raw binary (stable name)
```

### CI

[`.github/workflows/test.yml`](.github/workflows/test.yml) runs the pytest suite on Python 3.10–3.13 (parser, aggregator differentials against device-captured fixtures, source mapper, HTTP API, MCP tools, provisioning against a fake release server, and the C-agent wire protocol driven through a fake framing server with a `perf` shim), and gates every PR on `ruff` and `tools/check_version.py`. A frontend job adds the OpenAPI schema drift check, the TypeScript typegen drift check, vitest unit tests, a self-contained Playwright browser E2E (`frontend/e2e/`) that replays a fixture session through the real UI, and a smoke run of the docs screenshot harness (`frontend/docs-shots/`) that asserts the images come out non-blank.

[`.github/workflows/build.yml`](.github/workflows/build.yml) lints (`ruff`), runs the pytest suite, builds and smoke-runs the Python wheel (with a wheel-contents check), builds the static C agent for five architectures (x86_64, aarch64, aarch64_be, armv7, armeb), and builds static addr2line/readelf tools bundles (x86_64, aarch64) for `perflens provision`. Big-endian agent targets use musl toolchains from musl.cc since Ubuntu only ships little-endian sysroots. Tagged pushes (`v*`) create a GitHub Release and attach all artifacts — including raw `perflens-agent-linux-<arch>` binaries with stable names that `install-agent.sh` and the agent's `--update` fetch from `releases/latest/download/`. Tagged pushes also publish the package to [PyPI](https://pypi.org/project/perflens/) via Trusted Publishing (OIDC — no stored tokens).

---

## Project layout

```
perflens/
├── install-agent.sh              # curl-able agent installer (arch detect, no sudo)
├── agent-c/
│   ├── src/                      # C agent modules (agent.h + 10 .c files, static binary, zero deps)
│   ├── Makefile                  # native + cross-compile targets
│   └── vendor/zstd/              # vendored zstd amalgamation
├── pyproject.toml                # pip/uv package (console script: perflens)
├── src/perflens/                 # the server package
│   ├── app.py                    # AppContext + lifecycle + main()
│   ├── agentlink.py              # agent TCP wire protocol + AgentSession
│   ├── state.py                  # profiling/metrics state + rebuild worker
│   ├── sessions.py               # session persistence, replay, perf.data import
│   ├── web.py                    # FastAPI/uvicorn HTTP layer + SSE hub
│   ├── cli.py                    # perflens serve/import/push-agent/provision/mcp
│   ├── mcp/                      # MCP server (optional extra) — tools over the HTTP API
│   ├── parser.py                 # perf script / perf stat parser
│   ├── aggregator.py             # incremental per-event aggregation
│   ├── source_mapper.py          # addr2line pipeline + path remap
│   ├── symcache.py               # persistent caches (~/.perflens/cache)
│   ├── provision.py              # user-space static-tools download
│   └── ui/                       # built React app (Vite output; ships in the wheel)
├── frontend/                     # React + TypeScript + Vite UI source
├── skills/perflens-profiling/    # agent skill for the MCP server (SKILL.md)
├── docs/
│   ├── hero.svg
│   ├── architecture.svg
│   └── wire-protocol.svg
├── tests/
│   ├── conftest.py               # shared fixtures (device-captured sessions)
│   ├── test_*.py                 # pytest suite (parser, aggregator, HTTP, MCP, agent, ...)
│   ├── fixtures/                 # gzipped perf sessions from real devices
│   ├── sample_workload.c         # multi-function test program
│   └── Makefile                  # gcc -g -O0 -lm
├── build_package.sh              # local wheel + agent builds
├── .github/workflows/test.yml    # pytest matrix + browser e2e
├── .github/workflows/build.yml   # lint + test + wheel + agents + release
├── VERSION
├── LICENSE (MIT)
└── README.md (this file)
```

---

## Troubleshooting

**`perf_event_paranoid` too high.** The agent warns at startup if `/proc/sys/kernel/perf_event_paranoid > 1` and the UI may show limited events.

```bash
sudo sysctl -w kernel.perf_event_paranoid=1
```

**No function names.** Compile with `-g` and do not strip. `file ./myprogram` should say `not stripped` and `with debug_info`.

**No source line mapping.** Double-check `--binary` points at the exact unstripped binary running on the target and `--source-dir` contains the source files. Use `--path-map /build/src=/home/me/src` when your build was done under a different root.

**Agent can't connect.** The server must be reachable on `--port`. Check with `nc -zv <server-ip> 9999`.

**Container: one of the two `perf record` modes fails.** Which one depends on the container, so probe rather than assume. Some environments strip the perf capability set: `-p <pid>` returns empty and a system-wide `perf record -a` works. An unprivileged LXC container is the opposite case — at `perf_event_paranoid=1`, per-PID recording works and `-a` fails with "Failure to open any events for recording". `perf_event_paranoid` is not namespaced, so it is read-only from inside the container and lowering it requires the host.

**Call-graph probing hangs / slow startup.** Call-graph probing tests `fp`, `dwarf`, then `lbr` in sequence — this adds ~10-20 s on a typical target, longer on slow or hybrid-CPU hardware on first connection. Normal.

---

## Design rules

These are the rules the project is built to:

- **Simplicity first** — a small, deliberate server stack (fastapi/uvicorn/orjson/zstandard, all user-space via uv); the React + TypeScript UI ships prebuilt in the wheel so npm is a contributor-only tool; the agent stays zero-dependency static C
- **Defensive parsing** — `perf` output format varies across kernel versions; parser is forgiving
- **No secrets in code** — generic and open-source-friendly
- **No over-engineering** — if it doesn't earn its complexity, it gets cut

See [`CLAUDE.md`](CLAUDE.md) for the full internal reference, or the [documentation site](https://harshithsunku.github.io/perflens/) for the polished version.

---

## License

MIT. See [`LICENSE`](LICENSE).
