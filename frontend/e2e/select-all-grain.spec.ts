import { test, expect } from '@playwright/test';
import { AUTH_STATE, bigSelectAll } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// A selection is of the grain of the grid it was made in. A Select All over 50,000 objects is a server token of
// the Objects grid's rows; an Images grid beside it showed every tile ticked and "60,000 selected", unticking an
// image there added an image id to the objects' exceptions, and its Tag tagged the objects. Now only grids of the
// selection's grain show it (and tag or export it), and a pick in a grid of the other grain starts a new selection.
// This spec builds its own small dataset of images and objects, and opens it with an Images grid beside an Objects
// grid; the big Select All's count and token are faked, as in bigSelectAll.
test.use({ storageState: AUTH_STATE });

const DS_NAME = 'e2e-select-all-grain';
const IMAGES = 4;
const OBJECTS = 3;

let datasetId: number;
let cookie: string;

test.beforeAll(async () => {
    cookie = await adminLogin();
    for (const d of await apiFetch(cookie, '/datasets')) {
        if (d.name === DS_NAME) await deleteDatasetAndWait(cookie, d.id);  // a crashed run's
    }
    datasetId = (await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name: DS_NAME, description: 'selection grain fixture' }),
    })).id;
    const { ids } = await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
        method: 'POST',
        body: JSON.stringify({
            samples: Array.from({ length: IMAGES }, (_, i) => ({
                filepath: `s3://e2e-select-grain/img_${i}.jpg`,
                media_type: 'image',
                metadata: { idx: i },
            })),
        }),
    });
    await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
        method: 'POST',
        body: JSON.stringify({
            samples: Array.from({ length: OBJECTS }, (_, i) => ({
                filepath: `s3://e2e-select-grain/img_${i}.jpg#o0`,
                media_type: 'object',
                metadata: { parent_id: ids[i], crop_bbox: [0, 0, 0.5, 0.5], label: 'thing' },
            })),
        }),
    });
    await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
});

test.afterAll(async () => {
    if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
});

test("a Select All made in an Objects grid is not the Images grid's to show, tag or export", async ({ page }) => {
    await page.addInitScript(() => {
        localStorage.setItem('fridge-panel-layout-v2', JSON.stringify({
            id: 'root',
            type: 'group',
            direction: 'horizontal',
            children: [
                { id: 'images', type: 'leaf', data: { panelType: 'grid' }, size: 50 },
                { id: 'objects', type: 'leaf', data: { panelType: 'grid' }, size: 50 },
            ],
        }));
    });
    await page.goto(`/datasets/${datasetId}`);
    const grids = page.getByTestId('panel-grid');
    await expect(grids).toHaveCount(2, { timeout: 20_000 });
    const images = grids.nth(0);
    const objects = grids.nth(1);
    await expect(images.getByTestId('grid-view-count')).toHaveText(`${IMAGES} images`, { timeout: 20_000 });
    await objects.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();
    await expect(objects.getByTestId('grid-view-count')).toHaveText(`${OBJECTS} objects`, { timeout: 20_000 });

    await bigSelectAll(page, objects);
    await expect(objects.getByTestId('grid-cell')).toHaveCount(OBJECTS);
    await expect(objects.locator('[data-testid="grid-cell"][data-selected="false"]')).toHaveCount(0);

    // The Images grid shows none of it: no tile ticked, no count, no Tag; it says where the selection is.
    await expect(images.getByTestId('grid-cell')).toHaveCount(IMAGES);
    await expect(images.locator('[data-testid="grid-cell"][data-selected="true"]')).toHaveCount(0);
    await expect(images.getByTestId('grid-selection-count')).toHaveCount(0);
    await expect(images.getByTestId('grid-tag-button')).toHaveCount(0);
    await expect(images.getByTestId('grid-other-selection')).toHaveText('60,000 objects selected in another panel');

    // Its Export doesn't offer the objects as the selection; the Objects grid's does.
    const dialog = page.getByRole('dialog');
    await images.getByRole('button', { name: 'Export' }).click();
    await expect(dialog.getByRole('radio', { name: /Selected samples/ })).toBeDisabled();
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await objects.getByRole('button', { name: 'Export' }).click();
    await expect(dialog.getByRole('radio', { name: /Selected samples/ })).toBeChecked();
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);

    // Ticking an image starts a selection of images: the objects' Select All goes.
    const firstImage = images.getByTestId('grid-cell').first();
    await firstImage.hover();
    await firstImage.getByTestId('cell-checkbox').click();
    await expect(images.getByTestId('grid-selection-count')).toHaveText(/^\s*1 selected\s*×?\s*$/);
    await expect(objects.locator('[data-testid="grid-cell"][data-selected="true"]')).toHaveCount(0);
    await expect(objects.getByTestId('grid-tag-button')).toHaveCount(0);
    await expect(objects.getByTestId('grid-other-selection')).toHaveText('1 image selected in another panel');
    await expect(page.getByTestId('draft-filters')).toContainText('Grid (1 selected)');
    await expect(page.getByTestId('draft-filters')).not.toContainText('Select All');

    // The Images grid's Tag tags that one image.
    await images.getByTestId('grid-tag-button').click();
    await expect(page.getByRole('heading', { name: 'Tag — 1 Sample', exact: true })).toBeVisible({ timeout: 10_000 });
});
