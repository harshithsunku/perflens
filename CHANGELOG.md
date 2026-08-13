# Changelog

All notable changes to PerfLens are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html); pre-1.0
releases may break APIs between minor versions when needed.

## [Unreleased]

## [0.8.0] — 2026-08-13

A stabilization release. The feature set was frozen on 2026-08-13 with the
MCP server as the last capability added; everything since has been hygiene,
CI hardening and documentation truth. It also publishes the large body of
work that accumulated after 0.7.0 — the server module split, the React
frontend, and API v2.

**Upgrading:** API v2 renamed every REST endpoint (see below), and
`perflens.server` has been removed. The web UI and the agent wire protocol
are unaffected.

### Removed

- **`perflens.server`** — a compatibility shim introduced in the 0.6.0-era
  restructure and marked "kept for one release"; it has now outlived two.
  Import from the modules that actually hold the code: `perflens.app`
  (`AppContext`, `build_context`, `main`), `perflens.config`
  (`ServerConfig`), `perflens.state` (`ProfilingState`, `MetricsState`).
  This is a breaking change to a public import path. The repo-only
  `server/perflens_server.py` wrapper, the orphaned `run_server.sh`, and an
  empty `requirements-server.txt` went with it.

### Added

- **MCP server** (`perflens mcp`) — serves profiling data to LLM agents
  over the Model Context Protocol, so an agent can answer "why is this
  slow" against real `perf` data and drive a live run on a device.
  Nineteen tools: hot functions (self and total), hot stacks, hardware
  counters with derived IPC and miss rates, source hot lines, session
  comparison, per-thread breakdowns, device health, agent lifecycle, and
  file export. It is a client of the existing HTTP API — no core changes —
  and the SDK is an optional extra (`pip install 'perflens[mcp]'`), so the
  default install stays a four-dependency package. `--read-only` omits the
  device-control and export tools; config mutation, session deletion and
  the file browser are never exposed. Tools return ranked, capped views
  with an explicit next-page call, because a single event's snapshot runs
  from kilobytes to megabytes.
- **`skills/perflens-profiling/`** — a companion skill teaching an agent
  the method the tools support: orient first, pick the event that matches
  the question, read self *and* total, drill to hot source lines,
  corroborate with IPC and device health, and the pitfalls that produce
  confidently wrong answers.
- **API v2** — consistent REST surface (`/api/snapshot`, paginated
  `/api/sessions` with DELETE, `/api/sessions/{id}/export`,
  `/api/live/export`, `/api/threads`, `/api/window`, `/api/agent`,
  unified `/api/config`), a uniform `{"error": {code, message}}` envelope
  with real status codes, and SSE consolidated to `status` / `agent` /
  `data_version` / `perf_stat` / `metrics`.
- **React 19 + TypeScript + Vite frontend** replacing the vanilla-JS UI at
  full feature parity, with types generated from the committed OpenAPI
  schema and a self-contained Playwright E2E suite. The wheel ships the
  prebuilt assets, so `uvx perflens` still needs no Node.
- **Typed Pydantic API models** — the server split into `AppContext`
  modules (`app`, `config`, `state`, `agentlink`, `sessions`, `export`),
  with the OpenAPI schema exported to `frontend/openapi.json` and
  drift-checked in CI.
- UX polish: keyboard shortcuts with a help overlay, loading skeletons,
  a diff legend, and accessibility roles on tabs and icon-only buttons.

### Fixed

- **The no-UI fallback answered 503 for every unmatched path.** Once the
  frontend became a gitignored Vite output, a source checkout (and the
  Node-free CI job) took `create_app`'s fallback branch, whose catch-all
  route returned the "UI not built" page for *everything* — so path
  traversal probes got 503 instead of 404 and a mistyped API URL got HTML
  instead of the error envelope. It now answers `/` only and stays out of
  the OpenAPI schema; every other path 404s exactly as the static mount
  does.
- **OpenAPI drift across Python versions.** The 413 response carried no
  explicit description, so FastAPI filled it from
  `http.client.responses[413]` — whose phrase changed in Python 3.13
  ("Request Entity Too Large" → "Content Too Large"). Pinned, so the
  exported schema no longer depends on the interpreter.
- `__version__` had drifted to 0.6.0 while the package was 0.7.0; all
  three version locations now agree.
