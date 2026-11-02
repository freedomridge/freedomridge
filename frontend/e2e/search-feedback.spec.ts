import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal } from './helpers';
import { readState } from './api';

// Text and reverse-image search gave no sign they were running (the page's isSearching flag was
// set but never rendered) and silently asked for a fixed 100 results. Now the bar shows a
// "Searching…" badge while a search is in flight, the results count is a visible input shared by
// both searches, and the chip says N of M when fewer came back than asked for. The encoders are
// mocked: CI has no CLIP text tower and no outbound image fetch.
test.use({ storageState: AUTH_STATE });

async function fixtureSamples(page: Page, n: number) {
    const { datasetId } = readState();
    const q = await (await page.request.post(`/api/datasets/${datasetId}/samples/query`, { data: { limit: n, offset: 0 } })).json();
    return (q.items ?? q.samples).slice(0, n).map((s: { id: number }) => ({ id: s.id }));
}

test('reverse image search shows a Searching badge while it runs and says N of M on the chip', async ({ page }) => {
    const results = await fixtureSamples(page, 3);
    await page.route('**/api/search/by-image', async route => {
        await new Promise(r => setTimeout(r, 1500)); // slow enough to see the badge
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(results) });
    });
    await gotoDataset(page);
    await expectTotal(page, 12);
    await expect(page.getByTestId('search-result-limit')).toHaveValue('100');
    await page.getByTestId('image-search-trigger').click();
    const url = page.getByTestId('image-search-url');
    await url.fill('https://example.com/field.jpg');
    await url.press('Enter');
    const badge = page.getByTestId('search-in-progress');
    await expect(badge).toBeVisible({ timeout: 5_000 });
    await expect(badge).toHaveCount(0, { timeout: 10_000 });
    await expect(page.getByTestId('query-stage-chip').filter({ hasText: '3 of 100 requested' })).toBeVisible();
    await expectTotal(page, 3);
});

test('text search asks for the results count set in the bar', async ({ page }) => {
    const results = await fixtureSamples(page, 3);
    let requestedLimit: number | undefined;
    await page.route(u => u.pathname.endsWith('/api/search'), async route => {
        requestedLimit = route.request().postDataJSON()?.limit;
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(results) });
    });
    await gotoDataset(page);
    await expectTotal(page, 12);
    await page.getByTestId('search-result-limit').fill('3');
    const input = page.getByTestId('semantic-search-input');
    await input.fill('a river');
    await input.press('Enter');
    await expect(page.getByTestId('query-stage-chip').filter({ hasText: '“a river” (3)' })).toBeVisible({ timeout: 10_000 });
    expect(requestedLimit).toBe(3);
    await expectTotal(page, 3);
});
