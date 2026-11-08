import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// After a map re-fit, a lasso polygon drawn on the old map is refused by the server (it would select
// other samples). An applied "Embeddings Region" stage and a saved view holding one must not leave the
// grid and the scatter failing: the stale lasso is dropped with a notice, the map renders, and the
// selection is drawn again.
//
// The panel turns a lasso into an `embedding_polygon` stage only on a sampled scatter (over 100,000
// points); on this small map it would select ids, which no re-fit changes. So the polygon stage is
// put in place as the app persists an applied stage (its saved filter state), then saved as a view.
test.use({ storageState: AUTH_STATE });

const FIELD = 'e2e_lasso';
// Coordinates x = i % 10, y = floor(i / 10): this polygon holds x 0..4, y 0..5 = 30 of the 60 points.
const POLYGON = [[-0.5, -0.5], [4.5, -0.5], [4.5, 5.5], [-0.5, 5.5]];
let cookie = '';
let dsId = 0;

test.beforeAll(async () => {
    cookie = await adminLogin();
    dsId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-stale-lasso-${Date.now()}` }) })).id;
    const samples = Array.from({ length: 60 }, (_, i) => ({
        filepath: `s3://e2e-stale-lasso/s${i}.jpg`,
        metadata: { n: i },
        embeddings: { [FIELD]: Array.from({ length: 8 }, (_, d) => Math.sin(i * 0.37 + d) + (i % 3) * 2) },
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${dsId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    const coordinates = (ids as number[]).map((sampleId, i) => ({ sample_id: sampleId, x: i % 10, y: Math.floor(i / 10) }));
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/coordinates?field_name=${FIELD}`, {
        method: 'POST', body: JSON.stringify(coordinates),
    });
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/recompute-viz-rank?field_name=${FIELD}&full=true`, { method: 'POST' });
});

test.afterAll(async () => {
    if (dsId) await deleteDatasetAndWait(cookie, dsId, 120_000);
});

async function applyPolygonStage(page: Page, modelId: string | null, polygon: number[][] = POLYGON) {
    // Written from another page: on the dataset page the app's own (debounced) save could overwrite it.
    await page.goto('/');
    await page.evaluate(({ key, stage }) => {
        localStorage.setItem(key, JSON.stringify({
            viewStages: [stage], filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
            brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Embeddings',
        }));
    }, {
        key: `fridge-filters-${dsId}`,
        stage: {
            type: 'filter', description: 'Embeddings Region',
            filter: { field: FIELD, operator: 'embedding_polygon', value: polygon, model_id: modelId },
        },
    });
    await page.goto(`/datasets/${dsId}`);
}

// Lasso the whole canvas: the re-fitted map's layout is UMAP's, so a lasso over part of the canvas may
// hold no point. On this unsampled scatter the lasso selects the visible points by id.
async function drawLasso(page: Page): Promise<number> {
    const canvas = page.getByTestId('embeddings-canvas');
    await canvas.scrollIntoViewIfNeeded();
    const box = await canvas.boundingBox();
    if (!box) throw new Error('embeddings canvas has no bounding box');
    const x = (fx: number) => box.x + box.width * fx;
    const y = (fy: number) => box.y + box.height * fy;
    await page.mouse.move(x(0.02), y(0.02));
    await page.keyboard.down('Shift');
    await page.mouse.down();
    await page.mouse.move(x(0.98), y(0.02), { steps: 8 });
    await page.mouse.move(x(0.98), y(0.98), { steps: 8 });
    await page.mouse.move(x(0.02), y(0.98), { steps: 8 });
    await page.mouse.move(x(0.02), y(0.02), { steps: 8 });
    await page.mouse.up();
    await page.keyboard.up('Shift');
    const chip = page.getByTestId('embeddings-selected-count');
    await expect(chip).toBeVisible({ timeout: 10_000 });
    const selected = parseInt((await chip.textContent() || '').replace(/[^0-9]/g, ''), 10);
    expect(selected).toBeGreaterThan(0);
    return selected;
}

// Before the re-fit test below, which makes every polygon on this map stale.
test('a lasso chip says which map it is on, opens no form, and a click excludes it', async ({ page }) => {
    await page.goto(`/datasets/${dsId}`);
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^60(\s|\/|$).*points/, { timeout: 30_000 });
    // x 0..1, y 0..5: 12 of the 60 points.
    await applyPolygonStage(page, null, [[-0.5, -0.5], [1.5, -0.5], [1.5, 5.5], [-0.5, 5.5]]);
    await expectTotal(page, 12);
    const chip = page.getByTestId('query-stage-chip');
    await expect(chip).toContainText(`Embeddings region · ${FIELD} map`);
    // A click excludes it (it used to open the field form, showing only the field name), and another includes it.
    await chip.click();
    await expect(page.getByTestId('filter-mode-builder')).toHaveCount(0);
    await expect(chip).toContainText(`Exclude Embeddings region · ${FIELD} map`);
    await expectTotal(page, 48);
    await chip.click();
    await expectTotal(page, 12);
});

