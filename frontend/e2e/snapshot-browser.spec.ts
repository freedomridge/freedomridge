import { test, expect, type Page } from '@playwright/test';

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

const PIXEL = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a+XcAAAAASUVORK5CYII=', 'base64');

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
                body: PIXEL,
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

// --- The address is a link to what the page shows: search, view, page and sample ---

const param = (page: Page, name: string) => new URL(page.url()).searchParams.get(name);

// A version of 130 samples, ids 1 to 130 in pages of 60, whatever the search or view; each has a 1 × 1 image.
async function manySamples(page: Page): Promise<string[]> {
    const requests: string[] = [];
    await page.route('**/api/datasets/1/versions/41/samples?**', async route => {
        const url = new URL(route.request().url());
        requests.push(url.search);
        const offset = Number(url.searchParams.get('offset'));
        const ids = Array.from({ length: Math.min(60, 130 - offset) }, (_, index) => offset + index + 1);
        await route.fulfill({ json: { items: ids.map(id => ({ ...savedSample, id, metadata: {} })), total: 130 } });
    });
    await page.route('**/api/samples/*/media?**', route => route.fulfill({ contentType: 'image/png', body: PIXEL }));
    return requests;
}

test('the address holds the search, the saved view, the page and the open sample, and a reload keeps them', async ({ page }) => {
    const requests = await manySamples(page);
    await page.goto('/datasets/1/versions/41');
    const search = page.getByRole('textbox', { name: 'Search snapshot' });
    await search.fill('train');
    await search.press('Enter');
    await expect.poll(() => param(page, 'search')).toBe('train');
    await search.fill('accepted');
    await search.press('Enter');
    await expect.poll(() => param(page, 'search')).toBe('accepted');

    // Back steps through the searches, and the box shows the one the address names.
    await page.goBack();
    await expect(search).toHaveValue('train');
    expect(param(page, 'search')).toBe('train');

    await page.getByLabel('Saved view of this version').selectOption('rejected');
    await page.getByRole('button', { name: 'Next page' }).click();
    await expect(page.getByText('Page 2')).toBeVisible();
    await page.getByRole('button', { name: 'Open saved sample 61' }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    expect(new URL(page.url()).search).toBe('?search=train&view=rejected&page=2&sample=61');

    await page.reload();
    await expect(page.getByRole('dialog')).toBeVisible();
    const last = new URLSearchParams(requests[requests.length - 1]);
    expect([last.get('q'), last.get('view'), last.get('offset')]).toEqual(['train', 'rejected', '60']);

    // With the sample closed, the page behind it shows the same search, view and page.
    await page.keyboard.press('Escape');
    await expect(search).toHaveValue('train');
    await expect(page.getByLabel('Saved view of this version')).toHaveValue('rejected');
    await expect(page.getByText('Page 2')).toBeVisible();

    // A new search starts on the first page with no sample open.
    await search.fill('val');
    await search.press('Enter');
    await expect.poll(() => new URL(page.url()).search).toBe('?search=val&view=rejected');
});

test('Back closes the open sample, closing it goes back, and Previous and Next stay on its history entry', async ({ page }) => {
    await manySamples(page);
    await page.goto('/datasets/1/versions/41');
    const dialog = page.getByRole('dialog');
    const search = page.getByRole('textbox', { name: 'Search snapshot' });
    await search.fill('typed, not searched');
    await page.getByRole('button', { name: 'Open saved sample 1', exact: true }).click();
    await expect(dialog).toBeVisible();
    const entries = await page.evaluate(() => window.history.length);

    // The entry behind the sample has the same search, so the text typed there stays.
    await page.goBack();
    await expect(dialog).toHaveCount(0);
    expect(param(page, 'sample')).toBeNull();
    await expect(search).toHaveValue('typed, not searched');
    await page.goForward();
    await expect(dialog).toBeVisible();

    // Next twice: the address follows, with no new entry.
    await page.keyboard.press('ArrowRight');
    await expect(dialog.getByText('Sample 2', { exact: true })).toBeVisible();
    await page.keyboard.press('ArrowRight');
    await expect(dialog.getByText('Sample 3', { exact: true })).toBeVisible();
    await expect.poll(() => param(page, 'sample')).toBe('3');
    expect(await page.evaluate(() => window.history.length)).toBe(entries);

    // Closing right after a step: the history is as before the open, with the sample closed one Forward away.
    await page.keyboard.press('ArrowRight');
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    expect(param(page, 'sample')).toBeNull();
    expect(await page.evaluate(() => window.history.length)).toBe(entries);
    await page.goForward();
    await expect(dialog).toBeVisible();
    expect(param(page, 'sample')).toBe('4');
});

test('holding Next writes the address when the steps pause, not at each step', async ({ page }) => {
    await manySamples(page);
    await page.goto('/datasets/1/versions/41');
    await page.getByRole('button', { name: 'Open saved sample 1', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    // The page's own history writes from here on. Next follows each with one of its own, whose state carries its
    // mark (__NA); those aren't counted.
    await page.evaluate(() => {
        const writes: string[] = [];
        Object.assign(window, { addressWrites: writes });
        for (const name of ['pushState', 'replaceState'] as const) {
            const patched = window.history[name].bind(window.history);
            window.history[name] = (state: { __NA?: boolean } | null, unused: string, url?: string | URL | null) => {
                if (!state?.__NA) writes.push(name);
                patched(state, unused, url);
            };
        }
    });
    const addressWrites = () => page.evaluate(() => (window as unknown as { addressWrites: string[] }).addressWrites);

    for (let press = 0; press < 10; press += 1) await page.keyboard.press('ArrowRight');
    await expect.poll(async () => (await addressWrites()).length).toBeGreaterThan(0);
    await expect.poll(() => param(page, 'sample')).not.toBe('1');
    // One write for the ten steps. A stall between two presses longer than the pause would add one.
    const writes = await addressWrites();
    expect(writes.length).toBeLessThanOrEqual(2);
    expect(new Set(writes)).toEqual(new Set(['replaceState']));
});

test('a Compare link opened on a sample closes without leaving the page, and a search keeps its parameters', async ({ page }) => {
    const requests: string[] = [];
    page.on('request', request => {
        if (request.url().includes('/versions/41/samples')) requests.push(new URL(request.url()).search);
    });
    await page.goto('/datasets/1/versions/41?change_type=removed&diff_base=40&diff_target=41&sample=7');
    await expect(page.getByRole('dialog')).toBeVisible();
    const entries = await page.evaluate(() => window.history.length);

    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(new URL(page.url()).search).toBe('?change_type=removed&diff_base=40&diff_target=41');
    expect(await page.evaluate(() => window.history.length)).toBe(entries);
    await expect(page.getByText('Showing removed samples as they were in this saved version.')).toBeVisible();

    const search = page.getByRole('textbox', { name: 'Search snapshot' });
    await search.fill('train');
    await search.press('Enter');
    await expect.poll(() => new URL(page.url()).search).toBe('?change_type=removed&diff_base=40&diff_target=41&search=train');
    await expect.poll(() => requests[requests.length - 1]).toContain('q=train');
    const last = new URLSearchParams(requests[requests.length - 1]);
    expect([last.get('change_type'), last.get('diff_base'), last.get('diff_target')]).toEqual(['removed', '40', '41']);
});

test('a sample that is not on its page says so', async ({ page }) => {
    await page.goto('/datasets/1/versions/41?sample=99');
    await expect(page.getByRole('alert').filter({ hasText: 'Sample 99 is not on this page of the snapshot, so it is not open.' })).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('with the live address off, the address stays the page\'s path; Back, Forward and a reload still restore each entry', async ({ page }) => {
    // FRIDGE_LIVE_ADDRESS=off: the frontend server's real answer, with liveAddress false.
    await page.route('**/runtime-config', async route => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...(await response.json()), liveAddress: false } });
    });
    const requests = await manySamples(page);
    // A Compare link opens on its parameters, which then leave the address.
    await page.goto('/datasets/1/versions/41?change_type=removed&diff_base=40&diff_target=41');
    await expect(page.getByText('Showing removed samples as they were in this saved version.')).toBeVisible();
    await expect.poll(() => new URL(page.url()).search).toBe('');
    const search = page.getByRole('textbox', { name: 'Search snapshot' });
    await search.fill('train');
    await search.press('Enter');
    await page.getByRole('button', { name: 'Next page' }).click();
    await expect(page.getByText('Page 2')).toBeVisible();
    await page.getByRole('button', { name: 'Open saved sample 61' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await page.keyboard.press('ArrowRight');
    await expect(dialog.getByText('Sample 62', { exact: true })).toBeVisible();
    await page.waitForTimeout(500);  // the step is written to the entry once Next pauses
    expect(new URL(page.url()).search).toBe('');

    // A reload reopens the entry: the Compare link's parameters, the search, the page and the sample.
    await page.reload();
    await expect(dialog.getByText('Sample 62', { exact: true })).toBeVisible();
    const last = new URLSearchParams(requests[requests.length - 1]);
    expect([last.get('change_type'), last.get('q'), last.get('offset')]).toEqual(['removed', 'train', '60']);

    // Back closes the sample, then steps through the page and the search; Forward returns.
    await page.goBack();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByText('Page 2')).toBeVisible();
    await page.goBack();
    await expect(page.getByText('Page 1')).toBeVisible();
    await expect(search).toHaveValue('train');
    await page.goBack();
    await expect(search).toHaveValue('');
    await expect(page.getByText('Showing removed samples as they were in this saved version.')).toBeVisible();
    await page.goForward();
    await expect(search).toHaveValue('train');
    expect(new URL(page.url()).search).toBe('');
});
