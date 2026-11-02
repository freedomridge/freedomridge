// First-run experience: a bare dataset (filepaths only) must NUDGE, not blank.
// Before these states existed, zero embedding fields left the scatter a silently
// blank canvas and the map an empty world — indistinguishable from a bug.
import { test, expect } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE } from './helpers';

test.use({ storageState: AUTH_STATE });

const NAME = 'e2e-firstrun';
let dsId: number;

test.beforeAll(async () => {
    const cookie = await adminLogin();
    for (const d of await apiFetch(cookie, '/datasets')) {
        if (d.name === NAME) await deleteDatasetAndWait(cookie, d.id);
    }
    const ds = await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name: NAME, description: 'bare first-run fixture' }),
    });
    dsId = ds.id;
    await apiFetch(cookie, `/datasets/${dsId}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: [{ filepath: 's3://e2e/bare_0.jpg' }, { filepath: 's3://e2e/bare_1.jpg' }] }),
    });
});

test.afterAll(async () => {
    if (!dsId) return;
    const cookie = await adminLogin();
    await apiFetch(cookie, `/datasets/${dsId}`, { method: 'DELETE' });
});

test('bare dataset: embeddings panel nudges with the SDK snippet instead of a blank canvas', async ({ page }) => {
    await page.goto(`/datasets/${dsId}`);
    const nudge = page.getByTestId('embeddings-empty-no-fields');
    await expect(nudge).toBeVisible({ timeout: 20_000 });
    await expect(nudge).toContainText('compute_embeddings');
    await expect(nudge).toContainText(`Dataset.from_id(${dsId})`);
});

test('bare dataset: map names the missing locations instead of an empty world', async ({ page }) => {
    await page.goto(`/datasets/${dsId}`);
    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('map');
    const nudge = page.getByTestId('map-empty-no-locations');
    await expect(nudge).toBeVisible({ timeout: 20_000 });
    await expect(nudge).toContainText('0 of 2 samples have a location');
    await expect(nudge).toContainText('location=(lon, lat)');
});
