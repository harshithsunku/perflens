#!/usr/bin/env bash
# PerfLens Server Launcher
# Interactive script to start the PerfLens server on the local machine.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_TCP_PORT=9999
DEFAULT_HTTP_PORT=8080
DEFAULT_SOURCE_DIR="$SCRIPT_DIR/test"

BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[32m'
CYAN='\033[36m'
YELLOW='\033[33m'
RESET='\033[0m'

header() {
    echo ""
    echo -e "${BOLD}${CYAN}  PerfLens Server${RESET}"
    echo -e "  ${DIM}Real-time Linux performance profiler${RESET}"
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

pick_binary() {
    echo -e "  ${BOLD}Select binary with debug symbols:${RESET}"
    echo ""

    # Find ELF binaries with debug info under common locations
    local candidates=()
    while IFS= read -r f; do
        if file "$f" 2>/dev/null | grep -q 'ELF.*not stripped'; then
            candidates+=("$f")
        fi
    done < <(find "$SCRIPT_DIR/test" "$SCRIPT_DIR" -maxdepth 2 -type f -executable 2>/dev/null | head -20)

    if [ ${#candidates[@]} -eq 0 ]; then
        echo -e "  ${DIM}No debug binaries found automatically.${RESET}"
        prompt BINARY "  Path to binary" ""
        return
    fi

    local i=1
    for c in "${candidates[@]}"; do
        echo -e "    ${GREEN}${i})${RESET} $c"
        ((i++))
    done
    echo -e "    ${GREEN}${i})${RESET} Enter path manually"
    echo ""
    prompt choice "  Choice" "1"

    if [ "$choice" -eq "$i" ] 2>/dev/null; then
        prompt BINARY "  Path to binary" ""
    elif [ "$choice" -ge 1 ] && [ "$choice" -lt "$i" ] 2>/dev/null; then
        BINARY="${candidates[$((choice - 1))]}"
    else
        BINARY="${candidates[0]}"
    fi
}

header

# --- Source directory ---
prompt SOURCE_DIR "Source code directory" "$DEFAULT_SOURCE_DIR"
if [ ! -d "$SOURCE_DIR" ]; then
    echo -e "  ${YELLOW}Warning: directory '$SOURCE_DIR' not found${RESET}"
fi

# --- Binary ---
pick_binary
if [ -n "$BINARY" ] && [ ! -f "$BINARY" ]; then
    echo -e "  ${YELLOW}Warning: binary '$BINARY' not found${RESET}"
fi

# --- Ports ---
echo ""
prompt TCP_PORT "TCP port (agent connects here)" "$DEFAULT_TCP_PORT"
prompt HTTP_PORT "HTTP port (browser UI)" "$DEFAULT_HTTP_PORT"

# --- Summary ---
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo -e "  ${DIM}──────────────────────────────────────────${RESET}"
echo -e "  ${BOLD}Source dir :${RESET} $SOURCE_DIR"
echo -e "  ${BOLD}Binary     :${RESET} ${BINARY:-<none>}"
echo -e "  ${BOLD}TCP port   :${RESET} $TCP_PORT"
echo -e "  ${BOLD}HTTP port  :${RESET} $HTTP_PORT"
echo -e "  ${BOLD}UI URL     :${RESET} ${GREEN}http://${LOCAL_IP}:${HTTP_PORT}${RESET}"
echo -e "  ${DIM}──────────────────────────────────────────${RESET}"
echo ""

# Build command
CMD=(python3 "$SCRIPT_DIR/server/perflens_server.py"
     --source-dir "$SOURCE_DIR"
     --port "$TCP_PORT"
     --http-port "$HTTP_PORT")

if [ -n "$BINARY" ]; then
    CMD+=(--binary "$BINARY")
fi

echo -e "  ${DIM}${CMD[*]}${RESET}"
echo ""
echo -e "  ${GREEN}Starting server...${RESET}  (Ctrl+C to stop)"
echo ""

exec "${CMD[@]}"
