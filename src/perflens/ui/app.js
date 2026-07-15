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
    flamegraphZoom: null,   // null or {name, node} (derived from zoom names)
    flamegraphZoomPath: [], // ancestor chain [{name, node}] (derived)
    flamegraphZoomNames: [], // authoritative zoom: names from root — node
                             // refs go stale on every data refresh, names
                             // survive re-fetches
    selectedTid: null,      // null = all threads, or integer TID
    sourceFiles: [],        // [{path, found, total_samples, functions}] for current event
    metricsSystem: [],      // system metric snapshots (last ~150)
    metricsProcess: [],     // process metric snapshots
    metricsNetwork: null,   // latest network snapshot
    metricsPrevNetwork: null, // previous for rate calc
    metricsDisk: null,      // latest disk I/O snapshot (opt-in agent feature)
    metricsPrevDisk: null,  // previous for rate calc
    metricsCollapseLevel: 0, // 0=full, 1=compact, 2=minimal
};

let evtSource = null;
let flamegraphRects = [];   // parallel to SVG <g> elements for click handling
let errorTimer = null;
let fgClickTimer = null;    // single-click vs double-click disambiguation
let fgContextMenu = null;   // persistent context menu element

// Notify-and-fetch state: the newest chunk_count announced over SSE, the
// chunk_count of the data we actually hold, and an in-flight guard.
let dataVersion = 0;
let fetchedVersion = -1;
let perEventFetching = false;

function fetchPerEvent(evt, force) {
    if (state.isReplayMode || !evt) return;
    if (perEventFetching) return;
    if (!force && fetchedVersion >= dataVersion) return;
    perEventFetching = true;
    fetch('/api/per-event?event=' + encodeURIComponent(evt))
        .then(function (r) {
            if (!r.ok) throw new Error('no data for ' + evt);
            return r.json();
        })
        .then(function (resp) {
            perEventFetching = false;
            state.perEvent[resp.event] = resp.data;
            fetchedVersion = resp.version.chunk_count || 0;
            state.lastUpdateTime = Date.now();
            if (resp.event === state.selectedEvent) {
                state.totalSamples = resp.data.function_summary.total_samples;
                updateStatBar();
                renderCurrentEvent();
            }
            // Catch up if more chunks landed while we were fetching
            if (fetchedVersion < dataVersion) fetchPerEvent(state.selectedEvent);
        })
        .catch(function () { perEventFetching = false; });
}

// --- Theme ---
function getTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
}

function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('perflens-theme', theme); } catch (e) { /* ignore */ }
    var label = document.getElementById('theme-label');
    if (label) label.textContent = theme === 'dark' ? 'Dark' : 'Light';
}

function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem('perflens-theme'); } catch (e) { /* ignore */ }
    if (saved === 'light' || saved === 'dark') {
        setTheme(saved);
    }
    var btn = document.getElementById('theme-toggle');
    if (btn) {
        btn.addEventListener('click', function () {
            setTheme(getTheme() === 'dark' ? 'light' : 'dark');
            // Re-render flame graph to pick up new colors
            var evtData = state.perEvent[state.selectedEvent];
            if (evtData && evtData.flamegraph) {
                if (state.flamegraphZoom && state.flamegraphZoom.node) {
                    renderFlamegraph(state.flamegraphZoom.node, state.flamegraphZoom.node.value);
                } else {
                    renderFlamegraph(evtData.flamegraph, evtData.function_summary.total_samples);
                }
            }
            // Re-render sparklines
            updateMetricsSparklines();
            if (state.metricsSystem.length > 0) updateMetricsCards(state.metricsSystem[state.metricsSystem.length - 1]);
            if (state.metricsProcess.length > 0) updateProcessCard(state.metricsProcess[state.metricsProcess.length - 1]);
            // Re-render function table for bar colors
            renderCurrentEvent();
        });
    }
}

function themeColor(varName) {
    return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}

function isDark() { return getTheme() === 'dark'; }

// Init theme immediately
initTheme();

// --- Docs drawer ---
function initDocsDrawer() {
    var btn = document.getElementById('docs-btn');
    var drawer = document.getElementById('docs-drawer');
    var overlay = document.getElementById('docs-overlay');
    var closeBtn = document.getElementById('docs-close');
    if (!btn || !drawer || !overlay) return;

    function openDocs() {
        drawer.classList.remove('docs-closed');
        overlay.classList.remove('docs-closed');
        // Force reflow before adding visible class for transition
        void drawer.offsetHeight;
        drawer.classList.add('visible');
        overlay.classList.add('visible');
    }

    function closeDocs() {
        drawer.classList.remove('visible');
        overlay.classList.remove('visible');
        setTimeout(function () {
            drawer.classList.add('docs-closed');
            overlay.classList.add('docs-closed');
        }, 300);
    }

    btn.addEventListener('click', openDocs);
    overlay.addEventListener('click', closeDocs);
    if (closeBtn) closeBtn.addEventListener('click', closeDocs);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && drawer.classList.contains('visible')) closeDocs();
    });

    // Docs tab switching
    var docsTabs = drawer.querySelectorAll('.docs-tab[data-docs-tab]');
    var docsPanels = drawer.querySelectorAll('.docs-panel[data-docs-panel]');
    docsTabs.forEach(function(tab) {
        tab.addEventListener('click', function() {
            var target = tab.getAttribute('data-docs-tab');
            docsTabs.forEach(function(t) { t.classList.remove('active'); });
            docsPanels.forEach(function(p) { p.classList.remove('active'); });
            tab.classList.add('active');
            var panel = drawer.querySelector('.docs-panel[data-docs-panel="' + target + '"]');
            if (panel) panel.classList.add('active');
        });
    });
}
initDocsDrawer();

// --- Stat card config ---
const STAT_ORDER = [
    'ipc', 'cycles', 'instructions',
    'cache-misses', 'cache-references', 'cache_miss_rate',
    'branch-misses', 'branch-instructions', 'branch_miss_rate',
    'page-faults', 'context-switches', 'cpu-migrations', 'task-clock'
];
const STAT_LABELS = {
    'ipc': 'IPC', 'cycles': 'Cycles', 'instructions': 'Instructions',
    'cache-misses': 'Cache Miss', 'cache-references': 'Cache Refs',
    'cache_miss_rate': 'Cache Miss %',
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
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    if (typeof n === 'number' && !Number.isInteger(n)) return n.toFixed(2);
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
    // Force a live refetch — replay clobbered state.perEvent
    fetchedVersion = -1;
    fetchPerEvent(state.selectedEvent, true);
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
        if (tab.dataset.tab === 'threads') renderThreadsTab();
        // A flamegraph rendered while its tab was hidden is empty
        // (clientWidth 0 aborts the layout) — e.g. right after a session
        // replay. Re-render now that the container is visible.
        if (tab.dataset.tab === 'flamegraph' &&
            !document.querySelector('#flamegraph-container svg')) {
            renderCurrentEvent();
        }
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
    state.flamegraphZoomPath = [];
    state.flamegraphZoomNames = [];
    state.selectedTid = null;
    functionShowCount = 200;
    renderCurrentEvent();
    // Newly selected event may be missing or stale — pull it
    if (!state.isReplayMode) fetchPerEvent(state.selectedEvent, true);
});

