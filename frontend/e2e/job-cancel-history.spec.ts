import { randomUUID } from 'node:crypto';
import { test as base, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';

const test = base.extend<{ datasetId: number }>({
    datasetId: async ({ request }, provide) => {
        const response = await request.post('/api/datasets', { data: { name: `e2e-job-history-${randomUUID()}` } });
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

function active(id: string, kind: string, state: 'running' | 'queued', canCancel: boolean) {
    return {
        id, kind, state, lane: 'user', placement: 'server', started_by: 'Alex',
        progress: { done: 20, total: 100, message: null },
        cancel_requested: false, can_cancel: canCancel, stalled: false, retry_delayed: false,
    };
}

function record(id: string, kind: string, state: string, changes: Record<string, unknown> = {}) {
    return {
        id, kind, state, dataset_id: 1, lane: 'user', placement: 'server', started_by: 'Alex',
        progress: { done: 0, total: null, message: null },
        created_at: '2026-10-18T10:00:00Z', started_at: '2026-10-18T10:00:05Z', finished_at: '2026-10-18T10:02:10Z',
        attempt: 1, cancel_requested: false, ...changes,
    };
}

test.describe('job cancel and history', () => {
    test.use({ storageState: AUTH_STATE });

    test('cancels only offered jobs, after an in-page confirmation', async ({ page, datasetId }) => {
        let cancelled = false;
        const cancels: string[] = [];
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => route.fulfill({
            json: {
                running: [{ ...active('fit', 'umap_fit', 'running', true), cancel_requested: cancelled }],
                waiting: [active('theirs', 'bulk_tag', 'queued', false)],
                next_cursor: null,
            },
        }));
        await page.route('**/api/jobs/*/cancel', route => {
            cancels.push(new URL(route.request().url()).pathname);
            cancelled = true;
            return route.fulfill({ json: {} });
        });
        await page.goto(`/datasets/${datasetId}`);
        const indicator = page.getByTestId('dataset-jobs');
        await indicator.locator('summary').click();
        await expect(indicator.getByTestId('dataset-job')).toHaveCount(2);
        const [running, waiting] = await indicator.getByTestId('dataset-job').all();
        await expect(waiting.getByRole('button', { name: 'Cancel', exact: true })).toHaveCount(0);

        await running.getByRole('button', { name: 'Cancel', exact: true }).click();
        await expect(running).toContainText('Stop this job after its current step?');
        await running.getByRole('button', { name: 'Keep it' }).click();
        expect(cancels).toEqual([]);

        await running.getByRole('button', { name: 'Cancel', exact: true }).click();
        await running.getByRole('button', { name: 'Cancel job' }).click();
        await expect(indicator).toContainText('Cancellation requested');
        await expect(running.getByRole('button', { name: 'Cancel', exact: true })).toHaveCount(0);
        expect(cancels).toEqual(['/api/jobs/fit/cancel']);
    });

    test('a busy cancellation says to retry, without a global error toast', async ({ page, datasetId }) => {
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => route.fulfill({
            json: { running: [], waiting: [active('queued', 'create_version', 'queued', true)], next_cursor: null },
        }));
        await page.route('**/api/jobs/*/cancel', route => route.fulfill({
            status: 409, json: { detail: 'Job state is busy; retry cancellation' },
        }));
        await page.goto(`/datasets/${datasetId}`);
        const indicator = page.getByTestId('dataset-jobs');
        await indicator.locator('summary').click();
        await indicator.getByRole('button', { name: 'Cancel', exact: true }).click();
        await expect(indicator).toContainText('Remove this job from the queue?');
        await indicator.getByRole('button', { name: 'Cancel job' }).click();
        await expect(indicator).toContainText('The job is busy; try again in a moment.');
        await expect(page.getByText(/Couldn't load data/)).toHaveCount(0);
    });

    test('a double click sends one cancellation, and a job that already finished says so', async ({ page, datasetId }) => {
        const cancels: string[] = [];
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => route.fulfill({
            json: { running: [active('fit', 'umap_fit', 'running', true)], waiting: [], next_cursor: null },
        }));
        await page.route('**/api/jobs/*/cancel', async route => {
            cancels.push(route.request().url());
            await new Promise(resolve => setTimeout(resolve, 300));
            return route.fulfill({ json: record('fit', 'umap_fit', 'succeeded') });
        });
        await page.goto(`/datasets/${datasetId}`);
        const indicator = page.getByTestId('dataset-jobs');
        await indicator.locator('summary').click();
        await indicator.getByRole('button', { name: 'Cancel', exact: true }).click();
        await indicator.getByRole('button', { name: 'Cancel job' }).dblclick();
        await expect(page.getByText('Fitting UMAP had already finished before it could be cancelled')).toBeVisible();
        expect(cancels).toHaveLength(1);
    });

    test('history shows who ran what, how it ended and how long it took, page by page', async ({ page, datasetId }) => {
        await page.route(`**/api/datasets/${datasetId}/jobs?*`, route => {
            const cursor = new URL(route.request().url()).searchParams.get('cursor');
            return route.fulfill({
                json: cursor ? { items: [record('old', 'create_version', 'cancelled', { started_by: 'System' })], next_cursor: null } : {
                    items: [
                        record('stats', 'statistics', 'succeeded'),
                        record('tag', 'bulk_tag', 'failed', {
                            attempt: 3, error: { type: 'LockNotAvailableError', message: 'could not obtain lock' },
                        }),
                    ],
                    next_cursor: 'older',
                },
            });
        });
        await page.goto(`/datasets/${datasetId}`);
        await page.getByTestId('open-job-history').click();
        const dialog = page.getByTestId('job-history-dialog');
        const items = dialog.getByTestId('job-history-item');
        await expect(items).toHaveCount(2);
        await expect(items.nth(0)).toContainText('Updating statistics');
        await expect(items.nth(0)).toContainText('Succeeded');
        await expect(items.nth(0)).toContainText('by Alex');
        await expect(items.nth(0)).toContainText('took 2 min 5 s');
        await expect(items.nth(1)).toContainText('Failed');
        await expect(items.nth(1)).toContainText('3 attempts');
        await expect(items.nth(1)).toContainText('LockNotAvailableError: could not obtain lock');
        await dialog.getByRole('button', { name: 'Older jobs' }).click();
        await expect(items).toHaveCount(1);
        await expect(items.first()).toContainText('Cancelled');
        await expect(items.first()).toContainText('by System');
        await dialog.getByRole('button', { name: 'Newer jobs' }).click();
        await expect(items).toHaveCount(2);
    });

    test('history opens from the indicator and only polls while open', async ({ page, datasetId }) => {
        let reads = 0;
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => route.fulfill({
            json: { running: [active('fit', 'umap_fit', 'running', false)], waiting: [], next_cursor: null },
        }));
        await page.route(`**/api/datasets/${datasetId}/jobs?*`, route => {
            reads += 1;
            return route.fulfill({ json: { items: [], next_cursor: null } });
        });
        await page.goto(`/datasets/${datasetId}`);
        await page.getByTestId('dataset-jobs').locator('summary').click();
        await page.getByTestId('dataset-jobs').getByRole('button', { name: 'Job history' }).click();
        const dialog = page.getByTestId('job-history-dialog');
        await expect(dialog).toContainText('No jobs have run on this dataset yet.');
        await page.keyboard.press('Escape');
        await expect(dialog).toHaveCount(0);
        const closedAt = reads;
        await page.waitForTimeout(11_000);
        expect(reads).toBe(closedAt);
    });

    test('unavailable history says so instead of showing an empty list', async ({ page, datasetId }) => {
        await page.route(`**/api/datasets/${datasetId}/jobs?*`, route => route.fulfill({ status: 503, json: { detail: 'offline' } }));
        await page.goto(`/datasets/${datasetId}`);
        await page.getByTestId('open-job-history').click();
        const dialog = page.getByTestId('job-history-dialog');
        await expect(dialog.getByTestId('job-history-unavailable')).toBeVisible();
        await expect(dialog).not.toContainText('No jobs have run');
        await expect(page.getByText(/Couldn't load data/)).toHaveCount(0);
    });

    test('reads the real history and global job responses without assuming they are empty', async ({ page, datasetId }) => {
        for (const path of [`/api/datasets/${datasetId}/jobs`, '/api/admin/jobs']) {
            const response = await page.request.get(path);
            expect(response.ok()).toBeTruthy();
            const body = await response.json();
            expect(body.items).toEqual(expect.any(Array));
            for (const item of body.items) {
                expect(item).toMatchObject({ id: expect.any(String), kind: expect.any(String), started_by: expect.any(String) });
            }
        }
        await page.goto(`/datasets/${datasetId}`);
        await page.getByTestId('open-job-history').click();
        await expect(page.getByTestId('job-history')).toBeVisible();
    });

    test('System status lists background jobs that belong to no dataset', async ({ page }) => {
        await page.route('**/api/admin/jobs?*', route => route.fulfill({
            json: { items: [record('cleanup', 'selection_cleanup', 'succeeded', { dataset_id: null, lane: 'maintenance', started_by: 'System' })], next_cursor: null },
        }));
        await page.goto('/settings/status');
        const section = page.getByTestId('status-global-jobs');
        await expect(section).toContainText('selection cleanup');
        await expect(section).toContainText('Succeeded');
        await expect(section).toContainText('by System');
    });
});
