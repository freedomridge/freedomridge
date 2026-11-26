import zlib from 'node:zlib';
import { test, expect, type Page, type Route } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expectTotal, fillStable } from './helpers';
import { allSteps, createUser, signIn, stamp, type TestUser } from './access-helpers';
import { FIXTURE_VERSION, loadRegionFixture, regionsNotOptedIn } from './regions-fixture';

// Regions in the app (docs/REGIONS.md, the regions UI contract). A filter keeps or removes the samples inside a named
// boundary from a loaded set. In the app it is a chip like a map filter: named for the region, a click turns it
// around between in and not in, its outline is on the map while it is applied, and removing it doesn't move the map.
// The same chip however it arrives: a filter card's Query box, a q= link, an f= link, a saved view, saved filters.
// The boundaries are a small synthetic file loaded with the backend's offline command (regions-fixture.ts), only in a
// run that opts in with FRIDGE_E2E_LOAD_REGIONS=1. Without it the whole file skips: no hook runs, nothing is created.
test.use({ storageState: AUTH_STATE, permissions: ['clipboard-read', 'clipboard-write'] });
test.skip(regionsNotOptedIn !== null, regionsNotOptedIn ?? '');

const S = stamp();
let cookie = '';
let datasetId = 0;
let notLoaded: string | null = null;

// Points surely inside the fixture's shapes (its regions are rectangles), [lon, lat].
const PLACES: Record<string, Array<[number, number]>> = {
    kansas: [[-99, 38], [-98, 38.5], [-97, 39], [-100, 37.8]],
    nebraska: [[-100, 41], [-98, 41.5], [-99, 42]],
    hawaii: [[-155.5, 19.5], [-159.6, 22.0]],  // one on each of the state's two islands
    hawaiiSea: [[-157.5, 21.0]],  // the sea between them: not in Hawaii
    fiji: [[178.5, -17.5], [-179.0, -17.5]],  // one on each side of the antimeridian
    georgiaState: [[-83.0, 33.0]],
};
const NO_LOCATION = 2;
const TOTAL = Object.values(PLACES).flat().length + NO_LOCATION;  // 15
const KANSAS = 4;
const KS = 'In Kansas, United States of America';
const NOT_KS = 'Not in Kansas, United States of America';
const NE = 'In Nebraska, United States of America';
const NOT_NE = 'Not in Nebraska, United States of America';
const TOGGLE_TITLE = 'Click to toggle include/exclude';

