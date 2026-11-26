import { test, expect, type Page } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expectTotal, fillStable } from './helpers';
import { stamp } from './access-helpers';
import { loadRegionFixture, regionsNotOptedIn } from './regions-fixture';

// One place exclusion, however the filter card saves it (the backend-fixes contract, section 3.4). Typed in the Query
// tab and applied, `location not within "US-KS"` is saved bare; typed there, switched to the Builder tab and applied,
// it is saved as the only rule of an AND group. Both are the chip "Not in Kansas", and on a dataset whose objects
// carry no location both must keep the same images: the ones outside Kansas and the one with no location, never an
// image inside Kansas brought back through its location-less object. On a dataset without objects the two shapes
// already agree (pinned). The boundaries are the region specs' fixture (regions-fixture.ts), loaded only in a run that
// opts in with FRIDGE_E2E_LOAD_REGIONS=1; without it the file skips and creates nothing.
test.use({ storageState: AUTH_STATE, permissions: ['clipboard-read', 'clipboard-write'] });
test.skip(regionsNotOptedIn !== null, regionsNotOptedIn ?? '');

const S = stamp();
const TEXT = 'location not within "US-KS"';
const NOT_KS = 'Not in Kansas, United States of America';
// Points inside the fixture's rectangles, [lon, lat], and one image with no location.
const IMAGES: Array<[string, [number, number] | null]> = [
    ['ks_0', [-99, 38]],
    ['ks_1', [-98, 38.5]],
    ['ks_2', [-97, 39]],
    ['ne_0', [-100, 41]],
    ['ne_1', [-98, 41.5]],
    ['nowhere_0', null],
];
const OUTSIDE = IMAGES.filter(([name]) => !name.startsWith('ks_')).map(([name]) => name).sort();
let cookie = '';
let notLoaded: string | null = null;
const made: Record<'plain' | 'objects', number> = { plain: 0, objects: 0 };

const path = (dataset: string, name: string) => `s3://e2e-place-exclusion-${S}/${dataset}/${name}.jpg`;

async function makeDataset(kind: 'plain' | 'objects'): Promise<number> {
    const { id } = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-place-exclusion-${kind}-${S}` }) });
    const images = IMAGES.map(([name, point]) => ({
        filepath: path(kind, name),
        metadata: { place: name },
        ...(point ? { location: { type: 'Point', coordinates: point } } : {}),
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${id}/samples`, { method: 'POST', body: JSON.stringify({ samples: images }) });
    if (kind === 'objects') {
        // One object under each image, with no location of its own.
        const objects = (ids as number[]).map((parent, i) => ({
            filepath: `${path(kind, IMAGES[i][0])}#object`, media_type: 'object', metadata: { parent_id: parent, label: 'crate' },
        }));
        await apiFetch(cookie, `/datasets/${id}/samples`, { method: 'POST', body: JSON.stringify({ samples: objects }) });
    }
    await apiFetch(cookie, `/datasets/${id}/refresh-stats`, { method: 'POST' });
    // The page reads the field statistics as it opens: wait until they are counted, object rows included.
    const kinds = kind === 'objects' ? ['image', 'object'] : ['image'];
    await expect.poll(async () => {
        const fields = await apiFetch(cookie, `/datasets/${id}/field-stats`) as Array<{ name: string; stats_pending?: boolean; values?: string[] }>;
        const media = fields.find(field => field.name === 'media_type');
        return fields.every(field => !field.stats_pending) && kinds.every(each => media?.values?.includes(each));
    }, { timeout: 60_000, message: `field statistics of ${kind}` }).toBe(true);
    return id;
}

test.beforeAll(async () => {
    test.setTimeout(180_000);
    cookie = await adminLogin();
    notLoaded = await loadRegionFixture(cookie);
    if (notLoaded) return;
    made.plain = await makeDataset('plain');
    made.objects = await makeDataset('objects');
});

test.afterAll(async () => {
    test.setTimeout(180_000);
    for (const id of Object.values(made)) if (id) await deleteDatasetAndWait(cookie, id, 120_000);
});

test.beforeEach(() => {
    test.skip(notLoaded !== null, notLoaded ?? '');
});

const chips = (page: Page) => page.getByTestId('query-stage-chip');

/** The grid's queries as the page sends them (the ones that list samples), newest last. */
function gridQueries(page: Page): Array<Record<string, unknown>> {
    const sent: Array<Record<string, unknown>> = [];
    page.on('request', request => {
        if (request.method() !== 'POST' || !request.url().includes('/samples/query')) return;
        const body = request.postDataJSON() as Record<string, unknown> | null;
        if (body && typeof body.limit === 'number' && body.limit > 1) sent.push(body);
    });
    return sent;
}