document.getElementById('thread-filter').addEventListener('change', function(e) {
    var val = e.target.value;
    state.selectedTid = val ? parseInt(val) : null;
    state.flamegraphZoom = null;
    state.flamegraphZoomPath = [];
    state.flamegraphZoomNames = [];
    functionShowCount = 200;
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

    // Notify-and-fetch: the server broadcasts a tiny version stamp per
    // chunk; we pull only the event currently being viewed. The full
    // per-event blob (multi-MB on big profiles) is never pushed over SSE.
    evtSource.addEventListener('data_version', (e) => {
        if (state.isReplayMode) return;
        const v = JSON.parse(e.data);
        dataVersion = v.chunk_count || 0;
        state.chunkCount = v.chunk_count || 0;
        if (v.event_types && v.event_types.length) {
            state.eventTypes = v.event_types;
            updateEventSelector();
        }
        fetchPerEvent(state.selectedEvent);
    });

    evtSource.addEventListener('perf_stat', (e) => {
        state.perfStat = JSON.parse(e.data);
        updateStatBar();
    });

    evtSource.addEventListener('metrics_system', function(e) {
        var m = JSON.parse(e.data);
        state.metricsSystem.push(m);
        if (state.metricsSystem.length > 150) state.metricsSystem = state.metricsSystem.slice(-150);
        updateMetricsCards(m);
        updateSystemDetails(m);
        updateMetricsSparklines();
        showMetricsStrip();
    });

    evtSource.addEventListener('metrics_process', function(e) {
        var m = JSON.parse(e.data);
        state.metricsProcess.push(m);
        if (state.metricsProcess.length > 150) state.metricsProcess = state.metricsProcess.slice(-150);
        updateProcessCard(m);
        updateProcessDetails(m);
        updateMetricsSparklines();
    });

    evtSource.addEventListener('metrics_network', function(e) {
        var m = JSON.parse(e.data);
        state.metricsPrevNetwork = state.metricsNetwork;
        state.metricsNetwork = m;
        updateNetworkPanel(m, state.metricsPrevNetwork);
    });

    evtSource.addEventListener('metrics_disk', function(e) {
        var m = JSON.parse(e.data);
        state.metricsPrevDisk = state.metricsDisk;
        state.metricsDisk = m;
        updateDiskPanel(m, state.metricsPrevDisk);
    });

    evtSource.addEventListener('agent_connected', (e) => {
        const data = JSON.parse(e.data);
        state.platform = data.platform || {};
        updatePlatformInfo(state.platform);
        if (document.getElementById('view-landing') &&
            document.getElementById('view-landing').classList.contains('active')) {
            showProfilingView();
        }
        refreshControlBar();
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
        // Fetch existing metrics history on reconnect
        fetch('/api/metrics/history?type=system').then(function(r) { return r.json(); })
            .then(function(h) {
                if (h && h.length > 0) {
                    state.metricsSystem = h.slice(-METRICS_MAX);
                    showMetricsStrip();
                    updateMetricsCards(state.metricsSystem[state.metricsSystem.length - 1]);
                    updateMetricsSparklines();
                }
            }).catch(function() {});
        fetch('/api/metrics/history?type=process').then(function(r) { return r.json(); })
            .then(function(h) {
                if (h && h.length > 0) {
                    state.metricsProcess = h.slice(-METRICS_MAX);
                    updateProcessCard(state.metricsProcess[state.metricsProcess.length - 1]);
                }
            }).catch(function() {});
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

// --- Thread filter ---
function updateThreadFilter() {
    var evtData = state.perEvent[state.selectedEvent];
    var select = document.getElementById('thread-filter');
    var label = document.getElementById('thread-filter-label');
    if (!evtData || !evtData.threads || evtData.threads.length <= 1) {
        select.classList.add('hidden');
        label.classList.add('hidden');
        state.selectedTid = null;
        return;
    }
    select.classList.remove('hidden');
    label.classList.remove('hidden');
    var current = select.value;
    select.innerHTML = '<option value="">All threads (' + evtData.threads.length + ')</option>';
    evtData.threads.forEach(function(t) {
        var opt = document.createElement('option');
        opt.value = t.tid;
        opt.textContent = t.comm + ' (' + t.tid + ')';
        if (String(t.tid) === current) opt.selected = true;
        select.appendChild(opt);
    });
}

var _threadViewPending = false;
function renderCurrentEvent() {
    var evtData = state.perEvent[state.selectedEvent];
    if (!evtData) return;

    updateThreadFilter();
    updateSourceBanner();

    if (state.selectedTid !== null) {
        // Fetch per-thread view from server
        if (_threadViewPending) return;
        _threadViewPending = true;
        fetch('/api/thread-view?event=' + encodeURIComponent(state.selectedEvent) +
              '&tid=' + state.selectedTid)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                _threadViewPending = false;
                renderFunctionTable(data.function_summary);
                state.flamegraphZoom = null;
                state.flamegraphZoomPath = [];
                state.flamegraphZoomNames = [];
                renderFlamegraph(data.flamegraph, data.function_summary.total_samples);
            })
            .catch(function() { _threadViewPending = false; });
        return;
    }

    renderFunctionTable(evtData.function_summary);

    // Flamegraph: re-derive zoom from the name path (node references go
    // stale on every data refresh; walking by ancestry names survives it —
    // and unlike a global name search, cannot land on a different stack).
    if (state.flamegraphZoomNames && state.flamegraphZoomNames.length) {
        var node = applyZoomFromNames(evtData.flamegraph);
        if (node) {
            renderFlamegraph(node, node.value);
        } else {
            renderFlamegraph(evtData.flamegraph, evtData.function_summary.total_samples);
        }
    } else {
        renderFlamegraph(evtData.flamegraph, evtData.function_summary.total_samples);
    }

    if (state.currentSourceFile) {
        fetchAndRenderSource(state.currentSourceFile);
    }
}

// Walk the tree along state.flamegraphZoomNames, truncating where the path
// no longer matches (a function can disappear between rounds). Rebuilds the
// derived {flamegraphZoom, flamegraphZoomPath} node references and returns
// the zoom node, or null when nothing matches.
function applyZoomFromNames(tree) {
    var names = state.flamegraphZoomNames || [];
    var node = tree;
    var walked = [];
    for (var i = 0; i < names.length; i++) {
        var next = null;
        var kids = node.children || [];
        for (var j = 0; j < kids.length; j++) {
            if (kids[j].name === names[i]) { next = kids[j]; break; }
        }
        if (!next) break;
        node = next;
        walked.push(node);
    }
    state.flamegraphZoomNames = walked.map(function (n) { return n.name; });
    if (walked.length === 0) {
        state.flamegraphZoom = null;
        state.flamegraphZoomPath = [];
        return null;
    }
    state.flamegraphZoomPath = walked.slice(0, -1).map(function (n) {
        return { name: n.name, node: n };
    });
    state.flamegraphZoom = { name: node.name, node: node };
    return node;
}

// Name path from `root` down to `target` (identity match), or null.
function pathToNode(root, target) {
    if (root === target) return [];
    var kids = root.children || [];
    for (var i = 0; i < kids.length; i++) {
        if (kids[i] === target) return [kids[i].name];
        var sub = pathToNode(kids[i], target);
        if (sub) { sub.unshift(kids[i].name); return sub; }
    }
    return null;
}

// Zoom to a rendered rect's node: extend the authoritative name path by the
// clicked node's path relative to the current render root, then re-derive.
function zoomToRectNode(node) {
    var evtData = state.perEvent[state.selectedEvent];
    if (!evtData || !evtData.flamegraph) return;
    var renderRoot = (flamegraphRects.length > 0)
        ? flamegraphRects[0].node : evtData.flamegraph;
    var rel = pathToNode(renderRoot, node);
    if (rel === null) return;
    state.flamegraphZoomNames =
        (state.flamegraphZoomNames || []).concat(rel);
    var zoomed = applyZoomFromNames(evtData.flamegraph);
    if (zoomed) renderFlamegraph(zoomed, zoomed.value);
}

// --- Function Table ---
let functionSortKey = 'self';  // 'self' or 'total'
let functionFilter = '';       // search filter string
let functionShowCount = 200;   // progressive display count
let _lastFunctionData = null;  // cached for re-filter/re-sort

function _buildFunctionRow(f, i, maxSelfPct, maxTotalPct) {
    const selfPct = f.self_percent !== undefined ? f.self_percent : (f.percent || 0);
    const totalPct = f.total_percent || 0;
    const selfSamples = f.self_samples !== undefined ? f.self_samples : (f.samples || 0);
    const totalSamples = f.total_samples || 0;

    const selfBarW = Math.max(2, (selfPct / Math.max(maxSelfPct, 1)) * 100);
    const selfHue = Math.max(0, 120 - (selfPct / Math.max(maxSelfPct, 1)) * 120);
    const selfLight = isDark() ? 45 : 50;
    const selfColor = 'hsl(' + selfHue + ', 70%, ' + selfLight + '%)';

    const totalBarW = Math.max(2, (totalPct / Math.max(maxTotalPct, 1)) * 100);
    const totalLight = isDark() ? 40 : 50;
    const totalColor = 'hsl(210, 50%, ' + totalLight + '%)';

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
}

function renderFunctionTable(data) {
    _lastFunctionData = data;
    const tbody = document.getElementById('function-tbody');
    const scrollY = window.scrollY;

    if (!data.functions || data.functions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">No data yet</td></tr>';
        _updateFunctionStatus(0, 0);
        return;
    }

    // Sort
    var sorted = data.functions.slice().sort(function(a, b) {
        if (functionSortKey === 'total') return (b.total_samples || 0) - (a.total_samples || 0);
        return (b.self_samples || b.samples) - (a.self_samples || a.samples);
    });

    // Filter
    if (functionFilter) {
        var re;
        try { re = new RegExp(functionFilter, 'i'); } catch(e) { re = null; }
        if (re) {
            sorted = sorted.filter(function(f) {
                return re.test(f.name) || re.test(f.module || '');
            });
        }
    }

    var totalFunctions = data.functions.length;
    var filteredCount = sorted.length;
    var showCount = Math.min(functionShowCount, sorted.length);
    var visible = sorted.slice(0, showCount);

    const maxSelfPct = sorted.reduce((m, f) => Math.max(m, f.self_percent || f.percent || 0), 0);
    const maxTotalPct = sorted.reduce((m, f) => Math.max(m, f.total_percent || 0), 0);

    var html = visible.map(function(f, i) {
        return _buildFunctionRow(f, i, maxSelfPct, maxTotalPct);
    }).join('');

    // "Show more" row
    var remaining = sorted.length - showCount;
    if (remaining > 0) {
        html += '<tr class="fn-show-more-row"><td colspan="6">' +
            '<button class="fn-show-more-btn" id="fn-show-more">' +
            'Show ' + Math.min(remaining, 200) + ' more (' + remaining + ' remaining)' +
            '</button></td></tr>';
    }

    tbody.innerHTML = html;
    _updateFunctionStatus(filteredCount, totalFunctions);

    window.scrollTo(0, scrollY);

    // Click handler for source view
    tbody.querySelectorAll('tr[data-func]').forEach(row => {
        row.addEventListener('click', () => showSourceForFunction(row.dataset.func));
    });

    // Show more button
    var showMoreBtn = document.getElementById('fn-show-more');
    if (showMoreBtn) {
        showMoreBtn.addEventListener('click', function() {
            functionShowCount += 200;
            renderFunctionTable(data);
        });
    }

    // Sort header highlighting
    document.querySelectorAll('#function-table th.sortable').forEach(th => {
        th.classList.toggle('active', th.dataset.sort === functionSortKey);
    });
}

function _updateFunctionStatus(shown, total) {
    var el = document.getElementById('fn-status');
    if (!el) return;
    if (total === 0) { el.textContent = ''; return; }
    if (shown === total) {
        el.textContent = total + ' functions';
    } else {
        el.textContent = shown + ' of ' + total + ' functions';
    }
}

// Column header sort click handler
document.querySelectorAll('#function-table th.sortable').forEach(th => {
    th.addEventListener('click', () => {
        functionSortKey = th.dataset.sort;
        functionShowCount = 200;
        if (_lastFunctionData) renderFunctionTable(_lastFunctionData);
    });
});

// Function search input handler
(function() {
    var input = document.getElementById('fn-search');
    if (!input) return;
    input.addEventListener('input', debounce(function() {
        functionFilter = input.value.trim();
        functionShowCount = 200;
        if (_lastFunctionData) renderFunctionTable(_lastFunctionData);
    }, 150));
})();

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
// Module-based flamegraph color: kernel=warm, user binary=green, libraries=blue/purple
function fgModuleColor(name, module, inlined) {
    var dark = isDark();
    var lightBase = dark ? 42 : 50;
    var lightVar = hashCode(name + 'y') % 12;
    var satBase = inlined ? 40 : 70;
    var satVar = hashCode(name + 'x') % 15;
    var hue;

    if (!module || module === '[unknown]') {
        // Unknown → grey
        hue = 0;
        satBase = 0;
        lightBase = dark ? 35 : 60;
        lightVar = hashCode(name + 'y') % 8;
    } else if (module === '[kernel.kallsyms]' || module.indexOf('/vmlinux') >= 0 || module.indexOf('[kernel') >= 0) {
        // Kernel → warm orange/red
        hue = 20 + (hashCode(name) % 25);
    } else if (module.indexOf('.so') >= 0 || module.indexOf('ld-linux') >= 0 || module.indexOf('libc') >= 0 ||
               module.indexOf('libm') >= 0 || module.indexOf('libpthread') >= 0 || module.indexOf('libstdc++') >= 0) {
        // Shared libraries → blue/aqua
        hue = 190 + (hashCode(name) % 40);
    } else {
        // User binary → green
        hue = 80 + (hashCode(name) % 40);
    }

    return 'hsl(' + hue + ', ' + Math.min(satBase + satVar, 100) + '%, ' + (lightBase + lightVar) + '%)';
}

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
    const charWidth = 6.5;
    totalSamples = totalSamples || data.value;

    flamegraphRects = [];
    const maxDepth = flattenTree(data, 0, 0, width, flamegraphRects, totalSamples);
    const height = (maxDepth + 1) * rowHeight + 4;

    let html = '';

    // Breadcrumb trail for zoom
    if (state.flamegraphZoom) {
        html += '<div class="flamegraph-controls">' +
            '<button id="flamegraph-reset" class="fg-reset-btn">Reset Zoom</button>';
        // Build breadcrumb path
        var crumbs = [];
        var zPath = state.flamegraphZoomPath || [];
        for (var ci = 0; ci < zPath.length; ci++) {
            crumbs.push('<span class="fg-crumb" data-crumb-idx="' + ci + '">' + escapeHtml(zPath[ci].name) + '</span>');
        }
        crumbs.push('<span class="fg-crumb fg-crumb-current">' + escapeHtml(state.flamegraphZoom.name) + '</span>');
        html += '<div class="fg-breadcrumb">root &rsaquo; ' + crumbs.join(' &rsaquo; ') + '</div>';
        html += '</div>';
    }

    const fgTextColor = themeColor('--fg-text');

    let svg = '<svg width="' + width + '" height="' + height + '" xmlns="http://www.w3.org/2000/svg">';
    for (let idx = 0; idx < flamegraphRects.length; idx++) {
        const r = flamegraphRects[idx];
        const inlined = r.inlined;
        const color = fgModuleColor(r.name, r.module || '', inlined);
        const y = height - (r.depth + 1) * rowHeight;
        const inlinedAttr = inlined ? ' data-inlined="1"' : '';

        svg += '<g data-idx="' + idx + '"' + inlinedAttr + ' style="cursor:pointer">';
        svg += '<rect x="' + r.x + '" y="' + y + '" width="' + Math.max(r.w - 1, 1) +
               '" height="' + (rowHeight - 1) + '" fill="' + color + '" rx="2"></rect>';
        if (r.w > 36) {
            const maxChars = Math.floor((r.w - 6) / charWidth);
            if (maxChars > 1) {
                const label = r.name.length > maxChars ? r.name.substring(0, maxChars - 1) + '\u2026' : r.name;
                svg += '<text x="' + (r.x + 3) + '" y="' + (y + 13) + '" font-size="' + fontSize +
                       '" fill="' + fgTextColor + '" pointer-events="none">' + escapeHtml(label) + '</text>';
            }
        }
        svg += '</g>';
    }
    svg += '</svg>';

    html += svg;

    // Info bar (persistent div below SVG, updated on hover)
    html += '<div class="fg-info-bar" id="fg-info-bar">Hover over a frame to see details</div>';

    container.innerHTML = html;

    // Wire up hover for info bar
    const infoBar = document.getElementById('fg-info-bar');
    const svgEl = container.querySelector('svg');
    if (svgEl && infoBar) {
        svgEl.addEventListener('mousemove', function(e) {
            var g = e.target.closest('g[data-idx]');
            if (!g) return;
            var rect = flamegraphRects[parseInt(g.dataset.idx)];
            if (!rect) return;
            var mod = rect.module || '';
            var inlinedTag = rect.inlined ? ' [inlined]' : '';
            infoBar.textContent = rect.name + inlinedTag + '  (' + rect.value + ' samples, ' +
                rect.percent.toFixed(2) + '%)' + (mod ? '  \u2014 ' + mod : '');
            infoBar.classList.add('fg-info-active');
        });
        svgEl.addEventListener('mouseleave', function() {
            infoBar.textContent = 'Hover over a frame to see details';
            infoBar.classList.remove('fg-info-active');
        });
    }

    // Single click → zoom, right click → context menu
    container.querySelectorAll('g[data-idx]').forEach(g => {
        g.addEventListener('click', () => {
            clearTimeout(fgClickTimer);
            fgClickTimer = setTimeout(() => {
                const rect = flamegraphRects[parseInt(g.dataset.idx)];
                if (rect && rect.name !== 'root' && rect.node && rect.node.children && rect.node.children.length > 0) {
                    zoomToRectNode(rect.node);
                }
            }, 250);
        });

        g.addEventListener('dblclick', () => {
            clearTimeout(fgClickTimer);
            const rect = flamegraphRects[parseInt(g.dataset.idx)];
            if (rect && rect.name !== 'root') {
                state.pendingHighlight = rect.name;
                showSourceForFunction(rect.name);
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
            state.flamegraphZoomPath = [];
            state.flamegraphZoomNames = [];
            state.selectedTid = null;
            document.getElementById('thread-filter').value = '';
            const evtData = state.perEvent[state.selectedEvent];
            if (evtData) renderFlamegraph(evtData.flamegraph, evtData.function_summary.total_samples);
        });
    }

    // Breadcrumb click navigation
    container.querySelectorAll('.fg-crumb[data-crumb-idx]').forEach(crumb => {
        crumb.addEventListener('click', () => {
            var idx = parseInt(crumb.dataset.crumbIdx);
            var names = state.flamegraphZoomNames || [];
            if (idx < names.length) {
                // Breadcrumbs show the ancestry chain: crumb i = names[0..i]
                state.flamegraphZoomNames = names.slice(0, idx + 1);
                var evtData = state.perEvent[state.selectedEvent];
                if (evtData && evtData.flamegraph) {
                    var node = applyZoomFromNames(evtData.flamegraph);
                    if (node) renderFlamegraph(node, node.value);
                }
            }
        });
    });

    // Re-apply active search after re-render
    const searchInput = document.getElementById('fg-search');
    if (searchInput && searchInput.value.trim()) {
        applyFlamegraphSearch(searchInput.value.trim());
    }
}

function flattenTree(node, depth, x, width, rects, totalSamples) {
    const percent = totalSamples > 0 ? (node.value / totalSamples * 100) : 0;
    rects.push({ name: node.name, value: node.value, percent, depth, x, w: width,
                 node, inlined: !!node.inlined, module: node.module || '' });

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
            state.flamegraphZoomPath = [];
            state.flamegraphZoomNames = [];
            state.selectedTid = null;
            state.lastUpdateTime = Date.now();

            showReplayBanner(sessionId, data.metadata.timestamp);
            if (data.metrics) loadReplayMetrics(data.metrics);
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
                zoomToRectNode(rect.node);
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
    document.getElementById('wiz-skip').style.display = step === 4 ? '' : 'none';

    // Step-specific actions
    if (step === 3 && wizardData.connected) wizardVerifyPerf();
    if (step === 6) wizardBuildReview();
}

document.getElementById('wiz-back').addEventListener('click', function() {
    if (wizardStep > 1) wizardGoToStep(wizardStep - 1);
});

document.getElementById('wiz-next').addEventListener('click', function() {
    if (wizardStep < 6) {
        if (!wizardValidateStep(wizardStep)) return;
        // Apply binary/source config when leaving step 4 (Binary)
        if (wizardStep === 4) {
            wizardApplyStep4(function() { wizardGoToStep(wizardStep + 1); });
            return;
        }
        wizardGoToStep(wizardStep + 1);
    }
});

document.getElementById('wiz-skip').addEventListener('click', function() {
    // Skip is only visible on step 4 (binary/source)
    wizardGoToStep(wizardStep + 1);
});

function wizardValidateStep(step) {
    if (step === 1 && !wizardData.connected) {
        wizardSetStatus('wiz-connect-status', 'Connect to agent first', 'error');
        return false;
    }
    if (step === 2) {
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

// --- Step 2: Verify Perf & Probe Capabilities ---
function wizardVerifyPerf() {
    var result = document.getElementById('wiz-perf-result');
    result.innerHTML = '<div class="wiz-spinner">Verifying perf tool...</div>';
    document.getElementById('wiz-caps-section').classList.add('hidden');

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
            result.innerHTML = html;
            // Now probe capabilities
            if (data.functional) wizardProbeCapabilities();
        } else {
            html += '<div class="wiz-err">&#10007; perf not found: ' +
                escapeHtml(data.error || 'not available') + '</div>';
            result.innerHTML = html;
        }
    })
    .catch(function(err) {
        result.innerHTML = '<span class="wiz-err">Command failed: ' + escapeHtml(String(err)) + '</span>';
    });
}

function wizardProbeCapabilities() {
    var capsSection = document.getElementById('wiz-caps-section');
    var eventsEl = document.getElementById('wiz-caps-events');
    var cgEl = document.getElementById('wiz-caps-callgraph');

    eventsEl.innerHTML = '<div class="wiz-spinner">Probing supported events...</div>';
    capsSection.classList.remove('hidden');

    fetch('/api/agent/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd: 'reprobe', args: { pid: wizardData.pid }, timeout: 120 }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (!data.ok) {
            eventsEl.innerHTML = '<span class="wiz-err">' + escapeHtml(data.error || 'Probe failed') + '</span>';
            return;
        }
        wizardData.capabilities = data;
        var html = '';
        (data.record_events || []).forEach(function(evt) {
            html += '<span class="wiz-cap-tag record" title="Available for perf record">' + escapeHtml(evt) + '</span>';
        });
        (data.stat_only_events || []).forEach(function(evt) {
            html += '<span class="wiz-cap-tag stat" title="perf stat only">' + escapeHtml(evt) + '</span>';
        });
        if (!html) html = '<span class="wiz-err">No events detected</span>';
        eventsEl.innerHTML = html;

        var cg = data.callgraph_method;
        if (cg) {
            cgEl.innerHTML = 'Call-graph mode: <strong>' + escapeHtml(cg) + '</strong>';
        } else {
            cgEl.innerHTML = '<span class="wiz-warn">&#9888; No call-graph support detected (flat profile only)</span>';
        }
    })
    .catch(function(err) {
        eventsEl.innerHTML = '<span class="wiz-err">Probe failed: ' + escapeHtml(String(err)) + '</span>';
    });
}