test.beforeAll(async () => {
    test.setTimeout(180_000);
    cookie = await adminLogin();
    notLoaded = await loadRegionFixture(cookie);
    const { id } = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-regions-${S}` }) });
    datasetId = id;
    const samples: Array<Record<string, unknown>> = Object.entries(PLACES).flatMap(([place, points]) => points.map(([lon, lat], i) => ({
        filepath: `s3://e2e-regions/${place}_${i}.jpg`,
        metadata: { place },
        location: { type: 'Point', coordinates: [lon, lat] },
    })));
    for (let i = 0; i < NO_LOCATION; i += 1) samples.push({ filepath: `s3://e2e-regions/nowhere_${i}.jpg`, metadata: { place: 'nowhere' } });
    await apiFetch(cookie, `/datasets/${datasetId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
});

test.afterAll(async () => {
    test.setTimeout(120_000);
    if (datasetId) await deleteDatasetAndWait(cookie, datasetId, 120_000);
});

test.beforeEach(() => {
    test.skip(notLoaded !== null, notLoaded ?? '');
});

const chips = (page: Page) => page.getByTestId('query-stage-chip');
const toggleNamed = (page: Page, name: string) => chips(page).getByRole('button', { name, exact: true });
const outlines = (page: Page) => page.getByTestId('map-region-outline');
const q = (page: Page) => new URL(page.url()).searchParams.get('q');
const clipboard = (page: Page) => page.evaluate(() => navigator.clipboard.readText());

/** An f= link's value: stages as the app encodes them (version 1, raw deflate, base64url without padding). */
const encodeStages = (stages: unknown[]) => `1.${zlib.deflateRawSync(Buffer.from(JSON.stringify({ stages }))).toString('base64url')}`;
const regionStage = (operator: 'geo_region' | 'geo_region_exclude', value: string, wrap = false) => {
    const node = { field: 'location', operator, value };
    return { type: 'map_region', description: 'made elsewhere', filter: wrap ? { logic: 'AND', filters: [node] } : node };
};

async function openDataset(page: Page, query = '', total = TOTAL) {
    await page.goto(`/datasets/${datasetId}${query}`);
    await expectTotal(page, total);
}

async function addQuery(page: Page, text: string) {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    const box = page.getByTestId('filter-card-query');
    await fillStable(box, text);
    await box.press('Control+Enter');
}

async function removeChip(page: Page, index = 0) {
    const chip = chips(page).nth(index);
    await chip.hover();
    await chip.getByRole('button', { name: 'Remove this region filter', exact: true }).click();
}

test('a region typed in a filter card is a chip named for it; a click turns it around, with the address and the count', async ({ page }) => {
    await openDataset(page);
    await addQuery(page, 'location within Kansas');
    await expectTotal(page, KANSAS);
    await expect(chips(page)).toHaveCount(1);
    await expect(chips(page).first().getByRole('button')).toHaveCount(2);
    const toggle = toggleNamed(page, KS);
    await expect(toggle).toHaveAttribute('title', TOGGLE_TITLE);
    await expect(toggle).toHaveAccessibleDescription(TOGGLE_TITLE);
    await expect.poll(() => q(page)).toBe('location within "US-KS"');

    await toggle.click();
    await expect(toggleNamed(page, NOT_KS)).toBeVisible();
    await expect(chips(page)).toHaveCount(1);
    await expect(page.getByTestId('filter-card-query')).toHaveCount(0);  // no editor opens
    await expectTotal(page, TOTAL - KANSAS);  // the rest, the samples with no location included
    await expect.poll(() => q(page)).toBe('location not within "US-KS"');
    await toggleNamed(page, NOT_KS).click();
    await expect(toggleNamed(page, KS)).toBeVisible();
    await expectTotal(page, KANSAS);
    await expect.poll(() => q(page)).toBe('location within "US-KS"');

    await removeChip(page);
    await expect(chips(page)).toHaveCount(0);
    await expectTotal(page, TOTAL);
});

test('a q= link gives one chip per region part, by key or name in any case, written back with the key', async ({ page }) => {
    const cases: Array<[string, string[], number, string]> = [
        ['location within Kansas', [KS], KANSAS, 'location within "US-KS"'],
        ['location within us-ks', [KS], KANSAS, 'location within "US-KS"'],
        ['location within "US-KS" AND location not within "US-NE"', [KS, NOT_NE], KANSAS, 'location within "US-KS" AND location not within "US-NE"'],
        ['location within us:Midwest', ['In Midwest, United States of America'], KANSAS + 3, 'location within "US:midwest"'],
        ['location within FJ', ['In Fiji'], 2, 'location within "FJ"'],  // a country by its name; points each side of 180
        ['location within US-HI', ['In Hawaii, United States of America'], 2, 'location within "US-HI"'],  // two islands, not the sea
        ['location within US', ['In United States of America'], KANSAS + 3 + 2 + 1, 'location within "US"'],
    ];
    for (const [query, names, count, written] of cases) {
        await openDataset(page, `?q=${encodeURIComponent(query)}`, count);
        await expect(chips(page), query).toHaveCount(names.length);
        for (const [index, name] of names.entries()) {
            await expect(chips(page).nth(index).getByRole('button').first(), query).toHaveAccessibleName(name);
        }
        await expect.poll(() => q(page), { message: query }).toBe(written);
    }
});

test('an f= link, a saved view and saved filters give the same chip; a view of them is a readable q= link', async ({ page }) => {
    // f=: a stage made elsewhere, as an AND group holding the one region test, its key in lower case.
    await openDataset(page, `?f=${encodeStages([regionStage('geo_region_exclude', 'us-ks', true)])}`, TOTAL - KANSAS);
    await expect(chips(page).getByRole('button').first()).toHaveAccessibleName(NOT_KS);
    await expect.poll(() => q(page)).toBe('location not within "US-KS"');

    // A private saved view: the address writes its filters, as q=.
    const view = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({ name: `e2e-regions-view-${S}`, is_public: false, content: { viewStages: [regionStage('geo_region', 'US-NE')] } }),
    });
    try {
        await openDataset(page, `?view=${view.slug}`, 3);
        await expect(chips(page).getByRole('button').first()).toHaveAccessibleName(NE);
        await expect.poll(() => q(page)).toBe('location within "US-NE"');
    } finally {
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }

    // A public view, as the dataset's default view: the dataset opens on the chip; the address keeps the view.
    const shared = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({ name: `e2e-regions-default-${S}`, is_public: true, content: { viewStages: [regionStage('geo_region', 'US-KS', true)] } }),
    });
    try {
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: shared.id }) });
        await page.evaluate(id => localStorage.removeItem(`fridge-filters-${id}`), datasetId);
        await openDataset(page, '', KANSAS);
        await expect(chips(page).getByRole('button').first()).toHaveAccessibleName(KS);
        await openDataset(page, `?view=${shared.slug}`, KANSAS);
        await expect(chips(page).getByRole('button').first()).toHaveAccessibleName(KS);
        await expect.poll(() => new URL(page.url()).searchParams.get('view')).toBe(shared.slug);
    } finally {
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: null }) }).catch(() => undefined);
        await apiFetch(cookie, `/views/${shared.id}`, { method: 'DELETE' });
    }

    // The filters the app keeps for the dataset in this browser.
    await page.evaluate(({ key, stages }) => localStorage.setItem(key, JSON.stringify({
        viewStages: stages, filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
        brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
    })), { key: `fridge-filters-${datasetId}`, stages: [regionStage('geo_region', 'us-ks')] });
    await openDataset(page, '', KANSAS);
    await expect(chips(page).getByRole('button').first()).toHaveAccessibleName(KS);
});

test('a region inside a larger filter is an ordinary chip', async ({ page }) => {
    await openDataset(page, `?q=${encodeURIComponent('location within "US-KS" OR location within "US-NE"')}`, KANSAS + 3);
    await expect(chips(page)).toHaveCount(1);
    await expect(chips(page).getByRole('button', { name: /^(Not )?[Ii]n / })).toHaveCount(0);
    await expect(chips(page).getByRole('button', { name: 'Remove this region filter' })).toHaveCount(0);
});

test('Copy link writes the chips as q=, which opens in a fresh browser on the same chips and count', async ({ page, browser }) => {
    await openDataset(page);
    await addQuery(page, 'location within "US-KS"');
    await expectTotal(page, KANSAS);
    await addQuery(page, 'location within Nebraska');
    await expectTotal(page, 0);
    await toggleNamed(page, NE).click();
    await expectTotal(page, KANSAS);
    await page.getByTestId('copy-link').click();
    await expect(page.getByText(/^Link to these filters copied/)).toBeVisible();
    const link = new URL(await clipboard(page));
    expect(link.searchParams.get('q')).toBe('location within "US-KS" AND location not within "US-NE"');
    expect([...link.searchParams.keys()]).toEqual(['q']);

    const fresh = await browser.newContext({ storageState: AUTH_STATE });
    try {
        const other = await fresh.newPage();
        await other.goto(link.href);
        await expectTotal(other, KANSAS);
        await expect(chips(other)).toHaveCount(2);
        await expect(chips(other).nth(0).getByRole('button').first()).toHaveAccessibleName(KS);
        await expect(chips(other).nth(1).getByRole('button').first()).toHaveAccessibleName(NOT_NE);
        await expect(other).toHaveURL(link.href);
    } finally {
        await fresh.close();
    }
});

test('a name several regions share, or none has, is a mistake in the card with keys to choose from', async ({ page }) => {
    await openDataset(page);
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    const box = page.getByTestId('filter-card-query');
    const problem = page.getByTestId('filter-card-problem');

    await fillStable(box, 'location within Georgia');
    await box.press('Control+Enter');
    await expect(problem).toHaveText(
        'Line 1, column 17: \'Georgia\' names 2 regions: GE (country), US-GA (state or province of United States of America); '
        + 'write the key of the one you mean (did you mean \'GE\' or \'US-GA\'?)',
    );
    await expect(box).toBeVisible();  // the card stays open
    await expect(chips(page)).toHaveCount(0);

    await fillStable(box, 'location within Limburg');
    await box.press('Control+Enter');
    await expect(problem).toContainText('Line 1, column 17: \'Limburg\' names 2 regions: ');
    await expect(problem).toContainText('\'BE-VLI\'');
    await expect(problem).toContainText('\'NL-LI\'');

    await fillStable(box, 'location within Kansaz');
    await box.press('Control+Enter');
    await expect(problem).toContainText('Line 1, column 17: unknown region \'Kansaz\'');
    await expect(problem).toContainText('\'US-KS\'');
    await expect(chips(page)).toHaveCount(0);
    await expectTotal(page, TOTAL);
});

test('a chip for a key this server has no region for keeps the key, says why, and the grid says the refusal', async ({ page }) => {
    await page.goto(`/datasets/${datasetId}?f=${encodeStages([regionStage('geo_region', 'US-KZ')])}`);
    const toggle = chips(page).getByRole('button').first();
    await expect(toggle).toHaveAccessibleName('In US-KZ');
    await expect(toggle).toHaveAttribute('title', /^Unknown region 'US-KZ': the boundary set isn't loaded on this server or has no such region \(did you mean .*'US-KS' \(Kansas\)/);
    await expect(page.getByTestId('grid-unavailable')).toContainText('Invalid input: Unknown region \'US-KZ\': the boundary set isn\'t loaded on this server or has no such region');
    await expect(page.getByTestId('total-samples')).toHaveText('Count unavailable');
    // The server can't write it back as text: the address keeps f=.
    await expect.poll(() => new URL(page.url()).searchParams.has('f')).toBe(true);
    expect(q(page)).toBeNull();
});

type MapRequest = { zoom: number; bbox: [number, number, number, number]; filters: string };

/** The map's view, as the map asks for its points (the contract's hook). */
function mapRequests(page: Page): MapRequest[] {
    const seen: MapRequest[] = [];
    page.on('request', request => {
        if (request.method() !== 'POST' || !new URL(request.url()).pathname.endsWith(`/datasets/${datasetId}/map_points`)) return;
        const body = request.postDataJSON() ?? {};
        if (Array.isArray(body.bbox)) seen.push({ zoom: body.zoom, bbox: body.bbox, filters: JSON.stringify(body.filters ?? null) });
    });
    return seen;
}

/** Show the map in the right panel; false without a Mapbox token. */
async function showMap(page: Page): Promise<boolean> {
    await page.getByTestId('panel-embeddings').getByTestId('panel-type-select').selectOption('map');
    const map = page.getByTestId('panel-map');
    const noToken = map.getByText(/NEXT_PUBLIC_MAPBOX_TOKEN/);
    await expect(noToken.or(map.getByRole('button', { name: 'Zoom to Data' })).first()).toBeVisible({ timeout: 20_000 });
    return !(await noToken.isVisible());
}

/** Wait until the map has asked nothing new for a while (it asks 250 ms after it stops moving). */
async function settled(requests: MapRequest[]) {
    let last = -1;
    await expect.poll(() => {
        const now = requests.length;
        const still = now === last && now > 0;
        last = now;
        return still;
    }, { intervals: [1_500], timeout: 20_000 }).toBe(true);
}

/** The first request for filters other than `before`'s, once the page sends it. */
async function nextFor(requests: MapRequest[], before: MapRequest): Promise<MapRequest> {
    await expect.poll(() => requests.some(r => r.filters !== before.filters && requests.indexOf(r) > requests.indexOf(before)), { timeout: 15_000 }).toBe(true);
    return requests.slice(requests.indexOf(before) + 1).find(r => r.filters !== before.filters)!;
}

const sameView = (a: MapRequest, b: MapRequest) => expect({ zoom: a.zoom, bbox: a.bbox }).toEqual({ zoom: b.zoom, bbox: b.bbox });

test('on the map: each chip is outlined, included or excluded; applying "within" fits its samples; turning or removing a chip doesn\'t move the map', async ({ page }) => {
    test.setTimeout(150_000);
    const requests = mapRequests(page);
    await openDataset(page);
    test.skip(!(await showMap(page)), 'No Mapbox token configured');
    await settled(requests);
    expect(requests.at(-1)!.zoom).toBe(1);  // the world view

    // Applied: the map fits the samples inside Kansas.
    await addQuery(page, 'location within Kansas');
    await expectTotal(page, KANSAS);
    await expect.poll(() => requests.at(-1)?.zoom ?? 0, { timeout: 15_000 }).toBeGreaterThan(1);
    await settled(requests);
    const fitted = requests.at(-1)!;
    const [west, south, east, north] = fitted.bbox;
    for (const [lon, lat] of PLACES.kansas) {
        expect(lon >= west && lon <= east && lat >= south && lat <= north, `[${lon}, ${lat}] in the fitted view`).toBe(true);
    }
    expect(east - west, 'around the samples, not the world').toBeLessThan(90);
    await expect(outlines(page)).toHaveText(['US-KS included']);
    await expect(page.getByRole('list', { name: 'Regions outlined on the map' })).toHaveCount(1);

    // A "not within" chip applied: outlined, excluded, and the map stays.
    let before = requests.at(-1)!;
    await addQuery(page, 'location not within Nebraska');
    await expectTotal(page, KANSAS);
    sameView(await nextFor(requests, before), before);
    await expect(outlines(page)).toHaveText(['US-KS included', 'US-NE excluded']);

    // Turned around: the outline says so, and the map stays.
    await settled(requests);
    before = requests.at(-1)!;
    await toggleNamed(page, KS).click();
    await expect(outlines(page)).toHaveText(['US-KS excluded', 'US-NE excluded']);
    sameView(await nextFor(requests, before), before);

    // Removed, one then the other: its outline goes, and the map stays.
    await settled(requests);
    before = requests.at(-1)!;
    await removeChip(page, 1);
    await expect(outlines(page)).toHaveText(['US-KS excluded']);
    sameView(await nextFor(requests, before), before);
    await settled(requests);
    before = requests.at(-1)!;
    await removeChip(page, 0);
    await expect(outlines(page)).toHaveCount(0);
    sameView(await nextFor(requests, before), before);
});

test('on the map: a page opened with region chips stays on the world view; a region with no sample in it is fitted by its box', async ({ page }) => {
    test.setTimeout(150_000);
    const requests = mapRequests(page);
    await openDataset(page, `?q=${encodeURIComponent('location within "US-KS"')}`, KANSAS);
    test.skip(!(await showMap(page)), 'No Mapbox token configured');
    await settled(requests);
    expect(requests.map(r => r.zoom), 'opened with chips: no move').toEqual(requests.map(() => 1));
    await expect(outlines(page)).toHaveText(['US-KS included']);

    // A region no sample is in (the country Georgia): the map fits the region's bounding box.
    await removeChip(page);
    await expectTotal(page, TOTAL);
    await addQuery(page, 'location within GE');
    await expectTotal(page, 0);
    await expect.poll(() => requests.at(-1)?.zoom ?? 0, { timeout: 15_000 }).toBeGreaterThan(1);
    await settled(requests);
    const [west, south, east, north] = requests.at(-1)!.bbox;
    expect(west <= 44.6 && east >= 45.0 && south <= 41.6 && north >= 41.9, `[${west}, ${south}, ${east}, ${north}] holds Georgia's box`).toBe(true);
    await expect(outlines(page)).toHaveText(['GE included']);
});

