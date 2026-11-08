import { test, expect } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { API, adminLogin, apiFetch, readState } from './api';
import { AUTH_STATE, gotoDataset, expectTotal, expandField } from './helpers';

test.use({ storageState: AUTH_STATE });

test.afterEach(async ({ page }) => {
    await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('dataset description credits stay visible while browsing and filtering', async ({ page }) => {
    const { datasetId } = readState();
    await page.route(new RegExp(`/api/datasets/${datasetId}$`), async route => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...(await response.json()),
            description: 'KITTI — Geiger, Lenz, Stiller, Urtasun. CC BY-NC-SA 3.0 https://www.cvlibs.net/datasets/kitti/',
        } });
    });
    await gotoDataset(page);
    const credits = page.getByTestId('dataset-description');
    await expect(credits).toContainText('CC BY-NC-SA 3.0');
    await expect(credits.getByRole('link')).toHaveAttribute('href', 'https://www.cvlibs.net/datasets/kitti/');
    await page.getByTestId('grid-select-all').click();
    await expect(credits).toBeVisible();
});

test('guest at 100k can browse and save a view without calling selection_token', async ({ page }) => {
    const admin = await adminLogin();
    const username = `e2e_guest_${randomUUID()}`;
    const guest = await apiFetch(admin, '/users', {
        method: 'POST', body: JSON.stringify({ username, password: 'GuestTest123!', role: 'guest' }),
    });
    try {
        const login = await page.request.post(`${API}/auth/login`, { data: { username, password: 'GuestTest123!' } });
        expect(login.ok()).toBeTruthy();
        const password = await page.request.post(`${API}/auth/change-password`, {
            data: { current_password: 'GuestTest123!', new_password: 'GuestTest456!' },
        });
        expect(password.ok()).toBeTruthy();
        let tokenRequests = 0;
        page.on('request', request => { if (request.url().includes('/selection_token')) tokenRequests++; });
        // Keep real browsing rows and backend guest authorization; model the 100k count
        // at the API boundary instead of inserting 100k fixture rows into every CI run.
        await page.route(/\/api\/datasets\/\d+\/samples(?:\/query)?(?:\?|$)/, async route => {
            const response = await route.fetch();
            const json = await response.json();
            await route.fulfill({ response, json: { ...json, total: 100_000 } });
        });
        await gotoDataset(page);
        await page.getByTestId('search-open').click();
        await expect(page.getByRole('button', { name: 'Upload…' })).toBeVisible();
        await expect(page.getByTestId('image-search-url')).toHaveCount(0);
        await page.keyboard.press('Escape');
        const select = page.getByTestId('grid-select-all');
        await expect(select).toBeDisabled();
        await select.locator('..').hover();
        await expect(page.getByRole('tooltip')).toContainText('Browse, filter, export or save this view');
        await expectTotal(page, 100_000);
        await expect(page.getByRole('button', { name: 'Tag', exact: true })).toHaveCount(0);
        const { datasetId } = readState();
        const saved = await page.request.post(`${API}/datasets/${datasetId}/views`, {
            data: { name: `Guest browse view ${username}`, content: { stages: [] }, is_public: false },
        });
        expect(saved.ok()).toBeTruthy();
        expect(tokenRequests).toBe(0);
        // Returning to a small result set makes Select All available again.
        await page.unrouteAll({ behavior: 'wait' });
        await page.reload();
        await expectTotal(page, 12);
        await expect(select).toBeEnabled();
        await select.click();
        await expect(page.getByText('12 selected', { exact: true }).first()).toBeVisible();
        expect(tokenRequests).toBe(0);
        await page.getByRole('button', { name: 'Clear Draft', exact: true }).click();
        // A count race explains the restriction, then changing the view clears it.
        await page.route(`**/api/datasets/${datasetId}/samples/query`, async route => {
            const response = await route.fetch();
            const json = await response.json();
            const total = route.request().postDataJSON()?.limit === 1 ? 100_000 : json.total;
            await route.fulfill({ response, json: { ...json, total } });
        });
        await select.click();
        const notice = page.getByRole('status').filter({ hasText: 'Select All above 50,000' });
        await expect(notice).toBeVisible();
        await expandField(page, 'split');
        await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'train' }).check();
        await expect(notice).toHaveCount(0);
        expect(tokenRequests).toBe(0);
    } finally {
        await page.unrouteAll({ behavior: 'ignoreErrors' });
        await apiFetch(admin, `/users/${guest.id}`, { method: 'DELETE' });
    }
});


test.describe('touch credits', () => {
    test.use({ hasTouch: true });

    for (const width of [390, 600, 768]) {
        test(`credits remain readable without collapsing the header at ${width}px`, async ({ page }, testInfo) => {
            const { datasetId } = readState();
            const description = 'COCO val2017 — COCO Consortium, annotations CC BY 4.0. Photos © their Flickr creators; '
                + 'individual licenses (including non-commercial terms) and source links are in image metadata. '
                + 'https://cocodataset.org/#termsofuse';
            await page.setViewportSize({ width, height: 844 });
            await page.route(new RegExp(`/api/datasets/${datasetId}$`), async route => {
                const response = await route.fetch();
                await route.fulfill({ response, json: { ...(await response.json()), description } });
            });
            await gotoDataset(page);
            const credits = page.getByTestId('dataset-description');
            await expect(credits).toHaveAttribute('title', description);
            const box = await credits.boundingBox();
            expect(box).not.toBeNull();
            expect(box!.width).toBeGreaterThanOrEqual(160);
            expect(box!.height).toBeLessThanOrEqual(64);
            expect(box!.height).toBeGreaterThan(0);
            expect(box!.y + box!.height).toBeLessThan(300);
            const title = await page.getByRole('heading', { level: 1 }).boundingBox();
            expect(title!.width).toBeGreaterThanOrEqual(160);
            expect(title!.height).toBeLessThanOrEqual(28);
            const textBox = await page.getByTestId('dataset-description-text').boundingBox();
            expect(textBox!.height).toBeLessThanOrEqual(32);
            // Fulfill locally: verify a real touch opens the license, without external network.
            const licenseUrl = 'https://cocodataset.org/#termsofuse';
            await page.context().route('https://cocodataset.org/**', route => route.fulfill({
                contentType: 'text/html', body: '<h1>COCO license terms</h1>',
            }));
            try {
                const opened = page.waitForEvent('popup');
                await credits.getByRole('link', { name: 'Data credits & license' }).tap();
                const license = await opened;
                await expect(license).toHaveURL(licenseUrl);
                await expect(license.getByRole('heading')).toHaveText('COCO license terms');
                await license.close();
            } finally {
                await page.context().unrouteAll({ behavior: 'ignoreErrors' });
            }
            if (width === 390) await page.screenshot({ path: testInfo.outputPath('credits-390.png') });
        });
    }
});
