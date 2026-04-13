// PerfLens Web UI

let state = {
    totalSamples: 0,
    chunkCount: 0,
    eventTypes: [],
    selectedEvent: 'cycles',
    perEvent: {},           // {event_type: {function_summary, flamegraph, source_files, source}}
    perfStat: {},
    currentSourceFile: null,
    lastUpdateTime: null,
    isReplayMode: false,
    replaySessionId: null,
    flamegraphZoom: null,   // null or {name, node}
    sourceFiles: [],        // [{path, found, total_samples, functions}] for current event
};

let evtSource = null;
let flamegraphRects = [];   // parallel to SVG <g> elements for click handling
let errorTimer = null;
let fgClickTimer = null;    // single-click vs double-click disambiguation
let fgContextMenu = null;   // persistent context menu element

// --- Stat card config ---
const STAT_ORDER = [
    'ipc', 'cycles', 'instructions', 'cache-misses', 'cache-references',
    'branch-misses', 'branch-instructions', 'branch_miss_rate',
    'page-faults', 'context-switches', 'cpu-migrations', 'task-clock'
];
const STAT_LABELS = {
    'ipc': 'IPC', 'cycles': 'Cycles', 'instructions': 'Instructions',
    'cache-misses': 'Cache Miss', 'cache-references': 'Cache Refs',
    'branch-misses': 'Branch Miss', 'branch-instructions': 'Branches',
    'branch_miss_rate': 'Br Miss %', 'page-faults': 'Page Faults',
    'context-switches': 'Ctx Switch', 'cpu-migrations': 'CPU Migr',
    'task-clock': 'Task Clock',
};

// --- Utilities ---
function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatNumber(n) {
    if (n === undefined || n === null) return '--';
    if (typeof n === 'number' && !Number.isInteger(n)) return n.toFixed(2);
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
}

function formatStatValue(key, value) {
    if (key === 'ipc') return value.toFixed(2);
    if (key === 'branch_miss_rate') return value.toFixed(1) + '%';
    if (typeof value === 'number' && !Number.isInteger(value)) return value.toFixed(1);
    return formatNumber(value);
}

function debounce(fn, delay) {
    let timer;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), delay);
    };
}

function hashCode(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        hash = ((hash << 5) - hash) + str.charCodeAt(i);
        hash |= 0;
    }
    return Math.abs(hash);
}

// --- Error banner ---
function showError(msg) {
    const banner = document.getElementById('error-banner');
    document.getElementById('error-text').textContent = msg;
    banner.classList.add('visible');
    clearTimeout(errorTimer);
    errorTimer = setTimeout(() => banner.classList.remove('visible'), 5000);
}

function hideError() {
    document.getElementById('error-banner').classList.remove('visible');
    clearTimeout(errorTimer);
}

document.getElementById('error-close').addEventListener('click', hideError);

// --- Replay banner ---
function showReplayBanner(sessionId, timestamp) {
    state.isReplayMode = true;
    state.replaySessionId = sessionId;
    const banner = document.getElementById('replay-banner');
    document.getElementById('replay-text').textContent =
        '\u23EA REPLAY MODE \u2014 Session: ' + sessionId + ' from ' + (timestamp || '');
    banner.classList.add('visible');
}

function hideReplayBanner() {
    state.isReplayMode = false;
    state.replaySessionId = null;
    document.getElementById('replay-banner').classList.remove('visible');
}

// --- Stop button ---
document.getElementById('stop-btn').addEventListener('click', () => {
    fetch('/api/stop').then(r => r.json()).then(data => {
        if (data.error) showError(data.error);
    }).catch(() => showError('Stop not available'));
});

// --- Export dropdown ---
document.getElementById('export-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    document.getElementById('export-menu').classList.toggle('visible');
});

document.addEventListener('click', () => {
    document.getElementById('export-menu').classList.remove('visible');
});

document.getElementById('export-menu').addEventListener('click', (e) => {
    e.stopPropagation();
    const item = e.target.closest('.export-item');
    if (!item) return;
    document.getElementById('export-menu').classList.remove('visible');

    const action = item.dataset.action;
    const event = state.selectedEvent;
    const sessionId = state.replaySessionId || 'live';

    if (action === 'svg') {
        window.open('/api/export/flamegraph?event=' + encodeURIComponent(event) +
            '&session=' + encodeURIComponent(sessionId), '_blank');
    } else if (action === 'collapsed') {
        window.open('/api/export/session/' + encodeURIComponent(sessionId) + '?format=collapsed', '_blank');
    } else if (action === 'json') {
        window.open('/api/export/session/' + encodeURIComponent(sessionId) + '?format=json', '_blank');
    }
});

// --- Tab switching ---
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    });
});

function switchToTab(tabName) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
    const tabBtn = document.querySelector('.tab[data-tab="' + tabName + '"]');
    const tabContent = document.getElementById('tab-' + tabName);
    if (tabBtn) tabBtn.classList.add('active');
    if (tabContent) tabContent.classList.add('active');
}

// --- Event selector ---
document.getElementById('event-select').addEventListener('change', (e) => {
    state.selectedEvent = e.target.value;
    state.flamegraphZoom = null;
    renderCurrentEvent();
});

// --- SSE Connection ---
function connectSSE() {
    if (evtSource) {
        evtSource.close();
        evtSource = null;
    }
    evtSource = new EventSource('/api/stream');

    evtSource.addEventListener('status', (e) => {
        updateStatus(JSON.parse(e.data));
    });

    evtSource.addEventListener('event_types', (e) => {
        state.eventTypes = JSON.parse(e.data);
        updateEventSelector();
    });

    evtSource.addEventListener('per_event', (e) => {
        state.perEvent = JSON.parse(e.data);
        state.lastUpdateTime = Date.now();
        const evtData = state.perEvent[state.selectedEvent];
        if (evtData) {
            state.totalSamples = evtData.function_summary.total_samples;
        }
        updateStatBar();
        renderCurrentEvent();
    });

    evtSource.addEventListener('perf_stat', (e) => {
        state.perfStat = JSON.parse(e.data);
        updateStatBar();
    });

    evtSource.onerror = () => {
        evtSource.close();
        evtSource = null;
        updateStatus({ connected: false });
        setTimeout(connectSSE, 3000);
    };

    evtSource.onopen = () => {
        document.getElementById('status-dot').className = 'dot waiting';
        document.getElementById('status-text').textContent = 'Connected to server, waiting for agent...';
    };
}

