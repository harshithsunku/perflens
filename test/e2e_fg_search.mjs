#!/usr/bin/env node
/**
 * E2E test: flamegraph search/highlight feature.
 * Requires: server running on :8080 with data loaded.
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
    const errors = [];
    page.on('pageerror', err => errors.push(err.message));
    page.on('console', msg => {
        if (msg.type() === 'error' && !msg.text().includes('404'))
            console.error('CONSOLE:', msg.text());
    });
    return errors;
}

async function loadPageWithData() {
    await page.goto(URL, { waitUntil: 'networkidle2' });
    const sessions = await page.evaluate(async () => {
        const r = await fetch('/api/sessions');
        return r.json();
    });
    const session = sessions.find(s => s.total_samples > 0);
    if (!session) throw new Error('No session with samples');

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
        switchToTab('flamegraph');
        renderCurrentEvent();
    }, session.session_id);

    await page.waitForSelector('#flamegraph-container svg g[data-idx]', { timeout: 5000 });
}

// ── Test 1: Search input exists ──
async function testSearchInputExists() {
    const input = await page.$('#fg-search');
    assert(!!input, 'Search input exists');
    const matches = await page.$('#fg-search-matches');
    assert(!!matches, 'Match count span exists');
    const clear = await page.$('#fg-search-clear');
    assert(!!clear, 'Clear button exists');
    const hidden = await page.$eval('#fg-search-clear', el => el.classList.contains('hidden'));
    assert(hidden, 'Clear button hidden initially');
}

// ── Test 2: Type search → frames get dimmed/highlighted ──
async function testSearchDimsFrames() {
    await page.type('#fg-search', 'process');
    await new Promise(r => setTimeout(r, 350)); // debounce + margin

    const dimCount = await page.evaluate(() =>
        document.querySelectorAll('#flamegraph-container g.fg-dim').length);
    const matchCount = await page.evaluate(() =>
        document.querySelectorAll('#flamegraph-container g.fg-match').length);
    const totalFrames = await page.evaluate(() => flamegraphRects.length);

    assert(matchCount > 0, `Search "process" matched ${matchCount} frames`);
    assert(dimCount > 0, `Search "process" dimmed ${dimCount} frames`);
    assert(matchCount + dimCount === totalFrames, `match(${matchCount}) + dim(${dimCount}) = total(${totalFrames})`);
}

// ── Test 3: Match count text shows ──
async function testMatchCountText() {
    const text = await page.$eval('#fg-search-matches', el => el.textContent);
    assert(text.includes('/'), `Match text has fraction: "${text}"`);
    assert(text.includes('%'), `Match text has percentage: "${text}"`);
    assert(text.includes('frames'), `Match text says frames: "${text}"`);
}

// ── Test 4: Clear button visible during search ──
async function testClearButtonVisible() {
    const hidden = await page.$eval('#fg-search-clear', el => el.classList.contains('hidden'));
    assert(!hidden, 'Clear button visible during active search');
}

// ── Test 5: Case insensitive ──
async function testCaseInsensitive() {
    // Clear and type uppercase
    await page.evaluate(() => { document.getElementById('fg-search').value = ''; });
    await page.type('#fg-search', 'HANDLE_REQUEST');
    await new Promise(r => setTimeout(r, 350));

    const matchCount = await page.evaluate(() =>
        document.querySelectorAll('#flamegraph-container g.fg-match').length);
    assert(matchCount > 0, `Case-insensitive: "HANDLE_REQUEST" matched ${matchCount} frames`);
}

// ── Test 6: Regex support ──
async function testRegexSupport() {
    await page.evaluate(() => { document.getElementById('fg-search').value = ''; });
    await page.type('#fg-search', 'process|send');
    await new Promise(r => setTimeout(r, 350));

    const matchCount = await page.evaluate(() =>
        document.querySelectorAll('#flamegraph-container g.fg-match').length);
    assert(matchCount >= 2, `Regex "process|send" matched ${matchCount} frames (expected >=2)`);
}

// ── Test 7: Invalid regex shows message, doesn't crash ──
async function testInvalidRegex() {
    await page.evaluate(() => { document.getElementById('fg-search').value = ''; });
    await page.type('#fg-search', '[invalid(');
    await new Promise(r => setTimeout(r, 350));

    const text = await page.$eval('#fg-search-matches', el => el.textContent);
    assert(text.includes('invalid regex'), `Invalid regex shows error: "${text}"`);
}

// ── Test 8: Clear button resets everything ──
async function testClearButton() {
    await page.evaluate(() => { document.getElementById('fg-search').value = ''; });
    await page.type('#fg-search', 'main');
    await new Promise(r => setTimeout(r, 350));

    await page.click('#fg-search-clear');
    await new Promise(r => setTimeout(r, 100));

    const dimCount = await page.evaluate(() =>
        document.querySelectorAll('#flamegraph-container g.fg-dim').length);
    const matchCount = await page.evaluate(() =>
        document.querySelectorAll('#flamegraph-container g.fg-match').length);
    const inputVal = await page.$eval('#fg-search', el => el.value);
    const matchText = await page.$eval('#fg-search-matches', el => el.textContent);
    const clearHidden = await page.$eval('#fg-search-clear', el => el.classList.contains('hidden'));

    assert(dimCount === 0, 'No dimmed frames after clear');
    assert(matchCount === 0, 'No matched frames after clear');
    assert(inputVal === '', 'Input cleared');
    assert(matchText === '', 'Match text cleared');
    assert(clearHidden, 'Clear button hidden after clear');
}

// ── Test 9: Search persists after zoom ──
async function testSearchPersistsAfterZoom() {
    await page.type('#fg-search', 'process');
    await new Promise(r => setTimeout(r, 350));

    const matchesBefore = await page.evaluate(() =>
        document.querySelectorAll('#flamegraph-container g.fg-match').length);

    // Double-click a zoomable frame to trigger re-render
    const zoomIdx = await page.evaluate(() => {
        for (let i = 0; i < flamegraphRects.length; i++) {
            const r = flamegraphRects[i];
            if (r.name !== 'root' && r.node?.children?.length > 0) return i;
        }
        return -1;
    });

    if (zoomIdx >= 0) {
        const frame = await page.$(`#flamegraph-container svg g[data-idx="${zoomIdx}"]`);
        await frame.click({ clickCount: 2 });
        await new Promise(r => setTimeout(r, 400));

        const hasMatch = await page.evaluate(() =>
            document.querySelectorAll('#flamegraph-container g.fg-match').length > 0 ||
            document.querySelectorAll('#flamegraph-container g.fg-dim').length > 0);
        assert(hasMatch, 'Search still applied after zoom re-render');
    } else {
        console.log('    SKIP  No zoomable frame found');
    }

    // Reset zoom + clear search
    const resetBtn = await page.$('#flamegraph-reset');
    if (resetBtn) await page.click('#flamegraph-reset');
    await new Promise(r => setTimeout(r, 300));
    await page.click('#fg-search-clear');
    await new Promise(r => setTimeout(r, 100));
}

// ── Test 10: Ctrl+F focuses search ──
async function testCtrlFocuses() {
    // Blur the search input first
    await page.evaluate(() => document.getElementById('fg-search').blur());
    const focusedBefore = await page.evaluate(() =>
        document.activeElement === document.getElementById('fg-search'));
    assert(!focusedBefore, 'Search not focused before Ctrl+F');

    await page.keyboard.down('Control');
    await page.keyboard.press('f');
    await page.keyboard.up('Control');
    await new Promise(r => setTimeout(r, 100));

    const focusedAfter = await page.evaluate(() =>
        document.activeElement === document.getElementById('fg-search'));
    assert(focusedAfter, 'Ctrl+F focused search input');
}

// ── Test 11: fg-dim and fg-match CSS rules exist ──
async function testCSSRulesExist() {
    const rules = await page.evaluate(() => {
        let found = { dim: false, match: false };
        for (const sheet of document.styleSheets) {
            try {
                for (const rule of sheet.cssRules) {
                    if (rule.cssText?.includes('fg-dim')) found.dim = true;
                    if (rule.cssText?.includes('fg-match')) found.match = true;
                }
            } catch (e) {}
        }
        return found;
    });
    assert(rules.dim, 'CSS rule for fg-dim exists');
    assert(rules.match, 'CSS rule for fg-match exists');
}

// ── Test 12: Empty search removes all classes ──
async function testEmptySearchClears() {
    await page.type('#fg-search', 'main');
    await new Promise(r => setTimeout(r, 350));

    // Now clear by selecting all + delete
    await page.evaluate(() => { document.getElementById('fg-search').value = ''; });
    // Trigger input event manually since .value= doesn't fire it
    await page.evaluate(() => {
        document.getElementById('fg-search').dispatchEvent(new Event('input'));
    });
    await new Promise(r => setTimeout(r, 350));

    const dimCount = await page.evaluate(() =>
        document.querySelectorAll('#flamegraph-container g.fg-dim').length);
    assert(dimCount === 0, 'Empty input removes all dimming');
}

// ── Run all ──
async function main() {
    const errors = await setup();
    try {
        await loadPageWithData();
        console.log('\n── Flamegraph Search E2E Tests ──\n');

        await testSearchInputExists();
        await testSearchDimsFrames();
        await testMatchCountText();
        await testClearButtonVisible();
        await testCaseInsensitive();
        await testRegexSupport();
        await testInvalidRegex();
        await testClearButton();
        await testSearchPersistsAfterZoom();
        await testCtrlFocuses();
        await testCSSRulesExist();
        await testEmptySearchClears();

        if (errors.length > 0) {
            console.error('\nJS errors during tests:', errors);
        }

        console.log(`\n────────────────────────────`);
        console.log(`Total: ${passed} passed, ${failed} failed`);
        console.log(failed === 0 ? 'ALL TESTS PASSED' : 'SOME TESTS FAILED');
    } finally {
        await browser.close();
    }
    process.exit(failed > 0 ? 1 : 0);
}

main().catch(err => { console.error('FATAL:', err); process.exit(1); });
