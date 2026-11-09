import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// The embeddings panel writes a marker before loading its scatter and clears it once drawn: a marker left from
// another page load means that load crashed the tab, so it recovers with a small sample and says so. A marker from
// this page load only means the panel remounted mid-load (a Find similar search did that): no warning, no smaller
// sample (lib/load-marker.ts).
test.use({ storageState: AUTH_STATE });

const FIELD = 'e2e_marker';
let cookie = '';
let dsId = 0;
let key = '';

test.beforeAll(async () => {
    cookie = await adminLogin();
    dsId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-load-marker-${Date.now()}` }) })).id;
    const samples = Array.from({ length: 20 }, (_, i) => ({
        filepath: `s3://e2e-load-marker/s${i}.jpg`,
        embeddings: { [FIELD]: Array.from({ length: 4 }, (_, d) => Math.sin(i + d)) },
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${dsId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    const coordinates = (ids as number[]).map((sampleId, i) => ({ sample_id: sampleId, x: i % 5, y: Math.floor(i / 5) }));
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/coordinates?field_name=${FIELD}`, { method: 'POST', body: JSON.stringify(coordinates) });
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/recompute-viz-rank?field_name=${FIELD}&full=true`, { method: 'POST' });
    const me = await apiFetch(cookie, '/auth/me');
    key = `fridge:samplePctAttempt:${me.id}:${dsId}`;
});

test.afterAll(async () => {
    if (dsId) await deleteDatasetAndWait(cookie, dsId, 120_000);
});

test('a marker left by another page load means a crash: a small sample, and the panel says why', async ({ page }) => {
    await page.addInitScript(({ marker }) => {
        sessionStorage.setItem(marker, JSON.stringify({ pct: 30, origin: 1 }));  // an earlier page load's
    }, { marker: key });
    await page.goto(`/datasets/${dsId}`);
    await expect(page.getByText(/Reduced to a small sample/)).toBeVisible({ timeout: 30_000 });
});

test('a marker from this page load is a remount mid-load: no warning', async ({ page }) => {
    await page.addInitScript(({ marker }) => {
        // As the panel would leave it while loading, then remounting, in this same page load.
        sessionStorage.setItem(marker, JSON.stringify({ pct: 30, origin: performance.timeOrigin }));
    }, { marker: key });
    await page.goto(`/datasets/${dsId}`);
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^20(\s|\/|$).*points/, { timeout: 30_000 });
    await expect(page.getByText(/Reduced to a small sample/)).toHaveCount(0);
});

test('a reload mid-load is not a crash: pagehide clears the marker', async ({ page }) => {
    await page.goto(`/datasets/${dsId}`);
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^20(\s|\/|$).*points/, { timeout: 30_000 });
    // As a load still in flight would leave it, then the tab reloads.
    await page.evaluate(marker => {
        sessionStorage.setItem(marker, JSON.stringify({ pct: 30, origin: performance.timeOrigin }));
    }, key);
    await page.reload();
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^20(\s|\/|$).*points/, { timeout: 30_000 });
    await expect(page.getByText(/Reduced to a small sample/)).toHaveCount(0);
});