test('a lasso drawn on the previous map is dropped, the map renders, and the selection is drawn again', async ({ page }) => {
    test.setTimeout(300_000);
    // The stale lasso may be dropped as soon as the app refetches after the re-fit (the jobs indicator
    // invalidates the scatter) or only after the reload below: record the refusals and the warning
    // across reloads, so the test holds in either order.
    await page.addInitScript(() => {
        const log = (key: string, value: string) => {
            const seen = JSON.parse(sessionStorage.getItem(key) || '[]');
            seen.push(value);
            sessionStorage.setItem(key, JSON.stringify(seen));
        };
        window.addEventListener('fridge:map-refitted', (event) => log('e2e-refitted', JSON.stringify((event as CustomEvent).detail)));
        new MutationObserver(() => {
            if (document.body?.innerText.includes('was re-fitted, so the lasso selection drawn on the old map was removed')) log('e2e-warned', 'yes');
        }).observe(document, { subtree: true, childList: true, characterData: true });
    });
    const recorded = (key: string) => page.evaluate((k) => JSON.parse(sessionStorage.getItem(k) || '[]').length, key);
    await page.goto(`/datasets/${dsId}`);
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^60(\s|\/|$).*points/, { timeout: 30_000 });

    // A lasso on the never-re-fitted map (no model id), applied as a view stage and saved in a view.
    await applyPolygonStage(page, null);
    await expectTotal(page, 30);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByPlaceholder('Enter view name...').fill('lasso-view');
    await page.getByRole('button', { name: 'Save View' }).click();
    await expect(page.getByText('lasso-view')).toBeVisible();
    await page.getByRole('button', { name: 'Close Views' }).click();

    // Re-fit the map (Run now; the backend's runner fits it for real).
    const schedule = await apiFetch(cookie, `/datasets/${dsId}/schedules`, {
        method: 'POST', body: JSON.stringify({ job_type: 'map_refit', target: FIELD, enabled: false }),
    });
    await apiFetch(cookie, `/datasets/${dsId}/schedules/${schedule.id}/run`, { method: 'POST' });
    await expect.poll(async () => {
        const items = (await apiFetch(cookie, `/datasets/${dsId}/schedules`)).items;
        return items[0]?.last_job?.state;
    }, { timeout: 180_000, intervals: [1_000] }).toBe('succeeded');

    // The requests carrying the old lasso are refused; the stage is dropped with a warning, and the grid
    // and the scatter load the new map.
    await page.reload();
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(0, { timeout: 30_000 });
    await expect.poll(() => recorded('e2e-refitted'), { timeout: 30_000 }).toBeGreaterThan(0);
    await expect.poll(() => recorded('e2e-warned'), { timeout: 30_000 }).toBeGreaterThan(0);
    await expectTotal(page, 60);
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^60(\s|\/|$).*points/, { timeout: 30_000 });
    await expect(page.getByTestId('map-refitted-at')).toContainText('Map re-fitted on');
    await expect(page.getByText('Failed to load embeddings')).toHaveCount(0);

    // The saved view's lasso is dropped the same way when the view is opened, and the panel says so.
    const refusals = await recorded('e2e-refitted');
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByRole('button', { name: /lasso-view/ }).click();
    await expect.poll(() => recorded('e2e-refitted'), { timeout: 30_000 }).toBeGreaterThan(refusals);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(0, { timeout: 30_000 });
    await expectTotal(page, 60);
    await page.getByRole('button', { name: 'Close Views' }).click();
    await expect(page.getByTestId('map-changed-notice')).toBeVisible();

    // Drawn again, the lasso selects and applies as usual. The warning toast (fixed, bottom right) can
    // still cover the scatter (it shows for 10 s from whichever request met the stale lasso first, so
    // whether it is still up depends on timing): close it, as a user would, before drawing over it.
    await page.getByTestId('map-changed-notice').getByRole('button', { name: 'OK' }).click();
    await expect(page.getByTestId('map-changed-notice')).toHaveCount(0);
    const warning = page.getByText('was re-fitted, so the lasso selection drawn on the old map was removed');
    if (await warning.isVisible()) await warning.locator('xpath=..').getByRole('button').click();
    await expect(warning).toHaveCount(0);
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^60(\s|\/|$).*points/);
    const again = await drawLasso(page);
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, again);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);

    // And a polygon drawn on the new map (its model id) is accepted.
    const state = await apiFetch(cookie, `/datasets/${dsId}/embeddings/map-state?field_name=${FIELD}`);
    const lasso = { field: FIELD, operator: 'embedding_polygon', value: POLYGON, model_id: state.model_id };
    const inside = (await apiFetch(cookie, `/datasets/${dsId}/samples/query`, {
        method: 'POST', body: JSON.stringify({ limit: 0, filters: { logic: 'AND', filters: [lasso] } }),
    })).total;
    await applyPolygonStage(page, state.model_id);
    await expectTotal(page, inside);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);
    await expect(page.getByText('Failed to load embeddings')).toHaveCount(0);
});
