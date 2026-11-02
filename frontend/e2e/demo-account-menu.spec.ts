import { expect, test } from '@playwright/test';

const API = process.env.E2E_API_URL || 'http://localhost:8000/api';

test('demo accounts have no password-change menu entry', async ({ page }) => {
    const config = await page.request.get(`${API}/config`);
    const demoMode = (await config.json()).demo_mode;
    if (process.env.CI) expect(demoMode, 'CI must exercise demo authentication').toBe(true);
    test.skip(!demoMode, 'Requires DEMO_MODE=true');
    const login = await page.request.post(`${API}/auth/demo-login`);
    expect(login.ok()).toBeTruthy();
    await page.goto('/');
    await page.getByRole('button', { name: 'User menu' }).click();
    await expect(page.getByRole('menuitem', { name: 'Logout' })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Change Password' })).toHaveCount(0);
    await page.keyboard.press('Escape');
    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Security & Profile' })).toHaveCount(0);
});

test('ordinary accounts retain password changes', async ({ page }) => {
    const login = await page.request.post(`${API}/auth/login`, {
        data: { username: 'admin', password: 'admin123' },
    });
    expect(login.ok()).toBeTruthy();
    const me = await page.request.get(`${API}/auth/me`);
    if ((await me.json()).must_change_password) {
        const change = await page.request.post(`${API}/auth/change-password`, {
            data: { current_password: 'admin123', new_password: 'admin123' },
        });
        expect(change.ok()).toBeTruthy();
    }
    await page.goto('/');
    await page.getByRole('button', { name: 'User menu' }).click();
    await expect(page.getByRole('menuitem', { name: 'Change Password' })).toBeVisible();
    await page.keyboard.press('Escape');
    await page.goto('/settings');
    await page.getByRole('button', { name: 'Security & Profile' }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
});
