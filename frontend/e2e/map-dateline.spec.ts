import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// A view across the antimeridian shows the samples on both sides of it (docs/MAP_REQUIREMENTS.md #0). mapbox-gl
// reports such a view as west 179.999, east 180.001, and the east side used to be cut off at 180. Two samples 22 m
// apart, one on each side: selecting one in the grid flies the map in to zoom 18 on it (#12), and that view holds both.
test.use({ storageState: AUTH_STATE });

type MapRequest = { zoom: number; bbox: [number, number, number, number]; items: Array<{ id?: number | null; lon: number }> };

test('a view across the antimeridian shows the samples on both sides of it', async ({ page }) => {
    test.setTimeout(120_000);
    const name = 'e2e-map-dateline';
    const cookie = await adminLogin();
    const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name, is_global: true }) });
    const samples = [179.9999, -179.9999].map((lon, i) => ({
        filepath: `s3://${name}/${i}.jpg`,
        location: { type: 'Point', coordinates: [lon, 0] },
    }));
    await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    const requests: MapRequest[] = [];
    page.on('response', async response => {
        if (!response.url().includes('/map_points')) return;
        const body = JSON.parse(response.request().postData() || '{}');
        const answer = await response.json().catch(() => null);
        if (answer?.items && body.bbox) requests.push({ zoom: body.zoom, bbox: body.bbox, items: answer.items });
    });
    try {
        await page.goto(`/datasets/${ds.id}`);
        await page.getByTestId('panel-embeddings').getByTestId('panel-type-select').selectOption('map');
        const map = page.getByTestId('panel-map');
        const noToken = map.getByText(/NEXT_PUBLIC_MAPBOX_TOKEN/);
        await expect(noToken.or(map.getByRole('button', { name: 'Zoom to Data' })).first()).toBeVisible({ timeout: 20_000 });
        test.skip(await noToken.isVisible(), 'No Mapbox token configured');
        await expect.poll(() => requests.length, { timeout: 20_000 }).toBeGreaterThan(0);

        const cell = page.getByTestId('grid-cell').first();
        await cell.hover();
        await cell.getByTestId('cell-checkbox').click({ force: true });
        await expect(page.getByTestId('grid-selection-count')).toContainText('1 selected');
        await expect.poll(() => requests.some(request => request.zoom >= 12), { timeout: 20_000 }).toBe(true);
        const closeUp = requests.filter(request => request.zoom >= 12).at(-1)!;
        expect(closeUp.bbox[0]).toBeLessThan(180);
        expect(closeUp.bbox[2]).toBeGreaterThan(180);  // one world copy, across the antimeridian
        expect(closeUp.items.filter(item => item.id != null)).toHaveLength(2);
    } finally {
        await deleteDatasetAndWait(cookie, ds.id);
    }
});
