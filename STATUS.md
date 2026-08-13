# PerfLens — Project Status

Cross-session working state. Update at the start and end of every working
session. Release history lives in [CHANGELOG.md](CHANGELOG.md); this file
is what is *currently true* and what is *left to do*.

## Current phase — 0.9.0 in progress: the hands-on validation pass

**0.9.0 is the validation 0.8.0 shipped without, and it found a real one.**
The scope decision was taken deliberately (2026-08-13): rather than pick new
features, drive the profiler by hand first and fix what falls out, because
*every* defect 0.8.0 actually fixed was found by running something and
looking at the result. That held again.

**The headline finding: line-level source annotation — the project's
differentiator — was reporting each function's declaration line instead of
its hot line, on every modern `perf`.** `SCRIPT_FIELDS` asked for `sym` but
not `symoff`, so frames arrived as bare symbol names and the server fell
back to the symbol address. Measured on a live capture: 0 of 57,373 frames
carried an offset. Both committed fixtures were captured the same way, so
the entire suite agreed with the broken behaviour — and STATUS's own Phase 5
note about "real line-level heat, `>> 65.7%` on the hot line" was recording
the artifact.

Fixed on both sides: the agent now requests `symoff` (exact going forward),
and the server independently recovers each module's page-aligned load base
from the raw ip, which was in the data all along — so old agents and every
already-saved session resolve correctly too. One file went from 2 annotated
lines to 10; the profile from 76 distinct source lines to 356.

**This required unfreezing the agent**, on an explicit decision. The wire
protocol did not change, and the server-side recovery means a new server
works with old agents either way.

Also fixed, all found by hand: `/api/index/status` reporting 0 symbols with
a startup `--binary` (and `/api/index/files` empty for the same reason);
every agent reconnect persisting an empty session; session metadata carrying
a hardcoded `"0.5.0"`; `perflens_source_hotlines` dead-ending on a bare
filename; and an `OverflowError` that could abort a request from the
addr2line cache. Full detail in [CHANGELOG.md](CHANGELOG.md).

Current counts: **165 pytest** (was 152), 24 vitest, 10 Playwright.

### Still open for 0.9.0

- [ ] **`uvx perflens` in a container** — the one carried-over item still
      unverified, because this box has no Docker. Everything else on the
      0.8.0 carry-over list was driven by hand and passed; see below.
- [ ] **Docs screenshots need regenerating.** The source-annotation shot
      now shows the wrong thing — it was captured against the declaration-line
      bug. `npm run shots` then `npm run shots:live`.
- [ ] **The version is still 0.8.0.** Bump before shipping; the docs drawer
      renders it and is one of the screenshots, so bump *before* reshooting.
- [ ] Two stale remote branches (`origin/stabilize-0.8.0`,
      `origin/copilot/review-security-issues`) — both fully merged
      ancestors of master. Left alone deliberately: deleting remote
      branches is an outward-facing action and was not needed for this work.

### Validated by hand this pass

- [x] **The live loop** — connect, capability probe, continuous collection,
      pause/resume/stop, process switching, live event selection. 300k+
      samples across two captures.
- [x] **Deep recursion** — flamegraph depth capped at exactly 100
      (`MAX_FLAMEGRAPH_DEPTH`), `truncated` markers present, `/api/snapshot`
      returns 200. The 0.8.0 fix holds under a real 25-thread capture.
- [x] **Source annotation against a real build** — this is what turned up
      the headline defect.
- [x] **Session save → replay → diff across two separate captures** —
      `perflens_compare` returns real per-function deltas in percentage
      points, correctly normalized for differing sample counts.
- [x] **The MCP tools driven as an agent would** — all 19 registered;
      `perflens_status` now reports source mapping honestly, and
      `perflens_source_hotlines` returns the inner loop with context.

Note for the next session: **a hybrid-CPU dev box is misleading for
timing.** The capability probe took ~43 s here against a documented ~10-20 s;
that is this hardware, not a regression. Use the reference devices for
anything timing-sensitive, as this file has always said.

