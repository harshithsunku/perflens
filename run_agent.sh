#!/usr/bin/env bash
# PerfLens Agent Launcher
# Interactive script to start the PerfLens agent on a target device.
# Copy this file + agent/perflens_agent.py to the target device and run it.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_SCRIPT="$SCRIPT_DIR/agent/perflens_agent.py"
# Also check if agent is alongside this script (when copied to device)
if [ ! -f "$AGENT_SCRIPT" ]; then
    AGENT_SCRIPT="$SCRIPT_DIR/perflens_agent.py"
fi
if [ ! -f "$AGENT_SCRIPT" ]; then
    AGENT_SCRIPT="$(dirname "$0")/perflens_agent.py"
fi

DEFAULT_PORT=9999
DEFAULT_FREQ=99
DEFAULT_DURATION=8

BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[32m'
CYAN='\033[36m'
YELLOW='\033[33m'
RED='\033[31m'
RESET='\033[0m'

header() {
    echo ""
    echo -e "${BOLD}${CYAN}  PerfLens Agent${RESET}"
    echo -e "  ${DIM}Profiling agent for target devices${RESET}"
    echo ""
}

prompt() {
    local var_name=$1 prompt_text=$2 default=$3
    if [ -n "$default" ]; then
        echo -ne "  ${BOLD}${prompt_text}${RESET} ${DIM}[$default]${RESET}: "
    else
        echo -ne "  ${BOLD}${prompt_text}${RESET}: "
    fi
    read -r value
    value="${value:-$default}"
    eval "$var_name=\"$value\""
}

check_perf() {
    if ! command -v perf &>/dev/null; then
        echo -e "  ${RED}Error: 'perf' not found. Install linux-tools or perf.${RESET}"
        exit 1
    fi
}

pick_process() {
    echo -e "  ${BOLD}Select process to profile:${RESET}"
    echo ""

    # Show interesting user processes (skip kernel threads, short-lived, and this script)
    local procs
    procs=$(ps -eo pid,user,%cpu,comm --sort=-%cpu 2>/dev/null \
        | grep -v -E '^\s*(PID|$)' \
        | grep -v -E '\[(.*)\]' \
        | grep -v -E '(bash|sh|ssh|sshd|ps|grep|run_agent|perflens)' \
        | head -15)

    if [ -z "$procs" ]; then
        prompt PID "  PID to profile" ""
        return
    fi

    echo -e "    ${DIM}  PID  USER     %CPU  COMMAND${RESET}"
    local i=1
    local pids=()
    while IFS= read -r line; do
        local pid=$(echo "$line" | awk '{print $1}')
        pids+=("$pid")
        echo -e "    ${GREEN}${i})${RESET} $line"
        ((i++))
    done <<< "$procs"
    echo -e "    ${GREEN}${i})${RESET} Enter PID manually"
    echo ""
    prompt choice "  Choice" "1"

    if [ "$choice" -eq "$i" ] 2>/dev/null; then
        prompt PID "  PID" ""
    elif [ "$choice" -ge 1 ] && [ "$choice" -lt "$i" ] 2>/dev/null; then
        PID="${pids[$((choice - 1))]}"
    else
        PID="${pids[0]}"
    fi
}

header
check_perf

if [ ! -f "$AGENT_SCRIPT" ]; then
    echo -e "  ${RED}Error: Cannot find perflens_agent.py${RESET}"
    echo -e "  ${DIM}Expected at: $SCRIPT_DIR/agent/perflens_agent.py${RESET}"
    echo -e "  ${DIM}Or alongside this script: $(dirname "$0")/perflens_agent.py${RESET}"
    exit 1
fi

# --- Process selection ---
pick_process
if [ -z "$PID" ]; then
    echo -e "  ${RED}Error: No PID selected${RESET}"
    exit 1
fi
if ! kill -0 "$PID" 2>/dev/null; then
    echo -e "  ${YELLOW}Warning: PID $PID doesn't exist or no permission${RESET}"
fi

# --- Server connection ---
echo ""
prompt SERVER_IP "Server IP" ""
if [ -z "$SERVER_IP" ]; then
    echo -e "  ${RED}Error: Server IP is required${RESET}"
    exit 1
fi
prompt SERVER_PORT "Server port" "$DEFAULT_PORT"

# --- Advanced options ---
echo ""
echo -e "  ${DIM}Advanced (press Enter for defaults):${RESET}"
prompt FREQ "Sample frequency (Hz)" "$DEFAULT_FREQ"
prompt DURATION "Sample duration (seconds)" "$DEFAULT_DURATION"

# --- Summary ---
PROC_NAME=$(ps -p "$PID" -o comm= 2>/dev/null || echo "unknown")
echo ""
echo -e "  ${DIM}──────────────────────────────────────────${RESET}"
echo -e "  ${BOLD}Process    :${RESET} $PROC_NAME (PID $PID)"
echo -e "  ${BOLD}Server     :${RESET} $SERVER_IP:$SERVER_PORT"
echo -e "  ${BOLD}Frequency  :${RESET} ${FREQ} Hz"
echo -e "  ${BOLD}Duration   :${RESET} ${DURATION}s per sample"
echo -e "  ${DIM}──────────────────────────────────────────${RESET}"
echo ""

CMD=(python3 "$AGENT_SCRIPT"
     --pid "$PID"
     --server "$SERVER_IP"
     --port "$SERVER_PORT"
     --frequency "$FREQ"
     --duration "$DURATION")

# Check if we need sudo for perf
if [ "$(id -u)" -ne 0 ]; then
    echo -e "  ${YELLOW}Note: perf usually requires root. Running with sudo.${RESET}"
    CMD=(sudo "${CMD[@]}")
fi

echo -e "  ${DIM}${CMD[*]}${RESET}"
echo ""
echo -e "  ${GREEN}Starting profiler...${RESET}  (Ctrl+C to stop)"
echo ""

exec "${CMD[@]}"
