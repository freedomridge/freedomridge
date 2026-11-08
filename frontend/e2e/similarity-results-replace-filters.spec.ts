import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal, expandField } from './helpers';

// Find Similar on the image grid: the result set must BE the view. Before this, the grid ANDed the
// result ids with whatever else was active — in particular a forgotten DRAFT sidebar filter — so a
// 250-result search showed "like 9 images" while the chip said the search had succeeded. The modal
// must also say how big the current view is and warn when more results are asked for than it holds.
test.use({ storageState: AUTH_STATE });

async function findSimilarFromFirstTile(page: Page, n: number, scope: 'entire' | 'current') {
    const cell = page.getByTestId('grid-cell').first();
    await cell.hover();
    await cell.getByTestId('cell-checkbox').click({ force: true });
    await page.getByRole('button', { name: /^Find Similar/ }).click();
    await expect(page.getByRole('heading', { name: 'Find Similar Samples' })).toBeVisible({ timeout: 10_000 });
    await page.getByTestId('similarity-limit').fill(String(n));  // not the bar's results box (also a number input)
    await page.locator(`input[type="radio"][value="${scope}"]`).check();
}

test('a draft sidebar filter does not shrink Find Similar results: the results replace it', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, 12);
    // Leave a DRAFT filter in place (the "(no value)" toggle on `note`, 6 of 12) without applying it.
    await expandField(page, 'note');
    await page.getByTestId('panel-filters').locator('label', { hasText: '(no value)' }).click();
    await expect(page.getByTestId('draft-filters')).toBeVisible();
    await findSimilarFromFirstTile(page, 12, 'entire');
    // The modal says drafts will be replaced by the results.
    await expect(page.getByTestId('similarity-draft-note')).toBeVisible();
    await page.getByRole('button', { name: 'Search' }).click();
    // All 12 results are shown — not 12 ∩ draft(6).
    await expectTotal(page, 12);
    await expect(page.getByTestId('draft-filters')).toHaveCount(0);
    await expect(page.getByTestId('query-stage-chip').filter({ hasText: 'Similarity Search (12)' })).toBeVisible();
});

test('the modal shows the current view size, warns when asking for more, and the chip says N of M', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, 12);
    // Apply the "(no value)" filter for real: the current view is now 6 samples.
    await expandField(page, 'note');
    await page.getByTestId('panel-filters').locator('label', { hasText: '(no value)' }).click();
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 6);
    await findSimilarFromFirstTile(page, 10, 'current');
    await expect(page.getByText(/Current View \(6 samples, with filters\)/)).toBeVisible();
    const warning = page.getByTestId('similarity-scope-warning');
    await expect(warning).toBeVisible();
    await expect(warning).toContainText('at most 6');
    await page.getByRole('button', { name: 'Search' }).click();
    await expectTotal(page, 6);
    await expect(page.getByTestId('query-stage-chip').filter({ hasText: 'Similarity Search (6 of 10 requested)' })).toBeVisible();
});
