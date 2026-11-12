import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// A lasso polygon is drawn on one layout of its map, and records it (layout_id). Once the map is laid out again (a
// reduce, a re-fit), the same polygon may select other samples: the lasso keeps filtering on the current map, its chip
// turns amber with the reason, and a toast says so once. Nothing is removed until the user removes it.
//
// The panel turns a lasso into an `embedding_polygon` stage only on a sampled scatter (over 100,000 points); on this
// small map it would select ids, which no new layout changes. So the polygon stage is put in place as the app persists
// an applied stage (its saved filter state), then saved as a view.
test.use({ storageState: AUTH_STATE });

const FIELD = 'e2e_lasso';
// Coordinates x = i % 10, y = floor(i / 10): this polygon holds x 0..4, y 0..5 = 30 of the 60 points.
const POLYGON = [[-0.5, -0.5], [4.5, -0.5], [4.5, 5.5], [-0.5, 5.5]];
const WARNING = 'This map was laid out again after this selection was drawn. It may select different samples now. Redraw it or remove it.';
// When the filters came from a saved view the viewer may update.
const WARNING_IN_VIEW = `${WARNING.slice(0, -1)}, then use Update this view to save it.`;
const TOAST = `A lasso on the ${FIELD} map`;
let cookie = '';
let dsId = 0;
let sampleIds: number[] = [];

test.beforeAll(async () => {
    cookie = await adminLogin();
    dsId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-stale-lasso-${Date.now()}` }) })).id;
    const samples = Array.from({ length: 60 }, (_, i) => ({
        filepath: `s3://e2e-stale-lasso/s${i}.jpg`,
        metadata: { n: i },
        embeddings: { [FIELD]: Array.from({ length: 8 }, (_, d) => Math.sin(i * 0.37 + d) + (i % 3) * 2) },
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${dsId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    sampleIds = ids as number[];
    // Placed as projections (with each vector's version), which never make a new layout: the map has no layout id, as
    // a map has after the upgrade until it is laid out again.
    const waiting = await apiFetch(cookie, `/datasets/${dsId}/embeddings/vectors?field_name=${FIELD}&pending_only=true&limit=100`);
    const version = new Map((waiting.items as { sample_id: number; vector_version: string }[]).map((item) => [item.sample_id, item.vector_version]));
    const coordinates = sampleIds.map((sampleId, i) => ({
        sample_id: sampleId, x: i % 10, y: Math.floor(i / 10), vector_version: version.get(sampleId),
    }));
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/coordinates?field_name=${FIELD}`, {
        method: 'POST', body: JSON.stringify(coordinates),
    });
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/recompute-viz-rank?field_name=${FIELD}&full=true`, { method: 'POST' });
    expect((await apiFetch(cookie, `/datasets/${dsId}`)).map_layouts).toEqual({});
});

test.afterAll(async () => {
    if (dsId) await deleteDatasetAndWait(cookie, dsId, 120_000);
});

// With keepSource, the filters stay those of the saved view they came from, as when the lasso is redrawn in the app.
async function applyPolygonStage(page: Page, layoutId?: string | null, polygon: number[][] = POLYGON, keepSource = false) {
    // Written from another page: on the dataset page the app's own (debounced) save could overwrite it.
    await page.goto('/');
    await page.evaluate(({ key, stage, keepSource }) => {
        const sourceView = keepSource ? JSON.parse(localStorage.getItem(key) ?? '{}').sourceView : undefined;
        localStorage.setItem(key, JSON.stringify({
            viewStages: [stage], filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
            brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Embeddings', sourceView,
        }));
    }, {
        keepSource,
        key: `fridge-filters-${dsId}`,
        stage: {
            type: 'filter', description: 'Embeddings Region',
            filter: { field: FIELD, operator: 'embedding_polygon', value: polygon, ...(layoutId === undefined ? {} : { layout_id: layoutId }) },
        },
    });
    await page.goto(`/datasets/${dsId}`);
}

