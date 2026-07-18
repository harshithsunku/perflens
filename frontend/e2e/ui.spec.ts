// Browser E2E: the built React UI against a real server replaying a
// device-captured fixture session. Same coverage as the retired
// puppeteer suite (tests/e2e_ui.mjs), driven through the stable
// data-testid / window.__perflens contract.

import { expect, test, type Page } from '@playwright/test';

const FIXTURE = 'session-x86-baseline';

interface PerflensHook {
  rects: { name: string; value: number; depth: number }[];
  zoomNames: string[];
}

const pageErrors: string[] = [];

test.beforeEach(({ page }) => {
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') pageErrors.push(msg.text());
  });
});

async function replayFixture(page: Page) {
  await page.goto('/');
  await page.getByTestId('card-sessions').click();
  const row = page.locator('#sessions-table tbody tr', { hasText: FIXTURE });
  await row.locator('.replay-btn').first().click();
  await expect(page.locator('#replay-banner')).toHaveClass(/visible/);
}

test('landing page renders and lists saved sessions', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('h1')).toContainText('Lens');
  await page.getByTestId('card-sessions').click();
  await expect(page.locator('#sessions-table tbody tr')).toHaveCount(1);
  await expect(page.locator('#sessions-table tbody tr td').first())
    .toContainText(FIXTURE);
});

test('replaying a session fills the function table', async ({ page }) => {
  await replayFixture(page);
  await page.locator('.tab[data-tab="functions"]').click();
  const rows = page.locator('#function-table tbody tr[data-func]');
  await expect(rows.first()).toBeVisible();
  expect(await rows.count()).toBeGreaterThan(5);
  // Self% column renders a percentage bar
  await expect(rows.first().locator('.cpu-bar-text').first()).toContainText('%');
});

test('flamegraph renders, zooms by ancestry, breadcrumbs navigate, reset works',
    async ({ page }) => {
  await replayFixture(page);
  await page.locator('.tab[data-tab="flamegraph"]').click();
  const frames = page.locator('#flamegraph-container svg g[data-idx]');
  await expect(frames.first()).toBeVisible();
  const frameCount = await frames.count();
  expect(frameCount).toBeGreaterThan(3);

  // Find a deep frame with children (via the E2E hook) and click it
  const targetIdx = await page.evaluate(() => {
    const hook = (window as unknown as { __perflens: PerflensHook }).__perflens;
    const rects = hook.rects as unknown as
      { name: string; depth: number; node: { children?: unknown[] } }[];
    for (let i = rects.length - 1; i >= 0; i--) {
      if (rects[i].depth >= 2 && (rects[i].node.children?.length ?? 0) > 0) return i;
    }
    return -1;
  });
  expect(targetIdx).toBeGreaterThan(-1);
  await page.locator(`#flamegraph-container svg g[data-idx="${targetIdx}"]`).click();

  // Ancestry zoom: zoomNames non-empty, breadcrumb chain rendered
  await expect(page.locator('#flamegraph-reset')).toBeVisible();
  const zoomNames = await page.evaluate(() =>
    (window as unknown as { __perflens: PerflensHook }).__perflens.zoomNames);
  expect(zoomNames.length).toBeGreaterThanOrEqual(2);
  await expect(page.locator('.fg-crumb-current')).toHaveText(
    zoomNames[zoomNames.length - 1]);
  expect(await page.locator('.fg-crumb').count()).toBe(zoomNames.length);

  // Breadcrumb click zooms out to that ancestor
  await page.locator('.fg-crumb[data-crumb-idx="0"]').click();
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as { __perflens: PerflensHook }).__perflens.zoomNames.length,
  )).toBe(1);

  // Reset restores the full graph
  await page.locator('#flamegraph-reset').click();
  await expect(page.locator('#flamegraph-reset')).toBeHidden();
  await expect.poll(() => frames.count()).toBe(frameCount);
});

