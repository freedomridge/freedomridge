import { test, expect, type Page, type Route } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait, readState } from './api';
import { AUTH_STATE, expectTotal, gotoDataset } from './helpers';

test.use({ storageState: AUTH_STATE });

async function openHistogram(page: Page, field: string) {
    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('histogram');
    const histogram = page.getByTestId('panel-histogram');
    await histogram.getByTestId('histogram-field-select').selectOption(field);
    return histogram;
}

test('declared text is grouped by value; "Treat as number" bins it', async ({ page }) => {
    const cookie = await adminLogin();
    const dataset = await apiFetch(cookie, '/datasets', {
        method: 'POST', body: JSON.stringify({ name: `e2e-histogram-text-${Date.now()}` }),
    });
    try {
        // Numeric-looking text: the registry declares `serial` a string on first write.
        await apiFetch(cookie, `/datasets/${dataset.id}/samples`, {
            method: 'POST',
            body: JSON.stringify({ samples: Array.from({ length: 12 }, (_, i) => ({
                filepath: `s3://histogram-text/${i}.jpg`,
                metadata: { serial: String(100 + (i % 4) * 10) },
            })) }),
        });
        await page.goto(`/datasets/${dataset.id}`);
        await expectTotal(page, 12);
        const histogram = await openHistogram(page, 'serial');

        // Categories by default: one bar per serial.
        await expect(histogram.getByTitle(/^100 • 3 samples/)).toBeVisible();
        await expect(histogram.getByText(/Categories: 4/)).toBeVisible();

        const asNumber = histogram.getByTestId('histogram-as-number');
        await asNumber.check();
        await expect(histogram.getByText(/Bins: 8/)).toBeVisible();
        await expect(histogram.getByTitle(/^3 samples • 100 – 103\.75 • Click to Filter/)).toBeVisible();
        await expect(histogram.getByTitle(/^100 • 3 samples/)).toHaveCount(0);

        await asNumber.uncheck();
        await expect(histogram.getByTitle(/^100 • 3 samples/)).toBeVisible();
    } finally {
        await deleteDatasetAndWait(cookie, dataset.id);
    }
});

test('a broad histogram shows a labelled approximate preview until the exact result replaces it', async ({ page }) => {
    const cookie = await adminLogin();
    const { datasetId } = readState();
    // The 12-sample fixture is never broad: the real preview endpoint declines it.
    const real = await apiFetch(cookie, `/datasets/${datasetId}/histogram/preview`, {
        method: 'POST', body: JSON.stringify({ field: 'split' }),
    });
    expect(real).toEqual({ field: 'split', approximate: true, preview: { status: 'not_broad' } });

    // Stand in for a broad dataset: answer the preview from the server's shape, and hold the
    // exact request until the preview has been inspected.
    // The app POSTs its histogram requests, the field in the JSON body.
    const forSplit = (route: Route) => route.request().postDataJSON()?.field === 'split';
    await page.route(url => url.pathname.endsWith('/histogram/preview'), route => !forSplit(route) ? route.continue() : route.fulfill({ json: {
        field: 'split', total: 1000, missing: 0, distinct_total: 2, other_count: 0,
        buckets: [{ key: 'train', count: 520, label: 'train' }, { key: 'val', count: 480, label: 'val' }],
        approximate: true,
        preview: { status: 'ok', method: 'uniform_id_sample', id_range: [1, 200000], draws: 20000, matched: 1000, estimated_matching_rows: 10000 },
    } }));
    let release = () => {};
    const exactReleased = new Promise<void>(resolve => { release = resolve; });
    await page.route(url => url.pathname.endsWith('/histogram'), async route => {
        if (forSplit(route)) await exactReleased;
        await route.continue();
    });

    await gotoDataset(page);
    const histogram = await openHistogram(page, 'split');

    const banner = histogram.getByTestId('histogram-preview-banner');
    await expect(banner).toContainText('Approximate preview — not exact');
    await expect(banner).toContainText('small categories may be missing');
    await expect(histogram.getByTestId('histogram-preview-bar')).toHaveCount(2);
    await expect(histogram.getByTitle(/^Approximate • train • ≈52\.0% of the sample/)).toBeVisible();
    // Display-only: a preview bar never stages a filter.
    await histogram.getByTestId('histogram-preview-bar').first().click();
    await expect(histogram.getByTestId('histogram-filter-chip')).toHaveCount(0);
    await expect(histogram.getByTitle(/^train • \d+ samples/)).toHaveCount(0);

    release();
    await expect(histogram.getByTitle(/^train • 6 samples/)).toBeVisible();
    await expect(banner).toHaveCount(0);
    await expect(histogram.getByText('Total: 12')).toBeVisible();
});