- The test suite no longer behaves differently depending on whether the
  frontend happened to be built locally.
- **Deep call stacks returned 500 from `/api/snapshot`, blanking the UI.**
  orjson refuses to encode past a fixed nesting depth (254 containers), and
  a flame graph level costs two of them — the node and its children list —
  so a profile containing a stack deeper than ~126 frames could not be
  serialized at all. Deeply recursive workloads really do produce such
  stacks. Flame graph depth is now capped, with the cut point marked
  `truncated`, so one very deep stack renders short instead of taking the
  whole view down. Found by profiling a 25-thread local workload; the
  committed test fixtures are too shallow to reach it.
- **The docs drawer displayed a version the build was not.** It carried a
  hand-typed `v0.8.0` string inside a package that shipped as 0.7.0, wired
  to nothing and absent from the release checklist. It is now injected from
  the canonical `VERSION` file at build time, and `tools/check_version.py`
  asserts every location agrees — including that no version literal has
  crept back into the frontend source. Wired into CI.
- **The committed test fixtures carried device IP addresses** in their
  `session_id` and `agent` metadata, against this project's own rule that
  no addresses appear anywhere in the repo. Renamed to generic identifiers.
- **Replaying a saved session showed no hardware counters.** Both fixture
  materializers discarded the captured metadata and wrote a stub with
  `total_samples: 0` and an empty `perf_stat`, so a replayed session
  rendered a single empty stat card instead of the full counter set.

### Changed

- **Docs screenshots and the demo GIF are regenerated from the React UI**
  and now cover the differential view, timeline scrubbing, keyboard
  shortcuts, flame graph search and zoom, export, and the in-app docs
  drawer — twelve stills, up from seven captured before the rewrite. They
  come from a Playwright harness (`frontend/docs-shots/`) that CI
  smoke-runs on every pull request, so it cannot silently rot the way the
  puppeteer scripts it replaces did.
- Every runtime dependency gained an upper bound (`fastapi<1.0`,
  `uvicorn<1.0`, `orjson<4`, `zstandard<1.0`, `httpx<1.0`). Floors alone
  allowed a future major release to resolve into a fresh `uvx perflens`
  install and break it with no change here. Note the consequence: when
  FastAPI 1.0 ships, installs pin to the last 0.x until a new release.
- CI actions moved off the deprecated Node 20 runtime, lint now runs on
  pull requests (it had only ever run post-merge), and the release path
  verifies the schema and version before building.

## [0.7.0] — 2026-07-18

### Added

- **Continuous pipe-mode collection** — the agent prefers one long-lived
  `perf record -o - | perf script -i -` pipeline over discrete rounds:
  no sampling dead time while `perf script` runs, symbol tables parsed
  once per pipeline instead of once per round, and the end-of-round CPU
  spike becomes a small continuous load (`perf script` additionally runs
  at nice 5). Probed at startup; targets without pipe support fall back
  to the existing round loop automatically.
- **Streaming compression** — perf script output is zstd-compressed as
  it is read from the pipe instead of being buffered whole; measured
  peak agent RSS for a 40 MB round dropped from 81 MB to ~2 MB.
- **Record-event selection** — `start` accepts an `events` subset of the
  probed record events; the wizard's capability step and the new
  profiling-settings popover expose it as checkboxes.
- **Profiling settings popover** — replaces the old `prompt()` dialogs:
  live frequency/interval/event editing synced from agent status, with
  a transparent stop+start when a change requires restarting collection;
  shows collection mode, call-graph method, and agent version.
- **Switch process** — retarget profiling to another process from the
  control bar (live process list with CPU%), keeping current settings.
- **Metrics master toggle** — the metrics gear popover now exposes the
  protocol's `enabled` flag to turn all health metrics off/on.
- **Collection-mode indicator** in the control bar (`continuous · 99 Hz`);
  `status` now reports `agent_version`, the effective event list, and
  `pipe_mode`.

- **Differential view** — snapshot the live profile ("Set Baseline") or
  pick any saved session as the baseline; the function table gains a
  Δ Self column and the flame graph recolors by change (red = grew,
  blue = shrank, grey = unchanged), with exact deltas on hover.
- **Timeline scrubbing** — drag across any Device Health sparkline to
  restrict the Functions table and Flame Graph to samples received in
  that window (new `GET /api/time-window`); samples are stamped with
  arrival time on receipt.