// --- Step 3: Binary & Source ---
document.querySelectorAll('.wiz-browse-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
        var target = btn.dataset.target;
        var input = document.getElementById(target);
        var startPath = input.value.trim() || '/';
        var browseMode = (target === 'wiz-source-dir' || target === 'wiz-sysroot') ? 'dir' : 'file';
        openBrowseModal(startPath, browseMode, function(selected) {
            input.value = selected;
        });
    });
});

// Apply binary/source config when leaving step 4
function wizardApplyStep4(callback) {
    var binary = document.getElementById('wiz-binary').value.trim();
    var sourceDir = document.getElementById('wiz-source-dir').value.trim();
    var toolchainPrefix = document.getElementById('wiz-toolchain-prefix').value.trim();
    var sysroot = document.getElementById('wiz-sysroot').value.trim();
    var status = document.getElementById('wiz-binary-status');
    var indexStatus = document.getElementById('wiz-index-status');

    status.textContent = 'Applying...';
    status.className = 'wiz-status info';

    // Toolchain config must complete first — it sets addr2line/readelf used by binary indexing
    var toolchainStep = Promise.resolve();
    if (toolchainPrefix || sysroot) {
        var tcBody = {};
        if (toolchainPrefix) tcBody.prefix = toolchainPrefix;
        if (sysroot) tcBody.sysroot = sysroot;
        toolchainStep = fetch('/api/config/toolchain', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(tcBody),
        }).then(function(r) { return r.json(); }).then(function(r) {
            if (!r.ok) throw new Error(r.error || 'Toolchain config failed');
        });
    }

    toolchainStep.then(function() {
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

        if (promises.length === 0) {
            status.textContent = 'Applied';
            status.className = 'wiz-status ok';
            if (callback) callback();
            return;
        }

        return Promise.all(promises).then(function(results) {
            var errors = results.filter(function(r) { return !r.ok; });
            if (errors.length > 0) {
                status.textContent = errors.map(function(e) { return e.error; }).join('; ');
                status.className = 'wiz-status error';
            } else {
                status.textContent = 'Applied';
                status.className = 'wiz-status ok';
            }
            if (binary) {
                indexStatus.textContent = 'Indexing symbols and source files...';
                indexStatus.className = 'wiz-status info';
                indexStatus.classList.remove('hidden');
                wizardPollIndexStatus(function() {
                    if (callback) callback();
                });
            } else {
                if (callback) callback();
            }
        });
    }).catch(function(err) {
        status.textContent = 'Error: ' + err;
        status.className = 'wiz-status error';
        if (callback) callback();
    });
}

