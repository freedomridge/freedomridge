import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset } from './api';

// Tags used to be anonymous strings: nothing recorded when a tag was created or by whom, and bulk
// tagging didn't even stamp the sample. The Tag Management modal must now show "created <date> by
// <user>" for a tag applied through the app, and an honest "created: unknown" for a tag that arrived
// with the sample (ingest), rather than nothing.
test.use({ storageState: AUTH_STATE });

// A tag edit sets off a statistics job, which shows the jobs bar on every page open on the dataset:
// this spec uses its own copy of the fixture, not the shared one other specs are browsing.
let cookie = '';
let datasetId = 0;
test.beforeAll(async () => {
    cookie = await adminLogin();
    datasetId = await createFixtureDataset(cookie, `e2e-tag-registry-${Date.now()}`);
});
test.afterAll(async () => {
    if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
});

test('the tag modal shows when a tag was created and by whom; ingested tags read "unknown"', async ({ page }) => {
    const q = await (await page.request.post(`/api/datasets/${datasetId}/samples/query`, { data: { limit: 1, offset: 0 } })).json();
    const sid: number = (q.items ?? q.samples)[0].id;
    const tagged = await page.request.post(`/api/datasets/${datasetId}/samples/bulk-tag`, { data: { sample_ids: [sid], add_tags: ['dated-e2e'] } });
    expect(tagged.ok()).toBe(true);
    await gotoDataset(page, datasetId);
    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toBeVisible({ timeout: 10_000 });
    await page.getByTestId('grid-tag-button').click();
    await expect(page.getByText(/12 Samples/)).toBeVisible({ timeout: 10_000 });
    // Applied through the app: a real date and the user who did it (the fixture logs in as admin).
    const dated = page.locator('[data-testid="tag-row"][data-tag="dated-e2e"]');
    await expect(dated.getByTestId('tag-created')).toContainText(/^created .+ by admin$/);
    // Ingested with the samples (never bulk-tagged): registered on sight, dates unknown.
    const seed = page.locator('[data-testid="tag-row"][data-tag="seed"]');
    await expect(seed.getByTestId('tag-created')).toHaveText('created: unknown');
});
