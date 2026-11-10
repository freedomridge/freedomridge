import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal } from './helpers';
import { readState } from './api';

// Regression: a Select All over 50,000 samples is a server token that expires after
// 2 hours, so a view saved with it reopened as 0 samples. Such a view is saved with its filters only
// (Select All is exactly what they show), and the UI says why. The 12-row fixture never needs a
// token, so the two responses a big Select All gets (its count, its token) are faked.
test.use({ storageState: AUTH_STATE });

async function bigSelectAll(page: Page) {
    await page.route('**/samples/query', async route => {
        if (route.request().postDataJSON()?.limit === 1) {
            await route.fulfill({ json: { items: [], total: 60_000 } });  // Select All's count probe
        } else {
            await route.continue();
        }
    });
    await page.route('**/selection_token', route => route.fulfill({ json: { token: 'e2e-expired-token', count: 60_000 } }));
    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toContainText('60,000', { timeout: 10_000 });
    await page.unroute('**/samples/query');
    await page.unroute('**/selection_token');
}

test('a view with a big Select All is saved with its filters only, and says why', async ({ page }) => {
    const { datasetId } = readState();
    await gotoDataset(page);
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('idx');
    await page.locator('[data-radix-popper-content-wrapper] button:has(span:text-is("idx"))').click();
    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.locator('select').nth(1).selectOption('>=');
    await editor.getByPlaceholder('Value...').fill('9');
    await editor.getByRole('button', { name: 'Apply' }).click();
    await expectTotal(page, 3);

    await bigSelectAll(page);
    await page.getByTestId('apply-filters').click();  // the selection becomes a "60000 Selected" chip
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(2);

    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByPlaceholder('Enter view name...').fill(`e2e-temp-sel-${Date.now()}`);
    await expect(page.getByTestId('view-temporary-selection-note')).toContainText('Selected)” is a temporary selection');
    const saved = page.waitForRequest(r => /\/datasets\/\d+\/views$/.test(r.url()) && r.method() === 'POST');
    await page.getByRole('button', { name: 'Save filters only' }).click();
    const body = (await saved).postDataJSON();
    expect(JSON.stringify(body.content)).not.toContain('in_selection');
    expect(body.content.viewStages).toHaveLength(1);
    expect(body.content.viewStages[0].description).toBe('idx ≥ 9');

    // Clean up the saved view.
    const list = await (await page.request.get(`/api/datasets/${datasetId}/views`)).json();
    for (const v of list.filter((v: { name: string }) => v.name === body.name)) {
        await page.request.delete(`/api/views/${v.id}`);
    }
});

// A big Select All's chip holds a token, not ids, and the server has no "all but this token". Clicking the chip
// used to relabel it "Exclude: …" and leave the grid, the count and the export on the selected set.
test('a big Select All chip offers no exclude toggle', async ({ page }) => {
    await gotoDataset(page);
    await bigSelectAll(page);
    await page.getByTestId('apply-filters').click();
    const chip = page.getByTestId('query-stage-chip').first();
    await expect(chip).toContainText('Selected');
    await chip.click({ position: { x: 12, y: 10 } });
    await expect(chip).not.toContainText('Exclude');
    await expect(page.locator('[title="Click to toggle include/exclude"]')).toHaveCount(0);
});

test('the API refuses a view holding a selection token', async ({ page }) => {
    const { datasetId } = readState();
    const res = await page.request.post(`/api/datasets/${datasetId}/views`, {
        data: {
            name: `e2e-temp-sel-api-${Date.now()}`,
            content: { viewStages: [{ type: 'selection', description: 'x', filter: { field: 'id', operator: 'in_selection', value: 't' } }] },
        },
    });
    expect(res.status()).toBe(422);
    expect((await res.json()).detail).toContain('Save the filters');
});
