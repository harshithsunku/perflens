// Frames for docs/demo.gif, captured against a live session.
//
// The premise of the GIF is that sample counts climb as perf rounds stream
// in, so it cannot come from a replay -- a replayed session is a fixed
// dataset and every frame would be identical.
//
// Encoded afterwards by tools/encode-demo-gif.sh (ffmpeg, 2-pass palette,
// FPS=4, WIDTH=900), which is unchanged from the puppeteer era: 1280x720
// at 1x scales to the 900px-wide GIF without resampling artifacts.

import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import {
  collapseMetrics, flamegraphReady, focusContent, functionsReady, openHash,
} from './helpers';

const FRAMES_DIR = process.env.FRAMES_DIR || '/tmp/perflens-gif-frames';
// perf record flushes its ring buffer in batches, so chunks land every
// 2-4s regardless of the configured duration. Frames closer together than
// that just duplicate each other -- 450ms (the puppeteer-era interval)
// yielded 5 distinct images out of 32. Sample at ~900ms so consecutive
// frames straddle chunk arrivals and the counters visibly move.
const FRAME_MS = 900;

test('demo gif frames', async ({ page }) => {
  test.setTimeout(120_000);
  mkdirSync(FRAMES_DIR, { recursive: true });

  let frame = 0;
  const snapFrame = async () => {
    const started = Date.now();
    await page.screenshot({
      path: join(FRAMES_DIR, `f${String(frame++).padStart(3, '0')}.png`),
      animations: 'disabled',
    });
    // Deadline, not delay: a screenshot costs 80-150ms, so sleeping the
    // full interval afterwards would stretch the real cadence well past
    // FRAME_MS and play back slower than the encoder's FPS assumes.
    const remaining = FRAME_MS - (Date.now() - started);
    if (remaining > 0) await page.waitForTimeout(remaining);
  };

  // Phase 1 — the function table, with counts climbing between frames.
  await openHash(page, 'tab=functions');
  await functionsReady(page);
  await collapseMetrics(page);
  await focusContent(page);
  await page.addStyleTag({
    content: '*,*::before,*::after{transition:none!important;animation:none!important}',
  });
  for (let i = 0; i < 9; i++) await snapFrame();

  // Phase 2 — flip to the flame graph.
  await page.locator('.tab[data-tab="flamegraph"]').click();
  await flamegraphReady(page);
  await collapseMetrics(page);
  await focusContent(page);
  for (let i = 0; i < 6; i++) await snapFrame();

  // Phase 3 — click the hottest function through to annotated source.
  await page.locator('.tab[data-tab="functions"]').click();
  await functionsReady(page);
  await page.locator('#function-table tbody tr[data-func]').first().click();
  await expect(page.locator('#source-view .source-header'))
    .toBeVisible({ timeout: 20_000 });
  await collapseMetrics(page);
  await focusContent(page);
  for (let i = 0; i < 6; i++) await snapFrame();

  expect(frame).toBe(21);
});