function wizardPollIndexStatus(callback) {
    var indexStatus = document.getElementById('wiz-index-status');
    fetch('/api/index/status')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.indexing) {
                indexStatus.textContent = 'Indexing: ' + (data.symbols_loaded || 0) + ' symbols, ' +
                    (data.source_files_found || 0) + ' source files...';
                indexStatus.className = 'wiz-status info';
                setTimeout(function() { wizardPollIndexStatus(callback); }, 500);
            } else {
                indexStatus.textContent = 'Ready: ' + (data.symbols_loaded || 0) + ' symbols, ' +
                    (data.source_files_found || 0) + ' source files from binary';
                indexStatus.className = 'wiz-status ok';
                if (callback) callback();
            }
        })
        .catch(function() {
            indexStatus.classList.add('hidden');
            if (callback) callback();
        });
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
    var tcPrefix = document.getElementById('wiz-toolchain-prefix').value.trim();
    var sysrootVal = document.getElementById('wiz-sysroot').value.trim();
    if (tcPrefix) {
        html += '<div class="wiz-review-row"><span class="wiz-review-label">Toolchain</span>' +
            '<span class="wiz-review-value">' + escapeHtml(tcPrefix) + '</span></div>';
    }
    if (sysrootVal) {
        html += '<div class="wiz-review-row"><span class="wiz-review-label">Sysroot</span>' +
            '<span class="wiz-review-value">' + escapeHtml(sysrootVal) + '</span></div>';
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

// Sync the control bar with the agent's actual state (page reload, or a
// --server agent that was already profiling when the UI attached).
function refreshControlBar() {
    fetch('/api/agent/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd: 'status', timeout: 10 }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (!data.ok || !data.state) return;
        if (data.state === 'profiling' || data.state === 'paused') {
            showControlBar(data.pid, '');
            if (data.pid) wizardData.pid = data.pid;
            if (data.frequency) wizardData.frequency = data.frequency;
            if (data.duration) wizardData.duration = data.duration;
            if (data.state === 'paused') {
                document.getElementById('ctrl-state').textContent = 'Paused';
                document.getElementById('ctrl-state').className = 'paused';
                document.getElementById('ctrl-pause').classList.add('hidden');
                document.getElementById('ctrl-resume').classList.remove('hidden');
            }
        } else {
            hideControlBar();
        }
    })
    .catch(function() {});
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
// Device Health Metrics
// =====================================================================

var METRICS_MAX = 150; // ~5 min at 2s interval

function showMetricsStrip() {
    var strip = document.getElementById('metrics-strip');
    if (strip && strip.classList.contains('hidden')) {
        strip.classList.remove('hidden');
    }
}

function getMetricSeverity(key, value) {
    var t = {
        cpu_pct: {w: 80, c: 95}, mem_pct: {w: 85, c: 95},
        temp_c: {w: 80, c: 95}, proc_cpu: {w: 80, c: 95},
        oom_score: {w: 500, c: 800}
    }[key];
    if (!t || value == null) return 'normal';
    if (value >= t.c) return 'critical';
    if (value >= t.w) return 'warning';
    return 'normal';
}

function setSeverity(cardId, key, value) {
    var card = document.getElementById(cardId);
    if (!card) return;
    card.className = 'metric-card';
    var sev = getMetricSeverity(key, value);
    if (sev !== 'normal') card.classList.add('severity-' + sev);
}

function updateMetricsCards(sys) {
    var cpu = sys.cpu || {};
    var mem = sys.mem || {};
    var cpuPct = cpu.overall_pct;
    var memPct = mem.used_pct;

    var cpuEl = document.getElementById('mv-cpu');
    if (cpuEl) cpuEl.textContent = cpuPct != null ? cpuPct.toFixed(1) + '%' : '--';
    setSeverity('mc-cpu', 'cpu_pct', cpuPct);

    var memEl = document.getElementById('mv-mem');
    if (memEl) memEl.textContent = memPct != null ? memPct.toFixed(1) + '%' : '--';
    setSeverity('mc-mem', 'mem_pct', memPct);

    var tempEl = document.getElementById('mv-temp');
    if (tempEl) {
        if (sys.temp_c != null) {
            tempEl.textContent = sys.temp_c + '\u00B0C';
            setSeverity('mc-temp', 'temp_c', sys.temp_c);
        } else {
            tempEl.textContent = '--';
        }
    }

    var loadEl = document.getElementById('mv-load');
    if (loadEl && sys.load) loadEl.textContent = sys.load.avg_1m.toFixed(2);

    // Mini sparklines in cards
    renderCardSparkline('ms-cpu', state.metricsSystem.map(function(s) {
        return s.cpu ? s.cpu.overall_pct : null;
    }), themeColor('--spark-cpu'), 0, 100);
    renderCardSparkline('ms-mem', state.metricsSystem.map(function(s) {
        return s.mem ? s.mem.used_pct : null;
    }), themeColor('--spark-mem'), 0, 100);
    renderCardSparkline('ms-temp', state.metricsSystem.map(function(s) {
        return s.temp_c != null ? s.temp_c : null;
    }), themeColor('--spark-temp'), 20, 110);
    renderCardSparkline('ms-load', state.metricsSystem.map(function(s) {
        return s.load ? s.load.avg_1m : null;
    }), themeColor('--spark-load'), 0, null);
}

function updateProcessCard(proc) {
    var card = document.getElementById('mc-proc');
    if (!card) return;
    var valEl = document.getElementById('mv-proc');
    var lblEl = document.getElementById('ml-proc');
    if (valEl) {
        var parts = [];
        if (proc.cpu_pct != null) parts.push('CPU:' + proc.cpu_pct.toFixed(1) + '%');
        if (proc.rss_kb) parts.push('RSS:' + formatKB(proc.rss_kb));
        valEl.textContent = parts.join(' ') || '--';
    }
    if (lblEl) lblEl.textContent = proc.comm ? proc.comm + ' (' + proc.pid + ')' : 'Process';
    setSeverity('mc-proc', 'proc_cpu', proc.cpu_pct);

    renderCardSparkline('ms-proc-cpu', state.metricsProcess.map(function(p) {
        return p.cpu_pct;
    }), themeColor('--spark-proc-cpu'), 0, 100);
}

function formatKB(kb) {
    if (kb >= 1048576) return (kb / 1048576).toFixed(1) + 'GB';
    if (kb >= 1024) return (kb / 1024).toFixed(0) + 'MB';
    return kb + 'KB';
}

function formatBytes(b) {
    if (b >= 1073741824) return (b / 1073741824).toFixed(1) + 'GB';
    if (b >= 1048576) return (b / 1048576).toFixed(1) + 'MB';
    if (b >= 1024) return (b / 1024).toFixed(1) + 'KB';
    return b + 'B';
}

function formatRate(bps) {
    if (bps >= 1048576) return (bps / 1048576).toFixed(1) + ' MB/s';
    if (bps >= 1024) return (bps / 1024).toFixed(1) + ' KB/s';
    return bps + ' B/s';
}

// --- Sparkline rendering (single polyline SVG) ---

function renderCardSparkline(containerId, data, color, minVal, maxVal) {
    var el = document.getElementById(containerId);
    if (!el) return;
    var vals = data.filter(function(v) { return v != null; });
    if (vals.length < 2) { el.innerHTML = ''; return; }

    var w = 120, h = 24;
    var mn = minVal != null ? minVal : Math.min.apply(null, vals);
    var mx = maxVal != null ? maxVal : Math.max.apply(null, vals);
    var range = mx - mn || 1;

    var pts = vals.map(function(v, i) {
        var x = (i / (vals.length - 1)) * w;
        var y = h - ((v - mn) / range) * (h - 2) - 1;
        return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');

    el.innerHTML = '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
        '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.5" />' +
        '</svg>';
}

function renderSparkline(container, data, opts) {
    if (!container || !data || data.length < 2) {
        if (container) container.innerHTML = '';
        return;
    }
    var w = opts.width || 250, h = opts.height || 50;
    var color = opts.color || themeColor('--spark-cpu') || '#4ade80';
    var fill = opts.fillColor || (color + '15');
    var vals = data;
    var mn = opts.min != null ? opts.min : Math.min.apply(null, vals);
    var mx = opts.max != null ? opts.max : Math.max.apply(null, vals);
    var range = mx - mn || 1;

    var pts = vals.map(function(v, i) {
        var x = (i / (vals.length - 1)) * w;
        var y = h - 2 - ((v - mn) / range) * (h - 4);
        return x.toFixed(1) + ',' + y.toFixed(1);
    });
    var polyPts = pts.join(' ');
    var fillPts = '0,' + h + ' ' + polyPts + ' ' + w + ',' + h;

    // Threshold rects
    var tRects = '';
    if (opts.thresholds) {
        opts.thresholds.forEach(function(t) {
            var ty = h - 2 - ((t.value - mn) / range) * (h - 4);
            if (ty > 0 && ty < h) {
                tRects += '<rect x="0" y="0" width="' + w + '" height="' + ty.toFixed(1) +
                    '" fill="' + t.color + '" />';
            }
        });
    }

    var svg = '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
        tRects +
        '<polygon points="' + fillPts + '" fill="' + fill + '" />' +
        '<polyline points="' + polyPts + '" fill="none" stroke="' + color +
        '" stroke-width="1.5" />' +
        '</svg>';
    container.innerHTML = svg;
}

function updateMetricsSparklines() {
    var panel = document.getElementById('metrics-sparklines');
    if (!panel || state.metricsCollapseLevel >= 1) return;

    var warnBg = themeColor('--spark-warn-bg');
    var critBg = themeColor('--spark-crit-bg');
    var charts = [
        { id: 'sp-cpu', label: 'CPU %', data: state.metricsSystem.map(function(s) { return s.cpu ? s.cpu.overall_pct : 0; }),
          color: themeColor('--spark-cpu'), min: 0, max: 100, thresholds: [{value:80,color:warnBg},{value:95,color:critBg}] },
        { id: 'sp-mem', label: 'Memory %', data: state.metricsSystem.map(function(s) { return s.mem ? s.mem.used_pct : 0; }),
          color: themeColor('--spark-mem'), min: 0, max: 100, thresholds: [{value:85,color:warnBg},{value:95,color:critBg}] },
        { id: 'sp-temp', label: 'Temperature', data: state.metricsSystem.map(function(s) { return s.temp_c != null ? s.temp_c : 0; }),
          color: themeColor('--spark-temp'), min: 20, max: 110, thresholds: [{value:80,color:warnBg},{value:95,color:critBg}] },
    ];

    // Add process charts if we have process data
    if (state.metricsProcess.length > 1) {
        charts.push({
            id: 'sp-proc-cpu', label: 'Process CPU %',
            data: state.metricsProcess.map(function(p) { return p.cpu_pct || 0; }),
            color: themeColor('--spark-proc-cpu'), min: 0, max: 100, thresholds: [{value:80,color:warnBg}]
        });
        charts.push({
            id: 'sp-proc-rss', label: 'Process RSS (MB)',
            data: state.metricsProcess.map(function(p) { return (p.rss_kb || 0) / 1024; }),
            color: themeColor('--spark-proc-rss'), min: 0, max: null
        });
    }

    // Build panels (only add new ones, don't rebuild every time)
    charts.forEach(function(c) {
        var el = document.getElementById(c.id);
        if (!el) {
            var div = document.createElement('div');
            div.className = 'spark-panel';
            div.id = c.id;
            div.innerHTML = '<div class="sp-label">' + c.label + '</div>' +
                '<div class="sp-chart"></div><div class="sp-hover"></div>';
            panel.appendChild(div);
            el = div;
            _wireSparklineHover(el);
        }
        el._sparkData = c.data.filter(function(v) { return v != null; });
        var chartEl = el.querySelector('.sp-chart');
        renderSparkline(chartEl, el._sparkData, {
            width: 300, height: 50, color: c.color,
            min: c.min, max: c.max, thresholds: c.thresholds || []
        });
    });
}

// Hover readout: show the value under the cursor in the panel's .sp-hover.
// Samples arrive on the agent's metrics interval (default 2s), newest last.
function _wireSparklineHover(panelEl) {
    var chartEl = panelEl.querySelector('.sp-chart');
    var hoverEl = panelEl.querySelector('.sp-hover');
    if (!chartEl || !hoverEl) return;
    chartEl.addEventListener('mousemove', function(ev) {
        var data = panelEl._sparkData || [];
        if (data.length < 2) return;
        var r = chartEl.getBoundingClientRect();
        if (r.width <= 0) return;
        var idx = Math.round((ev.clientX - r.left) / r.width * (data.length - 1));
        idx = Math.max(0, Math.min(data.length - 1, idx));
        var v = data[idx];
        var agoSec = (data.length - 1 - idx) * 2;
        hoverEl.textContent = (typeof v === 'number' ? v.toFixed(1) : v) +
            (agoSec > 0 ? '  (' + agoSec + 's ago)' : '  (now)');
    });
    chartEl.addEventListener('mouseleave', function() {
        hoverEl.textContent = '';
    });
}

function updateNetworkPanel(current, previous) {
    var panel = document.getElementById('metrics-network');
    if (!panel) return;

    var ifaces = current.interfaces || {};
    var names = Object.keys(ifaces);
    if (names.length === 0) { panel.innerHTML = ''; return; }

    var html = '';
    names.forEach(function(name) {
        var c = ifaces[name];
        var rateStr = '';
        if (previous && previous.interfaces && previous.interfaces[name]) {
            var p = previous.interfaces[name];
            var dt = current.ts - previous.ts;
            if (dt > 0) {
                var rxRate = Math.round((c.rx_bytes - p.rx_bytes) / dt);
                var txRate = Math.round((c.tx_bytes - p.tx_bytes) / dt);
                rateStr = ' (' + formatRate(rxRate) + ' in, ' + formatRate(txRate) + ' out)';
            }
        }
        var rxExtra = [];
        if (c.rx_drops > 0) rxExtra.push(c.rx_drops + ' drops');
        if (c.rx_errors > 0) rxExtra.push(c.rx_errors + ' errs');
        var txExtra = [];
        if (c.tx_drops > 0) txExtra.push(c.tx_drops + ' drops');
        if (c.tx_errors > 0) txExtra.push(c.tx_errors + ' errs');
        html += '<div class="net-iface"><span class="net-label">' + name + '</span>: ' +
            'RX ' + formatBytes(c.rx_bytes) + ' (' + c.rx_packets + ' pkts' +
            (rxExtra.length ? ', ' + rxExtra.join(', ') : '') + ') ' +
            'TX ' + formatBytes(c.tx_bytes) + ' (' + c.tx_packets + ' pkts' +
            (txExtra.length ? ', ' + txExtra.join(', ') : '') + ')' +
            rateStr + '</div>';
    });
    panel.innerHTML = html;
}

// --- Disk I/O panel (agent sends cumulative counters; rates are deltas) ---

function updateDiskPanel(current, previous) {
    var panel = document.getElementById('metrics-disk');
    if (!panel) return;

    var devices = current.devices || {};
    var names = Object.keys(devices);
    if (names.length === 0 && !current.proc) { panel.innerHTML = ''; return; }

    var dt = previous ? current.ts - previous.ts : 0;
    var html = '';

    names.forEach(function(name) {
        var c = devices[name];
        var rateStr = '';
        if (dt > 0 && previous.devices && previous.devices[name]) {
            var p = previous.devices[name];
            var rdRate = Math.max(0, Math.round((c.read_bytes - p.read_bytes) / dt));
            var wrRate = Math.max(0, Math.round((c.write_bytes - p.write_bytes) / dt));
            var iops = Math.max(0, Math.round(
                ((c.reads - p.reads) + (c.writes - p.writes)) / dt));
            rateStr = ' (' + formatRate(rdRate) + ' read, ' + formatRate(wrRate) +
                ' write, ' + iops + ' IOPS)';
        }
        html += '<div class="net-iface"><span class="net-label">' + escapeHtml(name) +
            '</span>: read ' + formatBytes(c.read_bytes) + ' / write ' +
            formatBytes(c.write_bytes) + rateStr + '</div>';
    });

    if (current.proc) {
        var pr = current.proc;
        var procRate = '';
        if (dt > 0 && previous && previous.proc) {
            var pp = previous.proc;
            procRate = ' (' + formatRate(Math.max(0, Math.round((pr.read_bytes - pp.read_bytes) / dt))) +
                ' read, ' + formatRate(Math.max(0, Math.round((pr.write_bytes - pp.write_bytes) / dt))) +
                ' write)';
        }
        html += '<div class="net-iface"><span class="net-label">process</span>: read ' +
            formatBytes(pr.read_bytes) + ' / write ' + formatBytes(pr.write_bytes) +
            procRate + '</div>';
    }

    panel.innerHTML = html;
}

// --- Metrics settings popover (drives the agent's configure_metrics) ---

(function initMetricsSettings() {
    var btn = document.getElementById('metrics-settings-btn');
    var pop = document.getElementById('metrics-settings-pop');
    if (!btn || !pop) return;

    btn.addEventListener('click', function(e) {
        e.stopPropagation();
        pop.classList.toggle('hidden');
    });
    pop.addEventListener('click', function(e) { e.stopPropagation(); });
    document.addEventListener('click', function() { pop.classList.add('hidden'); });
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') pop.classList.add('hidden');
    });

    document.getElementById('msp-apply').addEventListener('click', function() {
        var status = document.getElementById('msp-status');
        status.textContent = 'Applying...';
        status.className = 'msp-status';
        fetch('/api/agent/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                cmd: 'configure_metrics',
                args: {
                    network: document.getElementById('msp-network').checked,
                    disk: document.getElementById('msp-disk').checked,
                    interval: parseInt(document.getElementById('msp-interval').value) || 2,
                },
            }),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.ok) {
                status.textContent = 'Applied';
                status.className = 'msp-status ok';
                // Older agents ignore unknown args — reflect what came back
                if (data.disk === false || data.disk === undefined) {
                    state.metricsDisk = null;
                    state.metricsPrevDisk = null;
                    var panel = document.getElementById('metrics-disk');
                    if (panel) panel.innerHTML = '';
                }
                if (data.network === false) {
                    state.metricsNetwork = null;
                    state.metricsPrevNetwork = null;
                    var np = document.getElementById('metrics-network');
                    if (np) np.innerHTML = '';
                }
            } else {
                status.textContent = data.error || 'No agent connected';
                status.className = 'msp-status error';
            }
        })
        .catch(function() {
            status.textContent = 'No agent connected';
            status.className = 'msp-status error';
        });
    });
})();