- **Opt-in per-thread CPU metrics** — the agent reports tid/comm/state/
  cumulative ticks for every thread of the profiled process
  (`configure_metrics {"threads": true}`, off by default); the Threads
  tab shows a real-time Live CPU column.
- **Shareable URLs** — tab, event, thread filter, flame-graph zoom path,
  and replayed session live in the URL hash and survive refresh.
- **Metrics settings sync** — opening the gear popover reads the
  agent's current collection settings instead of guessing.

- **Opt-in disk I/O metrics** — the agent can now report per-device
  throughput/IOPS (`/proc/diskstats`, partitions folded into their
  disk) and per-process read/write bytes (`/proc/<pid>/io`). Off by
  default to keep the agent light on embedded targets; toggled at
  runtime via `configure_metrics {"disk": true}`.
- **Metrics settings in the UI** — a gear on the Device Health strip
  drives the agent's `configure_metrics` live: network on/off, disk
  I/O on/off, collection interval (1/2/5/10s).
- **Sparkline hover readouts** — hovering a Device Health sparkline
  shows the value and age at the cursor.
- **TCP keepalive on the agent** — a dead network path now unblocks
  `recv()` within ~2 minutes so `--server` mode actually reconnects.

### Changed

- **Agent source split into modules** — `agent-c/perflens_agent.c`
  (4,600 lines) became `agent-c/src/` (`agent.h` + 10 focused `.c`
  files); same flags, same static binary, same cross-compile targets.

### Fixed

- **Perf children were immune to SIGTERM** — processes forked from the
  agent's worker threads inherited the thread's blocked-signal mask
  across exec, so stop/pause "immediate kill" never actually worked
  (rounds just ran to completion, and the new long-lived pipeline hung
  shutdown). Children now reset their signal mask before exec.
- **Agent**: snprintf buffer overflow in network metrics on hosts with
  many interfaces; unbounded `malloc` from a corrupt frame header
  (now capped at 64 MB); `buf_ensure` doubling past its cap; recv
  thread shutdown ordering (`shutdown()` before join, `close()` after);
  transient `socket()` failures no longer exit the daemon.
- **Server**: replacing a live agent no longer lets the old session's
  cleanup clobber the new session's state, and the rebuild worker can
  no longer fold a replaced session's chunks into the new session or
  die permanently on one bad chunk; inbound frame lengths capped at
  128 MB (a garbage header could previously claim a 4 GB allocation).
