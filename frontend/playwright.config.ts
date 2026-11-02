import { defineConfig } from '@playwright/test';

// GUI smoke tests. Requires the Freedom Ridge backend on :8000 (E2E_API_URL to
// override). The frontend dev server is reused if already running on :3000,
// otherwise started automatically.
export default defineConfig({
    testDir: './e2e',
    timeout: 60_000,
    retries: 0,
    reporter: [['list']],
    use: {
        baseURL: process.env.E2E_BASE_URL || 'http://localhost:3000',
        trace: 'retain-on-failure',
    },
    globalSetup: './e2e/global-setup',
    globalTeardown: './e2e/global-teardown',
    webServer: {
        command: `npm run dev -- --port ${new URL(process.env.E2E_BASE_URL || 'http://localhost:3000').port || '3000'}`,
        url: process.env.E2E_BASE_URL || 'http://localhost:3000',
        reuseExistingServer: true,
        timeout: 120_000,
    },
});
