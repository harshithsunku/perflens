// Static documentation content (trusted, authored HTML — no user input).
// Ported from the vanilla UI's docs drawer.

export const DOCS_TABS: { id: string; label: string }[] = [
  { id: 'start', label: 'Getting Started' },
  { id: 'config', label: 'Configuration' },
  { id: 'ui', label: 'UI Guide' },
  { id: 'trouble', label: 'Troubleshooting' },
  { id: 'arch', label: 'Architecture' },
];

export const DOCS_HTML: Record<string, string> = {
  start: `
<section class="docs-section">
  <h3>Overview</h3>
  <p>PerfLens is a remote Linux performance profiler with a real-time web UI. Drop the agent on any Linux device (ARM or x86), point it at a PID, and watch flame graphs, function tables, <code>perf stat</code> metrics, and line-level annotated source update live in your browser.</p>
  <p>No frontend frameworks at runtime, no Docker, no sudo. The server is a small Python package (fastapi, uvicorn, orjson, zstandard) that installs entirely user-space; the agent is a single static C binary with zero dependencies.</p>
</section>
<section class="docs-section">
  <h3>Quick Start</h3>
  <h4>1. Start the Server</h4>
  <p>On the machine where you want to view profiles:</p>
  <pre><code>uvx perflens --source-dir /path/to/source --binary /path/to/myprogram

# or: pip install --user perflens
perflens serve --source-dir /path/to/src --binary /path/to/binary</code></pre>
  <p>Then open <code>http://localhost:8080</code> in your browser.</p>
  <h4>2. Install and Start the Agent</h4>
  <p>On the target Linux device (no sudo needed):</p>
  <pre><code>curl -fsSL https://raw.githubusercontent.com/harshithsunku/perflens/master/install-agent.sh | sh
# or from the server machine: perflens push-agent user@device

# Agent connects out to the server
~/.perflens/bin/perflens-agent --server &lt;server-ip&gt;

# Or: agent listens, connect from the UI wizard
~/.perflens/bin/perflens-agent --listen</code></pre>
  <p>With <code>--server</code>, the UI switches automatically when the agent connects. With <code>--listen</code>, use the <strong>Live Debug</strong> wizard.</p>
</section>
<section class="docs-section">
  <h3>Prerequisites</h3>
  <table class="docs-table">
    <thead><tr><th>Component</th><th>Needs</th></tr></thead>
    <tbody>
      <tr><td><strong>Target device</strong></td><td>Linux and <code>perf</code> — nothing else (static agent binary)</td></tr>
      <tr><td><strong>Local machine</strong></td><td>Python 3.10+ (or frozen tarball); <code>addr2line</code> + <code>readelf</code> from binutils</td></tr>
      <tr><td><strong>Binary</strong></td><td>Compiled with <code>-g</code> (debug symbols), not stripped</td></tr>
      <tr><td><strong>Source</strong></td><td>A checkout of the source tree readable from the server machine</td></tr>
      <tr><td><strong>Cross-compile</strong></td><td>(Optional) Matching toolchain with <code>&lt;prefix&gt;addr2line</code> and <code>&lt;prefix&gt;readelf</code></td></tr>
    </tbody>
  </table>
</section>`,

  config: `
<section class="docs-section">
  <h3>Server Options</h3>
  <table class="docs-table">
    <thead><tr><th>Option</th><th>Default</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>--port PORT</code></td><td>9999</td><td>TCP port the agent connects to</td></tr>
      <tr><td><code>--http-port PORT</code></td><td>8080</td><td>HTTP port for the web UI</td></tr>
      <tr><td><code>--source-dir DIR</code></td><td><code>.</code></td><td>Root of the source tree for line annotation</td></tr>
      <tr><td><code>--binary PATH</code></td><td>&mdash;</td><td>Unstripped binary (enables addr2line source mapping)</td></tr>
      <tr><td><code>--map PATH</code></td><td>&mdash;</td><td>GNU ld linker map file (optional symbol fallback)</td></tr>
      <tr><td><code>--path-map FROM=TO</code></td><td>&mdash;</td><td>Rewrite compile-time paths to local paths</td></tr>
      <tr><td><code>--addr2line PATH</code></td><td>&mdash;</td><td>Custom addr2line binary</td></tr>
      <tr><td><code>--readelf PATH</code></td><td>&mdash;</td><td>Custom readelf binary</td></tr>
      <tr><td><code>--toolchain-prefix P</code></td><td>&mdash;</td><td>Cross-compilation prefix (e.g. <code>arm-linux-gnueabihf-</code>); derives addr2line + readelf</td></tr>
      <tr><td><code>--sysroot DIR</code></td><td>&mdash;</td><td>Sysroot for resolving shared library modules and source files</td></tr>
      <tr><td><code>--max-samples N</code></td><td>500000</td><td>Ring buffer cap before oldest samples drop</td></tr>
      <tr><td><code>--inline / --no-inline</code></td><td>on</td><td>Enable/disable inline function resolution</td></tr>
      <tr><td><code>--import FILE</code></td><td>&mdash;</td><td>Import a perf.data file at startup as a session</td></tr>
      <tr><td><code>--http-bind ADDR</code></td><td>127.0.0.1</td><td>Web UI bind address (<code>0.0.0.0</code> exposes it — no auth)</td></tr>
      <tr><td><code>--browse-root DIR</code></td><td>home dir</td><td>Directory the file picker is confined to</td></tr>
      <tr><td><code>--token SECRET</code></td><td>&mdash;</td><td>Shared secret agents must present (or <code>PERFLENS_TOKEN</code>)</td></tr>
      <tr><td><code>--sessions-dir DIR</code></td><td><code>~/.perflens/sessions</code></td><td>Where saved sessions are stored</td></tr>
    </tbody>
  </table>
</section>
<section class="docs-section">
  <h3>Agent Modes</h3>
  <p>The agent has three run modes (pick one):</p>
  <table class="docs-table">
    <thead><tr><th>Mode</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>--listen</code></td><td>Daemon: bind port, wait for server to connect in via wizard</td></tr>
      <tr><td><code>--server HOST</code></td><td>Daemon: connect out to server (reconnects with backoff)</td></tr>
      <tr><td><code>--output FILE</code></td><td>Headless: collect once, write to file (<code>-</code> = stdout). Requires <code>--pid</code></td></tr>
    </tbody>
  </table>
  <h3>Agent Options</h3>
  <table class="docs-table">
    <thead><tr><th>Option</th><th>Default</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>--pid PID</code></td><td>&mdash;</td><td>PID to profile (required for <code>--output</code>; set via wizard in daemon modes)</td></tr>
      <tr><td><code>--port PORT</code></td><td>9999</td><td>TCP port (listen or connect)</td></tr>
      <tr><td><code>--frequency HZ</code></td><td>99</td><td>perf record sampling frequency</td></tr>
      <tr><td><code>--duration SECS</code></td><td>8</td><td>Length of each collection round</td></tr>
      <tr><td><code>--rounds N</code></td><td>1</td><td>Number of rounds (<code>--output</code> mode only)</td></tr>
      <tr><td><code>--token SECRET</code></td><td>&mdash;</td><td>Shared secret sent in the hello (or <code>PERFLENS_TOKEN</code>)</td></tr>
      <tr><td><code>--update</code></td><td>&mdash;</td><td>Self-update from the latest GitHub release and exit</td></tr>
    </tbody>
  </table>
</section>
<section class="docs-section">
  <h3>Cross-Compilation &amp; Toolchain</h3>
  <p>For profiling cross-compiled targets (e.g. ARM binaries on an x86 host), use the toolchain options to point at the correct binutils:</p>
  <pre><code># Using toolchain prefix (recommended)
perflens serve --toolchain-prefix arm-linux-gnueabihf- \\
    --sysroot /path/to/target/rootfs \\
    --binary  /path/to/unstripped-binary

# Or specify tools individually
perflens serve --addr2line /opt/toolchain/bin/arm-linux-gnueabihf-addr2line \\
    --readelf /opt/toolchain/bin/arm-linux-gnueabihf-readelf</code></pre>
  <p><code>--toolchain-prefix</code> appends <code>addr2line</code> and <code>readelf</code> to the prefix. If you only set <code>--addr2line</code>, PerfLens infers the matching readelf automatically.</p>
  <p><code>--sysroot</code> resolves shared library paths (like <code>/lib/libpthread.so</code>) under the sysroot tree, similar to <code>perf --symfs</code>. Source files are also looked up under sysroot as a fallback.</p>
  <p>These can also be set at runtime from the wizard's <strong>Toolchain &amp; Cross-compilation</strong> section in step 4.</p>
</section>
<section class="docs-section">
  <h3>Supported Perf Events</h3>
  <table class="docs-table">
    <thead><tr><th>Event</th><th>Use</th><th>Mode</th></tr></thead>
    <tbody>
      <tr><td><code>cycles</code></td><td>CPU time / hot paths</td><td>record + stat</td></tr>
      <tr><td><code>instructions</code></td><td>IPC, retired instructions</td><td>record + stat</td></tr>
      <tr><td><code>cache-misses</code></td><td>Last-level cache misses</td><td>record + stat</td></tr>
      <tr><td><code>cache-references</code></td><td>LLC accesses</td><td>record + stat</td></tr>
      <tr><td><code>branch-misses</code></td><td>Branch prediction misses</td><td>record + stat</td></tr>
      <tr><td><code>branch-instructions</code></td><td>Total branches</td><td>record + stat</td></tr>
      <tr><td><code>page-faults</code></td><td>Minor/major page faults</td><td>stat only</td></tr>
      <tr><td><code>context-switches</code></td><td>Scheduling pressure</td><td>stat only</td></tr>
      <tr><td><code>cpu-migrations</code></td><td>Inter-CPU movement</td><td>stat only</td></tr>
    </tbody>
  </table>
  <p>The agent probes each event on the target kernel and only collects the ones that are actually supported.</p>
</section>`,

  ui: `
<section class="docs-section">
  <h3>Setup Wizard</h3>
  <ol class="docs-list">
    <li><strong>Connect</strong> &mdash; Enter the agent's IP and port (default 9999)</li>
    <li><strong>Process</strong> &mdash; Pick a PID from the process list or enter manually</li>
    <li><strong>Perf</strong> &mdash; Auto-probes supported perf events and call-graph modes on the target</li>
    <li><strong>Binary</strong> &mdash; Path to unstripped binary, source directory, and optional toolchain/sysroot settings</li>
    <li><strong>Options</strong> &mdash; Sampling frequency (Hz) and collection duration (seconds)</li>
    <li><strong>Start</strong> &mdash; Review settings and begin profiling</li>
  </ol>
</section>
<section class="docs-section">
  <h3>Profiling View</h3>
  <ul class="docs-list">
    <li><strong>Functions tab</strong> &mdash; Ranked table of hottest functions by self% and total%. Click a row to view annotated source.</li>
    <li><strong>Source tab</strong> &mdash; Line-level annotated source with heat-colored sample counts (red = hot, green = cold).</li>
    <li><strong>Flame Graph tab</strong> &mdash; Interactive SVG flame graph. Click to zoom, double-click for source. Search bar highlights matching functions.</li>
    <li><strong>Threads tab</strong> &mdash; Per-thread CPU breakdown. Click a thread to drill into its flamegraph, function table, and per-line source view.</li>
    <li><strong>Sessions tab</strong> &mdash; List of saved sessions. Replay any session or import a <code>perf.data</code> file.</li>
  </ul>
</section>
<section class="docs-section">
  <h3>Differential View</h3>
  <p>Click <strong>Set Baseline</strong> (next to the event selector) to snapshot the current profile, or use <strong>Baseline</strong> on any saved session to compare the live profile against a previous run. With the diff toggle on:</p>
  <ul class="docs-list">
    <li>The function table gains a <strong>&Delta; Self</strong> column: percentage-point change vs the baseline (red = grew, green = shrank, <em>new</em> = not in baseline).</li>
    <li>The flame graph recolors by change: red frames grew vs the baseline, blue shrank, grey unchanged. Hover shows the exact delta.</li>
  </ul>
  <p>Diff applies to the full view; it pauses while a thread filter or time window is active.</p>
</section>
<section class="docs-section">
  <h3>Timeline Scrubbing</h3>
  <p>Drag across any Device Health sparkline to select a time range — the Functions table and Flame Graph rebuild from only the samples received in that window. A chip next to the event selector shows the active window; click &times; to return to the full session. Limited to the raw-sample ring buffer (<code>--max-samples</code>) and live sessions.</p>
</section>
<section class="docs-section">
  <h3>Keyboard Shortcuts</h3>
  <table class="docs-table">
    <thead><tr><th>Key</th><th>Action</th></tr></thead>
    <tbody>
      <tr><td><code>1</code>&ndash;<code>5</code></td><td>Switch tab (Functions / Source / Flame Graph / Threads / Sessions)</td></tr>
      <tr><td><code>/</code></td><td>Search the flame graph</td></tr>
      <tr><td><code>Ctrl+F</code></td><td>Focus search on the Flame Graph tab</td></tr>
      <tr><td><code>t</code></td><td>Toggle light / dark theme</td></tr>
      <tr><td><code>?</code></td><td>Show the shortcut help overlay</td></tr>
      <tr><td><code>Esc</code></td><td>Close dialogs and popovers</td></tr>
    </tbody>
  </table>
</section>
<section class="docs-section">
  <h3>Shareable URLs</h3>
  <p>The active tab, event, thread filter, flame-graph zoom path, and replayed session are reflected in the URL hash — refresh keeps your place, and the link can be pasted to a teammate looking at the same server.</p>
</section>
<section class="docs-section">
  <h3>Thread Filtering</h3>
  <p>When profiling multi-threaded programs, use the <strong>Thread</strong> dropdown next to the event selector to filter all views by a specific thread. The dropdown shows thread names (from <code>pthread_setname_np</code> / <code>prctl</code>) and TIDs.</p>
</section>
<section class="docs-section">
  <h3>Device Health Strip</h3>
  <p>When the agent sends system metrics, a collapsible strip shows live CPU, memory, temperature, load, network, and per-process stats with sparkline charts. Hover a sparkline to read the value at that point in time.</p>
  <p>The gear button opens <strong>metrics settings</strong>: toggle network stats, enable the opt-in <strong>disk I/O</strong> and <strong>per-thread CPU</strong> collectors (both off by default), and change the collection interval — all applied to the live agent without restarting it.</p>
</section>
<section class="docs-section">
  <h3>Export Options</h3>
  <p>Use the <strong>Export</strong> button in the header to download:</p>
  <ul class="docs-list">
    <li><strong>Flamegraph SVG</strong> &mdash; The current flame graph as a standalone SVG file</li>
    <li><strong>Collapsed Stacks</strong> &mdash; Brendan Gregg format, compatible with FlameGraph tools</li>
    <li><strong>Session JSON</strong> &mdash; Full session data including all samples and metadata</li>
  </ul>
</section>`,

  trouble: `
<section class="docs-section">
  <h3>Common Issues</h3>
  <div class="docs-trouble">
    <h4>perf_event_paranoid too high</h4>
    <p>The agent warns if <code>/proc/sys/kernel/perf_event_paranoid &gt; 1</code>. Fix:</p>
    <pre><code>sudo sysctl -w kernel.perf_event_paranoid=1</code></pre>
  </div>
  <div class="docs-trouble">
    <h4>No function names</h4>
    <p>Compile with <code>-g</code> and do not strip. Verify: <code>file ./myprogram</code> should say <code>not stripped</code> and <code>with debug_info</code>.</p>
  </div>
  <div class="docs-trouble">
    <h4>No source line mapping</h4>
    <p>Check <code>--binary</code> points at the exact unstripped binary and <code>--source-dir</code> has the source. Use <code>--path-map /build/src=/home/me/src</code> if build paths differ.</p>
  </div>
  <div class="docs-trouble">
    <h4>Agent can't connect</h4>
    <p>Server must be reachable on <code>--port</code>. Test: <code>nc -zv &lt;server-ip&gt; 9999</code></p>
  </div>
  <div class="docs-trouble">
    <h4>Container: perf record is empty</h4>
    <p>Some containers strip perf capabilities. System-wide <code>perf record -a</code> usually works as a fallback.</p>
  </div>
  <div class="docs-trouble">
    <h4>Slow startup (6&ndash;12s)</h4>
    <p>Call-graph probing tests <code>fp</code>, <code>dwarf</code>, then <code>lbr</code> sequentially. Normal on first connection.</p>
  </div>
  <div class="docs-trouble">
    <h4>Cross-compiled binary: wrong symbols</h4>
    <p>Use <code>--toolchain-prefix</code> to ensure the correct <code>addr2line</code> and <code>readelf</code> for the target architecture. System binutils can't read cross-architecture binaries.</p>
  </div>
</section>`,

  arch: `
<section class="docs-section">
  <h3>Pipeline</h3>
  <pre class="docs-arch"><code>[Target device]                    [Local machine]
  Process (PID)                      Server (perflens serve)
     |                                   |
  perf record + perf stat                |
     |                                   |
  Agent (static C binary)                |
     |                                   |
     +-- TCP (5-byte hdr + zstd) --&gt;  recv + decompress
                                         |
                                    parser + source mapper
                                         |
                                    SSE --&gt; Browser (React SPA)</code></pre>
  <p><strong>In one sentence:</strong> <code>perf record</code> &rarr; agent &rarr; TCP+zstd &rarr; server &rarr; parser &rarr; source mapper &rarr; SSE &rarr; browser.</p>
</section>
<section class="docs-section">
  <h3>Wire Protocol</h3>
  <p>Every message: 5-byte header + payload.</p>
  <table class="docs-table">
    <thead><tr><th>Field</th><th>Size</th><th>Meaning</th></tr></thead>
    <tbody>
      <tr><td>LEN</td><td>4 bytes (uint32 BE)</td><td>Payload length</td></tr>
      <tr><td>FLAG</td><td>1 byte</td><td><code>0</code>=raw, <code>1</code>=zstd, <code>2</code>=cmd req, <code>3</code>=cmd resp, <code>4</code>=health</td></tr>
      <tr><td>PAYLOAD</td><td>LEN bytes</td><td>Perf script text, JSON, or compressed data</td></tr>
    </tbody>
  </table>
  <p>Zstd compression ratio: typically <strong>20&ndash;40&times;</strong> on real perf script output.</p>
</section>
<section class="docs-section">
  <h3>HTTP API</h3>
  <p>The full, typed API schema is served at <code>/api/openapi.json</code>.</p>
  <table class="docs-table">
    <thead><tr><th>Endpoint</th><th>Method</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>/api/status</code></td><td>GET</td><td>Server + agent state, sample totals</td></tr>
      <tr><td><code>/api/stream</code></td><td>GET</td><td>SSE: status, agent, data_version, perf_stat, metrics</td></tr>
      <tr><td><code>/api/snapshot</code></td><td>GET</td><td>Cached per-event snapshot (<code>?event=</code>)</td></tr>
      <tr><td><code>/api/sessions</code></td><td>GET</td><td>List saved sessions (<code>?offset=&amp;limit=</code>)</td></tr>
      <tr><td><code>/api/sessions/&lt;id&gt;</code></td><td>GET / DELETE</td><td>Replay / delete a session</td></tr>
      <tr><td><code>/api/sessions/&lt;id&gt;/export</code></td><td>GET</td><td>Export (<code>?format=collapsed|json|svg</code>)</td></tr>
      <tr><td><code>/api/live/export</code></td><td>GET</td><td>Export the live profile</td></tr>
      <tr><td><code>/api/source</code></td><td>GET</td><td>Annotated source (<code>?file=&amp;event=&amp;tid=</code>)</td></tr>
      <tr><td><code>/api/threads</code></td><td>GET</td><td>All threads overview (<code>?event=</code>)</td></tr>
      <tr><td><code>/api/threads/&lt;tid&gt;</code></td><td>GET</td><td>Per-thread flamegraph + functions (<code>?event=</code>)</td></tr>
      <tr><td><code>/api/window</code></td><td>GET</td><td>Flamegraph + functions for a time range</td></tr>
      <tr><td><code>/api/config</code></td><td>GET / PATCH</td><td>Runtime binary/source/toolchain configuration</td></tr>
      <tr><td><code>/api/agent</code></td><td>GET / DELETE</td><td>Agent connection info / disconnect</td></tr>
    </tbody>
  </table>
</section>
<section class="docs-section">
  <h3>Key Design Decisions</h3>
  <ul class="docs-list">
    <li>Small user-space dependency set (fastapi, uvicorn, orjson, zstandard) &mdash; no sudo, no Docker, <code>uvx perflens</code> just works</li>
    <li>React + TypeScript + Vite frontend, shipped prebuilt in the wheel &mdash; no Node needed at runtime</li>
    <li><code>addr2line</code> pipelined in batches of 500 addresses</li>
    <li>Session replay is lazy: raw chunks parsed on demand</li>
    <li>Single <code>SourceMapper</code> shared across all requests</li>
    <li>Agent probes events and call-graph modes before collecting</li>
    <li>Single static C agent binary — zero runtime dependencies, self-updating</li>
    <li>Per-thread profiling: parser extracts pid/tid/comm from perf script output</li>
  </ul>
</section>`,
};