type View = { zoom: number; bbox: [number, number, number, number] };
const viewOf = (r: MapRequest): View => ({ zoom: r.zoom, bbox: r.bbox });
const isSameView = (a: View, b: View) => a.zoom === b.zoom && JSON.stringify(a.bbox) === JSON.stringify(b.bbox);

/**
 * How long a move takes to show, by the contract: within about 2 s, plus an 0.8 s animation, and the map asks for its
 * points 250 ms after it stops. A view the page has already shown sends no request (they are cached), so "it didn't
 * move" is that nothing else is asked for within that time.
 */
const MOVE_WINDOW = 4_000;

/** For the time a move would take to show, the map asks only for views in `allowed`; where it rests is the last. */
async function staysOn(page: Page, requests: MapRequest[], from: number, allowed: View[], label: string) {
    const deadline = Date.now() + MOVE_WINDOW;
    for (;;) {
        const other = requests.slice(from).map(viewOf).find(view => !allowed.some(ok => isSameView(ok, view)));
        if (other || Date.now() > deadline) {
            expect.soft(other ?? null, `${label}: the map moved to`).toBeNull();
            break;
        }
        await page.waitForTimeout(250);
    }
    const last = requests.slice(from).at(-1);
    if (last) expect.soft(viewOf(last), `${label}: where the map rests`).toEqual(allowed.at(-1));
}

