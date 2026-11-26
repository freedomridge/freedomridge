import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal, fillStable } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { stamp } from './access-helpers';
import { loadRegionFixture, regionsNotOptedIn } from './regions-fixture';

// The map fits samples on both sides of longitude 180 (docs/MAP_REQUIREMENTS.md #7, beside the antimeridian rules of
// #13). map_extent answered the smallest and largest longitude: for samples at 179 and -179 a box from -179 to 179, and
// "Zoom to Data", and every other fit to an extent, zoomed out to the world for data a few degrees wide. By the
// contract, samples that fit in half the globe across the antimeridian get the box across it (w greater than e), and
// every fit to an extent fits it: the samples all shown, as close as the same samples elsewhere on the globe (the same
// padding and zoom limits), with the antimeridian inside the view. Samples wider than half the globe either way (over
// several continents) keep today's box and today's view. Nothing else about the map changes.
//
// Written from the contract, not from the code. The map's view is read as map-dateline.spec.ts and regions-ui.spec.ts
// read it: the bbox the map asks for its points with, its longitudes unwrapped (a view across the antimeridian ends past
// 180, docs/MAP_REQUIREMENTS.md #13). Below the zoom where every point shows (12) that bbox is a tile of a fixed grid
// that holds the view, so these tests see the view to within a tile: "the view holds every sample" is read as the tile
// holding them, with the map at the zoom the same samples get elsewhere; the pair 100 m apart is fitted past zoom 12,
// where the bbox is the view itself. "Of the order of the data's width" is read as under 30 degrees.
//
// The fits driven here: "Zoom to Data" (also after a reload), selecting samples in the grid (#12), and applying a region
// chip (needs the region fixture: FRIDGE_E2E_LOAD_REGIONS=1). Not driven: the fit to a drawn polygon (drawing one across
// the antimeridian needs clicks on the canvas at screen positions this test can't read) and a bubble's click (a bubble
// carries its own extent from map_points, in the view's frame across the antimeridian: #14, test_map_dateline.py).
test.use({ storageState: AUTH_STATE });

type Lonlat = [number, number];
type Bbox = [number, number, number, number];
type MapView = { dataset: number; zoom: number; bbox: Bbox };

// Four samples about three degrees wide, a few degrees either side of 180 (Fiji), and the same four about longitude 10,
// at the same latitudes: fitted alike, they get the same zoom.
const ACROSS: Lonlat[] = [[178.5, -17.5], [179.3, -16.6], [-179.4, -17.9], [-178.6, -16.9]];
const ELSEWHERE: Lonlat[] = [[8.5, -17.5], [9.3, -16.6], [10.6, -17.9], [11.4, -16.9]];

async function makeDataset(cookie: string, name: string, points: Lonlat[]): Promise<number> {
    const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `${name}-${stamp()}`, is_global: true }) });
    const samples = points.map(([lon, lat], i) => ({
        filepath: `s3://${name}/${i}.jpg`,
        location: { type: 'Point', coordinates: [lon, lat] },
    }));
    await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    return ds.id;
}

/** Every view the map asks for its points with, as it asks. */
function recordViews(page: Page): MapView[] {
    const seen: MapView[] = [];
    page.on('request', request => {
        const match = new URL(request.url()).pathname.match(/\/datasets\/(\d+)\/map_points$/);
        if (request.method() !== 'POST' || !match) return;
        const body = request.postDataJSON() ?? {};
        if (Array.isArray(body.bbox)) seen.push({ dataset: Number(match[1]), zoom: body.zoom, bbox: body.bbox });
    });
    return seen;
}

/** Wait until the map has asked nothing new for a while (it asks 250 ms after it stops moving). */
async function settled(views: () => MapView[]) {
    let last = -1;
    await expect.poll(() => {
        const now = views().length;
        const still = now === last && now > 0;
        last = now;
        return still;
    }, { intervals: [1_500], timeout: 30_000 }).toBe(true);
}

/** The map in the right-hand panel, settled on a view it asked for after `from` views; false without a Mapbox token. */
async function showMap(page: Page, views: MapView[], datasetId: number, total: number): Promise<boolean> {
    const own = () => views.filter(view => view.dataset === datasetId);
    const from = own().length;
    await expectTotal(page, total);
    const map = page.getByTestId('panel-map');
    const zoomToData = map.getByRole('button', { name: 'Zoom to Data' });
    const noToken = map.getByText(/NEXT_PUBLIC_MAPBOX_TOKEN/);
    // A reload keeps the layout this browser saved, the map showing; a fresh page shows the embeddings there.
    const kept = await zoomToData.or(noToken).first().waitFor({ timeout: 3_000 }).then(() => true, () => false);
    if (!kept) await page.getByTestId('panel-embeddings').getByTestId('panel-type-select').selectOption('map');
    await expect(noToken.or(zoomToData).first()).toBeVisible({ timeout: 20_000 });
    if (await noToken.isVisible()) return false;
    await expect.poll(() => own().length, { timeout: 20_000 }).toBeGreaterThan(from);
    await settled(own);
    return true;
}