// --- Status ---
function updateStatus(data) {
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    const agentEl = document.getElementById('stat-agent');
    const stopBtn = document.getElementById('stop-btn');

    if (data.connected) {
        dot.className = 'dot connected';
        text.textContent = 'Agent: ' + data.agent;
        agentEl.textContent = data.agent;
        stopBtn.classList.remove('hidden');
        hideReplayBanner();
        // Auto-switch to profiling view when legacy agent connects
        if (!data.managed && document.getElementById('view-landing') &&
            document.getElementById('view-landing').classList.contains('active')) {
            showProfilingView();
        }
    } else {
        dot.className = 'dot disconnected';
        text.textContent = 'Agent disconnected';
        agentEl.textContent = '--';
        stopBtn.classList.add('hidden');
    }
}

// --- Event selector ---
function updateEventSelector() {
    const select = document.getElementById('event-select');
    const current = select.value;
    select.innerHTML = state.eventTypes.map(evt =>
        '<option value="' + escapeAttr(evt) + '"' + (evt === current ? ' selected' : '') + '>' + escapeHtml(evt) + '</option>'
    ).join('');
    if (!state.eventTypes.includes(state.selectedEvent) && state.eventTypes.length > 0) {
        state.selectedEvent = state.eventTypes[0];
        select.value = state.selectedEvent;
    }
}

// --- Stat bar (dynamic cards) ---
function updateStatBar() {
    document.getElementById('stat-samples').textContent = formatNumber(state.totalSamples);

    const bar = document.getElementById('perf-stat-bar');
    const agentCard = document.getElementById('stat-card-agent');

    // Remove old dynamic cards
    bar.querySelectorAll('.stat-card-dynamic').forEach(el => el.remove());

    const s = state.perfStat;
    if (!s || Object.keys(s).length === 0) return;

    const entries = Object.entries(s)
        .filter(([k]) => k !== 'time_elapsed')
        .sort(([a], [b]) => {
            const ia = STAT_ORDER.indexOf(a);
            const ib = STAT_ORDER.indexOf(b);
            return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib);
        });

    entries.forEach(([key, data]) => {
        const div = document.createElement('div');
        div.className = 'stat-card stat-card-dynamic';
        div.innerHTML =
            '<div class="stat-value" title="' + escapeAttr(data.comment || '') + '">' +
            formatStatValue(key, data.value) + '</div>' +
            '<div class="stat-label">' + escapeHtml(STAT_LABELS[key] || key) + '</div>';
        bar.insertBefore(div, agentCard);
    });
}

// --- Source capability banner ---
function updateSourceBanner() {
    const banner = document.getElementById('source-banner');
    const evtData = state.perEvent[state.selectedEvent];
    if (!evtData || !evtData.source_files || evtData.source_files.length === 0) {
        banner.classList.add('visible');
        return;
    }
    const hasSource = evtData.source_files.some(f => f.found);
    banner.classList.toggle('visible', !hasSource);
}

// --- Last updated timer ---
setInterval(() => {
    const el = document.getElementById('last-update');
    if (!state.lastUpdateTime) { el.textContent = ''; return; }
    const ago = Math.round((Date.now() - state.lastUpdateTime) / 1000);
    el.textContent = ago < 2 ? 'Updated just now' : 'Updated ' + ago + 's ago';
}, 1000);

// --- Render current event ---
function renderCurrentEvent() {
    const evtData = state.perEvent[state.selectedEvent];
    if (!evtData) return;

    renderFunctionTable(evtData.function_summary);
    updateSourceBanner();

    // Flamegraph: try to preserve zoom
    if (state.flamegraphZoom) {
        const node = findNodeByName(evtData.flamegraph, state.flamegraphZoom.name);
        if (node) {
            state.flamegraphZoom.node = node;
            renderFlamegraph(node, node.value);
        } else {
            state.flamegraphZoom = null;
            renderFlamegraph(evtData.flamegraph, evtData.function_summary.total_samples);
        }
    } else {
        renderFlamegraph(evtData.flamegraph, evtData.function_summary.total_samples);
    }

    if (state.currentSourceFile) {
        fetchAndRenderSource(state.currentSourceFile);
    }
}

function findNodeByName(tree, name) {
    if (!tree) return null;
    if (tree.name === name) return tree;
    if (tree.children) {
        for (const child of tree.children) {
            const found = findNodeByName(child, name);
            if (found) return found;
        }
    }
    return null;
}

// --- Function Table ---
let functionSortKey = 'self';  // 'self' or 'total'

function renderFunctionTable(data) {
    const tbody = document.getElementById('function-tbody');
    const scrollY = window.scrollY;

    if (!data.functions || data.functions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">No data yet</td></tr>';
        return;
    }

    // Sort by selected column
    const sorted = data.functions.slice().sort((a, b) => {
        if (functionSortKey === 'total') {
            return (b.total_samples || 0) - (a.total_samples || 0);
        }
        return (b.self_samples || b.samples) - (a.self_samples || a.samples);
    });

    const maxSelfPct = sorted.reduce((m, f) => Math.max(m, f.self_percent || f.percent || 0), 0);
    const maxTotalPct = sorted.reduce((m, f) => Math.max(m, f.total_percent || 0), 0);

    tbody.innerHTML = sorted.map((f, i) => {
        const selfPct = f.self_percent !== undefined ? f.self_percent : (f.percent || 0);
        const totalPct = f.total_percent || 0;
        const selfSamples = f.self_samples !== undefined ? f.self_samples : (f.samples || 0);
        const totalSamples = f.total_samples || 0;

        // Self bar
        const selfBarW = Math.max(2, (selfPct / Math.max(maxSelfPct, 1)) * 100);
        const selfHue = Math.max(0, 120 - (selfPct / Math.max(maxSelfPct, 1)) * 120);
        const selfColor = 'hsl(' + selfHue + ', 70%, 45%)';

        // Total bar
        const totalBarW = Math.max(2, (totalPct / Math.max(maxTotalPct, 1)) * 100);
        const totalColor = 'hsl(210, 50%, 40%)';

        const moduleName = f.module ? f.module.split('/').pop() : '';
        return '<tr data-func="' + escapeAttr(f.name) + '">' +
            '<td>' + (i + 1) + '</td>' +
            '<td><strong>' + escapeHtml(f.name) + '</strong></td>' +
            '<td title="' + escapeAttr(f.module) + '">' + escapeHtml(moduleName) + '</td>' +
            '<td><div class="cpu-bar">' +
                '<div class="cpu-bar-fill" style="width:' + selfBarW + '%;background:' + selfColor + '"></div>' +
                '<span class="cpu-bar-text">' + selfPct.toFixed(1) + '%</span>' +
            '</div></td>' +
            '<td><div class="cpu-bar total-bar">' +
                '<div class="cpu-bar-fill" style="width:' + totalBarW + '%;background:' + totalColor + '"></div>' +
                '<span class="cpu-bar-text">' + totalPct.toFixed(1) + '%</span>' +
            '</div></td>' +
            '<td title="self: ' + selfSamples + ' / total: ' + totalSamples + '">' + selfSamples + '</td></tr>';
    }).join('');

    // Restore scroll position
    window.scrollTo(0, scrollY);

    // Click handler for source view
    tbody.querySelectorAll('tr').forEach(row => {
        row.addEventListener('click', () => {
            showSourceForFunction(row.dataset.func);
        });
    });

    // Sort header highlighting
    document.querySelectorAll('#function-table th.sortable').forEach(th => {
        th.classList.toggle('active', th.dataset.sort === functionSortKey);
    });
}

