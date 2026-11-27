import { test, expect, type Browser, type Locator, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { stamp } from './access-helpers';

// A polygon drawn on the map across longitude 180 finds the samples on both sides (contract g3, sections 2.3 and 2.4).
// The map's draw tool sends the points in the frame of the view: a shape drawn across the antimeridian comes with
// longitudes past 180, and the server tested each sample's own location against it, so a sample east of the
// antimeridian (-179.9) was never inside. By the contract, after a polygon is drawn across the antimeridian the grid
// shows the samples on both sides, the count is theirs, the map shows them, and the fit that follows the drawing shows
// both sides; switching the chip between include and exclude gives the two halves (the exclusion keeps every other
// sample that has a location: rewritten for contract g7, one rule everywhere, rule 1.6, it no longer keeps the one with
// no location, which it did); and the polygon carried in the page's address finds the same samples when the address is
// opened again.
//
// Written from the contract, not from the code. The map is driven as map-extent-antimeridian.spec.ts drives it: "Zoom to
// Data" fits it to the samples (a box across the antimeridian for samples either side of it, docs/MAP_REQUIREMENTS.md
// #7), and the map's view is read from the bbox it asks map_points with (below zoom 12 a tile that holds the view). Two
// samples 0.2 degrees apart sit at the middle of that box, two more at its corners: a square drawn round the middle of
// the canvas holds the pair and not the corners. Where the square landed is read from the points the page sends (the
// polygon in its map_extent request), and checked first: the drawing holds the pair and not the corners (a sample is
// inside when its location, moved by whole turns, is inside the shape), and across 180 some longitude is past 180, so a
// failure after those checks is the page's answer, not the drawing's. The same steps about longitude 10 are a pin that
// passes on main: they show the drawing itself works. A drawing is finished with Enter, as the draw tool allows; that it
// was finished is read from the requests the page then makes with the polygon.
test.use({ storageState: AUTH_STATE });

type Lonlat = [number, number];
type Bbox = [number, number, number, number];
type Asked = { path: string; body: Record<string, unknown>; answer: unknown; at: number };

interface Place {
    name: string;
    pair: [Lonlat, Lonlat];  // at the middle of the box "Zoom to Data" fits
    corners: [Lonlat, Lonlat];  // at two corners of that box
    across: boolean;
}

const ACROSS: Place = { name: 'across 180', pair: [[179.9, -17], [-179.9, -17]], corners: [[175, -12], [-175, -22]], across: true };
const ELSEWHERE: Place = { name: 'about longitude 10', pair: [[9.9, -17], [10.1, -17]], corners: [[5, -12], [15, -22]], across: false };

/** The dataset: the pair, the corners and a sample with no location; its id and the samples' ids in that order. */
async function makeDataset(cookie: string, place: Place): Promise<{ id: number; ids: number[] }> {
    const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-polygon-180-${stamp()}`, is_global: true }) });
    const located = [...place.pair, ...place.corners].map(([lon, lat], i) => ({
        filepath: `s3://e2e-polygon-180/${i}.jpg`,
        location: { type: 'Point', coordinates: [lon, lat] },
    }));
    const samples = [...located, { filepath: 's3://e2e-polygon-180/nowhere.jpg' }];
    const { ids } = await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    expect(ids).toHaveLength(5);
    return { id: ds.id, ids };
}

/** Every map_points, map_extent and samples/query request the page makes for the dataset, with its answer. */
function record(page: Page, datasetId: number): Asked[] {
    const seen: Asked[] = [];
    page.on('response', async response => {
        const request = response.request();
        const match = new URL(response.url()).pathname.match(/\/datasets\/(\d+)\/(map_points|map_extent|samples\/query)$/);
        if (request.method() !== 'POST' || !match || Number(match[1]) !== datasetId) return;
        const answer = await response.json().catch(() => null);
        seen.push({ path: match[2], body: request.postDataJSON() ?? {}, answer, at: seen.length });
    });
    return seen;
}

/** The polygon a filter holds (geo_polygon or geo_polygon_exclude, anywhere in it), or null. */
function polygonIn(node: unknown): Lonlat[] | null {
    if (Array.isArray(node)) {
        for (const item of node) {
            const found = polygonIn(item);
            if (found) return found;
        }
        return null;
    }
    if (!node || typeof node !== 'object') return null;
    const { operator, value, filters } = node as { operator?: string; value?: unknown; filters?: unknown };
    if ((operator === 'geo_polygon' || operator === 'geo_polygon_exclude') && Array.isArray(value)) return value as Lonlat[];
    return polygonIn(filters);
}

function rayCast(polygon: Lonlat[], lon: number, lat: number): boolean {
    let inside = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
        const [x1, y1] = polygon[i];
        const [x2, y2] = polygon[j];
        if ((y1 > lat) !== (y2 > lat) && lon < x1 + ((lat - y1) * (x2 - x1)) / (y2 - y1)) inside = !inside;
    }
    return inside;
}

