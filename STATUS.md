# PerfLens — Project Status

Cross-session working state for the ongoing overhaul. Update this file at the
start and end of every working session. The full plan lives in the session
plan file; this is the executable summary.

## Current phase

**Phases 0, 1a, 1b, 1c+1d, 2a, 2b+2c complete.** Next up: **Phase 2d —
source mapping at 500k-file / GB-DWARF scale**.

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
- [ ] **Phase 2d** — Source mapping at scale: background index (never sync
      `os.walk` on request path), persistent caches under `~/.perflens/cache`
      (source index + sqlite symbols.db), prefer llvm-symbolizer/dwarfdump,
      paginated `/api/index/files`.
- [ ] **Phase 3a+3b** — src-layout restructure + `pyproject.toml` (hatchling;
      deps: fastapi, uvicorn, zstandard, orjson) + `perflens` console script
      (serve / import / push-agent / provision) + `uvx perflens`.
      PyPI name `perflens` verified free (2026-07-15). Prepare, don't publish.
- [ ] **Phase 3e** — FastAPI/uvicorn migration of the HTTP layer (typed
      routes, asyncio SSE, StaticFiles from importlib.resources,
      GZipMiddleware, orjson). URL paths + JSON shapes preserved exactly.
- [ ] **Phase 3c+3d** — `provision.py` binary auto-download to
      `~/.perflens/bin` (graceful degrade offline); CI: sdist/wheel +
      wheel-contents check + ruff; drop PyInstaller server + Python-agent
      jobs; publish-on-tag prepared but disabled.
- [ ] **Phase 4** — pytest suite: parser, differential aggregator,
      source_mapper, HTTP API (httpx TestClient), C-agent protocol tests
      (fake framing server + mocked `perf` shim).
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
python3 test/test_parser_compat.py            # e2e parse tests must pass
cd agent-c && make && cd ..                   # static x86_64 agent builds
python3 server/perflens_server.py --http-port 8080 --port 9999 &
curl -s localhost:8080/api/status

# 2. Device (x86 shown; ARM identical with kali@10.10.3.249 + CROSS build)
ssh root@192.168.0.111 'mkdir -p /root/perflens-test'
scp agent-c/perflens-agent test/sample_workload.c test/Makefile \
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

`test/fixtures/session-{x86,arm}-baseline/` — real captured sessions
(chunks gzipped). Used by the Phase 2a differential aggregator test:
old batch path vs new incremental path must produce identical snapshots.

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
