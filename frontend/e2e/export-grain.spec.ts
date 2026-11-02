import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { API, adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// CSV export must apply the same IMAGE grain every other consumer applies.
//
// The bug: ExportDialog's getFilterForScope returned the raw stage tree, while the grid, map,
// histogram and the top-bar count all wrap it with withGrain() first. On a dataset that has
// object/patch rows, the dialog therefore showed the image-only `appliedCount` and then wrote a
// CSV containing those images PLUS every matching media_type='object' row. Silent — no error,
// just a wrong file.
//
// This can only be caught end-to-end: it is a frontend prop-threading bug, so no backend test
// reaches it, and the shared 12-sample fixture has no object rows to express it. So this spec
// builds its own small dataset (images + objects) and drives the real Export flow, including the
// download. Reverting the fix makes the final assertion fail with 12 rows instead of 8.
test.use({ storageState: AUTH_STATE });

const DS_NAME = 'e2e-export-grain';
const IMAGES = 8;
const OBJECTS = 4;

let datasetId: number;
let cookie: string;

test.beforeAll(async () => {
    cookie = await adminLogin();

    // Clean up a crashed previous run.
    for (const d of await apiFetch(cookie, '/datasets')) {
        if (d.name === DS_NAME) await deleteDatasetAndWait(cookie, d.id);
    }

    const ds = await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name: DS_NAME, description: 'export grain fixture' }),
    });
    datasetId = ds.id;

    // `grp` is on BOTH images and objects, so a filter on it matches every row in the dataset.
    // That is what makes the grain the only thing separating 8 from 12.
    const images = Array.from({ length: IMAGES }, (_, i) => ({
        filepath: `s3://e2e-export/img_${i}.jpg`,
        media_type: 'image',
        metadata: { grp: 'x', idx: i },
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: images }),
    });

    // Object filepaths are `<image>#o<n>` (the convention in python/fridge/test_objects.py);
    // `samples.filepath` is UNIQUE, so an object cannot reuse its parent's path.
    const objects = Array.from({ length: OBJECTS }, (_, i) => ({
        filepath: `s3://e2e-export/img_${i}.jpg#o0`,
        media_type: 'object',
        metadata: { grp: 'x', parent_id: ids[i], crop_bbox: [0, 0, 0.5, 0.5], label: 'thing' },
    }));
    await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: objects }),
    });

    await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
});

test.afterAll(async () => {
    if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
});

test('the fixture really is ambiguous without grain', async ({ page }) => {
    // Guards the guard: if this dataset ever stopped containing object rows that match the
    // filter, the export test below would pass for the wrong reason.
    const body = { filters: { logic: 'AND', filters: [{ field: 'grp', operator: '=', value: 'x' }] }, limit: 0 };
    const res = await page.request.post(`${API}/datasets/${datasetId}/samples/query`, { data: body });
    expect((await res.json()).total).toBe(IMAGES + OBJECTS);
});

test('CSV export applies image grain: the file matches the count the dialog shows', async ({ page }) => {
    // Seed an applied view stage. Export is only reachable with a stage/selection (canExport), and
    // `grp = x` matches images AND objects, so grain is the only thing that can separate them.
    await page.addInitScript((dsId: number) => {
        localStorage.setItem(`fridge-filters-${dsId}`, JSON.stringify({
            viewStages: [{
                type: 'filter',
                description: 'grp=x',
                filter: { logic: 'AND', filters: [{ field: 'grp', operator: '=', value: 'x' }] },
            }],
            filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
            brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
        }));
    }, datasetId);

    await page.goto(`/datasets/${datasetId}`);

    // The top bar is image-grained, so it shows the images only.
    await expect(page.getByTestId('total-samples')).toHaveText(
        // This dataset has object rows, so the sub-header counts — and says — images.
        new RegExp(`^\\s*${IMAGES} images\\s*$`), { timeout: 20_000 });

    await page.getByRole('button', { name: 'Export' }).click();

    // The dialog's own count for the applied scope is the image-grained one.
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText(new RegExp(`\\b${IMAGES}\\b`)).first()).toBeVisible({ timeout: 10_000 });

    const downloadPromise = page.waitForEvent('download');
    await dialog.getByRole('button', { name: /Export CSV/ }).click();
    const download = await downloadPromise;

    const stream = await download.createReadStream();
    const chunks: Buffer[] = [];
    for await (const c of stream) chunks.push(Buffer.from(c));
    const csv = Buffer.concat(chunks).toString('utf-8');

    const rows = csv.trim().split('\n');
    const dataRows = rows.length - 1; // minus the header

    // THE assertion. Ungrained (the bug) this is IMAGES + OBJECTS = 12.
    expect(dataRows, `CSV should hold the ${IMAGES} images, not the ${IMAGES + OBJECTS} images+objects`)
        .toBe(IMAGES);
    expect(csv).not.toContain('object');
});
