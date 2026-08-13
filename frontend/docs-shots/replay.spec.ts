// Docs screenshots captured from a replayed fixture session.
//
// This project covers the *whole* set, needs no `perf`, no agent and no
// device, and is smoke-run in CI on every PR. That smoke run is the point:
// the puppeteer scripts it replaces rotted for two months because nothing
// ever executed them, and `typeof` guards on deleted globals meant they
// reported success while capturing the wrong page. Broad coverage here is
// what makes the next UI change fail loudly instead of silently.
//
// live.spec.ts re-shoots the data-heavy subset afterwards and overwrites
// these. The fixture has no locally resolvable binary, so replay renders
// the hot path as `[unknown]` at 73% -- true, but a poor advertisement for
// a tool whose job is resolving symbols. Run order is therefore:
//
//     npm run shots        # this project — full set, deterministic
//     npm run shots:live   # overwrites 01,02,03,04,06,08,12 with real data
//
// The hero is the exception that stays here: it is the one shot whose
// subject is the counter bar, and the fixture was captured on ordinary
// hardware with plain event names (cycles, instructions). The dev box is
// hybrid-core, so a live hero would advertise `CPU_ATOM/CY...` labels that
// no typical user sees.

import { expect, test } from '@playwright/test';
import {
  collapseMetrics, flamegraphReady, focusContent, functionsReady, openFixture,
  snap, useLightTheme,
} from './helpers';

test.describe('docs screenshots (fixture replay)', () => {
  test('01 function table', async ({ page }) => {
    await openFixture(page, 'functions');
    await functionsReady(page);
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '01-functions.png');
  });

  test('02 flame graph', async ({ page }) => {
    await openFixture(page, 'flamegraph');
    await flamegraphReady(page);
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '02-flamegraph.png');
  });

  test('05 saved sessions', async ({ page }) => {
    await openFixture(page, 'sessions');
    await expect(page.getByTestId('sessions-list')).toBeVisible();
    await expect(page.locator('#sessions-table tbody tr').first()).toBeVisible();
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '05-sessions.png');
  });

  test('06 function table, light theme', async ({ page }) => {
    await useLightTheme(page);
    await openFixture(page, 'functions');
    await functionsReady(page);
    // Gate on the store having flipped, not just the CSS: the old script
    // mutated data-theme directly, which left zustand on 'dark' and the
    // flame-graph SVG rendering dark colours on a light page.
    await expect(page.locator('#theme-label')).toHaveText(/light/i);
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '06-functions-light.png');
  });

  test('08 flame graph search', async ({ page }) => {
    await openFixture(page, 'flamegraph');
    await flamegraphReady(page);
    const names = await page.evaluate(
      () => (window as unknown as { __perflens: { rects: { name: string }[] } })
        .__perflens.rects.map((r) => r.name));
    const stem = names.find((n) => n.length > 4 && !n.startsWith('['))?.slice(0, 4);
    await page.locator('#fg-search').fill(stem ?? 'main');
    await expect(page.locator('#fg-search-matches')).toContainText(/match|frame/i);
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '08-flamegraph-search.png');
  });

  test('09 keyboard shortcuts overlay', async ({ page }) => {
    await openFixture(page, 'functions');
    await functionsReady(page);
    await collapseMetrics(page);
    await focusContent(page);
    await page.keyboard.press('?');
    await expect(page.getByTestId('shortcuts-help')).toBeVisible();
    await snap(page, '09-shortcuts.png');
  });

  test('10 export menu', async ({ page }) => {
    await openFixture(page, 'functions');
    await functionsReady(page);
    await page.locator('#export-btn').click();
    await expect(page.locator('#export-menu')).toHaveClass(/visible/);
    await snap(page, '10-export-menu.png');
  });

  test('11 docs drawer', async ({ page }) => {
    await openFixture(page, 'functions');
    await functionsReady(page);
    await collapseMetrics(page);
    await focusContent(page);
    await page.locator('#docs-btn').click();
    await expect(page.locator('#docs-drawer')).toHaveClass(/visible/);
    // This is why the version bump had to precede the screenshots.
    await expect(page.locator('#docs-version')).toHaveText(/^v\d+\.\d+\.\d+$/);
    await snap(page, '11-docs-drawer.png');
  });

  test('12 flame graph zoomed with breadcrumbs', async ({ page }) => {
    await openFixture(page, 'flamegraph');
    await flamegraphReady(page);
    await page.locator('#flamegraph-container svg g[data-idx]').nth(3).click();
    await expect(page.locator('#flamegraph-reset')).toBeVisible();
    await collapseMetrics(page);
    await focusContent(page);
    await snap(page, '12-flamegraph-zoom.png');
  });
});

// The og:image is the only 2x shot -- docs/index.html pins og:image:width
// and og:image:height at 2880x1800. Everything else renders in a grid where
// 1x is indistinguishable and 2x is permanent weight in the repo.
test.describe('hero / og:image', () => {
  test.use({ deviceScaleFactor: 2 });

  test('07 overview', async ({ page }) => {
    await openFixture(page, 'functions');
    await functionsReady(page);
    // The hero's subject is the counter bar and the health strip, so this
    // shot deliberately does not collapse anything.
    await expect(page.locator('#perf-stat-bar .stat-card').nth(5)).toBeVisible();
    await expect(page.locator('#metrics-strip')).toBeVisible();
    await page.evaluate(() => window.scrollTo(0, 0));
    await snap(page, '07-overview.png', true);
  });
});
