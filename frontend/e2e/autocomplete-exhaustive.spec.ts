import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal } from './helpers';

// The value typeahead (SearchableSelect, shared by the Simple Filters sidebar and the Query
// Builder) on a high-cardinality field: when the server's bounded value scan hits its budget it
// returns `{values: [], exhaustive: false}` (only the cached preview, which cannot hold a unique
// value). The UI used to render that as "No matches" — false for a value that exists — while
// Enter applied the typed text as an exact filter and found it. Now the empty state must say the
// search was not exhaustive and point at Enter, and Enter must still apply the typed value.
//
// Driven through the Query Builder: every string field gets the picker there, whereas the
// sidebar renders the 12-row fixture's low-cardinality strings as checkboxes. The budget case is
// simulated by mocking the endpoint — the fixture could never trip a 2 s budget for real.
test.use({ storageState: AUTH_STATE });

async function openNoteValuePicker(page: Page) {
    await gotoDataset(page);
    await expectTotal(page, 12);
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('note');
    await page.locator('[data-radix-popper-content-wrapper]').getByRole('button', { name: /^note/i }).first().click();
    // Default operator is `=` → single-mode picker, placeholder "Select note...".
    const input = page.getByPlaceholder(/note/i).last();
    await input.click();
    return input;
}

test('typeahead: a non-exhaustive empty result says so instead of "No matches", Enter still applies the value', async ({ page }) => {
    await page.route('**/fields/*/autocomplete', route =>
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ values: [], exhaustive: false }) }));
    const input = await openNoteValuePicker(page);
    // No cached value of `note` ('x') matches this prefix → the picker asks the server, which
    // the mock answers as a budget-tripped (non-exhaustive) scan.
    await input.fill('zzz-not-in-cache');
    const dropdown = page.locator('[data-searchable-dropdown]');
    const notice = dropdown.getByTestId('autocomplete-not-exhaustive');
    await expect(notice).toBeVisible({ timeout: 10_000 });
    await expect(notice).toContainText('Press Enter');
    await expect(dropdown.getByText('No matches', { exact: true })).toHaveCount(0);
    // Enter still applies the typed text as the filter value (the indexed exact-match path).
    await input.press('Enter');
    await expect(page.getByText('zzz-not-in-cache', { exact: true })).toBeVisible();
});

test('typeahead: an exhaustive empty result still reads "No matches"', async ({ page }) => {
    await page.route('**/fields/*/autocomplete', route =>
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ values: [], exhaustive: true }) }));
    const input = await openNoteValuePicker(page);
    await input.fill('zzz-not-in-cache');
    const dropdown = page.locator('[data-searchable-dropdown]');
    await expect(dropdown.getByText('No matches', { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(dropdown.getByTestId('autocomplete-not-exhaustive')).toHaveCount(0);
});
