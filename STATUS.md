# PerfLens — Project Status

Cross-session working state for the ongoing overhaul. Update this file at the
start and end of every working session. The full plan lives in the session
plan file; this is the executable summary.

## Current phase

**Phase 0 complete** (baseline verified). Next up: **Phase 1a — C agent fixes**.

## Overhaul roadmap

- [x] **Phase 0** — STATUS.md + baseline verification on both devices
- [ ] **Phase 1a** — C agent fixes: `g_child_pids` race, start-while-paused
      thread leak, lock metrics/caps/pid mutations, add `--rounds`,
      `--version` + `--update` self-update (curl/wget from GitHub releases,
      verify + atomic rename), optional `--token` in hello.
      Wire protocol stays byte-identical.
- [ ] **Phase 1b** — Remove the Python agent entirely (C agent is the only
      agent). Add curl-able `install-agent.sh` (arch detect → download static
      binary from GitHub releases into `~/.perflens/bin`). Stable release
      asset naming: `perflens-agent-<ver>-linux-<arch>`.
- [ ] **Phase 1c+1d** — Server correctness + security: `_pending`/`_responses`
      cmd lock; `agent_session` global lock; `merge_perf_stat` accumulation
      (currently last-chunk-wins); static-file path-traversal fix; HTTP bind
      127.0.0.1 default + `--http-bind`; `/api/browse` confined to
      `--browse-root`; server-side `--token` validation.
- [ ] **Phase 2a** — Incremental aggregation (`server/aggregator.py`):
      EventAccumulator per event, inline expansion at ingest only,
      differential test vs old batch path (use `test/fixtures/`).
- [ ] **Phase 2b+2c** — Disk spooling of raw chunks (kills unbounded
      `_raw_chunks` RAM growth); replay cache; SSE → notify (`data_version`)
      + fetch (`GET /api/per-event`, gzip); UI version-tracked fetch with
      path-based zoom state.
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

### Agent (agent-c/perflens_agent.c)
- `g_child_pids` track/untrack (c:190-204) races collection thread vs
  cmd_stop/pause/signal handler — no lock.
- `start` while PAUSED spawns a second collection thread, leaks the first
  (c:2425/2507).
- Metrics config + caps/pid mutations partially outside `state_lock`
  (c:2648-2661, c:2469-2500, metrics loop c:2024-2042).
- No `--rounds` in headless mode (Python agent had it).
- No self-update, no `--version`, no auth token.

### Server (server/perflens_server.py)
- `_raw_chunks` grows unbounded in RAM all session (line 532) — GBs over
  hours. Fix: spool to disk at receive time (Phase 2b).
- `_rebuild_worker` re-aggregates ALL samples every chunk (748-772) — O(total)
  every ~8s; the main big-codebase bottleneck (Phase 2a).
- `_pending`/`_responses` race (441-483, 511-521) → KeyError can drop agent.
- `agent_session` global swapped without lock (613, 786-861).
- perf_stat last-chunk-wins (85) — counters should accumulate.
- Path traversal in static serving (1288).
- `/api/browse` serves entire filesystem; HTTP binds 0.0.0.0; no auth.
- Session replay re-parses everything per request, uncached (1929).
- `_build_source_index` synchronous full walk on first source request
  (source_mapper.py:483) — minutes on 500k-file trees.
- `/api/index/status` returns full DWARF file list (multi-MB JSON).

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
