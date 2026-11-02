import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset } from './helpers';

// The grid's image count must stay visible at all times — including after a selection is
// made. Previously the count lived only in the "Select All" button, which hides on
// selection, so the count disappeared.
test.use({ storageState: AUTH_STATE });

test('grid: image count is always shown and persists through selection', async ({ page }) => {
    await gotoDataset(page);
    const count = page.getByTestId('grid-view-count');
    await expect(count).toHaveText(/^\s*12 images\s*$/);

    // Make a selection (Select All) — its button hides once a selection is active, but
    // the view count must remain visible and unchanged.
    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-select-all')).toHaveCount(0); // selection active
    await expect(count).toHaveText(/^\s*12 images\s*$/);             // count persists
});
