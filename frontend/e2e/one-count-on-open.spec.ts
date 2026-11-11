import { randomUUID } from 'node:crypto';
import { test, expect, type Page, type Request } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait, readState } from './api';
import { AUTH_STATE, expectTotal, gotoDataset } from './helpers';

// Opening a dataset asked for the same COUNT twice: the top bar (applied filters, image grain) and the
// grid (its staged filters and grain) sent identical bodies under two query keys, so React Query could
// not share them; that is two full counts per open. On a dataset with object rows
// and an applied filter, both were sent once more when "does it have objects" answered (that answer
// changes the filter's shape), and so was the grid's first page. A count request is POST samples/query
// with limit 0; the has-objects probe (media_type = object) is not one of the counts compared here.
test.use({ storageState: AUTH_STATE });

const IMAGE_GRAIN = { field: 'media_type', operator: '!=', value: 'object' };
const OBJECT_PROBE = JSON.stringify({ logic: 'AND', filters: [{ field: 'media_type', operator: '=', value: 'object' }] });

type QueryBody = { limit?: number; filters?: unknown };

/** A count request (limit 0) of `datasetId`, other than the has-objects probe. */
function isCount(request: Request, datasetId: number) {
    if (request.method() !== 'POST' || !request.url().endsWith(`/api/datasets/${datasetId}/samples/query`)) return false;
    const body = request.postDataJSON() as QueryBody;
    return body.limit === 0 && JSON.stringify(body.filters) !== OBJECT_PROBE;
}

/** Every samples/query request the page sends for `datasetId`: counts (limit 0) and first pages. */
function recordQueries(page: Page, datasetId: number) {
    const seen = { counts: [] as unknown[], firstPages: 0 };
    page.on('request', request => {
        if (request.method() !== 'POST' || !request.url().endsWith(`/api/datasets/${datasetId}/samples/query`)) return;
        const body = request.postDataJSON() as QueryBody;
        if (isCount(request, datasetId)) seen.counts.push(body.filters);
        if (body.limit === 200 && (body as { offset?: number }).offset === 0) seen.firstPages += 1;
    });
    return seen;
}

/** Opens `datasetId` with `stages` applied and `drafts` staged in the sidebar, as the app persists both. */
async function openWith(page: Page, datasetId: number, stages: object[], drafts: object = {}) {
    await page.addInitScript(([id, viewStages, simpleFilters]) => {
        localStorage.setItem(`fridge-filters-${id}`, JSON.stringify({
            viewStages, filterMode: 'simple', simpleFilters, advancedFilters: { logic: 'AND', filters: [] },
            brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
        }));
    }, [datasetId, stages, drafts] as const);
    await page.goto(`/datasets/${datasetId}`);
}

const gridCount = (page: Page) => page.getByTestId('grid-view-count');
const NOT_HIDDEN = { type: 'filter', description: 'not hidden', filter: { field: 'tags', operator: '!=', value: 'hide' } };

test('opening a dataset sends one count when the top bar and the grid ask the same', async ({ page }) => {
    const { datasetId } = readState();
    const seen = recordQueries(page, datasetId);
    await gotoDataset(page);
    await expectTotal(page, 12);
    await expect(gridCount(page)).toHaveText(/^\s*12 images\s*$/);
    await page.waitForTimeout(1_500);  // anything still to come (a second count went out at once)
    expect(seen.counts).toEqual([{ logic: 'AND', filters: [IMAGE_GRAIN] }]);
});

test('a staged filter keeps its own grid count, and the top bar counts only what is applied', async ({ page }) => {
    const { datasetId } = readState();
    const seen = recordQueries(page, datasetId);
    await openWith(page, datasetId, [], { idx_min: 6 });
    await expectTotal(page, 12);
    await expect(gridCount(page)).toHaveText(/^\s*6 images\s*$/);
    await page.waitForTimeout(1_500);
    expect(seen.counts).toHaveLength(2);
    expect(seen.counts).toContainEqual({ logic: 'AND', filters: [IMAGE_GRAIN] });
    expect(seen.counts).toContainEqual({ logic: 'AND', filters: [IMAGE_GRAIN, { field: 'idx', operator: '>=', value: 6 }] });
});

