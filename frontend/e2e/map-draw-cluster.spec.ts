import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE } from './helpers';

// Regression: while drawing a map polygon, a vertex placed on a cluster bubble also
// flew the map in 2.5 zoom levels, so the rest of the polygon landed somewhere else. Needs real
// cluster bubbles (>= 100 located samples per cell): the eurosat dataset; skips where it isn't
// present (CI). Read-only — the polygon is never completed. (docs/MAP_REQUIREMENTS.md: cluster
// clicks OUTSIDE draw mode still zoom in; nothing here changes that.)
test.use({ storageState: AUTH_STATE });

async function slowClick(page: Page, x: number, y: number) {
    await page.mouse.move(x, y);
    await page.waitForTimeout(250);
    await page.mouse.down();
    await page.waitForTimeout(80);
    await page.mouse.up();
    await page.waitForTimeout(400);
}

test('a polygon vertex on a cluster bubble does not zoom the map', async ({ page }) => {
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'eurosat');
    test.skip(!ds, 'eurosat dataset not present');

    await page.goto(`/datasets/${ds.id}`);
    await page.getByTestId('panel-type-select').last().selectOption('map');
    const map = page.getByTestId('panel-map');
    await map.getByText('Loading map data...').waitFor({ state: 'hidden', timeout: 30_000 }).catch(() => {});
    await map.getByRole('button', { name: 'Zoom to Data' }).click();
    await page.waitForTimeout(4_000);

    const scale = map.locator('.mapboxgl-ctrl-scale');
    const box = (await map.locator('canvas.mapboxgl-canvas').first().boundingBox())!;
    // Open water at the left (the top-left corner sits under the map's buttons) starts a polygon.
    // The map's first click after load does not reach the draw handler, so click until it does.
    const mapEl = map.locator('.mapboxgl-map');
    for (let i = 0; i < 3 && !/mode-draw_polygon/.test((await mapEl.getAttribute('class')) || ''); i++) {
        await slowClick(page, box.x + 30, box.y + 330);
    }
    await expect(mapEl).toHaveClass(/mode-draw_polygon/);
    const before = await scale.textContent();
    // ...and the next vertex lands on the bubble in the middle of the data.
    await slowClick(page, box.x + box.width / 2, box.y + box.height / 2);
    await page.waitForTimeout(1_500);
    await expect(scale).toHaveText(before || '');
    await expect(mapEl).toHaveClass(/mode-draw_polygon/);
});