// Column header sort click handler
document.querySelectorAll('#function-table th.sortable').forEach(th => {
    th.addEventListener('click', () => {
        functionSortKey = th.dataset.sort;
        const evtData = state.perEvent[state.selectedEvent];
        if (evtData) renderFunctionTable(evtData.function_summary);
    });
});

// --- Source: show for function ---
function showSourceForFunction(funcName) {
    const evtData = state.perEvent[state.selectedEvent];
    if (!evtData) return;

    // If embedded source is available (session replay), search for the right file
    if (evtData.source) {
        let bestFile = null;
        let bestSamples = -1;
        for (const [filePath, lines] of Object.entries(evtData.source)) {
            // Check if this file has samples — prefer files with more data
            const total = lines.reduce((s, l) => s + l.samples, 0);
            if (total > bestSamples) {
                bestSamples = total;
                bestFile = filePath;
            }
        }
        if (bestFile) {
            state.currentSourceFile = bestFile;
            renderSourceFilePicker(evtData.source_files || [], bestFile);
            renderSourceView(bestFile, evtData.source[bestFile]);
            switchToTab('source');
            return;
        }
    }

    // source_files: [{path, found, total_samples, functions}]
    const sourceFiles = evtData.source_files || [];
    state.sourceFiles = sourceFiles;

    if (sourceFiles.length === 0) {
        state.currentSourceFile = null;
        document.getElementById('source-file-picker').innerHTML = '';
        document.getElementById('source-view').innerHTML =
            '<p class="source-unavailable">Source not available for this function.</p>';
        switchToTab('source');
        return;
    }

    // Find the file containing the clicked function
    const targetFile = sourceFiles.find(f => f.found && f.functions && f.functions.includes(funcName))
        || sourceFiles.find(f => f.found)
        || sourceFiles[0];

    if (targetFile && targetFile.found) {
        state.currentSourceFile = targetFile.path;
        renderSourceFilePicker(sourceFiles, targetFile.path);
        fetchAndRenderSource(targetFile.path);
        switchToTab('source');
    } else {
        state.currentSourceFile = null;
        document.getElementById('source-file-picker').innerHTML = '';
        document.getElementById('source-view').innerHTML =
            '<p class="source-unavailable">Source not available for ' + escapeHtml(funcName) + '</p>';
        switchToTab('source');
    }
}

// --- Source: file picker ---
function renderSourceFilePicker(sourceFiles, selectedFile) {
    const picker = document.getElementById('source-file-picker');
    if (!sourceFiles || sourceFiles.length <= 1) {
        picker.innerHTML = '';
        return;
    }

    picker.innerHTML = sourceFiles.map(f => {
        const active = f.path === selectedFile ? ' active' : '';
        const basename = f.path.split('/').pop();
        const status = f.found ? '' : ' (not found)';
        return '<button class="file-picker-btn' + active + '" data-path="' + escapeAttr(f.path) + '">' +
            escapeHtml(basename) + ' (' + f.total_samples + ')' + status + '</button>';
    }).join('');

    picker.querySelectorAll('.file-picker-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            state.currentSourceFile = btn.dataset.path;
            picker.querySelectorAll('.file-picker-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            fetchAndRenderSource(btn.dataset.path);
        });
    });
}

// --- Source: fetch and render ---
function fetchAndRenderSource(filePath) {
    const evtData = state.perEvent[state.selectedEvent];
    if (evtData && evtData.source && evtData.source[filePath]) {
        renderSourceView(filePath, evtData.source[filePath]);
        return;
    }

    const container = document.getElementById('source-view');
    container.innerHTML = '<p class="empty loading">Loading source...</p>';

    const url = '/api/source?file=' + encodeURIComponent(filePath) +
                '&event=' + encodeURIComponent(state.selectedEvent);
    fetch(url)
        .then(r => r.json())
        .then(data => {
            if (data.lines && data.lines.length > 0) {
                renderSourceView(data.file, data.lines);
            } else {
                container.innerHTML = '<p class="empty">No source data for ' + escapeHtml(filePath) + '</p>';
            }
        })
        .catch(err => {
            showError('Failed to load source: ' + err);
            container.innerHTML = '<p class="empty">Error loading source.</p>';
        });
}

// --- Source View ---
function renderSourceView(filePath, lines) {
    const container = document.getElementById('source-view');
    if (!lines || lines.length === 0) {
        container.innerHTML = '<p class="empty">No source data available.</p>';
        return;
    }

    const totalSamples = lines.reduce((sum, l) => sum + l.samples, 0);
    let hottestLine = 0;
    let maxSamples = 0;
    lines.forEach(l => {
        if (l.samples > maxSamples) {
            maxSamples = l.samples;
            hottestLine = l.line;
        }
    });

    // Render at least 2000 lines, or enough to include the hottest line + 100
    const maxLine = Math.max(2000, hottestLine + 100);
    const displayLines = lines.length > maxLine ? lines.slice(0, maxLine) : lines;
    const truncated = lines.length > displayLines.length;

    let html = '<div class="source-header">' + escapeHtml(filePath) +
               ' (' + totalSamples + ' samples, ' + escapeHtml(state.selectedEvent) + ')</div>';
    html += '<div class="source-scroll">';

    for (let i = 0; i < displayLines.length; i++) {
        const l = displayLines[i];
        let heat = 0;
        if (l.percent > 0) heat = 1;
        if (l.percent > 2) heat = 2;
        if (l.percent > 5) heat = 3;
        if (l.percent > 15) heat = 4;
        if (l.percent > 30) heat = 5;

        const samplesText = l.samples > 0 ? l.samples + ' (' + l.percent.toFixed(1) + '%)' : '';
        html += '<div class="source-line heat-' + heat + '" id="source-line-' + l.line + '">' +
            '<span class="line-no">' + l.line + '</span>' +
            '<span class="line-samples">' + samplesText + '</span>' +
            '<span class="line-code">' + escapeHtml(l.source) + '</span>' +
            '</div>';
    }
    html += '</div>';

    if (truncated) {
        html += '<div class="source-truncated">Showing first ' + displayLines.length +
                ' of ' + lines.length + ' lines. Hottest line: ' + hottestLine + '</div>';
    }

    container.innerHTML = html;

    if (hottestLine > 0 && hottestLine <= maxLine) {
        requestAnimationFrame(() => {
            const el = document.getElementById('source-line-' + hottestLine);
            if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        });
    }

    // Highlight function name if navigating from flamegraph
    if (state.pendingHighlight) {
        const funcName = state.pendingHighlight;
        state.pendingHighlight = null;
        requestAnimationFrame(() => {
            const sourceLines = container.querySelectorAll('.source-line');
            for (const sl of sourceLines) {
                const code = sl.querySelector('.line-code');
                if (code && code.textContent.includes(funcName)) {
                    sl.classList.add('source-flash');
                    sl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    sl.addEventListener('animationend', () => sl.classList.remove('source-flash'), { once: true });
                    break;
                }
            }
        });
    }
}