test('Retry asks the count again at once, also while it waits out a refusal', async ({ page }) => {
    // A refused count (a concept being rescored) retries on its own turn, every 5 s. Retry reloaded the
    // first page at once but left the count to that turn: one page and a short scrollbar meanwhile.
    const { datasetId } = readState();
    const refusal = 'Concept "water" is being rescored; use it again when its scoring job finishes';
    // Counts and first pages are refused. The pages stay refused until the count is answered, so the grid
    // keeps its Retry (a first page let through by its own retry turn would take it away).
    const refused = { counts: true, pages: true };
    await page.route(`**/api/datasets/${datasetId}/samples/query`, route => {
        const request = route.request();
        const refuse = isCount(request, datasetId) ? refused.counts : request.postDataJSON()?.limit === 200 && refused.pages;
        return refuse ? route.fulfill({ status: 409, json: { detail: refusal } }) : route.continue();
    });
    await gotoDataset(page);
    const unavailable = page.getByTestId('grid-unavailable');
    await expect(unavailable).toContainText('is being rescored', { timeout: 15_000 });
    // Just refused: the count now waits for its next turn, 5 s away.
    await page.waitForResponse(response => isCount(response.request(), datasetId) && response.status() === 409, { timeout: 15_000 });
    refused.counts = false;
    const asked = page.waitForRequest(request => isCount(request, datasetId), { timeout: 2_000 });
    await unavailable.getByRole('button', { name: 'Retry' }).click();
    await asked;
    await expect(gridCount(page)).toHaveText(/^\s*12 images\s*$/, { timeout: 2_000 });
    await expectTotal(page, 12);
    refused.pages = false;
    await expect(page.getByTestId('grid-cell')).toHaveCount(12, { timeout: 15_000 });
});

