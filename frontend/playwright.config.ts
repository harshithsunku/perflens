import { defineConfig, devices } from '@playwright/test';

const HTTP_PORT = process.env.PERFLENS_E2E_HTTP_PORT || '18477';

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  fullyParallel: false,   // specs share one server + replayed session
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${HTTP_PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: 'node e2e/start-server.mjs',
    url: `http://127.0.0.1:${HTTP_PORT}/api/status`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
