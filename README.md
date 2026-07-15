<p align="center">
  <img src="docs/hero.svg" alt="PerfLens — real-time Linux perf profiling in your browser" width="100%"/>
</p>

<p align="center">
  <a href="https://harshithsunku.github.io/perflens/"><img alt="docs site" src="https://img.shields.io/badge/docs-online-38bdf8?style=flat-square&logo=readthedocs&logoColor=white"/></a>
  <a href="https://github.com/harshithsunku/perflens/actions/workflows/build.yml"><img alt="build" src="https://github.com/harshithsunku/perflens/actions/workflows/build.yml/badge.svg?branch=master"/></a>
  <a href="https://github.com/harshithsunku/perflens/releases/latest"><img alt="release" src="https://img.shields.io/github/v/release/harshithsunku/perflens?style=flat-square&color=blue"/></a>
  <a href="https://github.com/harshithsunku/perflens/stargazers"><img alt="stars" src="https://img.shields.io/github/stars/harshithsunku/perflens?style=flat-square&color=fbbf24"/></a>
  <a href="#quick-start"><img alt="quick start" src="https://img.shields.io/badge/quick_start-60s-3fb950?style=flat-square"/></a>
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square"/>
  <img alt="agent" src="https://img.shields.io/badge/agent-static_C_binary-58a6ff?style=flat-square"/>
  <img alt="arch" src="https://img.shields.io/badge/arch-x86__64_%7C_aarch64-c084fc?style=flat-square"/>
  <img alt="wire" src="https://img.shields.io/badge/wire-zstd_%7C_5--byte_header-d29922?style=flat-square"/>
  <img alt="deps" src="https://img.shields.io/badge/deps-stdlib_only-f85149?style=flat-square"/>
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

No frontend frameworks. No Docker. Plain HTML/CSS/JS for the UI, and a single static C agent binary (~2 MB) with zero runtime dependencies — it runs on anything from bare-metal embedded boards to servers, installs with one curl command, and updates itself with `--update`.

---

## Highlights

- **Real-time streaming** — `perf record` runs in ~8s rounds; each round is compressed with zstd and streamed over a 5-byte framed TCP protocol
- **Live web UI** — Server-Sent Events push parsed function tables, flame graphs, and `perf stat` panels to the browser as new data arrives
- **Source-level annotation** — `addr2line` maps samples back to source lines; the UI heat-colors hot lines red/amber/green
- **Per-thread profiling** — filter flame graphs, function tables, and source annotations by thread; dedicated thread analysis view with per-thread CPU breakdown
- **Interactive SVG flame graphs** — vanilla JS, no d3, no bundling; zoomable, hoverable
- **Cross-compilation toolchain support** — `--toolchain-prefix` derives addr2line and readelf from a single prefix; `--sysroot` resolves shared libraries and source files under a sysroot tree
- **ARM + x86** — same agent code runs on aarch64, aarch64_be, armv7l, x86_64
- **Session save / replay** — raw chunks saved to disk, replayed lazily on demand via the UI's session list
- **Static C agent** — single binary with vendored zstd, no runtime dependencies; cross-compiles to aarch64, aarch64_be, armv7l, armeb, x86_64; one-line curl install and built-in self-update
- **Portable release packages** — PyInstaller-frozen server for Linux/macOS/Windows, prebuilt static agent binaries for five architectures
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

- `perflens_server.py` (`ThreadingHTTPServer` + a TCP listener thread) accepts one agent at a time and broadcasts parsed state to any number of SSE clients
- `parser.py` parses `perf script` and `perf stat` text into per-event sample lists, function summaries, and flame graph trees
- `source_mapper.py` pipelines addresses through `addr2line` in batches of 500, applies compile-time path prefix rewrites, and builds annotated source views
- A single `SourceMapper` is created at startup and shared across requests — no per-request forking
- Sessions are saved as raw chunks on disk and rebuilt on demand when the user replays them from the UI

---

## Wire protocol

<p align="center">
  <img src="docs/wire-protocol.svg" alt="PerfLens wire protocol: 5-byte header + payload" width="100%"/>
</p>

Every message is a 5-byte header followed by a payload of exactly `LEN` bytes:

```python
header = struct.pack('!IB', len(payload), compression_flag)
sock.sendall(header + payload)
```

| Field | Size | Meaning |
|-------|------|---------|
| `LEN` | 4 bytes (uint32, big-endian) | Payload length in bytes |
| `FLAG` | 1 byte (uint8) | `0` = raw UTF-8, `1` = zstd-compressed |
| `PAYLOAD` | `LEN` bytes | `perf script` text, optionally followed by a `### PERF_STAT ###` section |

The server reads the 5 header bytes first, then exactly `LEN` more. Compressed frames are piped through `zstd -d -c`. Typical ratio on real `perf script` output is **20–40×**.

---

## Quick Start

### Option A — pre-built release tarballs (recommended)

```bash
# On the machine where you want to view profiles
tar xf perflens-server-<ver>-linux-x86_64.tar.gz
./perflens-server-<ver>/perflens-server \
    --source-dir /path/to/sources \
    --binary     /path/to/unstripped-binary
# → http://localhost:8080
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
```

Pre-built server tarballs are published on every tagged release for:

| Platform | Tarball |
|----------|---------|
| Linux x86_64 | `perflens-server-<ver>-linux-x86_64.tar.gz` |
| macOS arm64 (Apple Silicon) | `perflens-server-<ver>-macos-arm64.tar.gz` |
| Windows x86_64 | `perflens-server-<ver>-windows-x86_64.tar.gz` |
| Agent — Linux x86_64 (static binary) | `perflens-agent-linux-x86_64` |
| Agent — Linux aarch64 (static binary) | `perflens-agent-linux-aarch64` |
| Agent — Linux aarch64 BE (static binary) | `perflens-agent-linux-aarch64_be` |
| Agent — Linux armv7l (static binary) | `perflens-agent-linux-armv7l` |
| Agent — Linux armv7 BE (static binary) | `perflens-agent-linux-armeb` |

> **Intel Mac users:** GitHub retired the free `macos-13` runner, so there's no pre-built macOS x86_64 tarball. Either build from source (`./build_package.sh --no-freeze --server`) or run the Linux tarball under a VM/container.

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
# Server
python3 server/perflens_server.py \
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
| **Local machine** | Python 3.8+ (or frozen tarball), `addr2line` and `readelf` from binutils (bundled in `bin/` or on PATH), ideally `zstd` for decompression. For cross-compiled targets: a matching toolchain with `<prefix>addr2line` and `<prefix>readelf` |
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
| `--max-samples N` | `500000` | Ring buffer cap before oldest samples drop |
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
| `/api/stream` | GET | Server-Sent Events: `status`, `event_types`, `per_event`, `perf_stat` |
| `/api/sessions` | GET | List saved sessions (metadata only) |
| `/api/sessions/<id>` | GET | Lazy-replay a session (parses raw chunks on demand) |
| `/api/source?file=<path>&event=<evt>&tid=<tid>` | GET | Annotated source for a single file (optionally filtered by thread) |
| `/api/thread-view?event=<evt>&tid=<tid>` | GET | Per-thread flamegraph and function summary |
| `/api/thread-summary?event=<evt>` | GET | Thread overview: all threads with sample counts and top functions |
| `/api/config/toolchain` | POST | Set toolchain prefix and sysroot at runtime |
| `/api/stop` | GET | Disconnect the active agent (triggers normal session save) |
| `/*` | GET | Static files from `ui/` |

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
./build_package.sh              # frozen server + agent (PyInstaller)
./build_package.sh --server     # server only
./build_package.sh --agent      # agent only
./build_package.sh --no-freeze  # skip PyInstaller, ship raw Python
```

Output lands in `dist/`:

```
dist/
├── perflens-server-<ver>.tar.gz
└── perflens-agent-<ver>.tar.gz
```

Drop cross-compiled binaries into the right slots before building to ship a fully self-contained package:

```
server/bin/         zstd, addr2line, readelf           (server target OS)
agent/bin/aarch64/  zstd                               (ARM64 little-endian)
agent/bin/aarch64_be/ zstd                             (ARM64 big-endian)
agent/bin/armv7l/   zstd                               (32-bit ARM)
```

The agent launcher auto-prepends the correct arch directory to `$PATH` based on `uname -m`. If a bundled binary is missing, agent and server both fall back to system tools.

### CI

[`.github/workflows/build.yml`](.github/workflows/build.yml) builds the server on three runners (`ubuntu-latest`, `macos-latest`, `windows-latest`) and the static C agent for five architectures (x86_64, aarch64, aarch64_be, armv7l, armeb). Big-endian targets use musl toolchains from musl.cc since Ubuntu only ships little-endian sysroots. Tagged pushes (`v*`) create a GitHub Release and attach all artifacts — including raw `perflens-agent-linux-<arch>` binaries with stable names that `install-agent.sh` and the agent's `--update` fetch from `releases/latest/download/`.

---

## Project layout

```
perflens/
├── install-agent.sh              # curl-able agent installer (arch detect, no sudo)
├── agent-c/
│   ├── perflens_agent.c          # C agent (~3200 lines, static binary, zero deps)
│   ├── Makefile                  # native + cross-compile targets
│   └── vendor/zstd/              # vendored zstd amalgamation
├── server/
│   ├── perflens_server.py        # TCP listener + ThreadingHTTPServer
│   ├── parser.py                 # perf script / perf stat parser
│   ├── source_mapper.py          # addr2line pipeline + path remap
│   └── bin/                      # bundled zstd / addr2line / readelf
├── ui/
│   ├── index.html                # single-page app
│   ├── app.js                    # all UI logic (vanilla JS, ~2000 lines)
│   └── style.css                 # dark + light themes (CSS custom properties)
├── docs/
│   ├── hero.svg
│   ├── architecture.svg
│   └── wire-protocol.svg
├── test/
│   ├── sample_workload.c         # multi-function test program
│   └── Makefile                  # gcc -g -O0 -lm
├── build_package.sh              # builds the release tarballs
├── .github/workflows/build.yml   # multi-OS CI + release automation
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

**LXC / container: `perf record -p <pid>` is empty.** Some container environments strip the perf capability set. A system-wide `perf record -a` usually works; the agent's `-p <pid>` mode does not.

**Call-graph probing hangs / slow startup.** Call-graph probing tests `fp`, `dwarf`, then `lbr` in sequence — this adds ~6–12 seconds on first connection. Normal.

---

## Design rules

These are the rules the project is built to:

- **Simplicity first** — Python stdlib, plain HTML/JS/CSS, no framework, no npm, no virtualenv
- **Defensive parsing** — `perf` output format varies across kernel versions; parser is forgiving
- **No secrets in code** — generic and open-source-friendly
- **No over-engineering** — if it doesn't earn its complexity, it gets cut

See [`CLAUDE.md`](CLAUDE.md) for the full internal reference, or the [documentation site](https://harshithsunku.github.io/perflens/) for the polished version.

---

## License

MIT. See [`LICENSE`](LICENSE).