// --- Platform info ---

function updatePlatformInfo(platform) {
    var el = document.getElementById('metrics-platform');
    if (!el || !platform) return;
    var parts = [];
    if (platform.arch) parts.push(platform.arch);
    if (platform.kernel) parts.push(platform.kernel);
    if (platform.perf_version) parts.push(platform.perf_version);
    el.textContent = parts.length ? parts.join(' \u2502 ') : '';
}

// --- System details: per-core CPU, memory breakdown, scheduling ---

function formatUptime(sec) {
    if (sec == null) return '--';
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
}

function formatCount(n) {
    if (n == null) return '--';
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'G';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
}

function updateSystemDetails(sys) {
    var panel = document.getElementById('metrics-sys-detail');
    if (!panel || state.metricsCollapseLevel >= 1) return;

    var html = '';
    var cpu = sys.cpu || {};
    var mem = sys.mem || {};
    var load = sys.load || {};

    // Per-core CPU bars
    if (cpu.per_core && cpu.per_core.length > 0) {
        html += '<div class="detail-section"><span class="detail-label">Per-Core CPU</span>';
        html += '<div class="core-bars">';
        cpu.per_core.forEach(function(pct, i) {
            var sev = pct >= 95 ? 'crit' : pct >= 80 ? 'warn' : 'ok';
            html += '<div class="core-bar" title="Core ' + i + ': ' + pct.toFixed(0) + '%">' +
                '<div class="core-fill core-' + sev + '" style="height:' + Math.max(pct, 2) + '%"></div>' +
                '</div>';
        });
        html += '</div>';
        if (cpu.freq_mhz) {
            var freq = cpu.freq_mhz;
            if (Array.isArray(freq)) freq = freq.reduce(function(a, b) { return a + b; }, 0) / freq.length;
            html += '<span class="detail-note">' + (freq / 1000).toFixed(2) + ' GHz</span>';
        }
        if (cpu.num_cores) html += '<span class="detail-note">' + cpu.num_cores + ' cores</span>';
        html += '</div>';
    }

    // Memory breakdown
    if (mem.total_kb) {
        html += '<div class="detail-section"><span class="detail-label">Memory</span>';
        var used = (mem.used_kb || 0);
        var cached = (mem.cached_kb || 0);
        var buffers = (mem.buffers_kb || 0);
        var total = mem.total_kb;
        var usedPct = (used / total * 100).toFixed(0);
        var cachePct = (cached / total * 100).toFixed(0);
        var bufPct = (buffers / total * 100).toFixed(0);
        html += '<div class="mem-bar-wrap">' +
            '<div class="mem-bar">' +
            '<div class="mem-seg mem-used" style="width:' + usedPct + '%" title="Used: ' + formatKB(used) + '"></div>' +
            '<div class="mem-seg mem-cached" style="width:' + cachePct + '%" title="Cache: ' + formatKB(cached) + '"></div>' +
            '<div class="mem-seg mem-buffers" style="width:' + bufPct + '%" title="Buffers: ' + formatKB(buffers) + '"></div>' +
            '</div></div>';
        html += '<span class="detail-note">' + formatKB(used) + ' used</span>';
        html += '<span class="detail-note">' + formatKB(cached) + ' cache</span>';
        html += '<span class="detail-note">' + formatKB(buffers) + ' buf</span>';
        html += '<span class="detail-note">' + formatKB(total) + ' total</span>';
        if (mem.swap_total_kb > 0) {
            var swapPct = mem.swap_used_kb > 0 ? (mem.swap_used_kb / mem.swap_total_kb * 100).toFixed(0) : '0';
            html += '<span class="detail-note">Swap: ' + formatKB(mem.swap_used_kb || 0) + ' / ' + formatKB(mem.swap_total_kb) + ' (' + swapPct + '%)</span>';
        }
        html += '</div>';
    }

    // Load averages + uptime
    if (load.avg_1m != null) {
        html += '<div class="detail-section"><span class="detail-label">Load</span>';
        html += '<span class="detail-val">' + load.avg_1m.toFixed(2) + '</span>';
        html += '<span class="detail-val">' + (load.avg_5m != null ? load.avg_5m.toFixed(2) : '--') + '</span>';
        html += '<span class="detail-val">' + (load.avg_15m != null ? load.avg_15m.toFixed(2) : '--') + '</span>';
        html += '<span class="detail-note">1m / 5m / 15m</span>';
        if (sys.uptime_sec != null) html += '<span class="detail-note">Up: ' + formatUptime(sys.uptime_sec) + '</span>';
        html += '</div>';
    }

    // Scheduling pressure
    if (sys.context_switches != null || sys.interrupts != null) {
        html += '<div class="detail-section"><span class="detail-label">Scheduling</span>';
        if (sys.context_switches != null) html += '<span class="detail-note">Ctx: ' + formatCount(sys.context_switches) + '</span>';
        if (sys.interrupts != null) html += '<span class="detail-note">IRQ: ' + formatCount(sys.interrupts) + '</span>';
        if (sys.procs_running != null) html += '<span class="detail-note">Run: ' + sys.procs_running + '</span>';
        if (sys.procs_blocked != null && sys.procs_blocked > 0) html += '<span class="detail-note sev-warn">Blocked: ' + sys.procs_blocked + '</span>';
        html += '</div>';
    }

    panel.innerHTML = html;
}

