#!/usr/bin/env node
/**
 * Browser E2E: drives the real UI (puppeteer) against a real server
 * replaying a device-captured fixture session.
 *
 * Self-contained: starts its own server on a free port with an isolated
 * PERFLENS_HOME, materializes a fixture session, and tears everything
 * down. Covers session replay through the UI, the function table, the
 * flamegraph (render, single-click ancestry zoom, breadcrumbs, reset,
 * search, context menu), the export menu + endpoints, and asserts zero
 * JS errors throughout.
 *
 *   node tests/e2e_ui.mjs        (PERFLENS_PYTHON overrides the python)
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';

const REPO = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const FIXTURE = path.join(REPO, 'tests', 'fixtures', 'session-x86-baseline');

let passed = 0, failed = 0;
function assert(cond, msg) {
    if (cond) { passed++; console.log(`  PASS  ${msg}`); }
    else      { failed++; console.error(`  FAIL  ${msg}`); }
}

function pickPython() {
    if (process.env.PERFLENS_PYTHON) return process.env.PERFLENS_PYTHON;
    const venv = path.join(REPO, '.venv', 'bin', 'python');
    return fs.existsSync(venv) ? venv : 'python3';
}

function freePort() {
    return new Promise((resolve, reject) => {
        const srv = net.createServer();
        srv.listen(0, '127.0.0.1', () => {
            const port = srv.address().port;
            srv.close(() => resolve(port));
        });
        srv.on('error', reject);
    });
}

function materializeSession(sessionsDir) {
    const id = 'e2e-fixture';
    const dest = path.join(sessionsDir, id);
    fs.mkdirSync(dest, { recursive: true });
    const chunks = fs.readdirSync(FIXTURE)
        .filter(f => f.startsWith('chunk_') && f.endsWith('.txt.gz'))
        .sort();
    chunks.forEach((f, i) => {
        const data = zlib.gunzipSync(fs.readFileSync(path.join(FIXTURE, f)));
        const name = `chunk_${String(i).padStart(5, '0')}.txt`;
        fs.writeFileSync(path.join(dest, name), data);
    });
    const meta = JSON.parse(
        fs.readFileSync(path.join(FIXTURE, 'metadata.json'), 'utf8'));
    meta.session_id = id;
    fs.writeFileSync(path.join(dest, 'metadata.json'), JSON.stringify(meta));
    return id;
}

async function waitForServer(url, proc, timeoutMs = 30000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (proc.exitCode !== null)
            throw new Error(`server exited early (code ${proc.exitCode})`);
        try {
            const r = await fetch(url + '/api/status');
            if (r.ok) return;
        } catch { /* not up yet */ }
        await new Promise(r => setTimeout(r, 200));
    }
    throw new Error('server did not become ready');
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'perflens-e2e-'));
    const sessionsDir = path.join(home, 'sessions');
    const sessionId = materializeSession(sessionsDir);

    const httpPort = await freePort();
    const agentPort = await freePort();
    const URL = `http://127.0.0.1:${httpPort}`;

    const server = spawn(
        pickPython(),
        ['-m', 'perflens.cli', 'serve',
         '--http-port', String(httpPort), '--port', String(agentPort),
         '--sessions-dir', sessionsDir, '--source-dir', home],
        {
            cwd: REPO,
            env: { ...process.env, PERFLENS_HOME: home,
                   PYTHONPATH: path.join(REPO, 'src') },
            stdio: ['ignore', 'pipe', 'pipe'],
        });
    let serverLog = '';
    server.stdout.on('data', d => { serverLog += d; });
    server.stderr.on('data', d => { serverLog += d; });

    let browser;
    try {
        await waitForServer(URL, server);

        browser = await puppeteer.launch({
            headless: true,
            args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'],
        });
        const page = await browser.newPage();
        const pageErrors = [];
        page.on('pageerror', err => pageErrors.push(err.message));
        page.on('console', msg => {
            if (msg.type() === 'error' && !msg.text().includes('404'))
                console.error('  CONSOLE:', msg.text());
        });

        await page.goto(URL, { waitUntil: 'networkidle2' });

        // ── Landing page → Sessions ──
        console.log('\n── Session replay ──');
        await page.waitForSelector('#card-sessions', { timeout: 5000 });
        await page.click('#card-sessions');
        await page.waitForSelector('#sessions-list .replay-btn',
                                   { timeout: 5000 });
        const listedId = await page.$eval(
            '#sessions-table tbody tr td', td => td.textContent);
        assert(listedId === sessionId, `Session listed (${listedId})`);

        // Click inside the page: the list can re-render (initial
        // loadSessions racing the card click), detaching handles.
        await page.evaluate(() =>
            document.querySelector('#sessions-list .replay-btn').click());
        await page.waitForSelector('#replay-banner.visible',
                                   { timeout: 15000 });
        assert(true, 'Replay banner visible');

        // Replay lands on the Functions tab with rows
        await page.waitForSelector(
            '#tab-functions.active #function-table tbody tr',
            { timeout: 10000 });
        const rows = await page.$$eval('#function-table tbody tr',
                                       trs => trs.length);
        assert(rows > 5, `Function table has ${rows} rows`);

        // ── Flamegraph ──
        console.log('\n── Flamegraph ──');
        await page.click('.tab[data-tab="flamegraph"]');
        await page.waitForSelector('#flamegraph-container svg g[data-idx]',
                                   { timeout: 10000 });
        const frameCount = await page.$$eval(
            '#flamegraph-container svg g[data-idx]', gs => gs.length);
        assert(frameCount > 3, `Flamegraph has ${frameCount} frames`);

        // Single click on a frame with children → ancestry-path zoom.
        // Prefer a plainly-named frame ([unknown] etc. make poor test
        // subjects), fall back to any zoomable one.
        const zoomable = await page.evaluate(() => {
            let fallback = null;
            for (let i = 1; i < flamegraphRects.length; i++) {
                const r = flamegraphRects[i];
                if (r.name === 'root' || !r.node || !r.node.children
                    || r.node.children.length === 0) continue;
                if (/^[A-Za-z_][\w:.]*$/.test(r.name))
                    return { idx: i, name: r.name };
                fallback = fallback || { idx: i, name: r.name };
            }
            return fallback;
        });
        assert(zoomable !== null, 'Found a zoomable frame');

        await page.evaluate(idx => {
            document.querySelector(`g[data-idx="${idx}"]`)
                .dispatchEvent(new MouseEvent('click', { bubbles: true }));
        }, zoomable.idx);
        await sleep(600); // single-click zoom fires after a 250ms timer

        const zoomNames = await page.evaluate(
            () => state.flamegraphZoomNames.slice());
        assert(zoomNames.length > 0
               && zoomNames[zoomNames.length - 1] === zoomable.name,
               `Zoomed via ancestry names [${zoomNames.join(' > ')}]`);

        // Breadcrumbs: ancestors are clickable, the current level is not
        const crumbs = await page.evaluate(() => ({
            all: document.querySelectorAll('.fg-crumb').length,
            clickable: document.querySelectorAll('.fg-crumb[data-crumb-idx]').length,
            current: document.querySelectorAll('.fg-crumb-current').length,
        }));
        assert(crumbs.all === zoomNames.length
               && crumbs.clickable === zoomNames.length - 1
               && crumbs.current === 1,
               `Breadcrumb chain matches zoom depth (${crumbs.all})`);

        // The zoom target is now the rendered root (frame counts can go
        // either way: zooming reveals frames pruned as sub-pixel before)
        const zoomedRoot = await page.evaluate(() => flamegraphRects[0].name);
        assert(zoomedRoot === zoomable.name,
               `Zoom target is the rendered root (${zoomedRoot})`);

        // Reset restores the full graph
        await page.click('#flamegraph-reset');
        await sleep(200);
        const resetCount = await page.$$eval(
            '#flamegraph-container svg g[data-idx]', gs => gs.length);
        const resetNames = await page.evaluate(
            () => state.flamegraphZoomNames.length);
        assert(resetCount === frameCount && resetNames === 0,
               'Reset restored full flamegraph');

        // ── Flamegraph search ──
        console.log('\n── Flamegraph search ──');
        // The search box takes a regex — use a plainly-named frame
        const anyName = await page.evaluate(() => {
            const r = flamegraphRects.find(
                r => r.name !== 'root' && /^[A-Za-z_][\w:.]*$/.test(r.name));
            return r ? r.name : null;
        });
        assert(anyName !== null, `Found a searchable frame (${anyName})`);
        await page.type('#fg-search', anyName.slice(0, 6));
        await sleep(400); // debounced 200ms
        const matchInfo = await page.evaluate(() => ({
            text: document.getElementById('fg-search-matches').textContent,
            matches: document.querySelectorAll('g.fg-match').length,
            dimmed: document.querySelectorAll('g.fg-dim').length,
        }));
        assert(matchInfo.matches > 0,
               `Search highlights ${matchInfo.matches} frames`);
        assert(/\d+ \/ \d+ frames/.test(matchInfo.text),
               `Match counter shows "${matchInfo.text}"`);
        assert(matchInfo.dimmed > 0, 'Non-matching frames dimmed');

        await page.click('#fg-search-clear');
        await sleep(100);
        const cleared = await page.evaluate(() => ({
            value: document.getElementById('fg-search').value,
            matches: document.querySelectorAll('g.fg-match').length,
        }));
        assert(cleared.value === '' && cleared.matches === 0,
               'Search clear button resets highlight');

        // ── Context menu ──
        console.log('\n── Context menu ──');
        await page.evaluate(idx => {
            document.querySelector(`g[data-idx="${idx}"]`)
                .dispatchEvent(new MouseEvent('contextmenu',
                    { bubbles: true, clientX: 300, clientY: 200 }));
        }, zoomable.idx);
        await sleep(100);
        const menuVisible = await page.$eval(
            '#fg-context-menu', el => el.style.display !== 'none');
        assert(menuVisible, 'Right-click opens context menu');
        await page.click('h1');
        await sleep(100);

        // ── Export menu + endpoints ──
        console.log('\n── Export ──');
        await page.click('#export-btn');
        await sleep(100);
        assert(await page.$eval('#export-menu',
                                el => el.classList.contains('visible')),
               'Export menu opens');
        const actions = await page.$$eval('#export-menu .export-item',
                                          els => els.map(e => e.dataset.action));
        assert(['collapsed', 'json', 'svg'].every(a => actions.includes(a)),
               `Export menu has svg/collapsed/json (${actions})`);
        await page.click('h1');

        const svg = await page.evaluate(async (sid) => {
            const r = await fetch(`/api/export/flamegraph?event=cycles&session=${sid}`);
            return { ok: r.ok, ct: r.headers.get('content-type'),
                     text: await r.text() };
        }, sessionId);
        assert(svg.ok && svg.ct.includes('svg')
               && svg.text.startsWith('<svg')
               && svg.text.trim().endsWith('</svg>'),
               'SVG export is a well-formed SVG');

        const collapsed = await page.evaluate(async (sid) => {
            const r = await fetch(`/api/export/session/${sid}?format=collapsed`);
            const text = await r.text();
            const lines = text.trim().split('\n');
            return {
                ok: r.ok,
                lines: lines.length,
                valid: lines.every(l => !isNaN(parseInt(l.split(' ').pop()))),
                disposition: r.headers.get('content-disposition') || '',
            };
        }, sessionId);
        assert(collapsed.ok && collapsed.lines > 0 && collapsed.valid,
               `Collapsed export valid (${collapsed.lines} stacks)`);
        assert(collapsed.disposition.includes('attachment'),
               'Collapsed export is a download');

        const json = await page.evaluate(async (sid) => {
            const r = await fetch(`/api/export/session/${sid}?format=json`);
            const d = await r.json();
            return { ok: r.ok, sid: d.metadata?.session_id,
                     events: Object.keys(d.per_event || {}).length };
        }, sessionId);
        assert(json.ok && json.sid === sessionId && json.events > 0,
               `JSON export has metadata + ${json.events} events`);

        // ── No JS errors across the whole run ──
        console.log('\n── Page health ──');
        assert(pageErrors.length === 0,
               `No JS errors (got ${pageErrors.length}: ${pageErrors.join('; ')})`);
    } catch (err) {
        failed++;
        console.error('FATAL:', err);
        if (serverLog) console.error('--- server log ---\n' + serverLog);
    } finally {
        if (browser) await browser.close();
        server.kill('SIGTERM');
        await new Promise(r => { server.on('exit', r); setTimeout(r, 5000); });
        fs.rmSync(home, { recursive: true, force: true });
    }

    console.log(`\n────────────────────────────`);
    console.log(`Total: ${passed} passed, ${failed} failed`);
    console.log(failed === 0 ? 'ALL TESTS PASSED' : 'SOME TESTS FAILED');
    process.exit(failed > 0 ? 1 : 0);
}

main();
