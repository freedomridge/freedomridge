import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal } from './helpers';
import { readState } from './api';

// Find similar, in the filter bar: a "Searching…" sign while a search runs, the results count it asks for, a search
// within the filters (they stay) or over the whole dataset (it replaces them), "top N" on the search's chip, and the
// chip's include/exclude toggle. The encoders are mocked: CI has no CLIP text tower and no outbound image fetch.
// Fixture: 12 samples, idx 0-11, split=train for idx 0-5 and val for 6-11.
test.use({ storageState: AUTH_STATE });

const TRAIN = { logic: 'AND', filters: [{ field: 'split', operator: 'in', value: ['train'] }] };

async function fixtureSamples(page: Page, n: number, filters?: object) {
    const { datasetId } = readState();
    const q = await (await page.request.post(`/api/datasets/${datasetId}/samples/query`, { data: { limit: n, offset: 0, filters } })).json();
    return (q.items ?? q.samples).slice(0, n).map((s: { id: number }) => ({ id: s.id }));
}

async function searchText(page: Page, prompt: string) {
    await page.getByTestId('search-open').click();
    const input = page.getByTestId('semantic-search-input');
    await input.fill(prompt);
    await input.press('Enter');
}

test('reverse image search shows a Searching sign while it runs and says how many it found on the chip', async ({ page }) => {
    const results = await fixtureSamples(page, 3);
    await page.route('**/api/search/by-image', async route => {
        await new Promise(r => setTimeout(r, 1500)); // slow enough to see the sign
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(results) });
    });
    await gotoDataset(page);
    await expectTotal(page, 12);
    await page.getByTestId('search-open').click();
    await expect(page.getByTestId('search-result-limit')).toHaveValue('100');
    const url = page.getByTestId('image-search-url');
    await url.fill('https://example.com/field.jpg');
    await url.press('Enter');
    const sign = page.getByTestId('search-in-progress');
    await expect(sign).toBeVisible({ timeout: 5_000 });
    await expect(sign).toHaveCount(0, { timeout: 10_000 });
    const chip = page.getByTestId('query-stage-chip').filter({ hasText: 'Similar to 🖼' });
    await expect(chip).toContainText('(3 found)');
    await expect(chip.locator('[title]').first()).toHaveAttribute('title', /top 3 of 100 requested/);
    await expectTotal(page, 3);
});

// A short answer the server could not finish checking (its check for matches the index left behind ran out of
// time) says so on the chip; before, "3 found" read as "only 3 match".
test('a search the server could not finish checking says more may match', async ({ page }) => {
    const results = await fixtureSamples(page, 3);
    await page.route(u => u.pathname.endsWith('/api/search'), async route => {
        await route.fulfill({
            status: 200, contentType: 'application/json', headers: { 'X-Search-Incomplete': '1' }, body: JSON.stringify(results),
        });
    });
    await gotoDataset(page);
    await expectTotal(page, 12);
    await searchText(page, 'a river');
    const chip = page.getByTestId('query-stage-chip').first();
    await expect(chip).toContainText('(3 found, more may match)', { timeout: 10_000 });
    await expectTotal(page, 3);
});

test('text search asks for the results count set in the panel; the chip runs it again for another count', async ({ page }) => {
    const results = await fixtureSamples(page, 3);
    const limits: number[] = [];
    await page.route(u => u.pathname.endsWith('/api/search'), async route => {
        limits.push(route.request().postDataJSON()?.limit);
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(results) });
    });
    await gotoDataset(page);
    await expectTotal(page, 12);
    await page.getByTestId('search-open').click();
    await page.getByTestId('search-result-limit').fill('3');
    const input = page.getByTestId('semantic-search-input');
    await input.fill('a river');
    await input.press('Enter');
    await expect(page.getByTestId('query-stage-chip').filter({ hasText: 'Similar to “a river”' })).toBeVisible({ timeout: 10_000 });
    expect(limits).toEqual([3]);
    await expectTotal(page, 3);

    // "top N" on the chip runs the same search again.
    await page.getByTestId('search-chip-limit').selectOption('500');
    await expect.poll(() => limits).toEqual([3, 500]);
    await expect(page.getByTestId('query-stage-chip').filter({ hasText: 'Similar to “a river”' })).toBeVisible();
});

