---
name: perflens-profiling
description: Diagnose CPU performance problems in Linux programs using the PerfLens MCP server — find bottlenecks, hot functions, hot source lines, per-thread breakdowns, and regressions between builds. Use this whenever someone asks why a program, service, or embedded workload is slow, wants a profile or flamegraph read, mentions perf, CPU hotspots, high CPU usage, throughput or latency regressions, or asks what changed between two runs. Also use it when driving a live profiling run on a remote device. Reach for this even when PerfLens is not named — if the question is "why is this slow" and profiling data exists or could be collected, this is the method.
---

# Profiling with PerfLens

PerfLens samples a running process with `perf`, symbolizes the result down
to source lines, and serves it over an API these tools query. Your job is
to turn that into a diagnosis someone can act on: not "memcpy is hot" but
"this loop is memory-bound because it walks the array with a stride that
misses cache, here is the line."

The failure mode to avoid is answering from the first tool call. A single
function table names a symbol; it does not tell you why that symbol is
busy, whether it is one thread out of twenty, or whether the machine was
throttling at the time. Cheap extra calls buy a real answer.

## Start by orienting

Call `perflens_status` first, always. It tells you whether there is live
data or only saved sessions, which perf events actually have samples,
whether a device agent is connected, and whether symbols are loaded. Every
other tool takes a `source` argument — `"live"` or a session id from
`perflens_list_sessions` — and guessing it wrong wastes a turn.

Read the whole "Source mapping" line before concluding anything from it.
Only when it reports nothing resolvable at all — no symbols *and* no source
index — is line-level annotation genuinely unavailable, and then no further
tool call will fix it: the server needs `--binary` pointing at an unstripped
build. A zero symbol count on its own does not mean that.

## Pick the event that matches the question

Profiles are per event, and reading the wrong one produces confident
nonsense. `cycles` is the default and answers "where does wall-clock time
go". Reach for others deliberately:

- `instructions` alongside `cycles` gives IPC — the single best signal for
  whether the CPU is doing work or stalling.
- `cache-misses` / `cache-references` when you suspect memory. A
  `cache-misses` profile shows where misses happen, *not* where time goes;
  a function can dominate it while costing little.
- `branch-misses` for misprediction-heavy code.

## The drill-down loop

Work from the whole profile inward, and stop when you can point at code:

1. `perflens_hot_functions` — read it twice, once with `sort="self"` and
   once with `sort="total"`. Self finds the leaf burning cycles; total
   finds the subtree responsible for it. When they disagree, the story is
   usually in the gap: a cheap-looking caller driving an expensive leaf.
   Check the coverage line — if the top functions cover 12% of samples, the
   cost is diffuse and naming one function would be wrong.
2. `perflens_hot_stacks` — the flamegraph as ranked text. This is what
   tells you *how* the hot leaf is reached, which is usually where the fix
   belongs. A hot `memcpy` is meaningless; a hot `memcpy` under
   `parse_config → copy_defaults` in a request path is a bug.
3. `perflens_threads` on any multi-threaded target, before drawing
   conclusions from the aggregate. One saturated worker among ten idle ones
   looks like a mild hotspot process-wide, and the per-thread view
   (`perflens_thread_detail`) is the only place that shows it. These are
   live-data tools; saved sessions record which threads existed but not
   their per-thread aggregates.
4. `perflens_list_source_files` then `perflens_source_hotlines` — the
   payoff. Quote the actual hot lines back to the user with their sample
   share. This is the difference between a report they trust and one they
   have to verify themselves.

Ask for small limits and iterate. Every tool returns a ranked, capped view
and tells you the exact call for the next page, because full profiles run
to hundreds of kilobytes and will swamp your context for no benefit.

## Corroborate before concluding

Two checks separate a real diagnosis from a plausible one:

- `perflens_perf_stat` gives IPC and cache/branch miss rates. Low IPC with
  a high cache-miss rate means the code is starved on memory — a data
  layout or access pattern problem, where "optimize this function" is the
  wrong advice. Healthy IPC means the code really is doing the work.
- `perflens_device_metrics` catches the case where the profile describes a
  symptom rather than a cause: a thermally throttled board, a device that
  was swapping, or a CPU already saturated by something else entirely.

## Comparing two runs

For "what regressed", use `perflens_compare` with the known-good session as
`baseline` and the suspect run (or `"live"`) as `target`, rather than
eyeballing two function tables. Deltas are in percentage points of self
time, so differing sample counts do not distort them. Read the `appeared`
and `vanished` rows as carefully as the changed ones — a function that
appears from nothing is often the whole story, and one that vanishes may
have been inlined or renamed rather than fixed.

## Driving a live profiling run

When the user wants a fresh profile of something running on a device:

1. `perflens_agent_connect(host, port)` if the agent was started with
   `--listen`. Skip this when it was started with `--server`, since it
   connects in by itself. Expect ~10-20 s, longer on slow or hybrid-CPU
   hardware: the agent probes which perf
   events and call-graph modes work on that kernel before reporting ready.
2. `perflens_list_processes` to find the pid. This round-trips to the
   device and can take a while on a busy target.
3. `perflens_start_profiling(pid)`, optionally narrowing `events` to those
   `perflens_agent_info` reported as usable on that device.
4. **Wait a few seconds before analysing.** `perf record` flushes its ring
   buffer in batches, so the first chunk may carry only counter data. An
   immediate query returning nothing means "not yet", not "no data" —
   report the former, and re-check `perflens_status` rather than declaring
   the run empty.
5. Analyse as above, then `perflens_stop_profiling`. The run is saved as a
   session, so it stays comparable later.

## Pitfalls that produce confidently wrong answers

- **Stripped binary**: no source mapping, and function names may be
  addresses or library symbols. Ask for the unstripped build rather than
  guessing at `[unknown]` frames.
- **`perf_event_paranoid > 1`** on the device silently narrows the usable
  event set. If an event you expected is missing from `perflens_status`,
  that is usually why.
- **Kernel frames**: when the question is about user code, a kernel-heavy
  stack usually points at syscall or I/O behaviour, not at code you can
  micro-optimize.
- **Inline frames** are only expanded when the server runs with `--inline`.
  Without it, inlined functions are attributed to their caller, which can
  make a wrapper look hot.
- **`[unknown]` dominating the profile** means symbolization failed, not
  that the program spends its time in an unknown function. Say so.

## Reporting

Lead with the bottleneck and the evidence for it — function, share of
samples, the call path that reaches it, and the source lines if available.
Then the corroboration (IPC, miss rates, thread distribution). Then the
recommendation. If the data does not support a single answer — diffuse
cost, missing symbols, too few samples — say that plainly instead of
picking the top row of a table and calling it the bottleneck.
