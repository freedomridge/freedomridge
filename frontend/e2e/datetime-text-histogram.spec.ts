import { test, expect } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expectTotal } from './helpers';

test.use({ storageState: AUTH_STATE });

// A time-named field registered as text (its first value wasn't a datetime) keeps time bars in the histogram, but its
// filters compare text, so a bar can't be staged as a filter of its instants: it says how to make it one instead.
const FIELD = 'seen_at';
let cookie = '';
let datasetId = 0;

test.beforeAll(async () => {
    cookie = await adminLogin();
    datasetId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-text-times-${Date.now()}` }) })).id;
    const values = ['unknown', '2026-01-01T08:00:00Z', '2026-01-01T10:00:00+02:00', '2026-01-02 09:30:00', '2026-01-03T12:00:00Z'];
    for (const [index, value] of values.entries()) {  // one at a time, so the first registers the field as text
        await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
            method: 'POST',
            body: JSON.stringify({ samples: [{ filepath: `s3://e2e-text-times/${index}.jpg`, metadata: { [FIELD]: value } }] }),
        });
    }
    await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
    const schema: Array<{ name: string; type: string }> = await apiFetch(cookie, `/datasets/${datasetId}/schema`);
    expect(['string', 'enum']).toContain(schema.find(field => field.name === FIELD)?.type);  // text, not datetime
});

test.afterAll(async () => {
    if (datasetId) await deleteDatasetAndWait(cookie, datasetId);
});

test("a text field's time bars are not offered as filters", async ({ page }) => {
    await page.goto(`/datasets/${datasetId}`);
    await expectTotal(page, 5);
    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('histogram');
    const histogram = page.getByTestId('panel-histogram');
    await histogram.getByTestId('histogram-field-select').selectOption(FIELD);
    const bars = histogram.getByTitle(/ UTC • Declare seen_at a datetime \(Dataset Settings\) to filter by these times$/);
    await expect(bars.first()).toBeVisible({ timeout: 20_000 });
    await expect(bars.first()).toBeDisabled();
    await expect(histogram.getByTitle(/Click to Filter$/)).toHaveCount(0);
    await expect(histogram.getByTestId('histogram-filter-chip')).toHaveCount(0);
});
