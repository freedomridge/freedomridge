import { test, expect, type Browser } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch } from './api';

// An admin resets a user's password in the Edit User dialog. Its one button says when it will reset the typed
// password, and pressing it (or Enter in the field) resets it: there used to be a separate Reset next to the field,
// and Save Changes closed the dialog without the password, so the user kept the old one.
test.use({ storageState: AUTH_STATE });

const OLD = 'E2e-Old-Pw-2026!';
const NEW = 'E2e-New-Pw-2026!';

async function signIn(browser: Browser, username: string, password: string): Promise<number> {
    const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    try {
        return (await context.request.post('/api/auth/login', { data: { username, password } })).status();
    } finally {
        await context.close();
    }
}

async function withUser(run: (username: string, id: number, cookie: string) => Promise<void>) {
    const cookie = await adminLogin();
    const username = `e2e_reset_${Date.now()}_${Math.floor(Math.random() * 1e6)}`;
    const user = await apiFetch(cookie, '/users', { method: 'POST', body: JSON.stringify({ username, password: OLD, role: 'guest' }) });
    try {
        await run(username, user.id, cookie);
    } finally {
        await apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' });
    }
}

test('the dialog resets a typed password with its one button', async ({ page, browser }) => {
    await withUser(async (username) => {
        await page.goto('/users');
        await page.getByRole('row').filter({ hasText: username }).getByTitle('Edit User').click();
        const save = page.getByTestId('edit-user-save');
        await expect(save).toHaveText('Save Changes');
        await expect(page.getByRole('button', { name: 'Reset', exact: true })).toHaveCount(0);

        await page.getByLabel('New password').fill(NEW);
        await expect(save).toHaveText('Reset Password');
        await save.click();
        await expect(page.getByText('Password reset. They are signed out; they had no API tokens.')).toBeVisible();

        expect(await signIn(browser, username, OLD)).toBe(401);
        expect(await signIn(browser, username, NEW)).toBe(200);
    });
});

test('Enter in the password field saves the other changes and resets the password', async ({ page, browser }) => {
    await withUser(async (username, id, cookie) => {
        await page.goto('/users');
        await page.getByRole('row').filter({ hasText: username }).getByTitle('Edit User').click();
        await page.getByRole('dialog').getByRole('combobox').selectOption('power_user');
        await page.getByLabel('New password').fill(NEW);
        await expect(page.getByTestId('edit-user-save')).toHaveText('Save and Reset Password');
        await page.getByLabel('New password').press('Enter');
        await expect(page.getByText('Password reset. They are signed out; they had no API tokens.')).toBeVisible();

        const users = await apiFetch(cookie, '/users') as { id: number; role: string }[];
        expect(users.find(user => user.id === id)?.role).toBe('power_user');
        expect(await signIn(browser, username, NEW)).toBe(200);
    });
});
