import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset } from './api';

// Regression: tagging the image in the single-image view meant closing it, ticking
// its tile and using the grid's Tag button. The view now has its own Tag button for the image shown.
test.use({ storageState: AUTH_STATE });

// A tag edit sets off a statistics job, which shows the jobs bar on every page open on the dataset:
// this spec uses its own copy of the fixture, not the shared one other specs are browsing.
let cookie = '';
let datasetId = 0;
test.beforeAll(async () => {
    cookie = await adminLogin();
    datasetId = await createFixtureDataset(cookie, `e2e-modal-tag-${Date.now()}`);
});
test.afterAll(async () => {
    if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
});

test('tag the image shown in the single-image view without leaving it', async ({ page }) => {
    const tag = `e2e_modal_${Date.now()}`;
    const withTag = async () => (await (await page.request.post(`/api/datasets/${datasetId}/samples/query`, {
        data: { filters: { field: 'tags', operator: 'in', value: [tag] }, limit: 20 },
    })).json()).items as Array<{ id: number }>;

    await gotoDataset(page, datasetId);
    await page.getByTestId('grid-cell').nth(3).click();
    const next = page.getByTitle('Next sample (Right arrow)');
    await expect(next).toBeVisible({ timeout: 10_000 });

    await page.getByTestId('modal-tag').click();
    await expect(page.getByText('Tag — 1 Sample')).toBeVisible();
    await page.getByPlaceholder('Enter tag name...').fill(tag);
    await page.getByRole('button', { name: 'Add', exact: true }).click();
    // Tagging makes the grid refetch its pages. Hold those refetches back (not the view's own
    // one-sample refresh, limit 1) so the view re-renders while the grid is still reloading —
    // the order CI hit, where the view's Previous/Next vanished for good.
    await page.route('**/samples/query', async route => {
        if (route.request().postDataJSON()?.limit !== 1) await new Promise(r => setTimeout(r, 3_000));
        await route.continue();
    });
    await page.getByRole('button', { name: 'Apply Changes' }).click();

    // Tag dialog gone, the view still open — with its Previous/Next — and its Properties list the new tag.
    await expect(page.getByText('Tag — 1 Sample')).toHaveCount(0);
    await expect(page.getByRole('dialog').getByText(tag)).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(3_500);  // let the held grid refetch land
    await expect(next).toBeVisible();
    await page.unroute('**/samples/query');
    // Exactly the one image, and the grid selection was not touched.
    const tagged = await withTag();
    expect(tagged).toHaveLength(1);
    await expect(page.getByTestId('grid-selection-count')).toHaveCount(0);

    // Escape closes only the tag dialog, not the view.
    await page.getByTestId('modal-tag').click();
    await expect(page.getByText('Tag — 1 Sample')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByText('Tag — 1 Sample')).toHaveCount(0);
    await expect(next).toBeVisible();
});