test.describe('datasets of its own', () => {
    let cookie = '';
    const created: number[] = [];

    test.beforeAll(async () => {
        cookie = await adminLogin();
    });
    test.afterAll(async () => {
        for (const id of created) await deleteDatasetAndWait(cookie, id, 120_000);
    });

    async function dataset(name: string, images: object[], objects: (ids: number[]) => object[] = () => []) {
        const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `${name}-${randomUUID()}` }) });
        created.push(ds.id);
        const { ids } = await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples: images }) });
        const children = objects(ids);
        if (children.length) await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples: children }) });
        await apiFetch(cookie, `/datasets/${ds.id}/refresh-stats`, { method: 'POST' });
        return { id: ds.id as number, ids: ids as number[] };
    }

    const sixImages = (prefix: string) =>
        Array.from({ length: 6 }, (_, i) => ({ filepath: `s3://${prefix}/img_${i}.jpg`, metadata: { idx: i } }));

    /** Opens `id` with "not hidden" applied and waits until both counts show `n`. */
    async function openNotHidden(page: Page, id: number, n: number) {
        await openWith(page, id, [NOT_HIDDEN]);
        await expectTotal(page, n);
        await expect(gridCount(page)).toHaveText(new RegExp(`^\\s*${n} images\\s*$`));
        await page.waitForTimeout(1_500);
    }

    async function tagHidden(page: Page, cell: number) {
        await page.getByTestId('grid-cell').nth(cell).click();
        await page.getByTestId('modal-tag').click();
        await page.getByPlaceholder('Enter tag name...').fill('hide');
        await page.getByRole('button', { name: 'Add', exact: true }).click();
        await page.getByRole('button', { name: 'Apply Changes' }).click();
    }

    test('with object rows and an applied filter, the count and the first page are asked once', async ({ page }) => {
        // Whether the dataset has object rows decides the filter's shape (withGrain's cross-grain link);
        // asked before that answer, the count and the first page went out in both shapes.
        const { id } = await dataset('e2e-one-count-objects',
            Array.from({ length: 4 }, (_, i) => ({ filepath: `s3://e2e-one-count/img_${i}.jpg`, media_type: 'image', metadata: { idx: i } })),
            ids => ids.slice(0, 2).map((parent, i) => ({
                filepath: `s3://e2e-one-count/img_${i}.jpg#o0`, media_type: 'object',
                metadata: { parent_id: parent, crop_bbox: [0, 0, 0.5, 0.5], label: 'thing' },
            })));
        const seen = recordQueries(page, id);
        await openWith(page, id, [{ type: 'filter', description: 'idx < 3', filter: { field: 'idx', operator: '<', value: 3 } }]);
        await expectTotal(page, 3);
        await expect(gridCount(page)).toHaveText(/^\s*3 images\s*$/);
        await page.waitForTimeout(1_500);
        expect(seen.counts).toHaveLength(1);
        expect(JSON.stringify(seen.counts[0])).toContain('has_matching_object');  // the shape for a dataset with objects
        expect(seen.firstPages).toBe(1);
    });

    test('after a tag edit both counts are asked again, once', async ({ page }) => {
        // The top bar used to keep its number after a tag edit (only the grid's count was asked again).
        const { id } = await dataset('e2e-one-count-tag', sixImages('e2e-one-count-tag'));
        await openNotHidden(page, id, 6);
        const seen = recordQueries(page, id);
        await tagHidden(page, 2);

        await expectTotal(page, 5);
        await expect(gridCount(page)).toHaveText(/^\s*5 images\s*$/);
        await page.waitForTimeout(1_500);
        expect(seen.counts).toHaveLength(1);
    });

    test('after a tag edit the top bar keeps its number until the new count arrives, but not across a filter change', async ({ page }) => {
        // The version moved by a tag edit gave the count a new key with no data: "Counting…" for a whole
        // COUNT (and no number for the export dialog or the similarity modal). For the same filters the
        // previous number stays; for other filters it would be wrong, so those still say Counting….
        const { id } = await dataset('e2e-one-count-keep', sixImages('e2e-one-count-keep'));
        await openNotHidden(page, id, 6);
        await page.route(`**/api/datasets/${id}/samples/query`, async route => {
            if (isCount(route.request(), id)) await new Promise(resolve => setTimeout(resolve, 3_000));
            await route.continue();
        });
        const total = page.getByTestId('total-samples');

        const recount = page.waitForRequest(request => isCount(request, id));
        await tagHidden(page, 0);
        await recount;
        await expect(total).toHaveText(/^\s*6 samples\s*$/, { timeout: 1_000 });  // the count is held
        await expectTotal(page, 5);

        await page.keyboard.press('Escape');  // close the single-image view
        await expect(page.getByTitle('Next sample (Right arrow)')).toHaveCount(0);
        const other = page.waitForRequest(request => isCount(request, id));
        await page.getByRole('button', { name: 'Clear All' }).click();
        await other;
        await expect(total).toHaveText('Counting…', { timeout: 1_000 });
        await expectTotal(page, 6);
    });

    test('a finished bulk job asks each count again once', async ({ page }) => {
        // notifySamplesMutated moves the count's version; the "refresh this dataset's views" invalidation
        // right after it also refetched the old version's key, which React had not left yet: two counts.
        const { id } = await dataset('e2e-one-count-job', sixImages('e2e-one-count-job'));
        await openNotHidden(page, id, 6);
        const seen = recordQueries(page, id);
        const submitted = await page.request.post(`/api/datasets/${id}/samples/bulk-tag`, {
            data: { filters: { field: 'idx', operator: '<', value: 2 }, add_tags: ['hide'] },
        });
        expect(submitted.status()).toBe(202);
        await expect.poll(async () => ((await (await page.request.get(`/api/datasets/${id}/jobs`)).json()).items ?? [])
            .some((job: { kind: string; state: string }) => job.kind === 'bulk_mutation' && job.state === 'succeeded'),
        { timeout: 30_000 }).toBe(true);
        // As when a long action answers 202: the page reads its job history now and sees the job finished.
        await page.evaluate(datasetId => window.dispatchEvent(new CustomEvent('fridge:job-submitted', { detail: { datasetId } })), id);
        await expectTotal(page, 4);
        await expect(gridCount(page)).toHaveText(/^\s*4 images\s*$/);
        await page.waitForTimeout(1_500);
        expect(seen.counts).toHaveLength(1);
    });

    test('a version restore asks each count again once', async ({ page }) => {
        // The restore invalidated every query and then moved the version: the old key and the new one.
        const { id, ids } = await dataset('e2e-one-count-revert', sixImages('e2e-one-count-revert'));
        await apiFetch(cookie, `/datasets/${id}/versions`, { method: 'POST', body: JSON.stringify({ name: 'before' }) });
        await apiFetch(cookie, `/datasets/${id}/samples/bulk-tag`, { method: 'POST', body: JSON.stringify({ sample_ids: [ids[0]], add_tags: ['hide'] }) });
        await apiFetch(cookie, `/datasets/${id}/versions`, { method: 'POST', body: JSON.stringify({ name: 'after' }) });
        await openNotHidden(page, id, 5);
        const seen = recordQueries(page, id);
        page.on('dialog', dialog => dialog.accept());
        await page.getByTitle('Dataset Settings').click();
        await page.getByRole('button', { name: 'Versions', exact: true }).click();
        const before = page.getByText('before', { exact: true }).locator('xpath=ancestor::div[contains(@class, "rounded-lg")][1]');
        await before.getByRole('button', { name: 'Revert to this snapshot' }).click();
        await expectTotal(page, 6);
        await expect(gridCount(page)).toHaveText(/^\s*6 images\s*$/);
        await page.waitForTimeout(1_500);
        expect(seen.counts).toHaveLength(1);
    });
});
