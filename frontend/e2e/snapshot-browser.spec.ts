import { test, expect } from '@playwright/test';

const savedSample = {
    id: 7,
    version_id: 41,
    filepath: 'saved-image.jpg',
    media_type: 'image',
    tags: ['train', 'accepted'],
    metadata: { segmentation: { maskPath: 'saved-mask.png' } },
};

const snapshot = {
    id: 41,
    name: 'training-v1',
    sample_count: 1,
    include_metadata: true,
    snapshot_format: 3,
    created_at: '2026-10-16T12:00:00Z',
    dataset_snapshot: {
        name: 'Training',
        media_fields: ['filepath'],
        grid_media_field: 'filepath',
        modal_media_field: 'filepath',
        segmentation_config: { default_mask_targets: { '1': 'car' } },
        fields: [{ field_name: 'segmentation', field_type: 'dict' }],
    },
};

test.beforeEach(async ({ page }) => {
    await page.route('**/api/**', async route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/api/auth/me') {
            await route.fulfill({ json: { id: 1, username: 'viewer', role: 'guest' } });
        } else if (url.pathname === '/api/datasets/1/versions/41') {
            await route.fulfill({ json: snapshot });
        } else if (url.pathname === '/api/datasets/1/versions/41/views') {
            await route.fulfill({ json: { views: [{ name: 'rejected', description: 'tagged rejected' }] } });
        } else if (url.pathname === '/api/datasets/1/versions/41/samples') {
            const none = url.searchParams.get('q') === 'missing-label' || url.searchParams.get('view') === 'rejected';
            const items = none ? [] : [savedSample];
            await route.fulfill({ json: { items, total: items.length } });
        } else if (url.pathname === '/api/samples/7/media' && url.searchParams.get('version_id') === '41') {
            await route.fulfill({
                contentType: 'image/png',
                body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a+XcAAAAASUVORK5CYII=', 'base64'),
            });
        } else {
            await route.fulfill({ status: 404, json: { detail: 'No live data in this test' } });
        }
    });
});

test('browse deleted samples and masks without requesting live state', async ({ page }) => {
    const mediaRequests: string[] = [];
    const mutations: string[] = [];
    page.on('request', request => {
        if (request.url().includes('/api/samples/7/media')) mediaRequests.push(request.url());
        if (request.url().includes('/api/') && request.method() !== 'GET') mutations.push(request.url());
    });
    await page.goto('/datasets/1/versions/41');
    await expect(page.getByText('Read-only snapshot')).toBeVisible();
    await expect(page.getByText('1 matching samples')).toBeVisible();
    await page.getByRole('button', { name: 'Open saved sample 7' }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('button', { name: /^Tag/ })).toHaveCount(0);
    await expect.poll(() => mediaRequests.some(url => new URL(url).searchParams.get('field') === 'segmentation.maskPath')).toBe(true);
    expect(mediaRequests.every(url => new URL(url).searchParams.get('version_id') === '41')).toBe(true);
    expect(mutations).toEqual([]);
});

test('search is applied to saved samples', async ({ page }) => {
    await page.goto('/datasets/1/versions/41');
    await expect(page.getByText('1 matching samples')).toBeVisible();
    await page.getByRole('textbox', { name: 'Search snapshot' }).fill('missing-label');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await expect(page.getByText('0 matching samples')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Open saved sample 7' })).toHaveCount(0);
});

test('a saved view the version recorded narrows the saved samples', async ({ page }) => {
    await page.goto('/datasets/1/versions/41');
    await expect(page.getByText('1 matching samples')).toBeVisible();
    await page.getByLabel('Saved view of this version').selectOption('rejected');
    await expect(page.getByText('0 matching samples')).toBeVisible();
    await page.getByLabel('Saved view of this version').selectOption('');
    await expect(page.getByText('1 matching samples')).toBeVisible();
});

test('a version that recorded no saved views says so', async ({ page }) => {
    await page.route('**/api/datasets/1/versions/41/views', route => route.fulfill({ json: { views: null } }));
    await page.goto('/datasets/1/versions/41');
    await expect(page.getByText("This version did not record the dataset's saved views.")).toBeVisible();
    await expect(page.getByLabel('Saved view of this version')).toHaveCount(0);
});

test('hash-only snapshots explain why historical browsing is unavailable', async ({ page }) => {
    await page.route('**/api/datasets/1/versions/41', route => route.fulfill({ json: { ...snapshot, include_metadata: false } }));
    await page.goto('/datasets/1/versions/41');
    await expect(page.getByRole('alert').filter({ hasText: 'Hash-only versions cannot be browsed' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Search snapshot' })).toHaveCount(0);
});
