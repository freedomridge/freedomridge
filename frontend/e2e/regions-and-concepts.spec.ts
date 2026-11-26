import zlib from 'node:zlib';
import { test, expect, type Page } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expectTotal } from './helpers';
import { stamp } from './access-helpers';
import { loadRegionFixture } from './regions-fixture';

// A named region's chip and a concept's chip in one view (docs/REGIONS.md, docs/FILTER_QUERY.md): each feature gave
// its chip a text form, so together with a plain filter they are one readable q= link, in the address and by Copy
// link, which opens on the same three chips and the same samples. The boundaries are the regions suite's synthetic
// file (regions-fixture.ts); the samples are eight points in its Kansas and Nebraska, in two embedding groups.
test.use({ storageState: AUTH_STATE, permissions: ['clipboard-read', 'clipboard-write'] });

const S = stamp();
let cookie = '';
let datasetId = 0;
let notLoaded: string | null = null;
let sampleIds: number[] = [];

// idx 0-3 are group A, which "equipment" is seeded from; 4-7 group B. Each group has two points in each state.
const KANSAS: Array<[number, number]> = [[-99, 38], [-98, 38.5]];
const NEBRASKA: Array<[number, number]> = [[-100, 41], [-98, 41.5]];
const POINTS = [...KANSAS, ...NEBRASKA, ...KANSAS, ...NEBRASKA];