## Previous phase — 0.8.0 released

**The feature freeze that governed 0.8.0 is discharged.** It was declared on
2026-08-13 with the MCP server as the last capability added, and held until
the stabilization checklist cleared. 0.9.0 is the next line of work and is
not bound by it — decide its scope deliberately rather than inheriting the
freeze by default.

**0.8.0 is released.** `stabilize-0.8.0` fast-forwarded onto master
([PR #1](https://github.com/harshithsunku/perflens/pull/1), merged
2026-08-13), tagged `v0.8.0`, published to PyPI and to a GitHub Release with
16 assets. Verified end to end: `pip install perflens==0.8.0` into a clean
3.12 interpreter serves the UI from inside the wheel and reports 0.8.0 in
`/api/openapi.json`.

Fast-forward rather than squash on purpose — the phase table below references
commits by hash, and squashing would have orphaned every one of them while
also collapsing seven self-contained messages into one.

The merge was used as the release dress rehearsal, which is worth repeating
next time: pushing to master runs the whole of `build.yml` (wheel, five agent
architectures, binutils bundles, wheel smoke-run) with `publish to PyPI` and
`release` **skipped**, because both are guarded on `refs/tags/v*`. Those two
jobs showed `skipped` on the master run and `success` on the tag run, so the
guard is confirmed by observation rather than by reading the YAML.

Phases, in the order they landed (each one its own commit, with STATUS.md
updated in the same commit so this file is never behind the code):

| | Phase | Commit |
|---|---|---|
| 1 | Hygiene — fixture IPs, compat shims, version drift, metadata merge | `820b8ae` |
| 2 | CI actions off Node 20, three CI gates, dependency bounds | `828c4e5` |
| 3 | Version bump to 0.8.0 + CHANGELOG entry | `799261e` |
| 4 | Docs screenshots/GIF on a new Playwright harness (+ a 500 it exposed) | `2a67d0f` |
| 5 | Verification — clean-room wheel, MCP on live data | `a49bae5` |

The version bump sits *before* the docs assets on purpose: the docs drawer
renders the version and is one of the screenshots, so shooting at 0.7.0
would have committed a PNG advertising a version we do not ship.

- **Published:** 0.8.0 (PyPI, tag `v0.8.0`, 2026-08-13). Previous: 0.7.0.
- **Version:** all seven locations agree on 0.8.0, enforced by
  `tools/check_version.py` in CI. Nothing is unreleased on master.
- **0.8.0 contents:** the post-0.7.0 backlog (`cfbe5c8` AppContext split ·
  `bc6e6c5` React 19 frontend · `eb7664a` API v2 · `f847309` UX polish ·
  `598e90b` CI fix · `4a966c7` MCP server) plus the five stabilization
  commits in the table above. Full detail in [CHANGELOG.md](CHANGELOG.md).
- **CI is green** on master and on the tag (pytest 3.10–3.13, frontend
  vitest + Playwright + OpenAPI drift + docs-shots smoke, wheel + five agent
  architectures + two tools bundles). Current counts: **152 pytest,
  24 vitest, 10 Playwright, 10 docs shots.**

### Start-here for the next session

```bash
uv venv .venv && uv pip install -p .venv/bin/python -e '.[dev]'
make -C agent-c                              # protocol tests need the real binary
.venv/bin/python -m pytest tests/            # 152 tests
.venv/bin/python tools/check_version.py      # all version locations agree
.venv/bin/ruff check src/ tests/ tools/
npm --prefix frontend ci
npm --prefix frontend run test               # vitest, 24 tests
npm --prefix frontend run build              # emits into src/perflens/ui/
npm --prefix frontend run e2e                # Playwright, self-contained
```

Note the `dev` extra is what pulls in `mcp` — without it the 28 MCP tests
**skip silently** and the suite reports 124 passed / 1 skipped instead of
152. Easy to read past when you're expecting green.

Two things that bite if forgotten:

- **`VERSION` drives the agent's baked-in version**, so `make -C agent-c
  clean && make -C agent-c` after any version change or
  `test_agent_protocol.py::test_hello` fails against a stale binary.
- **The frontend is a gitignored Vite output.** A source checkout without
  `npm run build` has no `src/perflens/ui/`, which is the configuration CI
  runs in — worth reproducing locally (move the directory aside) before
  trusting a green local suite.

## Carried into 0.9.0 — shipped but not hand-validated

**Worked through in 0.9.0; kept for the reasoning.** 0.8.0 was released on
green automated suites and a scripted verification pass. It was *not* driven
by hand on real hardware first — a deliberate call to ship a coherent release
rather than hold a half-finished branch, with the validation moving to 0.9.0.

That matters because the automated suites cover what someone thought to
assert, and **every defect this release fixed was found by running something
and looking at the result, not by an assertion** — the deep-stack 500, four
identical screenshots, an empty flame graph, an MCP tool giving false advice.
Assume the same is true of what is still hiding.

**That prediction was correct.** Working this list in 0.9.0 turned up the
declaration-line annotation bug, which had been shipping since the `-F`
normalization was added and which no assertion could have caught, because
the fixtures carry the same defect. See the current-phase section at the top
for what is validated and what is still open.

A local session needs no remote device — `tools/live-capture.sh` starts the
workload, server and agent against `127.0.0.1` and waits for a sample floor:

```bash
tools/live-capture.sh            # server on :8089, matrixlab, 25 threads
# then open http://127.0.0.1:8089
```

Worth exercising specifically, roughly in order of how much of the release
touched it:

- [ ] **The whole live loop on a real device**, not just loopback: agent
      connects, capability probe, continuous collection, pause / resume /
      stop, process switching, live settings changes. Nothing in this
      release touched the agent or the wire protocol, but nothing here
      re-verified them on hardware either.
- [ ] **Deep recursion**, since that is what the `/api/snapshot` fix
      addresses. Profile something with stacks well past ~126 frames and
      confirm the flame graph renders truncated rather than the view going
      blank. `MAX_FLAMEGRAPH_DEPTH` is the knob.
- [ ] **Source annotation against your own build** — the fix path depends on
      `--binary` pointing at an unstripped `-g` binary and on `--source-dir`
      / `--path-map` resolving. Cross-compiled targets exercise
      `--toolchain-prefix` and `--sysroot`, which nothing here covered.
- [ ] **Session save → replay → diff**, including setting a baseline across
      two separate captures. Replay diffing a session against itself is all
      zeros by construction, so the differential view is only meaningfully
      testable with two real runs.
- [ ] **The MCP tools from an actual agent**, not the scripted driver used
      in Phase 5. The interesting question is whether the responses are
      *useful* for answering "why is this slow", which no assertion covers.
- [ ] **The docs site as rendered**, not just as diffs: `docs/index.html`
      hero, the 12 tour cards, the GIF. Check the screenshots still describe
      what their captions claim after any UI change you make.
- [ ] **`uvx perflens` from the built wheel on a machine that is not this
      one** — ideally in a container, which would close the caveat on the
      Phase 5 clean-room check (clean interpreter, but no Docker here, so
      independence from system binutils is unproven).

Anything this turns up is a 0.9.0 fix (or a 0.8.1 if it is severe enough to
warrant one). Since 0.8.0 is on PyPI, a bad enough finding can be yanked but
never replaced at the same version — so triage severity before deciding
whether it waits for 0.9.0.

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
- [x] **Phase 5 — the shipped wheel stands alone** (2026-08-13). Built
      `perflens-0.8.0-py3-none-any.whl`, installed it into a fresh 3.12
      interpreter in an otherwise **empty directory** — no repo, no Node, no
      `node_modules` — and served from there: `/api/status` ok, the UI came
      out of the wheel (`<title>PerfLens</title>`), `/api/openapi.json`
      reported 0.8.0, hashed assets 200, and an unmatched path 404'd rather
      than 503'd (the regression `598e90b` fixed still holds). The clean env
      resolved fastapi 0.141 / starlette 1.6 — newer than the dev venv and
      inside the new upper bounds, so the caps were exercised, not just
      declared.
      **Caveat, stated plainly:** this box has no Docker, so it is a clean
      *interpreter*, not a clean *container*. It does not prove independence
      from system binutils (`addr2line`/`readelf`), which the server probes
      at startup and degrades gracefully without. A container run is still
      worth doing somewhere that has one.
- [x] **Phase 5 — MCP driven against a live local session** (2026-08-13).
      All 19 tools registered; 9 driven end to end over a real MCP client
      session against a live 25-thread `matrixlab` capture — including the
      two families the fixtures *structurally* cannot cover: per-thread
      views (`perflens_threads` returned 25 named threads,
      `perflens_thread_detail` drilled into one) and source annotation
      (`perflens_source_hotlines` returned real line-level heat, `>> 65.7%`
      on the hot line). Both are dark to the committed fixtures, which are
      single-threaded with no locally resolvable binary.
      One defect found and fixed: `perflens_status` reported
      *"No symbols loaded: line-level source annotation needs `--binary`"*
      on a server where source annotation demonstrably worked, which would
      steer an agent away from a working feature. Root cause is server-side
      — `symbols_loaded`/`source_files_found` only count the eager
      `pre_index()` pass, which runs when a binary is configured *at
      runtime*; passing `--binary` at startup leaves them 0 while resolution
      happens lazily. Fixed in the MCP layer (report `source_index_files`
      too, warn only when nothing is resolvable) rather than by changing
      startup behaviour late in a stabilization release. **The underlying
      counters are still wrong** — see below.
- [x] **`/api/index/status` undercounts when `--binary` is passed at
      startup** — **fixed in 0.9.0.** `symbols_loaded` and
      `source_files_found` stayed 0 while `source_index_files` was populated
      and annotation worked, because only `pre_index()` set them and it ran
      on the runtime-configure path. `build_context()` now runs the same
      eager pass in a daemon thread when `cfg.binary_path` is set, so
      `serve` still comes up immediately. Reproducing it live also confirmed
      an undocumented second consequence: `/api/index/files` was empty for
      the same reason, since `_dwarf_source_files` is written by
      `pre_index()` too. Verified: 289 symbols, 35 source files, 35 DWARF
      files on a startup-`--binary` server.
      Optional extra, still open: an MCP evaluation set (10 Q/A against the
      committed fixtures, per the mcp-builder format) to catch regressions
      in tool usefulness rather than tool correctness.

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

### Release checklist (0.8.0 done — this is the recipe for 0.9.0)

All six steps ran for 0.8.0. Kept as the recipe for every future release —
the order matters in two places, flagged inline.

1. ✅ Bump **four** places — `VERSION`, `pyproject.toml`,
   `src/perflens/__init__.py`, `frontend/package.json` (+ the two `version`
   keys in `package-lock.json`). Don't check by hand: `python
   tools/check_version.py` asserts all of them plus the generated schema,
   and CI runs it.
2. ✅ `python tools/export_openapi.py && npm --prefix frontend run typegen`,
   then confirm `git diff` shows only the version line.
3. ✅ `make -C agent-c clean && make -C agent-c` (version is compiled in) —
   **before** pytest, or `test_agent_protocol.py::test_hello` fails against
   a stale binary.
4. ✅ CHANGELOG entry under a literal `## [0.8.0]` heading — `build.yml`
   awk-extracts that exact form for the release body and produces **empty
   notes silently** if it doesn't match. Verify with:
   ```bash
   awk -v v=0.8.0 '$0 ~ "^## \\[" v "\\]" {c=1;next} c&&/^## \[/{exit} c' \
       CHANGELOG.md | head
   ```
   Currently extracts 125 lines across Removed / Added / Fixed / Changed.
5. ✅ **Merge the PR.** Fast-forward, not squash — STATUS references
   phase commits by hash and squashing orphans them.

   **Merging does not publish the package.** `build.yml` runs on a push to
   master, but `publish-pypi` and `release` are both guarded by
   `if: startsWith(github.ref, 'refs/tags/v')`, so on a branch push they are
   skipped. What the merge *does* do is run the full build — wheel, five
   agent architectures, binutils bundles, wheel smoke-run — which is the
   closest pre-flight to a real release that exists, and the first time the
   bumped action versions run outside a pull request.

   **Merging does republish the docs site.** GitHub Pages is configured as
   `branch=master, path=/docs` (legacy build), so
   <https://harshithsunku.github.io/perflens/> updates on merge, not on tag.
   The new screenshots and prose go public at that moment. That is the
   desired outcome here — the currently-live site still shows pre-React
   screenshots — but it is the one outward-facing effect of merging, so
   don't merge expecting nothing to change for other people.

   Note the site will then describe 0.8.0 while `uvx perflens` still installs
   0.7.0, which predates API v2. That gap already exists on master (`6240168`
   brought the site to API v2 before the freeze); tagging is what closes it,
   so a long delay between merge and tag widens a mismatch users can see.
6. ✅ **Tag `v0.8.0`** — drives the GitHub Release and the PyPI publish via
   Trusted Publishing. **This is the irreversible step:** the publish uses
   `skip-existing: true`, so a botched 0.8.0 can be yanked but never
   replaced. One residual risk with no safe pre-flight:
   `download-artifact@v5` at `build.yml:124,316` sits in tag-gated jobs, so
   the tag is the first thing to exercise the v5 download path. An `rc` tag
   would not help — `VERSION` already reads 0.8.0, so it would publish the
   real thing. If it fails, the failure mode is a red `release` job on an
   already-published PyPI version: fix the workflow and re-run the job,
   don't re-tag.

## Known limitations (current, by design or accepted)

- Single agent connection at a time; a new agent replaces the current one.
- Per-thread views are live-only — a saved session's replay carries the
  thread list but no per-thread aggregates.
- Live `perf_stat` has no REST endpoint; it is read from the SSE head.
- Capability probing adds ~10-20 s on a typical target, longer on slow or hybrid-CPU hardware to first-connection startup.
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
- **2026-08-13** — **0.8.0 released.** `stabilize-0.8.0` fast-forwarded onto
  master, tagged, published to PyPI with 16 GitHub Release assets, verified
  by installing from PyPI into a clean interpreter. Two process notes worth
  reusing: the merge is a free release rehearsal, because `build.yml` runs in
  full on a master push while the two publish jobs skip on their
  `refs/tags/v*` guard — observed as `skipped` on the master run and
  `success` on the tag run; and fast-forward beat squash here because this
  file cites phase commits by hash. The one thing knowingly traded away:
  hands-on validation on real hardware moved to 0.9.0 rather than gating the
  release — see [Carried into 0.9.0](#carried-into-090--shipped-but-not-hand-validated).
- **2026-08-13** — All five stabilization phases complete on
  `stabilize-0.8.0`; branch left open by design at the time. The merge and the tag are
  owner decisions taken after hands-on validation, not the tail end of the
  automated work — the PyPI publish cannot be undone, and every defect this
  release actually fixed was found by *running* something rather than by an
  assertion. See [Carried into 0.9.0](#carried-into-090--shipped-but-not-hand-validated)
  for what is worth driving by hand.
- **2026-08-13** — Phase 5: verification. The wheel was proven to stand
  alone from an empty directory, and the MCP tools were driven against a
  live 25-thread capture — which is the only way the per-thread and
  source-annotation tools get exercised at all, since the fixtures are
  single-threaded with no resolvable binary. That run also caught
  `perflens_status` telling an agent source annotation was unavailable on a
  server where it worked. Recurring theme across Phases 4 and 5, worth
  keeping: **the committed fixtures are a shallow, single-threaded, 12k-sample
  profile, and a whole class of defect only appears under a real one.**
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