async function openMap(page: Page, views: MapView[], datasetId: number, total: number): Promise<boolean> {
    await page.goto(`/datasets/${datasetId}`);
    return showMap(page, views, datasetId, total);
}

/** Every answer map_extent gives the page, to say in a failure what the server answered. */
function recordExtents(page: Page): unknown[] {
    const seen: unknown[] = [];
    page.on('response', async response => {
        if (!new URL(response.url()).pathname.endsWith('/map_extent')) return;
        seen.push(await response.json().catch(() => null));
    });
    return seen;
}

/** The view the map rests on after something that moves it: time for the move to start, then nothing new asked. */
async function restingView(page: Page, views: MapView[], datasetId: number): Promise<MapView> {
    await page.waitForTimeout(2_500);
    const own = () => views.filter(view => view.dataset === datasetId);
    await settled(own);
    return own().at(-1)!;
}

/** Press "Zoom to Data": what map_extent answered, and the view the map rests on. */
async function zoomToData(page: Page, views: MapView[], datasetId: number) {
    const answered = page.waitForResponse(response => response.request().method() === 'POST'
        && new URL(response.url()).pathname.endsWith(`/datasets/${datasetId}/map_extent`));
    await page.getByTestId('panel-map').getByRole('button', { name: 'Zoom to Data' }).click();
    const answer = await (await answered).json();
    return { answer, view: await restingView(page, views, datasetId) };
}

/** The view holds every sample (a longitude counted on any world copy), and is about the data's width. */
function expectFitted(view: MapView, points: Lonlat[], context: string, widest = 30) {
    const [west, south, east, north] = view.bbox;
    const where = `${context}; the map asked for ${JSON.stringify(view)}`;
    for (const [lon, lat] of points) {
        const inside = [lon, lon + 360, lon - 360].some(x => x >= west && x <= east) && lat >= south && lat <= north;
        expect(inside, `[${lon}, ${lat}] in the view: ${where}`).toBe(true);
    }
    expect(east - west, `about the data's width, not the world's: ${where}`).toBeLessThan(widest);
}

function expectAcross(view: MapView, context: string) {
    const [west, , east] = view.bbox;
    expect(west < 180 && east > 180, `the antimeridian inside the view: ${context}; the map asked for ${JSON.stringify(view)}`).toBe(true);
}

test('"Zoom to Data" fits a cluster far from 180, also after a reload', async ({ page }) => {
    test.setTimeout(150_000);
    const cookie = await adminLogin();
    const id = await makeDataset(cookie, 'e2e-extent-elsewhere', ELSEWHERE);
    const views = recordViews(page);
    try {
        test.skip(!(await openMap(page, views, id, ELSEWHERE.length)), 'No Mapbox token configured');
        let { answer, view } = await zoomToData(page, views, id);
        expectFitted(view, ELSEWHERE, `map_extent answered ${JSON.stringify(answer)}`);
        expect(view.zoom).toBeGreaterThan(1);

        await page.reload();
        await showMap(page, views, id, ELSEWHERE.length);
        ({ answer, view } = await zoomToData(page, views, id));
        expectFitted(view, ELSEWHERE, `after a reload, map_extent answered ${JSON.stringify(answer)}`);
    } finally {
        await deleteDatasetAndWait(cookie, id);
    }
});

test('"Zoom to Data" fits samples either side of 180 as close as the same samples elsewhere, also after a reload', async ({ page }) => {
    test.setTimeout(180_000);
    const cookie = await adminLogin();
    const elsewhere = await makeDataset(cookie, 'e2e-extent-same-elsewhere', ELSEWHERE);
    const across = await makeDataset(cookie, 'e2e-extent-across', ACROSS);
    const views = recordViews(page);
    try {
        test.skip(!(await openMap(page, views, elsewhere, ELSEWHERE.length)), 'No Mapbox token configured');
        const there = (await zoomToData(page, views, elsewhere)).view;
        expectFitted(there, ELSEWHERE, 'the same samples about longitude 10');

        await openMap(page, views, across, ACROSS.length);
        let { answer, view } = await zoomToData(page, views, across);
        const context = `map_extent answered ${JSON.stringify(answer)}`;
        expectFitted(view, ACROSS, context);
        expectAcross(view, context);
        expect(view.zoom, `as close as about longitude 10: ${context}`).toBe(there.zoom);

        await page.reload();
        await showMap(page, views, across, ACROSS.length);
        ({ answer, view } = await zoomToData(page, views, across));
        const again = `after a reload, map_extent answered ${JSON.stringify(answer)}`;
        expectFitted(view, ACROSS, again);
        expectAcross(view, again);
        expect(view.zoom, `as close as about longitude 10: ${again}`).toBe(there.zoom);
    } finally {
        await deleteDatasetAndWait(cookie, across);
        await deleteDatasetAndWait(cookie, elsewhere);
    }
});

