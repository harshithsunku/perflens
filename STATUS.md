# PerfLens — Project Status

Cross-session working state. Update at the start and end of every working
session. Release history lives in [CHANGELOG.md](CHANGELOG.md); this file
is what is *currently true* and what is *left to do*.

## Current phase — stabilization (feature freeze)

**The feature set is frozen as of 2026-08-13.** The MCP server was the last
capability added; from here the work is stabilization, verification and
documentation, not new surface.

**Working branch: `stabilize-0.8.0`.** The work is phased, and STATUS.md is
updated in the same commit as each phase, so a fresh session can resume from
the checklist alone. Phase order and rationale live in the plan; the short
version is: hygiene → CI/deps → version bump → docs assets → verification →
tag. The version bump sits *before* the docs assets on purpose — the docs
drawer renders the version, and it is one of the screenshots.

- **Published:** 0.7.0 (PyPI, tag `v0.7.0`).
- **Version: 0.8.0 on the branch, not yet tagged.** Bumped in Phase 3, ahead
  of the docs assets on purpose — the docs drawer renders the version and is
  one of the screenshots, so shooting at 0.7.0 would have baked a stale
  number into a committed PNG. `tools/check_version.py` enforces agreement
  across all seven locations; run it instead of hand-checking.
  `CHANGELOG.md` has its `## [0.8.0]` heading, which the release workflow
  awk-extracts for the GitHub Release body.
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
.venv/bin/python tools/check_version.py      # all version locations agree
.venv/bin/ruff check src/ tests/ tools/
npm --prefix frontend ci
npm --prefix frontend run test               # vitest, 24 tests
npm --prefix frontend run build              # emits into src/perflens/ui/
npm --prefix frontend run e2e                # Playwright, self-contained
```

Note the `dev` extra is what pulls in `mcp` — without it the 28 MCP tests
**skip silently** and the suite reports 121 passed / 1 skipped instead of
149. Easy to read past when you're expecting green.

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

- [x] **Phase 1 — the tracked fixtures were leaking device IPs** (2026-08-13)
      — not on the old checklist, found while auditing the tree.
      `tests/fixtures/session-{x86,arm}-baseline/metadata.json` carried
      `session_id`/`agent` of the form `<ts>_<ip>:9999`
      (`192.168.0.111`, `10.10.3.249`), against this project's own no-IPs
      rule — and a Sessions-tab screenshot would have published one. Renamed
      to `device-x86`/`device-arm`. Provably inert: both materializers
      (`tests/conftest.py`, `frontend/e2e/start-server.mjs`) rewrite the
      identity fields on materialize, the gzipped chunks are IP-clean
      (verified), and `metrics.json` has no addresses. `WizardView.tsx`'s
      `placeholder="192.168.1.100"` is now generic prose too.
- [x] **Phase 1 — both materializers now merge captured metadata**
      (2026-08-13) — they used to *discard* it and write a stub with
      `total_samples: 0, perf_stat: {}`, so replay rendered one empty stat
      card instead of twelve counters. They now spread the committed
      metadata and force only the identity fields. Replay of the x86 fixture
      returns 13 `perf_stat` counters, 12090 samples, platform and metrics.
      `event_types` stays `[]` on purpose — the server's per-event keys are
      authoritative (`store/live.ts` falls back to them), so a metadata list
      that disagreed would offer dead entries in the event dropdown.
- [x] **Phase 1 — compat shims retired** (2026-08-13) — `server/`,
      `src/perflens/server.py`, plus the orphaned `run_server.sh` (zero
      repo references, and its `DEFAULT_SOURCE_DIR` pointed at a `test/`
      directory that doesn't exist) and the 0-byte `requirements-server.txt`.
      `perflens.server` was a public import path in the 0.6.0 and 0.7.0
      wheels, so this is a **breaking removal** and needs a CHANGELOG
      `### Removed` entry at 0.8.0.
