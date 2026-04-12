#!/usr/bin/env node
/**
 * E2E test: export dropdown and endpoints.
 */
import puppeteer from 'puppeteer';

const URL = 'http://localhost:8080';
let browser, page;
let passed = 0, failed = 0;

function assert(cond, msg) {
    if (cond) { passed++; console.log(`  PASS  ${msg}`); }
    else      { failed++; console.error(`  FAIL  ${msg}`); }
}

async function setup() {
    browser = await puppeteer.launch({
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'],
    });
    page = await browser.newPage();
    page.on('pageerror', err => console.error('PAGE_ERR:', err.message));
}

async function loadPage() {
    await page.goto(URL, { waitUntil: 'networkidle2' });
    // Load a session with data
    const sessions = await page.evaluate(async () => {
        const r = await fetch('/api/sessions');
        return r.json();
    });
    const session = sessions.find(s => s.total_samples > 0);
    if (session) {
        await page.evaluate(async (sid) => {
            const r = await fetch('/api/sessions/' + sid);
            const data = await r.json();
            state.perEvent = data.per_event;
            state.eventTypes = data.metadata.event_types || [];
            state.selectedEvent = state.eventTypes[0] || 'cycles';
            state.totalSamples = data.metadata.total_samples;
            state.lastUpdateTime = Date.now();
            updateEventSelector();
            updateStatBar();
            renderCurrentEvent();
        }, session.session_id);
    }
}

// ── Test 1: Export button exists ──
async function testExportButtonExists() {
    const btn = await page.$('#export-btn');
    assert(!!btn, 'Export button exists');
    const menu = await page.$('#export-menu');
    assert(!!menu, 'Export menu exists');
    const visible = await page.$eval('#export-menu', el => el.classList.contains('visible'));
    assert(!visible, 'Export menu hidden by default');
}

// ── Test 2: Click export button shows menu ──
async function testExportMenuToggle() {
    await page.click('#export-btn');
    await new Promise(r => setTimeout(r, 100));
    const visible = await page.$eval('#export-menu', el => el.classList.contains('visible'));
    assert(visible, 'Menu visible after click');

    // Click elsewhere to dismiss
    await page.click('h1');
    await new Promise(r => setTimeout(r, 100));
    const hidden = await page.$eval('#export-menu', el => !el.classList.contains('visible'));
    assert(hidden, 'Menu hidden after click elsewhere');
}

// ── Test 3: Menu has 3 items with correct actions ──
async function testMenuItems() {
    const items = await page.evaluate(() => {
        return Array.from(document.querySelectorAll('#export-menu .export-item'))
            .map(el => ({ action: el.dataset.action, text: el.textContent }));
    });
    assert(items.length === 3, `Menu has 3 items (got ${items.length})`);
    assert(items.some(i => i.action === 'svg'), 'Has SVG action');
    assert(items.some(i => i.action === 'collapsed'), 'Has collapsed action');
    assert(items.some(i => i.action === 'json'), 'Has JSON action');
}

// ── Test 4: SVG endpoint returns valid SVG ──
async function testSvgEndpoint() {
    const result = await page.evaluate(async () => {
        const r = await fetch('/api/export/flamegraph?event=cycles');
        const text = await r.text();
        return {
            ok: r.ok,
            contentType: r.headers.get('content-type'),
            hasSvg: text.includes('<svg'),
            hasTitle: text.includes('PerfLens Flamegraph'),
            hasStyle: text.includes('<style>'),
            endsCorrectly: text.trim().endsWith('</svg>'),
            size: text.length,
        };
    });
    assert(result.ok, 'SVG endpoint returns 200');
    assert(result.contentType.includes('svg'), `Content-Type is SVG: ${result.contentType}`);
    assert(result.hasSvg, 'Response contains <svg');
    assert(result.hasTitle, 'SVG has PerfLens title');
    assert(result.hasStyle, 'SVG has embedded style');
    assert(result.endsCorrectly, 'SVG ends with </svg>');
}

// ── Test 5: Collapsed endpoint returns valid format ──
async function testCollapsedEndpoint() {
    const result = await page.evaluate(async () => {
        const r = await fetch('/api/export/session/live?format=collapsed');
        const text = await r.text();
        const lines = text.trim().split('\n');
        let valid = true;
        let total = 0;
        for (const line of lines) {
            const parts = line.split(' ');
            const count = parseInt(parts[parts.length - 1]);
            if (isNaN(count)) { valid = false; break; }
            total += count;
        }
        return {
            ok: r.ok,
            contentType: r.headers.get('content-type'),
            lines: lines.length,
            total,
            valid,
            hasDisposition: (r.headers.get('content-disposition') || '').includes('collapsed'),
        };
    });
    assert(result.ok, 'Collapsed endpoint returns 200');
    assert(result.contentType.includes('text/plain'), `Content-Type: ${result.contentType}`);
    assert(result.lines > 0, `Has ${result.lines} stack lines`);
    assert(result.valid, 'All lines match collapsed format');
    assert(result.total > 0, `Total samples: ${result.total}`);
    assert(result.hasDisposition, 'Has .collapsed in Content-Disposition');
}

// ── Test 6: JSON endpoint returns valid data ──
async function testJsonEndpoint() {
    const result = await page.evaluate(async () => {
        const r = await fetch('/api/export/session/live?format=json');
        const data = await r.json();
        return {
            ok: r.ok,
            hasMetadata: !!data.metadata,
            hasPerEvent: !!data.per_event,
            sessionId: data.metadata?.session_id,
            totalSamples: data.metadata?.total_samples,
            events: Object.keys(data.per_event || {}),
            hasDisposition: (r.headers.get('content-disposition') || '').includes('.json'),
        };
    });
    assert(result.ok, 'JSON endpoint returns 200');
    assert(result.hasMetadata, 'Has metadata');
    assert(result.hasPerEvent, 'Has per_event');
    assert(result.totalSamples > 0, `Total samples: ${result.totalSamples}`);
    assert(result.events.length > 0, `Events: ${result.events}`);
    assert(result.hasDisposition, 'Has .json in Content-Disposition');
}

// ── Test 7: Export uses selected event ──
async function testExportUsesSelectedEvent() {
    // Default event should be cycles
    const event = await page.evaluate(() => state.selectedEvent);
    const result = await page.evaluate(async (evt) => {
        const r = await fetch('/api/export/flamegraph?event=' + evt);
        const text = await r.text();
        return text.includes(evt);
    }, event);
    assert(result, `SVG export includes selected event "${event}"`);
}

// ── Run ──
async function main() {
    await setup();
    try {
        await loadPage();
        console.log('\n── Export E2E Tests ──\n');
        await testExportButtonExists();
        await testExportMenuToggle();
        await testMenuItems();
        await testSvgEndpoint();
        await testCollapsedEndpoint();
        await testJsonEndpoint();
        await testExportUsesSelectedEvent();

        console.log(`\n────────────────────────────`);
        console.log(`Total: ${passed} passed, ${failed} failed`);
        console.log(failed === 0 ? 'ALL TESTS PASSED' : 'SOME TESTS FAILED');
    } finally {
        await browser.close();
    }
    process.exit(failed > 0 ? 1 : 0);
}

main().catch(err => { console.error('FATAL:', err); process.exit(1); });
