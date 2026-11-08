import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal } from './helpers';

// Each filter's card edits it as text too (docs/FILTER_QUERY.md): Builder | Query. Add Filter takes a pasted list,
// a chip's card shows its filter as the text the server writes, a mistake is shown where it is without changing
// anything, and switching back to Builder keeps the edit. Fixture: 12 samples, idx 0-11, split=train for idx 0-5
// and val for 6-11. Read-only against the fixture.
test.use({ storageState: AUTH_STATE });

async function addByQuery(page: Page, query: string) {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    await page.getByTestId('filter-card-query').fill(query);
    await page.getByTestId('filter-card-apply').click();
}

test('Add Filter takes a pasted list as text, and a chip card edits its filter as text', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, 12);
    await expect(page.getByTestId('no-filters')).toBeVisible();

    // A column pasted between brackets: spaces, newlines and commas all separate values.
    await addByQuery(page, 'idx in (0  1\n2, 3 4\n7)');
    await expectTotal(page, 6);
    const chips = page.getByTestId('query-stage-chip');
    await expect(chips).toHaveCount(1);

    // The chip's card opens on the builder; Query shows the filter as the server writes it.
    await chips.first().click();
    await page.getByTestId('filter-mode-query').click();
    const text = page.getByTestId('filter-card-query');
    await expect(text).toHaveValue('idx in (0, 1, 2, 3, 4, 7)');

    // A misspelt field: the error says where and what was meant, and the filter stays as it was.
    await text.fill('idx in (0 1 2 3 4 7)\nAND splt = train');
    await page.getByTestId('filter-card-apply').click();
    await expect(page.getByTestId('filter-card-problem')).toHaveText(
        "Line 2, column 5: unknown field 'splt' (did you mean 'split'?)",
    );
    await expectTotal(page, 6);

    // Fixed, Ctrl+Enter applies: the chip holds both rules.
    await text.fill('idx in (0 1 2 3 4 7)\nAND split = train');
    await text.press('Control+Enter');
    await expectTotal(page, 5);
    await expect(chips).toHaveCount(1);

    // Cancel leaves the filter as it was.
    await chips.first().click();
    await page.getByTestId('filter-mode-query').click();
    await text.fill('split = val');
    await page.getByRole('button', { name: 'Cancel' }).click();
    await expectTotal(page, 5);

    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
});

test('switching a card from Query to Builder keeps the edit, and an empty Add Filter query goes back to the fields', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, 12);

    // Add Filter: Query with nothing typed, back to Builder, is the field list again.
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    await expect(page.getByTestId('filter-card-query')).toBeVisible();
    await page.getByTestId('filter-mode-builder').click();
    await expect(page.getByPlaceholder('Search fields to filter by...')).toBeVisible();
    await page.keyboard.press('Escape');

    await addByQuery(page, 'split = train');
    await expectTotal(page, 6);
    const chip = page.getByTestId('query-stage-chip').first();
    await expect(chip).toContainText('train');

    // Edit as text, switch to the builder: it shows the edited value, and Apply keeps it.
    await chip.click();
    await page.getByTestId('filter-mode-query').click();
    await page.getByTestId('filter-card-query').fill('split = val');
    await page.getByTestId('filter-mode-builder').click();
    await expect(page.getByTestId('filter-card-query')).toHaveCount(0);
    await page.getByTestId('filter-card-apply').click();
    await expect(chip).toContainText('val');
    await expectTotal(page, 6);

    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
});