test.beforeAll(async () => {
    test.setTimeout(180_000);
    cookie = await adminLogin();
    notLoaded = await loadRegionFixture(cookie);
    const { id } = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-regions-concepts-${S}` }) });
    datasetId = id;
    const samples = POINTS.map(([lon, lat], idx) => ({
        filepath: `s3://e2e-regions-concepts/img_${idx}.jpg`,
        metadata: { idx },
        location: { type: 'Point', coordinates: [lon, lat] },
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${datasetId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    sampleIds = ids;
    await apiFetch(cookie, `/datasets/${datasetId}/embeddings`, {
        method: 'POST',
        body: JSON.stringify(ids.map((sampleId: number, idx: number) => {
            const vector = Array.from({ length: 8 }, () => 0);
            vector[idx < 4 ? 0 : 1] = 1;
            return { sample_id: sampleId, vector, field_name: 'e2e_clip' };
        })),
    });
    await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
    const made = await apiFetch(cookie, `/datasets/${datasetId}/concepts`, {
        method: 'POST',
        body: JSON.stringify({ name: 'equipment', embedding_field: 'e2e_clip', kind: 'seed', seed_ids: ids.slice(0, 4) }),
    });
    await jobDone(made.job_id);
});

async function jobDone(jobId?: string) {
    if (jobId) await expect.poll(async () => (await apiFetch(cookie, `/jobs/${jobId}`)).state, { timeout: 60_000 }).toBe('succeeded');
}

test.afterAll(async () => {
    test.setTimeout(120_000);
    if (datasetId) await deleteDatasetAndWait(cookie, datasetId, 120_000);
});

test.beforeEach(() => {
    test.skip(notLoaded !== null, notLoaded ?? '');
});

const chips = (page: Page) => page.getByTestId('query-stage-chip');
const q = (page: Page) => new URL(page.url()).searchParams.get('q');
const encodeStages = (stages: unknown[]) => `1.${zlib.deflateRawSync(Buffer.from(JSON.stringify({ stages }))).toString('base64url')}`;

test('a region chip, a concept chip and a plain filter are one q= link, which opens on the same three chips', async ({ page, browser }) => {
    const concepts = await apiFetch(cookie, `/datasets/${datasetId}/concepts`) as Array<{ id: number; name: string }>;
    const equipment = concepts.find(concept => concept.name === 'equipment')!;
    const hide = { field: '_concept', operator: 'concept_exclude', value: { concept_id: equipment.id, op: '>', threshold: 0.9 } };
    const stages = [
        { type: 'filter', description: 'location within US-KS', filter: { field: 'location', operator: 'geo_region', value: 'US-KS' } },
        {
            type: 'filter', description: 'Exclude: equipment', filter: hide,
            metadata: { concept: true, conceptId: equipment.id, conceptName: 'equipment', op: '>', threshold: 0.9, mode: 'exclude' },
        },
        { type: 'filter', description: 'idx ≥ 4', filter: { field: 'idx', operator: '>=', value: 4 } },
    ];
    const written = 'location within "US-KS" AND not concept "equipment" > 0.9 AND idx >= 4';
    // In Kansas: idx 0, 1, 4, 5; not the concept (group A hidden): 4-7; idx >= 4: 4-7. Together: idx 4 and 5.
    const expectChips = async (on: Page) => {
        await expectTotal(on, 2);
        await expect(chips(on)).toHaveText([/In Kansas, United States of America/, /Exclude: equipment/, /idx ≥ 4/]);
    };

    await page.goto(`/datasets/${datasetId}?f=${encodeStages(stages)}`);
    await expectChips(page);
    await expect.poll(() => q(page)).toBe(written);
    await page.getByTestId('copy-link').click();
    await expect(page.getByText(/^Link to these filters copied/)).toBeVisible();
    const link = new URL(await page.evaluate(() => navigator.clipboard.readText()));
    expect(link.searchParams.get('q')).toBe(written);
    expect([...link.searchParams.keys()]).toEqual(['q']);

    const fresh = await browser.newContext({ storageState: AUTH_STATE });
    try {
        const other = await fresh.newPage();
        await other.goto(link.href);
        await expectChips(other);
        await expect(other).toHaveURL(link.href);
        // The concept chip is the panel's, and the region chip still turns around.
        await other.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('concepts');
        await expect(other.getByTestId('concept-card').filter({ hasText: 'equipment' })).toContainText('⊘ excl');
        await chips(other).first().getByRole('button', { name: 'In Kansas, United States of America', exact: true }).click();
        await expect(chips(other).first()).toHaveText(/Not in Kansas, United States of America/);
        await expect.poll(() => q(other)).toBe('location not within "US-KS" AND not concept "equipment" > 0.9 AND idx >= 4');
        await expectTotal(other, 2);  // idx 6 and 7, in Nebraska
    } finally {
        await fresh.close();
    }
});

type MapRequest = { zoom: number; bbox: [number, number, number, number]; filters: string };

/** The map's view, as the map asks for its points (as in regions-ui.spec.ts). */
function mapRequests(page: Page): MapRequest[] {
    const seen: MapRequest[] = [];
    page.on('request', request => {
        if (request.method() !== 'POST' || !new URL(request.url()).pathname.endsWith(`/datasets/${datasetId}/map_points`)) return;
        const body = request.postDataJSON() ?? {};
        if (Array.isArray(body.bbox)) seen.push({ zoom: body.zoom, bbox: body.bbox, filters: JSON.stringify(body.filters ?? null) });
    });
    return seen;
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

// A view the app opens with fewer stages than it holds (a deleted concept's left out) is still a view opened, not
// filters applied in the page: the map doesn't fit its region chip, as for any view (map-panel.tsx regionsRestored).
test('a saved view with a region and a deleted concept opens, the map showing, without the concept\'s stage: it says so, keeps the region chip and doesn\'t move the map', async ({ page }) => {
    test.setTimeout(180_000);
    const name = `e2e-region-gone-concept-${S}`;
    const made = await apiFetch(cookie, `/datasets/${datasetId}/concepts`, {
        method: 'POST',
        body: JSON.stringify({ name: `gone-${S}`, embedding_field: 'e2e_clip', kind: 'seed', seed_ids: sampleIds.slice(4) }),
    });
    await jobDone(made.job_id);
    const concepts = await apiFetch(cookie, `/datasets/${datasetId}/concepts`) as Array<{ id: number; name: string }>;
    const gone = concepts.find(concept => concept.name === `gone-${S}`)!;
    const view = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({
            name,
            is_public: false,
            content: {
                viewStages: [
                    { type: 'filter', description: 'location within US-KS', filter: { field: 'location', operator: 'geo_region', value: 'US-KS' } },
                    {
                        type: 'filter',
                        description: `Exclude: ${gone.name}`,
                        filter: { field: '_concept', operator: 'concept_exclude', value: { concept_id: gone.id, op: '>', threshold: 0.62 } },
                        metadata: { concept: true, conceptId: gone.id, conceptName: gone.name, op: '>', threshold: 0.62, mode: 'exclude' },
                    },
                ],
            },
        }),
    });
    await apiFetch(cookie, `/datasets/${datasetId}/concepts/${gone.id}`, { method: 'DELETE' });
    try {
        const requests = mapRequests(page);
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, POINTS.length);
        await page.getByTestId('panel-embeddings').getByTestId('panel-type-select').selectOption('map');
        const map = page.getByTestId('panel-map');
        const noToken = map.getByText(/NEXT_PUBLIC_MAPBOX_TOKEN/);
        await expect(noToken.or(map.getByRole('button', { name: 'Zoom to Data' })).first()).toBeVisible({ timeout: 20_000 });
        test.skip(await noToken.isVisible(), 'No Mapbox token configured');
        await settled(requests);
        const home = requests.at(-1)!;

        await page.getByRole('button', { name: 'Saved Views' }).click();
        await page.getByRole('button', { name: new RegExp(`^${name}`) }).click();
        await expectTotal(page, KANSAS.length * 2);
        await expect(chips(page)).toHaveText([/In Kansas, United States of America/]);
        await expect(page.getByText(
            `From the view “${name}”, “Exclude: ${gone.name}” was left out: it filters by a concept this dataset no longer has.`,
            { exact: true },
        )).toBeVisible();
        await expect(page.getByTestId('map-region-outline')).toHaveText(['US-KS included']);
        await expect.poll(() => requests.some(r => r.filters !== home.filters), { timeout: 15_000 }).toBe(true);
        await settled(requests);
        const views = requests.slice(requests.indexOf(home)).map(r => ({ zoom: r.zoom, bbox: r.bbox }));
        expect(views, 'the map stays where it was').toEqual(views.map(() => ({ zoom: home.zoom, bbox: home.bbox })));
    } finally {
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});
