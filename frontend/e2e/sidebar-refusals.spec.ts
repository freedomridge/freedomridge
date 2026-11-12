import { test, expect, type Page, type Route } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, asLargeDataset, expandField, expectTotal } from './helpers';

test.use({ storageState: AUTH_STATE, viewport: { width: 1600, height: 1100 } });

// #138's refusals reaching the sidebar's streamed filtered values after the stream began: a concept
// being rescored (the field waits and retries, as every view does). Each answer is the real stream with
// refusal lines put in place of the fields, as the server writes them when the refusal comes mid-stream.
// (A lasso drawn on a re-fitted map was refused too until 2.11; it now filters, and its chip warns.)
const RESCORED = 'Concept "sky" is being rescored; use it again when its scoring job finishes';
const FIELD = 'e2e_refusal_lasso';
let cookie = '';
let dsId = 0;

test.beforeAll(async () => {
    test.setTimeout(120_000);
    cookie = await adminLogin();
    dsId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-sidebar-refusals-${Date.now()}` }) })).id;
    const samples = Array.from({ length: 60 }, (_, i) => ({
        filepath: `s3://e2e-sidebar-refusals/s${i}.jpg`,
        metadata: { level: i + 1, brightness: (i + 1) * 10 },
        embeddings: { [FIELD]: Array.from({ length: 8 }, (_, d) => Math.sin(i * 0.37 + d) + (i % 3) * 2) },
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${dsId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    const coordinates = (ids as number[]).map((sampleId, i) => ({ sample_id: sampleId, x: i % 10, y: Math.floor(i / 10) }));
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/coordinates?field_name=${FIELD}`, { method: 'POST', body: JSON.stringify(coordinates) });
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/recompute-viz-rank?field_name=${FIELD}&full=true`, { method: 'POST' });
    await apiFetch(cookie, `/datasets/${dsId}/refresh-stats`, { method: 'POST' });
});

test.afterAll(async () => {
    if (dsId) await deleteDatasetAndWait(cookie, dsId, 120_000);
});

// Opens the dataset with `stage` applied and `fields` expanded (as the app persists both).
async function openWith(page: Page, stage: object, fields: string[]) {
    await page.goto('/');
    await page.evaluate(({ id, stage, fields }) => {
        localStorage.setItem(`fridge-filters-${id}`, JSON.stringify({
            viewStages: [stage], filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
            brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
        }));
        localStorage.setItem(`fridge-sidebar-expanded-${id}`, JSON.stringify(fields));
    }, { id: dsId, stage, fields });
    await page.goto(`/datasets/${dsId}`);
}

// Answers the facet stream with `refusal` in place of every field, the first `times` times.
async function refuseFacets(page: Page, refusal: object, times: number) {
    let refused = 0;
    await page.route(`**/datasets/${dsId}/field-stats`, async (route: Route) => {
        const body = route.request().postDataJSON();
        if (!body?.fields || refused >= times) return route.continue();
        refused += 1;
        const lines = [...body.fields.map((name: string) => ({ name, refusal })), { done: true }];
        await route.fulfill({ contentType: 'application/x-ndjson', body: lines.map(line => `${JSON.stringify(line)}\n`).join('') });
    });
    return () => refused;
}

test('a concept rescored mid-stream: the fields wait for its scoring, then fill in when they retry', async ({ page }) => {
    const refused = await refuseFacets(page, { status: 409, detail: RESCORED }, 1);
    await openWith(page, { type: 'filter', description: 'level >= 55', filter: { field: 'level', operator: '>=', value: 55 } }, ['brightness']);
    await expectTotal(page, 6);
    const sidebar = page.getByTestId('panel-filters');
    const brightness = sidebar.getByTestId('facet-status-brightness');
    await expect(brightness).toHaveText('Dataset values · waiting for concept scoring…');
    expect(refused()).toBe(1);
    await expect(brightness).toHaveText(/Filtered values/, { timeout: 15_000 });  // the 5 s retry
    await expect(sidebar.getByText('550 - 600', { exact: true })).toBeVisible();
});

test('fields refused at different moments retry together, in one request', async ({ page }) => {
    await asLargeDataset(page, dsId);  // level stays collapsed until the test expands it
    const requests: string[][] = [];
    let refusing = true;
    await page.route(`**/datasets/${dsId}/field-stats`, async (route: Route) => {
        const body = route.request().postDataJSON();
        if (!body?.fields) return route.continue();
        requests.push([...body.fields].sort());
        if (!refusing) return route.continue();
        const lines = [...body.fields.map((name: string) => ({ name, refusal: { status: 409, detail: RESCORED } })), { done: true }];
        await route.fulfill({ contentType: 'application/x-ndjson', body: lines.map(line => `${JSON.stringify(line)}\n`).join('') });
    });
    // brightness filters the view, so its section opens and asks at once; level is opened 1.5 s later.
    await openWith(page, { type: 'filter', description: 'brightness >= 550', filter: { field: 'brightness', operator: '>=', value: 550 } }, []);
    await expectTotal(page, 6);
    const sidebar = page.getByTestId('panel-filters');
    const waiting = 'Dataset values · waiting for concept scoring…';
    await expect(sidebar.getByTestId('facet-status-brightness')).toHaveText(waiting);
    await page.waitForTimeout(1500);
    await expandField(page, 'level');
    await expect(sidebar.getByTestId('facet-status-level')).toHaveText(waiting);
    const levelAsked = requests.findIndex(fields => fields.includes('level'));
    // Each field used to retry 5 s after its own refusal, in a request of its own.
    await expect.poll(() => requests.length, { timeout: 12_000 }).toBeGreaterThan(levelAsked + 1);
    expect(requests[levelAsked + 1]).toEqual(['brightness', 'level']);
    const recovering = requests.length;
    refusing = false;
    await expect(sidebar.getByTestId('facet-status-brightness')).toHaveText(/Filtered values/, { timeout: 15_000 });
    await expect(sidebar.getByTestId('facet-status-level')).toHaveText(/Filtered values/);
    await expect(sidebar.getByText('55 - 60', { exact: true })).toBeVisible();
    expect(requests.slice(recovering)).toEqual([['brightness', 'level']]);
});