- [x] **Phase 1 — version drift closed mechanically** (2026-08-13) —
      `DocsDrawer.tsx` hardcoded `v0.8.0` in the *shipped UI* while the
      package was 0.7.0, wired to nothing and absent from every release
      checklist. It now renders `__PERFLENS_VERSION__`, injected by
      `vite.config.ts` from the canonical `VERSION` file (needed
      `@types/node`, since `tsconfig.node.json` typechecks the vite config
      under `strict`). `tools/check_version.py` asserts all seven locations
      agree and that no `vX.Y.Z` literal survives in `frontend/src/`;
      `frontend/src/version.test.ts` guards the injection itself.
      `frontend/package.json` had already drifted to 0.8.0 and was pulled
      back to 0.7.0 so the invariant holds until Phase 3.

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
- [x] **Phase 4 — docs screenshots and demo GIF regenerated on the React
      UI** (2026-08-13). The old assets were captured 2026-05-17, two months
      before the React rewrite, so the landing page advertised a UI that no
      longer existed. 12 stills (was 7) plus a re-recorded GIF.
      The puppeteer scripts were **deleted, not ported**: every hook they
      used was gone (`showView()`, `switchToTab()`, `renderCurrentEvent()`,
      `.fn-source-link`, a non-bubbling `new Event('change')`, direct
      `data-theme` mutation that leaves the zustand store on dark so the SVG
      renders dark on a light page) — and `typeof` guards meant they kept
      *reporting success* while capturing the landing page. That
      silent-success property is why they rotted unnoticed.
      Replaced by Playwright projects in `frontend/docs-shots/`, reusing
      `e2e/start-server.mjs` and `tools/encode-demo-gif.sh` unchanged.
      - `npm run shots` (docs-replay) captures the **full** set from a
        committed fixture — no `perf`, no agent, no device.
        `npm run shots:live` then overwrites the data-heavy subset from a
        real `tests/matrixlab` run (25 threads) via `tools/live-capture.sh`.
        Replay owns the whole set on purpose: it is what CI can execute.
      - **`test.yml` now smoke-runs the replay project on every PR** and
        asserts ≥8 PNGs over 20 KB. Nothing is committed by CI. This is the
        item that stops the next UI rewrite from silently orphaning the
        harness — the specs' assertions catch a broken harness, and the size
        check catches the subtler case of tests passing while images come
        out blank.
      - Live is needed for: threads (`/api/threads` reads live
        `all_samples`), source annotation (needs a locally built `-g`
        binary), and the GIF (a replay is a fixed dataset, so every frame
        would be identical). The function table and flame graph are shot
        live too — the fixture has no resolvable binary, so replaying it
        renders the hot path as `[unknown]` at 73%, which is accurate but a
        poor advertisement for a symbol-resolving profiler.
      - `npm run e2e` is now pinned to `--project=chromium`; without that,
        CI would start running the docs projects and fail on missing `perf`.
      Three things worth carrying forward, all found by *looking at the
      output* rather than by a failing assertion:
      1. Gating on data readiness is not gating on visibility. The first
         run produced four identical screenshots because the stat bar plus
         the health strip are taller than a 900px viewport and the content
         each shot was named after sat below the fold. Hence
         `focusContent()` and `collapseMetrics()`.
      2. `window.__perflens` is a live getter over the current layout, so it
         reports the *previous* event's rects mid-switch. Trusting it alone
         committed a screenshot of an empty flame graph; `flamegraphReady()`
         now gates on rendered SVG nodes.
      3. `perf record` batches ring-buffer flushes, so chunks land every
         2-4s no matter what `duration` says. GIF frames at the old 450ms
         interval yielded 5 distinct images out of 32; at ~900ms it is 7 of
         21, and the landing-page caption no longer claims counts "climb".
- [x] **Phase 2 — dependency upper bounds** (2026-08-13) —
      `fastapi<1.0`, `uvicorn<1.0`, `orjson<4`, `zstandard<1.0`,
      `httpx<1.0` (`mcp>=2,<3` was already bounded). Floors alone let a
      future major resolve into a fresh `uvx perflens` and break it with no
      change on our side, which defeats a long-term release; re-resolving
      under the bounds changed nothing (fastapi 0.139, pydantic 2.13,
      uvicorn 0.51, orjson 3.11, zstandard 0.25), so they document what is
      tested rather than restrict it. `starlette` and `pydantic` are left
      unconstrained on purpose — fastapi pins them transitively and a second
      constraint only creates resolver conflicts. **Cost, recorded
      deliberately:** when fastapi 1.0 ships, installs pin to the last 0.x
      until someone cuts a release. `ruff` is pinned exactly (`==0.15.22`)
      in both `pyproject.toml` and `build.yml`.
      `frontend/openapi.json` stays committed: `npm run typegen` consumes it
      into the shipped bundle, so generating it in CI would make the
      frontend's types depend on CI's resolver.
