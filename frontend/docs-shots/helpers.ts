// Shared machinery for the docs screenshot projects.
//
// These replace a pair of puppeteer scripts that drove the pre-React DOM.
// Three of their bugs are worth not repeating, and each has a helper here:
//
//   1. They called page-global functions (showView, switchToTab) behind
//      `typeof x === 'function'` guards. The functions were deleted in the
//      React rewrite, so the guards turned every call into a silent no-op
//      and the scripts captured the landing page while reporting success.
//      Fix: navigate by URL hash, and *assert* the view arrived.
//   2. They set data-theme on documentElement directly, which leaves the
//      zustand store on 'dark' -- so the SVG colors, read through
//      themeColor(), stayed dark on a light page. Fix: seed localStorage
//      before load, which is the same path the app reads on boot.
//   3. They slept fixed intervals instead of waiting for content.
//      Fix: gate on real conditions.

import { expect, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';

export const FIXTURE = 'session-x86-baseline';

export const OUT_DIR = process.env.OUT_DIR
  || join(dirname(new URL(import.meta.url).pathname), '..', '..', 'docs', 'screenshots');

export interface PerflensHook {
  rects: { name: string; value: number; depth: number }[];
  zoomNames: string[];
}

/**
 * Seed the persisted theme before the app boots. useUi reads this key on
 * initialization, so the store and the CSS agree from the first paint --
 * no dark flash, and no SVG rendered with the wrong palette.
 */
export async function useLightTheme(page: Page) {
  await page.addInitScript(() => localStorage.setItem('perflens-theme', 'light'));
}

/**
 * Open a view through the URL-hash deep link the app already supports, so
 * no clicking is needed and the entry point is identical every run.
 */
export async function openHash(page: Page, hash: string) {
  await page.goto(`/#${hash}`);
  await expect(page.locator('#view-profiling')).toBeVisible();
}

/** Replay the committed fixture and land on a given tab. */
export async function openFixture(page: Page, tab: string, extra = '') {
  await openHash(page, `tab=${tab}&session=${FIXTURE}${extra}`);
  await expect(page.locator('#replay-banner')).toHaveClass(/visible/);
}

/**
 * Hold everything still before a capture. Transitions are the real hazard:
 * the docs drawer and the export menu both animate in via a .visible class,
 * so an ungated screenshot catches them half-slid.
 */
export async function settle(page: Page) {
  await page.addStyleTag({
    content: `*, *::before, *::after {
      transition: none !important;
      animation: none !important;
      caret-color: transparent !important;
    }`,
  });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(120);
}

/**
 * Pick the event worth screenshotting from whatever this machine recorded.
 *
 * Hybrid-core x86 reports `cpu_core/cycles/` and `cpu_atom/cycles/` rather
 * than a plain `cycles`, and the selector is alphabetical -- so the default
 * selection is branch-instructions on the efficiency cores, which is a
 * strange thing to put on a landing page. Prefer cycles, and prefer the
 * performance cores when the split exists.
 */
export async function pickCyclesEvent(page: Page): Promise<string | null> {
  const options = await page.locator('#event-select option')
    .evaluateAll((els) => els.map((e) => (e as HTMLOptionElement).value));
  return options.find((o) => o === 'cycles')
    ?? options.find((o) => o.includes('cpu_core') && o.includes('cycles'))
    ?? options.find((o) => o.includes('cycles'))
    ?? options[0] ?? null;
}

/** Select the best cycles-like event and wait for the view to follow. */
export async function useCyclesEvent(page: Page) {
  const event = await pickCyclesEvent(page);
  if (!event) return;
  await page.selectOption('#event-select', event);
  await page.waitForTimeout(250);
}

/**
 * Collapse the Device Health strip to its minimal level.
 *
 * The strip plus the counter bar is taller than a 900px viewport, which
 * leaves a tour screenshot showing chrome and a sliver of the thing it is
 * named after. The collapse control is a real user affordance (it cycles
 * full -> compact -> minimal), so this is how someone would actually make
 * room for the profile. The hero shot deliberately skips it.
 */
export async function collapseMetrics(page: Page) {
  const strip = page.locator('#metrics-strip');
  const btn = page.locator('#metrics-collapse-btn');
  if (!(await btn.count())) return;
  // The control cycles full -> compact -> minimal -> full, so clicking a
  // fixed number of times is only correct from a known starting level.
  // Drive to the target state instead; callers may invoke this repeatedly.
  for (let i = 0; i < 3; i++) {
    if (/minimal/.test((await strip.getAttribute('class')) ?? '')) break;
    await btn.click();
    await page.waitForTimeout(80);
  }
  await page.waitForTimeout(80);
}

/**
 * Bring the tab content to the top of the viewport.
 *
 * Necessary because the stat bar and the Device Health strip together are
 * taller than a 900px viewport, so a shot taken at scroll-0 shows chrome and
 * none of the thing it is named after. The first run of this harness
 * produced four identical screenshots for exactly that reason.
 */
export async function focusContent(page: Page) {
  await page.evaluate(() => {
    document.querySelector('#tabs')?.scrollIntoView({ block: 'start' });
  });
  await page.waitForTimeout(80);
}

/**
 * Wait until the flamegraph has actually painted frames.
 *
 * Gates on the rendered SVG, not just `window.__perflens`: the hook is a
 * live getter over the current layout and reports the *previous* event's
 * rects while a switch is still re-rendering. Trusting it alone produced a
 * committed screenshot of an empty flame graph.
 */
export async function flamegraphReady(page: Page, min = 10) {
  const frames = page.locator('#flamegraph-container svg g[data-idx]');
  await expect.poll(() => frames.count(), { timeout: 20_000 })
    .toBeGreaterThan(min);
  await page.waitForFunction((m) => {
    const h = (window as unknown as { __perflens?: PerflensHook }).__perflens;
    return !!h && h.rects.length > m;
  }, min);
  await page.waitForTimeout(150);
}

/** Wait until the function table has real rows, not loading skeletons. */
export async function functionsReady(page: Page, min = 5) {
  const rows = page.locator('#function-table tbody tr[data-func]');
  await expect(rows.first()).toBeVisible();
  expect(await rows.count()).toBeGreaterThan(min);
}

export async function snap(page: Page, name: string, scale = false) {
  mkdirSync(OUT_DIR, { recursive: true });
  await settle(page);
  await page.screenshot({
    path: join(OUT_DIR, name),
    animations: 'disabled',
    scale: scale ? 'device' : 'css',
  });
}
