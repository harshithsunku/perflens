# PerfLens — Project Status

Cross-session working state. Update at the start and end of every working
session. Release history lives in [CHANGELOG.md](CHANGELOG.md); this file
is what is *currently true* and what is *left to do*.

## Current phase — stabilization (feature freeze)

**The feature set is frozen as of 2026-08-13.** The MCP server was the last
capability added; from here the work is stabilization, verification and
documentation, not new surface.

- **Published:** 0.7.0 (PyPI, tag `v0.7.0`).
- **Version is deliberately held at 0.7.0** — `VERSION`, `pyproject.toml`
  and `src/perflens/__init__.py` all agree. It moves to 0.8.0 only when the
  stabilization checklist below is clear.
- **Unreleased on master** — a large body of work sits between the `v0.7.0`
  tag and HEAD:
  - `cfbe5c8` server split into `AppContext` modules + typed Pydantic API
  - `bc6e6c5` React 19 + TypeScript + Vite frontend, Playwright E2E
  - `eb7664a` **API v2** — REST surface renamed, `{"error": {code, message}}`
    envelope, SSE consolidated
  - `f847309` UX polish (keyboard shortcuts, skeletons, diff legend, a11y)
  - `598e90b` CI fix (no-UI fallback 404s like the static mount; 413 phrase
    pinned against the Python 3.13 rename)
  - `4a966c7` **MCP server** (`perflens mcp`) + `skills/perflens-profiling/`
- **CI is green** on all workflows (pytest 3.10–3.13, frontend vitest +
  Playwright + OpenAPI drift, wheel + five agent architectures).

### Start-here for the next session

```bash
uv venv .venv && uv pip install -p .venv/bin/python -e '.[dev]'
make -C agent-c                              # protocol tests need the real binary
.venv/bin/python -m pytest tests/            # 149 tests
npm --prefix frontend ci
npm --prefix frontend run test               # vitest
npm --prefix frontend run build              # emits into src/perflens/ui/
npm --prefix frontend run e2e                # Playwright, self-contained
```

Two things that bite if forgotten:

