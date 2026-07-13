import { defineConfig, devices } from '@playwright/test'

// Thin end-to-end smoke layer. Covers what the Vitest/jsdom suite can't: the
// app running in a real browser against a real (mock) network, including the
// EventSource live-update path. Two web servers are started for the run — the
// mock backend on :8000 and the Vite dev server on :5173, which proxies /api to
// the mock (see vite.config.ts). Single worker: the mock's SSE counter is
// shared state, so tests must not run concurrently.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: [
    {
      command: 'node e2e/mock-api.mjs',
      url: 'http://127.0.0.1:8000/api/overview',
      reuseExistingServer: !process.env.CI,
      stdout: 'pipe',
    },
    {
      command: 'npm run dev',
      url: 'http://localhost:5173',
      reuseExistingServer: !process.env.CI,
    },
  ],
})
