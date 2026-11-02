import { test, expect, type Locator } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE } from './helpers';
import fixture from './fixtures/cuboid-projection.json';

// Uses the shared E2E_API_URL helper and the configured Playwright baseURL.
test.use({ storageState: AUTH_STATE });

async function expectEndpoints(overlay: Locator, width: number) {
    const scale = width / fixture.calibration.image_size[0];
    for (const entry of fixture.cases) {
        const lines = overlay.locator(`[data-cuboid-id="${entry.cuboid.id}"] [data-cuboid-edge]`);
        await expect(lines).toHaveCount(entry.edges.length);
        const endpoints = await lines.evaluateAll(elements => elements.map(el =>
            ['x1', 'y1', 'x2', 'y2'].map(attr => Number(el.getAttribute(attr)))));
        endpoints.forEach((edge, i) => edge.forEach((value, j) => {
            expect(Math.abs(value - entry.edges[i].flat()[j] * scale)).toBeLessThan(0.5);
        }));
    }
}

test('calibrated cuboids draw in thumbnails and the modal; toggles, object selection and filtering work', async ({ page }) => {
    const cookie = await adminLogin();
    const dataset = await apiFetch(cookie, '/datasets', {
        method: 'POST', body: JSON.stringify({ name: `e2e-cuboids-${Date.now()}` }),
    });
    try {
        const entries = fixture.cases; // camera, near-plane and LiDAR boxes
        const { ids: [parentId] } = await apiFetch(cookie, `/datasets/${dataset.id}/samples`, {
            method: 'POST', body: JSON.stringify({ samples: [{ filepath: 'synthetic/cuboids.png', metadata: {
                calibration: { type: 'calibration', ...fixture.calibration },
                ground_truth_3d: entries.map(c => c.cuboid),
            } }] }),
        });
        const { ids: objectIds } = await apiFetch(cookie, `/datasets/${dataset.id}/samples`, {
            method: 'POST', body: JSON.stringify({ samples: entries.map(c => ({
                filepath: `synthetic/crops/${c.cuboid.id}.jpg`, media_type: 'object', metadata: {
                    parent_id: parentId, parent_filepath: 'synthetic/cuboids.png', cuboid_id: c.cuboid.id, cuboid_field: 'ground_truth_3d',
                    label: c.cuboid.label, distance_m: c.distance,
                    length_m: c.cuboid.size[0], width_m: c.cuboid.size[1], height_m: c.cuboid.size[2], yaw: c.cuboid.yaw,
                    crop_bbox: c.extent!.map((v, i) => v / fixture.calibration.image_size[i % 2]),
                },
            })) }),
        });
        // Another parent prevents a vacuous cross-grain result.
        await apiFetch(cookie, `/datasets/${dataset.id}/samples`, {
            method: 'POST', body: JSON.stringify({ samples: [{ filepath: 'synthetic/empty.png' }] }),
        });
        const matched = await apiFetch(cookie, `/datasets/${dataset.id}/samples/query`, {
            method: 'POST', body: JSON.stringify({ filters: {
                field: '_cg', operator: 'has_matching_object',
                value: { field: 'distance_m', operator: '<=', value: 10 },
            } }),
        });
        expect(matched.items.map((s: { id: number }) => s.id)).toEqual([parentId]);

        const pngs = await page.evaluate(() => [1242, 512].map(width => {
            const canvas = document.createElement('canvas');
            canvas.width = width;
            canvas.height = Math.round(375 * width / 1242);
            const context = canvas.getContext('2d')!;
            context.fillStyle = '#344155';
            context.fillRect(0, 0, canvas.width, canvas.height);
            return canvas.toDataURL('image/png').split(',')[1];
        }));
        await page.route('**/media**', route => route.fulfill({ contentType: 'image/png',
            body: Buffer.from(pngs[new URL(route.request().url()).searchParams.has('w') ? 1 : 0], 'base64') }));
        await page.goto(`/datasets/${dataset.id}`);
        const sidebar = page.getByTestId('panel-filters');
        await expect(sidebar.getByText('Overlays', { exact: true })).toBeVisible();
        const row = sidebar.locator('label[title="ground_truth_3d"]').locator('..');
        await expect(row).toBeVisible();
        await row.getByTitle('Show on image', { exact: true }).click();
        const cell = page.getByTestId('grid-cell').filter({ has: page.locator('[data-cuboid-id="front"]') });
        await expect(cell).toHaveCount(1);
        await expect(cell.getByTestId('cuboid-overlay')).toHaveAttribute('viewBox', '0 0 512 155');
        await expectEndpoints(cell.getByTestId('cuboid-overlay'), 512);
        await cell.click();
        const modal = page.getByRole('dialog');
        await expect(modal).toBeVisible();
        await expectEndpoints(modal.getByTestId('cuboid-overlay'), 1242);
        await expect(modal.locator('[data-cuboid-id]')).toHaveCount(3);
        await modal.getByRole('button', { name: 'Toggle ground_truth_3d overlay' }).click();
        await expect(modal.locator('[data-cuboid-edge]')).toHaveCount(0);
        await modal.getByRole('button', { name: 'Toggle ground_truth_3d overlay' }).click();
        await expect(modal.locator('[data-cuboid-id]')).toHaveCount(3);
        await modal.getByRole('button', { name: 'Hide Near 2', exact: true }).click();
        await expect(modal.locator('[data-cuboid-id="near"]')).toHaveCount(0);
        await expect(modal.locator('[data-cuboid-id="front"]')).toHaveCount(1);
        await page.keyboard.press('Escape');

        await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects', exact: true }).click();
        await expect(page.getByTestId('grid-view-count')).toHaveText(/3 objects/);
        await page.getByTestId('grid-cell').filter({ has: page.locator(`img[src*="/samples/${objectIds[1]}/media"]`) }).click();
        await expect(modal.locator('[data-cuboid-id="near"][data-selected="true"]')).toHaveCount(1);
        await expect(modal.locator('[data-cuboid-id="front"][data-selected="true"]')).toHaveCount(0);
    } finally {
        await page.unrouteAll({ behavior: 'wait' });
        await deleteDatasetAndWait(cookie, dataset.id);
    }
});
