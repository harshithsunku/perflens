# PerfLens

A real-time Linux performance profiler with a web UI. Uses `perf` to collect profiling data from a running process on a remote Linux device (ARM or x86), streams it to a local server, and displays it in real-time in a browser with line-level code annotation, interactive flame graphs, and function-level breakdowns.

## Architecture

```
[Target Device]                    [Local Machine]
   Process (PID)                     Python Server
      |                                  |
   perf collects data                    |
      |                                  |
   Device Agent ----TCP socket----> Receives & parses data
                                         |
                                    Maps to source (addr2line)
                                         |
                                    Serves HTML UI (HTTP)
                                         |
                                    Browser shows real-time view
```

**Device Agent** (`agent/perflens_agent.py`):
- Lightweight Python script that runs on the target device
- Uses `perf record` + `perf script` to collect profiling data
- Streams data over TCP using a length-prefix protocol
- Collects multiple events: cycles, instructions, cache-misses, branch-misses
- Also runs `perf stat` for summary metrics (IPC, miss rates)

**Server** (`server/perflens_server.py`):
- Python server that receives and parses perf data
- Maps function addresses to source lines using `addr2line` + debug symbols
- Serves a web UI over HTTP with real-time updates via Server-Sent Events (SSE)
- Saves profiling sessions to disk for later replay

## Prerequisites

- **Target device:** Linux (ARM or x86) with `perf` installed
- **Local machine:** Python 3.8+, `addr2line`, `readelf` (from binutils)
- **Binary:** Compiled with debug symbols (`-g`) for source-level annotation
- The local machine needs a copy of the profiled binary (with debug info)

## Quick Start

### 1. Compile your program with debug symbols

```bash
gcc -g -O0 -o myprogram myprogram.c -lm
```

### 2. Start the server

```bash
python3 server/perflens_server.py \
    --source-dir /path/to/source \
    --binary /path/to/myprogram \
    --port 9999 \
    --http-port 8080
```

### 3. Copy the agent to the target device

```bash
scp agent/perflens_agent.py user@device:/tmp/
```

### 4. Run the agent on the target device

```bash
# On the target device
./myprogram &
PID=$!
python3 /tmp/perflens_agent.py --pid $PID --server <server-ip> --port 9999
```

### 5. Open the web UI

Navigate to `http://<server-ip>:8080` in your browser.

## Features

- **Real-time function table** — sorted by CPU% with color-coded bars
- **Source code view** — line-level heat coloring (red=hot, yellow=warm)
- **Interactive flame graph** — SVG-based, hover for details
- **Multiple perf events** — switch between cycles, instructions, cache-misses, branch-misses
- **Perf stat dashboard** — IPC, cycle count, cache miss rate, branch miss rate
- **Session save/replay** — saved sessions can be reviewed later
- **ARM and x86 support** — same agent works on both architectures

## Configuration

### Server Options

| Option | Default | Description |
|---|---|---|
| `--port` | 9999 | TCP port for agent connections |
| `--http-port` | 8080 | HTTP port for web UI |
| `--source-dir` | `.` | Path to source code directory |
| `--binary` | (none) | Path to binary with debug symbols |

### Agent Options

| Option | Default | Description |
|---|---|---|
| `--pid` | (required) | PID of process to profile |
| `--server` | (none) | Server IP (omit for stdout mode) |
| `--port` | 9999 | Server port |
| `--frequency` | 99 | Sampling frequency in Hz |
| `--duration` | 3 | Duration of each collection in seconds |

## Supported Perf Events

- `cycles` — CPU cycles
- `instructions` — retired instructions
- `cache-misses` — last-level cache misses
- `branch-misses` — branch prediction misses
- `page-faults` — page faults (in perf stat)
- `task-clock` — CPU time (in perf stat)

## Troubleshooting

**perf: permission denied / perf_event_paranoid**
```bash
# Check the current setting
cat /proc/sys/kernel/perf_event_paranoid
# Set to -1 for full access (requires root on host)
sudo sysctl -w kernel.perf_event_paranoid=-1
```

**No function names in output**
- Ensure binary is compiled with `-g` (debug symbols)
- Ensure the binary is not stripped (`file myprogram` should show "not stripped")

**No source line mapping**
- Ensure `--binary` points to the exact binary running on the target (with debug info)
- Ensure `--source-dir` contains the source files

**Agent can't connect**
- Check firewall: the server port (default 9999) must be reachable
- Verify with: `nc -zv <server-ip> 9999`

## Project Structure

```
perflens/
├── agent/perflens_agent.py     # Device agent
├── server/
│   ├── perflens_server.py      # Main server (TCP + HTTP)
│   ├── parser.py               # Perf script/stat output parser
│   └── source_mapper.py        # Source code annotation via addr2line
├── ui/
│   ├── index.html              # Web UI
│   ├── style.css               # Styles
│   └── app.js                  # Frontend logic
├── test/
│   ├── sample_workload.c       # Test program
│   └── Makefile
├── sessions/                   # Saved profiling sessions
├── README.md
└── LICENSE
```

## Wire Protocol

Agent → Server communication uses a simple length-prefix protocol over TCP:
- 4 bytes: message length (uint32 big-endian)
- N bytes: UTF-8 text (perf script output + optional perf stat)

## License

MIT License. See [LICENSE](LICENSE).
