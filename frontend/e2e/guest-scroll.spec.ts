import { test, expect } from '@playwright/test';

const datasetId = process.env.E2E_GUEST_DATASET_ID;
test.skip(!datasetId, 'Requires a disposable demo stack and >=200 real local images; use playwright.guest.config.ts');
test.use({ storageState: { cookies: [], origins: [] } });

test('a demo guest scrolls the default grid without media 503s or logout', async ({ page }, testInfo) => {
    await page.goto('/login');
    await page.getByRole('button', { name: /Try as Guest/ }).click();
    await expect(page).toHaveURL('/');
    const identity = await page.request.get('/api/auth/me');
    expect(identity.status()).toBe(200);
    expect(await identity.json()).toMatchObject({ role: 'guest', is_demo_account: true });

    const mediaStatuses: Record<number, number> = {};
    const apiFailures: Record<number, number> = {};
    const images = new Set<string>();
    let logout = false;
    page.on('framenavigated', frame => {
        if (frame === page.mainFrame() && new URL(frame.url()).pathname === '/login') logout = true;
    });
    page.on('response', response => {
        const path = new URL(response.url()).pathname;
        const media = path.match(/^\/api\/samples\/(\d+)\/media$/);
        if (media) {
            mediaStatuses[response.status()] = (mediaStatuses[response.status()] || 0) + 1;
            if (response.status() === 200) images.add(media[1]);
        } else if (path.startsWith('/api/') && response.status() >= 400) {
            apiFailures[response.status()] = (apiFailures[response.status()] || 0) + 1;
        }
    });
    await page.goto(`/datasets/${datasetId}`);
    const total = page.getByTestId('total-samples');
    await expect(total).toBeVisible();
    await expect.poll(async () => Number((await total.innerText()).replace(/[^0-9]/g, ''))).toBeGreaterThanOrEqual(200);
    const imageCount = Number((await total.innerText()).replace(/[^0-9]/g, ''));
    expect(imageCount).toBeGreaterThanOrEqual(200);
    const grid = page.getByTestId('panel-grid');
    const scroller = grid.locator('.overflow-y-auto').first();
    await expect(grid.getByTestId('grid-cell').first()).toBeVisible();
    // Move less than a viewport each step so lazy image loading sees every row,
    // while leaving the normal sidebar/grid layout and thumbnail size intact.
    for (let step = 0; step < 400; step++) {
        await page.waitForTimeout(120);
        expect(logout).toBe(false);
        const atEnd = await scroller.evaluate(element => {
            if (element.scrollTop + element.clientHeight >= element.scrollHeight - 2) return true;
            element.scrollTop += element.clientHeight * 0.8;
            return false;
        });
        if (atEnd) break;
    }
    await expect.poll(() => images.size).toBe(imageCount);
    await expect(grid.getByText('Unavailable', { exact: true })).toHaveCount(0);
    // Exercise the same session check that image errors request.
    const checked = page.waitForResponse(response => new URL(response.url()).pathname === '/api/auth/me');
    await page.evaluate(() => window.dispatchEvent(new Event('fridge:check-session')));
    expect((await checked).status()).toBe(200);
    const measurement = { imageCount, uniqueImages: images.size, mediaStatuses, apiFailures, logout };
    console.log('Guest scroll measurement:', JSON.stringify(measurement));
    await testInfo.attach('guest-scroll-measurement', { body: JSON.stringify(measurement, null, 2), contentType: 'application/json' });
    expect(mediaStatuses[503] || 0).toBe(0);
    expect(apiFailures).toEqual({});
    expect(logout).toBe(false);
    await expect(page).toHaveURL(`/datasets/${datasetId}`);
});

test('an initial auth/me 503 retries without redirecting the demo guest to login', async ({ page }) => {
    expect((await page.request.post('/api/auth/demo-login')).status()).toBe(200);
    let checks = 0;
    let logout = false;
    page.on('framenavigated', frame => {
        if (frame === page.mainFrame() && new URL(frame.url()).pathname === '/login') logout = true;
    });
    await page.route('**/api/auth/me', route => {
        checks += 1;
        return checks === 1 ? route.fulfill({ status: 503, json: { detail: 'Busy' } }) : route.continue();
    });
    await page.goto(`/datasets/${datasetId}`);
    await expect.poll(() => checks, { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
    await expect(page.getByTestId('grid-cell').first()).toBeVisible();
    expect(logout).toBe(false);
    await expect(page).toHaveURL(`/datasets/${datasetId}`);
});
