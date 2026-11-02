import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal } from './helpers';

// Shift-click ranges from the last SELECTED tile. Opening a tile (plain click → sample modal) is
// not a selection and must not move that anchor. The bug: select tile 1, open tile 6, shift-click
// tile 9 → the range came out as 6..9 (from the opened image) instead of 1..9.
test.use({ storageState: AUTH_STATE });

test('shift-click ranges from the last selected tile, not the last opened one', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, 12);
    const cells = page.getByTestId('grid-cell');
    const count = page.getByTestId('grid-selection-count');
    // Select tile 1 (index 0) via its checkbox: this is the anchor.
    await cells.nth(0).hover();
    await cells.nth(0).getByTestId('cell-checkbox').click({ force: true });
    await expect(count).toContainText('1 selected');
    // Open tile 6 (index 5) with a plain click, then close the modal.
    await cells.nth(5).click();
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 10_000 });
    await page.keyboard.press('Escape');
    await expect(page.getByTitle('Next sample (Right arrow)')).toHaveCount(0);
    // Shift-click tile 9 (index 8): the range must be 1..9 = 9 tiles, not 6..9 (+ tile 1) = 5.
    await cells.nth(8).hover();
    await cells.nth(8).getByTestId('cell-checkbox').click({ force: true, modifiers: ['Shift'] });
    await expect(count).toContainText('9 selected');
});
