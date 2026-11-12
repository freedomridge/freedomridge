import { defineConfig } from '@playwright/test';

// The specs whose tests make the server lay out a map for real (a reduce, a managed re-fit). A fit keeps the API busy
// for seconds (numba's threads and first-call compiles), and a spec waiting on the API meanwhile timed out.
const FITS = /\.fits\.spec\.ts$/;

// GUI smoke tests. Requires the Freedom Ridge backend on :8000 (E2E_API_URL to
// override). The frontend dev server is reused if already running on :3000,
// otherwise started automatically.
export default defineConfig({
    testDir: './e2e',
    timeout: 60_000,
    retries: 0,
    reporter: [['list']],
    projects: [
        // Every other spec, in parallel as before.
        { name: 'default', testIgnore: FITS },
        // Then the fits, after the default project has finished, one file at a time and each file's tests in order.
        // A file filter naming a fits spec would run the whole default project first, as its dependency: to run just
        // it, `npx playwright test --project=fits --no-deps <file>`. A failed default spec also skips this project in the
        // same command, so scripts/ci_local.sh runs the two projects as two commands.
        { name: 'fits', testMatch: FITS, dependencies: ['default'], fullyParallel: false, workers: 1 },
    ],
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
