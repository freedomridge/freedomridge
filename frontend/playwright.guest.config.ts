import { defineConfig } from '@playwright/test';

// An already-running disposable production build: backend :8099, next start :3099.
// Seed >=200 local images in its FRIDGE_DATA_ROOT, open that dataset to guests (Dataset Settings →
// Visibility → Everyone signed in), then run with E2E_GUEST_DATASET_ID.
// No global setup, admin login, or shared development stack is used.
export default defineConfig({
    testDir: './e2e',
    testMatch: 'guest-scroll.spec.ts',
    outputDir: '../data/auth-regression/playwright',
    timeout: 120_000,
    workers: 1,
    retries: 0,
    reporter: 'list',
    use: {
        baseURL: 'http://127.0.0.1:3099',
        browserName: 'chromium',
        viewport: { width: 1440, height: 1000 },
        trace: 'retain-on-failure',
    },
});