- [x] **Phase 2 — Node 20 deprecation** (2026-08-13) — 17 call sites:
      `checkout` v4→v5 (×5), `setup-python` v5→v6 (×3), `setup-node`
      v4→v5 (×2), `upload-artifact` v4→v5 (×4), `download-artifact` v4→v5
      (×2), `softprops/action-gh-release` v2→v3 (×1).
      `pypa/gh-action-pypi-publish@release/v1` is a rolling ref, no bump.
      upload/download moved in one commit — v5 artifacts are unreadable by
      a v4 download, and they cross jobs (`python-package` uploads,
      `publish-pypi` and `release` download).
      **Residual risk:** `build.yml`'s two `download-artifact` sites are
      tag-gated, so the v5 download path is first exercised by the `v0.8.0`
      tag itself. There is no safe pre-flight — an `rc` tag would publish
      the real 0.8.0 to PyPI, since `VERSION` already reads 0.8.0 by then.
      Failure mode is a red `release` job on an already-published version,
      recoverable by fixing the workflow and re-running the job.
- [x] **Phase 2 — three CI gaps closed** (2026-08-13), all found by
      auditing the workflows rather than by a failure.
      (1) `ruff` ran only in `build.yml`, which has no `pull_request`
      trigger — **no PR had ever been linted**. Moved into `test.yml` and
      widened to `src/ tests/ tools/`, which was already clean.
      (2) The typegen drift check `CONTRIBUTING.md` claimed CI performed
      did not exist; added, and verified locally to be in sync.
      (3) The OpenAPI drift and version checks were missing from
      `build.yml`'s release path, so a tag could ship a wheel whose schema
      disagreed with the committed artifact the TS types came from.
      `build.yml` deliberately did **not** get a `pull_request` trigger —
      it compiles binutils and five agent architectures.
- [x] **Starlette deprecation — resolved by the dependency graph, not by us**
      (2026-08-13). `TestClient` warned that `httpx` support was deprecated
      in favour of `httpx2`. But `mcp>=2` requires `httpx2>=2.5`, and
      starlette prefers `httpx2` whenever it is importable — so the warning
      stops firing the moment the `dev` extra is installed, which is what CI
      does. Worth knowing: `src/perflens/mcp/client.py` still imports
      `httpx` directly (7 call sites, 5 of them exception handlers), so both
      libraries are installed side by side and the `httpx` bound is what
      keeps that working. No code change made.
- [ ] **Phase 5 — clean-container `uvx` run** of the built wheel: no Node, no
      binutils, no repo — confirms the shipped artifact stands alone.
- [ ] **Phase 5 — MCP against a live local session.** The tools have 28 tests
      against fixture sessions and were driven end to end over stdio against
      a live server. The gap that matters is the two tool families the
      fixtures *structurally* cannot cover: per-thread views (both fixtures
      are single-threaded) and source annotation (no locally-resolvable
      binary). A `tests/matrixlab` capture covers both. Optional extra: an
      evaluation set (10 Q/A against the committed fixtures, per the
      mcp-builder format) to catch regressions in tool usefulness.

### Deferred past 0.8.0

Explicit decisions, recorded so a later session doesn't re-litigate them.

- **Device E2E matrix** — full live run on both reference devices. Deferred:
  needs hardware this session doesn't have, and the local `matrixlab`
  capture exercises the same server-side paths.
- **Scale tests** — ~1 h continuous-collection RSS boundedness and a
  synthetic ~500k-file source tree. Deferred: hours of wall-clock for a
  property no recent change touches.
- **MCP against a live *device*** — the local-session leg lands in Phase 5;
  the device leg travels with the device E2E matrix above.
- **The IPs in git history.** Device addresses and ssh targets in commits
  before 2026-08-13 remain reachable. Clearing them needs `git filter-repo`
  + force-push, which rewrites every SHA, breaks existing clones and
  orphans the `v0.7.0` tag. **Decision (2026-08-13): not doing it.** They
  are private-range addresses with low practical exposure, and a stable
  long-term release is a bad moment to invalidate every clone.
  Note carefully: Phase 1 sanitized the *tracked* fixture metadata, but the
  old blobs are still in history. "We cleaned the IPs" is not the same as
  "the IPs are gone".

