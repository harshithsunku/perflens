# PerfLens — Project Status

Cross-session working state. Update at the start and end of every working
session. Release history lives in [CHANGELOG.md](CHANGELOG.md); this file
is what is *currently true* and what is *left to do*.

## Current phase — the stabilization pass toward 0.12.0

Started 2026-09-14 on branch `stabilize-0.12.0`, from the plan at
`~/.claude/plans/understand-this-project-and-deep-fox.md` and the agent review
in the gitignored `AGENT_FINDINGS.md` (38 findings, A-01..A-38, all
re-verified against the code before any of them was touched). Order of work:
agent → transport → server → UI → docs, hardware, release. No new features;
the four findings that need a wire change (A-18, A-25, A-28, A-31) are
deferred. Decisions taken with the user: one sampling event by default
(UI-side), musl for every release asset, both SSH beds plus a checklist for
the big-endian target, and a 0.12.0 release at the end.

- [x] **Phase 0 — baseline.** master fast-forwarded to PR #4's merge; the
      gate was green (361 pytest, 24 vitest, 10 Playwright, typecheck, ruff,
      mypy, version, OpenAPI drift, three cross builds with `soft-float ABI,
      BE8` on armeb). `tests/test_parser_compat.py` — two tests with no
      assertion, hidden by a warning filter naming a deleted file — removed;
      the `core` fixture is defined once.
- [x] **Phase 1 — agent correctness (sixth unfreeze, no wire change).**
      A-01 (size flush), A-02 (stat coverage), A-03 (call-graph probe), A-04
      (nice in rounds), A-05 (monotonic), A-06 (LC_ALL=C + parser), A-08
      (bounded reaping, process groups), A-09 (send timeouts), A-10
      (close-on-exec), A-11 (frame cap, bounded queue), A-13 (atomics), A-14
      (ids, validation, scoped args, bounded writer), A-26 (writev,
      TCP_NODELAY), A-37 (send helpers), A-38 (leftovers), plus A-21's
      logging and A-22's buffers because the files were open. Found and
      fixed on the way, not in the findings: a child forked from the command
      thread ran the agent's SIGTERM handler before exec and shut down the
      session socket — the protocol suite caught it as "agent disconnected"
      right after `stop`. **58 protocol tests** (was 43 collected), 385
      pytest in all. Cross builds green on all three local toolchains.
- [x] **Phase 2 — agent responsiveness (same unfreeze).** A-07 (probe on
      the collection thread, cancellable, `status: probing`), A-12 (10 s
      window, silent peer replaced), A-16 (overlapping rounds, free-space
      check), A-19 (metrics: open-once files, thermal zone choice, cpufreq
      holes, pre-3.14 meminfo, dynamic core count), A-20 (process list
      reads less, per-core CPU%), A-23 (batched probe, ~7 runs), A-24
      (per-pid check on switch), A-36 (wakeable sleeps). Not moved off the
      command thread, deliberately: `list_processes` (0.5 s), `verify_perf`
      (≤15 s) and `update`. `make -C agent-c check` runs C unit tests over
      the metrics parsers. **68 protocol tests.**
- [x] **Phase 3 — build, update, CI hardening.** A-30 (checksum before
      exec, via the device's `sha256sum`), A-32 (hardening flags; ASan+UBSan
      and TSan jobs — TSan was clean once `g_shutdown` and the child-pid
      slots became C11 atomics), A-33 (`--gc-sections`, strip in CI, the
      unstripped binary kept as an artifact), A-34 (musl for all five
      assets, soft-float armv7; `x86_64-linux-musl-cross.tgz`,
      `aarch64-linux-musl-cross.tgz` and `arm-linux-musleabi-cross.tgz`
      uploaded to the `toolchains` release, sha256 in the PR). The x86_64
      musl agent passed the full protocol suite natively; the ARM ones
      are `readelf`-checked here and run on hardware in Phase 7. CI asserts
      static linking and the float ABI, and shellchecks the scripts.
      **72 protocol tests.**
- [x] **Phase 4 — transport (server side of the socket).** Legacy hello
      tokens refused when the server has one configured (the item SECURITY.md
      scheduled for 0.12.0); one malformed frame no longer ends a session
      (non-object metrics, a bad stat line, `cpu: null` in the summary);
      stat-only chunks keep their counters; keepalive plus a 30 s send bound
      on every agent socket and a 1..600 s bound on relayed command
      timeouts; the accept loop survives an `accept()` error; an unwritable
      sessions dir is refused at startup and tolerated at connect; chunks
      spool through a temp name and a failed one is a gap, not an
      overwrite; `metadata.json` is written at session start and refreshed
      per chunk (`live: true`), finalized by a uvicorn shutdown hook that
      stops the agent and joins the save, and a startup sweep removes empty
      session dirs and rebuilds metadata for orphaned chunks; a replacement
      agent waits for the old receiver to finish before state is reset;
      frame caps 64 KB pre-auth / 80 MB in session / 256 MB decompressed.
      Not done: parsing off the socket thread (4.11) — measure the metrics
      gap on the hybrid bed in Phase 7 first; the agent side's stat and
      command replies no longer wait on the data send since Phase 1's
      `writev` change, so the case for it is weaker than when the plan was
      written. **9 new session tests** in `tests/test_agentlink_session.py`.
- [x] **Phase 5 — server.** The shared source mapper gets a lock per
      `addr2line` pipe and one around its cache-mutating phases; tool reads
      time out (30 s), a dead or hung tool is restarted and given up after
      three failures, and unanswered addresses are never cached or
      persisted as `??`; the mapper is closed when replaced or at shutdown.
      Snapshot cost is per chunk again: no tree copy, the worker serializes
      a changed event once (JSON + a deflate segment) and `/api/snapshot`
      splices the bytes, gzip included. Ring-derived views are memoized per
      `(generation, chunk_count)`; the parser is 2× faster (tab-led lines
      are frames); `generation`, `ring_samples` and `session_samples` on
      every version stamp; the SSE burst starts with `status`/`agent`; the
      CORS wildcard is gone; hybrid-spelled counters derive IPC and the
      miss rates (server and MCP); corrupt metadata, failed deletes, the
      replay-cache race, the on-loop import write, `--import` failures,
      unbounded `limit`, equal ports and malformed map entries all
      answer honestly. Deferred: per-sample memory layout (5.13), pending
      the soak. **32 new tests.**
- [x] **Phase 6 — UI.** Fetch bookkeeping (in-flight event switches,
      coalesced stamps, `generation` resets, bounded requests, surfaced
      snapshot failures with the ambiguous-event fallback); SSE backoff
      3 → 30 s with a distinct "server unreachable" state and no forced
      exit from replay (the banner has the button); the StrictMode boot
      bug; banners outside the hidden view with `role="alert"` and a sticky
      variant; the swallowed-error sites routed to the banner; source view
      keep-previous-data with cancellation and scroll only on file change;
      replay no longer leaks live queries; control bar keeps the agent's
      settings on a switch, shows `probing` with elapsed time, plain Stop
      and Start beside Disconnect; wizard validation, poll cap and
      cancellation, no advance on a rejected path, config restore,
      connected reset; **A-15: one sampling event by default**; search
      boxes keep spaces and report invalid patterns; delegated flame-graph
      handlers and memoized rects; `var(--…)` sparklines; the six undefined
      CSS tokens defined and the light tertiary contrast fixed; a11y
      labels, focus and tab roles; delete confirmation, `?` button, drag
      hint, live/recovered session tags. **49 vitest (was 24), 16
      Playwright scenarios (was 10).** Deferred: component render tests
      needing jsdom, the visual overhaul.
