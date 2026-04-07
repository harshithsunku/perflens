// PerfLens Web UI

let state = {
    totalSamples: 0,
    chunkCount: 0,
    eventTypes: [],
    selectedEvent: 'cycles',
    perEvent: {},       // {event_type: {function_summary, flamegraph, source}}
    perfStat: {},
    currentSourceFile: null,
};

// --- Tab switching ---
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    });
});

// --- Event selector ---
document.getElementById('event-select').addEventListener('change', (e) => {
    state.selectedEvent = e.target.value;
    renderCurrentEvent();
});

// --- SSE Connection ---
function connectSSE() {
    const evtSource = new EventSource('/api/stream');

    evtSource.addEventListener('status', (e) => {
        updateStatus(JSON.parse(e.data));
    });

    evtSource.addEventListener('event_types', (e) => {
        state.eventTypes = JSON.parse(e.data);
        updateEventSelector();
    });

    evtSource.addEventListener('per_event', (e) => {
        state.perEvent = JSON.parse(e.data);
        // Update total samples from selected event
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
        updateStatus({ connected: false });
        setTimeout(connectSSE, 3000);
    };

    evtSource.onopen = () => {
        const dot = document.getElementById('status-dot');
        const text = document.getElementById('status-text');
        dot.className = 'dot waiting';
        text.textContent = 'Connected to server, waiting for agent...';
    };
}

function updateStatus(data) {
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    const agentEl = document.getElementById('stat-agent');

    if (data.connected) {
        dot.className = 'dot connected';
        text.textContent = 'Agent connected: ' + data.agent;
        agentEl.textContent = data.agent;
    } else {
        dot.className = 'dot disconnected';
        text.textContent = 'Agent disconnected';
        agentEl.textContent = '--';
    }
}

function updateEventSelector() {
    const select = document.getElementById('event-select');
    const current = select.value;
    select.innerHTML = state.eventTypes.map(evt =>
        `<option value="${evt}" ${evt === current ? 'selected' : ''}>${evt}</option>`
    ).join('');
    // If selected event doesn't exist anymore, pick first
    if (!state.eventTypes.includes(state.selectedEvent) && state.eventTypes.length > 0) {
        state.selectedEvent = state.eventTypes[0];
        select.value = state.selectedEvent;
    }
}

function formatNumber(n) {
    if (n === undefined || n === null) return '--';
    if (typeof n === 'number' && !Number.isInteger(n)) return n.toFixed(2);
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
}

function updateStatBar() {
    const s = state.perfStat;
    document.getElementById('stat-samples').textContent = formatNumber(state.totalSamples);
    document.getElementById('stat-ipc').textContent =
        s.ipc ? s.ipc.value.toFixed(2) : '--';
    document.getElementById('stat-cycles').textContent =
        s.cycles ? formatNumber(s.cycles.value) : '--';
    document.getElementById('stat-instructions').textContent =
        s.instructions ? formatNumber(s.instructions.value) : '--';
    document.getElementById('stat-cache-misses').textContent =
        s['cache-misses'] ? formatNumber(s['cache-misses'].value) : '--';
    document.getElementById('stat-branch-misses').textContent =
        s['branch-misses'] ? formatNumber(s['branch-misses'].value) : '--';
}

// --- Render current event ---
function renderCurrentEvent() {
    const evtData = state.perEvent[state.selectedEvent];
    if (!evtData) return;

    renderFunctionTable(evtData.function_summary);
    renderFlamegraph(evtData.flamegraph, evtData.function_summary.total_samples);

    // Update source view if we have a file selected
    if (state.currentSourceFile && evtData.source[state.currentSourceFile]) {
        renderSourceView(state.currentSourceFile, evtData.source[state.currentSourceFile]);
    }
}

// --- Function Table ---
function renderFunctionTable(data) {
    const tbody = document.getElementById('function-tbody');
    if (!data.functions || data.functions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty">No data yet</td></tr>';
        return;
    }

    const maxPercent = data.functions[0].percent;
    tbody.innerHTML = data.functions.map((f, i) => {
        const barWidth = Math.max(2, (f.percent / Math.max(maxPercent, 1)) * 100);
        const hue = Math.max(0, 120 - (f.percent / Math.max(maxPercent, 1)) * 120);
        const barColor = `hsl(${hue}, 70%, 45%)`;
        const moduleName = f.module.split('/').pop();
        return `<tr data-func="${escapeAttr(f.name)}">
            <td>${i + 1}</td>
            <td><strong>${escapeHtml(f.name)}</strong></td>
            <td title="${escapeAttr(f.module)}">${escapeHtml(moduleName)}</td>
            <td>
                <div class="cpu-bar">
                    <div class="cpu-bar-fill" style="width:${barWidth}%;background:${barColor}"></div>
                    <span class="cpu-bar-text">${f.percent.toFixed(1)}%</span>
                </div>
            </td>
            <td>${f.samples}</td>
        </tr>`;
    }).join('');

    // Click handler for source view
    tbody.querySelectorAll('tr').forEach(row => {
        row.addEventListener('click', () => {
            showSourceForFunction(row.dataset.func);
        });
    });
}

function showSourceForFunction(funcName) {
    const evtData = state.perEvent[state.selectedEvent];
    if (!evtData || !evtData.source) return;

    for (const [filePath, lines] of Object.entries(evtData.source)) {
        if (lines.some(l => l.samples > 0)) {
            state.currentSourceFile = filePath;
            renderSourceView(filePath, lines);
            // Switch to source tab
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
            document.querySelector('.tab[data-tab="source"]').classList.add('active');
            document.getElementById('tab-source').classList.add('active');
            return;
        }
    }
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

    let html = `<div class="source-header">${escapeHtml(filePath)} (${totalSamples} samples, ${state.selectedEvent})</div>`;
    lines.forEach(l => {
        let heat = 0;
        if (l.percent > 0) heat = 1;
        if (l.percent > 2) heat = 2;
        if (l.percent > 5) heat = 3;
        if (l.percent > 15) heat = 4;
        if (l.percent > 30) heat = 5;

        if (l.samples > maxSamples) {
            maxSamples = l.samples;
            hottestLine = l.line;
        }

        const samplesText = l.samples > 0 ? `${l.samples} (${l.percent.toFixed(1)}%)` : '';
        html += `<div class="source-line heat-${heat}" id="source-line-${l.line}">`;
        html += `<span class="line-no">${l.line}</span>`;
        html += `<span class="line-samples">${samplesText}</span>`;
        html += `<span class="line-code">${escapeHtml(l.source)}</span>`;
        html += `</div>`;
    });

    container.innerHTML = html;

    if (hottestLine > 0) {
        const el = document.getElementById('source-line-' + hottestLine);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
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
    const rowHeight = 18;
    const fontSize = 11;
    totalSamples = totalSamples || data.value;

    const rects = [];
    const maxDepth = flattenTree(data, 0, 0, width, rects, totalSamples);
    const height = (maxDepth + 1) * rowHeight + 4;

    let svg = `<svg width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">`;
    rects.forEach(r => {
        const hue = 30 + (hashCode(r.name) % 30);
        const sat = 80 + (hashCode(r.name + 'x') % 20);
        const light = 45 + (hashCode(r.name + 'y') % 15);
        const color = `hsl(${hue}, ${sat}%, ${light}%)`;
        const y = height - (r.depth + 1) * rowHeight;

        svg += `<g>`;
        svg += `<rect x="${r.x}" y="${y}" width="${Math.max(r.w - 1, 1)}" height="${rowHeight - 1}" `;
        svg += `fill="${color}" rx="1" `;
        svg += `><title>${escapeHtml(r.name)} (${r.value} samples, ${r.percent.toFixed(1)}%)</title></rect>`;
        if (r.w > 40) {
            const maxChars = Math.floor(r.w / 7);
            const label = r.name.length > maxChars ? r.name.substring(0, maxChars) + '..' : r.name;
            svg += `<text x="${r.x + 3}" y="${y + 13}" font-size="${fontSize}" fill="#fff" `;
            svg += `pointer-events="none">${escapeHtml(label)}</text>`;
        }
        svg += `</g>`;
    });
    svg += `</svg>`;

    container.innerHTML = svg;
}

function flattenTree(node, depth, x, width, rects, totalSamples) {
    const percent = totalSamples > 0 ? (node.value / totalSamples * 100) : 0;
    rects.push({ name: node.name, value: node.value, percent, depth, x, w: width });

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

function hashCode(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        hash = ((hash << 5) - hash) + str.charCodeAt(i);
        hash |= 0;
    }
    return Math.abs(hash);
}

// --- Utilities ---
function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

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
        html += `<tr>`;
        html += `<td>${escapeHtml(s.session_id)}</td>`;
        html += `<td>${escapeHtml(s.agent || '--')}</td>`;
        html += `<td>${s.total_samples}</td>`;
        html += `<td>${(s.event_types || []).join(', ')}</td>`;
        html += `<td>${escapeHtml(s.timestamp || '')}</td>`;
        html += `<td><button class="replay-btn" data-session="${escapeAttr(s.session_id)}">Replay</button></td>`;
        html += `</tr>`;
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
                alert('Error: ' + data.error);
                return;
            }
            // Load session data into state
            state.perEvent = data.per_event;
            state.eventTypes = data.metadata.event_types || [];
            state.perfStat = data.metadata.perf_stat || {};
            const firstEvt = state.eventTypes[0] || 'cycles';
            state.selectedEvent = firstEvt;
            state.totalSamples = data.metadata.total_samples;

            updateEventSelector();
            updateStatBar();
            renderCurrentEvent();

            // Switch to functions tab
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
            document.querySelector('.tab[data-tab="functions"]').classList.add('active');
            document.getElementById('tab-functions').classList.add('active');

            const dot = document.getElementById('status-dot');
            const text = document.getElementById('status-text');
            dot.className = 'dot waiting';
            text.textContent = 'Replaying: ' + sessionId;
        })
        .catch(err => alert('Failed to load session: ' + err));
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
