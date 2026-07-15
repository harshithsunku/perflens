# PerfLens — Project Status

Cross-session working state for the ongoing overhaul. Update this file at the
start and end of every working session. The full plan lives in the session
plan file; this is the executable summary.

## Current phase

**Phases 0–4 complete** (0, 1, 2, 3a+3b, 3e, 3c+3d, 4). Next up:
**Phase 5 — device E2E matrix + 0.6.0 release prep**.

### Start-here for the next session

- Run the packaged server: `uvx --from ./dist/perflens-0.6.0-py3-none-any.whl
  perflens serve ...` (build with `uv build`). Dev mode now needs the deps
  (fastapi/uvicorn/orjson/zstandard): `uv venv .venv && uv pip install -e
  '.[dev]'` then `.venv/bin/perflens serve ...`. Plain `PYTHONPATH=src
  python3 -m perflens.cli serve` only works in an env with those deps
  installed; same for the compat shim `server/perflens_server.py`.
- Tests: `make -C agent-c && .venv/bin/python -m pytest tests/` (109 tests)
  and `npm ci && npm run e2e` (22-assertion puppeteer run, self-contained).
  CI runs both: `.github/workflows/test.yml` (pytest 3.10–3.13 matrix +
  browser e2e on push/PR) and `build.yml` (pytest before the wheel build).
- Build/CI/README are all current as of Phase 4. Remaining doc debt: the
  docs/ GitHub Pages site still describes the old tarball install —
  refresh it during Phase 5 release prep.
- Sessions now default to `~/.perflens/sessions` (`--sessions-dir` to
  override; `PERFLENS_HOME` moves the whole root — tests use this).
  The old repo-root `sessions/` dir is no longer read.

## Overhaul roadmap

- [x] **Phase 0** — STATUS.md + baseline verification on both devices
- [x] **Phase 1a** — C agent fixes (commit 83b2bdc): CAS-slot child tracking,
      start-while-paused rejection, reprobe-while-profiling rejection
      (use-after-free), metrics config locking, `--rounds`, `--version` +
      `--update` self-update, `--token`/`PERFLENS_TOKEN` in hello,
      getaddrinfo hostname support in `--server`.
- [x] **Phase 1b** — Python agent removed; C agent is the only agent.
      `install-agent.sh` added (curl-able, arch+endian detect, verifies
      binary, installs to `~/.perflens/bin`). Release assets: raw static
      binaries with STABLE names `perflens-agent-linux-<arch>` (required by
      `releases/latest/download/` for installer + `--update`) plus versioned
      tarballs. CI `build-agent` (Python) job dropped; docs/README/UI docs
      updated.
- [x] **Phase 1c+1d** — Server correctness + security, all done:
      `_cmd_lock` around pending/response maps (+ fail-fast on disconnect);
      `agent_session_lock` + shared `_install_agent_session()` for both
      connect paths; `merge_perf_stat` accumulation (SSE now broadcasts the
      merged totals; perf_stat reset on new connection); multi-marker
      `split_perf_data` (multi-round `--output` files no longer lose rounds
      2..N); path-traversal fixes for static files AND session ids; HTTP
      binds 127.0.0.1 by default (`--http-bind` to expose, with warning);
      `/api/browse` confined to `--browse-root` (default: home);
      `--token`/`PERFLENS_TOKEN` validated at hello (hmac.compare_digest);
      stale parser test group index fixed (30/30 pass now).
- [x] **Phase 2a** — Incremental aggregation done: `server/aggregator.py`
      (EventAccumulator + AggregatorSet + build_per_event_batch); rebuild
      worker now folds only NEW chunks (O(new) per chunk, was O(total));
      inline expansion + addr2line at ingest only; source-file index
      incremental; replay/import reuse the same aggregator path;
      `test/test_aggregator_diff.py` pins batch↔incremental equivalence on
      both device fixtures (12/12 event combos identical). Aggregates cover
      the full session; the --max-samples deque remains only as the raw
      window for thread/source drill-downs.