- [ ] **Phase 7 — docs, hardware pass, 0.12.0.** Done: docs drift
      (`--module-map`, `cpu-clock`/`task-clock`, the CI description, musl
      toolchains, "12 .c files", the build snippets in README, the docs
      site and CONTRIBUTING, the in-app docs for the single-event default
      and live-only thread views); hardware pass on both SSH beds (below);
      the big-endian checklist written for the user; version bumped to
      0.12.0 in all seven places. Left: docs screenshots, CI on the pushed
      branch, the release itself, and the user's big-endian run.

### Hardware pass, 2026-09-14

Both SSH beds ran the stabilization branch's musl agent, installed through
`install-agent.sh` from a local staging release (`sha256: verified` on
both). Each run: `start` with one event (`cycles`) at 99 Hz and a 60 s
interval, five minutes of SSE watched from the controller, a stop and
restart on the same pid, disconnect.

| | x86_64 container (hybrid, paranoid 0) | ARM64 board (8 cores, paranoid -1) |
|---|---|---|
| probe (`Probe finished in`) | 10.8 s | 12.7 s |
| mode, call graph | continuous, `fp` | continuous, `fp` |
| chunk interval | 60.1–60.3 s | 50.7–60.1 s (16 MB size flush) |
| chunk text → wire | 9.8 MB → 0.49 MB | 16.8 MB → 0.80 MB |
| samples per chunk | ~18,600 | ~29,900 |
| stat sections | every chunk after the first, `time_elapsed` 60.0 s each | same |
| metrics gap, max | 2.0 s (150 frames) | 2.4 s (150 frames) |
| restart on the same pid | 0.0 s, capabilities carried over | same |
| server errors / bad frames | 0 | 0 |

- **The hardware run found a regression the suite could not.** The agent
  now runs perf with `LC_ALL=C`, which prints counters without grouping,
  and the grouping-tolerant stat parser added in Phase 1 matched a
  12-digit plain count under neither of its branches: every counter except
  `page-faults` vanished from the live stat bar on both beds. Every
  committed fixture was captured under a grouping locale and the protocol
  shim printed `1,234,567`, so nothing could show it. Fixed (plain digits
  accepted first), the shim now prints C-locale numbers, and the stat
  coverage protocol test asserts the parsed values. Re-parsed from the
  spooled chunks: `cycles`, `instructions`, `task-clock` and IPC are all
  present (container IPC 2.4 on the E-cores its cpuset exposes, ARM IPC
  1.1).
- **A-02 verified.** Every chunk after the first carries exactly one stat
  round of 60.0 s: the whole interval is counted, where 0.11.0 counted
  about half of it, one chunk late.
- **A-01 verified.** On the ARM board 26 threads at 99 Hz produce ~17 MB
  of `perf script` text a minute, so chunks flush on size just before the
  interval ends; no chunk was dropped.
- **Plan item 4.11 (parse off the socket thread) is not needed.** With
  9.8 MB and 16.8 MB chunks parsed inline, the largest gap between system
  metrics frames was 2.0 s and 2.4 s against a 2 s cadence.
- **Probe time** is 10.8 s and 12.7 s, down from ~24 s of `perf` sleeps in
  0.11.0, but above the plan's "under 5 s on x86" target on the hybrid
  container, where every event is probed per PMU.
- **Self-update** on the ARM board, over an ssh reverse tunnel to a
  loopback origin: a tampered asset is refused with "checksum mismatch"
  and the binary left untouched; a missing sidecar warns and proceeds over
  curl; a matching sidecar logs "Checksum verified". A plaintext LAN origin
  is refused outright, as designed.
- **The armv7 soft-float agent** runs under the ARM board's 64-bit kernel:
  six record events, `fp`, pipe mode, probe 15.5 s, a headless round of
  2,048 samples with its stat section.
- **Not run here:** the big-endian ARMv7 target (the user's checklist, at
  `~/.perflens-testbeds/bigendian-checklist-0.12.0.md`) and the overnight
  soak.

## Previous phase — 0.11.0 released

**0.11.0 is released** (2026-09-13): tag `v0.11.0`, a GitHub release with all 21
assets, and PyPI. It carries the big-endian pass, server-side naming of frames
the target's `perf` cannot symbolize, `--perf` for a perf outside `PATH`, and
consistent `event` resolution across views and exports. PR #3 was
fast-forwarded onto master, so its commit hashes are the ones on master.

Verified after publishing: the master build ran green with `release` and
`publish to PyPI` skipped, and the tag build ran both. The published `armeb`
agent matches its checksum and reads `soft-float ABI, BE8`; the
`latest/download` agent URLs resolve; and `perflens==0.11.0` installed from
PyPI into a fresh 3.12 venv reports 0.11.0, serves the UI from inside the wheel
and answers `/api/status`.

**Two defects were caught on the way to the tag. Neither shipped.**

- **mcp 2.2 hid every MCP tool error.** CI resolved mcp 2.2.0 while the local
  venv had 2.0.0. From 2.2 the SDK passes a message through only for its own
  `ToolError`, so every next-step hint the tools raise reached agents as
  "Error executing tool". Fixed by making `PerfLensError` a `ToolError`. Six of
  the seven failing tests had already been failing in CI on the PR's first run;
  a green local suite said nothing, because the local venv could not show it.
- **Pipe mode was off on every hybrid x86 CPU.** The call-chain check added in
  the big-endian pass matched `<event>:`, which a PMU-qualified name
  (`cpu_core/cycles/:`) never contains, so continuous collection fell back to
  rounds. Nothing asserted on it: it surfaced only because the regenerated hero
  read "rounds" where 0.10.0's read "continuous". Chains are now detected by
  their indented frame lines, verified on the hybrid machine.

The release also moved "refuse legacy hello tokens" from 0.11.0 to 0.12.0 in
SECURITY.md, since pre-0.10.0 agents are still downloadable (see the correction
below).

**Correction, found 2026-09-13: the project has been public all along.** The
0.10.0 section below opens with "never been shared publicly … nothing is on
PyPI". That was written before release and never revisited: PyPI carries 0.6.0
through 0.10.0, and GitHub releases with agent binaries go back to 0.5.0. It
matters beyond wording. The 2026-08-15 decision to drop backward compatibility
rested on there being "no pre-0.10.0 agent population to protect", and there
may well be one. The decision is not reversed here, but its premise was wrong,
and the legacy hello-token path it left untested is reachable from real
installs.

### Open

- [x] **`armeb-linux-musleabi-cross.tgz` is on the `toolchains` release**
      (uploaded 2026-09-13); the `armeb` build leg is green.
- [x] **The local dev venv now runs mcp 2.2.0**, matching CI and a fresh
      install (upgraded 2026-09-13; 355 pytest pass on it). The 2.0.0 venv is
      how the MCP error regression stayed invisible locally.
