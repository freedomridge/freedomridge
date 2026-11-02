import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset } from './helpers';

// Select-All: below the token threshold it keeps an explicit id list (so id-based ops still
// work); above it, a weightless server-side token. The 12-image fixture is below the
// threshold, so this exercises the id-list path + the shared selection indicator / tag flow.
// (The token path's server side — create_selection_token, tag-by-token — is covered by the
// backend tests.)
test.use({ storageState: AUTH_STATE });

test('Select All shows the selection count and clears cleanly', async ({ page }) => {
    await gotoDataset(page);

    await page.getByTestId('grid-select-all').click();
    const indicator = page.getByTestId('grid-selection-count');
    await expect(indicator).toBeVisible({ timeout: 10_000 });
    await expect(indicator).toContainText('12 selected');

    // Clearing drops the selection and restores Select All.
    await indicator.click();
    await expect(page.getByTestId('grid-selection-count')).toHaveCount(0);
    await expect(page.getByTestId('grid-select-all')).toBeVisible();
});

test('Tag modal loads counts for a Select-All', async ({ page }) => {
    await gotoDataset(page);

    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toBeVisible({ timeout: 10_000 });

    await page.getByTestId('grid-tag-button').click();
    await expect(page.getByText(/12 Samples/)).toBeVisible({ timeout: 10_000 });
});

test('Select All can be applied as a view stage and clears the selection', async ({ page }) => {
    await gotoDataset(page);

    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toBeVisible({ timeout: 10_000 });

    // hasUnappliedChanges surfaces the Apply button; applying bakes the selection into a stage
    // and clears it (grid returns to the no-selection state).
    await page.getByTestId('apply-filters').click();
    await expect(page.getByTestId('grid-selection-count')).toHaveCount(0);
    await expect(page.getByTestId('grid-select-all')).toBeVisible();
});

// When the machine's network changes, Chromium fails the requests still waiting for a connection
// (net::ERR_NETWORK_CHANGED), and right after a dataset opens Select All's request can be one of them.
// React Query asks a dropped query again; a dropped Select All request used to fail the whole
// Select All instead (an alert, and no selection).
test('Select All asks again when its request is dropped', async ({ page }) => {
    await gotoDataset(page);
    let dropped = 0;
    await page.route('**/api/datasets/*/samples/query', async (route) => {
        const body = route.request().postDataJSON();
        // Select All's first request: its count of the grid's view (the open's object probe is `=`).
        if (dropped === 0 && body?.limit === 1 && JSON.stringify(body.filters).includes('"!="')) {
            dropped += 1;
            return route.abort('failed');
        }
        return route.continue();
    });

    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toContainText('12 selected', { timeout: 10_000 });
    expect(dropped).toBe(1);
});