- [x] **Phase 2b+2c** — Done. 2b (landed with the 2a commit): chunks
      spooled to disk as received (`chunk_%05d.zst` stores the compressed
      payload as-is), `_raw_chunks` RAM growth eliminated, `_save_session`
      metadata-only, replay cache (`replay_cache.json.gz`, config-keyed;
      2nd replay ~40x faster). 2c: SSE now carries only a tiny
      `data_version` stamp per chunk (was the full multi-MB per_event
      blob); new `GET /api/per-event?event=X` serves the cached snapshot
      gzip-encoded (~11x); UI fetches only the viewed event, with
      in-flight guard + catch-up; flamegraph zoom is now an ancestry
      name-path (`flamegraphZoomNames`) that survives data refreshes and
      cannot mis-target same-named frames in other stacks; breadcrumbs =
      ancestry chain. Verified in a real browser (puppeteer): live
      notify→fetch→render, zoom survives chunk refresh, 0 JS errors.
      NOTE: test/e2e_flamegraph.mjs is stale relative to the CURRENT UI
      (expects pre-swap single/double-click semantics; fails identically
      on HEAD) — rewrite in Phase 4.
- [x] **Phase 2d** — Done. New `server/symcache.py`: persistent caches under
      `~/.perflens/cache` (override root: `PERFLENS_HOME`) — sqlite
      `symbols.db` for addr2line resolutions / inline chains / symbol
      tables / DWARF file lists keyed by binary identity
      (realpath+mtime+size), plus `source_index_<sha1>.json.gz`.
      Source index: `os.scandir` background build, instant load from
      cache at startup, atomic swap; `_find_source_file` NEVER walks the
      tree (misses aren't negative-cached while the index is missing).
      Probe prefers `llvm-addr2line`; `llvm-dwarfdump --show-sources`
      used for DWARF file lists when present. `/api/index/status` now
      truncates the DWARF list (200 + total + truncated flag); new
      paginated `/api/index/files?offset=&limit=&q=`.
      Verified: 100k-file synthetic tree scans+persists in 0.14s, warm
      start loads it in 0.05s; live session populated symbols.db; warm
      restart hits all three caches (symbols/addr2line/index) on replay.
- [x] **Phase 3a+3b** — Done. `git mv` to src-layout: `src/perflens/{server,
      cli,parser,aggregator,source_mapper,symcache}.py` + `ui/` inside the
      package; intra-package imports fixed; UI served via
      `importlib.resources`; sessions moved to `~/.perflens/sessions`
      (`--sessions-dir` flag); in-process `zstandard` decompression with
      external-binary fallback; compat shim kept at
      `server/perflens_server.py`. `pyproject.toml` (hatchling, dep:
      zstandard only for now — fastapi/uvicorn/orjson land WITH the 3e
      migration); console script `perflens` with subcommands `serve`
      (default) / `import` / `push-agent USER@HOST` (ssh arch detect →
      download release binary → scp) / `version`. Verified: wheel builds
      via `uv build` (100 KB, UI files confirmed inside), `uvx --from
      ./dist/...whl perflens serve` cold-runs with UI served from the
      wheel and an agent connect round-trip, shim works, both test
      suites pass. PyPI name `perflens` free (2026-07-15); prepare, don't
      publish.
- [x] **Phase 3e** — Done. HTTP layer migrated to FastAPI/uvicorn: new
      `src/perflens/web.py` (all 26 endpoints ported 1:1, orjson responses,
      asyncio SSE hub fed from worker threads via call_soon_threadsafe,
      StaticFiles for the UI, uvicorn in the main thread); `server.py` lost
      its 876-line BaseHTTPRequestHandler and now owns only agent
      protocol/state/sessions (SSE broadcasts go through a pluggable sink
      the web layer registers). Deliberate parity choices: manual JSON body
      + query parsing (no pydantic validation → error shapes stay
      `{'error': ...}`, never FastAPI's `{'detail': ...}`), hand-rolled
      gzip in `_json()` (same >8KB/level-1 policy; GZipMiddleware avoided
      because it interacts badly with SSE streaming). Verified by
      golden-diff: every endpoint captured from the OLD server pre-
      migration and compared — all JSON semantically identical, exports
      (collapsed/SVG) + index.html byte-identical, all error status codes
      equal, traversal probes still 404. Live e2e on the new stack: agent
      connect → ping → start → 3 chunks streamed → SSE (agent_connected,
      data_version per chunk, accumulated perf_stat, all 3 metrics types,
      keepalives) → gzip per-event fetch (20KB→2.4KB) → pause/resume/stop
      → session saved → replay 0.27s cold / 9ms cached; puppeteer browser
      run clean (only pre-existing favicon 404); uvx cold-run of the wheel
      OK (16 deps resolved). BONUS FIX found by the golden diff: function
      summaries had hash-randomized ordering for equal-count functions
      (`set(a)|set(b)` union in aggregator.py + parser.py) — now a
      deterministic dict-based union; output stable across processes.
- [x] **Phase 3c+3d** — Done. `src/perflens/provision.py`: tool resolution
      flag → PATH → `~/.perflens/bin` → sha256-verified download of the
      static `perflens-tools-linux-<arch>.tar.gz` release asset (x86_64 +
      aarch64); `perflens provision` / `provision --status` CLI; server
      `probe_tools` auto-provisions addr2line/readelf at startup when
      both PATH and cache miss, degrades with instructions offline.
      Verified against a fake release server: fresh-home download,
      idempotent re-run, checksum-mismatch REFUSED, offline degrade
      (exit 1 + instructions), and a full bare-machine e2e (stripped
      PATH + fresh PERFLENS_HOME → server auto-provisions at startup →
      replay with line-level source annotation through the downloaded
      static addr2line, inline probe passes). CI overhauled: PyInstaller
      server matrix + AlmaLinux legacy job DELETED; new `python-package`
      job (ruff + both suites + `python -m build` + twine check +
      wheel-contents assert + install-and-serve smoke test); new
      `build-tools` job builds static binutils 2.44 (pinned sha256)
      addr2line+readelf for x86_64 + aarch64 — recipe validated locally
      for BOTH arches (key trick: `make -C binutils LDFLAGS=-all-static`
      relink; plain `-static` produces dynamic binaries via libtool);
      `publish-pypi` job prepared but `if: false` (enable steps are in
      the workflow comment); release job attaches wheel/sdist + agent
      binaries + tools bundles with new uvx-first release notes.
      `build_package.sh` rewritten (--server → uv/python -m build with
      contents check; PyInstaller path deleted). README install story
      now uvx/pipx-first; layout + badges + prerequisites updated.
      All 15 pre-existing ruff findings fixed — `ruff check src/` is
      CLEAN and the CI lint job is strict. VERSION file synced 0.5.0 →
      0.6.0 (it drives agent version + asset names).
- [x] **Phase 4** — Done. `test/` renamed `tests/` (git mv, fixtures
      intact); everything is pytest now (`[tool.pytest.ini_options]` in
      pyproject, shared `tests/conftest.py` with fixture-session loaders
      and an isolated `PERFLENS_HOME` fixture). 109 tests:
      parser (30) + parser_compat (15) + aggregator (12) + aggregator_diff
      (12, differential vs both device fixtures) + source_mapper (9) +
      **test_http_api.py** (25: every endpoint's shape via FastAPI
      TestClient, path/session-id traversal + browse confinement
      regressions, replay + replay-cache, exports, gzip negotiation, SSE
      initial frames + worker-thread broadcast against a real uvicorn) +
      **test_provision.py** (17: fake release HTTP server — fresh
      download, flat/hostile bundle members, checksum/sidecar/incomplete
      refusals, offline degrade, resolve_tool precedence flag→PATH→cache→
      download, CLI idempotence) + **test_agent_protocol.py** (13: the
      real C binary against a fake framing server with a `perf` shim on
      PATH — hello incl. --token/env token, ping/status/unknown-cmd,
      full lifecycle with zstd data frames decompressed and verified
      (probe results, PERF_STAT section), start-while-paused rejection,
      metrics frames, reconnect-after-disconnect, --output multi-round
      marker layout). Stale puppeteer e2e (3 files, pre-zoom-swap
      semantics) replaced by ONE self-contained `tests/e2e_ui.mjs`
      (22 assertions: starts its own server + isolated PERFLENS_HOME,
      materializes the x86 fixture session, drives the real UI — landing
      → replay → function table → flamegraph ancestry zoom/breadcrumbs/
      reset/search/context menu → export menu + all 3 export endpoints;
      zero JS errors). `npm run e2e`; package-lock.json committed for CI.
      New CI `test.yml`: pytest matrix (3.10–3.13, agent built first) +
      browser-e2e job; `build.yml` now runs pytest too. BONUS UI FIX
      found by the e2e: after a session replay the Flame Graph tab
      rendered empty (renderFlamegraph aborts at clientWidth 0 while the
      tab is hidden; live mode repaints on the next SSE refresh, replay
      never does) — the tab-click handler now re-renders when the
      container has no SVG. Doc sweep: README/CLAUDE/CONTRIBUTING/
      .gitignore/tools-README updated for tests/ + current stack.
- [ ] **Phase 5** — Full device E2E matrix, 1h RSS-bounded scale test,
      synthetic 500k-file source-index test, clean-container `uvx` run,
      0.6.0 release prep.

Key decisions (user-confirmed): bugs → scalability → packaging order;
**Python agent will be removed** (single static C agent, kept lightweight for
embedded, with self-update); server stdlib-only rule **lifted** (world-class
stack via uv, all user-space); PyInstaller frozen-server builds dropped once
uvx works; HTTP defaults to localhost bind.

## Known issues (baseline, 2026-07-15)

### Agent (agent-c/perflens_agent.c) — ALL FIXED in 83b2bdc
- ~~`g_child_pids` race~~ → lock-free CAS slots, signal-safe.
- ~~start-while-PAUSED thread leak~~ → rejected; stale thread joined.
- ~~reprobe while profiling freed caps under collection thread~~ → rejected.
- ~~Metrics config unlocked~~ → under state_lock.
- ~~No --rounds/--version/--update/--token~~ → all added.
- NEW (latent, shared with old Python agent): multi-round `--output` files
  concatenate `### PERF_STAT ###` sections; server's `split_perf_data`
  splits on the FIRST marker only, so rounds 2..N are lost on `--import`.
  Fix in parser.py during Phase 1c.

### Server (server/perflens_server.py)
- `_raw_chunks` grows unbounded in RAM all session — GBs over hours.
  Fix: spool to disk at receive time (Phase 2b).
- `_rebuild_worker` re-aggregates ALL samples every chunk — O(total)
  every ~8s; the main big-codebase bottleneck (Phase 2a).
- Session replay re-parses everything per request, uncached (Phase 2b).
- `_build_source_index` synchronous full walk on first source request
  (source_mapper.py:483) — minutes on 500k-file trees (Phase 2d).
- `/api/index/status` returns full DWARF file list (multi-MB JSON) (2d).
- ~~cmd response race / agent_session race / perf_stat last-wins / path
  traversal (static + session id) / 0.0.0.0 bind / unconfined browse /
  no auth~~ — ALL FIXED in Phase 1c+1d.

### UI (ui/app.js)
- Full re-render (table sort + flamegraph innerHTML) on every per_event SSE
  message; per_event payloads are multi-MB on big profiles (Phase 2c).
- Flamegraph zoom resets silently if zoomed function vanishes between rounds.

### Tests
- `test/test_parser_compat.py`: 15 regex-level checks FAIL — **stale test,
  not a parser bug**: HEADER_RE gained the optional `/tid` capture group
  (commit 8659c72) shifting event from group(4)→group(5); test still reads
  group(4) (test line 160). All 15 end-to-end parse tests pass. Fix the test
  in Phase 1.
- No tests for server HTTP layer, source_mapper, or agent.

## Test device state

| | x86 | ARM |
|---|---|---|
| SSH | `root@192.168.0.111` | `kali@10.10.3.249` |
| Host | Proxmox host `pve2` | Kali NetHunter, SDM845 phone |
| Arch / cores | x86_64 / 4 | aarch64 / 8 |
| Kernel | 6.17.13-2-pve | 6.12.92-sdm845-nh |
| perf | 6.12.95 | 7.0.12 |
| perf_event_paranoid | 4 (agent runs as root — OK) | 2 (own-process profiling OK) |
| gcc on device | 14.2 | 15.3 |
| Test dir | `/root/perflens-test/` | `~/perflens-test/` |
| Last verified | 2026-07-15 baseline PASS | 2026-07-15 baseline PASS |

Local dev machine: 192.168.0.85 (same subnet as x86 device; ARM device
reached via gateway). Local aarch64 cross-toolchain available:
`aarch64-linux-gnu-gcc` → `make CROSS=aarch64-linux-gnu-` in `agent-c/`.

## Baseline smoke test (repeat any session)

```bash
# 1. Local
.venv/bin/python -m pytest tests/            # full suite must pass
cd agent-c && make && cd ..                   # static x86_64 agent builds
python3 server/perflens_server.py --http-port 8080 --port 9999 &
curl -s localhost:8080/api/status

# 2. Device (x86 shown; ARM identical with kali@10.10.3.249 + CROSS build)
ssh root@192.168.0.111 'mkdir -p /root/perflens-test'
scp agent-c/perflens-agent tests/sample_workload.c tests/Makefile \
    root@192.168.0.111:/root/perflens-test/
ssh root@192.168.0.111 'cd /root/perflens-test && make && \
    nohup ./sample_workload >/dev/null 2>&1 & \
    nohup ./perflens-agent --listen --port 9999 > agent.log 2>&1 &'
# NOTE: use pkill -x (never pkill -f — it matches the ssh shell and kills it)

# 3. Drive via API
curl -X POST localhost:8080/api/connect -d '{"host":"192.168.0.111","port":9999}'
WPID=$(ssh root@192.168.0.111 'pgrep -x sample_workload | head -1')
curl -X POST localhost:8080/api/agent/command -d "{\"cmd\":\"start\",\"args\":{\"pid\":$WPID}}"
sleep 20
curl localhost:8080/api/status                # total_samples > 0, chunks grow
curl 'localhost:8080/api/thread-summary?event=cycles'
curl -X POST localhost:8080/api/agent/command -d '{"cmd":"stop"}'
curl localhost:8080/api/stop                  # disconnect → session saved
curl localhost:8080/api/sessions              # then GET /api/sessions/<id>
```

Baseline result 2026-07-15: **PASS on both devices** — connect, capability
probe (6 record events, fp callgraph), streaming (~3k samples/round),
pause/resume/stop, health metrics (incl. temp on ARM), session save,
replay (<150ms, 0.28MB JSON). Symbolization better on ARM (libm has symbols
there). x86 workload shows ~86% [unknown] frames — fp unwind can't cross
stripped libm; expected, not a bug.

## Regression fixtures

`tests/fixtures/session-{x86,arm}-baseline/` — real captured sessions
(chunks gzipped). Used by the Phase 2a differential aggregator test
(old batch path vs new incremental path must produce identical
snapshots), the HTTP API replay tests, and the browser e2e.

## Session log

- **2026-07-15** — Full project analysis (agents/server/UI/CI). Plan approved
  and saved. Phase 0 executed: C agent built natively + aarch64 cross,
  baseline PASS on both devices, fixtures captured, STATUS.md created.
- **2026-07-15 (cont.)** — Phase 1a: C agent race/state-machine fixes +
  self-update/--rounds/--token/getaddrinfo, all verified live against the
  running server (state machine incl. rejection paths, self-update against
  a fake release HTTP server, --rounds 2 headless). Phase 1b: Python agent
  and run_agent.sh deleted, install-agent.sh added (endianness detection
  verified on x86_64 + aarch64 device), build_package.sh + CI reworked
  (raw stable-name binaries as release assets), all docs updated.
- **2026-07-15 (cont.)** — Phase 2a: incremental aggregation. Differential
  test 12/12 identical vs batch on device fixtures; live streaming verified
  (consecutive per_event snapshots grow 2985→3057 samples, hybrid-CPU event
  names like cpu_core/cycles/ handled); replay through the new batch path
  0.22s. NOTE: local dev box has a hybrid CPU — useful extra test coverage.
- **2026-07-15 (cont.)** — Phase 1c+1d: server correctness + security.
  Verified live: traversal blocked (plain + URL-encoded + session-id),
  bind 127.0.0.1, browse snapped to home, token rejection AND acceptance
  (local agent), full inbound-mode regression on the x86 device with token
  (perf_stat accumulation confirmed: cycles 74.2B→98.8B→172.9B final in
  saved session; replay OK). NOTE: x86 device rebooted into kernel
  7.0.14-4-pve since baseline (was 6.17.13-2-pve).
- **2026-07-15 (cont.)** — Phases 2a–2d (see roadmap entries above for
  detail + verification evidence) and Phase 3a+3b: package restructure,
  pyproject, `perflens` CLI, uvx cold-run verified. Dev-box note: it has
  a hybrid CPU (event names like `cpu_atom/cycles/`) and slow perf-script
  rounds (~15-20s per 5s round) — use the real devices for timing-
  sensitive checks, and never `pgrep -f` for the workload (matches
  wrapper shells; use `pgrep -x workload`).
- **2026-07-15 (cont.)** — Phase 3e: FastAPI/uvicorn migration (see roadmap
  entry for full detail + verification evidence). Method worth reusing:
  golden-capture every endpoint from the old implementation BEFORE
  touching it, then semantic-diff the new one — this caught a latent
  hash-randomization bug in function-summary tie ordering that had
  nothing to do with the migration itself. ruff run on src/: web.py
  clean; ~15 pre-existing style findings (B904/E741/B007/E731/F541 in
  server/source_mapper/symcache/parser) left for the 3d ruff CI job.
- **2026-07-15 (cont.)** — Phase 3c+3d (see roadmap entry for detail).
  Notes for the future: the static-binutils CI recipe was validated
  locally for both arches before landing (x86_64 native + aarch64 via
  local cross toolchain); binutils 2.44 source sha256 is pinned in the
  workflow env. `perflens provision` flows all tested against a local
  fake release server (PERFLENS_UPDATE_URL override — same mechanism
  the agent self-update tests used). The misleading "inline disabled
  (-i not supported)" log line when no --binary is set was reworded.
  All 15 ruff findings fixed; `ruff check src/` clean.
- **2026-07-15 (cont.)** — Phase 4: pytest suite + browser e2e + CI (see
  roadmap entry for full detail). 109 pytest tests + 22-assertion
  puppeteer run, all green; `test.yml` added, `build.yml` runs pytest.
  Notes for the future: the agent protocol tests need `make -C agent-c`
  first (they skip, not fail, without the binary — CI builds it before
  pytest); the `perf` shim technique (PATH shim + PERF_SHIM_LOG) makes
  the full agent lifecycle testable in ~2s with no root and no real
  perf. The e2e caught a real replay-mode bug (flamegraph tab rendered
  empty — clientWidth 0 while hidden) — fixed in the tab-click handler.
  e2e clicks on re-renderable lists must be dispatched inside the page
  (`page.evaluate(... .click())`), not via element handles, or the
  initial-load `loadSessions()` race detaches them.
