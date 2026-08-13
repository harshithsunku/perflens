// Docs screenshots that require live server state.
//
// Run against a server started by tools/live-capture.sh:
//
//     tools/live-capture.sh &
//     PERFLENS_EXTERNAL_SERVER=1 PERFLENS_BASE_URL=http://127.0.0.1:8089 \
//       npm --prefix frontend run shots:live
//
// Each of these is here for a structural reason, not convenience:
//   - threads   /api/threads reads live all_samples; replay returns empty
//   - source    needs a locally built -g binary; fixture addresses don't
//               resolve on this machine
//   - timeline  sparkline drag is disabled in replay mode
//   - diff      a session diffed against itself is all zeros
// The function table and flame graph are also shot here: the fixture has no
// resolvable binary, so replay renders them as [unknown] at 73% -- true, but
// a poor advertisement for a profiler whose job is resolving symbols.

import { expect, test } from '@playwright/test';
import {
  collapseMetrics, flamegraphReady, focusContent, functionsReady, openHash, snap,
  useCyclesEvent, useLightTheme,
} from './helpers';

test.describe('docs screenshots (live capture)', () => {
  test('01 function table', async ({ page }) => {
    await openHash(page, 'tab=functions');
    await useCyclesEvent(page);
    await functionsReady(page);
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '01-functions.png');
  });

  test('02 flame graph', async ({ page }) => {
    await openHash(page, 'tab=flamegraph');
    await useCyclesEvent(page);
    await flamegraphReady(page);
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '02-flamegraph.png');
  });

  test('03 annotated source', async ({ page }) => {
    await openHash(page, 'tab=functions');
    await useCyclesEvent(page);
    await functionsReady(page);
    // Click through from the hottest function the way a user would. The
    // whole row is the click target -- the classes the old puppeteer script
    // looked for (.fn-source-link, .src-link) never existed in the React app.
    await page.locator('#function-table tbody tr[data-func]').first().click();
    await expect(page.locator('.tab[data-tab="source"]')).toHaveClass(/active/);
    await expect(page.locator('#source-view .source-header')).toBeVisible({ timeout: 20_000 });
    await expect(page.locator('#source-view .line-code').first()).toBeVisible();
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '03-source.png');
  });

  test('04 per-thread breakdown', async ({ page }) => {
    await openHash(page, 'tab=threads');
    await useCyclesEvent(page);
    const rows = page.locator('.thread-row[data-tid]');
    await expect(rows.first()).toBeVisible({ timeout: 20_000 });
    expect(await rows.count()).toBeGreaterThan(1);
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '04-threads.png');
  });

  test('06 function table, light theme', async ({ page }) => {
    await useLightTheme(page);
    await openHash(page, 'tab=functions');
    await useCyclesEvent(page);
    await functionsReady(page);
    await expect(page.locator('#theme-label')).toHaveText(/light/i);
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '06-functions-light.png');
  });

  test('08 flame graph search', async ({ page }) => {
    await openHash(page, 'tab=flamegraph');
    await useCyclesEvent(page);
    await flamegraphReady(page);
    const names = await page.evaluate(
      () => (window as unknown as { __perflens: { rects: { name: string }[] } })
        .__perflens.rects.map((r) => r.name));
    // Pick a stem that actually matches several frames in this fixture.
    const stem = names.find((n) => n.length > 4 && !n.startsWith('['))?.slice(0, 4);
    await page.locator('#fg-search').fill(stem ?? 'main');
    await expect(page.locator('#fg-search-matches')).toContainText(/match|frame/i);
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '08-flamegraph-search.png');
  });

  test('12 flame graph zoomed with breadcrumbs', async ({ page }) => {
    await openHash(page, 'tab=flamegraph');
    await useCyclesEvent(page);
    await flamegraphReady(page);
    await page.locator('#flamegraph-container svg g[data-idx]').nth(3).click();
    await expect(page.locator('#flamegraph-reset')).toBeVisible();
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '12-flamegraph-zoom.png');
  });
});

// The og:image, shot last so it overwrites the replay project's version.
// It has to come from a live session: the replay hero renders a red "Agent
// disconnected" badge and a "Source view unavailable" warning bar, which is
// accurate for a replay but a poor first impression in every link preview.
// 2x because docs/index.html pins og:image:width/height at 2880x1800.
test.describe('hero / og:image', () => {
  test.use({ deviceScaleFactor: 2 });

  test('07 overview', async ({ page }) => {
    await openHash(page, 'tab=functions');
    await useCyclesEvent(page);
    await functionsReady(page);
    // The hero's subject is the counter bar and the health strip, so this
    // shot deliberately collapses nothing.
    await expect(page.locator('#metrics-strip')).toBeVisible();
    await expect(page.locator('#perf-stat-bar .stat-card').nth(5)).toBeVisible();
    await page.evaluate(() => window.scrollTo(0, 0));
    await snap(page, '07-overview.png', true);
  });
});