test('flamegraph search highlights and dims frames', async ({ page }) => {
  await replayFixture(page);
  await page.locator('.tab[data-tab="flamegraph"]').click();
  await expect(page.locator('#flamegraph-container svg g[data-idx]').first())
    .toBeVisible();

  // Search for the hottest leaf's name prefix
  const name = await page.evaluate(() => {
    const hook = (window as unknown as { __perflens: PerflensHook }).__perflens;
    return hook.rects[hook.rects.length - 1]?.name ?? '';
  });
  expect(name.length).toBeGreaterThan(0);
  await page.locator('#fg-search').fill(name.slice(0, 4));
  await expect(page.locator('#fg-search-matches')).toContainText('frames');
  expect(await page.locator('g.fg-match').count()).toBeGreaterThan(0);
  expect(await page.locator('g.fg-dim').count()).toBeGreaterThan(0);

  await page.locator('#fg-search-clear').click();
  await expect(page.locator('#fg-search-matches')).toHaveText('');
  expect(await page.locator('g.fg-dim').count()).toBe(0);
});

test('flamegraph context menu offers source / zoom / copy', async ({ page }) => {
  await replayFixture(page);
  await page.locator('.tab[data-tab="flamegraph"]').click();
  const frame = page.locator('#flamegraph-container svg g[data-idx="1"]');
  await frame.click({ button: 'right' });
  const menu = page.locator('#fg-context-menu');
  await expect(menu).toBeVisible();
  await expect(menu.locator('.fg-ctx-item[data-action="source"]')).toBeVisible();
  await expect(menu.locator('.fg-ctx-item[data-action="zoom"]')).toBeVisible();
  await expect(menu.locator('.fg-ctx-item[data-action="copy"]')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
});

test('export menu lists the three formats and the endpoints serve them',
    async ({ page, request }) => {
  await replayFixture(page);
  await page.locator('#export-btn').click();
  await expect(page.locator('#export-menu')).toHaveClass(/visible/);
  for (const action of ['svg', 'collapsed', 'json']) {
    await expect(page.locator(`#export-menu .export-item[data-action="${action}"]`))
      .toBeVisible();
  }

  // The endpoints behind the menu items work for the replayed session
  const svg = await request.get(
    `/api/sessions/${FIXTURE}/export?format=svg&event=cycles`);
  expect(svg.ok()).toBeTruthy();
  expect((await svg.text()).startsWith('<svg')).toBeTruthy();

  const collapsed = await request.get(
    `/api/sessions/${FIXTURE}/export?format=collapsed`);
  expect(collapsed.ok()).toBeTruthy();
  const firstLine = (await collapsed.text()).trim().split('\n')[0];
  const count = parseInt(firstLine.slice(firstLine.lastIndexOf(' ') + 1), 10);
  expect(count).toBeGreaterThan(0);

  const json = await request.get(`/api/sessions/${FIXTURE}/export?format=json`);
  expect(json.ok()).toBeTruthy();
  const body = await json.json();
  expect(body.metadata.session_id).toBe(FIXTURE);
  expect(Object.keys(body.per_event).length).toBeGreaterThan(0);
});

test('event selector switches events; URL hash tracks the view', async ({ page }) => {
  await replayFixture(page);
  const options = page.locator('#event-select option');
  const n = await options.count();
  expect(n).toBeGreaterThan(1);
  const second = await options.nth(1).getAttribute('value');
  await page.locator('#event-select').selectOption(second!);
  await expect(page.locator('#function-table tbody tr[data-func]').first())
    .toBeVisible();
  // URL hash carries session + non-default event for shareable links
  await expect.poll(() => page.url()).toContain('session=' + FIXTURE);
});

test('no page errors across the whole run', async ({ page }) => {
  await replayFixture(page);
  for (const tab of ['functions', 'flamegraph', 'threads', 'sessions']) {
    await page.locator(`.tab[data-tab="${tab}"]`).click();
    await page.waitForTimeout(200);
  }
  expect(pageErrors, 'page JS errors: ' + pageErrors.join('; ')).toHaveLength(0);
});
