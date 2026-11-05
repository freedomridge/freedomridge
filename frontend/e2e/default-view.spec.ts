import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset } from './api';

// A dataset's default view: an administrator stars a public saved view, the dataset opens on it, Clear All
// goes back to it, and Show all images removes it until the next Clear All. This spec sets a default view,
// so it uses its own copy of the fixture, not the shared one other specs are browsing.
test.use({ storageState: AUTH_STATE });

const IDX_AT_LEAST_6 = { field: 'idx', operator: '>=', value: 6 };
let cookie = '';
let datasetId = 0;
let viewName = '';
test.beforeAll(async () => {
    cookie = await adminLogin();
    datasetId = await createFixtureDataset(cookie, `e2e-default-view-${Date.now()}`);
    viewName = `e2e-later-half-${Date.now()}`;
    await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({
            name: viewName,
            is_public: true,
            content: {
                filters: { logic: 'AND', filters: [IDX_AT_LEAST_6] },
                viewStages: [{ type: 'filter', description: 'idx ≥ 6', filter: IDX_AT_LEAST_6 }],
            },
        }),
    });
});
test.afterAll(async () => {
    if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
});

async function addIdxFilter(page: Page, op: string, value: string) {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('idx');
    await page.locator('[data-radix-popper-content-wrapper] button:has(span:text-is("idx"))').click();
    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.locator('select').nth(1).selectOption(op);
    await editor.getByPlaceholder('Value...').fill(value);
    await editor.getByRole('button', { name: 'Apply' }).click();
}

test('the dataset opens on its default view, Clear All keeps it and Show all images leaves it', async ({ page }) => {
    test.setTimeout(120_000);
    await gotoDataset(page, datasetId);
    await expectTotal(page, 12);
    await expect(page.getByTestId('default-view-label')).toHaveCount(0);

    // An administrator stars the view in Saved Views.
    await page.getByRole('button', { name: 'Saved Views' }).click();
    const row = page.locator('div.group', { hasText: viewName });
    await row.hover();
    const saved = page.waitForResponse(r => r.url().endsWith(`/api/datasets/${datasetId}`) && r.request().method() === 'PATCH');
    await row.getByTestId('view-default-toggle').click();
    expect((await saved).status()).toBe(200);
    await expect(row.getByTestId('view-default-badge')).toBeVisible();
    await page.getByRole('button', { name: 'Close Views' }).click();

    // Opening the dataset again starts on the default view, labelled.
    await gotoDataset(page, datasetId);
    await expectTotal(page, 6);
    await expect(page.getByTestId('default-view-label')).toHaveText(`Default view: ${viewName}`);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);
    await expect(page.getByTestId('default-view-star')).toHaveCount(1);
    await expect(page.getByRole('button', { name: 'Clear All' })).toHaveCount(0);

    // A filter of one's own narrows it; Clear All removes that and keeps the default view.
    await addIdxFilter(page, '>=', '9');
    await expectTotal(page, 3);
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 6);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);
    await expect(page.getByTestId('default-view-label')).toBeVisible();

    // Show all images removes it, also after a reload, until the Default view button brings it back.
    await page.getByRole('button', { name: 'Show all images' }).click();
    await expectTotal(page, 12);
    await expect(page.getByTestId('default-view-label')).toHaveCount(0);
    await page.waitForTimeout(500);  // the filters are saved for the next visit after 300 ms
    await gotoDataset(page, datasetId);
    await expectTotal(page, 12);
    await page.getByRole('button', { name: 'Default view', exact: true }).click();
    await expectTotal(page, 6);
    await expect(page.getByTestId('default-view-label')).toBeVisible();
});

test('the API returns the default view only to the app: the samples route still returns every sample', async () => {
    const view = (await apiFetch(cookie, `/datasets/${datasetId}/views`)).find((v: { name: string }) => v.name === viewName);
    await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: view.id }) });
    const dataset = await apiFetch(cookie, `/datasets/${datasetId}`);
    expect(dataset.default_view?.name).toBe(viewName);
    const samples = await apiFetch(cookie, `/datasets/${datasetId}/samples?limit=50`);
    expect(samples.total).toBe(12);
});