// --- Process details: state, threads, FDs, faults, CSW, OOM ---

function updateProcessDetails(proc) {
    var panel = document.getElementById('metrics-proc-detail');
    if (!panel || state.metricsCollapseLevel >= 1) return;

    var html = '';

    // Process identity + state
    html += '<div class="detail-section"><span class="detail-label">Process</span>';
    if (proc.comm) html += '<span class="detail-val">' + escapeHtml(proc.comm) + ' (' + proc.pid + ')</span>';
    if (proc.state) html += '<span class="detail-note">State: ' + escapeHtml(proc.state) + '</span>';
    html += '</div>';

    // Resources: threads, FDs, vsize
    if (proc.threads != null || proc.fds != null || proc.vsize_kb != null) {
        html += '<div class="detail-section"><span class="detail-label">Resources</span>';
        if (proc.threads != null) html += '<span class="detail-note">Threads: ' + proc.threads + '</span>';
        if (proc.fds != null) html += '<span class="detail-note">FDs: ' + proc.fds + '</span>';
        if (proc.vsize_kb != null) html += '<span class="detail-note">VSize: ' + formatKB(proc.vsize_kb) + '</span>';
        if (proc.rss_kb != null) html += '<span class="detail-note">RSS: ' + formatKB(proc.rss_kb) + '</span>';
        html += '</div>';
    }

    // Page faults
    if (proc.minor_faults != null || proc.major_faults != null) {
        html += '<div class="detail-section"><span class="detail-label">Page Faults</span>';
        if (proc.minor_faults != null) html += '<span class="detail-note">Minor: ' + formatCount(proc.minor_faults) + '</span>';
        if (proc.major_faults != null) {
            var majClass = proc.major_faults > 0 ? ' sev-warn' : '';
            html += '<span class="detail-note' + majClass + '">Major: ' + formatCount(proc.major_faults) + '</span>';
        }
        html += '</div>';
    }

    // Context switches
    if (proc.voluntary_csw != null || proc.involuntary_csw != null) {
        html += '<div class="detail-section"><span class="detail-label">Ctx Switches</span>';
        if (proc.voluntary_csw != null) html += '<span class="detail-note">Vol: ' + formatCount(proc.voluntary_csw) + '</span>';
        if (proc.involuntary_csw != null) html += '<span class="detail-note">Invol: ' + formatCount(proc.involuntary_csw) + '</span>';
        html += '</div>';
    }

    // OOM score
    if (proc.oom_score != null) {
        var oomSev = getMetricSeverity('oom_score', proc.oom_score);
        var oomClass = oomSev === 'critical' ? ' sev-crit' : oomSev === 'warning' ? ' sev-warn' : '';
        html += '<div class="detail-section"><span class="detail-label">OOM</span>';
        html += '<span class="detail-note' + oomClass + '">Score: ' + proc.oom_score + '</span>';
        html += '</div>';
    }

    panel.innerHTML = html;
}

