// PerfLens Web UI

let state = {
    totalSamples: 0,
    chunkCount: 0,
    functionSummary: null,
    flamegraphData: null,
    sourceData: {},
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

// --- SSE Connection ---
function connectSSE() {
    const evtSource = new EventSource('/api/stream');

    evtSource.addEventListener('status', (e) => {
        const data = JSON.parse(e.data);
        updateStatus(data);
    });

    evtSource.addEventListener('function_summary', (e) => {
        const data = JSON.parse(e.data);
        state.functionSummary = data;
        state.totalSamples = data.total_samples;
        state.chunkCount++;
        updateSummaryBar();
        renderFunctionTable(data);
    });

    evtSource.addEventListener('flamegraph', (e) => {
        state.flamegraphData = JSON.parse(e.data);
        renderFlamegraph(state.flamegraphData);
    });

    evtSource.addEventListener('source', (e) => {
        state.sourceData = JSON.parse(e.data);
        if (state.currentSourceFile && state.sourceData[state.currentSourceFile]) {
            renderSourceView(state.currentSourceFile, state.sourceData[state.currentSourceFile]);
        }
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
    const agentEl = document.getElementById('agent-status');

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

function updateSummaryBar() {
    document.getElementById('total-samples').textContent = state.totalSamples.toLocaleString();
    document.getElementById('chunk-count').textContent = state.chunkCount;
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
        return `<tr data-file="${escapeAttr(f.file || '')}" data-func="${escapeAttr(f.name)}">
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
            const funcName = row.dataset.func;
            showSourceForFunction(funcName);
        });
    });
}

function showSourceForFunction(funcName) {
    // Find which source file contains this function
    for (const [filePath, lines] of Object.entries(state.sourceData)) {
        // Check if any line has samples (the file is relevant)
        const hasData = lines.some(l => l.samples > 0);
        if (hasData) {
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

    let html = `<div class="source-header">${escapeHtml(filePath)} (${totalSamples} samples)</div>`;
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

    // Auto-scroll to hottest line
    if (hottestLine > 0) {
        const el = document.getElementById('source-line-' + hottestLine);
        if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }
}

// --- Flame Graph (simple SVG) ---
function renderFlamegraph(data) {
    const container = document.getElementById('flamegraph-container');
    if (!data || !data.children || data.children.length === 0) {
        container.innerHTML = '<p class="empty">No flame graph data yet.</p>';
        return;
    }

    const width = container.clientWidth - 32;
    const rowHeight = 18;
    const fontSize = 11;

    // Flatten the tree into rectangles
    const rects = [];
    const maxDepth = flattenTree(data, 0, 0, width, rects);
    const height = (maxDepth + 1) * rowHeight + 4;

    let svg = `<svg width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">`;
    rects.forEach(r => {
        const hue = 30 + Math.random() * 30;  // warm colors
        const sat = 80 + Math.random() * 20;
        const light = 45 + Math.random() * 15;
        const color = `hsl(${hue}, ${sat}%, ${light}%)`;
        const y = height - (r.depth + 1) * rowHeight;

        svg += `<g>`;
        svg += `<rect x="${r.x}" y="${y}" width="${Math.max(r.w - 1, 1)}" height="${rowHeight - 1}" `;
        svg += `fill="${color}" rx="1" `;
        svg += `><title>${escapeHtml(r.name)} (${r.value} samples, ${r.percent.toFixed(1)}%)</title></rect>`;
        if (r.w > 40) {
            const label = r.name.length > r.w / 7 ? r.name.substring(0, Math.floor(r.w / 7)) + '...' : r.name;
            svg += `<text x="${r.x + 3}" y="${y + 13}" font-size="${fontSize}" fill="#fff" `;
            svg += `pointer-events="none">${escapeHtml(label)}</text>`;
        }
        svg += `</g>`;
    });
    svg += `</svg>`;

    container.innerHTML = svg;
}

function flattenTree(node, depth, x, width, rects) {
    const percent = state.totalSamples > 0 ? (node.value / state.totalSamples * 100) : 0;
    rects.push({ name: node.name, value: node.value, percent, depth, x, w: width });

    let maxDepth = depth;
    let childX = x;
    if (node.children) {
        node.children.forEach(child => {
            const childWidth = (child.value / node.value) * width;
            if (childWidth >= 1) {
                const d = flattenTree(child, depth + 1, childX, childWidth, rects);
                maxDepth = Math.max(maxDepth, d);
            }
            childX += childWidth;
        });
    }
    return maxDepth;
}

// --- Utilities ---
function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// --- Init ---
fetch('/api/status')
    .then(r => r.json())
    .then(data => {
        if (data.agent_connected) {
            updateStatus({ connected: true, agent: data.agent_addr });
        }
        if (data.total_samples > 0) {
            state.totalSamples = data.total_samples;
            state.chunkCount = data.chunk_count;
            updateSummaryBar();
        }
    })
    .catch(() => {});

connectSSE();