/** Contract 1.1: inside when the location, moved east or west by whole turns, lies inside the shape. */
function insideByRule(polygon: Lonlat[], [lon, lat]: Lonlat): boolean {
    return [-2, -1, 0, 1, 2].some(k => rayCast(polygon, lon + 360 * k, lat));
}

/** A view (a bbox the map asked with, its longitudes unwrapped) holds the point on some world copy. */
function holds(bbox: Bbox, [lon, lat]: Lonlat): boolean {
    const [west, south, east, north] = bbox;
    return [lon, lon - 360, lon + 360].some(x => x >= west && x <= east) && lat >= south && lat <= north;
}

const views = (seen: Asked[], from = 0) => seen.filter(asked => asked.at >= from && asked.path === 'map_points' && Array.isArray(asked.body.bbox));
const bboxOf = (asked: Asked) => asked.body.bbox as Bbox;
const idsIn = (answer: unknown): number[] => ((answer as { items?: Array<{ id?: number | null }> } | null)?.items ?? [])
    .map(item => item.id).filter((id): id is number => typeof id === 'number').sort((a, b) => a - b);

/** Nothing new asked for a while (the map asks 250 ms after it stops moving). */
async function settled(seen: Asked[]) {
    let last = -1;
    await expect.poll(() => {
        const still = seen.length === last;
        last = seen.length;
        return still;
    }, { intervals: [1_500], timeout: 30_000 }).toBe(true);
}

/** The map in the right-hand panel; false without a Mapbox token. */
async function showMap(page: Page): Promise<boolean> {
    const map = page.getByTestId('panel-map');
    const zoomToData = map.getByRole('button', { name: 'Zoom to Data' });
    const noToken = map.getByText(/NEXT_PUBLIC_MAPBOX_TOKEN/);
    const kept = await zoomToData.or(noToken).first().waitFor({ timeout: 3_000 }).then(() => true, () => false);
    if (!kept) await page.getByTestId('panel-embeddings').getByTestId('panel-type-select').selectOption('map');
    await expect(noToken.or(zoomToData).first()).toBeVisible({ timeout: 20_000 });
    return !(await noToken.isVisible());
}

/** Whether the draw tool is drawing. It sets the map's classes when it next renders, so the mouse moves first. */
async function isDrawing(page: Page, mapEl: Locator, at: { x: number; y: number }): Promise<boolean> {
    await page.mouse.move(at.x + 4, at.y + 4);
    await page.mouse.move(at.x, at.y + 8);
    return expect(mapEl).toHaveClass(/mode-draw_polygon/, { timeout: 1_000 }).then(() => true, () => false);
}

