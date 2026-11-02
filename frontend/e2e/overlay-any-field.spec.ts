import { test, expect, type Locator } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE } from './helpers';

// Requires an isolated stack via E2E_API_URL / Playwright baseURL.
// This spec deliberately ingests arbitrary field names through the real API.
test.use({ storageState: AUTH_STATE });

test('box fields and every open polyline segment draw and toggle independently in grid and modal', async ({ page }) => {
    const cookie = await adminLogin();
    const dataset = await apiFetch(cookie, '/datasets', {
        method: 'POST', body: JSON.stringify({ name: `overlay-any-field-${Date.now()}` }),
    });
    try {
        // Warm empty discovery to exercise append-on-write, with statistics as a fallback.
        await apiFetch(cookie, `/datasets/${dataset.id}/field-stats`);
        await apiFetch(cookie, `/datasets/${dataset.id}/samples`, {
            method: 'POST',
            body: JSON.stringify({ samples: [{ filepath: '/overlay-fixture.png', metadata: {
                vehicles: [{ label: 'car', bbox: [0.1, 0.1, 0.2, 0.2] }],
                pedestrians: [{ label: 'person', bbox: [0.6, 0.1, 0.15, 0.3] }],
                lanes: [{ label: 'area/open', closed: false, paths: [
                    [[0.1, 0.6], [0.3, 0.8]], [[0.6, 0.6], [0.9, 0.9]],
                ] }],
            } }] }),
        });
        await expect.poll(async () => {
            const schema = await apiFetch(cookie, `/datasets/${dataset.id}/field-stats`);
            return schema.filter((field: { name: string; type: string }) =>
                ['vehicles', 'pedestrians', 'lanes'].includes(field.name) &&
                ['detections', 'polygons'].includes(field.type)).length;
        }, { timeout: 60_000 }).toBe(3);
        const png = await page.evaluate(() => {
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = 200;
            const ctx = canvas.getContext('2d')!;
            ctx.fillStyle = '#333333';
            ctx.fillRect(0, 0, 200, 200);
            return canvas.toDataURL('image/png').split(',')[1];
        });
        await page.route('**/media**', route => route.fulfill({ contentType: 'image/png', body: Buffer.from(png, 'base64') }));
        await page.goto(`/datasets/${dataset.id}`);
        const cell = page.getByTestId('grid-cell').first();
        await expect(cell).toBeVisible();
        const fields = ['vehicles', 'pedestrians', 'lanes'];
        for (const field of fields) {
            const toggle = page.getByTestId(`overlay-toggle-${field}`);
            await expect(toggle).toBeVisible();
            await toggle.click();
            await expect(toggle).toHaveAttribute('aria-pressed', 'true');
        }
        async function check(scope: Locator, hidden?: string) {
            await expect(scope.getByText('car', { exact: true }).filter({ visible: true })).toHaveCount(hidden === 'vehicles' ? 0 : 1);
            await expect(scope.getByText('person', { exact: true }).filter({ visible: true })).toHaveCount(hidden === 'pedestrians' ? 0 : 1);
            await expect(scope.locator('rect[fill="none"][stroke]')).toHaveCount(hidden === 'vehicles' || hidden === 'pedestrians' ? 1 : 2);
            const segments = scope.locator('polyline[data-label="area/open"]');
            await expect(segments).toHaveCount(hidden === 'lanes' ? 0 : 2);
            if (hidden !== 'lanes') {
                await expect(segments.nth(0)).toHaveAttribute('points', '20,120 60,160');
                await expect(segments.nth(1)).toHaveAttribute('points', '120,120 180,180');
                for (const segment of await segments.all()) await expect(segment).toHaveAttribute('fill', 'none');
                await expect(scope.locator('path[data-label="area/open"]')).toHaveCount(0);
            }
        }
        await check(cell);
        for (const field of fields) {
            await page.getByTestId(`overlay-toggle-${field}`).click();
            await check(cell, field);
            await page.getByTestId(`overlay-toggle-${field}`).click();
        }
        await cell.click();
        const modal = page.getByRole('dialog');
        // Scope shape checks to the image, excluding the metadata label list.
        const image = modal.locator('img[alt="Sample"]').locator('..');
        await check(image);
        for (const field of fields) {
            await modal.getByTestId(`modal-overlay-toggle-${field}`).click();
            await check(image, field);
            await modal.getByTestId(`modal-overlay-toggle-${field}`).click();
        }
        await modal.getByRole('button', { name: /Hide Labels/ }).click();
        await expect(image.locator('rect[fill="none"][stroke], polyline')).toHaveCount(0);
        await modal.getByRole('button', { name: /Show Labels/ }).click();
        await check(image);
    } finally {
        await deleteDatasetAndWait(cookie, dataset.id);
    }
});
