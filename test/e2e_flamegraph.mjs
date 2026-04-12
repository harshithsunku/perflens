#!/usr/bin/env node
/**
 * E2E test: flamegraph click behaviors + context menu + source flash.
 * Requires: server running on :8080 with data already loaded.
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
    page.on('pageerror', err => console.error('PAGE ERROR:', err.message));
    page.on('console', msg => {
        if (msg.type() === 'error') console.error('CONSOLE:', msg.text());
    });
}

async function loadPageWithData() {
    await page.goto(URL, { waitUntil: 'networkidle2' });

    // Find session with samples and replay it
    const sessions = await page.evaluate(async () => {
        const r = await fetch('/api/sessions');
        return r.json();
    });
    const session = sessions.find(s => s.total_samples > 0);
    if (!session) throw new Error('No session with samples found');

    await page.evaluate(async (sid) => {
        const r = await fetch('/api/sessions/' + sid);
        const data = await r.json();
        state.perEvent = data.per_event;
        state.eventTypes = data.metadata.event_types || [];
        state.perfStat = data.metadata.perf_stat || {};
        state.selectedEvent = state.eventTypes[0] || 'cycles';
        state.totalSamples = data.metadata.total_samples;
        state.flamegraphZoom = null;
        state.lastUpdateTime = Date.now();
        updateEventSelector();
        updateStatBar();
        // Switch to flamegraph tab first so container has width for SVG layout
        switchToTab('flamegraph');
        renderCurrentEvent();
    }, session.session_id);

    // Wait for flamegraph SVG to render
    await page.waitForSelector('#flamegraph-container svg g[data-idx]', { timeout: 5000 });
}

// ── Test 1: No JS errors on page load ──
async function testNoErrors() {
    const errors = [];
    page.on('pageerror', err => errors.push(err.message));
    await page.reload({ waitUntil: 'networkidle2' });
    await loadPageWithData();
    assert(errors.length === 0, `No JS errors on load (got ${errors.length}: ${errors.join('; ')})`);
}

// ── Test 2: Flamegraph renders with clickable frames ──
async function testFlamegraphRenders() {
    const frameCount = await page.$$eval('#flamegraph-container svg g[data-idx]', gs => gs.length);
    assert(frameCount > 3, `Flamegraph has ${frameCount} frames (expected >3)`);
}

// ── Test 3: Context menu element exists (created at init) ──
async function testContextMenuExists() {
    const exists = await page.$eval('#fg-context-menu', el => !!el);
    assert(exists, 'Context menu DOM element exists');

    const visible = await page.$eval('#fg-context-menu', el => el.classList.contains('visible'));
    assert(!visible, 'Context menu hidden by default');

    const items = await page.$$eval('#fg-context-menu .fg-ctx-item', items => items.map(i => i.dataset.action));
    assert(items.length === 3, `Context menu has 3 items (got ${items.length})`);
    assert(items.includes('source'), 'Has "source" action');
    assert(items.includes('zoom'), 'Has "zoom" action');
    assert(items.includes('copy'), 'Has "copy" action');
}

// ── Test 4: Single click on flamegraph frame → switches to Source tab ──
async function testSingleClickSource() {
    // Click on a non-root frame
    const frame = await page.$('#flamegraph-container svg g[data-idx="1"]');
    assert(!!frame, 'Found flamegraph frame [data-idx=1]');

    // Get frame name
    const frameName = await page.evaluate(() => flamegraphRects[1]?.name);
    console.log(`    Clicking frame: "${frameName}"`);

    await frame.click();
    // Wait for 250ms click timer + tab switch
    await new Promise(r => setTimeout(r, 400));

    const activeTab = await page.$eval('.tab.active', el => el.dataset.tab);
    assert(activeTab === 'source', `Single click switched to Source tab (got: ${activeTab})`);
}

// ── Test 5: Double click on flamegraph frame → zooms (stays on flamegraph tab) ──
async function testDoubleClickZoom() {
    // Go back to flamegraph tab
    await page.click('.tab[data-tab="flamegraph"]');
    await new Promise(r => setTimeout(r, 100));

    // Find a frame with children (non-leaf)
    const zoomableIdx = await page.evaluate(() => {
        for (let i = 0; i < flamegraphRects.length; i++) {
            const r = flamegraphRects[i];
            if (r.name !== 'root' && r.node && r.node.children && r.node.children.length > 0) return i;
        }
        return -1;
    });
    assert(zoomableIdx >= 0, `Found zoomable frame at idx ${zoomableIdx}`);

    const zoomName = await page.evaluate((idx) => flamegraphRects[idx]?.name, zoomableIdx);
    console.log(`    Double-clicking frame: "${zoomName}"`);

    const frame = await page.$(`#flamegraph-container svg g[data-idx="${zoomableIdx}"]`);
    await frame.click({ clickCount: 2 });
    await new Promise(r => setTimeout(r, 300));

    // Should still be on flamegraph tab
    const activeTab = await page.$eval('.tab.active', el => el.dataset.tab);
    assert(activeTab === 'flamegraph', `Double click stayed on Flamegraph tab (got: ${activeTab})`);

    // Reset button should appear
    const hasReset = await page.$('#flamegraph-reset');
    assert(!!hasReset, 'Zoom reset button appeared');

    // Zoom state should be set
    const zoomState = await page.evaluate(() => state.flamegraphZoom?.name);
    assert(zoomState === zoomName, `Zoom state set to "${zoomState}" (expected "${zoomName}")`);

    // Reset zoom
    if (hasReset) await page.click('#flamegraph-reset');
    await new Promise(r => setTimeout(r, 200));
}

// ── Test 6: Right-click shows context menu ──
async function testRightClickContextMenu() {
    const frame = await page.$('#flamegraph-container svg g[data-idx="2"]');
    assert(!!frame, 'Found frame for right-click test');

    const frameName = await page.evaluate(() => flamegraphRects[2]?.name);
    console.log(`    Right-clicking frame: "${frameName}"`);

    await frame.click({ button: 'right' });
    await new Promise(r => setTimeout(r, 100));

    const visible = await page.$eval('#fg-context-menu', el => el.classList.contains('visible'));
    assert(visible, 'Context menu visible after right-click');

    const storedFunc = await page.$eval('#fg-context-menu', el => el.dataset.func);
    assert(storedFunc === frameName, `Context menu stores func "${storedFunc}" (expected "${frameName}")`);

    // Dismiss with click elsewhere
    await page.click('body', { offset: { x: 10, y: 10 } });
    await new Promise(r => setTimeout(r, 100));

    const hiddenAfter = await page.$eval('#fg-context-menu', el => !el.classList.contains('visible'));
    assert(hiddenAfter, 'Context menu hidden after clicking elsewhere');
}

// ── Test 7: Context menu "View source" action ──
async function testContextMenuViewSource() {
    // Go back to flamegraph tab
    await page.click('.tab[data-tab="flamegraph"]');
    await new Promise(r => setTimeout(r, 100));

    const frame = await page.$('#flamegraph-container svg g[data-idx="3"]');
    await frame.click({ button: 'right' });
    await new Promise(r => setTimeout(r, 100));

    // Click "View source"
    await page.click('#fg-context-menu .fg-ctx-item[data-action="source"]');
    await new Promise(r => setTimeout(r, 300));

    const activeTab = await page.$eval('.tab.active', el => el.dataset.tab);
    assert(activeTab === 'source', `"View source" switched to Source tab (got: ${activeTab})`);
}

// ── Test 8: Context menu "Copy function name" ──
async function testContextMenuCopy() {
    await page.click('.tab[data-tab="flamegraph"]');
    await new Promise(r => setTimeout(r, 100));

    const frame = await page.$('#flamegraph-container svg g[data-idx="2"]');
    await frame.click({ button: 'right' });
    await new Promise(r => setTimeout(r, 100));

    // Click "Copy function name" — won't actually copy in headless but shouldn't throw
    const noError = await page.evaluate(() => {
        const item = document.querySelector('#fg-context-menu .fg-ctx-item[data-action="copy"]');
        try { item.click(); return true; }
        catch (e) { return false; }
    });
    assert(noError, 'Copy function name did not throw');
}

// ── Test 9: Root node click does nothing ──
async function testRootNodeSkipped() {
    await page.click('.tab[data-tab="flamegraph"]');
    await new Promise(r => setTimeout(r, 100));

    // Find root frame
    const rootIdx = await page.evaluate(() => {
        for (let i = 0; i < flamegraphRects.length; i++) {
            if (flamegraphRects[i].name === 'root') return i;
        }
        return -1;
    });

    if (rootIdx >= 0) {
        const frame = await page.$(`#flamegraph-container svg g[data-idx="${rootIdx}"]`);
        await frame.click();
        await new Promise(r => setTimeout(r, 400));

        const activeTab = await page.$eval('.tab.active', el => el.dataset.tab);
        assert(activeTab === 'flamegraph', `Root click stayed on flamegraph tab (got: ${activeTab})`);
    } else {
        console.log('    SKIP  Root node not rendered (too small)');
    }
}

// ── Test 10: Function table renders self/total columns ──
async function testFunctionTableColumns() {
    await page.click('.tab[data-tab="functions"]');
    await new Promise(r => setTimeout(r, 100));

    const headers = await page.$$eval('#function-table th', ths => ths.map(t => t.textContent.trim()));
    assert(headers.includes('Self %'), `Has "Self %" column header`);
    assert(headers.includes('Total %'), `Has "Total %" column header`);

    // Check rows exist
    const rowCount = await page.$$eval('#function-tbody tr', rows => rows.length);
    assert(rowCount > 0, `Function table has ${rowCount} rows`);

    // Check a specific function: main should have 0% self
    const mainRow = await page.evaluate(() => {
        const rows = document.querySelectorAll('#function-tbody tr');
        for (const row of rows) {
            if (row.dataset.func === 'main') {
                const bars = row.querySelectorAll('.cpu-bar-text');
                return { self: bars[0]?.textContent, total: bars[1]?.textContent };
            }
        }
        return null;
    });
    assert(mainRow !== null, 'Found main function row');
    if (mainRow) {
        assert(mainRow.self === '0.0%', `main self = ${mainRow.self} (expected 0.0%)`);
        assert(parseFloat(mainRow.total) > 0, `main total = ${mainRow.total} (expected >0%)`);
    }
}

// ── Test 11: Sort toggle works ──
async function testSortToggle() {
    // Click "Total %" header
    await page.click('#function-table th.sortable[data-sort="total"]');
    await new Promise(r => setTimeout(r, 200));

    const activeSort = await page.$eval('#function-table th.sortable.active', el => el.dataset.sort);
    assert(activeSort === 'total', `Sort switched to total (got: ${activeSort})`);

    // First row should be main or handle_request (highest total %)
    const firstFunc = await page.$eval('#function-tbody tr:first-child', r => r.dataset.func);
    const highTotal = ['main', 'handle_request'];
    assert(highTotal.includes(firstFunc), `Top by total is "${firstFunc}" (expected main or handle_request)`);
}

// ── Test 12: Source flash CSS class exists ──
async function testSourceFlashCSS() {
    const hasAnimation = await page.evaluate(() => {
        const sheets = document.styleSheets;
        for (const sheet of sheets) {
            try {
                for (const rule of sheet.cssRules) {
                    if (rule.cssText && rule.cssText.includes('source-flash')) return true;
                }
            } catch (e) {}
        }
        return false;
    });
    assert(hasAnimation, 'source-flash CSS animation rule exists');
}

// ── Test 13: Escape dismisses context menu ──
async function testEscapeDismiss() {
    await page.click('.tab[data-tab="flamegraph"]');
    await new Promise(r => setTimeout(r, 100));

    const frame = await page.$('#flamegraph-container svg g[data-idx="1"]');
    await frame.click({ button: 'right' });
    await new Promise(r => setTimeout(r, 100));

    let visible = await page.$eval('#fg-context-menu', el => el.classList.contains('visible'));
    assert(visible, 'Context menu visible before Escape');

    await page.keyboard.press('Escape');
    await new Promise(r => setTimeout(r, 100));

    visible = await page.$eval('#fg-context-menu', el => el.classList.contains('visible'));
    assert(!visible, 'Context menu hidden after Escape');
}

// ── Run all ──
async function main() {
    await setup();
    try {
        await loadPageWithData();
        console.log('\n── Flamegraph E2E Tests ──\n');

        await testNoErrors();
        await testFlamegraphRenders();
        await testContextMenuExists();
        await testSingleClickSource();
        await testDoubleClickZoom();
        await testRightClickContextMenu();
        await testContextMenuViewSource();
        await testContextMenuCopy();
        await testRootNodeSkipped();
        await testFunctionTableColumns();
        await testSortToggle();
        await testSourceFlashCSS();
        await testEscapeDismiss();

        console.log(`\n────────────────────────────`);
        console.log(`Total: ${passed} passed, ${failed} failed`);
        console.log(failed === 0 ? 'ALL TESTS PASSED' : 'SOME TESTS FAILED');
    } finally {
        await browser.close();
    }
    process.exit(failed > 0 ? 1 : 0);
}

main().catch(err => {
    console.error('FATAL:', err);
    process.exit(1);
});
