import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// The Objects grid and the cross-grain link on a dataset of its own (object-view.spec.ts needs open-images-v7, which
// CI doesn't have). Objects are media_type='object' rows of their image's dataset, linked to it by samples.parent_id
// (from metadata.parent_id). A filter on one grain carries over to the other: the object grid shows the objects of
// the matching images, the image grid the images of the matching objects. An object whose parent_id names no image
// joins nothing.
test.use({ storageState: AUTH_STATE });

type Sample = { id: number; metadata: Record<string, unknown> };

async function objectDataset(cookie: string, name: string): Promise<number> {
    const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name, is_global: true }) });
    const images = ['corn', 'soy', 'wheat'].map(crop => ({ filepath: `s3://${name}/${crop}.jpg`, metadata: { crop } }));
    await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples: images }) });
    const found = await apiFetch(cookie, `/datasets/${ds.id}/samples/query`, {
        method: 'POST',
        body: JSON.stringify({ filters: { logic: 'AND', filters: [] }, limit: 10, offset: 0 }),
    });
    const image = Object.fromEntries(found.items.map((s: Sample) => [s.metadata.crop, s.id]));
    const object = (key: string, metadata: Record<string, unknown>) => ({
        filepath: `s3://${name}/${key}`, media_type: 'object', metadata,
    });
    const objects = [
        object('corn.jpg#0', { parent_id: image.corn, label: 'weed' }),
        object('corn.jpg#1', { parent_id: image.corn, label: 'weed' }),
        object('soy.jpg#0', { parent_id: image.soy, label: 'rock' }),
        object('loose#0', { parent_id: 'not an image', label: 'weed' }),
    ];
    await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples: objects }) });
    return ds.id;
}

// A persisted view-stage filter, so the page opens filtered through the same stagedFilters -> withGrain path the
// sidebar, the query builder and the lasso feed.
async function openFiltered(page: Page, datasetId: number, filter: Record<string, unknown> | null) {
    if (filter) {
        await page.addInitScript(([id, leaf]: [number, Record<string, unknown>]) => {
            localStorage.setItem(`fridge-filters-${id}`, JSON.stringify({
                viewStages: [{ type: 'filter', description: 'object-link', filter: { logic: 'AND', filters: [leaf] } }],
                filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
                brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
            }));
        }, [datasetId, filter] as [number, Record<string, unknown>]);
    }
    await page.goto(`/datasets/${datasetId}`);
}

// The grid's count in image grain, then in object grain. The label switches before the object count arrives, so each
// is waited for as an exact text.
async function expectCounts(page: Page, images: string, objects: string) {
    const count = page.getByTestId('grid-view-count');
    await expect(count).toHaveText(new RegExp(`^${images}$`), { timeout: 20_000 });
    await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();
    await expect(count).toHaveText(new RegExp(`^${objects}$`), { timeout: 20_000 });
}

async function withObjectDataset(run: (id: number) => Promise<void>) {
    const cookie = await adminLogin();
    const id = await objectDataset(cookie, `e2e-object-link-${Date.now()}-${Math.floor(Math.random() * 1e6)}`);
    try {
        await run(id);
    } finally {
        await deleteDatasetAndWait(cookie, id);
    }
}

test('the Objects grid shows every object of the dataset', async ({ page }) => {
    test.setTimeout(90_000);
    await withObjectDataset(async id => {
        await openFiltered(page, id, null);
        await expectCounts(page, '3 images', '4 objects');  // the loose one too: it is an object
    });
});

test('a filter on images shows their objects in the Objects grid', async ({ page }) => {
    test.setTimeout(90_000);
    await withObjectDataset(async id => {
        await openFiltered(page, id, { field: 'crop', operator: '=', value: 'corn' });
        await expectCounts(page, '1 image', '2 objects');
    });
});

test('a filter on objects shows their images in the image grid, and an object with no image adds none', async ({ page }) => {
    test.setTimeout(90_000);
    await withObjectDataset(async id => {
        // Three weed objects: two of the corn image, and one whose parent_id names no image.
        await openFiltered(page, id, { field: 'label', operator: '=', value: 'weed' });
        await expectCounts(page, '1 image', '3 objects');
    });
});