/** Draw a square round the middle of the canvas (its half side 15% of the canvas's smaller side); finish it with Enter. */
async function drawSquare(page: Page, map: Locator) {
    const canvasLocator = map.locator('canvas.mapboxgl-canvas').first();
    const canvas = (await canvasLocator.boundingBox())!;
    const cx = canvas.x + canvas.width / 2;
    const cy = canvas.y + canvas.height / 2;
    const half = Math.min(canvas.width, canvas.height) * 0.15;
    const mapEl = map.locator('.mapboxgl-map');
    // A click on the map's background starts a drawing. It is on the square's left side, so a vertex placed there too
    // leaves the square as it is.
    const start = { x: cx - half, y: cy };
    await expect(async () => {
        if (await isDrawing(page, mapEl, start)) return;
        await page.mouse.click(start.x, start.y);
        expect(await isDrawing(page, mapEl, start), 'a click on the map started a drawing').toBe(true);
    }).toPass({ timeout: 15_000 });
    for (const [dx, dy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) await page.mouse.click(cx + dx * half, cy + dy * half);
    await canvasLocator.focus();  // the draw tool reads Enter on the map
    await page.keyboard.press('Enter');
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

/** The `f` parameter of the page's address once it is no longer `before` (the address is written after the entry). */
async function addressAfter(page: Page, before: string | null): Promise<string> {
    await expect.poll(() => new URL(page.url()).searchParams.get('f'), { timeout: 15_000 }).not.toBe(before);
    expect(new URL(page.url()).searchParams.get('f'), `the address carries the filter: ${page.url()}`).toBeTruthy();
    return page.url();
}

async function drawAndCheck(page: Page, browser: Browser, place: Place) {
    test.setTimeout(240_000);
    const cookie = await adminLogin();
    const { id, ids } = await makeDataset(cookie, place);
    const pairIds = ids.slice(0, 2).sort((a, b) => a - b);
    const located: Lonlat[] = [...place.pair, ...place.corners];
    const seen = record(page, id);
    try {
        await page.goto(`/datasets/${id}`);
        await expectTotal(page, 5);
        test.skip(!(await showMap(page)), 'No Mapbox token configured');
        const map = page.getByTestId('panel-map');

        // "Zoom to Data": the box holding the four located samples, the pair at its middle. The map rests on it once a
        // view closer than the world's (zoom 3 or more) holding them is asked for and nothing new follows.
        await map.getByRole('button', { name: 'Zoom to Data' }).click();
        await expect.poll(() => views(seen).some(asked => {
            const bbox = bboxOf(asked);
            return Number(asked.body.zoom) >= 3 && located.every(point => holds(bbox, point));
        }), { timeout: 30_000 }).toBe(true);
        await settled(seen);
        const fitted = views(seen).at(-1)!;
        if (place.across) expect(bboxOf(fitted)[0] < 180 && bboxOf(fitted)[2] > 180, `the map rests across 180: ${JSON.stringify(fitted.body)}`).toBe(true);

        const drawnFrom = seen.length;
        await drawSquare(page, map);

        // The fit that follows the drawing asks map_extent with the polygon: the points the page sends.
        await expect.poll(() => seen.some(asked => asked.at >= drawnFrom && asked.path === 'map_extent' && polygonIn(asked.body.filters)), { timeout: 15_000 }).toBe(true);
        const extentAsked = seen.find(asked => asked.at >= drawnFrom && asked.path === 'map_extent' && polygonIn(asked.body.filters))!;
        const polygon = polygonIn(extentAsked.body.filters)!;
        const drawn = `the polygon drawn: ${JSON.stringify(polygon)}`;
        expect(place.pair.every(point => insideByRule(polygon, point)), `the drawing holds the pair: ${drawn}`).toBe(true);
        expect(place.corners.some(point => insideByRule(polygon, point)), `the drawing holds no corner: ${drawn}`).toBe(false);
        if (place.across) {
            expect(polygon.some(([lon]) => lon > 180 || lon < -180), `the draw tool sends the view's frame, past 180: ${drawn}`).toBe(true);
        }

        // The grid shows the pair, both sides of 180, and only them.
        await expect(page.getByTestId('grid-cell'), `the grid for ${drawn}`).toHaveCount(2, { timeout: 15_000 });
        // The grid's page of samples (its count is asked apart, with limit 0, and may be answered after it).
        const gridAnswer = () => seen.filter(asked => asked.at >= drawnFrom && asked.path === 'samples/query'
            && Number(asked.body.limit) > 0 && polygonIn(asked.body.filters)).at(-1);
        await expect.poll(() => idsIn(gridAnswer()?.answer), { message: `the grid's samples for ${drawn}` }).toEqual(pairIds);
        // map_extent counts the pair, and the fit shows both: the map moves in to the samples the polygon holds and rests
        // on a view holding the pair (across 180 there).
        expect((extentAsked.answer as { count?: number } | null)?.count, `map_extent answered ${JSON.stringify(extentAsked.answer)} for ${drawn}`).toBe(2);
        const zoomOf = (asked: Asked) => Number(asked.body.zoom);
        await expect.poll(() => views(seen, drawnFrom).some(asked => polygonIn(asked.body.filters) && zoomOf(asked) > zoomOf(fitted)), { timeout: 20_000 }).toBe(true);
        await settled(seen);
        const rest = views(seen, drawnFrom).filter(asked => polygonIn(asked.body.filters)).at(-1)!;
        const restsOn = `the map rests on ${JSON.stringify(rest.body.bbox)} at zoom ${rest.body.zoom} after ${drawn}`;
        expect(place.pair.every(point => holds(bboxOf(rest), point)), `the fit after the drawing shows the pair: ${restsOn}`).toBe(true);
        if (place.across) expect(bboxOf(rest)[0] < 180 && bboxOf(rest)[2] > 180, `the fit is across 180: ${restsOn}`).toBe(true);
        // The map shows the pair: the points it is answered for the polygon hold both.
        await expect.poll(() => views(seen, drawnFrom).some(asked => polygonIn(asked.body.filters)
            && pairIds.every(pairId => idsIn(asked.answer).includes(pairId))), { timeout: 20_000, message: `the map shows the pair for ${drawn}` }).toBe(true);

        // Applied, the chip's count is the pair's; switched to exclude, every other sample that has a location (the two
        // corners, not the one with no location); switched back, the pair again. Each opened from its address finds the
        // same.
        await page.getByTestId('apply-filters').click();
        const chip = page.getByTestId('query-stage-chip').filter({ hasText: 'Map Region' });
        await expect(chip).toHaveCount(1);
        await expectTotal(page, 2);
        const included = await addressAfter(page, null);
        await chip.click();
        await expect(chip).toContainText('Exclude Map Region');
        await expectTotal(page, 2);
        await expect(page.getByTestId('grid-cell')).toHaveCount(2);
        const excludedAddress = await addressAfter(page, new URL(included).searchParams.get('f'));
        await expectTotalOpened(browser, excludedAddress, 2);
        await chip.click();
        await expect(chip).not.toContainText('Exclude');
        await expectTotal(page, 2);
        await expectTotalOpened(browser, await addressAfter(page, new URL(excludedAddress).searchParams.get('f')), 2);
    } finally {
        await deleteDatasetAndWait(cookie, id);
    }
}

test('a polygon drawn about longitude 10 shows the samples inside it, and its chip switches to the rest', async ({ page, browser }) => {
    await drawAndCheck(page, browser, ELSEWHERE);
});

test('a polygon drawn across 180 shows the samples on both sides, its fit shows both, and its chip switches to the rest', async ({ page, browser }) => {
    await drawAndCheck(page, browser, ACROSS);
});