/** Map page helpers for one test's page. */
function keptMap(page: Page, requests: MapRequest[]) {
    const zoomToData = page.getByTestId('panel-map').getByRole('button', { name: 'Zoom to Data' });
    const mapShowing = () => expect(zoomToData).toBeVisible({ timeout: 20_000 });
    /** The filters kept for the dataset forgotten, from another page so the dataset page's own save can't put them back. */
    async function forgetKeptFilters() {
        await page.goto('/');
        await page.evaluate(id => localStorage.removeItem(`fridge-filters-${id}`), datasetId);
    }
    /** The Map chosen in the right-hand panel of this page (whatever it shows now), so this browser keeps it. */
    async function chooseMap() {
        const select = page.getByTestId('panel-type-select').nth(2);
        await select.selectOption('embeddings');
        await select.selectOption('map');
        await mapShowing();
        await settled(requests);
    }
    /**
     * This browser keeps a layout with the Map panel showing: chosen on the dataset's page (with whatever filters it
     * opens on), then left. Chosen again before each opening, so each starts from that layout whatever the visit
     * before it kept.
     */
    async function keepMapShowing() {
        await forgetKeptFilters();
        await page.goto(`/datasets/${datasetId}`);
        await expect(page.getByTestId('total-samples')).toHaveText(/^\s*\d+ (samples|images)\s*$/, { timeout: 15_000 });
        await chooseMap();
        await page.goto('/');
    }
    /** Holds the page's first GET of `pathname` until the kept Map is on screen and has asked for its points. */
    async function answerAfterMap(pathname: string) {
        const seen = { held: false, mapFirst: false };
        const from = requests.length;
        const matches = (url: URL) => url.pathname === pathname;
        await page.route(matches, async route => {
            if (seen.held || route.request().method() !== 'GET') return route.fallback();
            seen.held = true;
            const deadline = Date.now() + 15_000;
            while (Date.now() < deadline && !(requests.length > from && await zoomToData.isVisible())) await new Promise(r => setTimeout(r, 100));
            seen.mapFirst = requests.length > from;
            await route.fallback();
        });
        return { seen, done: () => page.unroute(matches) };
    }
    return { zoomToData, mapShowing, forgetKeptFilters, chooseMap, keepMapShowing, answerAfterMap };
}

