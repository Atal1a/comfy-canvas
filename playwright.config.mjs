import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  use: {
    baseURL: 'http://127.0.0.1:43178',
    browserName: 'chromium',
    channel: process.env.CI ? undefined : 'msedge',
    viewport: { width: 1280, height: 800 },
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'node tools/serve_frontend_test.mjs',
    url: 'http://127.0.0.1:43178/healthz',
    reuseExistingServer: false,
    timeout: 15_000,
  },
});
