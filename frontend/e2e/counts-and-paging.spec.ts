import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal } from './helpers';

// counts and loading states.
test.use({ storageState: AUTH_STATE });

test('a filter matching nothing shows 0, not the previous count', async ({ page }) => {
    // Regression: the grid only ever reported totals > 0, so after a zero-row filter the header
    // kept "12 images" / "Select All (12)" next to the empty grid.
    await gotoDataset(page);
    await expectTotal(page, 12);
    await expect(page.getByTestId('grid-view-count')).toHaveText(/^\s*12 images\s*$/);

    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('idx');
    await page.locator('[data-radix-popper-content-wrapper] button:has(span:text-is("idx"))').click();
    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.locator('select').nth(1).selectOption('>=');
    await editor.getByPlaceholder('Value...').fill('1000');
    await editor.getByRole('button', { name: 'Apply' }).click();

    await expectTotal(page, 0);
    await expect(page.getByTestId('grid-view-count')).toHaveText(/^\s*0 images\s*$/);
    await expect(page.getByTestId('grid-select-all')).not.toContainText('(12)');
});

test('the modal walks past the rows the grid has loaded', async ({ page }) => {
    // Regression: Next dead-ended at row 200 (the end of the first loaded page) of 27,000. Uses the
    // real eurosat dataset (> 200 rows); skips where it isn't present (CI). Read-only.
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'eurosat');
    test.skip(!ds, 'eurosat dataset not present');

    await page.goto(`/datasets/${ds.id}`);
    await expect(page.getByTestId('grid-view-count')).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });
    await page.getByTestId('grid-cell').nth(0).click();
    const next = page.getByTitle('Next sample (Right arrow)');
    await expect(next).toBeVisible({ timeout: 10_000 });

    // Step to the last row of the first 200-row page...
    for (let i = 0; i < 199; i++) await page.keyboard.press('ArrowRight');
    await expect(next).toBeVisible();
    const title = page.getByRole('dialog').getByRole('heading').first();
    const atRow200 = await title.textContent();
    // ...and one more: the 201st row is not loaded yet; Next must fetch it, not vanish.
    await next.click();
    await expect(title).not.toHaveText(atRow200 || '', { timeout: 15_000 });
    await expect(next).toBeVisible();
});