/** The map fitted the Kansas samples. */
function expectFittedOnKansas(view: View, home: View) {
    expect(view.zoom, 'the fitted view is closer than the world view').toBeGreaterThan(home.zoom);
    const [west, south, east, north] = view.bbox;
    for (const [lon, lat] of PLACES.kansas) {
        expect(lon >= west && lon <= east && lat >= south && lat <= north, `[${lon}, ${lat}] in the fitted view`).toBe(true);
    }
}

test('on the map, kept showing: a link, a saved view\'s link, a reload and the default view (its answer late) bring region chips in without moving it; a filter card or an edited chip moves it', async ({ page }) => {
    test.setTimeout(300_000);
    const requests = mapRequests(page);
    const { mapShowing, forgetKeptFilters, chooseMap, keepMapShowing, answerAfterMap } = keptMap(page, requests);

    // The layout this browser keeps has the Map panel showing; the page with no filters opened on it rests on `home`.
    await openDataset(page);
    test.skip(!(await showMap(page)), 'No Mapbox token configured');
    await settled(requests);
    let from = requests.length;
    await page.goto('/');
    await openDataset(page);
    await mapShowing();
    await expect.poll(() => requests.length, { timeout: 20_000 }).toBeGreaterThan(from);
    await settled(requests);
    const unfiltered = requests.slice(from).map(viewOf);
    const home = unfiltered.at(-1)!;

    const view = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({ name: `e2e-regions-kept-map-${S}`, is_public: false, content: { viewStages: [regionStage('geo_region', 'US-KS')] } }),
    });
    const shared = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({ name: `e2e-regions-kept-map-default-${S}`, is_public: true, content: { viewStages: [regionStage('geo_region', 'US-KS', true)] } }),
    });
    const kansasLink = `?q=${encodeURIComponent('location within "US-KS"')}`;
    try {
        // Each opening returns the request count from which its map is judged.
        const ways: Array<[string, () => Promise<number>]> = [
            ['a q= link', async () => {
                const at = requests.length;
                await openDataset(page, kansasLink, KANSAS);
                return at;
            }],
            // The page shows only "Opening the link…" until the view is read, so its chips can't arrive after the map.
            ['a saved view\'s ?view= link', async () => {
                const at = requests.length;
                await openDataset(page, `?view=${view.slug}`, KANSAS);
                return at;
            }],
            ['a reload of a page holding a region chip', async () => {
                await openDataset(page, kansasLink, KANSAS);
                await chooseMap();
                const at = requests.length;
                await page.reload();
                await expectTotal(page, KANSAS);
                return at;
            }],
            ['the dataset\'s default view, the dataset\'s answer arriving after the map is up', async () => {
                await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: shared.id }) });
                await forgetKeptFilters();
                const late = await answerAfterMap(`/api/datasets/${datasetId}`);
                const at = requests.length;
                await openDataset(page, '', KANSAS);
                await late.done();
                expect.soft(late.seen, 'the dataset\'s answer was held until the map was up').toEqual({ held: true, mapFirst: true });
                return at;
            }],
        ];
        for (const [way, open] of ways) {
            await keepMapShowing();
            const at = await open();
            const map = page.getByTestId('panel-map').getByRole('button', { name: 'Zoom to Data' });
            await expect.soft(map, `${way}: the Map panel this browser keeps is showing`).toBeVisible({ timeout: 20_000 });
            if (!(await map.isVisible())) continue;
            await expect(outlines(page), way).toHaveText(['US-KS included']);
            await staysOn(page, requests, at, unfiltered, way);
        }
    } finally {
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: null }) }).catch(() => undefined);
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
        await apiFetch(cookie, `/views/${shared.id}`, { method: 'DELETE' });
    }

    // Applied in the page, by a filter card: the map fits the samples in the region.
    await keepMapShowing();
    await openDataset(page);
    await mapShowing();
    await settled(requests);
    await addQuery(page, 'location within Kansas');
    await expectTotal(page, KANSAS);
    await expect.poll(() => requests.at(-1)?.zoom ?? 0, { timeout: 15_000 }).toBeGreaterThan(home.zoom);
    await settled(requests);
    expectFittedOnKansas(viewOf(requests.at(-1)!), home);

    // Applied in the page by editing an ordinary chip's card into a region test (a region chip itself has no card).
    await keepMapShowing();
    await openDataset(page);
    await mapShowing();
    await addQuery(page, 'place = "nebraska"');
    await expectTotal(page, 3);
    await settled(requests);
    from = requests.length;
    await chips(page).first().click();
    await page.getByTestId('filter-mode-query').click();
    const box = page.getByTestId('filter-card-query');
    await fillStable(box, 'location within "US-KS"');
    await box.press('Control+Enter');
    await expectTotal(page, KANSAS);
    await expect(toggleNamed(page, KS)).toBeVisible();
    await expect.poll(() => requests.slice(from).at(-1)?.zoom ?? 0, { timeout: 15_000 }).toBeGreaterThan(home.zoom);
    await settled(requests);
    expectFittedOnKansas(viewOf(requests.at(-1)!), home);
});

