import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';

// Object-grain grid actions: selecting ONE object surfaces "Find Similar" (similar OBJECTS)
// and "Find Unlabeled" (object -> patch look-alikes, via a dialog that picks patch size +
// count). Validates against open-images-v7 (image + object rows + clip_patch tiles); skips
// cleanly if absent so CI stays green.
test.use({ storageState: AUTH_STATE });

// Flip the grid to object grain; returns once object cells are rendered.
async function gotoObjectGrain(page: import('@playwright/test').Page) {
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'open-images-v7');
    test.skip(!ds, 'open-images-v7 dataset not present');
    await page.goto(`/datasets/${ds.id}`);
    const count = page.getByTestId('grid-view-count');
    await expect(count).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });
    await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();
    await expect.poll(async () => {
        const t = (await count.textContent()) || '';
        return /object/.test(t) ? parseInt(t.replace(/[^0-9]/g, ''), 10) : 0;
    }, { timeout: 20_000 }).toBeGreaterThan(0);
}

// Select exactly one object: hover the cell to reveal its checkbox, then click it.
async function selectOneObject(page: import('@playwright/test').Page) {
    const firstCell = page.getByTestId('grid-cell').first();
    await firstCell.scrollIntoViewIfNeeded();
    await firstCell.hover();
    await firstCell.getByTestId('cell-checkbox').click();
    await expect(page.getByText(/1 selected/).first()).toBeVisible({ timeout: 10_000 });
}

test('object grain: "Find Unlabeled" opens a dialog with patch size + count', async ({ page }) => {
    await gotoObjectGrain(page);
    await selectOneObject(page);

    const findUnlabeled = page.getByRole('button', { name: 'Find Unlabeled' });
    await expect(findUnlabeled).toBeVisible();
    await findUnlabeled.click();

    // The dialog (previously the search ran with no popup) — patch size + count selectors.
    await expect(page.getByTestId('unlabeled-scale-select')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('unlabeled-limit-input')).toBeVisible();

    // Pick a size + run; the search resolves to an "Unlabeled look-alikes" view stage.
    await page.getByTestId('unlabeled-scale-select').selectOption('8x8');
    await page.getByTestId('unlabeled-search-btn').click();
    await expect(page.getByText(/Unlabeled look-alikes/).first()).toBeVisible({ timeout: 30_000 });
});

test('object grain: "Find Similar" on an object opens the similarity dialog', async ({ page }) => {
    await gotoObjectGrain(page);
    await selectOneObject(page);

    const findSimilar = page.getByRole('button', { name: /Find Similar/ });
    await expect(findSimilar).toBeVisible();
    await findSimilar.click();

    // The modal opens (previously this silently no-op'd because the seed object wasn't in the
    // shared grid-samples ref).
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByRole('heading', { name: 'Find Similar Samples' })).toBeVisible({ timeout: 10_000 });
    // For an object seed the field is LOCKED to clip_obj (no editable dropdown whose choice
    // would be silently ignored).
    await expect(dialog.getByText('clip_obj (object embedding)')).toBeVisible();
    await expect(dialog.locator('select')).toHaveCount(0);
    // Run it -> a "Similar objects" view stage applies.
    await page.getByRole('button', { name: 'Search' }).click();
    await expect(page.getByText(/Similar objects/).first()).toBeVisible({ timeout: 30_000 });
});

test('object grain: modal prev/next navigates the object list (opens parent image)', async ({ page }) => {
    await gotoObjectGrain(page);

    // Click an object cell (NOT the checkbox) — the modal opens on the object's PARENT image.
    await page.getByTestId('grid-cell').first().click();
    const next = page.getByTitle('Next sample (Right arrow)');
    await expect(next).toBeVisible({ timeout: 10_000 });
    // First object -> no Previous yet.
    await expect(page.getByTitle('Previous sample (Left arrow)')).toHaveCount(0);
    // Stepping indexes the OBJECT list (by the clicked row), not the shown parent image, so it
    // advances and Previous appears (this was dead — idx=-1 disabled both arrows).
    await next.click();
    await expect(page.getByTitle('Previous sample (Left arrow)')).toBeVisible({ timeout: 10_000 });
});