// --- Flame Graph ---
function renderFlamegraph(data, totalSamples) {
    const container = document.getElementById('flamegraph-container');
    if (!data || !data.children || data.children.length === 0) {
        container.innerHTML = '<p class="empty">No flame graph data yet.</p>';
        return;
    }

    const width = container.clientWidth - 32;
    if (width < 10) return;
    const rowHeight = 18;
    const fontSize = 11;
    totalSamples = totalSamples || data.value;

    flamegraphRects = [];
    const maxDepth = flattenTree(data, 0, 0, width, flamegraphRects, totalSamples);
    const height = (maxDepth + 1) * rowHeight + 4;

    let html = '';

    if (state.flamegraphZoom) {
        html += '<div class="flamegraph-controls">' +
            '<button id="flamegraph-reset" class="fg-reset-btn">Reset Zoom</button>' +
            '<span class="fg-zoom-label">Zoomed: ' + escapeHtml(state.flamegraphZoom.name) + '</span>' +
            '</div>';
    }

    let svg = '<svg width="' + width + '" height="' + height + '" xmlns="http://www.w3.org/2000/svg">';
    for (let idx = 0; idx < flamegraphRects.length; idx++) {
        const r = flamegraphRects[idx];
        const inlined = r.inlined;
        const hue = 30 + (hashCode(r.name) % 30);
        const sat = inlined ? 50 + (hashCode(r.name + 'x') % 15) : 80 + (hashCode(r.name + 'x') % 20);
        const light = 45 + (hashCode(r.name + 'y') % 15);
        const color = 'hsl(' + hue + ', ' + sat + '%, ' + light + '%)';
        const y = height - (r.depth + 1) * rowHeight;
        const inlinedAttr = inlined ? ' data-inlined="1"' : '';
        const inlinedTag = inlined ? ' (inlined)' : '';

        svg += '<g data-idx="' + idx + '"' + inlinedAttr + ' style="cursor:pointer">';
        svg += '<rect x="' + r.x + '" y="' + y + '" width="' + Math.max(r.w - 1, 1) +
               '" height="' + (rowHeight - 1) + '" fill="' + color + '" rx="1">' +
               '<title>' + escapeHtml(r.name) + inlinedTag + ' (' + r.value + ' samples, ' +
               r.percent.toFixed(1) + '%)</title></rect>';
        if (r.w > 40) {
            const maxChars = Math.floor(r.w / 7);
            const label = r.name.length > maxChars ? r.name.substring(0, maxChars) + '..' : r.name;
            svg += '<text x="' + (r.x + 3) + '" y="' + (y + 13) + '" font-size="' + fontSize +
                   '" fill="#fff" pointer-events="none">' + escapeHtml(label) + '</text>';
        }
        svg += '</g>';
    }
    svg += '</svg>';

    html += svg;
    container.innerHTML = html;

    // Single click → source, double click → zoom, right click → context menu
    container.querySelectorAll('g[data-idx]').forEach(g => {
        g.addEventListener('click', () => {
            clearTimeout(fgClickTimer);
            fgClickTimer = setTimeout(() => {
                const rect = flamegraphRects[parseInt(g.dataset.idx)];
                if (rect && rect.name !== 'root') {
                    state.pendingHighlight = rect.name;
                    showSourceForFunction(rect.name);
                }
            }, 250);
        });

        g.addEventListener('dblclick', () => {
            clearTimeout(fgClickTimer);
            const rect = flamegraphRects[parseInt(g.dataset.idx)];
            if (rect && rect.node && rect.node.children && rect.node.children.length > 0) {
                state.flamegraphZoom = { name: rect.name, node: rect.node };
                renderFlamegraph(rect.node, rect.node.value);
            }
        });

        g.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            const rect = flamegraphRects[parseInt(g.dataset.idx)];
            if (rect) showFgContextMenu(e.clientX, e.clientY, rect.name, parseInt(g.dataset.idx));
        });
    });

    // Reset zoom button
    const resetBtn = document.getElementById('flamegraph-reset');
    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            state.flamegraphZoom = null;
            const evtData = state.perEvent[state.selectedEvent];
            if (evtData) renderFlamegraph(evtData.flamegraph, evtData.function_summary.total_samples);
        });
    }

    // Re-apply active search after re-render
    const searchInput = document.getElementById('fg-search');
    if (searchInput && searchInput.value.trim()) {
        applyFlamegraphSearch(searchInput.value.trim());
    }
}

function flattenTree(node, depth, x, width, rects, totalSamples) {
    const percent = totalSamples > 0 ? (node.value / totalSamples * 100) : 0;
    rects.push({ name: node.name, value: node.value, percent, depth, x, w: width,
                 node, inlined: !!node.inlined });

    let maxDepth = depth;
    let childX = x;
    if (node.children) {
        node.children.forEach(child => {
            const childWidth = (child.value / node.value) * width;
            if (childWidth >= 1) {
                const d = flattenTree(child, depth + 1, childX, childWidth, rects, totalSamples);
                maxDepth = Math.max(maxDepth, d);
            }
            childX += childWidth;
        });
    }
    return maxDepth;
}

// --- Flamegraph resize handler ---
window.addEventListener('resize', debounce(() => {
    const evtData = state.perEvent[state.selectedEvent];
    if (!evtData) return;
    if (state.flamegraphZoom && state.flamegraphZoom.node) {
        renderFlamegraph(state.flamegraphZoom.node, state.flamegraphZoom.node.value);
    } else {
        renderFlamegraph(evtData.flamegraph, evtData.function_summary.total_samples);
    }
}, 200));