test('on the map, in the page: a saved view chosen from Saved Views, Default view and Clear All going back to a default view, and Back and Forward bring region chips without moving it', async ({ page }) => {
    test.setTimeout(300_000);
    const requests = mapRequests(page);
    const { mapShowing, forgetKeptFilters, keepMapShowing } = keptMap(page, requests);
    const address = () => page.url();
    /** The last view the map asked for, once it has settled, and the request count from which a change is judged. */
    async function now(): Promise<[View, number]> {
        await settled(requests);
        return [viewOf(requests.at(-1)!), requests.length];
    }

    await openDataset(page);
    test.skip(!(await showMap(page)), 'No Mapbox token configured');
    const view = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({ name: `e2e-regions-chosen-${S}`, is_public: false, content: { viewStages: [regionStage('geo_region', 'US-KS')] } }),
    });
    const shared = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({ name: `e2e-regions-default-${S}-in-page`, is_public: true, content: { viewStages: [regionStage('geo_region', 'US-KS', true)] } }),
    });
    // Each case on a fresh page that opened with the map kept showing, so a move in one can't hide one in the next.
    async function openPlain() {
        await keepMapShowing();
        await openDataset(page);
        await mapShowing();
    }
    // What the address says once it has caught up with the filters (each a history entry of its own).
    const onDefaultView = (url: URL) => url.searchParams.get('view') === shared.slug;
    const noFilters = (url: URL) => url.searchParams.get('q') === '' && !url.searchParams.has('view');
    const onNebraska = (url: URL) => (url.searchParams.get('q') ?? '').includes('nebraska');
    async function openOnDefaultView() {
        await keepMapShowing();
        await forgetKeptFilters();
        await openDataset(page, '', KANSAS);
        await mapShowing();
        await expect(outlines(page)).toHaveText(['US-KS included']);
        await expect.poll(() => onDefaultView(new URL(address()))).toBe(true);
    }
    /** `action` changes the filters to `total` samples; the address catches up before the next change. */
    async function changed(action: () => Promise<unknown>, total: number, addressIs: (url: URL) => boolean) {
        await action();
        await expectTotal(page, total);
        await expect.poll(() => addressIs(new URL(address()))).toBe(true);
    }
    try {
        // A saved view chosen from Saved Views on an open page with the map showing.
        await openPlain();
        let [before, from] = await now();
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await page.getByRole('button', { name: new RegExp(`^${view.name}`) }).click();
        await expectTotal(page, KANSAS);
        await expect(outlines(page)).toHaveText(['US-KS included']);
        await staysOn(page, requests, from, [before], 'a saved view chosen from Saved Views');

        // A dataset whose default view holds a region: Show all images, then the Default view button brings it back.
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: shared.id }) });
        await openOnDefaultView();
        await changed(() => page.getByRole('button', { name: 'Show all images' }).click(), TOTAL, noFilters);
        [before, from] = await now();
        await page.getByRole('button', { name: 'Default view', exact: true }).click();
        await expectTotal(page, KANSAS);
        await expect(outlines(page)).toHaveText(['US-KS included']);
        await staysOn(page, requests, from, [before], 'the Default view button');

        // Other filters instead, then Clear All goes back to the default view.
        await openOnDefaultView();
        await changed(() => page.getByRole('button', { name: 'Show all images' }).click(), TOTAL, noFilters);
        await changed(() => addQuery(page, 'place = "nebraska"'), 3, onNebraska);
        [before, from] = await now();
        await page.getByRole('button', { name: 'Clear All' }).click();
        await expectTotal(page, KANSAS);
        await expect(outlines(page)).toHaveText(['US-KS included']);
        await staysOn(page, requests, from, [before], 'Clear All going back to the default view');

        // The chip removed, then Back brings it back.
        await openOnDefaultView();
        await changed(() => removeChip(page), TOTAL, noFilters);
        [before, from] = await now();
        await page.goBack();
        await expectTotal(page, KANSAS);
        await expect(outlines(page)).toHaveText(['US-KS included']);
        await staysOn(page, requests, from, [before], 'Back to the chip');

        // Forward onto an entry with the region: Show all images, Default view, Back, then Forward.
        await openOnDefaultView();
        await changed(() => page.getByRole('button', { name: 'Show all images' }).click(), TOTAL, noFilters);
        await changed(() => page.getByRole('button', { name: 'Default view', exact: true }).click(), KANSAS, onDefaultView);
        await page.goBack();
        await expectTotal(page, TOTAL);
        await expect(outlines(page)).toHaveCount(0);
        [before, from] = await now();
        await page.goForward();
        await expectTotal(page, KANSAS);
        await expect(outlines(page)).toHaveText(['US-KS included']);
        await staysOn(page, requests, from, [before], 'Forward onto the region');
    } finally {
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: null }) }).catch(() => undefined);
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
        await apiFetch(cookie, `/views/${shared.id}`, { method: 'DELETE' });
    }
});