- **UI**: the per-thread source view was broken (wrong field name +
  CSS classes that didn't exist) — it now renders with the same heat
  annotation as the main source view; the control bar shows the
  agent's real PID/state after a page reload; resetting flamegraph
  zoom also resets the thread filter dropdown; large float values
  format correctly; the in-app docs caught up with the 0.6.0 era.

## [0.6.0] — 2026-07-15

**The "pip install perflens" release.** The server is now a proper Python
package — `uvx perflens` / `pipx install perflens` / `pip install --user
perflens` — with a FastAPI/uvicorn HTTP layer, incremental aggregation
that scales to long sessions and huge codebases, persistent symbol
caches, security hardening, a single self-updating C agent, and a full
automated test suite.

### Added

- **Python package** — src-layout + `pyproject.toml`; console script
  `perflens` with subcommands `serve` (default), `import`, `push-agent
  USER@HOST` (ssh arch-detect → download → scp), `provision`, and
  `version`. The web UI ships inside the wheel.
- **`install-agent.sh`** — one-line curl installer for the agent:
  detects arch + endianness, verifies the binary, installs to
  `~/.perflens/bin`, no sudo.
- **Agent self-update and hardening** — `--update` (download, verify,
  atomic self-replace), `--rounds N` for headless capture, `--version`,
  `--token`/`PERFLENS_TOKEN` shared-secret auth, hostname support in
  `--server`.
- **`perflens provision`** — when binutils is missing, downloads
  sha256-verified static `addr2line`/`readelf` bundles (x86_64,
  aarch64) into `~/.perflens/bin`; the server auto-provisions at
  startup and degrades with instructions when offline.
- **Persistent symbol/source caches** under `~/.perflens/cache` —
  sqlite-backed addr2line resolutions, inline chains, symbol tables,
  and DWARF file lists keyed by binary identity, plus a cached source
  index built in the background (100k-file tree: 0.14s cold scan,
  0.05s warm load). `llvm-addr2line`/`llvm-dwarfdump` preferred when
  present.
- **`GET /api/per-event`** — gzipped per-event snapshots; SSE now
  carries only a tiny `data_version` stamp per chunk (was a multi-MB
  blob). Flamegraph zoom became an ancestry name-path that survives
  live data refreshes.
- **Paginated `/api/index/files`** (+ truncated `/api/index/status`)
  for very large DWARF file lists.
- **`--sessions-dir`** — sessions now live under `~/.perflens/sessions`
  (`PERFLENS_HOME` relocates the whole root).
- **Test suite** — 109 pytest tests: parser, differential aggregator
  checks against device-captured fixtures, source mapper, every HTTP
  endpoint (including traversal and gzip regressions), provisioning
  against a fake release server, and the C-agent wire protocol driven
  end-to-end through a fake framing server with a `perf` shim. Plus a
  self-contained puppeteer browser E2E replaying a real fixture
  session through the UI.

### Changed

- **Single agent** — the C agent (static binary, ~2 MB, vendored zstd,
  zero runtime dependencies) is now the only agent.
- **HTTP layer migrated to FastAPI/uvicorn** (`web.py`) with orjson
  responses and an asyncio SSE hub. Every URL path and JSON shape is
  unchanged — verified by golden-diffing all endpoints against the old
  stdlib server.
- **Incremental aggregation** — each new chunk folds into the running
  per-event state in O(new samples) instead of re-aggregating
  everything (~8s cycle on long sessions before). Aggregates now cover
  the full session; `--max-samples` only bounds the raw-sample window
  used for thread/source drill-downs.
- **Sessions spool to disk as compressed chunks while streaming** —
  server RAM stays flat over long sessions; replay is parsed on demand
  and cached (second open ~40× faster).
- **Security defaults** — the web UI binds `127.0.0.1` unless
  `--http-bind` says otherwise; the wizard's file browser is confined
  to `--browse-root` (default: home); agents can be required to
  present a shared `--token` (constant-time comparison).
- **Server requirements** — Python 3.10+ with a small, deliberate
  dependency set (fastapi, uvicorn, orjson, zstandard), all
  user-space; zstd decompression is in-process now (external binary
  only as fallback).

### Fixed

- Agent: child-PID tracking race in the signal path (lock-free CAS
  slots), start-while-paused thread leak, reprobe-while-profiling
  use-after-free, unlocked metrics config.
- Server: command-response and agent-session install races; `perf stat`
  totals now accumulate across rounds (last-write-wins before);
  multi-round `--output` captures no longer lose rounds 2..N on
  import; path traversal via static files and session ids; function
  summaries had hash-randomized ordering for equal-count ties.
- UI: the Flame Graph tab rendered empty after a session replay (the
  layout aborts at zero width while the tab is hidden and replay mode
  never repaints).

### Removed

- The Python agent and `run_agent.sh` — superseded by the C agent +
  `install-agent.sh`.
- PyInstaller frozen-server tarballs (and the AlmaLinux legacy build) —
  the wheel is the only server artifact.

### Infrastructure

- New `test.yml` workflow: pytest matrix on Python 3.10–3.13 (with the
  agent built first) + a browser-E2E job, on every push/PR.
- `build.yml` overhauled: strict ruff, pytest, wheel/sdist with
  contents check and install-and-serve smoke test, static C agents for
  five architectures, static binutils tools bundles for two, tag-driven
  GitHub Releases with stable-name agent assets, and a prepared
  (disabled until Trusted Publishing is configured) PyPI publish job.

## [0.5.0] — 2026-05-17

**The "enterprise + docs" release.** Per-thread analysis lands as a full
tab. Cross-compilation toolchains and sysroots are first-class. The web
UI scales to large codebases and the Linux server binary is now fully
portable across glibc versions. A polished GitHub Pages documentation
site ships alongside the code.

### Added

- **Per-thread analysis tab** — a dedicated Threads view shows each tid
  with sample counts, CPU share, and top functions. Flame graphs,
  function tables, and source annotations can be filtered to a single
  thread.
- **Cross-compilation support** — `--toolchain-prefix` derives both
  `addr2line` and `readelf` from a single prefix. `--sysroot` resolves
  shared-library module paths and source files under a target tree,
  similar to `perf --symfs`. Settings can also be changed at runtime via
  the wizard's *Toolchain & Cross-compilation* section.
- **Multi-threaded test workload** — `test/matrixlab` is a new
  multi-threaded program for end-to-end testing of the per-thread flow.
- **Documentation site** — a hand-crafted static site under
  [`docs/`](docs/) deployed via GitHub Pages at
  https://harshithsunku.github.io/perflens/. Long-scroll landing,
  architecture deep-dive, full CLI/HTTP reference, dark+light themes,
  copy-buttons, scroll-spy TOC, live demo GIF.
- **CONTRIBUTING.md + CODE_OF_CONDUCT.md** — short, project-specific
  guides for the public repo.
- **`tools/` directory** — author-side puppeteer + ffmpeg scripts that
  regenerate the docs screenshots and demo GIF. Gives the puppeteer
  devDependency a visible purpose in-tree.
- **Interactive tabbed in-app docs** — the in-UI Docs panel was rebuilt
  with tabs (Getting Started, Configuration, UI Guide, Troubleshooting,
  Architecture) and now reflects every enterprise feature.

### Changed

- **Server hot paths optimized** — sample ring buffer is now a `deque`,
  flame graph and function table rebuilds run in a background worker,
  and a vaddr cache reduces `addr2line` calls. Noticeably snappier on
  large codebases and long sessions.
- **Wizard re-ordered** — *Process* before *Perf* so the pid-required
  error can't fire on the wrong step. The wizard also shows the agent's
  probed perf capabilities up front and pre-indexes the binary so the
  Source tab is ready when the first samples land.
- **Flame graph + function table scale further** — table virtualization
  was tuned for codebases with thousands of functions; flame graph
  rendering layout was rewritten for legibility at depth.
- **Thread source files load on demand** — clicking a thread in the
  Threads tab fetches its source-file list, instead of eagerly loading
  every thread's files at render time.
- **Linux server binary is portable** — the PyInstaller-frozen server
  for Linux x86_64 is now built inside a `manylinux_2_28` container, so
  it runs on any glibc ≥ 2.28 distro (RHEL 8, Ubuntu 18.04+, Debian 10+,
  etc.). A separate `linux-x86_64-legacy` build covers glibc ≥ 2.35
  hosts that want the more recent Python.
- **README quick-start examples** use a `<ver>` placeholder rather than
  a hardcoded version that drifts every release.

### Fixed

- **Threads tab missed some tids** — `perf script -F` field list now
  includes `tid` so the parser sees every thread, not just main.
- **C agent** — fixed the interactive command protocol, the
  `list_processes` sort order, and an unsafe signal handler path.
- **Source mapping on large codebases** — the source mapper now handles
  binaries with very long DIE chains and unusual compile-unit layouts
  without falling over.

### Infrastructure

- **CI builds the Linux server in `manylinux_2_28`** (with the
  `EXTERNALLY-MANAGED` marker handling for PEP 668), and adds an
  ubuntu-22.04 legacy build for older Python use cases.
- `RUNPATH` is stripped before PyInstaller, so the frozen binary plays
  nicely with static-linking experiments downstream.
- A build status, latest-release, stars, and docs-site badge now live
  at the top of the README.
- `node_modules/` is untracked and gitignored; `package.json` +
  `package-lock.json` remain in-tree so the docs regen pipeline is
  reproducible.

### Documentation

- Live demo GIF (`docs/demo.gif`) showing the function table updating
  in real time, then flame graph, then source view.
- Open Graph + Twitter card meta tags on all docs pages so link unfurls
  render correctly on Slack, Twitter/X, and Discord.

---

## [0.4.0] — 2026-04-13

First public-quality release with cross-compilation hooks, the wizard
flow, two-mode agent (`--server` / `--listen`), and the initial saved
sessions / replay UI. See [GitHub release notes](https://github.com/harshithsunku/perflens/releases/tag/v0.4.0)
for the original asset list.

## [0.2.1] — 2026-04-12

CI hardening, C agent build matrix expansion.

## [0.2.0] — 2026-04-11

Second iteration: C agent, capability probing, multi-arch tarballs.

## [0.1.0] — 2026-04-10

Initial public preview.