// --- Flamegraph search ---
function applyFlamegraphSearch(query) {
    const container = document.getElementById('flamegraph-container');
    const matchesEl = document.getElementById('fg-search-matches');
    const clearBtn = document.getElementById('fg-search-clear');
    const groups = container.querySelectorAll('g[data-idx]');

    if (!query) {
        groups.forEach(g => { g.classList.remove('fg-dim', 'fg-match'); });
        matchesEl.textContent = '';
        clearBtn.classList.add('hidden');
        return;
    }

    clearBtn.classList.remove('hidden');

    let re;
    try { re = new RegExp(query, 'i'); }
    catch (e) { matchesEl.textContent = 'invalid regex'; return; }

    let matchCount = 0;
    let matchSamples = 0;
    let totalFrames = flamegraphRects.length;

    groups.forEach(g => {
        const idx = parseInt(g.dataset.idx);
        const rect = flamegraphRects[idx];
        if (!rect) return;
        if (re.test(rect.name)) {
            g.classList.remove('fg-dim');
            g.classList.add('fg-match');
            matchCount++;
            matchSamples += rect.value;
        } else {
            g.classList.add('fg-dim');
            g.classList.remove('fg-match');
        }
    });

    // Sample % relative to root
    const rootValue = flamegraphRects.length > 0 ? flamegraphRects[0].value : 0;
    const pct = rootValue > 0 ? (matchSamples / rootValue * 100).toFixed(1) : '0.0';
    matchesEl.textContent = matchCount + ' / ' + totalFrames + ' frames (' + pct + '%)';
}

document.getElementById('fg-search').addEventListener('input', debounce((e) => {
    applyFlamegraphSearch(e.target.value.trim());
}, 200));

document.getElementById('fg-search-clear').addEventListener('click', () => {
    const input = document.getElementById('fg-search');
    input.value = '';
    applyFlamegraphSearch('');
    input.focus();
});

// Ctrl+F focuses search when flamegraph tab active
document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        const fgTab = document.getElementById('tab-flamegraph');
        if (fgTab && fgTab.classList.contains('active')) {
            e.preventDefault();
            document.getElementById('fg-search').focus();
        }
    }
});

// --- Sessions ---
function loadSessions() {
    fetch('/api/sessions')
        .then(r => r.json())
        .then(sessions => renderSessionsList(sessions))
        .catch(() => {});
}

function renderSessionsList(sessions) {
    const container = document.getElementById('sessions-list');
    if (!sessions || sessions.length === 0) {
        container.innerHTML = '<p class="empty">No saved sessions.</p>';
        return;
    }

    let html = '<table id="sessions-table"><thead><tr>';
    html += '<th>Session</th><th>Agent</th><th>Samples</th><th>Events</th><th>Time</th><th></th>';
    html += '</tr></thead><tbody>';
    sessions.forEach(s => {
        html += '<tr>' +
            '<td>' + escapeHtml(s.session_id) + '</td>' +
            '<td>' + escapeHtml(s.agent || '--') + '</td>' +
            '<td>' + s.total_samples + '</td>' +
            '<td>' + escapeHtml((s.event_types || []).join(', ')) + '</td>' +
            '<td>' + escapeHtml(s.timestamp || '') + '</td>' +
            '<td><button class="replay-btn" data-session="' + escapeAttr(s.session_id) + '">Replay</button></td>' +
            '</tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;

    container.querySelectorAll('.replay-btn').forEach(btn => {
        btn.addEventListener('click', () => replaySession(btn.dataset.session));
    });
}

function replaySession(sessionId) {
    fetch('/api/sessions/' + encodeURIComponent(sessionId))
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                showError('Replay error: ' + data.error);
                return;
            }
            state.perEvent = data.per_event;
            state.eventTypes = data.metadata.event_types || [];
            state.perfStat = data.metadata.perf_stat || {};
            state.selectedEvent = state.eventTypes[0] || 'cycles';
            state.totalSamples = data.metadata.total_samples;
            state.flamegraphZoom = null;
            state.lastUpdateTime = Date.now();

            showReplayBanner(sessionId, data.metadata.timestamp);
            updateEventSelector();
            updateStatBar();
            renderCurrentEvent();
            switchToTab('functions');
        })
        .catch(err => showError('Failed to load session: ' + err));
}

// --- Flamegraph context menu (created once, reused) ---
(function initFgContextMenu() {
    fgContextMenu = document.createElement('div');
    fgContextMenu.id = 'fg-context-menu';
    fgContextMenu.className = 'fg-context-menu';
    fgContextMenu.innerHTML =
        '<div class="fg-ctx-item" data-action="source">View source</div>' +
        '<div class="fg-ctx-item" data-action="zoom">Zoom in</div>' +
        '<div class="fg-ctx-item" data-action="copy">Copy function name</div>';
    document.body.appendChild(fgContextMenu);

    fgContextMenu.addEventListener('click', (e) => {
        e.stopPropagation();
        const item = e.target.closest('.fg-ctx-item');
        if (!item) return;
        const action = item.dataset.action;
        const funcName = fgContextMenu.dataset.func;
        const rectIdx = parseInt(fgContextMenu.dataset.idx);
        hideFgContextMenu();

        if (action === 'source' && funcName && funcName !== 'root') {
            state.pendingHighlight = funcName;
            showSourceForFunction(funcName);
        } else if (action === 'zoom') {
            const rect = flamegraphRects[rectIdx];
            if (rect && rect.node && rect.node.children && rect.node.children.length > 0) {
                state.flamegraphZoom = { name: rect.name, node: rect.node };
                renderFlamegraph(rect.node, rect.node.value);
            }
        } else if (action === 'copy' && funcName) {
            navigator.clipboard.writeText(funcName).catch(() => {});
        }
    });

    document.addEventListener('click', hideFgContextMenu);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideFgContextMenu(); });
    window.addEventListener('scroll', hideFgContextMenu, true);
})();

function showFgContextMenu(x, y, funcName, rectIdx) {
    fgContextMenu.dataset.func = funcName;
    fgContextMenu.dataset.idx = rectIdx;
    fgContextMenu.style.left = x + 'px';
    fgContextMenu.style.top = y + 'px';
    fgContextMenu.classList.add('visible');
}

function hideFgContextMenu() {
    if (fgContextMenu) fgContextMenu.classList.remove('visible');
}

// --- Import perf.data ---
(function initImport() {
    var importBtn = document.getElementById('import-btn');
    var importFile = document.getElementById('import-file');
    var importStatus = document.getElementById('import-status');
    if (!importBtn || !importFile) return;

    importBtn.addEventListener('click', function () { importFile.click(); });

    importFile.addEventListener('change', function () {
        var file = importFile.files[0];
        if (!file) return;
        importFile.value = '';

        importBtn.disabled = true;
        importStatus.textContent = 'Importing ' + file.name + '...';

        fetch('/api/import', { method: 'POST', body: file })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                importBtn.disabled = false;
                if (data.error) {
                    importStatus.textContent = '';
                    showError('Import failed: ' + data.error);
                    return;
                }
                importStatus.textContent = 'Imported ' + data.total_samples + ' samples';
                loadSessions();
                replaySession(data.session_id);
            })
            .catch(function (err) {
                importBtn.disabled = false;
                importStatus.textContent = '';
                showError('Import failed: ' + err);
            });
    });
})();

// =====================================================================
// View management
// =====================================================================

