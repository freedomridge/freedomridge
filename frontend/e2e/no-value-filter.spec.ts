import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal, expandAllFields } from './helpers';
import { readState } from './api';

// Validates the Simple Filters "(no value)" toggle (is_null), including that it is only
// offered for fields that actually have missing values. In the fixture only `note` is
// partial (6 of 12), so exactly one toggle appears and applying it scopes to the 6
// samples without `note`.
test.use({ storageState: AUTH_STATE });

test('filter sidebar: "(no value)" shows only for partial fields and applies is_null', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, 12);

    const sidebar = page.getByTestId('panel-filters');
    const noValue = sidebar.locator('label', { hasText: '(no value)' });

    // Expand every field's section, so each one could show the toggle.
    await expandAllFields(page, 'note');

    // Only `note` has missing values — every other fixture field is fully populated.
    await expect(noValue).toHaveCount(1);

    // Applying it reaches the server as is_null → the 6 samples without `note`.
    await noValue.click();
    await expect(page.getByText('Unapplied filters')).toBeVisible();
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 6);

    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
});

test('filter: tags exists/not_exists respect the native array column (not metadata)', async ({ page }) => {
    const { datasetId } = readState();
    const total = async (filters: unknown) =>
        (await (await page.request.post(`/api/datasets/${datasetId}/samples/query`, { data: { filters, limit: 1 } })).json()).total;

    // Every fixture sample has tags (['seed', ...]). The bug: `exists` probed metadata ? 'tags',
    // but tags is a NATIVE TEXT[] column, so it matched 0 rows — while `in` worked. exists must
    // now equal the populated count, and exists + not_exists must partition the dataset.
    expect(await total({ logic: 'AND', filters: [{ field: 'tags', operator: 'exists', value: true }] })).toBe(12);
    expect(await total({ logic: 'AND', filters: [{ field: 'tags', operator: 'not_exists', value: true }] })).toBe(0);
    expect(await total({ logic: 'AND', filters: [{ field: 'tags', operator: 'in', value: ['seed'] }] })).toBe(12);
});

test('query builder: the tags value dropdown is portaled (visible) and keeps the editor open', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, 12);

    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('tags');
    await page.locator('[data-radix-popper-content-wrapper]').getByRole('button', { name: /^tags/i }).first().click();

    // Open the value picker. Its dropdown is portaled to <body> so the editor's overflow-y-auto
    // can't clip it (the reported bug: the list rendered but was cut off / not over the window).
    await page.getByPlaceholder(/tags/i).last().click();
    const dropdown = page.locator('[data-searchable-dropdown]');
    await expect(dropdown).toBeVisible({ timeout: 5_000 });

    // Selecting from the portaled list must NOT dismiss the editor (Radix onInteractOutside guard).
    await dropdown.getByRole('button').first().click();
    await expect(page.getByRole('button', { name: 'Apply' })).toBeVisible();
});
