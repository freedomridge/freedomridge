import { test, expect, type Page } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// Shared by stale-lasso.spec.ts and stale-lasso.fits.spec.ts (the tests that lay the map out again, which run in the
// `fits` project, after every other spec: playwright.config.ts).
//
// A lasso polygon is drawn on one layout of its map, and records it (layout_id). Once the map is laid out again (a
// reduce, a re-fit), the same polygon may select other samples: the lasso keeps filtering on the current map, its chip
// turns amber with the reason, and a toast says so once. Nothing is removed until the user removes it.
//
// The panel turns a lasso into an `embedding_polygon` stage only on a sampled scatter (over 100,000 points); on this
// small map it would select ids, which no new layout changes. So the polygon stage is put in place as the app persists
// an applied stage (its saved filter state), then saved as a view.

export const FIELD = 'e2e_lasso';
// Coordinates x = i % 10, y = floor(i / 10): this polygon holds x 0..4, y 0..5 = 30 of the 60 points.
export const POLYGON = [[-0.5, -0.5], [4.5, -0.5], [4.5, 5.5], [-0.5, 5.5]];
export const WARNING = 'This map was laid out again after this selection was drawn. It may select different samples now. Redraw it or remove it.';
// When the filters came from a saved view the viewer may update.
export const WARNING_IN_VIEW = `${WARNING.slice(0, -1)}, then use Update this view to save it.`;
const TOAST = `A lasso on the ${FIELD} map`;

/** A dataset of 60 samples on one map without a layout id, made before the calling spec file's tests and deleted after
 * them, with the helpers that act on it. Each file has its own. */
export function lassoDataset(name: string) {
    const ds = { cookie: '', id: 0, sampleIds: [] as number[] };

    test.beforeAll(async () => {
        ds.cookie = await adminLogin();
        ds.id = (await apiFetch(ds.cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `${name}-${Date.now()}` }) })).id;
        const samples = Array.from({ length: 60 }, (_, i) => ({
            filepath: `s3://e2e-stale-lasso/s${i}.jpg`,
            metadata: { n: i },
            embeddings: { [FIELD]: Array.from({ length: 8 }, (_, d) => Math.sin(i * 0.37 + d) + (i % 3) * 2) },
        }));
        const { ids } = await apiFetch(ds.cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
        ds.sampleIds = ids as number[];
        // Placed as projections (with each vector's version), which never make a new layout: the map has no layout id,
        // as a map has after the upgrade until it is laid out again.
        const waiting = await apiFetch(ds.cookie, `/datasets/${ds.id}/embeddings/vectors?field_name=${FIELD}&pending_only=true&limit=100`);
        const version = new Map((waiting.items as { sample_id: number; vector_version: string }[]).map((item) => [item.sample_id, item.vector_version]));
        const coordinates = ds.sampleIds.map((sampleId, i) => ({
            sample_id: sampleId, x: i % 10, y: Math.floor(i / 10), vector_version: version.get(sampleId),
        }));
        await apiFetch(ds.cookie, `/datasets/${ds.id}/embeddings/coordinates?field_name=${FIELD}`, {
            method: 'POST', body: JSON.stringify(coordinates),
        });
        await apiFetch(ds.cookie, `/datasets/${ds.id}/embeddings/recompute-viz-rank?field_name=${FIELD}&full=true`, { method: 'POST' });
        expect((await apiFetch(ds.cookie, `/datasets/${ds.id}`)).map_layouts).toEqual({});
    });

    test.afterAll(async () => {
        if (ds.id) await deleteDatasetAndWait(ds.cookie, ds.id, 120_000);
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
            key: `fridge-filters-${ds.id}`,
            stage: {
                type: 'filter', description: 'Embeddings Region',
                filter: { field: FIELD, operator: 'embedding_polygon', value: polygon, ...(layoutId === undefined ? {} : { layout_id: layoutId }) },
            },
        });
        await page.goto(`/datasets/${ds.id}`);
    }

    // What the polygon selects on the map as it is now.
    async function inside(polygon: number[][] = POLYGON): Promise<number> {
        const lasso = { field: FIELD, operator: 'embedding_polygon', value: polygon };
        return (await apiFetch(ds.cookie, `/datasets/${ds.id}/samples/query`, {
            method: 'POST', body: JSON.stringify({ limit: 0, filters: { logic: 'AND', filters: [lasso] } }),
        })).total;
    }

    async function currentLayout(): Promise<string> {
        return (await apiFetch(ds.cookie, `/datasets/${ds.id}`)).map_layouts[FIELD];
    }

    return { ds, applyPolygonStage, inside, currentLayout };
}

// Each toast that says so, counted as it appears (a toast goes after 10 s), from this page load on.
export async function countToasts(page: Page) {
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

export async function expectWarns(page: Page, title = WARNING) {
    const chip = page.getByTestId('query-stage-chip');
    await expect(chip).toHaveAttribute('data-stale-lasso', 'true', { timeout: 30_000 });
    await expect(chip.locator(`[title="${title}"]`)).toHaveCount(1);
    await expect(chip).toContainText(`Embeddings region · ${FIELD} map`);
}
