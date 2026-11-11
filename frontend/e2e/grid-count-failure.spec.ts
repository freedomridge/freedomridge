import { test, expect, type Request } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// When the grid's COUNT failed (a guest's 30 s statement timeout on a large view on the public demo, asked three
// times more), the grid stopped at its first page of 200 rows and said "Counting…" for good, with no Retry: the
// rest of the view couldn't be reached. Now it says the count couldn't be had, keeps fetching pages as the view is
// scrolled until a short one, and offers to count again. This spec makes its own dataset of more than two pages;
// its counts are answered with the error a statement timeout gives.
test.use({ storageState: AUTH_STATE });

const DS_NAME = 'e2e-grid-count-failure';
const ROWS = 450;  // two full pages of 200 and a short one
const FAILURE = 'QueryCanceledError: canceling statement due to statement timeout';

let datasetId: number;
let ids: number[];
let cookie: string;

test.beforeAll(async () => {
    cookie = await adminLogin();
    for (const d of await apiFetch(cookie, '/datasets')) {
        if (d.name === DS_NAME) await deleteDatasetAndWait(cookie, d.id);  // a crashed run's
    }
    datasetId = (await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name: DS_NAME, description: 'count failure fixture' }),
    })).id;
    ({ ids } = await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
        method: 'POST',
        body: JSON.stringify({
            samples: Array.from({ length: ROWS }, (_, i) => ({ filepath: `s3://e2e-count-failure/img_${i}.jpg`, metadata: { idx: i } })),
        }),
    }));
    await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
});

test.afterAll(async () => {
    if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
});

const isQuery = (request: Request) =>
    request.method() === 'POST' && request.url().endsWith(`/api/datasets/${datasetId}/samples/query`);

// The grid's count (limit 0, the image grain); the open's has-objects probe asks `media_type = object`.
const isCount = (request: Request) =>
    isQuery(request) && request.postDataJSON()?.limit === 0 && JSON.stringify(request.postDataJSON().filters).includes('"!="');

test('a failed count keeps every row reachable, says so, and can be asked again', async ({ page }) => {
    let failCounts = true;
    await page.route(`**/api/datasets/${datasetId}/samples/query`, route =>
        failCounts && isCount(route.request())
            ? route.fulfill({ status: 500, json: { detail: FAILURE } })
            : route.continue());
    const pageOffsets: number[] = [];
    let lastRowShown = false;
    page.on('request', request => {
        if (isQuery(request) && request.postDataJSON()?.limit === 200) pageOffsets.push(request.postDataJSON().offset);
        if (request.url().includes(`/api/samples/${ids[ROWS - 1]}/media`)) lastRowShown = true;
    });

    await page.goto(`/datasets/${datasetId}`);
    const grid = page.getByTestId('panel-grid');
    const count = grid.getByTestId('grid-view-count');
    // The count is asked three times more first (about 7 s).
    await expect(count).toHaveText('Count unavailable', { timeout: 30_000 });
    const failed = grid.getByTestId('grid-count-failed');
    await expect(failed).toContainText(`Couldn't count this view: ${FAILURE}`);
    await expect(page.getByTestId('total-samples')).toHaveText('Count unavailable');

    // Scrolling on fetches the pages after the first, down to the last row, and no page after the short one.
    await grid.getByTestId('grid-cell').first().hover();
    await expect.poll(async () => {
        await page.mouse.wheel(0, 4_000);
        return lastRowShown;
    }, { timeout: 30_000 }).toBe(true);
    await expect(grid.locator('.animate-pulse')).toHaveCount(0);  // no placeholder tiles past the end
    expect(pageOffsets).toEqual(expect.arrayContaining([0, 200, 400]));
    expect(pageOffsets).not.toContain(600);

    // Retry counts again; the loaded rows stay (the first page isn't asked again).
    failCounts = false;
    const pagesBefore = pageOffsets.length;
    await failed.getByRole('button', { name: 'Retry' }).click();
    await expect(count).toHaveText(`${ROWS} images`, { timeout: 15_000 });
    await expect(failed).toHaveCount(0);
    await expect(page.getByTestId('total-samples')).toHaveText(`${ROWS} samples`);
    expect(pageOffsets.slice(pagesBefore)).not.toContain(0);
});