// Metrics collapse toggle
(function() {
    var btn = document.getElementById('metrics-collapse-btn');
    if (!btn) return;
    btn.addEventListener('click', function() {
        var strip = document.getElementById('metrics-strip');
        if (!strip) return;
        state.metricsCollapseLevel = (state.metricsCollapseLevel + 1) % 3;
        strip.classList.remove('compact', 'minimal');
        if (state.metricsCollapseLevel === 1) {
            strip.classList.add('compact');
            btn.innerHTML = '&#9654;'; // right arrow
        } else if (state.metricsCollapseLevel === 2) {
            strip.classList.add('minimal');
            btn.innerHTML = '&#9650;'; // up arrow
        } else {
            btn.innerHTML = '&#9660;'; // down arrow
            updateMetricsSparklines();
        }
    });
})();

// Load metrics on session replay
function loadReplayMetrics(metrics) {
    if (!metrics) return;
    state.metricsSystem = (metrics.system || []).slice(-METRICS_MAX);
    state.metricsProcess = (metrics.process || []).slice(-METRICS_MAX);
    if (metrics.network && metrics.network.length > 0) {
        state.metricsNetwork = metrics.network[metrics.network.length - 1];
    }
    if (state.metricsSystem.length > 0) {
        showMetricsStrip();
        updateMetricsCards(state.metricsSystem[state.metricsSystem.length - 1]);
        updateMetricsSparklines();
    }
    if (state.metricsProcess.length > 0) {
        updateProcessCard(state.metricsProcess[state.metricsProcess.length - 1]);
    }
    if (state.metricsNetwork) {
        var prev = metrics.network && metrics.network.length > 1 ?
            metrics.network[metrics.network.length - 2] : null;
        updateNetworkPanel(state.metricsNetwork, prev);
    }
    if (metrics.disk && metrics.disk.length > 0) {
        state.metricsDisk = metrics.disk[metrics.disk.length - 1];
        var prevDisk = metrics.disk.length > 1 ?
            metrics.disk[metrics.disk.length - 2] : null;
        updateDiskPanel(state.metricsDisk, prevDisk);
    }
}

// =====================================================================
// Thread Analysis Tab
// =====================================================================

var _threadDetailState = { tid: null, comm: '', data: null };

function renderThreadsTab() {
    var overview = document.getElementById('threads-overview');
    var detail = document.getElementById('thread-detail');
    if (!overview) return;

    // If in detail view, keep it
    if (_threadDetailState.tid !== null && !detail.classList.contains('hidden')) return;

    var evt = state.selectedEvent || 'cycles';
    fetch('/api/thread-summary?event=' + encodeURIComponent(evt))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.threads || data.threads.length === 0) {
                overview.innerHTML = '<p class="empty">No thread data yet</p>';
                return;
            }
            renderThreadOverview(data);
        })
        .catch(function() {
            overview.innerHTML = '<p class="empty">Failed to load thread data</p>';
        });
}

function renderThreadOverview(data) {
    var overview = document.getElementById('threads-overview');
    var detail = document.getElementById('thread-detail');
    overview.classList.remove('hidden');
    detail.classList.add('hidden');
    _threadDetailState.tid = null;

    var html = '<table class="threads-table"><thead><tr>' +
        '<th>Thread</th><th>TID</th><th>Samples</th><th>CPU %</th>' +
        '<th>Top Function</th><th>Top Functions</th></tr></thead><tbody>';

    data.threads.forEach(function(t) {
        var barW = Math.max(2, Math.min(100, t.percent));
        var topFuncsHtml = '';
        if (t.top_functions && t.top_functions.length > 0) {
            topFuncsHtml = t.top_functions.map(function(f) {
                return escapeHtml(f.name) + ' <span style="opacity:0.5">' + f.percent + '%</span>';
            }).join(', ');
        }
        html += '<tr class="thread-row" data-tid="' + t.tid + '" data-comm="' + escapeAttr(t.comm) + '">' +
            '<td><strong>' + escapeHtml(t.comm || '(unnamed)') + '</strong></td>' +
            '<td>' + t.tid + '</td>' +
            '<td>' + t.samples.toLocaleString() + '</td>' +
            '<td><span class="thread-cpu-bar" style="width:' + barW + 'px"></span>' + t.percent + '%</td>' +
            '<td><code>' + escapeHtml(t.top_function || '-') + '</code></td>' +
            '<td class="thread-top-funcs">' + topFuncsHtml + '</td>' +
            '</tr>';
    });
    html += '</tbody></table>';
    html += '<p style="font-size:11px;color:var(--text-tertiary);margin-top:8px;">' +
        data.total_samples.toLocaleString() + ' total samples across ' +
        data.threads.length + ' threads</p>';

    overview.innerHTML = html;

    // Click handlers
    overview.querySelectorAll('.thread-row').forEach(function(row) {
        row.addEventListener('click', function() {
            var tid = parseInt(row.getAttribute('data-tid'));
            var comm = row.getAttribute('data-comm');
            openThreadDetail(tid, comm);
        });
    });
}

function openThreadDetail(tid, comm) {
    var overview = document.getElementById('threads-overview');
    var detail = document.getElementById('thread-detail');
    overview.classList.add('hidden');
    detail.classList.remove('hidden');

    _threadDetailState.tid = tid;
    _threadDetailState.comm = comm;

    document.getElementById('thread-detail-title').textContent =
        (comm || '(unnamed)') + ' (TID ' + tid + ')';

    // Reset to functions tab
    detail.querySelectorAll('.thread-dtab').forEach(function(t) { t.classList.remove('active'); });
    detail.querySelectorAll('.thread-detail-panel').forEach(function(p) { p.classList.remove('active'); });
    detail.querySelector('.thread-dtab[data-thread-tab="t-functions"]').classList.add('active');
    document.getElementById('t-functions').classList.add('active');

    // Load data
    var evt = state.selectedEvent || 'cycles';
    document.getElementById('thread-fn-table').innerHTML = '<p class="empty loading">Loading...</p>';
    document.getElementById('thread-fg-container').innerHTML = '<p class="empty">Switch to Flame Graph tab to view</p>';
    document.getElementById('thread-source-files').innerHTML = '';
    document.getElementById('thread-source-view').innerHTML = '';

    fetch('/api/thread-view?event=' + encodeURIComponent(evt) + '&tid=' + tid)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            _threadDetailState.data = data;
            renderThreadFunctions(data.function_summary);
            renderThreadFlamegraph(data.flamegraph, data.function_summary.total_samples);
            if (data.source_files && data.source_files.length > 0) {
                renderThreadSourceFiles(data.source_files, tid);
            }
        })
        .catch(function() {
            document.getElementById('thread-fn-table').innerHTML =
                '<p class="empty">Failed to load thread data</p>';
        });
}