test('on a dataset with objects, inside and not inside count the right images and objects', async ({ page }) => {
    test.setTimeout(120_000);
    const { id } = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-regions-objects-${S}` }) });
    try {
        const images = [
            ...PLACES.kansas.slice(0, 3).map(([lon, lat], i) => ({ filepath: `s3://e2e-regions-objects/ks_${i}.jpg`, location: { type: 'Point', coordinates: [lon, lat] } })),
            { filepath: 's3://e2e-regions-objects/ne_0.jpg', location: { type: 'Point', coordinates: PLACES.nebraska[0] } },
            { filepath: 's3://e2e-regions-objects/nowhere_0.jpg' },
        ];
        await apiFetch(cookie, `/datasets/${id}/samples`, { method: 'POST', body: JSON.stringify({ samples: images }) });
        const found = await apiFetch(cookie, `/datasets/${id}/samples/query`, { method: 'POST', body: JSON.stringify({ limit: 10, offset: 0 }) });
        const objects = (found.items as Array<{ id: number }>).flatMap(image => [0, 1].map(n => ({
            filepath: `s3://e2e-regions-objects/${image.id}#${n}`, media_type: 'object', metadata: { parent_id: image.id },
        })));
        await apiFetch(cookie, `/datasets/${id}/samples`, { method: 'POST', body: JSON.stringify({ samples: objects }) });
        await apiFetch(cookie, `/datasets/${id}/refresh-stats`, { method: 'POST' });

        const count = page.getByTestId('grid-view-count');
        const grain = page.getByTestId('grain-toggle');
        for (const [query, imagesText, objectsText] of [
            ['location within "US-KS"', '3 images', '6 objects'],
            ['location not within "US-KS"', '2 images', '4 objects'],  // the image with no location included
        ]) {
            await page.goto(`/datasets/${id}?q=${encodeURIComponent(query)}`);
            await expect(count, query).toHaveText(imagesText, { timeout: 20_000 });
            await grain.getByRole('button', { name: 'Objects' }).click();
            await expect(count, query).toHaveText(objectsText, { timeout: 20_000 });
            await grain.getByRole('button', { name: 'Images' }).click();
        }
    } finally {
        await deleteDatasetAndWait(cookie, id, 120_000);
    }
});

/** A job as GET /api/jobs/<id> answers, shaped from a real one. */
async function jobLike(changes: Record<string, unknown>): Promise<Record<string, unknown>> {
    const listed = await apiFetch(cookie, `/datasets/${datasetId}/jobs?include_system_statistics=true&limit=1`);
    const real = await apiFetch(cookie, `/jobs/${listed.items[0].id}`);
    return { ...real, dataset_id: null, ...changes };
}

const DOWNLOAD_FAILED = 'Could not download https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.1/geojson/'
    + 'ne_10m_admin_1_states_provinces.geojson: All connection attempts failed. On a server with no internet, load the file '
    + 'with python -m backend.regions load <file> --set natural-earth-admin1';

test('the Region boundaries card shows the loaded set with its counts', async ({ page }) => {
    await page.goto('/settings/status');
    const card = page.getByTestId('status-regions');
    await expect(card.getByRole('heading', { name: 'Region boundaries', level: 2 })).toBeVisible();
    await expect(card.getByTestId('status-regions-badge')).toHaveText('LOADED');
    await expect(card).toContainText(
        'Countries, states or provinces, and groups of states, for filters such as location within "US-KS". A filter naming a region is refused until its boundaries are loaded.',
    );
    const set = card.getByTestId('status-regions-set');
    await expect(set).toContainText('Natural Earth 1:10m admin 1 states and provinces 5.1.1');
    await expect(set.getByTestId('status-regions-loaded')).toHaveText(
        new RegExp(`^Version ${FIXTURE_VERSION} loaded .+ from a file on the server: 5 countries, 8 states or provinces and 3 groups\\.$`),
    );
    await expect(set).toContainText('This version of Freedom Ridge loads version 5.1.1: download and load it again to update.');
    await expect(set).toContainText('Downloaded from https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.1/geojson/ne_10m_admin_1_states_provinces.geojson · Public domain');
    await expect(set.getByRole('button', { name: 'Download and load again' })).toBeEnabled();
    await expect(set.getByTestId('status-regions-not-loaded')).toHaveCount(0);
});

