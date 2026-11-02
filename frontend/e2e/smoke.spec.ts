import { test, expect, Page } from '@playwright/test';
import { DATASET_NAME, readState } from './api';
import { fillStable } from './helpers';

// GUI smoke: the app is reachable, login works, and a dataset's grid renders
// every sample within a time budget. Budgets are generous (dev-server compiles
// on first hit) — they catch hangs and broken pages, not perf regressions.
const LOAD_BUDGET_MS = 20_000;

async function login(page: Page) {
    await page.goto('/login');
    // fillStable, not fill: a plain fill can be undone by React hydration, which submits empty
    // credentials and fails as a (correct) 401. See the note on fillStable in helpers.ts.
    await fillStable(page.getByLabel('Username'), 'admin');
    await fillStable(page.getByLabel('Password'), 'admin123');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

test('unauthenticated visitors are redirected to login', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
});

test('login succeeds and the dataset list shows the fixture', async ({ page }) => {
    const t0 = Date.now();
    await login(page);
    await expect(page.getByText(DATASET_NAME).first()).toBeVisible({ timeout: 15_000 });
    console.log(`[timing] login -> dataset list: ${Date.now() - t0}ms`);
});

test('dataset grid renders all 12 samples within budget', async ({ page }) => {
    const { datasetId } = readState();
    await login(page);

    const t0 = Date.now();
    await page.goto(`/datasets/${datasetId}`);
    // Total count is server-truth: the grid toolbar shows it once loaded.
    await expect(page.getByText('Select All (12)')).toBeVisible({ timeout: LOAD_BUDGET_MS });
    const elapsed = Date.now() - t0;
    console.log(`[timing] dataset page -> grid ready: ${elapsed}ms`);
    expect(elapsed).toBeLessThan(LOAD_BUDGET_MS);
});

test('sample modal: prev/next walks the source panel\'s samples', async ({ page }) => {
    const { datasetId } = readState();
    await login(page);
    await page.goto(`/datasets/${datasetId}`);
    await expect(page.getByText('Select All (12)')).toBeVisible({ timeout: LOAD_BUDGET_MS });

    // Open the modal on the first cell. At index 0 there's a Next but no Previous.
    await page.getByTestId('grid-cell').first().click();
    const next = page.getByTitle('Next sample (Right arrow)');
    await expect(next).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTitle('Previous sample (Left arrow)')).toHaveCount(0);

    // Advancing reads the SOURCE panel's sample list (per-panel ref), so the index moves and
    // Previous now appears.
    await next.click();
    await expect(page.getByTitle('Previous sample (Left arrow)')).toBeVisible();
});

test('bad credentials are rejected with an error message', async ({ page }) => {
    await page.goto('/login');
    // Also fillStable here: if hydration ate the value, this would submit EMPTY credentials and
    // still show an error — passing for the wrong reason instead of testing a wrong password.
    await fillStable(page.getByLabel('Username'), 'admin');
    await fillStable(page.getByLabel('Password'), 'wrong-password');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByText(/invalid|incorrect|failed/i)).toBeVisible({ timeout: 10_000 });
});
