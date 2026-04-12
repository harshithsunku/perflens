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
   Process (PID)                         Python server (perflens_server.py)
      |                                      |
   perf record + perf stat                   |
      |                                      |
   Agent (perflens_agent.py)                 |
      |                                      |
      +---- TCP (5-byte header + zstd) ----> recv + decompress
                                             |
                                        parser.py  (perf script / perf stat)
                                        source_mapper.py  (addr2line + source)
                                             |
                                        SSE  --->  Browser (app.js)
```

### Wire protocol
- 5-byte header: 4-byte uint32 big-endian payload length + 1-byte compression
  flag (`0` = raw, `1` = zstd).
- Payload: UTF-8 perf script output, optionally followed by a
  `### PERF_STAT ###` section.
- Agent compresses with `zstd -1 -c`, server decompresses with `zstd -d -c`.
  Typical ratio on real perf script output is 20–40×.

### Key design decisions
- Python stdlib only. No Flask, no npm, no Docker, no virtualenv, no pip
  dependencies on either side.
- Plain HTML + vanilla JS + CSS for the UI. No bundler, no framework.
- `ThreadingHTTPServer` for concurrent HTTP + SSE; a separate thread owns
  the TCP listener.
- `addr2line -f` (no `-i`, no `-p`) pipelined in batches of 500 addresses.
- Session replay is lazy: raw chunks are saved to disk and parsed on demand.
- A single `SourceMapper` is created at server startup and shared across all
  requests — no per-request forking.
- The agent probes supported perf events and call-graph modes (`fp`, `dwarf`,
  `lbr`) on the target before collecting, and uses whichever works.
- The agent is **Python 3.5 compatible** — no f-strings, no dataclasses, no
  `subprocess.run(capture_output=True)`. This lets it run on older ARM or
  x86 Linux targets where only Python 3.5 is available.

---

## File layout

```
perflens/
├── agent/
│   └── perflens_agent.py         # Python 3.5+ device agent
├── agent-c/
│   ├── perflens_agent.c          # C agent (~1000 lines, static binary)
│   ├── Makefile                  # native + cross-compile targets
│   └── vendor/zstd/              # zstd single-file amalgamation
├── server/
│   ├── perflens_server.py        # TCP listener + ThreadingHTTPServer
│   ├── parser.py                 # perf script / perf stat parser
│   ├── source_mapper.py          # addr2line pipeline + path remap
│   └── bin/                      # bundled zstd / addr2line / readelf
├── ui/
│   ├── index.html                # single-page app
│   ├── app.js                    # all UI logic (vanilla JS)
│   └── style.css                 # dark theme
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
| `/api/source?file=&event=`| GET    | Annotated source for a single file              |
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
--max-samples N       Ring buffer cap             (default 500000)
```

## Agent CLI

```
--pid PID             Process to profile          (required)
--server HOST         Server host. Omit for stdout mode.
--port PORT           Server TCP port             (default 9999)
--frequency HZ        perf record -F              (default 99)
--duration SECS       Length of each round        (default 8)
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