### Release checklist for 0.8.0 (when the above is clear)

1. Bump **four** places — `VERSION`, `pyproject.toml`,
   `src/perflens/__init__.py`, `frontend/package.json` (+ the two `version`
   keys in `package-lock.json`). Don't check by hand: `python
   tools/check_version.py` asserts all of them plus the generated schema.
2. `python tools/export_openapi.py && npm --prefix frontend run typegen`,
   then confirm `git diff` shows only the version line.
3. `make -C agent-c clean && make -C agent-c` (version is compiled in) —
   **before** pytest, or `test_agent_protocol.py::test_hello` fails against
   a stale binary.
4. Write the CHANGELOG entry from the unreleased commits, under a literal
   `## [0.8.0]` heading — `build.yml` awk-extracts that exact form for the
   release body and produces **empty notes silently** if it doesn't match.
   Include a `### Removed` entry for `perflens.server`.
5. Tag `v0.8.0` — the tag drives the GitHub Release and the PyPI publish
   via Trusted Publishing. PyPI publish uses `skip-existing: true`, so a
   botched version can only be yanked, never replaced. Run the
   clean-container wheel check *before* tagging.

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
- **2026-08-13** — Phase 4: docs assets regenerated on a new Playwright
  harness, and a real bug fell out of it. Standing up a live 25-thread
  `matrixlab` capture made `/api/snapshot` return **500**: orjson cannot
  encode past a fixed nesting depth (254 containers), a flamegraph level
  costs two of them, so a stack deeper than ~126 frames could not be
  serialized at all — and the failure blanked the entire UI rather than
  rendering one deep stack short. `_copy_tree` had already been made
  iterative for Python's own recursion limit; the *serializer* limit is a
  separate constraint nobody had hit, because the committed fixtures are
  shallow. Capped at `MAX_FLAMEGRAPH_DEPTH`, cut points marked
  `truncated`, three regression tests. The general lesson: the fixtures are
  a single-threaded 12k-sample profile, and several classes of defect only
  appear under a genuinely heavy one.
- **2026-08-13** — Phase 3: version bumped to 0.8.0 across all seven
  locations, CHANGELOG restructured into a real `## [0.8.0]` entry with a
  `### Removed` note for `perflens.server`. The first PR CI run also
  confirmed the Phase 2 gates actually execute — lint, version consistency
  and typegen drift all ran green on `pull_request`, which none of them had
  ever done before.
- **2026-08-13** — Phase 2: CI and dependency hardening. 17 action call
  sites off Node 20, upper bounds on every runtime dependency, ruff pinned,
  and three CI gaps closed. The one worth remembering: `build.yml` has no
  `pull_request` trigger, so putting a check there means it only runs
  *after* merge — lint had been in that position since it was added. When
  adding a gate, check which workflow actually gates PRs.
- **2026-08-13** — Phase 1 of the 0.8.0 stabilization, on branch
  `stabilize-0.8.0`: fixture IPs sanitized, compat shims and orphans
  deleted, version drift closed mechanically, both fixture materializers
  switched from discarding captured metadata to merging it. Ended green —
  149 pytest, 24 vitest (2 new), 10 Playwright, ruff clean on the widened
  `src/ tests/ tools/` scope. Two things worth carrying forward: the
  *tracked* fixtures were leaking device IPs, which the old checklist had
  missed by tracking only git history; and the puppeteer capture scripts
  were not merely stale but **silently succeeding** — `typeof` guards on
  deleted globals meant they reported success while shooting the wrong
  page. Prefer a harness that fails loudly, which is what the CI smoke job
  in Phase 4 is for.
- **2026-08-13** — MCP server + companion skill (`4a966c7`), then feature
  freeze declared and the version held at 0.7.0 (`531f27b`). Notes: the MCP
  Python SDK is on **2.x** (`MCPServer`, not 1.x's `FastMCP`;
  `input_schema`, not `inputSchema`) — check the installed package, the
  reference docs in circulation are still 1.x. httpx's ASGI transport
  buffers whole responses, so it cannot consume SSE at all; SSE-dependent
  tests need a real uvicorn instance. Annotated source records use
  `line`/`source` keys, not the `line_no`/`text` the Pydantic model
  suggests.
