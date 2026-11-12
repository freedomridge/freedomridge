import { test, expect, type Page } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expandField, expectTotal, fillStable } from './helpers';

test.use({ storageState: AUTH_STATE });

// A field declared `datetime` compares moments: a value's offset is applied, and no offset means UTC. Its values here
// are written with different offsets, so their text order (E, D, B, C, A) isn't their time order (A = B, E, C, D).
// The filter card's picker, the sidebar's date slider and the histogram's bars send and show UTC, and each filter
// shows exactly the samples whose moment it covers.
const SAMPLES: Record<string, { text: string; utc: string }> = {
    A: { text: '2026-01-01T10:00:00+02:00', utc: '2026-01-01T08:00:00.000000' },
    B: { text: '2026-01-01T08:00:00Z', utc: '2026-01-01T08:00:00.000000' },
    C: { text: '2026-01-01T09:30:00Z', utc: '2026-01-01T09:30:00.000000' },
    // The latest, half a millisecond past 11:00 UTC: finer than the slider's milliseconds.
    D: { text: '2026-01-01T06:00:00.0005-05:00', utc: '2026-01-01T11:00:00.000500' },
    E: { text: '2026-01-01 08:30:00', utc: '2026-01-01T08:30:00.000000' },
};
const FIELD = 'observed_at';
// A 1x1 PNG for every tile, so each keeps its <img> (and its sample id) instead of failing to load.
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64');

let cookie = '';
let datasetId = 0;
const nameOf: Record<number, string> = {};

test.beforeAll(async () => {
    cookie = await adminLogin();
    datasetId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-datetime-instants-${Date.now()}` }) })).id;
    const names = Object.keys(SAMPLES);
    const { ids } = await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: names.map(name => ({
            filepath: `s3://e2e-datetime-instants/${name}.jpg`,
            metadata: { [FIELD]: SAMPLES[name].text },
        })) }),
    });
    ids.forEach((id: number, i: number) => { nameOf[id] = names[i]; });
    await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
    const schema: Array<{ name: string; type: string }> = await apiFetch(cookie, `/datasets/${datasetId}/schema`);
    expect(schema.find(field => field.name === FIELD)?.type).toBe('datetime');
});

test.afterAll(async () => {
    if (datasetId) await deleteDatasetAndWait(cookie, datasetId);
});

test.beforeEach(async ({ page }) => {
    await page.route('**/samples/*/media**', route => route.fulfill({ contentType: 'image/png', body: PNG }));
    await page.goto(`/datasets/${datasetId}`);
    await expectTotal(page, 5);
});

// The samples the grid shows, by name, from each tile's media address.
async function shownSamples(page: Page): Promise<string[]> {
    const sources = await page.getByTestId('grid-cell').locator('img[src*="/media"]')
        .evaluateAll(images => images.map(image => image.getAttribute('src') ?? ''));
    return sources.map(src => nameOf[Number(/\/samples\/(\d+)\/media/.exec(src)?.[1])] ?? src).sort();
}

test("the filter card's UTC time is compared with each sample's moment, not its text", async ({ page }) => {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill(FIELD);
    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.getByRole('button', { name: new RegExp(`^${FIELD}`) }).first().click();
    await editor.getByTestId('filter-rule').locator('select').nth(1).selectOption('>=');
    await fillStable(editor.getByLabel('Date and time (UTC)'), '2026-01-01T09:00');
    await expect(editor.getByText('UTC', { exact: true })).toBeVisible();
    await editor.getByTestId('filter-card-apply').click();

    // From 09:00 UTC: C (09:30 UTC) and D (11:00 UTC). As text, A ("…T10:00:00+02:00", 08:00 UTC) would be in and
    // D ("…T06:00:00.0005-05:00") out.
    await expectTotal(page, 2);
    await expect.poll(() => shownSamples(page)).toEqual(['C', 'D']);
    await expect(page.getByTestId('query-stage-chip')).toHaveText(`${FIELD} ≥ 2026-01-01 09:00 UTC`);
});