function showView(viewId) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const el = document.getElementById(viewId);
    if (el) el.classList.add('active');
}

function showProfilingView() {
    showView('view-profiling');
}

// Auto-switch to profiling when legacy agent connects via SSE
let _autoSwitched = false;
function checkAutoSwitch() {
    if (!_autoSwitched && state.totalSamples > 0) {
        _autoSwitched = true;
        showProfilingView();
    }
}

// Patch renderCurrentEvent to also check auto-switch
const _origRenderCurrentEvent = renderCurrentEvent;
renderCurrentEvent = function() {
    _origRenderCurrentEvent();
    checkAutoSwitch();
};

// =====================================================================
// Wizard
// =====================================================================

let wizardStep = 1;
let wizardData = {
    host: '',
    port: 9999,
    connected: false,
    perfVerified: false,
    perfVersion: '',
    binary: '',
    sourceDir: '',
    pid: null,
    processName: '',
    frequency: 99,
    duration: 8,
    agentHello: null,
    capabilities: null,
};

// --- Landing page ---
document.getElementById('card-live').addEventListener('click', function() {
    showView('view-wizard');
    wizardGoToStep(1);
    // Try to load saved wizard state
    fetch('/api/wizard/state')
        .then(function(r) { return r.json(); })
        .then(function(ws) {
            if (ws.agent_host) document.getElementById('wiz-host').value = ws.agent_host;
            if (ws.agent_port) document.getElementById('wiz-port').value = ws.agent_port;
            if (ws.binary_path) document.getElementById('wiz-binary').value = ws.binary_path;
            if (ws.source_dir) document.getElementById('wiz-source-dir').value = ws.source_dir;
            if (ws.pid) document.getElementById('wiz-pid').value = ws.pid;
            if (ws.frequency) document.getElementById('wiz-frequency').value = ws.frequency;
            if (ws.duration) document.getElementById('wiz-duration').value = ws.duration;
        })
        .catch(function() {});
});

document.getElementById('card-sessions').addEventListener('click', function() {
    showProfilingView();
    switchToTab('sessions');
    loadSessions();
});

// --- Wizard navigation ---
function wizardGoToStep(step) {
    wizardStep = step;
    document.querySelectorAll('.wiz-step').forEach(function(el) { el.classList.remove('active'); });
    var stepEl = document.getElementById('wiz-step-' + step);
    if (stepEl) stepEl.classList.add('active');

    // Update progress bar
    document.querySelectorAll('.wiz-prog-step').forEach(function(el) {
        var s = parseInt(el.dataset.step);
        el.classList.remove('active', 'done');
        if (s === step) el.classList.add('active');
        else if (s < step) el.classList.add('done');
    });

    // Footer buttons
    document.getElementById('wiz-back').style.display = step > 1 ? '' : 'none';
    document.getElementById('wiz-next').style.display = step < 6 ? '' : 'none';
    document.getElementById('wiz-skip').style.display = step === 3 ? '' : 'none';

    // Step-specific actions
    if (step === 2 && wizardData.connected) wizardVerifyPerf();
    if (step === 6) wizardBuildReview();
}

document.getElementById('wiz-back').addEventListener('click', function() {
    if (wizardStep > 1) wizardGoToStep(wizardStep - 1);
});

document.getElementById('wiz-next').addEventListener('click', function() {
    if (wizardStep < 6) {
        if (!wizardValidateStep(wizardStep)) return;
        // Apply binary/source config when leaving step 3
        if (wizardStep === 3) {
            wizardApplyStep3(function() { wizardGoToStep(wizardStep + 1); });
            return;
        }
        wizardGoToStep(wizardStep + 1);
    }
});

document.getElementById('wiz-skip').addEventListener('click', function() {
    // Skip is only visible on step 3 (binary/source)
    wizardGoToStep(wizardStep + 1);
});

function wizardValidateStep(step) {
    if (step === 1 && !wizardData.connected) {
        wizardSetStatus('wiz-connect-status', 'Connect to agent first', 'error');
        return false;
    }
    if (step === 4) {
        var pid = document.getElementById('wiz-pid').value;
        if (!pid) {
            wizardSetStatus('wiz-pid-status', 'Select or enter a PID', 'error');
            return false;
        }
        wizardData.pid = parseInt(pid);
    }
    if (step === 5) {
        wizardData.frequency = parseInt(document.getElementById('wiz-frequency').value) || 99;
        wizardData.duration = parseInt(document.getElementById('wiz-duration').value) || 8;
    }
    return true;
}

function wizardSetStatus(id, msg, cls) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg;
    el.className = 'wiz-status' + (cls ? ' ' + cls : '');
}

// --- Step 1: Connect ---
document.getElementById('wiz-connect-btn').addEventListener('click', function() {
    var host = document.getElementById('wiz-host').value.trim();
    var port = parseInt(document.getElementById('wiz-port').value) || 9999;
    if (!host) {
        wizardSetStatus('wiz-connect-status', 'Enter host address', 'error');
        return;
    }

    var btn = document.getElementById('wiz-connect-btn');
    btn.disabled = true;
    wizardSetStatus('wiz-connect-status', 'Connecting to ' + host + ':' + port + '...', 'info');

    fetch('/api/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ host: host, port: port }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        btn.disabled = false;
        if (data.ok) {
            wizardData.connected = true;
            wizardData.host = host;
            wizardData.port = port;
            wizardData.agentHello = data.hello;
            var platform = data.hello && data.hello.platform;
            var info = platform ? platform.arch + ' / ' + platform.kernel : 'connected';
            wizardSetStatus('wiz-connect-status', 'Connected: ' + info, 'ok');
        } else {
            wizardSetStatus('wiz-connect-status', data.error || 'Connection failed', 'error');
        }
    })
    .catch(function(err) {
        btn.disabled = false;
        wizardSetStatus('wiz-connect-status', 'Error: ' + err, 'error');
    });
});

// --- Step 2: Verify Perf ---
function wizardVerifyPerf() {
    var result = document.getElementById('wiz-perf-result');
    result.innerHTML = '<div class="wiz-spinner">Verifying perf tool...</div>';

    fetch('/api/agent/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd: 'verify_perf' }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (!data.ok && data.error) {
            result.innerHTML = '<span class="wiz-err">Error: ' + escapeHtml(data.error) + '</span>';
            return;
        }
        var html = '';
        if (data.available) {
            html += '<div class="wiz-ok">&#10003; perf found: ' + escapeHtml(data.version || '?') + '</div>';
            if (data.functional) {
                html += '<div class="wiz-ok">&#10003; perf is functional</div>';
                wizardData.perfVerified = true;
                wizardData.perfVersion = data.version;
            } else {
                html += '<div class="wiz-err">&#10007; perf stat check failed: ' +
                    escapeHtml(data.error || 'unknown error') + '</div>';
            }
            if (data.perf_event_paranoid > 1) {
                html += '<div class="wiz-warn">&#9888; perf_event_paranoid=' + data.perf_event_paranoid +
                    ' &mdash; some events may be unavailable</div>';
            }
        } else {
            html += '<div class="wiz-err">&#10007; perf not found: ' +
                escapeHtml(data.error || 'not available') + '</div>';
        }
        result.innerHTML = html;
    })
    .catch(function(err) {
        result.innerHTML = '<span class="wiz-err">Command failed: ' + escapeHtml(String(err)) + '</span>';
    });
}

