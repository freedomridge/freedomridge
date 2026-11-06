import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal } from './helpers';

// The filter bar's query editor (docs/FILTER_QUERY.md): a pasted list becomes one filter, the chips read back
// as text, and a mistake is shown where it is without changing anything. Fixture: 12 samples, idx 0-11,
// split=train for idx 0-5 and val for 6-11. Read-only against the fixture.
test.use({ storageState: AUTH_STATE });

test('a pasted list applies as one filter, reads back as text, and a mistake changes nothing', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, 12);

    await page.getByTestId('filter-query-trigger').click();
    const text = page.getByTestId('filter-query-text');
    await expect(text).toHaveValue('');
    // A column pasted between brackets: spaces, newlines and commas all separate values.
    await text.fill('idx in (0  1\n2, 3 4\n7)\nAND split = train');
    await page.getByTestId('filter-query-apply').click();
    await expect(page.getByTestId('filter-query-editor')).toBeHidden();
    await expectTotal(page, 5);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(2);

    // The chips read back as the text the server writes.
    await page.getByTestId('filter-query-trigger').click();
    await expect(text).toHaveValue('idx in (0, 1, 2, 3, 4, 7)\nAND split = "train"');

    // A misspelt field: the error says where and what was meant, and the filters stay as they were.
    await text.fill('idx in (0 1 2 3 4 7)\nAND splt = train');
    await page.getByTestId('filter-query-apply').click();
    await expect(page.getByTestId('filter-query-problem')).toHaveText(
        "Line 2, column 5: unknown field 'splt' (did you mean 'split'?)",
    );
    // An edited query survives a stray click outside; Escape closes it.
    await page.getByTestId('total-samples').click();
    await expect(text).toHaveValue('idx in (0 1 2 3 4 7)\nAND splt = train');
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('filter-query-editor')).toBeHidden();
    await expectTotal(page, 5);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(2);

    // Deleting a line removes its chip; Ctrl+Enter applies.
    await page.getByTestId('filter-query-trigger').click();
    await text.fill('idx in (0 1 2 3 4 7)');
    await text.press('Control+Enter');
    await expectTotal(page, 6);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);

    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
});