function renderThreadFunctions(data) {
    var container = document.getElementById('thread-fn-table');
    if (!data.functions || data.functions.length === 0) {
        container.innerHTML = '<p class="empty">No function data</p>';
        return;
    }

    var total = data.total_samples;
    var html = '<table class="threads-table"><thead><tr>' +
        '<th>Function</th><th>Self %</th><th>Self</th><th>Total %</th><th>Total</th><th>Module</th>' +
        '</tr></thead><tbody>';

    data.functions.forEach(function(f) {
        var selfPct = f.self_percent || f.percent || 0;
        var totalPct = f.total_percent || 0;
        var barW = Math.max(1, Math.min(80, selfPct * 0.8));
        html += '<tr class="thread-row" data-func="' + escapeAttr(f.name) + '">' +
            '<td><code>' + escapeHtml(f.name) + '</code></td>' +
            '<td><span class="thread-cpu-bar" style="width:' + barW + 'px;background:' +
            heatColor(selfPct / 100) + '"></span>' + selfPct.toFixed(1) + '%</td>' +
            '<td>' + (f.self_samples || f.samples) + '</td>' +
            '<td>' + totalPct.toFixed(1) + '%</td>' +
            '<td>' + (f.total_samples || 0) + '</td>' +
            '<td style="font-size:11px;color:var(--text-tertiary)">' +
            escapeHtml((f.module || '').split('/').pop()) + '</td></tr>';
    });
    html += '</tbody></table>';
    html += '<p style="font-size:11px;color:var(--text-tertiary);margin-top:8px;">' +
        total.toLocaleString() + ' samples in this thread</p>';
    container.innerHTML = html;
}

function heatColor(ratio) {
    // 0 = green, 1 = red
    if (ratio < 0.33) return '#3fb950';
    if (ratio < 0.66) return '#d29922';
    return '#f85149';
}

function renderThreadFlamegraph(data, totalSamples) {
    var container = document.getElementById('thread-fg-container');
    if (!data || !data.children || data.children.length === 0) {
        container.innerHTML = '<p class="empty">No flame graph data</p>';
        return;
    }

    var width = container.clientWidth - 32;
    if (width < 10) width = 600;
    var rowHeight = 18;
    var fontSize = 11;
    var charWidth = 6.5;
    totalSamples = totalSamples || data.value;

    var rects = [];
    var maxDepth = flattenTree(data, 0, 0, width, rects, totalSamples);
    var height = (maxDepth + 1) * rowHeight + 4;
    var fgTextColor = themeColor('--fg-text');

    var svg = '<svg width="' + width + '" height="' + height + '" class="flamegraph-svg" ' +
        'style="display:block;margin:0 auto;">';

    rects.forEach(function(r) {
        if (r.w < 0.5) return;
        var y = height - (r.depth + 1) * rowHeight;
        var color = fgModuleColor(r.name, r.module || '', r.inlined);

        svg += '<g>';
        svg += '<rect x="' + r.x + '" y="' + y + '" width="' + Math.max(r.w - 1, 1) +
            '" height="' + (rowHeight - 1) + '" fill="' + color + '" rx="2"/>';
        if (r.w > 36) {
            var maxChars = Math.floor((r.w - 6) / charWidth);
            if (maxChars > 1) {
                var label = r.name.length > maxChars ? r.name.substring(0, maxChars - 1) + '\u2026' : r.name;
                svg += '<text x="' + (r.x + 3) + '" y="' + (y + 13) + '" font-size="' + fontSize +
                    '" fill="' + fgTextColor + '" pointer-events="none">' + escapeHtml(label) + '</text>';
            }
        }
        svg += '<title>' + escapeHtml(r.name) + ' (' + r.value + ' samples, ' +
            r.percent.toFixed(1) + '%)</title>';
        svg += '</g>';
    });
    svg += '</svg>';

    var html = svg;
    html += '<div class="fg-info-bar">Hover over a frame to see details</div>';
    container.innerHTML = html;

    // Hover info
    var infoBar = container.querySelector('.fg-info-bar');
    var svgEl = container.querySelector('svg');
    if (svgEl && infoBar) {
        svgEl.addEventListener('mousemove', function(e) {
            var g = e.target.closest('g');
            if (!g) return;
            var title = g.querySelector('title');
            if (title) {
                infoBar.textContent = title.textContent;
                infoBar.classList.add('fg-info-active');
            }
        });
        svgEl.addEventListener('mouseleave', function() {
            infoBar.textContent = 'Hover over a frame to see details';
            infoBar.classList.remove('fg-info-active');
        });
    }
}

function renderThreadSourceFiles(sourceFiles, tid) {
    var container = document.getElementById('thread-source-files');
    if (!sourceFiles || sourceFiles.length === 0) {
        container.innerHTML = '';
        return;
    }

    var html = '<div class="thread-source-file-list">';
    sourceFiles.forEach(function(f, i) {
        var basename = f.path.split('/').pop();
        html += '<button class="thread-source-file-btn' +
            '" data-path="' + escapeAttr(f.path) + '">' +
            escapeHtml(basename) + ' (' + f.total_samples + ')</button>';
    });
    html += '</div>';
    container.innerHTML = html;

    // Don't auto-load — wait for click
    document.getElementById('thread-source-view').innerHTML =
        '<p class="empty">Click a source file to view annotated source</p>';

    container.querySelectorAll('.thread-source-file-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            container.querySelectorAll('.thread-source-file-btn').forEach(function(b) {
                b.classList.remove('active');
            });
            btn.classList.add('active');
            loadThreadSource(btn.getAttribute('data-path'), tid);
        });
    });
}

function loadThreadSource(filePath, tid) {
    var container = document.getElementById('thread-source-view');
    container.innerHTML = '<p class="empty loading">Loading source...</p>';

    var evt = state.selectedEvent || 'cycles';
    var url = '/api/source?file=' + encodeURIComponent(filePath) +
        '&event=' + encodeURIComponent(evt) + '&tid=' + tid;

    fetch(url)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.lines && data.lines.length > 0) {
                renderThreadSourceView(container, data.file, data.lines);
            } else {
                container.innerHTML = '<p class="empty">No source data for this thread in ' +
                    escapeHtml(filePath) + '</p>';
            }
        })
        .catch(function() {
            container.innerHTML = '<p class="empty">Error loading source</p>';
        });
}

function renderThreadSourceView(container, filePath, lines) {
    if (!lines || lines.length === 0) {
        container.innerHTML = '<p class="empty">No source data</p>';
        return;
    }

    var totalSamples = 0;
    var maxSamples = 0;
    var hottestLine = 0;
    lines.forEach(function(l) {
        totalSamples += l.samples;
        if (l.samples > maxSamples) {
            maxSamples = l.samples;
            hottestLine = l.line;
        }
    });

    var maxLine = Math.max(2000, hottestLine + 100);
    var displayLines = lines.length > maxLine ? lines.slice(0, maxLine) : lines;

    var html = '<div class="source-header">' + escapeHtml(filePath) +
        ' (' + totalSamples + ' samples, thread ' +
        escapeHtml(_threadDetailState.comm || '') + ')</div>';
    html += '<div class="source-scroll">';

    // Same markup + heat scale as the main source view
    displayLines.forEach(function(l) {
        var heat = 0;
        if (l.percent > 0) heat = 1;
        if (l.percent > 2) heat = 2;
        if (l.percent > 5) heat = 3;
        if (l.percent > 15) heat = 4;
        if (l.percent > 30) heat = 5;
        var samplesText = l.samples > 0 ? l.samples + ' (' + l.percent.toFixed(1) + '%)' : '';
        html += '<div class="source-line heat-' + heat + '" data-line="' + l.line + '">' +
            '<span class="line-no">' + l.line + '</span>' +
            '<span class="line-samples">' + samplesText + '</span>' +
            '<span class="line-code">' + escapeHtml(l.source) + '</span></div>';
    });
    html += '</div>';
    container.innerHTML = html;

    // Scroll to hottest line
    if (hottestLine > 0) {
        var hotEl = container.querySelector('[data-line="' + hottestLine + '"]');
        if (hotEl) hotEl.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
}

// Thread detail tab switching
(function() {
    var detail = document.getElementById('thread-detail');
    if (!detail) return;
    detail.querySelectorAll('.thread-dtab').forEach(function(tab) {
        tab.addEventListener('click', function() {
            var target = tab.getAttribute('data-thread-tab');
            detail.querySelectorAll('.thread-dtab').forEach(function(t) { t.classList.remove('active'); });
            detail.querySelectorAll('.thread-detail-panel').forEach(function(p) { p.classList.remove('active'); });
            tab.classList.add('active');
            var panel = document.getElementById(target);
            if (panel) panel.classList.add('active');
        });
    });

    // Back button
    var backBtn = document.getElementById('thread-back-btn');
    if (backBtn) {
        backBtn.addEventListener('click', function() {
            _threadDetailState.tid = null;
            _threadDetailState.data = null;
            document.getElementById('threads-overview').classList.remove('hidden');
            detail.classList.add('hidden');
            renderThreadsTab();
        });
    }
})();

// =====================================================================

fetch('/api/status')
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.agent_connected) {
            // Agent already connected — skip to profiling view
            updateStatus({ connected: true, agent: data.agent_addr });
            showProfilingView();
            refreshControlBar();
        }
        // Else: stay on landing page
    })
    .catch(function() {});

connectSSE();
loadSessions();
