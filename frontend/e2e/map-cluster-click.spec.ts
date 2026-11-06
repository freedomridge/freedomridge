import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// A click on a cluster bubble always breaks it apart (docs/MAP_REQUIREMENTS.md #14). It used to
// zoom in 2.5 levels at the bubble, which left a tight group in one cell: the same bubble again.
// Each test makes its own dataset of 150 located samples near (10, 10) (few, as other specs wait on the
// job runner that ingests and deletes them) and clicks their bubble on
// the map's opening world view (zoom 1, centred on 0, 0), so no view it reaches is cached already.
test.use({ storageState: AUTH_STATE });

type MapItem = { id?: number | null; count?: number; lon: number; lat: number };
type MapResponse = { zoom: number; items: MapItem[] };

const bubbles = (response: MapResponse) => response.items.filter(item => item.id == null);
const TOTAL = 150;  // at least _MAP_CLUSTER_MIN_POINTS (100), so they make one bubble
const whole = (response: MapResponse) =>
    response.items.length === 1 && bubbles(response).length === 1 && bubbles(response)[0].count === TOTAL;

async function clickTheWholeBubble(page: Page, name: string, step: [number, number]) {
    const cookie = await adminLogin();
    const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name, is_global: true }) });
    const samples = Array.from({ length: TOTAL }, (_, i) => ({
        filepath: `s3://${name}/${i}.jpg`,
        location: { type: 'Point', coordinates: [10 + (i % 10) * step[0], 10 + Math.floor(i / 10) * step[1]] },
    }));
    await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    const responses: MapResponse[] = [];
    page.on('response', async response => {
        if (!response.url().includes('/map_points')) return;
        const zoom = JSON.parse(response.request().postData() || '{}').zoom;
        const body = await response.json().catch(() => null);
        if (body?.items && typeof zoom === 'number') responses.push({ zoom, items: body.items });
    });
    try {
        await page.goto(`/datasets/${ds.id}`);
        await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('map');
        const map = page.getByTestId('panel-map');
        const noToken = map.getByText(/NEXT_PUBLIC_MAPBOX_TOKEN/);
        await expect(noToken.or(map.getByRole('button', { name: 'Zoom to Data' })).first()).toBeVisible({ timeout: 20_000 });
        test.skip(await noToken.isVisible(), 'No Mapbox token configured');
        await expect.poll(() => responses.some(whole), { timeout: 20_000 }).toBe(true);
        const before = responses.find(whole)!;
        expect(before.zoom).toBe(1);
        await page.waitForTimeout(1_000);

        // Where mapbox-gl draws the bubble on the world view: 512 px tiles, Web Mercator.
        const box = (await map.locator('canvas.mapboxgl-canvas').boundingBox())!;
        const world = 512 * 2 ** before.zoom;
        const { lon, lat } = bubbles(before)[0];
        const mercator = Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
        await page.mouse.click(
            box.x + box.width / 2 + (lon / 360) * world,
            box.y + box.height / 2 - (mercator / (2 * Math.PI)) * world,
        );
        await expect.poll(() => responses.at(-1)!.zoom, { timeout: 20_000 }).toBeGreaterThan(before.zoom);
        await page.waitForTimeout(1_000);
        return responses.at(-1)!;
    } finally {
        await deleteDatasetAndWait(cookie, ds.id);
    }
}

test('a click on a tight group\'s bubble zooms in until its samples show', async ({ page }) => {
    test.setTimeout(120_000);
    // 200 m across: 2.5 levels deeper than the world view, a cell is still about 800 km wide.
    const after = await clickTheWholeBubble(page, 'e2e-map-click-tight', [0.0002, 0.00015]);
    expect(after.items.length).toBeGreaterThan(1);
    expect(bubbles(after).every(bubble => (bubble.count ?? 0) < TOTAL)).toBe(true);
});

test('a click on a wide group\'s bubble splits it into smaller bubbles or points', async ({ page }) => {
    test.setTimeout(120_000);
    // About 50 km across: one cell on the world view, several once it fills the map.
    const after = await clickTheWholeBubble(page, 'e2e-map-click-wide', [0.05, 0.033]);
    expect(after.zoom).toBeLessThan(12);  // split by the grid, not by the zoom where every point shows
    expect(after.items.length).toBeGreaterThan(1);
    expect(bubbles(after).every(bubble => (bubble.count ?? 0) < TOTAL)).toBe(true);
});
