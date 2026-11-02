import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset } from './helpers';

// which samples seed a similarity search.
test.use({ storageState: AUTH_STATE });

async function tick(page: import('@playwright/test').Page, i: number) {
    const cell = page.getByTestId('grid-cell').nth(i);
    await cell.hover();
    await cell.getByTestId('cell-checkbox').click({ force: true });
}

test('with several samples selected the dialog says it averages them', async ({ page }) => {
    // Regression: the search averaged every selected vector while the dialog named one "Reference Sample".
    await gotoDataset(page);
    await tick(page, 0);
    await tick(page, 1);
    await expect(page.getByTestId('grid-selection-count')).toContainText('2 selected');
    await page.getByRole('button', { name: /^Find Similar/ }).first().click();
    await expect(page.getByTestId('similarity-seed')).toHaveText('Averaging 2 selected samples');
});

test('Find Similar in the sample modal seeds with THAT sample, not the grid selection', async ({ page }) => {
    // The seed was re-read from the selection at search time, so with 2 tiles ticked the modal's
    // Find Similar averaged them instead of using the sample on screen.
    await gotoDataset(page);
    await tick(page, 0);
    await tick(page, 1);
    await page.getByTestId('grid-cell').nth(5).click();
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 10_000 });
    await page.getByTestId('modal-find-similar').click();
    await expect(page.getByTestId('similarity-seed')).not.toContainText('Averaging');

    const req = page.waitForRequest(r => r.url().includes('/api/search') && r.method() === 'POST');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    const body = (await req).postDataJSON();
    expect(body.sample_id).toBeTruthy();
    expect(body.sample_ids).toBeUndefined();
});