/** The places of the images the grid shows: its newest query, sent again as the page sent it. */
async function shown(page: Page, sent: Array<Record<string, unknown>>, datasetId: number): Promise<string[]> {
    expect(sent.length, 'the grid sent no query').toBeGreaterThan(0);
    const answer = await apiFetch(cookie, `/datasets/${datasetId}/samples/query`, { method: 'POST', body: JSON.stringify(sent[sent.length - 1]) });
    return (answer.items as Array<{ metadata: { place: string } }>).map(item => item.metadata.place).sort();
}

async function openCard(page: Page) {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    const box = page.getByTestId('filter-card-query');
    await fillStable(box, TEXT);
    return box;
}

/** (a) The Query tab: the text applied as it is. */
async function applyFromQuery(page: Page) {
    const box = await openCard(page);
    await box.press('Control+Enter');
}

/** (b) The Query tab, then the Builder tab, then Apply: the Builder saves the rule inside an AND group. */
async function applyFromBuilder(page: Page) {
    const box = await openCard(page);
    await page.getByTestId('filter-mode-builder').click();
    await expect(box).toHaveCount(0);
    await page.getByTestId('filter-card-apply').click();
}

/**
 * Wait until the page has kept its filters as a reload or a link restores them: the address holds them (a `q=` or
 * `f=` with a value; none or an empty one with no stages) and the filters it stores for the dataset in this browser
 * hold `stages` stages. The page writes both a moment after
 * a change (the stored filters after a short delay, the address once the server has written the filter as text), so a
 * reload or a link taken sooner than a person could would lose a filter that was applied.
 */
async function waitUntilKept(page: Page, datasetId: number, stages: number) {
    await expect.poll(() => {
        // With no filter the address may keep an empty `q=` (the whole dataset).
        const params = new URL(page.url()).searchParams;
        return Boolean(params.get('q') || params.get('f'));
    }, { message: 'the address holds the filters', timeout: 15_000 }).toBe(stages > 0);
    await expect.poll(() => page.evaluate(id => {
        try {
            const stored = JSON.parse(localStorage.getItem(`fridge-filters-${id}`) || 'null');
            return Array.isArray(stored?.viewStages) ? stored.viewStages.length : 0;
        } catch {
            return -1;
        }
    }, datasetId), { message: 'the stored filters hold the stages', timeout: 15_000 }).toBe(stages);
}

async function expectNotInKansas(page: Page, sent: Array<Record<string, unknown>>, datasetId: number, where: string) {
    // The chip first, so a page that lost its filter (and shows every image) can't count as one that kept it.
    await expect(chips(page), where).toHaveCount(1);
    await expect(chips(page).first().getByRole('button').first(), where).toHaveAccessibleName(NOT_KS);
    await expectTotal(page, OUTSIDE.length);
    await expect(page.getByTestId('grid-cell'), where).toHaveCount(OUTSIDE.length);
    await expect.poll(() => shown(page, sent, datasetId), { message: where, timeout: 15_000 }).toEqual(OUTSIDE);
}

test('on a dataset whose objects carry no location, the Builder\'s shape of "not within" keeps what the Query tab\'s keeps', async ({ page, browser }) => {
    test.setTimeout(150_000);
    const id = made.objects;
    const sent = gridQueries(page);
    await page.goto(`/datasets/${id}`);
    await expectTotal(page, IMAGES.length);

    await applyFromQuery(page);
    await expectNotInKansas(page, sent, id, 'the Query tab');
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, IMAGES.length);
    await waitUntilKept(page, id, 0);  // the Query tab's filter gone from the address and the stored filters

    await applyFromBuilder(page);
    await expectNotInKansas(page, sent, id, 'the Builder tab');

    await waitUntilKept(page, id, 1);
    await page.reload();
    await expectNotInKansas(page, sent, id, 'after a reload');

    await waitUntilKept(page, id, 1);
    await page.getByTestId('copy-link').click();
    await expect(page.getByText(/^Link to these filters copied/)).toBeVisible();
    const link = await page.evaluate(() => navigator.clipboard.readText());
    const fresh = await browser.newContext({ storageState: AUTH_STATE });
    try {
        const other = await fresh.newPage();
        const sentThere = gridQueries(other);
        await other.goto(link);
        await expectNotInKansas(other, sentThere, id, 'Copy link, opened in a fresh browser');
    } finally {
        await fresh.close();
    }
});

test('on a dataset without objects, the Query tab\'s and the Builder\'s "not within" already keep the same images', async ({ page }) => {
    test.setTimeout(120_000);
    const id = made.plain;
    const sent = gridQueries(page);
    await page.goto(`/datasets/${id}`);
    await expectTotal(page, IMAGES.length);
    await applyFromQuery(page);
    await expectNotInKansas(page, sent, id, 'the Query tab');
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, IMAGES.length);
    await waitUntilKept(page, id, 0);
    await applyFromBuilder(page);
    await expectNotInKansas(page, sent, id, 'the Builder tab');
    await waitUntilKept(page, id, 1);
    await page.reload();
    await expectNotInKansas(page, sent, id, 'after a reload');
});