- **`VERSION` drives the agent's baked-in version**, so `make -C agent-c
  clean && make -C agent-c` after any version change or
  `test_agent_protocol.py::test_hello` fails against a stale binary.
- **The frontend is a gitignored Vite output.** A source checkout without
  `npm run build` has no `src/perflens/ui/`, which is the configuration CI
  runs in — worth reproducing locally (move the directory aside) before
  trusting a green local suite.

## Stabilization checklist

Ordered roughly by user impact.

- [x] **Docs site brought to API v2** (2026-08-13) — `docs/reference.html`
      had documented the pre-v2 surface (`/api/per-event`,
      `/api/thread-summary`, `/api/thread-view`, `/api/time-window`,
      `/api/connect`, `/api/stop`, `/api/export/*`, `/api/import`,
      `/api/config/*`, `/api/wizard/state`), so anyone following it got
      404s. All 24 endpoints now match `web.py`, with the error-model note
      and a pointer to `/api/openapi.json`; new MCP section; project layout
      and CI paragraph corrected. `architecture.html` SSE/endpoint wording
      updated; `index.html` no longer claims a vanilla-JS UI and gained an
      MCP feature card.
- [ ] **The docs site's screenshots and demo GIF show the old UI.**
      `docs/screenshots/*.png` and `docs/demo.gif` were captured
      2026-05-17, two months before the React rewrite — so the landing page
      advertises a UI that no longer exists, missing the differential view,
      timeline scrubbing and keyboard shortcuts along the way. Regenerating
      needs a live server + agent + workload (see `tools/README.md`), and
      the capture scripts were written against the old vanilla-JS DOM;
      expect to port them to the React app's `data-testid` contract rather
      than just re-running them.
- [ ] **Decide about the IPs in git history.** The device addresses and ssh
      targets removed from STATUS.md on 2026-08-13 are still reachable in
      earlier commits, against the project's no-IPs rule. Clearing them
      needs a history rewrite (`git filter-repo` + force-push), which
      breaks existing clones — a deliberate call, not a drive-by fix. They
      are private-range addresses, so the practical exposure is low.
- [ ] **Server dependencies are floors only** (`fastapi>=0.110`,
      `uvicorn>=0.29`, `orjson>=3.9`, `zstandard>=0.21`, `mcp>=2,<3`). CI
      installs the latest each run, so a FastAPI or Pydantic release can
      shift the generated OpenAPI schema and fail the drift check without
      any change on our side. Decide: upper bounds, or generate the schema
      in CI instead of diffing a committed artifact.
- [ ] **Node 20 deprecation** — `actions/checkout@v4`,
      `actions/setup-python@v5`, `actions/setup-node@v4` and
      `actions/upload-artifact@v4` are being forced onto Node 24 with a
      warning in every run. Bump before the forced migration.
- [ ] **Starlette deprecation** — `TestClient` warns that `httpx` support is
      deprecated in favour of `httpx2`. Affects the test suite only, but it
      will become an error eventually.
- [ ] **Retire the compat shims.** `server/perflens_server.py` and
      `src/perflens/server.py` were introduced as "one release" bridges in
      the 0.6.0-era restructure and have now outlived two releases. Remove
      at 0.8.0, or commit to keeping them.
- [ ] **Device E2E matrix** — full live run on both reference devices
      (x86_64 and aarch64): connect, capability probe, continuous
      collection, pause/resume/stop, health metrics, session save, replay,
      and the same flow driven through the MCP tools.
- [ ] **Scale tests** — long-run RSS boundedness (~1 h of continuous
      collection) and a synthetic large source tree (~500k files) through
      the source index.
- [ ] **Clean-container `uvx` run** of the built wheel: no Node, no
      binutils, no repo — confirms the shipped artifact stands alone.
- [ ] **MCP on real data** — the tools are covered by 28 tests against
      fixture sessions and were driven end to end over stdio against a live
      server, but not yet against a live *device* session. Optional extra:
      an evaluation set (10 Q/A against the committed fixtures, per the
      mcp-builder format) to catch regressions in tool usefulness.

### Release checklist for 0.8.0 (when the above is clear)

1. Bump **three** places — `VERSION`, `pyproject.toml`,
   `src/perflens/__init__.py` — they must agree, and `info.version` in the
   exported schema follows.
2. `python tools/export_openapi.py && npm --prefix frontend run typegen`,
   then confirm `git diff` shows only the version line.
3. `make -C agent-c clean && make -C agent-c` (version is compiled in).
4. Write the CHANGELOG entry from the unreleased commits listed above.
5. Tag `v0.8.0` — the tag drives the GitHub Release and the PyPI publish
   via Trusted Publishing.

## Known limitations (current, by design or accepted)

- Single agent connection at a time; a new agent replaces the current one.
- Per-thread views are live-only — a saved session's replay carries the
  thread list but no per-thread aggregates.
- Live `perf_stat` has no REST endpoint; it is read from the SSE head.
- Capability probing adds ~8–14 s to first-connection startup.
- In continuous pipe mode the first chunk after `start` may carry only
  PERF_STAT data before samples begin flowing.
- `addr2line` source mapping needs an unstripped `-g` build.
- Some container environments reject `perf record -p <pid>`; system-wide
  `perf record -a` usually works instead.

## Reference devices

Kept generic on purpose — this repo carries no addresses, hostnames or
credentials.

| | x86 reference | ARM reference |
|---|---|---|
| Arch / cores | x86_64 / 4 | aarch64 / 8 |
| Kernel | 6.x | 6.x |
| `perf_event_paranoid` | agent runs as root | 2 (own-process profiling OK) |
| Notes | hypervisor host | phone-class SoC, has thermal metrics |

The local dev box has a **hybrid CPU** (event names like `cpu_atom/cycles/`)
and slow `perf script` rounds — useful for parser coverage, misleading for
timing. Use the reference devices for anything timing-sensitive, and
`pgrep -x` (never `pgrep -f`, which matches wrapper shells).

Cross-compiling the agent: `make -C agent-c CROSS=aarch64-linux-gnu-`.

## Regression fixtures

`tests/fixtures/session-{x86,arm}-baseline/` — real captured sessions,
chunks gzipped. Used by the differential aggregator test (batch vs
incremental must agree), the HTTP API replay tests, the Playwright E2E, and
the MCP tool tests.

## Session log

Condensed; anything older is in the CHANGELOG and git history.

- **2026-07-15/16** — the 0.6.0 overhaul: agent hardening, incremental
  aggregation, disk spooling + replay cache, persistent symbol caches,
  src-layout package, FastAPI migration, provisioning, pytest suite. Then
  post-0.6.0 features: opt-in disk/thread metrics, differential view,
  timeline scrubbing, shareable URLs.
- **2026-07-18** — 0.7.0 released. Then the module split, React frontend,
  API v2 and UX polish landed together; that push left CI red.
- **2026-08-13** — CI repaired (`598e90b`): the no-UI fallback was
  answering 503 for every unmatched path once the UI became a gitignored
  build artifact, and the 413 reason phrase changed under Python 3.13.
  Method worth reusing: reproduce the CI *environment* locally (move
  `src/perflens/ui` aside) rather than trusting a green local suite.
- **2026-08-13** — Documentation sweep. All eight tracked `.md` files
  audited: none redundant, but STATUS.md was ~80% obsolete (and carried
  device IPs, against this project's own rule), CONTRIBUTING still
  described a vanilla-JS UI and a deleted root `package.json`, README
  pointed at a puppeteer E2E that no longer exists, and `tools/README`
  documented an `npm install` that cannot work. Then the GitHub Pages site
  was brought to API v2. Worth remembering: docs staleness clusters around
  *renames* — the API v2 commit renamed every endpoint, and four files
  kept the old names for weeks because nothing tests prose.
- **2026-08-13** — MCP server + companion skill (`4a966c7`), then feature
  freeze declared and the version held at 0.7.0 (`531f27b`). Notes: the MCP
  Python SDK is on **2.x** (`MCPServer`, not 1.x's `FastMCP`;
  `input_schema`, not `inputSchema`) — check the installed package, the
  reference docs in circulation are still 1.x. httpx's ASGI transport
  buffers whole responses, so it cannot consume SSE at all; SSE-dependent
  tests need a real uvicorn instance. Annotated source records use
  `line`/`source` keys, not the `line_no`/`text` the Pydantic model
  suggests.