// --- Step 3: Binary & Source ---
document.querySelectorAll('.wiz-browse-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
        var target = btn.dataset.target;
        var input = document.getElementById(target);
        var startPath = input.value.trim() || '/';
        openBrowseModal(startPath, target === 'wiz-source-dir' ? 'dir' : 'file', function(selected) {
            input.value = selected;
        });
    });
});

// Apply binary/source config when leaving step 3
function wizardApplyStep3(callback) {
    var binary = document.getElementById('wiz-binary').value.trim();
    var sourceDir = document.getElementById('wiz-source-dir').value.trim();
    var status = document.getElementById('wiz-binary-status');

    var promises = [];
    if (binary) {
        promises.push(
            fetch('/api/config/binary', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: binary }),
            }).then(function(r) { return r.json(); })
        );
        wizardData.binary = binary;
    }
    if (sourceDir) {
        promises.push(
            fetch('/api/config/source', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: sourceDir }),
            }).then(function(r) { return r.json(); })
        );
        wizardData.sourceDir = sourceDir;
    }

    if (promises.length > 0) {
        status.textContent = 'Applying...';
        status.className = 'wiz-status info';
        Promise.all(promises).then(function(results) {
            var errors = results.filter(function(r) { return !r.ok; });
            if (errors.length > 0) {
                status.textContent = errors.map(function(e) { return e.error; }).join('; ');
                status.className = 'wiz-status error';
            } else {
                status.textContent = 'Applied';
                status.className = 'wiz-status ok';
            }
            if (callback) callback();
        }).catch(function(err) {
            status.textContent = 'Error: ' + err;
            status.className = 'wiz-status error';
            if (callback) callback();
        });
    } else {
        if (callback) callback();
    }
}

// --- Step 4: Process selection ---
document.getElementById('wiz-refresh-procs').addEventListener('click', function() {
    var list = document.getElementById('wiz-proc-list');
    var status = document.getElementById('wiz-pid-status');
    list.innerHTML = '<div class="wiz-spinner">Loading process list...</div>';
    status.textContent = '';

    fetch('/api/agent/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd: 'list_processes', timeout: 30 }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (!data.ok) {
            list.innerHTML = '<p class="empty">' + escapeHtml(data.error || 'Failed') + '</p>';
            return;
        }
        var procs = data.processes || [];
        if (procs.length === 0) {
            list.innerHTML = '<p class="empty">No processes found</p>';
            return;
        }
        var html = '<table class="wiz-proc-table"><thead><tr>' +
            '<th>PID</th><th>Name</th><th>CPU%</th><th>Command</th></tr></thead><tbody>';
        procs.forEach(function(p) {
            html += '<tr data-pid="' + p.pid + '">' +
                '<td>' + p.pid + '</td>' +
                '<td>' + escapeHtml(p.comm) + '</td>' +
                '<td>' + p.cpu + '</td>' +
                '<td title="' + escapeAttr(p.cmdline) + '">' +
                    escapeHtml(p.cmdline.substring(0, 80)) + '</td></tr>';
        });
        html += '</tbody></table>';
        list.innerHTML = html;

        // Click to select process
        list.querySelectorAll('tr[data-pid]').forEach(function(row) {
            row.addEventListener('click', function() {
                list.querySelectorAll('tr').forEach(function(r) { r.classList.remove('selected'); });
                row.classList.add('selected');
                var pid = parseInt(row.dataset.pid);
                document.getElementById('wiz-pid').value = pid;
                wizardData.pid = pid;
                wizardData.processName = row.querySelector('td:nth-child(2)').textContent;
                status.textContent = 'Selected PID ' + pid + ' (' + wizardData.processName + ')';
                status.className = 'wiz-status ok';
            });
        });
    })
    .catch(function(err) {
        list.innerHTML = '<p class="empty">Error: ' + escapeHtml(String(err)) + '</p>';
    });
});

// --- Step 6: Review & Start ---
function wizardBuildReview() {
    wizardData.frequency = parseInt(document.getElementById('wiz-frequency').value) || 99;
    wizardData.duration = parseInt(document.getElementById('wiz-duration').value) || 8;

    var summary = document.getElementById('wiz-review-summary');
    var platform = wizardData.agentHello && wizardData.agentHello.platform;
    var html = '' +
        '<div class="wiz-review-row"><span class="wiz-review-label">Agent</span>' +
            '<span class="wiz-review-value">' + escapeHtml(wizardData.host + ':' + wizardData.port) + '</span></div>' +
        '<div class="wiz-review-row"><span class="wiz-review-label">Platform</span>' +
            '<span class="wiz-review-value">' + escapeHtml(platform ? platform.arch + ' / ' + platform.kernel : '?') + '</span></div>' +
        '<div class="wiz-review-row"><span class="wiz-review-label">Process</span>' +
            '<span class="wiz-review-value">PID ' + (wizardData.pid || '?') +
            (wizardData.processName ? ' (' + escapeHtml(wizardData.processName) + ')' : '') + '</span></div>' +
        '<div class="wiz-review-row"><span class="wiz-review-label">Frequency</span>' +
            '<span class="wiz-review-value">' + wizardData.frequency + ' Hz</span></div>' +
        '<div class="wiz-review-row"><span class="wiz-review-label">Duration</span>' +
            '<span class="wiz-review-value">' + wizardData.duration + 's per round</span></div>';
    if (wizardData.binary) {
        html += '<div class="wiz-review-row"><span class="wiz-review-label">Binary</span>' +
            '<span class="wiz-review-value">' + escapeHtml(wizardData.binary) + '</span></div>';
    }
    summary.innerHTML = html;
}

