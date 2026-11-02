import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { readState } from './api';

// A global-filter stage's rule must render its REAL operator even while the schema
// (POST /field-stats) is still loading. The bug: with no schema, opsForType falls back
// to the string operator list, the rule's '>=' matches no <option>, and the browser
// silently displays the first option — so a range applied from the Simple Filters
// sidebar showed as `=` in the stage editor (and looked wrongly converted). On large
// filtered datasets the schema recompute takes long enough that this was the norm.
test.use({ storageState: AUTH_STATE });

test('stage editor shows the rule operators while field-stats is still loading', async ({ page }) => {
    const { datasetId } = readState();

    // The exact stage shape applyFilters() produces for a sidebar range filter.
    const persisted = {
        viewStages: [{
            type: 'filter',
            description: 'Filter (score)',
            filter: {
                logic: 'AND', filters: [
                    { field: 'score', operator: '>=', value: 1 },
                    { field: 'score', operator: '<=', value: 4 },
                ],
            },
        }],
        filterMode: 'simple', simpleFilters: {},
        advancedFilters: { logic: 'AND', filters: [] },
        brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
    };
    await page.addInitScript(([k, v]) => localStorage.setItem(k as string, v as string),
        [`fridge-filters-${datasetId}`, JSON.stringify(persisted)]);

    // Hold every field-stats response so the schema never resolves during the test —
    // the editor must not need it to render the rule truthfully.
    await page.route('**/field-stats', () => { /* never fulfill */ });

    await page.goto(`/datasets/${datasetId}`);
    const chip = page.getByTestId('query-stage-chip').first();
    await expect(chip).toHaveText(/Filter \(score\)/, { timeout: 20_000 });
    await chip.click();

    const editor = page.locator('[data-radix-popper-content-wrapper]');
    const operatorDisplays = editor.locator('select').evaluateAll(sels =>
        sels.map(s => (s as HTMLSelectElement).selectedOptions[0]?.textContent ?? '')
    );
    await expect(editor.locator('select').first()).toBeVisible({ timeout: 10_000 });
    const displayed = await operatorDisplays;
    expect(displayed).toContain('>=');
    expect(displayed).toContain('<=');
    expect(displayed).not.toContain('=');
});
