import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal, expandField } from './helpers';

// a selection must describe what the grid shows.
// Fixture: 12 images, split=train for idx 0-5 and val for 6-11, plus object rows.
test.use({ storageState: AUTH_STATE });

test('Select All is dropped when the view changes — it no longer means "all"', async ({ page }) => {
    // Regression: after Select All and a filter leaving 6 images, Tag still targeted all
    // 12 (and in token mode every tile of the new view was painted selected).
    await gotoDataset(page);
    await expectTotal(page, 12);
    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toContainText('12 selected');

    await expandField(page, 'split');
    await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'train', exact: true }).check();
    await expect(page.getByTestId('grid-selection-count')).toHaveCount(0);
    await expect(page.getByTestId('grid-select-all')).toBeVisible();
});

test('a hand-picked selection survives a view change', async ({ page }) => {
    // Only Select All is view-bound; picked samples persist across views.
    await gotoDataset(page);
    await expectTotal(page, 12);
    const cells = page.getByTestId('grid-cell');
    await cells.nth(0).hover();
    await cells.nth(0).getByTestId('cell-checkbox').click({ force: true });
    await expect(page.getByTestId('grid-selection-count')).toContainText('1 selected');

    await expandField(page, 'split');
    await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'val', exact: true }).check();
    await expect(page.getByTestId('grid-view-count')).toHaveText(/^\s*6 images\s*$/);
    await expect(page.getByTestId('grid-selection-count')).toContainText('1 selected');
});

test('a view change resets the shift-click anchor', async ({ page }) => {
    // Regression: the anchor is an index into the OLD ordering — select tile 3, change the view,
    // shift-click tile 5 → it ranged 3..5 of the NEW grid (+ the first pick) = 4 selected.
    await gotoDataset(page);
    await expectTotal(page, 12);
    const cells = page.getByTestId('grid-cell');
    const count = page.getByTestId('grid-selection-count');
    await cells.nth(2).hover();
    await cells.nth(2).getByTestId('cell-checkbox').click({ force: true });
    await expect(count).toContainText('1 selected');

    await expandField(page, 'split');
    await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'val', exact: true }).check();
    await expect(page.getByTestId('grid-view-count')).toHaveText(/^\s*6 images\s*$/);
    await cells.nth(4).hover();
    await cells.nth(4).getByTestId('cell-checkbox').click({ force: true, modifiers: ['Shift'] });
    await expect(count).toContainText('2 selected');  // a fresh pick, not a stale range
});

test('in object grain the modal selects the clicked OBJECT, not its parent image', async ({ page }) => {
    // Regression: the modal shows the parent image; Space toggled that image's id, so "1 selected"
    // appeared but no object tile was highlighted. Uses the real open-images-v7 dataset (image
    // rows + object rows), like object-view.spec.ts; skips where it isn't present (CI). Read-only.
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'open-images-v7');
    test.skip(!ds, 'open-images-v7 dataset not present');

    await page.goto(`/datasets/${ds.id}`);
    await expect(page.getByTestId('grid-view-count')).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });
    await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();
    await expect(page.getByTestId('grid-view-count')).toHaveText(/[1-9][\d,]*\s+object/, { timeout: 20_000 });

    const cells = page.getByTestId('grid-cell');
    await cells.nth(0).click();
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 10_000 });
    await page.keyboard.press(' ');
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('grid-selection-count')).toContainText('1 selected');
    await expect(cells.nth(0)).toHaveAttribute('data-selected', 'true');
});
