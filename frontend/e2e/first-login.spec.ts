import { expect, test } from '@playwright/test';
import { adminLogin, apiFetch } from './api';
import { fillStable } from './helpers';

test('a new user sees the home page right after the required password change', async ({ browser }) => {
    // Until the password is changed every API but auth answers 403, so the home page's dataset list
    // fails first. It must load once the password is changed, without a reload.
    const cookie = await adminLogin();
    const username = `e2e_first_login_${Date.now()}`;
    const password = 'E2e-First-Login-2026!';
    const user = await apiFetch(cookie, '/users', { method: 'POST', body: JSON.stringify({ username, password, role: 'power_user' }) });
    const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    try {
        const page = await ctx.newPage();
        expect((await page.request.post('/api/auth/login', { data: { username, password } })).ok()).toBeTruthy();
        const datasets = page.waitForResponse(r => new URL(r.url()).pathname === '/api/datasets' && r.request().method() === 'GET');
        await page.goto('/');
        expect((await datasets).status()).toBe(403);

        const dialog = page.getByRole('dialog');
        const fields = dialog.locator('input[type="password"]');
        await fillStable(fields.nth(0), password);
        await fillStable(fields.nth(1), `${password}-2`);
        await fillStable(fields.nth(2), `${password}-2`);
        await expect(dialog.getByLabel('Also revoke my API tokens')).not.toBeChecked();  // offered, unticked
        await dialog.getByRole('button', { name: 'Change Password' }).click();
        await expect(dialog).toHaveCount(0);

        await expect(page.getByText('No datasets yet').or(page.locator('a[href^="/datasets/"]')).first()).toBeVisible();
    } finally {
        await ctx.close();
        await apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' });
    }
});
