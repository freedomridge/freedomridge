import { randomUUID } from 'node:crypto';
import { test as base, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';

const test = base.extend<{ datasetId: number }>({
    datasetId: async ({ request }, provide) => {
        const response = await request.post('/api/datasets', { data: { name: `e2e-jobs-${randomUUID()}` } });
        expect(response.ok()).toBeTruthy();
        const dataset = await response.json();
        try {
            const samples = await request.post(`/api/datasets/${dataset.id}/samples`, {
                data: { samples: [{ filepath: 's3://e2e/job-indicator.jpg', tags: ['seed'], metadata: { note: 'private fixture' } }] },
            });
            expect(samples.ok()).toBeTruthy();
            await provide(dataset.id);
        } finally {
            const deleted = await request.delete(`/api/datasets/${dataset.id}`);
            expect(deleted.ok()).toBeTruthy();
        }
    },
});

function job(id: string, kind: string, state: 'running' | 'queued', lane: 'user' | 'maintenance' = 'user') {
    return {
        id, kind, state, lane, placement: 'server', started_by: 'Alex',
        progress: { done: 20, total: 100, message: 'Measuring samples' },
        cancel_requested: false, stalled: false, retry_delayed: false,
    };
}

test.describe('dataset job indicator', () => {
    test.use({ storageState: AUTH_STATE });

    test('shows running work, its submitter and progress, plus waiting work without blocking edits', async ({ page, datasetId }) => {
        let finished = false;
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => route.fulfill({
            json: finished ? { running: [], waiting: [], next_cursor: null } : {
                running: [job('fit', 'umap_fit', 'running')],
                waiting: [job('tag', 'bulk_tag', 'queued')],
                next_cursor: null,
            },
        }));
        await page.goto(`/datasets/${datasetId}`);
        const indicator = page.getByTestId('dataset-jobs');
        await expect(indicator).toContainText('Fitting UMAP');
        await expect(indicator).toContainText('1 waiting');
        await indicator.locator('summary').click();
        await expect(indicator).toContainText('by Alex');
        await expect(indicator).toContainText('20 / 100');
        await expect(indicator).toContainText('Waiting behind Fitting UMAP');
        await expect(indicator).toContainText('Ordinary edits remain available');
        const tag = `indicator_edit_${Date.now()}`;
        await page.getByTestId('grid-cell').first().click();
        await page.getByTestId('modal-tag').click();
        await page.getByPlaceholder('Enter tag name...').fill(tag);
        await page.getByRole('button', { name: 'Add', exact: true }).click();
        await page.getByRole('button', { name: 'Apply Changes' }).click();
        // Saving, the Tag dialog shows the tag twice (its row and the pending "+1"): check it once the dialog
        // has closed, in the single-image view that stays open.
        await expect(page.getByText('Tag — 1 Sample')).toHaveCount(0);
        await expect(page.getByRole('dialog').getByText(tag)).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.getByTestId('total-samples')).toBeVisible();
        finished = true;
        await expect(indicator).toHaveCount(0, { timeout: 12_000 });
    });

    test('keeps routine upkeep quiet and navigates every waiting page', async ({ page, datasetId }) => {
        const running = { ...job('stats', 'statistics', 'running', 'maintenance'), started_by: 'System' };
        let nextPage = false;
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => {
            const cursor = new URL(route.request().url()).searchParams.get('cursor');
            nextPage ||= cursor === 'next';
            return route.fulfill({
                json: { running: [running], waiting: [job(cursor ? 'second' : 'first', cursor ? 'create_version' : 'bulk_tag', 'queued')], next_cursor: cursor ? null : 'next' },
            });
        });
        await page.goto(`/datasets/${datasetId}`);
        const indicator = page.getByTestId('dataset-jobs');
        await expect(indicator).toContainText('Updating statistics');
        await expect(indicator).not.toHaveAttribute('role', 'alert');
        await indicator.locator('summary').click();
        await expect(indicator).toContainText('by System');
        await expect(indicator).toContainText('Waiting for an execution slot');
        await expect(indicator).not.toContainText('Waiting behind Updating statistics');
        await indicator.getByRole('button', { name: 'Next waiting jobs' }).click();
        await expect(indicator).toContainText('Creating version');
        await expect(indicator.getByTestId('dataset-job')).toHaveCount(2);
        await indicator.getByRole('button', { name: 'Previous waiting jobs' }).click();
        await expect(indicator).toContainText('Bulk tagging');
        await expect(indicator).not.toContainText('Creating version');
        expect(nextPage).toBe(true);
    });

    test('shows cancellation and retries truthfully, and handles unavailable status quietly', async ({ page, datasetId }) => {
        let unavailable = false;
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => route.fulfill(
            unavailable ? { status: 503, json: { detail: 'temporarily offline' } } : {
                json: {
                    running: [{ ...job('cancel', 'umap_fit', 'running'), cancel_requested: true }],
                    waiting: [{ ...job('retry', 'bulk_tag', 'queued'), retry_delayed: true }],
                    next_cursor: null,
                },
            },
        ));
        await page.goto(`/datasets/${datasetId}`);
        await page.getByTestId('dataset-jobs').locator('summary').click();
        await expect(page.getByTestId('dataset-jobs')).toContainText('Cancellation requested');
        await expect(page.getByTestId('dataset-jobs')).toContainText('Waiting to retry');
        unavailable = true;
        await expect(page.getByTestId('dataset-jobs-unavailable')).toBeVisible({ timeout: 12_000 });
        await expect(page.getByText(/Couldn't load data/)).toHaveCount(0);
        unavailable = false;
        await page.getByTestId('dataset-jobs-unavailable').getByRole('button', { name: 'Retry' }).click();
        await expect(page.getByTestId('dataset-jobs')).toBeVisible();
    });

    test('reads the real dataset-scoped active jobs response without assuming an empty queue', async ({ page, datasetId }) => {
        const response = await page.request.get(`/api/datasets/${datasetId}/jobs/active`);
        expect(response.ok()).toBeTruthy();
        const body = await response.json();
        expect(body.running).toEqual(expect.any(Array));
        expect(body.resuming).toEqual(expect.any(Array));
        expect(body.waiting).toEqual(expect.any(Array));
        expect(body.next_cursor === null || typeof body.next_cursor === 'string').toBeTruthy();
        for (const item of [...body.running, ...body.resuming, ...body.waiting]) {
            expect(item).toMatchObject({ id: expect.any(String), kind: expect.any(String), started_by: expect.any(String) });
            for (const key of ['error', 'result', 'resources', 'created_by', 'lease_token']) expect(item).not.toHaveProperty(key);
        }
        await page.goto(`/datasets/${datasetId}`);
        await expect(page.getByTestId('total-samples')).toBeVisible();
    });

    test('shows stalled leases and server retry state even when the browser clock is wrong', async ({ page, datasetId }) => {
        await page.clock.setFixedTime(new Date('2050-01-01T00:00:00Z'));
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => route.fulfill({
            json: {
                running: [{ ...job('stalled', 'umap_fit', 'running'), stalled: true }],
                waiting: [
                    job('waiting', 'create_version', 'queued'),
                    { ...job('retry', 'statistics', 'queued', 'maintenance'), retry_delayed: true,
                        failures: 1, available_at: '2026-10-17T00:00:00Z' },
                ],
                next_cursor: null,
            },
        }));
        await page.goto(`/datasets/${datasetId}`);
        const indicator = page.getByTestId('dataset-jobs');
        await indicator.locator('summary').click();
        await expect(indicator).toContainText('Stalled — waiting for a worker');
        await expect(indicator).toContainText('Waiting behind stalled Fitting UMAP');
        await expect(indicator).toContainText('Waiting to retry');
        await expect(indicator.getByText('Running', { exact: true })).toHaveCount(0);
    });

    test('polls one bounded page after visiting several waiting pages', async ({ page, datasetId }) => {
        const requests: string[] = [];
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => {
            const cursor = new URL(route.request().url()).searchParams.get('cursor') || '1';
            requests.push(cursor);
            const number = Number(cursor);
            return route.fulfill({ json: {
                running: [job('running', 'umap_fit', 'running')],
                waiting: [job(`waiting-${number}`, `page_${number}`, 'queued')],
                next_cursor: number < 4 ? String(number + 1) : null,
            } });
        });
        await page.goto(`/datasets/${datasetId}`);
        const indicator = page.getByTestId('dataset-jobs');
        await indicator.locator('summary').click();
        for (const number of [2, 3, 4]) {
            await indicator.getByRole('button', { name: /Next waiting jobs|Show more waiting jobs/ }).click();
            await expect(indicator).toContainText(`page ${number}`);
        }
        const before = requests.length;
        await expect.poll(() => requests.length, { timeout: 8_000 }).toBeGreaterThan(before);
        await page.waitForTimeout(1_000);
        expect(requests.slice(before)).toEqual(['4']);
        await expect(indicator.getByTestId('dataset-job')).toHaveCount(2);
        await expect(indicator).toContainText('Fitting UMAP');
    });

    test('returns to the first page when the current waiting page finishes', async ({ page, datasetId }) => {
        let finished = false;
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => {
            const cursor = new URL(route.request().url()).searchParams.get('cursor');
            return route.fulfill({ json: {
                running: [],
                waiting: finished && cursor ? [] : [job(cursor ? 'second' : 'first', cursor ? 'bulk_tag' : 'create_version', 'queued')],
                next_cursor: cursor || finished ? null : 'next',
            } });
        });
        await page.goto(`/datasets/${datasetId}`);
        const indicator = page.getByTestId('dataset-jobs');
        await indicator.locator('summary').click();
        await indicator.getByRole('button', { name: /Next waiting jobs|Show more waiting jobs/ }).click();
        await expect(indicator).toContainText('Bulk tagging');
        finished = true;
        await expect(indicator).not.toContainText('Bulk tagging', { timeout: 12_000 });
        await expect(indicator).toContainText('Creating version');
        await expect(indicator.getByRole('button', { name: 'Previous waiting jobs' })).toHaveCount(0);
    });

    test('shows dataset work while browsing a saved version', async ({ page, datasetId }) => {
        const created = await page.request.post(`/api/datasets/${datasetId}/versions`, {
            data: { name: 'indicator-snapshot', include_metadata: true },
        });
        expect(created.ok()).toBeTruthy();
        const version = await created.json();
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => route.fulfill({
            json: { running: [job('reverting', 'revert_version', 'running')], waiting: [], next_cursor: null },
        }));
        await page.goto(`/datasets/${datasetId}/versions/${version.id}`);
        await expect(page.getByText('Read-only snapshot')).toBeVisible();
        await expect(page.getByTestId('dataset-jobs')).toContainText('Reverting version');
        await expect(page.getByRole('link', { name: 'Back to current dataset' })).toHaveAttribute('href', `/datasets/${datasetId}`);
    });

    test('keeps a retrying lane owner visible across waiting pages and until it finishes', async ({ page, datasetId }) => {
        let phase: 'retry' | 'ready' | 'done' = 'retry';
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => {
            const cursor = new URL(route.request().url()).searchParams.get('cursor');
            return route.fulfill({ json: {
                running: [job('upkeep', 'statistics', 'running', 'maintenance')],
                resuming: phase === 'done' ? [] : [{ ...job('owner', 'umap_fit', 'queued'), placement: 'compute', retry_delayed: phase === 'retry' }],
                waiting: [job(cursor ? 'second' : 'first', cursor ? 'create_version' : 'bulk_tag', 'queued')],
                next_cursor: cursor ? null : 'next',
            } });
        });
        await page.goto(`/datasets/${datasetId}`);
        const indicator = page.getByTestId('dataset-jobs');
        await expect(indicator.locator('summary')).toContainText('Fitting UMAP');
        await indicator.locator('summary').click();
        const owner = indicator.locator('[data-job-id="owner"]');
        await expect(owner).toContainText('Waiting to retry');
        await expect(indicator.locator('[data-job-id="first"]')).toContainText('Waiting behind Fitting UMAP');
        await indicator.getByRole('button', { name: 'Next waiting jobs' }).click();
        await expect(owner).toHaveCount(1);
        await expect(indicator.locator('[data-job-id="second"]')).toContainText('Waiting behind Fitting UMAP');
        phase = 'ready';
        await expect(owner).toContainText('Waiting to resume on compute', { timeout: 12_000 });
        await expect(owner).not.toContainText('Waiting behind');
        phase = 'done';
        await expect(owner).toHaveCount(0, { timeout: 12_000 });
        await expect(indicator.locator('[data-job-id="second"]')).toContainText('Waiting for an execution slot');
        await expect(indicator).toContainText('Updating statistics');
    });

    test('still handles an expired session instead of swallowing authentication failures', async ({ page, datasetId }) => {
        await page.route(`**/api/datasets/${datasetId}/jobs/active?*`, route => route.fulfill({
            status: 401, json: { detail: 'Session expired' },
        }));
        await page.goto(`/datasets/${datasetId}`);
        await expect(page).toHaveURL(/\/login\?expired=true/);
        await expect(page.getByTestId('dataset-jobs')).toHaveCount(0);
    });
});
