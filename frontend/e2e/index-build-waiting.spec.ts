import { randomUUID } from 'node:crypto';
import { test as base, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';

// One index build at a time (backend index_jobs.enqueue_build): a build queued while another runs hasn't started,
// holds no dataset lane, and says what it waits for instead of "Waiting for an execution slot".
const test = base.extend<{ datasetId: number }>({
    datasetId: async ({ request }, provide) => {
        const response = await request.post('/api/datasets', { data: { name: `e2e-index-wait-${randomUUID()}` } });
        expect(response.ok()).toBeTruthy();
        const dataset = await response.json();
        try {
            await provide(dataset.id);
        } finally {
            const deleted = await request.delete(`/api/datasets/${dataset.id}`);
            expect(deleted.ok()).toBeTruthy();
        }
    },
});

test.use({ storageState: AUTH_STATE });

test('a queued index build says it waits for another index build', async ({ page, datasetId }) => {
    const waiting = {
        id: 'build', kind: 'field_index_build', state: 'queued', lane: 'user', placement: 'server', started_by: 'Alex',
        progress: { done: 0, total: null, message: null },
        cancel_requested: false, stalled: false, retry_delayed: false,
        waiting_for: 'for another index build to finish (one index build at a time)',
    };
    await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => route.fulfill({
        json: { running: [], resuming: [], waiting: [waiting], next_cursor: null },
    }));
    await page.goto(`/datasets/${datasetId}`);
    const indicator = page.getByTestId('dataset-jobs');
    await expect(indicator).toContainText('1 waiting');
    await indicator.locator('summary').click();
    await expect(indicator).toContainText('Waiting for another index build to finish (one index build at a time)');
    await expect(indicator).not.toContainText('Waiting for an execution slot');
});