test('a search stays within the filters, or searches the whole dataset and replaces them', async ({ page }) => {
    const results = await fixtureSamples(page, 3, TRAIN);  // what a search within split=train can return
    const bodies: { filters?: unknown }[] = [];
    await page.route(u => u.pathname.endsWith('/api/search'), async route => {
        bodies.push(route.request().postDataJSON());
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(results) });
    });
    await gotoDataset(page);
    await expectTotal(page, 12);
    // With no filters there is nothing to search within: no choice.
    await page.getByTestId('search-open').click();
    await expect(page.getByTestId('search-scope-filtered')).toHaveCount(0);
    await page.keyboard.press('Escape');

    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    await page.getByTestId('filter-card-query').fill('split = train');
    await page.getByTestId('filter-card-apply').click();
    await expectTotal(page, 6);

    // Within your filters (the default): the search sends them, and they stay as a chip beside the search's.
    await page.getByTestId('search-open').click();
    await expect(page.getByTestId('search-scope-filtered')).toHaveAttribute('aria-pressed', 'true');
    await page.keyboard.press('Escape');
    await searchText(page, 'a tractor');
    const chips = page.getByTestId('query-stage-chip');
    await expect(chips.filter({ hasText: 'within filters' })).toBeVisible({ timeout: 10_000 });
    await expect(chips).toHaveCount(2);
    expect(bodies[0].filters).toBeTruthy();
    await expectTotal(page, 3);

    // A new search within the filters replaces the earlier search, not the filters.
    await searchText(page, 'a barn');
    await expect(chips.filter({ hasText: 'Similar to “a barn”' })).toBeVisible({ timeout: 10_000 });
    await expect(chips).toHaveCount(2);

    // Whole dataset: no filters sent, and the results replace them.
    await page.getByTestId('search-open').click();
    await page.getByTestId('search-scope-all').click();
    const input = page.getByTestId('semantic-search-input');
    await input.fill('a field');
    await input.press('Enter');
    await expect(chips.filter({ hasText: 'Similar to “a field”' })).toBeVisible({ timeout: 10_000 });
    await expect(chips).toHaveCount(1);
    expect(bodies[2].filters).toBeUndefined();
    await expectTotal(page, 3);

    // A filter added after it stays when the chip's "top N" runs the whole-dataset search again.
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    await page.getByTestId('filter-card-query').fill('split = train');
    await page.getByTestId('filter-card-apply').click();
    await expect(chips).toHaveCount(2);
    await page.getByTestId('search-chip-limit').selectOption('500');
    await expect.poll(() => bodies.length).toBe(4);
    expect(bodies[3].filters).toBeUndefined();
    await expect(chips).toHaveCount(2);
    await expect(page.getByTestId('search-chip-limit')).toHaveValue('500');
});

test('a search chip toggles to exclude its results', async ({ page }) => {
    const results = await fixtureSamples(page, 3);
    await page.route(u => u.pathname.endsWith('/api/search'), async route => {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(results) });
    });
    await gotoDataset(page);
    await expectTotal(page, 12);
    await searchText(page, 'a river');
    const chip = page.getByTestId('query-stage-chip').first();
    await expect(chip).toContainText('Similar to “a river”', { timeout: 10_000 });
    await expectTotal(page, 3);
    // It excluded nothing before: the toggle changed the label and left the ids included. (Clicked at its left
    // end, away from its "top N".)
    await chip.click({ position: { x: 12, y: 10 } });
    await expect(chip).toContainText('Exclude:');
    await expectTotal(page, 9);
    await chip.click({ position: { x: 12, y: 10 } });
    await expectTotal(page, 3);
});