// The United States, Europe and Australia: the widest empty stretch, 103 degrees over Asia, avoids 180 but is narrower
// than 180, so map_extent answers today's box, -122 to 151, and the map shows what it shows today. At that width the
// view is the whole world at zoom 1 or below, one tile, whichever way it is centred: the pin holds the box the page
// fits and the whole-world view.
const THREE_CONTINENTS: Lonlat[] = [[-122, 38], [-74, 41], [2, 49], [13, 52], [116, -32], [151, -34]];

test('"Zoom to Data" on samples over three continents fits today\'s box, as before', async ({ page }) => {
    test.setTimeout(120_000);
    const cookie = await adminLogin();
    const id = await makeDataset(cookie, 'e2e-extent-continents', THREE_CONTINENTS);
    const views = recordViews(page);
    try {
        test.skip(!(await openMap(page, views, id, THREE_CONTINENTS.length)), 'No Mapbox token configured');
        const { answer, view } = await zoomToData(page, views, id);
        expect(answer).toEqual({ w: -122, s: -34, e: 151, n: 52, count: 6 });
        expect(view.bbox, `the map asked for ${JSON.stringify(view)}`).toEqual([-180, -85, 180, 85]);
        expect(view.zoom, `the map asked for ${JSON.stringify(view)}`).toBeLessThanOrEqual(1);
    } finally {
        await deleteDatasetAndWait(cookie, id);
    }
});

// Two samples about 85 m apart: fitted past zoom 12, where the map asks for its view itself, not a tile.
for (const [place, pair] of [
    ['across 180', [[179.9996, -17.0002], [-179.9996, -16.9998]]],
    ['about longitude 10', [[9.9996, -17.0002], [10.0004, -16.9998]]],
] as Array<[string, Lonlat[]]>) {
    test(`"Zoom to Data" flies in to a pair of samples ${place}`, async ({ page }) => {
        test.setTimeout(120_000);
        const cookie = await adminLogin();
        const id = await makeDataset(cookie, 'e2e-extent-pair', pair);
        const views = recordViews(page);
        try {
            test.skip(!(await openMap(page, views, id, pair.length)), 'No Mapbox token configured');
            const { answer, view } = await zoomToData(page, views, id);
            const context = `map_extent answered ${JSON.stringify(answer)}`;
            expect(view.zoom, `past the zoom where every point shows: ${context}; the map asked for ${JSON.stringify(view)}`).toBeGreaterThanOrEqual(12);
            expectFitted(view, pair, context, 0.05);
        } finally {
            await deleteDatasetAndWait(cookie, id);
        }
    });
}

/**
 * Select every sample in the grid with Select All: one change of the selection, so one fit. (Ticked one by one, each
 * tick fits the map, and a view the map has shown before sends no request: the last request needn't be the last view.)
 */
async function selectAll(page: Page, count: number) {
    await expect(page.getByTestId('grid-cell')).toHaveCount(count, { timeout: 15_000 });
    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toContainText(`${count} selected`);
}

for (const [place, points] of [['far from 180', ELSEWHERE], ['either side of 180', ACROSS]] as Array<[string, Lonlat[]]>) {
    test(`selecting samples ${place} in the grid fits the map to them`, async ({ page }) => {
        test.setTimeout(120_000);
        const cookie = await adminLogin();
        const id = await makeDataset(cookie, 'e2e-extent-select', points);
        const views = recordViews(page);
        try {
            test.skip(!(await openMap(page, views, id, points.length)), 'No Mapbox token configured');
            await selectAll(page, points.length);
            const view = await restingView(page, views, id);
            expectFitted(view, points, `${points.length} samples selected`);
            if (points === ACROSS) expectAcross(view, `${points.length} samples selected`);
        } finally {
            await deleteDatasetAndWait(cookie, id);
        }
    });
}

test('applying a region chip across 180 fits the map to its samples', async ({ page }) => {
    test.skip(regionsNotOptedIn !== null, regionsNotOptedIn ?? '');
    test.setTimeout(180_000);
    const cookie = await adminLogin();
    const notLoaded = await loadRegionFixture(cookie);
    test.skip(notLoaded !== null, notLoaded ?? '');
    // The fixture's Fiji (FJ) is two rectangles, 177 to 180 and -180 to -178, latitudes -19 to -16: ACROSS is inside.
    const id = await makeDataset(cookie, 'e2e-extent-region', [...ACROSS, ...ELSEWHERE]);
    const views = recordViews(page);
    const extents = recordExtents(page);
    try {
        test.skip(!(await openMap(page, views, id, ACROSS.length + ELSEWHERE.length)), 'No Mapbox token configured');
        await page.getByRole('button', { name: 'Add Filter' }).click();
        await page.getByTestId('filter-mode-query').click();
        const box = page.getByTestId('filter-card-query');
        await fillStable(box, 'location within FJ');
        await box.press('Control+Enter');
        await expectTotal(page, ACROSS.length);
        const view = await restingView(page, views, id);
        const context = `the region applied, map_extent answered ${JSON.stringify(extents)}`;
        expectFitted(view, ACROSS, context);
        expectAcross(view, context);
    } finally {
        await deleteDatasetAndWait(cookie, id);
    }
});
