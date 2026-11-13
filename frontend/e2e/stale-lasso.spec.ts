import { test, expect } from '@playwright/test';
import { AUTH_STATE, expectTotal } from './helpers';
import { FIELD, lassoDataset } from './stale-lasso-helpers';

// Lassos on a map that isn't laid out again here (stale-lasso-helpers.ts says what a stale lasso is). The tests that lay
// it out again are in stale-lasso.fits.spec.ts, which runs after every other spec (playwright.config.ts).
test.use({ storageState: AUTH_STATE });

const { ds, applyPolygonStage } = lassoDataset('e2e-stale-lasso');

test('a lasso chip says which map it is on, opens no form, and a click excludes it', async ({ page }) => {
    await page.goto(`/datasets/${ds.id}`);
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
        key: `fridge-filters-${ds.id}`,
        state: {
            viewStages: [], filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
            // Two of the region's points were on screen; x 0..1, y 0..5 holds 12.
            brushedIds: ds.sampleIds.slice(0, 2), selectedIds: [], currentPolygon: null, selectionSource: 'Embeddings',
            currentEmbeddingRegion: {
                field: FIELD, polygon: [[-0.5, -0.5], [1.5, -0.5], [1.5, 5.5], [-0.5, 5.5]], token: '', total: 12, layoutId: null,
            },
        },
    });
    await page.goto(`/datasets/${ds.id}`);
    await page.getByTestId('apply-filters').click();
    await expect(page.getByTestId('query-stage-chip')).toContainText(`Embeddings region · ${FIELD} map`);
    await expectTotal(page, 12);
});
