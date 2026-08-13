#!/usr/bin/env bash
# tools/live-capture.sh — stand up a real profiling session on this machine
# for the docs screenshots and the demo GIF.
#
# Four shots and the GIF cannot come from replaying a fixture: the threads
# tab reads live server state, source annotation needs a locally resolvable
# -g binary, timeline scrubbing is disabled in replay mode, and a session
# diffed against itself is all zeros. This gets all of them without a remote
# device — the agent dials 127.0.0.1 and profiles a local workload.
#
# Everything is pinned (thread count, throttle, frequency, sample floor) so
# two runs are comparable, which the differential shot depends on.
#
# Usage:
#     tools/live-capture.sh              # run until interrupted
#     MIN_SAMPLES=40000 tools/live-capture.sh
#
# Leaves the server on $HTTP_PORT. Ctrl-C tears down the whole process group.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

HTTP_PORT="${HTTP_PORT:-8089}"
TCP_PORT="${TCP_PORT:-9899}"
FREQUENCY="${FREQUENCY:-199}"
DURATION="${DURATION:-4}"
MIN_SAMPLES="${MIN_SAMPLES:-20000}"
# Narrow the recorded event set. Hybrid-core x86 splits every event across
# two PMUs (cpu_core/cycles/, cpu_atom/cycles/), so the full probed set
# becomes 12 events and 24 counter cards with truncated labels — accurate,
# but not what a reader on ordinary hardware will see. Empty = all probed.
RECORD_EVENTS="${RECORD_EVENTS:-cycles,instructions}"
MATRIXLAB_THREADS="${MATRIXLAB_THREADS:-25}"
MATRIXLAB_THROTTLE_US="${MATRIXLAB_THROTTLE_US:-1000}"

# Never the author's real ~/.perflens: sessions saved here get screenshotted,
# and a personal session list is not what should land in docs/.
export PERFLENS_HOME="${PERFLENS_HOME:-/tmp/perflens-docs-home}"

PYTHON="${PERFLENS_PYTHON:-$REPO_ROOT/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

pids=()
cleanup() {
    for p in "${pids[@]:-}"; do
        [ -n "$p" ] && kill "$p" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM

say() { printf '\033[36m[live-capture]\033[0m %s\n' "$*"; }

# --- workload ------------------------------------------------------------
say "building matrixlab with symbols"
make -C tests/matrixlab symbols >/dev/null

# A stale symbol binary silently poisons the source shot: the replay cache
# keys on the binary *path*, not its contents, so annotations from a previous
# build survive a rebuild. Clear the caches whenever we rebuild.
say "clearing symbol caches (stale addresses would poison the source view)"
rm -rf "$PERFLENS_HOME/cache" "${HOME}/.perflens/cache" 2>/dev/null || true
mkdir -p "$PERFLENS_HOME"

say "starting matrixlab (${MATRIXLAB_THREADS} threads, ${MATRIXLAB_THROTTLE_US}us throttle)"
MATRIXLAB_THREADS="$MATRIXLAB_THREADS" \
MATRIXLAB_THROTTLE_US="$MATRIXLAB_THROTTLE_US" \
    tests/matrixlab/run.sh >/dev/null 2>&1 &
WORKLOAD_PID=$!
pids+=("$WORKLOAD_PID")
sleep 1
kill -0 "$WORKLOAD_PID" 2>/dev/null || { echo "workload died at startup" >&2; exit 1; }
say "workload pid=$WORKLOAD_PID"

# --- server --------------------------------------------------------------
say "starting perflens serve on :$HTTP_PORT"
"$PYTHON" -m perflens.cli serve \
    --http-port "$HTTP_PORT" --port "$TCP_PORT" \
    --binary tests/matrixlab/bin/matrixlab.sym \
    --source-dir tests/matrixlab >/tmp/perflens-docs-server.log 2>&1 &
pids+=("$!")

for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$HTTP_PORT/api/status" >/dev/null 2>&1 && break
    sleep 0.5
done
curl -sf "http://127.0.0.1:$HTTP_PORT/api/status" >/dev/null \
    || { echo "server never came up; see /tmp/perflens-docs-server.log" >&2; exit 1; }
say "server up"

# --- agent ---------------------------------------------------------------
say "starting agent -> 127.0.0.1:$TCP_PORT"
agent-c/perflens-agent --server 127.0.0.1 --port "$TCP_PORT" \
    >/tmp/perflens-docs-agent.log 2>&1 &
pids+=("$!")

say "waiting for agent hello + capability probe (takes ~10s)"
for _ in $(seq 1 120); do
    if curl -sf "http://127.0.0.1:$HTTP_PORT/api/agent" 2>/dev/null \
        | grep -q '"connected": *true'; then break; fi
    sleep 0.5
done

# --- collect -------------------------------------------------------------
say "starting collection on pid=$WORKLOAD_PID at ${FREQUENCY}Hz"
events_json=''
if [ -n "$RECORD_EVENTS" ]; then
    events_json=$(printf '%s' "$RECORD_EVENTS" | "$PYTHON" -c \
        'import json,sys; print(",\"events\":" + json.dumps(sys.stdin.read().strip().split(",")))')
fi
curl -sf -X POST "http://127.0.0.1:$HTTP_PORT/api/agent/command" \
    -H 'Content-Type: application/json' \
    -d "{\"cmd\":\"start\",\"args\":{\"pid\":$WORKLOAD_PID,\"frequency\":$FREQUENCY,\"duration\":$DURATION$events_json},\"timeout\":60}" \
    >/dev/null

say "waiting for >= $MIN_SAMPLES samples"
for _ in $(seq 1 240); do
    n=$(curl -sf "http://127.0.0.1:$HTTP_PORT/api/status" 2>/dev/null \
        | "$PYTHON" -c 'import sys,json; print(json.load(sys.stdin).get("total_samples",0))' 2>/dev/null || echo 0)
    if [ "${n:-0}" -ge "$MIN_SAMPLES" ]; then
        say "collected $n samples — ready"
        break
    fi
    sleep 2
done

say "server ready at http://127.0.0.1:$HTTP_PORT — Ctrl-C to tear down"
wait
