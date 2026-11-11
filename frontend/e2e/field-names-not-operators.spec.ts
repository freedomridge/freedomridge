import { test, expect } from '@playwright/test';
import { AUTH_STATE, expandField, expectTotal } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// The sidebar keeps a field's draft under the field's name, and read `exclude_` at the start of a name as "not":
// ticking `blurry` under a field named `exclude_reason` previewed and applied `reason not in ["blurry"]`. There is
// no field `reason`, so it matched all six images while the chip said "reason: not blurry". The field filters
// itself now. (Names ending in `_min` / `_max` are covered by tests/draft-keys.test.cjs.)
test.use({ storageState: AUTH_STATE });

const DS_NAME = 'e2e-field-names-not-operators';

let datasetId: number;
let cookie: string;

test.beforeAll(async () => {
    cookie = await adminLogin();
    for (const d of await apiFetch(cookie, '/datasets')) {
        if (d.name === DS_NAME) await deleteDatasetAndWait(cookie, d.id);  // a crashed run's
    }
    datasetId = (await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name: DS_NAME, description: 'field names fixture' }),
    })).id;
    await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
        method: 'POST',
        body: JSON.stringify({
            samples: Array.from({ length: 6 }, (_, i) => ({
                filepath: `s3://e2e-field-names/img_${i}.jpg`,
                metadata: { idx: i, ...(i < 2 ? { exclude_reason: 'blurry' } : i === 2 ? { exclude_reason: 'duplicate' } : {}) },
            })),
        }),
    });
    await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
});

test.afterAll(async () => {
    if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
});

test('a field named exclude_reason filters itself', async ({ page }) => {
    await page.goto(`/datasets/${datasetId}`);
    await expectTotal(page, 6);
    await expandField(page, 'exclude_reason');
    await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'blurry', exact: true }).check();
    await expect(page.getByTestId('draft-filters')).toContainText('exclude_reason: blurry');
    await expect(page.getByTestId('grid-view-count')).toHaveText(/^\s*2 images\s*$/);
    await expect(page.getByTestId('field-section-exclude_reason')).toHaveAttribute('data-filtered', 'true');

    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 2);
    await expect(page.getByTestId('query-stage-chip')).toContainText('exclude_reason: blurry');
});