document.getElementById('wiz-start-btn').addEventListener('click', function() {
    var btn = document.getElementById('wiz-start-btn');
    btn.disabled = true;
    wizardSetStatus('wiz-start-status', 'Starting profiling...', 'info');

    fetch('/api/agent/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            cmd: 'start',
            args: {
                pid: wizardData.pid,
                frequency: wizardData.frequency,
                duration: wizardData.duration,
            },
            timeout: 120,
        }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        btn.disabled = false;
        if (data.ok) {
            wizardSetStatus('wiz-start-status', 'Profiling started', 'ok');
            // Save wizard state
            fetch('/api/wizard/state', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    step: 6, pid: wizardData.pid, frequency: wizardData.frequency,
                    duration: wizardData.duration,
                }),
            }).catch(function() {});

            // Show profiling view with control bar
            showProfilingView();
            showControlBar(wizardData.pid, wizardData.processName);
            state.managedAgent = true;
        } else {
            wizardSetStatus('wiz-start-status', data.error || 'Start failed', 'error');
        }
    })
    .catch(function(err) {
        btn.disabled = false;
        wizardSetStatus('wiz-start-status', 'Error: ' + err, 'error');
    });
});

// =====================================================================
// Control Bar
// =====================================================================

function showControlBar(pid, name) {
    var bar = document.getElementById('control-bar');
    bar.classList.remove('hidden');
    document.getElementById('ctrl-state').textContent = 'Profiling';
    document.getElementById('ctrl-state').className = '';
    document.getElementById('ctrl-pid').textContent = 'PID ' + pid + (name ? ' (' + name + ')' : '');
    document.getElementById('ctrl-pause').classList.remove('hidden');
    document.getElementById('ctrl-resume').classList.add('hidden');
}

function hideControlBar() {
    document.getElementById('control-bar').classList.add('hidden');
}

document.getElementById('ctrl-pause').addEventListener('click', function() {
    fetch('/api/agent/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd: 'pause' }),
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) {
            document.getElementById('ctrl-state').textContent = 'Paused';
            document.getElementById('ctrl-state').className = 'paused';
            document.getElementById('ctrl-pause').classList.add('hidden');
            document.getElementById('ctrl-resume').classList.remove('hidden');
        }
    }).catch(function() {});
});

document.getElementById('ctrl-resume').addEventListener('click', function() {
    fetch('/api/agent/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd: 'resume' }),
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) {
            document.getElementById('ctrl-state').textContent = 'Profiling';
            document.getElementById('ctrl-state').className = '';
            document.getElementById('ctrl-pause').classList.remove('hidden');
            document.getElementById('ctrl-resume').classList.add('hidden');
        }
    }).catch(function() {});
});

document.getElementById('ctrl-stop').addEventListener('click', function() {
    fetch('/api/stop').then(function(r) { return r.json(); }).then(function(data) {
        if (data.stopped) {
            document.getElementById('ctrl-state').textContent = 'Stopped';
            document.getElementById('ctrl-state').className = 'paused';
            state.managedAgent = false;
        }
    }).catch(function() {});
});

document.getElementById('ctrl-settings').addEventListener('click', function() {
    // Quick settings: change frequency/duration via configure command
    var freq = prompt('Sampling frequency (Hz):', wizardData.frequency);
    if (freq === null) return;
    var dur = prompt('Collection duration (seconds):', wizardData.duration);
    if (dur === null) return;

    fetch('/api/agent/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            cmd: 'configure',
            args: { frequency: parseInt(freq), duration: parseInt(dur) },
        }),
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) {
            wizardData.frequency = data.frequency;
            wizardData.duration = data.duration;
        }
    }).catch(function() {});
});

// =====================================================================
// File Browser Modal
// =====================================================================

var browseCallback = null;
var browseMode = 'file';  // 'file' or 'dir'
var browseSelected = null;

function openBrowseModal(startPath, mode, callback) {
    browseCallback = callback;
    browseMode = mode;
    browseSelected = null;
    document.getElementById('browse-modal').classList.remove('hidden');
    browseTo(startPath);
}

function closeBrowseModal() {
    document.getElementById('browse-modal').classList.add('hidden');
    browseCallback = null;
}

function browseTo(path) {
    var pathEl = document.getElementById('browse-path');
    var entriesEl = document.getElementById('browse-entries');
    pathEl.textContent = path;
    entriesEl.innerHTML = '<div class="wiz-spinner">Loading...</div>';
    browseSelected = (browseMode === 'dir') ? path : null;

    fetch('/api/browse?path=' + encodeURIComponent(path))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                entriesEl.innerHTML = '<p class="empty">' + escapeHtml(data.error) + '</p>';
                return;
            }
            var html = '';
            if (data.parent && data.parent !== data.path) {
                html += '<div class="browse-entry" data-path="' + escapeAttr(data.parent) + '" data-dir="1">' +
                    '<span class="be-icon">..</span><span class="be-name">..</span></div>';
            }
            (data.entries || []).forEach(function(e) {
                html += '<div class="browse-entry" data-path="' + escapeAttr(e.path) + '"' +
                    (e.is_dir ? ' data-dir="1"' : '') + '>' +
                    '<span class="be-icon">' + (e.is_dir ? '&#128193;' : '&#128196;') + '</span>' +
                    '<span class="be-name">' + escapeHtml(e.name) + '</span>' +
                    (e.size !== undefined ? '<span class="be-size">' + formatNumber(e.size) + '</span>' : '') +
                    '</div>';
            });
            entriesEl.innerHTML = html;

            entriesEl.querySelectorAll('.browse-entry').forEach(function(entry) {
                entry.addEventListener('click', function() {
                    if (entry.dataset.dir === '1') {
                        browseTo(entry.dataset.path);
                    } else {
                        entriesEl.querySelectorAll('.browse-entry').forEach(function(e) { e.classList.remove('selected'); });
                        entry.classList.add('selected');
                        browseSelected = entry.dataset.path;
                    }
                });
                entry.addEventListener('dblclick', function() {
                    if (entry.dataset.dir === '1') {
                        browseTo(entry.dataset.path);
                    } else {
                        browseSelected = entry.dataset.path;
                        confirmBrowse();
                    }
                });
            });
        })
        .catch(function(err) {
            entriesEl.innerHTML = '<p class="empty">Error: ' + escapeHtml(String(err)) + '</p>';
        });
}

function confirmBrowse() {
    if (browseSelected && browseCallback) {
        browseCallback(browseSelected);
    }
    closeBrowseModal();
}

document.getElementById('browse-select').addEventListener('click', confirmBrowse);
document.getElementById('browse-cancel').addEventListener('click', closeBrowseModal);
document.querySelector('.modal-close').addEventListener('click', closeBrowseModal);

// Close modal on Escape
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        var modal = document.getElementById('browse-modal');
        if (!modal.classList.contains('hidden')) closeBrowseModal();
    }
});

// =====================================================================
// Init
// =====================================================================

fetch('/api/status')
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.agent_connected) {
            // Agent already connected — skip to profiling view
            updateStatus({ connected: true, agent: data.agent_addr });
            showProfilingView();
            _autoSwitched = true;
            if (data.managed) {
                state.managedAgent = true;
                showControlBar(wizardData.pid || '?', '');
            }
        }
        // Else: stay on landing page
    })
    .catch(function() {});

connectSSE();
loadSessions();
