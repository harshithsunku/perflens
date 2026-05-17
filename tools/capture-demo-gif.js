// tools/capture-demo-gif.js
//
// Capture frames for the live-demo GIF embedded in the README and docs site.
// Same setup as capture-screenshots.js (running server + agent + workload).
//
// Run:
//   node tools/capture-demo-gif.js
//   tools/encode-demo-gif.sh        # encode frames to docs/demo.gif
//
// Output: /tmp/perflens-gif-frames/f000.png ... f032.png

const path = require('path');
const fs = require('fs');
const puppeteer = require(path.join(__dirname, '..', 'node_modules', 'puppeteer'));

const BASE = process.env.PERFLENS_URL || 'http://localhost:8089';
const FRAMES_DIR = process.env.FRAMES_DIR || '/tmp/perflens-gif-frames';

fs.rmSync(FRAMES_DIR, { recursive: true, force: true });
fs.mkdirSync(FRAMES_DIR, { recursive: true });

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
    const browser = await puppeteer.launch({
        headless: 'new',
        args: ['--no-sandbox', '--disable-setuid-sandbox'],
        defaultViewport: { width: 1280, height: 720, deviceScaleFactor: 1 },
    });
    const page = await browser.newPage();

    console.log('navigate');
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
    await sleep(1500);

    await page.evaluate(() => {
        if (typeof showView === 'function') showView('view-profiling');
        if (typeof switchToTab === 'function') switchToTab('functions');
        const sel = document.getElementById('event-select');
        if (sel) for (const opt of sel.options) {
            if (opt.value.includes('cycles') && opt.value.includes('cpu_core')) {
                sel.value = opt.value;
                sel.dispatchEvent(new Event('change'));
                break;
            }
        }
    });
    await sleep(1200);
    await page.evaluate(() => {
        const el = document.querySelector('#tabs');
        if (el) el.scrollIntoView({ behavior: 'instant', block: 'start' });
    });
    await sleep(600);

    let frame = 0;
    const snap = async () => {
        const p = path.join(FRAMES_DIR, `f${String(frame).padStart(3, '0')}.png`);
        await page.screenshot({ path: p });
        frame++;
    };

    console.log('phase 1: functions table updating');
    for (let i = 0; i < 14; i++) { await snap(); await sleep(450); }

    console.log('phase 2: flamegraph');
    await page.evaluate(() => {
        const b = document.querySelector('button.tab[data-tab="flamegraph"]');
        if (b) b.click();
    });
    await sleep(300);
    await page.evaluate(() => {
        if (typeof renderCurrentEvent === 'function') renderCurrentEvent();
    });
    await sleep(700);
    for (let i = 0; i < 10; i++) { await snap(); await sleep(450); }

    console.log('phase 3: source view');
    await page.evaluate(() => {
        const b = document.querySelector('button.tab[data-tab="functions"]');
        if (b) b.click();
    });
    await sleep(400);
    await page.evaluate(() => {
        const row = document.querySelector('#function-table tbody tr');
        if (row) row.click();
    });
    await sleep(1200);
    await page.evaluate(() => {
        const el = document.querySelector('#tabs');
        if (el) el.scrollIntoView({ behavior: 'instant', block: 'start' });
    });
    for (let i = 0; i < 8; i++) { await snap(); await sleep(450); }

    await browser.close();
    console.log('frames:', frame, 'in', FRAMES_DIR);
})().catch(e => { console.error(e); process.exit(1); });
