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
    document.querySelector('.tab[data-tab="' + tabName + '"]').classList.add('active');
    document.getElementById('tab-' + tabName).classList.add('active');
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
        `<option value="${evt}" ${evt === current ? 'selected' : ''}>${evt}</option>`
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
function renderFunctionTable(data) {
    const tbody = document.getElementById('function-tbody');
    const scrollY = window.scrollY;

    if (!data.functions || data.functions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty">No data yet</td></tr>';
        return;
    }

    const maxPercent = data.functions[0].percent;
    tbody.innerHTML = data.functions.map((f, i) => {
        const barWidth = Math.max(2, (f.percent / Math.max(maxPercent, 1)) * 100);
        const hue = Math.max(0, 120 - (f.percent / Math.max(maxPercent, 1)) * 120);
        const barColor = 'hsl(' + hue + ', 70%, 45%)';
        const moduleName = f.module ? f.module.split('/').pop() : '';
        return '<tr data-func="' + escapeAttr(f.name) + '">' +
            '<td>' + (i + 1) + '</td>' +
            '<td><strong>' + escapeHtml(f.name) + '</strong></td>' +
            '<td title="' + escapeAttr(f.module) + '">' + escapeHtml(moduleName) + '</td>' +
            '<td><div class="cpu-bar">' +
                '<div class="cpu-bar-fill" style="width:' + barWidth + '%;background:' + barColor + '"></div>' +
                '<span class="cpu-bar-text">' + f.percent.toFixed(1) + '%</span>' +
            '</div></td>' +
            '<td>' + f.samples + '</td></tr>';
    }).join('');

    // Restore scroll position
    window.scrollTo(0, scrollY);

    // Click handler for source view
    tbody.querySelectorAll('tr').forEach(row => {
        row.addEventListener('click', () => {
            showSourceForFunction(row.dataset.func);
        });
    });
}

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

    // Limit render to 2000 lines or hottest line + 100
    const maxLine = Math.max(2000, hottestLine + 100);
    const displayLines = lines.length > maxLine ? lines.slice(0, maxLine) : lines;
    const truncated = lines.length > displayLines.length;

    let html = '<div class="source-header">' + escapeHtml(filePath) +
               ' (' + totalSamples + ' samples, ' + state.selectedEvent + ')</div>';
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
        const hue = 30 + (hashCode(r.name) % 30);
        const sat = 80 + (hashCode(r.name + 'x') % 20);
        const light = 45 + (hashCode(r.name + 'y') % 15);
        const color = 'hsl(' + hue + ', ' + sat + '%, ' + light + '%)';
        const y = height - (r.depth + 1) * rowHeight;

        svg += '<g data-idx="' + idx + '" style="cursor:pointer">';
        svg += '<rect x="' + r.x + '" y="' + y + '" width="' + Math.max(r.w - 1, 1) +
               '" height="' + (rowHeight - 1) + '" fill="' + color + '" rx="1">' +
               '<title>' + escapeHtml(r.name) + ' (' + r.value + ' samples, ' +
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

    // Click-to-zoom
    container.querySelectorAll('g[data-idx]').forEach(g => {
        g.addEventListener('click', () => {
            const rect = flamegraphRects[parseInt(g.dataset.idx)];
            if (rect && rect.node && rect.node.children && rect.node.children.length > 0) {
                state.flamegraphZoom = { name: rect.name, node: rect.node };
                renderFlamegraph(rect.node, rect.node.value);
            }
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
}

function flattenTree(node, depth, x, width, rects, totalSamples) {
    const percent = totalSamples > 0 ? (node.value / totalSamples * 100) : 0;
    rects.push({ name: node.name, value: node.value, percent, depth, x, w: width, node });

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
            '<td>' + (s.event_types || []).join(', ') + '</td>' +
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

// --- Init ---
fetch('/api/status')
    .then(r => r.json())
    .then(data => {
        if (data.agent_connected) {
            updateStatus({ connected: true, agent: data.agent_addr });
        }
    })
    .catch(() => {});

connectSSE();
loadSessions();
