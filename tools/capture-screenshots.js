// tools/capture-screenshots.js
//
// Capture the 7 UI screenshots used by the docs site, driving a live PerfLens
// server with puppeteer.
//
// Prereqs (one-time, from the repo root):
//   npm install
//
// Setup (in three terminals OR background processes):
//   1. ./test/sample_workload                         # CPU-busy test program
//   2. python3 server/perflens_server.py \
//        --http-port 8089 --port 9899 \
//        --binary test/sample_workload --source-dir test
//   3. agent-c/perflens-agent --server 127.0.0.1 --port 9899 \
//        --pid $(pgrep -f sample_workload | tail -1) \
//        --duration 4 --frequency 199
//   4. curl -X POST http://localhost:8089/api/agent/command \
//        -H "Content-Type: application/json" \
//        -d '{"cmd":"start","args":{"pid":'"$(pgrep -f sample_workload | tail -1)"',"frequency":199,"duration":4},"timeout":30}'
//   5. Wait for /api/status to show "total_samples" > 1000
//
// Run:
//   node tools/capture-screenshots.js
//
// Output: docs/screenshots/01..07-*.png

const path = require('path');
const fs = require('fs');
const puppeteer = require(path.join(__dirname, '..', 'node_modules', 'puppeteer'));

const BASE = process.env.PERFLENS_URL || 'http://localhost:8089';
const OUT = process.env.OUT_DIR ||
    path.join(__dirname, '..', 'docs', 'screenshots');

fs.mkdirSync(OUT, { recursive: true });

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function snap(page, name) {
    const p = path.join(OUT, `${name}.png`);
    await page.screenshot({ path: p, fullPage: false });
    console.log('  ->', p);
}

async function clickTab(page, tab) {
    await page.evaluate(t => {
        const b = document.querySelector(`button.tab[data-tab="${t}"]`);
        if (b) b.click();
    }, tab);
}

async function scrollTo(page, sel) {
    await page.evaluate(s => {
        const el = document.querySelector(s);
        if (el) el.scrollIntoView({ behavior: 'instant', block: 'start' });
    }, sel);
}

async function selectCyclesEvent(page) {
    await page.evaluate(() => {
        const sel = document.getElementById('event-select');
        if (!sel) return;
        let pick = null;
        for (const opt of sel.options) {
            if (opt.value.includes('cycles') && opt.value.includes('cpu_core')) {
                pick = opt.value; break;
            }
        }
        if (!pick) for (const opt of sel.options) {
            if (opt.value.includes('cycles')) { pick = opt.value; break; }
        }
        if (pick) { sel.value = pick; sel.dispatchEvent(new Event('change')); }
    });
}

(async () => {
    const browser = await puppeteer.launch({
        headless: 'new',
        args: ['--no-sandbox', '--disable-setuid-sandbox'],
        defaultViewport: { width: 1440, height: 900, deviceScaleFactor: 2 },
    });
    const page = await browser.newPage();

    console.log('[1] navigate');
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await sleep(1500);

    console.log('[2] profiling view + cycles event');
    await page.evaluate(() => {
        if (typeof showView === 'function') showView('view-profiling');
        if (typeof switchToTab === 'function') switchToTab('functions');
    });
    await sleep(400);
    await selectCyclesEvent(page);
    await sleep(800);
    await scrollTo(page, '#tabs');
    await sleep(300);
    await snap(page, '01-functions');

    console.log('[3] flame graph');
    await clickTab(page, 'flamegraph');
    await sleep(300);
    await page.evaluate(() => {
        if (typeof renderCurrentEvent === 'function') renderCurrentEvent();
    });
    await sleep(1500);
    await scrollTo(page, '#flamegraph-container');
    await sleep(300);
    await snap(page, '02-flamegraph');

    console.log('[4] source view');
    await clickTab(page, 'functions');
    await sleep(600);
    await page.evaluate(() => {
        const fn = document.querySelector(
            '#function-table .fn-source-link, #function-table .src-link, #function-table .fn-name');
        if (fn) { fn.click(); return; }
        const row = document.querySelector('#function-table tbody tr');
        if (row) row.click();
    });
    await sleep(2000);
    await scrollTo(page, '#tabs');
    await sleep(300);
    await snap(page, '03-source');

    console.log('[5] threads');
    await clickTab(page, 'threads');
    await sleep(2500);
    await scrollTo(page, '#tabs');
    await sleep(300);
    await snap(page, '04-threads');

    console.log('[6] sessions');
    await clickTab(page, 'sessions');
    await sleep(1500);
    await scrollTo(page, '#tabs');
    await sleep(300);
    await snap(page, '05-sessions');

    console.log('[7] overview (top of profiling view)');
    await page.evaluate(() => window.scrollTo(0, 0));
    await sleep(500);
    await snap(page, '07-overview');

    console.log('[8] functions, light theme');
    await clickTab(page, 'functions');
    await sleep(300);
    await page.evaluate(() => {
        document.documentElement.setAttribute('data-theme', 'light');
        try { localStorage.setItem('perflens-theme', 'light'); } catch (e) {}
        const lbl = document.getElementById('theme-label');
        if (lbl) lbl.textContent = 'Light';
    });
    await sleep(800);
    await scrollTo(page, '#tabs');
    await sleep(300);
    await snap(page, '06-functions-light');

    await browser.close();
    console.log('done.');
})().catch(e => { console.error(e); process.exit(1); });
