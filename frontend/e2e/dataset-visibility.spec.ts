import { test, expect, type Page } from '@playwright/test';
import { adminLogin, apiFetch } from './api';
import { AUTH_STATE } from './helpers';

test.use({ storageState: AUTH_STATE });

async function setVisibility(page: Page, datasetId: number, value: 'private' | 'everyone') {
    await page.goto(`/datasets/${datasetId}`);
    await page.getByRole('button', { name: 'Dataset Settings', exact: true }).click();
    const dialog = page.getByRole('dialog');
    const visibility = dialog.getByLabel('Visibility');
    await expect(visibility).toHaveValue(value === 'everyone' ? 'private' : 'everyone');
    await visibility.selectOption(value);
    await dialog.getByRole('button', { name: 'Save Changes' }).click();
    await expect(dialog).toBeHidden();
}

test('a new dataset is private until an admin opens it to everyone in Dataset Settings', async ({ page, browser }) => {
    const cookie = await adminLogin();
    const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-visibility-${Date.now()}` }) });
    expect(ds.is_global).toBe(false);
    const username = `e2e_visibility_${Date.now()}`;
    const password = 'E2e-Visibility-Pw-2026!';
    const user = await apiFetch(cookie, '/users', { method: 'POST', body: JSON.stringify({ username, password, role: 'guest' }) });
    const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    try {
        const guest = await ctx.newPage();
        expect((await guest.request.post('/api/auth/login', { data: { username, password } })).ok()).toBeTruthy();
        // A new account must change its password before it can read anything.
        const changed = await guest.request.post('/api/auth/change-password', {
            data: { current_password: password, new_password: `${password}-2` },
        });
        expect(changed.ok()).toBeTruthy();
        const denied = await guest.request.get(`/api/datasets/${ds.id}`);
        expect(denied.status()).toBe(403);
        expect((await denied.json()).detail).toBe('Access denied for this dataset');

        await setVisibility(page, ds.id, 'everyone');
        expect((await apiFetch(cookie, `/datasets/${ds.id}`)).is_global).toBe(true);
        expect((await guest.request.get(`/api/datasets/${ds.id}`)).status()).toBe(200);

        await setVisibility(page, ds.id, 'private');
        expect((await apiFetch(cookie, `/datasets/${ds.id}`)).is_global).toBe(false);
        expect((await guest.request.get(`/api/datasets/${ds.id}`)).status()).toBe(403);
    } finally {
        await ctx.close();
        await apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' });
        await apiFetch(cookie, `/datasets/${ds.id}`, { method: 'DELETE' });
    }
});