// What the polygon selects on the map as it is now.
async function inside(polygon: number[][] = POLYGON): Promise<number> {
    const lasso = { field: FIELD, operator: 'embedding_polygon', value: polygon };
    return (await apiFetch(cookie, `/datasets/${dsId}/samples/query`, {
        method: 'POST', body: JSON.stringify({ limit: 0, filters: { logic: 'AND', filters: [lasso] } }),
    })).total;
}

async function currentLayout(): Promise<string> {
    return (await apiFetch(cookie, `/datasets/${dsId}`)).map_layouts[FIELD];
}

// Each toast that says so, counted as it appears (a toast goes after 10 s), from this page load on.
async function countToasts(page: Page) {
    await page.addInitScript((text) => {
        const counted = window as unknown as { lassoToasts: number };
        counted.lassoToasts = 0;
        new MutationObserver((records) => {
            for (const record of records) {
                for (const node of record.addedNodes) {
                    if (node instanceof HTMLElement && node.textContent?.includes(text)) counted.lassoToasts += 1;
                }
            }
        }).observe(document, { subtree: true, childList: true });
    }, TOAST);
    return () => page.evaluate(() => (window as unknown as { lassoToasts: number }).lassoToasts);
}

async function expectWarns(page: Page, title = WARNING) {
    const chip = page.getByTestId('query-stage-chip');
    await expect(chip).toHaveAttribute('data-stale-lasso', 'true', { timeout: 30_000 });
    await expect(chip.locator(`[title="${title}"]`)).toHaveCount(1);
    await expect(chip).toContainText(`Embeddings region · ${FIELD} map`);
}

// Before the tests below, which lay the map out again.
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
    await expect(chip).not.toHaveAttribute('data-stale-lasso', 'true');
});

// A lasso on a sampled scatter keeps its polygon as the selection and only its visible points as ids. The saved
// state held the ids without the polygon, so after a reload (or Back) Apply, Tag and Export acted on the visible
// points alone. This small map never samples, so the state is put in place as the app saves it.
test('a lasso comes back after a reload as its whole region, not its visible points', async ({ page }) => {
    await page.goto('/');
    await page.evaluate(({ key, state }) => localStorage.setItem(key, JSON.stringify(state)), {
        key: `fridge-filters-${dsId}`,
        state: {
            viewStages: [], filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
            // Two of the region's points were on screen; x 0..1, y 0..5 holds 12.
            brushedIds: sampleIds.slice(0, 2), selectedIds: [], currentPolygon: null, selectionSource: 'Embeddings',
            currentEmbeddingRegion: {
                field: FIELD, polygon: [[-0.5, -0.5], [1.5, -0.5], [1.5, 5.5], [-0.5, 5.5]], token: '', total: 12, layoutId: null,
            },
        },
    });
    await page.goto(`/datasets/${dsId}`);
    await page.getByTestId('apply-filters').click();
    await expect(page.getByTestId('query-stage-chip')).toContainText(`Embeddings region · ${FIELD} map`);
    await expectTotal(page, 12);
});

