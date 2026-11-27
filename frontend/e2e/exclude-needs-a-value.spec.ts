import zlib from 'node:zlib';
import { test, expect, type Browser, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { stamp } from './access-helpers';

// One rule for a missing value, everywhere (contract g7, rule 1.6 and section 7.5): an excluded place shows only the
// samples that have a location. On a dataset without objects, a map region chip switched to exclude counts the located
// samples outside the region, never the sample with no location; switched back, it counts those inside again. The
// region comes in an f= link as the app writes a drawn one (a `map_region` stage), so the chip is the one a drawing
// makes without depending on the draw tool; the switch is the chip's own click. Each count is the page's sub-header,
// and the excluded chip's address, opened in a fresh browser, counts the same.
//
// Written from the contract, not from the code. The count inside the region is asked first, as main answers it, so a
// failure on main is the excluded count's: main counts the sample with no location among the excluded (4, not 3).
test.use({ storageState: AUTH_STATE });

type Lonlat = [number, number];

// A square about longitude 10, latitude 20 (an open ring, as the draw tool sends one).
const SQUARE: Lonlat[] = [[9, 19], [11, 19], [11, 21], [9, 21]];
const INSIDE: Lonlat[] = [[10, 20], [10.5, 20.5]];
const OUTSIDE: Lonlat[] = [[30, 20], [10, 40], [-50, -10]];

/** An f= link's value: stages as the app encodes them (version 1, raw deflate, base64url without padding). */
const encodeStages = (stages: unknown[]) => `1.${zlib.deflateRawSync(Buffer.from(JSON.stringify({ stages }))).toString('base64url')}`;
const regionStage = { type: 'map_region', description: 'Map Region', filter: { field: 'location', operator: 'geo_polygon', value: SQUARE } };

/** The dataset: the samples inside the square, those outside it, and one with no location. */
async function makeDataset(cookie: string): Promise<number> {
    const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-exclude-needs-a-value-${stamp()}` }) });
    const located = [...INSIDE, ...OUTSIDE].map(([lon, lat], i) => ({
        filepath: `s3://e2e-exclude-needs-a-value/${i}.jpg`,
        location: { type: 'Point', coordinates: [lon, lat] },
    }));
    const samples = [...located, { filepath: 's3://e2e-exclude-needs-a-value/nowhere.jpg' }];
    const { ids } = await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    expect(ids).toHaveLength(INSIDE.length + OUTSIDE.length + 1);
    return ds.id;
}

/** The operator of the region in the page's address (its f= stages decoded), or null. */
function operatorInAddress(page: Page): string | null {
    const value = new URL(page.url()).searchParams.get('f');
    if (!value || !value.startsWith('1.')) return null;
    try {
        const { stages } = JSON.parse(zlib.inflateRawSync(Buffer.from(value.slice(2), 'base64url')).toString()) as {
            stages: Array<{ filter?: { operator?: string } }>;
        };
        return stages.length === 1 ? stages[0].filter?.operator ?? null : null;
    } catch {
        return null;
    }
}

/** The page's address once it carries the region with `operator` (the address is written after the entry). */
async function addressWith(page: Page, operator: string): Promise<string> {
    await expect.poll(() => operatorInAddress(page), { timeout: 15_000, message: 'the address carries the chip' }).toBe(operator);
    return page.url();
}

/** The address opened in a new browser context, signed in and nothing else kept: the count it shows. */
async function expectTotalOpened(browser: Browser, address: string, n: number) {
    const context = await browser.newContext({ storageState: AUTH_STATE });
    try {
        const page = await context.newPage();
        await page.goto(address);
        await expectTotal(page, n);
    } finally {
        await context.close();
    }
}

test('a map region chip switched to exclude counts the samples outside it that have a location, not the one with none', async ({ page, browser }) => {
    test.setTimeout(120_000);
    const cookie = await adminLogin();
    const id = await makeDataset(cookie);
    try {
        const link = encodeStages([regionStage]);
        await page.goto(`/datasets/${id}?f=${link}`);
        const chip = page.getByTestId('query-stage-chip').filter({ hasText: 'Map Region' });
        await expect(chip).toHaveCount(1);
        await expect(chip).not.toContainText('Exclude');
        await expectTotal(page, INSIDE.length);  // asked first: the samples inside, as main counts them

        await chip.click();
        await expect(chip).toContainText('Exclude Map Region');
        await expectTotal(page, OUTSIDE.length);
        await expect(page.getByTestId('grid-cell')).toHaveCount(OUTSIDE.length);
        await expectTotalOpened(browser, await addressWith(page, 'geo_polygon_exclude'), OUTSIDE.length);

        await chip.click();
        await expect(chip).not.toContainText('Exclude');
        await expectTotal(page, INSIDE.length);
        await expect(page.getByTestId('grid-cell')).toHaveCount(INSIDE.length);
        await expectTotalOpened(browser, await addressWith(page, 'geo_polygon'), INSIDE.length);
    } finally {
        await deleteDatasetAndWait(cookie, id);
    }
});
