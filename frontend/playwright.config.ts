import { defineConfig, devices } from '@playwright/test';

const HTTP_PORT = process.env.PERFLENS_E2E_HTTP_PORT || '18477';
const BASE_URL = process.env.PERFLENS_BASE_URL || `http://127.0.0.1:${HTTP_PORT}`;

// The docs-live and docs-gif projects need live server state (per-thread
// aggregates, source annotation, a moving sample count), which a replayed
// session cannot provide. They run against a server started separately by
// tools/live-capture.sh, so the managed webServer is switched off for them.
const EXTERNAL_SERVER = !!process.env.PERFLENS_EXTERNAL_SERVER;

// Screenshots for docs/screenshots/. Only the og:image is shot at 2x --
// the tour cards render in a grid where 1x is indistinguishable, and
// every extra 2x PNG is permanent weight in the repo.
const DOCS_VIEWPORT = { width: 1440, height: 900 };

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  fullyParallel: false,   // specs share one server + replayed session
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },

    // Deterministic docs shots: fixture replay only, no `perf` needed, so
    // this project is safe to smoke-run in CI on every PR.
    {
      name: 'docs-replay',
      testDir: './docs-shots',
      testMatch: /replay\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: DOCS_VIEWPORT },
    },

    // Live-only shots: threads, source annotation, timeline scrubbing, diff.
    {
      name: 'docs-live',
      testDir: './docs-shots',
      testMatch: /live\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: DOCS_VIEWPORT },
    },

    // GIF frames at the geometry the encoder expects: 1280x720 @1x scales
    // to the 900px-wide docs/demo.gif without resampling artifacts.
    {
      name: 'docs-gif',
      testDir: './docs-shots',
      testMatch: /demo-gif\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1280, height: 720 },
        deviceScaleFactor: 1,
      },
    },
  ],
  webServer: EXTERNAL_SERVER ? undefined : {
    command: 'node e2e/start-server.mjs',
    url: `${BASE_URL}/api/status`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