test('a lasso saved before the upgrade stays as it is until its map is laid out again, then warns and keeps filtering', async ({ page }) => {
    test.setTimeout(180_000);
    const toasts = await countToasts(page);
    // No layout id: drawn before the upgrade, on the map as it was then.
    await applyPolygonStage(page);
    await expectTotal(page, 30);
    await expect(page.getByTestId('query-stage-chip')).not.toHaveAttribute('data-stale-lasso', 'true');
    expect(await toasts()).toBe(0);

    // The map is laid out again (Run UMAP reduction, or the API).
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/reduce`, { method: 'POST', body: JSON.stringify({ field_name: FIELD }) });
    expect(await currentLayout()).toBeTruthy();
    const now = await inside();
    await page.reload();
    await expectWarns(page);
    await expect.poll(toasts, { timeout: 15_000 }).toBe(1);
    await expectTotal(page, now);  // still filtering, on the map as it is now
    // Excluded, it is the same lasso: still amber, and no second toast.
    await page.getByTestId('query-stage-chip').click();
    await expectTotal(page, 60 - now);
    await expectWarns(page);
    expect(await toasts()).toBe(1);

    // One drawn on the current layout doesn't warn.
    await applyPolygonStage(page, await currentLayout());
    await expectTotal(page, now);
    await expect(page.getByTestId('query-stage-chip')).not.toHaveAttribute('data-stale-lasso', 'true');
});

test('after a re-fit, a lasso drawn on the map before it warns and keeps filtering, in a view too, until the view is updated', async ({ page }) => {
    test.setTimeout(300_000);
    const toasts = await countToasts(page);
    await page.goto(`/datasets/${dsId}`);
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^60(\s|\/|$).*points/, { timeout: 30_000 });

    // A lasso on the map's current layout, applied as a view stage and saved in a view.
    const before = await currentLayout();
    await applyPolygonStage(page, before);
    await expectTotal(page, await inside());
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);
    await expect(page.getByTestId('query-stage-chip')).not.toHaveAttribute('data-stale-lasso', 'true');
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
    expect(await currentLayout()).not.toBe(before);
    const now = await inside();

    // The lasso is kept and warns once; the grid and the scatter load the new map, filtered by it. Its filters are the
    // view just saved, which its owner may update.
    await page.reload();
    await expectWarns(page, WARNING_IN_VIEW);
    await expect.poll(toasts, { timeout: 15_000 }).toBe(1);
    await expectTotal(page, now);
    await expect(page.getByTestId('map-refitted-at')).toContainText('Map re-fitted on');
    await expect(page.getByText('Failed to load embeddings')).toHaveCount(0);

    // The saved view's lasso is kept and warns the same way when the view is opened.
    await page.getByTestId('query-stage-chip').hover();
    await page.getByTestId('query-stage-chip').getByRole('button').click();  // removed by the user, as only the user does
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(0);
    await expectTotal(page, 60);
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByRole('button', { name: /lasso-view/ }).click();
    await page.getByRole('button', { name: 'Close Views' }).click();
    await expectWarns(page, WARNING_IN_VIEW);
    await expectTotal(page, now);

    // And a lasso drawn on the new layout doesn't warn. Its filters still come from the view, which they now differ
    // from: Update this view saves them in it, and the view opens without the warning.
    const layout = await currentLayout();
    await applyPolygonStage(page, layout, POLYGON, true);
    await expectTotal(page, now);
    await expect(page.getByTestId('query-stage-chip')).not.toHaveAttribute('data-stale-lasso', 'true');
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await expect(page.getByTestId('view-source')).toContainText('“lasso-view”');
    page.once('dialog', dialog => void dialog.accept());
    await page.getByTestId('view-update').click();
    await expect(page.getByTestId('view-source')).toHaveCount(0);
    const views = await apiFetch(cookie, `/datasets/${dsId}/views`);
    expect(views.find((v: { name: string }) => v.name === 'lasso-view').content.viewStages[0].filter.layout_id).toBe(layout);
    await page.getByRole('button', { name: 'Close Views' }).click();
    await page.getByTestId('query-stage-chip').hover();
    await page.getByTestId('query-stage-chip').getByRole('button').click();
    await expectTotal(page, 60);
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByRole('button', { name: /lasso-view/ }).click();
    await page.getByRole('button', { name: 'Close Views' }).click();
    await expectTotal(page, now);
    await expect(page.getByTestId('query-stage-chip')).not.toHaveAttribute('data-stale-lasso', 'true');
    expect(await toasts()).toBe(0);  // since the page loaded with the redrawn lasso
});

test('tagging a selection picked under a lasso drawn on an earlier layout says so, with the count', async ({ page }) => {
    // Around every point, so the count doesn't hang on where the map's layout put them.
    const everything = [[-1e6, -1e6], [1e6, -1e6], [1e6, 1e6], [-1e6, 1e6]];
    await applyPolygonStage(page, 'an-earlier-layout', everything);
    await expectWarns(page);
    await expectTotal(page, 60);
    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toBeVisible({ timeout: 10_000 });
    await page.getByTestId('grid-tag-button').click();
    const warning = page.getByTestId('tag-stale-lasso-warning');
    await expect(warning).toContainText(`earlier layout of the ${FIELD} map`);
    await expect(warning).toContainText('Applying changes 60 samples');
});
