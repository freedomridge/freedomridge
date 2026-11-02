import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset } from './helpers';

// the sample modal.
test.use({ storageState: AUTH_STATE });

test('keys typed into the modal\'s own controls do not change the sample', async ({ page }) => {
    // Regression: the modal's window-level shortcuts fired inside its own inputs — ArrowRight in the
    // patch controls jumped to the next sample. Needs a dataset with patch embeddings (the real
    // synth-demo dataset); skips where it isn't present (CI). Read-only.
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'synth-demo');
    test.skip(!ds, 'synth-demo dataset not present');

    await page.goto(`/datasets/${ds.id}`);
    await expect(page.getByTestId('grid-view-count')).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });
    await page.getByTestId('grid-cell').nth(0).click();
    const dialog = page.getByRole('dialog');
    const title = dialog.getByRole('heading').first();
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 10_000 });
    const before = await title.textContent();

    const control = dialog.locator('select').first();
    await expect(control).toBeVisible();
    await control.focus();
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(500);
    await expect(title).toHaveText(before || '');

    // Outside a control the shortcut still works.
    await title.click();
    await page.keyboard.press('ArrowRight');
    await expect(title).not.toHaveText(before || '');
});

test('the modal offers Find Similar and Copy path for the sample on screen', async ({ page }) => {
    // Regression: the modal had no per-sample actions — Find Similar meant close, tick, grid button.
    await gotoDataset(page);
    await page.getByTestId('grid-cell').nth(0).click();
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('modal-copy-path')).toBeVisible();
    await page.getByTestId('modal-find-similar').click();
    await expect(page.getByRole('heading', { name: 'Find Similar Samples' })).toBeVisible({ timeout: 10_000 });
});