- [x] **Post-release cleanup** (2026-09-13): the merged `validate-bigendian`
      branch is deleted on GitHub and locally, and the pre-rewrite commits are
      purged from the local object store. GitHub still serves them by hash
      (and through PR #3's force-push event) until GitHub Support removes them;
      that request is the owner's to file.
- [x] **Refuse legacy hello tokens when the server has a token set** —
      done in the 0.12.0 stabilization pass (Phase 4); the rejection names
      the upgrade.
- [ ] **Server RSS drift after the sample ring fills — the overnight soak never
      ran.** Deferred by the user (2026-09-13). The only run under 0.10.0 lasted
      20 minutes and ended in a deliberate stop, not a crash, with the function
      count still climbing. Detail in the 0.10.0 section.
- [x] **`/api/threads`, `/api/threads/<tid>`, `/api/window` and `/api/source`
      resolve `event` like the snapshot and exports** (2026-09-13). They
      defaulted to `event=cycles` and matched on base names, so a bare `cycles`
      merged both PMUs of a hybrid CPU and a missing event answered an empty
      200.
- [x] **perf outside `PATH`** (2026-09-13): `--perf` / `PERFLENS_PERF`, and the
      wizard, the control bar and `perflens_agent_connect` can set it on a
      running agent through `verify_perf {perf}`.
- [x] **`perflens push-agent` on big-endian ARM** (2026-09-13): it now probes
      byte order the way `install-agent.sh` does.
- [x] The armv7 agent under a 64-bit kernel (2026-09-14, the 0.12.0 musl
      soft-float build on the ARM64 bed's `CONFIG_COMPAT` kernel): probes
      six record events, `fp` call graphs and pipe mode in 15.5 s and
      collects a headless round (2,048 samples, 7.4 MB, one stat section).

### perf outside `PATH`, verified on hardware (2026-09-13)

On the ARM64 bed, with the agent's `PATH` stripped of perf (only `sleep` left,
which perf itself runs):

- **CLI.** With no `--perf` the agent warns and names the fix, then fails with
  "no perf record events", as before. A bogus `--perf` fails at startup.
  `--perf /usr/bin/perf` and `PERFLENS_PERF` both probe six record events and
  collect (398 samples in a single 3 s round).
- **Server API**, against a `--listen` agent: `verify_perf` with no path reports
  `available:false, path:perf`; a bogus path is rejected with its reason and the
  current perf kept; `/usr/bin/perf` is adopted, after which `/api/agent` and
  `status` report `perf version 7.1.5` and the path; `start` runs continuous
  mode (4,841 samples in 15 s); a swap mid-collection is refused.
- **UI, in a real browser** (Playwright, scripted, against the device): the
  wizard shows perf missing with the hint, the path field verifies and probes,
  and profiling starts. The control bar's perf field rejects a bogus path and
  resumes on the old perf, then swaps to a second valid path with a re-probe.
  The path is remembered in wizard state. No page errors.

That run found a pre-existing bug, fixed in the same change: the control bar
never appeared after a wizard start (see CHANGELOG).

**Not re-run on the big-endian target.** The mechanism is identical and that
device's single core is shared with its own services; the `armeb` agent was
cross-compiled and checked with `readelf` (soft-float, BE8) only.

## Previous phase — 0.10.0 released: the pre-launch stabilization pass

*Written before the release. Its claim that the project was unpublished was
wrong; see the correction above.* This pass closed what a pre-launch audit
turned up, so the first people who look at it find something that holds up.

**The headline finding: `--listen` was an unauthenticated remote-control
daemon.** It bound every interface, accepted any peer, and executed all 13
commands — profiling any PID, enumerating every process and command line, and
triggering a self-update that downloads and runs a binary. `--token` made it
*worse*, not better: the agent embedded the secret in its hello, which goes to
whoever completes the TCP handshake before that peer has proved anything, so a
port scanner harvested it. The server then republished it over HTTP via
`GET /api/agent`.

Fixed with **pairing-code authentication** (design chosen deliberately over
mutual HMAC — this runs in controlled environments, and a per-start random code
copied out of band is the `adb pair` model, needs no vendored crypto in the
agent, and makes the tokenless default *secure* rather than open, which is why
`--listen` can safely keep binding `0.0.0.0`). This is the **third** agent
unfreeze and the first that changes the wire protocol.

### Verified on hardware

Driven end to end on the ARM64 bed (`ssh kali@kali`, aarch64, kernel 6.12.92,
perf 7.0.12, paranoid=-1): pairing code generated and read back from the log
the way an operator does, wrong code rejected, correct code accepted, 6 events
probed, 7,483 samples collected, stop clean. `--server` mode verified with a
matching token, and rejected with a mismatched one.

**That is where a real bug in this pass's own fix surfaced.** The reconnect
backoff for an unauthenticated session incremented a delay that nothing ever
slept on, so a misconfigured fleet still reconnected once a second — 90
connections in 90 seconds, measured. The connect loop's backoff never engages
here because the TCP connect *succeeds* every time; only the auth fails. Fixed
with an explicit wait, re-measured at 1/2/4/8/16/30/30s, and covered by
`test_failed_auth_backs_off_instead_of_spinning`, which was then verified
non-vacuous by removing the sleep and watching it fail with 0.08s gaps.

### The methodology item, which mattered most

`tests/test_line_oracle.py` derives its expected line numbers from marker
comments in a hand-written C file rather than from captured output. Both
existing fixtures were produced by running the agent, which is exactly why the
0.8.0 declaration-line regression survived 165 tests and reached a docs
screenshot — a fixture cannot falsify the tool that generated it. Verified
non-vacuous: fed the frame shape the regression produced, it fails.

`tests/test_response_models.py` closes the same class of gap on the HTTP side,
and found a live bug immediately: `/api/snapshot` declared `SnapshotResponse`
for a body that is `SnapshotAllResponse`, and the wrong type had already
reached `frontend/openapi.json` and the generated TypeScript.

### State

- **Tests: 309 pytest** (was 192), 24 vitest, 10 Playwright, 10 docs shots.
- Green: pytest, ruff, **mypy** (new, clean), `check_version` on 0.10.0,
  vitest, **tsc typecheck** (new — covers the 10 `.ts` files that were in no
  tsconfig project), Playwright, OpenAPI + typegen no-drift, and pytest in
  CI's no-UI configuration (308 passed, 2 skipped).
- Docs screenshots regenerated with **both** halves (`shots` then
  `shots:live`), demo GIF re-encoded, `wire-protocol.svg` gained the
  handshake.
- New: `SECURITY.md`, issue templates, PR template.

### Still open

- [x] **Released.** Pushed to master, tagged `v0.10.0`. The push to master was
      the dress rehearsal — full `build.yml` green (wheel, five agent
      architectures, two tools bundles) with `publish to PyPI` and `release`
      showing `skipped`, confirming the tag guard by observation rather than
      by reading the YAML. `test.yml` green on 3.10–3.13. The wheel was also
      clean-room installed from a fresh 3.12 venv before tagging: serves the
      UI from inside the wheel, reports 0.10.0, `/api/snapshot` returns the
      shape it now declares.
- [x] **Three merged remote branches deleted** (`stabilize-0.8.0`,
      `copilot/review-security-issues`, `validate-0.9.0`) — each verified an
      ancestor of master first. `origin/master` is now the only branch.
- [x] **The x86_64 LXC bed was re-run on 2026-08-15** — see the pass below.
      The auth change is confirmed orthogonal to the constrained-permission
      and PMU-split paths. The pass also found an unrelated export bug that
      neither bed had caught, for the same reason the symoff bug survived.
- [x] **Frames the target's perf cannot name are now named server-side**
      (2026-08-28). A `perf` built without libelf returns `[unknown]` for
      every userspace frame; the server recovers the name from the address
      against the unstripped binary. Verified on the big-endian bed by
      replaying a saved session: **one 99.6% `[unknown]` row became 64 named
      functions**, 13,534 of 13,534 userspace frames resolved, and the profile
      reads correctly — `__aeabi_dmul` 53.4% + `__aeabi_dsub` 37.6%, i.e.
      soft-float emulation, called from `matrix_multiply_naive` /
      `matrix_multiply_blocked`. `/api/index/status` now reports the frame
      naming outcome so a blank profile is diagnosable rather than mysterious.

      **Shared libraries resolve too**, which an earlier note in this file got
      wrong by reasoning instead of testing. What decides it is the address
      form, not whether the module is a library: per-round collection emits
      file-relative offsets for `.so` frames as well (observed as
      `6195 (/lib/libpthread-2.18.so)`), and those resolve given a local copy via
      `--sysroot` or `--module-map`. Only an *absolute* address is
      unresolvable, because the load base can only be recovered by voting
      with named frames and a libelf-less perf never supplies any. Covered by
      three tests.
- [ ] Server RSS drift after the sample ring fills (carried from 0.9.0).
      **Re-measured 2026-08-15 under 0.10.0: the cap holds and the drift is
      no worse — +1.99 MB/min against 0.9.0's ~3 MB/min.** Cap reached at
      t+180s; over the following 17 minutes RSS oscillated between 1048 and
      1138 MB around a mean of 1084 while samples stayed pinned at 500,000
      and 124 further chunks arrived. So this is a regression check that
      passes, **not** an answer: the function count was still climbing
      steeply when the run ended (3,476 → 5,732, ~89/min at the end), so the
      code-discovery hypothesis was never given the chance to asymptote.
      Both measurements to date (0.9.0: 22 min, this one: 20 min) are
      structurally too short. A harness for an unattended multi-hour run now
      exists — `~/.perflens-testbeds/scripts/soak-run.sh`, documented in
      `SOAK.md` — and its verdict line distinguishes "flattening" from
      "still climbing with a static profile", which is the case that would
      disprove the hypothesis. The question stays open until it runs
      overnight.
- [x] **Big-endian is executed, not merely compiled** — closed 2026-08-28 on a
      third bed, a big-endian ARMv7 embedded target. See the pass below. The
      byte-order surface came through clean; what the bed actually broke was
      everything *around* it.

## Design — the symbolization pipeline

**Principle: the agent captures and ships bytes; the controller interprets.**
Measured against it, today's agent still runs `perf script` on the device, and
that call *is* the interpretation step. It is why the device needs a capable
`perf` at all, and it is the heaviest thing the agent runs (the code already
wraps it in `nice 5`).

This design note exists because a device turned up whose `perf` was built
without libelf. It resolves kernel frames from `/proc/kallsyms` — plain text,
no ELF parsing — and returns `[unknown]` for **every** userspace frame,
including libc. 99.6% of samples landed in one bucket.

### The four pipelines

```
P1  text       device: record + script    names from the device's perf     (today)
P2  text       device: record + script    names from controller symbol lookup
P3  perf.data  device: record only        names from controller perf --symfs
P4  text       device: record + script    device first, controller fills gaps
```

### What was measured, not assumed

| experiment | result |
|---|---|
| Push a `/tmp/perf-<pid>.map` to the device | **fails** — perf ignores it for file-backed mappings |
| Controller `perf` 6.14 reading a big-endian armv7b `perf.data` from perf 4.4 | **works** — byte-swaps, header intact |
| …with `--symfs` | **works** — `__subdf3+0xb4`, full symbols |
| …on *pipe-mode* data | **works, and recovers call chains** — 201 frames from 92 samples, where the device's own `perf script` gave 0 |
| Wire size, 8 s / 167 samples, compressed | text **1,861 B** vs perf.data **2,997 B** (~1.6× more) |

Two conclusions worth keeping. **P3 costs ~1.6× the wire bytes** — perf.data is
smaller raw but compresses far worse than repetitive text — which on an embedded
target is a good trade, since the LAN is abundant and the single CPU is not.
And **P3 independently fixes the call-graph loss**, because that loss was in the
device's old `perf script`, not in `perf record`.

### The decision ladder

The trigger should be **the data, not a probe**. A probe is synthetic and tests
one binary at one moment; a frame arriving as `[unknown]` is ground truth about
this process, right now. The useful signal is the ratio, because the partial
case (some modules resolve, some do not) is the common one and a probe misses
it entirely.

| observed | path |
|---|---|
| ~0% unknown | stay on P1 — cheapest wire, nothing to do |
| high, local symbols available | **P2** — resolve server-side; also fixes saved sessions |
| high, no local symbols, perf.data readable | **P3** — stop running `script` on the device |
| neither | degrade honestly — keep `[unknown]` and **say why** |

P3 needs negotiation rather than a flag, because it depends on the *version
relationship* between two perfs and the failing direction is real: this
project's own 2026-08-15 pass recorded controller 6.14.11 unable to read a
7.0.12 device's perf.data.

### Status

**P2 is implemented** (see the pass below). P3 and a
`--symbolize auto|device|server-symbols|server-perf` selector are **deferred by
decision**, along with the enablers they need: build-id matching, and the agent
shipping `/proc/PID/maps` so shared-library load bases can be recovered.

### Two STATUS corrections this note carries

- The 0.9.0-era item "`--binary` attributes every frame to that binary" was
  **already fixed in `source_mapper.py`** by `_binary_for_frame()`, with tests.
  It survived only in `aggregator.py`, where it caused a *different* bug: that
  call site computed a different binary than `map_samples_to_lines` did, so its
  `_addr2line_cache` lookup used a mismatched key and the per-file function
  lists silently under-reported for every non-main module. Fixed here.
- The 2026-08-28 note claiming this feature "collides with" that open item was
  written from the stale entry rather than from the code. The blocker was much
  smaller than stated.

## Test pass 2026-08-28 — the big-endian bed, and the five defects it found

**The longest-standing open item is closed.** Big-endian had been compile-only
since 0.9.0, with the note "inspection is not execution, and byte-order bugs
are invisible on little-endian hardware by construction". A third bed is now
available and the item is settled by running it.

### The bed

A **big-endian ARMv7 embedded target** — no SSH server, and no internet from
the device, which is the real deployment constraint rather than a lab
convenience.

| | |
|---|---|
| Arch | `armv7b` — 32-bit **big-endian** ARMv7 |
| ELF | `EI_DATA=MSB`, `e_flags=0x05800202` = EABI5 \| **BE-8** \| **soft-float** |
| Cores / RAM | **1** core, 996 MB |
| Kernel / perf | 4.4.0 (Buildroot) / perf 4.4.0, installed under a vendor prefix **not on `PATH`** |
| Events | **software only** — no PMU, no `arm-pmu` in `/proc/interrupts` |
| perf knobs | `paranoid=1`, `kptr_restrict=0`, kallsyms readable |

What makes it valuable is not only endianness. It is the **first PMU-less,
single-core, old-perf, no-SSH** bed, and four of the five defects below have
nothing to do with byte order — they were simply invisible on hardware that
has a PMU and a modern perf.

### The byte-order verdict: the wire path was already correct

0.9.0's static analysis holds exactly. Framing (`wire.c` `htonl`/`ntohl`
against the server's `struct '!IB'`), the pairing-code hex comparison, the
JSON payloads and the zstd container all came through clean on the first run.
Evidence: 33 chunks and 15,671 samples exchanged with no desync, auth accepted,
zstd compressing 14× on BE and decompressing on the LE server, `/proc`-derived
metrics matching the device's own `free` exactly (`mem.total_kb 995964`), and
per-thread attribution splitting 25.2 / 25.1 / 24.9 / 24.8 % across four
workers. **Byte order was never the problem. The build flags were.**

### The five defects

1. **The published `armeb` asset cannot run on real big-endian ARM.** Two
   independent reasons, both measured rather than reasoned about, by shipping
   three hello-world variants to the device:

   | build | `e_flags` | result |
   |---|---|---|
   | BE-8 + soft-float (`-march=armv7-a`) | `0x5800200` | runs |
   | BE-32 + soft-float (no `-march`) | `0x5000200` | **Illegal instruction** |
   | hard-float — *what the repo published* | `0x5000400` | **Illegal instruction** |

   The `armeb` toolchain defaults to ARMv5 and emits **BE-32**, while ARMv6+
   implements **BE-8** only; and the CPU reports no `vfp` and runs a
   soft-float userland. Fixed by building `armeb` with
   `armeb-linux-musleabi-` and `-march=armv7-a`. Soft-float also runs on VFP
   hardware, so one published asset still covers both.

2. **A PMU-less target could not be profiled at all.** `CANDIDATE_EVENTS`
   held six hardware events plus three that are all in `STAT_ONLY_EVENTS`, so
   on this device `record_event_count` was **0** and `start` failed with *"no
   perf record events available"*. Fixed by probing `cpu-clock` and
   `task-clock`. This is not an exotic case — it is most embedded and network
   hardware, which is the market a remote Linux profiler exists for.

   They are a **fallback**, probed only when the PMU yields no record event,
   not two more entries in `CANDIDATE_EVENTS`. The first version made them
   ordinary candidates and that silently took every PMU-capable target from
   six record events to eight — two extra sample streams measuring what
   `cycles` already covers, on every existing user's hardware. Caught by
   running the probe on this dev box before pushing, not by a test.

3. **Record capability was inferred from a `perf stat` probe.** `event_works()`
   asks `perf stat`, and stat accepting an event does not mean record will
   take it — which is precisely what the hardcoded `STAT_ONLY_EVENTS` list was
   papering over. The agent now measures it with a short `perf record`. The
   test shim had been modelling this exact asymmetry all along (its `stat`
   branch special-cases `task-clock`, its `record` branch does not), and it is
   what caught the naive version of fix 2.

4. **Pipe mode silently dropped call graphs.** On perf 4.4 the same capture
   gave ~10 frames per sample through a file and exactly **one leaf frame**
   per sample through `record -o - | script -i -`. `pipe_mode_works()` accepted
   it because the check was `rc == 0 && out.len > 0` — output being non-empty,
   not output being *right*. Continuous mode would have been chosen and every
   flame graph would have collapsed to a single level with nothing reporting an
   error. Now verified by comparing line count against sample count.

5. **`--toolchain-prefix` silently substituted the host `addr2line`.**
   `config.py` gated on `os.path.isfile()`, which is False for the bare
   relative name the flag documents, so the cross tool was replaced by the
   host's x86_64 one and logged only as `(system)` — symbolizing a big-endian
   binary with the wrong architecture's tool. readelf four lines below already
   fell back to `shutil.which`; addr2line was the asymmetric half, and it is
   the half that resolves source lines.

Defects 3 and 4 are the same lesson this file keeps recording, one level
further in: **assert the answer is right, not merely non-empty.**

### Found and deliberately not fixed

**A target whose `perf` cannot symbolize leaves the profile unusable, even
though the server holds everything needed to resolve it.** This perf build
resolves kernel frames from kallsyms but returns `[unknown]` for *every*
userspace frame — including `libc` and `libpthread`, so it is not a PIE or
stripping problem. 99.6% of samples land in one `[unknown]`.

The addresses are file-relative vaddrs and resolve perfectly against the
unstripped binary with the cross toolchain — `0x12018` →
`workload_compress` at `workloads.c:502`, and on the PIE build `0x125c8` →
`__aeabi_dadd` in libgcc's `ieee754-df.S`, which is the *correct* answer: on a
soft-float CPU the matrix multiply spends its time in emulated double
arithmetic. So the server could name every frame and does not, because
function names key off perf's `sym` field and `_base_candidate()` needs a
known symbol name to derive the load base.

**Now fixed** — see the design note above and the implementation entry below.
The server resolves these addresses itself, additively: a frame is renamed only
when the address lands inside a known symbol *and* addr2line independently
agrees, so an uncertain frame keeps `[unknown]` rather than acquiring a
confidently wrong name.

### Also reproduced: the export event filter, more sharply than before

The 2026-08-15 finding was **still open and confirmed on this bed** (fixed
since — see that pass), and this capture demonstrates it more cleanly because
the two events have near-equal counts:

| query | expected | actual |
|---|---|---|
| `collapsed&event=cpu-clock` | 7,261 | **14,519** (the sum of both) |
| `collapsed&event=task-clock` | 7,258 | **14,519** |
| `collapsed&event=__bogus__` | 404 | **200, 14,519** |

### Notes worth keeping

- **The agent has no way to point at a non-PATH `perf`.** `#define PERF "perf"`
  (`agent.h:59`) and this device keeps perf outside `PATH`. Launching
  with a `PATH=` prefix is the entire workaround; it works, and no agent change
  was made for it.
- **Offline transfer over the LAN is the whole story.** busybox has `wget`
  (also `tftp`, `ftpget`) but no `nc`, no `head`, no `which`, and `setsid`
  exists only as a busybox applet. `python3 -m http.server` on the controller
  plus `wget` on the device, verified by `sha256sum` on both ends, is
  sufficient and needs nothing installed on the device.
- `install-agent.sh` is the one arch resolver of the three that gets this
  device right: `uname -m` is `armv7b`, which its `arm*` case plus the
  endianness probe maps to `armeb`. `perflens push-agent` would not
  (`_ARCH_MAP` has no `armv7b` and does no endian probe) and needs ssh, which
  this device does not offer.
- **The BE build is reproducible** — rebuilding matrixlab gave a byte-identical
  sha256 to the copy already on the device.
- **Single core.** matrixlab was run at `MATRIXLAB_THREADS=4` and 49 Hz, not
  the usual 25 threads at 99 Hz; even so load average reached ~19. The
  device's own services stayed up throughout. Anything heavier on a
  single-core target is unwise.
- musl cross toolchains default to **static-pie**; the device runs those fine.
  Non-PIE was tested too and changes nothing about symbolization.

### Required follow-up before CI is green

`build.yml` now asks for `armeb-linux-musleabi-cross.tgz`, but the
`toolchains` release currently carries only `armeb-linux-musleabihf-cross.tgz`
and `aarch64_be-linux-musl-cross.tgz`. **The soft-float tarball must be
uploaded to that release or the `armeb` matrix leg will fail.** The tarball is
at `~/.perflens-testbeds/toolchains/armeb-linux-musleabi.tgz`.

**Uploaded 2026-09-13**, and the `armeb` leg has been green since. The file
unpacks to `armeb-linux-musleabi-cross/`, which is exactly the directory
the workflow derives from the asset name, so it must be uploaded *under* that
name: `armeb-linux-musleabi-cross.tgz`, not its on-disk
`armeb-linux-musleabi.tgz`.

## Test pass 2026-08-15 — the x86 bed, and one bug it found

A testing-only pass on both beds, run after the 0.10.0 release to close the
one hardware item it shipped with. No code was changed. Both beds were moved
to 0.10.0 agents first; **backward compatibility was dropped from scope by
decision** — the project has never been shared publicly, so there is no
pre-0.10.0 agent population to protect. The legacy hello-token path in
`agentlink.py` is therefore untested, not removed, and not verified.

### The x86 bed is confirmed — the auth work is orthogonal

Everything the open item asked for, on `paranoid=1` hybrid hardware:

- **All eight authentication cases.** 128-bit pairing code generated and read
  from the log the way an operator does; correct code accepted, wrong rejected;
  three failures drop the session; a silent peer dropped at **31.0 s** against
  `AUTH_TIMEOUT_SECS 30`. Every one of the 13 non-`auth` `CMD_TABLE` commands
  answers `unauthenticated` — the gate really does cover the table by
  construction.
- **The hello carries no secret, read off the wire** rather than off the
  source: no `token` field, and the code appears nowhere in the payload across
  8 reconnects. It is also absent from `GET /api/agent` and `/api/status`.
- **Reconnect backoff re-measured independently: 2.0 / 3.0 / 5.0 / 9.0 / 17.0 /
  31.0 / 31.0 s** — sleeps of 1/2/4/8/16/30/30 plus ~1 s of exchange. The bug
  0.10.0 found on ARM does not recur here.
- **`--bind` works**, exercised on hardware for the first time: bound to
  `127.0.0.1` the socket shows as `127.0.0.1:9999`, a local peer connects and a
  remote one is refused.
- **The constrained path is intact**: `perf record -a` still fails outright,
  `-p PID` works. The capability probe took **19 s**, not the ~43 s recorded in
  0.9.0 — that figure was pessimistic, not a regression baseline.
- **Hybrid PMU handling is correct.** Twelve PMU-split events; a bare `cycles`
  now returns a **400 `ambiguous_event` naming both candidates** rather than
  0.9.0's silent 404. Snapshot, `/api/threads`, `/api/threads/<tid>`,
  `/api/window` (with and without `tid`) and `/api/source` all return real data
  on both PMUs — asserted non-empty, not merely 200.
- **Line-level annotation holds on x86**, which had never been confirmed:
  `matrix_multiply.c` L14 57.7% / L43 30.5% on `cpu_core`, L14 46.7% / L15
  20.1% on `cpu_atom`. Both are real inner loops (naive and blocked multiply),
  and 9–11 distinct lines are annotated — the symoff regression would have
  collapsed them onto one.
- **Replay of a 150 k-sample x86 session**: all 12 events, 324–992 functions
  each, **every source file resolved**, 26 threads. Session metadata reports
  `0.10.0`, so the hardcoded-version bug stays fixed.
- **No empty sessions.** Roughly a dozen reconnects across the pass created
  exactly one session directory. The 332 empty directories in the sessions tree
  are all dated 2026-08-14, i.e. pre-existing debris from the bug 0.9.0 fixed.
- Opt-in disk and thread metrics stream on this bed for the first time;
  metrics history is correctly spaced (143 points, median gap 2.05 s); MCP's 19
  tools work against hybrid hardware and `perflens_status` reports all twelve
  PMU-qualified names.
- LXC metric leakage is **unchanged, not worse**: 24 host cores are reported
  against a 4-CPU cpuset, but the four saturated indices are exactly the
  cpuset's (4, 20, 22, 23), and memory is container-correct via lxcfs.

### The finding: both export endpoints ignore `event` for two of three formats

**Fixed 2026-09-13 on `validate-bigendian`.** Every format now resolves `event`
through the helper `/api/snapshot` uses; `collapsed` and `svg` require one when
a profile holds several, and `json` without one still carries them all. The
bare-`cycles` and empty-`event=` cases below are fixed with it. The new tests
assert per-event *counts* against the fixture rather than status codes, and
all seven fail against the previous server. The same inconsistency remains in
`/api/threads` and `/api/window`; see *Open* at the top.

**`/api/live/export` and `/api/sessions/<id>/export` silently ignore the
`event` parameter when `format` is `collapsed` or `json`.** `svg` is correct.

The `collapsed` case is the damaging one: it merges every event into a single
profile. On the x86 capture the exported stack counts total **364,536**, which
is the exact sum of all twelve events; the largest single event is 48,200. A
flamegraph built from that file adds cycles, instructions, cache-misses and
branch-misses together. `json` is misleading rather than wrong — it returns
`per_event` for *all* events, correctly keyed, so no data is corrupted, but the
caller who asked for one event gets a 34 MB blob of twelve. A **bogus event
name returns 200** on both.

The mechanism is visible in `_export_response` (`web.py`): the `collapsed`
branch calls `export_collapsed(all_samples)` with no event at all, the `json`
branch calls `build_per_event_data` over `get_event_types(all_samples)`, and
only the `svg` branch calls `filter_samples_by_event` — which is also why it
alone 404s on an event with no samples.

This is **not a 0.10.0 regression**; it predates the release. It survived
because the 0.9.0 device pass recorded `live_export: collapsed / json / svg all
200` — status codes, not content. That is the same lesson as the symoff bug one
level further in: assert the answer is *right*, not merely non-empty.

Related, smaller, and worth fixing alongside it: a bare `cycles` is resolved
inconsistently. `/api/snapshot` rejects it with 400 `ambiguous_event`, while
the SVG export path silently merges both PMUs (19,255 + 6,883 = 26,138 samples)
under a title that reads `cycles`. An empty `event=` returns a 65-byte SVG
with a 200.

### Also observed

- **The session listing can advertise event names the replay does not use.**
  Metadata is frozen at capture time, while replay re-parses the stored chunks
  with the current parser. The 0.9.0 import session still lists
  `cpu_atom/cycles/P` (the raw `perf script` text genuinely contains the `/P`
  precise-level modifier), but replaying it yields `cpu_atom/cycles/`. The
  parser now normalizes the modifier; the frozen metadata does not.
- **`perf.data` import from either device remains blocked** by the version gap
  (controller `perf` 6.14.11, both devices 7.0.12). PerfLens surfaces perf's
  own error cleanly as `import_failed`. Known limitation, unchanged.
- Session metadata is written when the session *ends*, not on `stop`, so a
  session is absent from `/api/sessions` until the agent disconnects. By
  design, but easy to mistake for a lost capture.
- The 32-bit ARM agent on the ARM bed (`~/bin/perflens-a32`) was **not**
  upgraded and is still 0.8.0; armv7 under 0.10.0 is untested.

## Previous phase — 0.9.0 released: the hands-on validation pass

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
works with old agents either way. The agent was unfrozen a **second** time
later in the pass, for the `--update` architecture fix below; the wire
protocol is untouched by both.

Also fixed, all found by hand: `/api/index/status` reporting 0 symbols with
a startup `--binary` (and `/api/index/files` empty for the same reason);
every agent reconnect persisting an empty session; session metadata carrying
a hardcoded `"0.5.0"`; `perflens_source_hotlines` dead-ending on a bare
filename; and an `OverflowError` that could abort a request from the
addr2line cache. Full detail in [CHANGELOG.md](CHANGELOG.md).

**The pass then moved off the dev box onto real devices (2026-08-14), and
that found a second class of defect the dev box structurally could not.**
Two reference beds: an ARM64 phone running Kali (permissive perf,
`paranoid=-1`, non-root) and an x86_64 LXC container on a hybrid P/E-core
host (`paranoid=1`, read-only, `perf record -a` unavailable). The inventory
is deliberately not in this repo — addresses and device paths would violate
the generic rule — and lives at `~/.perflens-testbeds/config.yaml` on the
machine that owns the devices.

**The headline device finding: on a hybrid P/E-core CPU, asking for `cycles`
returned nothing.** That hardware reports `cpu_core/cycles/` and
`cpu_atom/cycles/` and never a bare `cycles`, while the agent's `start`
response still advertises the plain names it asked for. The 404 from
`/api/snapshot` was the visible half; the quiet half was `filter_samples_by_event`
comparing with `==`, so `/api/threads`, `/api/window`, `/api/source` and the
live exports all defaulted to `event=cycles` and returned an *empty and
entirely plausible* result. The UI escaped only because it picks the first
name the SSE stamp offers — which is exactly why hand-driving the UI on this
same hardware, as the first half of this pass did, never surfaced it.

Two more device-only defects: `agent --update` picked its release asset from
`uname()`, which reports the *kernel's* arch, so a 32-bit agent on a 64-bit
kernel self-updated to the 64-bit binary; and the release matrix published
the 32-bit agent as `armv7l` when all three consumers ask for `armv7`,
making that the one 404 of five against the live v0.8.0 release and breaking
install, push and self-update on 32-bit ARM entirely.

Current counts: **192 pytest** (was 165, then 152), 24 vitest, 10 Playwright.

### Where the work lives

Merged to master via [PR #2](https://github.com/harshithsunku/perflens/pull/2)
and released as `v0.9.0`.

Worth keeping: pushing a feature branch runs **no** CI. `test.yml` triggers
on push to main/master plus `pull_request`, `build.yml` on push to
main/master plus `v*` tags, so a branch push with no PR open fires neither.
Opening the PR is what ran the gates — and it failed on the first attempt,
on `ruff` F811 (a duplicated import), which the local loop had never run.
`ruff check src/ tests/ tools/` is step 2 of that job and belongs in any
pre-push check.

| | commit | what |
|---|---|---|
| 1 | `86c00f3` | ip-based line recovery + agent `symoff` |
| 2 | `9085aae` | pre-index a startup `--binary` |
| 3 | `40de454` | stop saving empty sessions; real version in metadata |
| 4 | `2cb60a3` | MCP basename resolution |
| 5 | `ac4708e` | drop Tailwind/Radix + a test collecting nothing |
| 6 | `23df429` | docs corrected against the code |
| 7 | `be7116d` | binutils-independence caveat closed |
| 8 | `f6d999c` | status entry for the dev-box half |
| 9 | `24ffee2` | hybrid-CPU event resolution + halved per-sample memory |
| 10 | `9bc4296` | agent `--update` asset from compile-time macros |
| 11 | `3fcf947` | publish the 32-bit agent as `armv7` |
| 12 | `8b526bc` | docs: the device pass, and what the devices disproved |
| 13 | `35ca9d6` | drop a duplicated pytest import (ruff F811, caught by CI) |
| 14 | (release) | version 0.9.0, CHANGELOG entry, regenerated docs assets |

Verified green on the branch: 192 pytest, `check_version.py`, 24 vitest, 10
Playwright, OpenAPI + typegen regenerated, and pytest in CI's no-UI
configuration (191 passed, 1 skipped — `test_static_ui_served`, correctly).

**Read commits 1 and 9 first if you review nothing else.** Commit 1 changes
profiling output; commit 9 is the one that made whole endpoints return
believable empty results on hybrid hardware. Commit 10 is the second
deliberate exception to the agent freeze.

### Still open for 0.9.0

- [x] **The wheel does not depend on system binutils** — the caveat left
      open at 0.8.0, closed **without Docker** and more directly than a
      container would have closed it. A container proves nothing here
      unless its base image happens to lack binutils; what actually needed
      proving is behaviour when `addr2line`/`readelf` are absent, and that
      is testable by stripping `PATH`.

      Built the wheel, installed it with `uv` into a fresh 3.12 venv in an
      empty directory (no repo, no Node), and ran it with a `PATH`
      containing ordinary shell utilities and **no binutils, no perf, no
      zstd**. The server detected the tools missing, auto-provisioned the
      static bundle into `~/.perflens/bin`, and came up. `/api/status` ok,
      UI served from inside the wheel, `/api/openapi.json` 0.8.0, an
      unmatched path 404s (the `598e90b` regression still holds), and all
      eight `/api/index/status` keys present.

      The provisioned toolchain then did real work — 289 symbols through
      the provisioned `readelf`, addresses resolved to five distinct source
      lines through the provisioned `addr2line`. That also exercises the
      provisioning path against the **real** GitHub release, which the test
      suite only covers against a fake release server.

      `uvx --from <wheel> perflens serve` verified separately. The clean env
      resolved fastapi 0.141.1 / starlette 1.6.0 / uvicorn 0.52.2 — newer
      than the dev venv and inside the upper bounds, so the caps were
      exercised rather than merely declared.
- [x] **Docs screenshots regenerated for 0.9.0.** `03-source.png` had been
      advertising the bug — captured before the annotation fix, showing heat
      on a function's declaration line. It now shows the inner loop at 61.8%
      on L14. Both halves were run, in order, after the version bump.

      The first live capture was re-shot: it caught the workload at a quiet
      moment (11.7K samples, 36.9% CPU) and made a visibly weaker hero than
      0.8.0's. `MIN_SAMPLES=90000` gave a denser one — 32.6K samples in the
      selected event, 98.5% CPU, 384 functions. Worth knowing that the floor
      is tunable and that the default is not always enough for the hero.

      Both the old and new hero render PMU-qualified event names
      (`CPU_ATOM/CY…`, `cpu_core/cycles/`) because the dev box is hybrid.
      That is the status quo rather than a regression — 0.8.0's committed
      shot has exactly the same naming — and `reference.html` now explains
      it. A non-hybrid capture would read better; it needs a non-hybrid
      machine.
- [x] **Version bumped to 0.9.0** across all seven locations, agent rebuilt
      (`VERSION` is compiled in), schema and typegen regenerated. The bump
      landed *before* the screenshots on purpose: the docs drawer renders
      the version and is itself one of the shots — `11-docs-drawer.png` now
      reads v0.9.0.
- [x] **The response-model contract is still unenforced** — *(closed in
      0.10.0 by `tests/test_response_models.py`, which found a live bug on
      its first run.)* Planned this pass, not done. `_json` returns a raw Starlette `Response`, and
      FastAPI skips `response_model` entirely when a handler does that, so
      all 26 routes declare a schema nothing checks. `IndexStatus` had
      drifted to declaring 3 of the 8 fields it returns; that instance is
      fixed, **the class of defect is not**. CI checks schema-vs-committed-file
      drift, never schema-vs-reality. A test validating a few representative
      route bodies against their declared models would close it. The design
      itself is correct and should not change — orjson plus per-route gzip
      is the whole reason.
- [x] **`--binary` attributes every frame to that binary** — **closed
      2026-08-28, in two stages.** `source_mapper.py` was fixed earlier by
      `_binary_for_frame()`, which returns `--binary` only for a module that
      is plausibly the executable (deliberately *not* a basename comparison —
      the documented cross-compile workflow points `--binary` at a
      differently-named unstripped build). The last call site, in
      `aggregator.py`, was fixed with the symbolization work: it computed a
      different binary than `map_samples_to_lines` did, so its
      `_addr2line_cache` lookup used a mismatched key and the per-file
      function lists under-reported for every non-main module. `--module-map`
      now covers the remaining case, a device path that exists nowhere
      locally.
- [x] **Test coverage gaps** — *(closed in 0.10.0: `test_export.py`,
      `test_cli.py`, auth-path tests for `agentlink.py` in
      `test_agentlink_auth.py`, mypy in CI, and `npm run typecheck` covering
      the test files.)* Unchanged except for sessions:
      `agentlink.py` (569 lines) is the largest untested Python module — the
      15 agent-protocol tests drive the *C binary* and import no `perflens`
      module at all. No `test_export.py`, no `test_cli.py`. No mypy or
      pyright anywhere. The frontend has no `typecheck` script (only via
      `tsc -b && vite build`), and every test/e2e/docs-shots `.ts` file sits
      outside all tsconfig projects, so none is ever typechecked.
- [x] **`build.yml` has no `pull_request` trigger** — *(closed in 0.10.0 by
      `83ce146`.)* So wheel packaging and
      all five agent cross-compiles are still post-merge discoveries. The
      `symoff` change touches the agent, which makes this more relevant than
      it was: a cross-compile break would not surface on the PR.
- [ ] **Server memory still drifts slowly after the ring buffer is full.**
      The interning fix took the plateau from ~1.9 GB to ~1.1 GB at the
      default `--max-samples 500000`, confirmed on a 22-minute device soak
      (152 chunks, cap reached at ~3 min). But RSS went 1100 MB at t+180s to
      1157 MB at t+1330s — roughly 3 MB/min, and it did **not** visibly
      flatten. It is not the sample ring, which is capped and holding: the
      profile was still discovering code over the run (function count 464 →
      581), which grows both the aggregation dicts and the interned string
      table. That should asymptote with the target's code footprint, but
      "should" is doing real work in that sentence. Needs a multi-hour
      unattended run to confirm; at 3 MB/min it would be ~180 MB/hour if it
      does not.
- [x] **Big-endian is built but never executed.** *(Closed 2026-08-28 — see
      the 2026-08-28 pass. The static analysis below held up exactly: the
      wire path was correct. The build flags were not.)* All five release targets
      compile clean, including `armeb` and `aarch64_be` (musl toolchains,
      see the note below). Static analysis is reassuring — the framing uses
      `htonl`/`ntohl` against the server's `struct '!IB'`, payloads are
      UTF-8 text and JSON, zstd's container is little-endian by spec, and no
      multi-byte type-punning remains in `agent-c` after the `--update` fix.
      But inspection is not execution, and byte-order bugs are invisible on
      little-endian hardware by construction, which is precisely how they
      survive. Both reference devices are little-endian. Accepted
      deliberately as compile-only; do not record it as passing.
- [x] Two stale remote branches — *(deleted in 0.10.0, each verified an
      ancestor of master first.)* (`origin/stabilize-0.8.0`,
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

### Validated on real devices (2026-08-14)

All on the ARM bed unless noted. Details, including the per-device `serve`
invocations, are in `~/.perflens-testbeds/config.yaml`.

- [x] **Both connection patterns** — `--server` (agent dials out) and
      `--listen` (server dials in via `POST /api/agent/connect`).
- [x] **Token auth** — a wrong token is rejected server-side; the correct
      one is accepted.
- [x] **The full control surface** — pause/resume, live `configure`, event
      subset selection, process switching, `list_processes`.
- [x] **Per-thread and timeline reads** — `/api/threads`,
      `/api/threads/<tid>`, `/api/window` with a `tid` filter. Thread
      attribution is real, not smeared: `sort-engine-*` threads show
      `merge`/`shellsort`, `matrix-worker-*` show `matrix_multiply_naive`.
- [x] **Opt-in disk and thread metrics**, and metrics history (183 points
      over 366 s, correctly spaced).
- [x] **All six export paths**, live and from a saved session; SVG is valid
      XML.
- [x] **`perf.data` import**, replay and symbolization.
- [x] **The distribution flows** — `install-agent.sh`, `push-agent`,
      `agent --update` and `provision`, exercised against a local asset
      server via `PERFLENS_UPDATE_URL` so the real download/verify/install
      code runs without needing a re-release. `provision` was also forced
      down its download path with an empty `PATH`, and the x86 `readelf` it
      installed correctly read an AArch64 ELF.
- [x] **`--output` headless mode** — two rounds, 39,464 samples, 26 tids,
      and 13 perf-stat counters correctly merged across rounds by
      `split_perf_data`.
- [x] **32-bit ARM end to end.** The aarch64 kernel has `CONFIG_COMPAT=y`,
      and the statically linked armv7 agent needs no armhf runtime — which is the
      case the zero-dependency design exists for. Results were
      indistinguishable from the 64-bit agent: 366k samples, 464 functions,
      26 threads, 31/31 source files, `matrix_multiply.c` L14/L15 at
      37.5%/30.1% against the 64-bit run's 36.9%/32.5%.

Notes for the next session:

- **A hybrid-CPU dev box is misleading for timing.** The capability probe
  took ~43 s here against a documented ~10-20 s; that is this hardware, not
  a regression.
- **A hybrid-CPU dev box is also misleading for event names**, which is the
  more dangerous half and was only caught by testing the API rather than the
  UI. If a device reports PMU-qualified events, do not assume a bare event
  name reaches anything.
- **The armv7 and big-endian toolchains are not installable from either
  machine's package manager.** Kali's `libc6-dev:armhf` wants
  `linux-libc-dev 7.0.12` while its repo carries `6.16.0-1`, and the
  workstation has no passwordless sudo. Static musl cross toolchains are
  unpacked under `~/.perflens-testbeds/toolchains/`; put them on `PATH` and
  the Makefile's `CROSS=` targets work. Note the Makefile names
  `arm-linux-gnueabihf-`, which is *not* what is installed there — pass
  `CROSS=arm-linux-musleabihf-`.
- **`comm` truncates at 15 characters**, so `pgrep -x perflens-agent32`
  silently matches nothing and a restart script's kill step becomes a no-op,
  leaving two agents fighting over the server's single connection slot. The
  32-bit binary is deployed as `perflens-a32` to stay under the limit.

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

*Boxes below updated 2026-09-13 against the 0.9.0 "Validated by hand" and
"Validated on real devices" lists. The two left open were never recorded as
done.*

- [x] **The whole live loop on a real device**, not just loopback: agent
      connects, capability probe, continuous collection, pause / resume /
      stop, process switching, live settings changes. Nothing in this
      release touched the agent or the wire protocol, but nothing here
      re-verified them on hardware either.
- [x] **Deep recursion**, since that is what the `/api/snapshot` fix
      addresses. Profile something with stacks well past ~126 frames and
      confirm the flame graph renders truncated rather than the view going
      blank. `MAX_FLAMEGRAPH_DEPTH` is the knob.
- [x] **Source annotation against your own build** — the fix path depends on
      `--binary` pointing at an unstripped `-g` binary and on `--source-dir`
      / `--path-map` resolving. Cross-compiled targets exercise
      `--toolchain-prefix` and `--sysroot`, which nothing here covered.
- [x] **Session save → replay → diff**, including setting a baseline across
      two separate captures. Replay diffing a session against itself is all
      zeros by construction, so the differential view is only meaningfully
      testable with two real runs.
- [ ] **The MCP tools from an actual agent**, not the scripted driver used
      in Phase 5. The interesting question is whether the responses are
      *useful* for answering "why is this slow", which no assertion covers.
- [ ] **The docs site as rendered**, not just as diffs: `docs/index.html`
      hero, the 12 tour cards, the GIF. Check the screenshots still describe
      what their captions claim after any UI change you make.
- [x] **`uvx perflens` from the built wheel on a machine that is not this
      one** — *(its purpose, independence from system binutils, was closed
      in 0.9.0 by stripping `PATH`; never re-run on another machine)* — ideally in a container, which would close the caveat on the
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

- **2026-08-13** — **0.9.0 validation pass**, on branch `validate-0.9.0`
  (pushed, not merged, not released). The scope call was validation-first
  over new features, on the reasoning this file already recorded: every
  defect 0.8.0 fixed was found by running something. That paid immediately —
  **line-level source annotation had been putting every sample on its
  function's declaration line** on every modern `perf`, because
  `SCRIPT_FIELDS` omitted `symoff`. The agent was unfrozen to add it, and
  the server additionally recovers the load base from the raw ip so old
  agents and already-saved sessions are fixed too.

  Four lessons worth carrying:

  1. **The fixtures did not just fail to catch it — they encoded it.** Both
     were captured through the same `-F` path and carry zero offsets, so
     every assertion agreed with the broken behaviour. A fixture is a
     recording of one configuration, and "the tests pass" means "it still
     behaves the way it behaved when we recorded it".
  2. **A compatibility feature broke the thing it normalized.** The `-F`
     field list exists to make output consistent across kernels; older perf,
     which cannot use it, was the only configuration that worked. Prefer
     auditing what a normalization *drops*.
  3. **The instrument was wrong, not just missing.** The container check
     carried from 0.8.0 was a proxy for "does this need system binutils".
     Stripping `PATH` answered that directly and needed no Docker. Ask what
     property is under test before assuming the tool that was named for it.
  4. **`npm run shots` overwrites committed screenshots in place.** Running
     it as a CI smoke check and staging the result silently downgrades seven
     of them. Now documented in `tools/README.md`; `git checkout --
     docs/screenshots/` undoes it.

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