test('the card, not loaded, follows the load its button starts to its failure, with the command that loads from disk (answers faked)', async ({ page }) => {
    // The real button downloads 40.7 MB: here the page's admin requests are answered by the test.
    const real = await apiFetch(cookie, '/admin/regions');
    await page.route('**/api/admin/regions', (route: Route) => route.request().method() === 'GET'
        ? route.fulfill({ json: { sets: real.sets.map((set: Record<string, unknown>) => ({ ...set, loaded: null, load_job: null })) } })
        : route.continue());
    const jobId = '00000000-0000-4000-8000-00000000e2e0';
    let loads = 0;
    await page.route('**/api/admin/regions/load', (route: Route) => {
        loads += 1;
        return route.fulfill({ status: 202, json: { job_id: jobId, state: 'queued', started: true } });
    });
    const queued = await jobLike({ id: jobId, kind: 'region_load', state: 'queued', started_at: null, finished_at: null, error: null });
    const running = { ...queued, state: 'running', started_at: new Date().toISOString() };
    const failed = { ...running, state: 'failed', finished_at: new Date().toISOString(), error: { type: 'RegionLoadError', message: DOWNLOAD_FAILED } };
    let reads = 0;
    await page.route(`**/api/jobs/${jobId}`, (route: Route) => {
        reads += 1;
        return route.fulfill({ json: reads <= 1 ? queued : reads <= 2 ? running : failed });
    });

    await page.goto('/settings/status');
    const card = page.getByTestId('status-regions');
    await expect(card.getByTestId('status-regions-badge')).toHaveText('NOT LOADED');
    await expect(card.getByTestId('status-regions-not-loaded')).toHaveText('Not loaded.');
    const button = card.getByRole('button', { name: 'Download and load', exact: true });
    await button.click();
    await expect(card.getByTestId('status-regions-badge')).toHaveText('LOADING');
    await expect(button).toBeDisabled();
    const status = card.getByTestId('status-regions-job');
    await expect(status).toHaveRole('status');
    await expect(status).toContainText(/Waiting to start\. It's listed under Background jobs below\.|Downloading and loading, started /);
    await expect(status).toContainText('Downloading and loading, started ', { timeout: 10_000 });
    await expect(card.getByTestId('status-regions-failure')).toHaveText(`The load failed: ${DOWNLOAD_FAILED}`, { timeout: 15_000 });
    await expect(card.getByTestId('status-regions-badge')).toHaveText('FAILED');
    await expect(button).toBeEnabled();
    expect(loads).toBe(1);
});

for (const outcome of ['LOADED', 'FAILED'] as const) {
    test(`a page opened, and reloaded, while the listing names a running load follows it without starting one, to ${outcome} as the job ends (answers faked)`, async ({ page }) => {
        const real = await apiFetch(cookie, '/admin/regions');
        const jobId = `00000000-0000-4000-8000-00000000e2e${outcome === 'LOADED' ? '2' : '3'}`;
        let ended = false;
        const listings: string[] = [];
        await page.route('**/api/admin/regions', (route: Route) => {
            if (route.request().method() !== 'GET') return route.continue();
            listings.push(ended ? 'after the job' : 'while it runs');
            const sets = real.sets.map((set: Record<string, unknown>) => (ended
                ? { ...set, loaded: outcome === 'LOADED' ? set.loaded : null, load_job: null }
                : { ...set, loaded: null, load_job: { job_id: jobId, state: 'running' } }));
            return route.fulfill({ json: { sets } });
        });
        let loads = 0;
        await page.route('**/api/admin/regions/load', (route: Route) => {
            loads += 1;
            return route.fulfill({ status: 500, json: { detail: 'e2e: no load may be started here' } });
        });
        const running = await jobLike({ id: jobId, kind: 'region_load', state: 'running', started_at: new Date().toISOString(), finished_at: null, error: null });
        const message = '5 countries, 8 states or provinces and 3 groups loaded (e2e-fixture-1)';
        const ending = outcome === 'LOADED'
            ? { ...running, state: 'succeeded', finished_at: new Date().toISOString(), progress: { ...(running.progress as object ?? {}), message }, result: { message } }
            : { ...running, state: 'failed', finished_at: new Date().toISOString(), error: { type: 'RegionLoadError', message: DOWNLOAD_FAILED } };
        await page.route(`**/api/jobs/${jobId}`, (route: Route) => route.fulfill({ json: ended ? ending : running }));

        const card = page.getByTestId('status-regions');
        const badge = card.getByTestId('status-regions-badge');
        const button = card.getByRole('button', { name: 'Download and load', exact: true });
        const status = card.getByTestId('status-regions-job');
        for (const how of ['opened', 'reloaded']) {
            if (how === 'opened') await page.goto('/settings/status');
            else await page.reload();
            await expect(badge, how).toHaveText('LOADING');
            await expect(button, how).toBeDisabled();
            await expect(status, how).toHaveRole('status');
            await expect(status, how).toContainText('A load was already running; this is its progress.');
            await expect(status, how).toContainText('Downloading and loading, started ');
        }

        ended = true;
        if (outcome === 'LOADED') {
            await expect(badge).toHaveText('LOADED', { timeout: 15_000 });
            await expect(status).toContainText(`${message}.`);
            await expect(card.getByTestId('status-regions-loaded')).toHaveText(
                new RegExp(`^Version ${FIXTURE_VERSION} loaded .+ from a file on the server: 5 countries, 8 states or provinces and 3 groups\\.$`),
            );
            await expect(card.getByRole('button', { name: 'Download and load again', exact: true })).toBeEnabled();
        } else {
            await expect(card.getByTestId('status-regions-failure')).toHaveText(`The load failed: ${DOWNLOAD_FAILED}`, { timeout: 15_000 });
            await expect(badge).toHaveText('FAILED');
            await expect(button).toBeEnabled();
        }
        await expect(status).not.toContainText('A load was already running');
        expect(loads, 'no load request sent').toBe(0);
        // The listing is read when the page opens (and reloads), and again when the followed job ends.
        expect(listings.filter(read => read === 'while it runs').length, 'listing reads while the job ran').toBeGreaterThanOrEqual(2);
        expect(listings.at(-1), 'the last listing read').toBe('after the job');
    });
}

test('a load the server refuses shows its words; a region load job is "Loading region boundaries" among Background jobs (answers faked)', async ({ page }) => {
    await page.route('**/api/admin/regions/load', (route: Route) => route.fulfill({ status: 409, json: { detail: 'e2e: a load is refused' } }));
    const failedJob = await jobLike({
        id: '00000000-0000-4000-8000-00000000e2e1', kind: 'region_load', state: 'failed', lane: 'maintenance',
        error: { type: 'RegionLoadError', message: DOWNLOAD_FAILED }, finished_at: new Date().toISOString(),
    });
    await page.route('**/api/admin/jobs?*', (route: Route) => route.fulfill({ json: { items: [failedJob], next_cursor: null } }));
    await page.goto('/settings/status');
    const card = page.getByTestId('status-regions');
    await card.getByRole('button', { name: /^Download and load/ }).click();
    await expect(card.getByRole('alert')).toHaveText('e2e: a load is refused');
    const job = page.getByTestId('job-history-item').filter({ hasText: 'Loading region boundaries' });
    await expect(job).toHaveCount(1);
    await expect(job).toContainText(`RegionLoadError: ${DOWNLOAD_FAILED}`);
});

test('someone who isn\'t an admin has no Region boundaries card, and the server refuses them the sets', async ({ browser }) => {
    let user: TestUser | undefined;
    try {
        user = await createUser(cookie, `e2e_regions_${S}`);
        const { context, page } = await signIn(browser, user);
        try {
            await page.goto('/settings/status');
            await expect(page.getByText('Access Denied')).toBeVisible();
            await expect(page.getByTestId('status-regions')).toHaveCount(0);
            expect((await page.request.get('/api/admin/regions')).status()).toBe(403);
        } finally {
            await context.close();
        }
    } finally {
        await allSteps(user ? [() => apiFetch(cookie, `/users/${user!.id}`, { method: 'DELETE' })] : []);
    }
});