test.describe('the sidebar date slider', () => {
    test.beforeEach(async ({ page }) => {
        await expandField(page, FIELD);
        const section = page.getByTestId('panel-filters').getByTestId(`field-section-${FIELD}`);
        // Its ends are the earliest and latest moments, 08:00 UTC and 11:00:00.0005 UTC (to its millisecond),
        // labelled with their UTC dates.
        const handle = section.getByRole('slider').first();
        await expect(handle).toHaveAttribute('aria-valuemin', String(Date.UTC(2026, 0, 1, 8)));
        await expect(handle).toHaveAttribute('aria-valuemax', String(Date.UTC(2026, 0, 1, 11)));
        await expect(section).toContainText('Jan 1, 2026UTCJan 1, 2026');
    });

    test('a moved lower handle is the only bound: the latest sample, past the upper end, stays', async ({ page }) => {
        const section = page.getByTestId('panel-filters').getByTestId(`field-section-${FIELD}`);
        const lower = section.getByRole('slider').first();
        await lower.focus();
        await page.keyboard.press('ArrowRight');
        await expect(lower).toHaveAttribute('aria-valuenow', String(Date.UTC(2026, 0, 1, 11)));
        await page.getByTestId('apply-filters').click();

        // From 11:00:00.000 UTC with no upper bound: D, at 11:00:00.0005 UTC. An upper bound at the slider's end
        // (11:00:00.000) would leave out D, and as text no value is after "2026-01-01T11:00:00.000Z".
        await expectTotal(page, 1);
        await expect.poll(() => shownSamples(page)).toEqual(['D']);
        await expect(page.getByTestId('query-stage-chip')).toHaveText(`${FIELD} ≥ 2026-01-01 11:00:00.000 UTC`);
    });

    test('a moved upper handle is the only bound, compared as moments', async ({ page }) => {
        const section = page.getByTestId('panel-filters').getByTestId(`field-section-${FIELD}`);
        const upper = section.getByRole('slider').last();
        await upper.focus();
        await page.keyboard.press('ArrowLeft');
        await expect(upper).toHaveAttribute('aria-valuenow', String(Date.UTC(2026, 0, 1, 8)));
        await page.getByTestId('apply-filters').click();

        // Up to 08:00:00.000 UTC: A and B. As text, D ("…T06…") and E ("2026-01-01 08:30:00") would be in, A and B out.
        await expectTotal(page, 2);
        await expect.poll(() => shownSamples(page)).toEqual(['A', 'B']);
        await expect(page.getByTestId('query-stage-chip')).toHaveText(`${FIELD} ≤ 2026-01-01 08:00:00.000 UTC`);
    });
});

// A bar's label as UTC text with six fraction digits, which orders like its moment.
const micro = (label: string) => (label.length === 19 ? `${label}.` : label).padEnd(26, '0');

for (const which of ['first', 'last'] as const) {
    test(`a histogram bar's click shows exactly the bar's samples (the ${which} bar)`, async ({ page }) => {
        await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('histogram');
        const histogram = page.getByTestId('panel-histogram');
        await histogram.getByTestId('histogram-field-select').selectOption(FIELD);
        const bars = histogram.getByTitle(/^\d+ samples? • \S+ – \S+ UTC • Click to Filter$/);
        await expect(bars.first()).toBeVisible({ timeout: 20_000 });
        await expect(histogram.getByTestId('histogram-utc')).toHaveText('Times in UTC');
        const bar = which === 'first' ? bars.first() : bars.last();
        const title = (await bar.getAttribute('title')) ?? '';
        const [, count, start, end] = /^(\d+) samples? • (\S+) – (\S+) UTC • /.exec(title) ?? [];
        // The samples whose moment is within the bar's labels (its first and last microsecond, UTC). The first bar
        // holds A, whose text sorts after the bar's end; the last holds D, whose text sorts before its start.
        const expected = Object.keys(SAMPLES).filter(name => SAMPLES[name].utc >= micro(start) && SAMPLES[name].utc <= micro(end));
        expect(expected).toContain(which === 'first' ? 'A' : 'D');
        expect(expected).toHaveLength(Number(count));

        await bar.click();
        await expect(histogram.getByTestId('histogram-filter-chip')).toContainText(`${start} - ${end} UTC`);
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, Number(count));
        await expect.poll(() => shownSamples(page)).toEqual(expected);
    });
}
