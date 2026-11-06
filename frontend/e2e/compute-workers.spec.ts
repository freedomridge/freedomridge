import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { API, adminLogin, apiFetch } from './api';

// Settings → Compute Workers: a machine running python -m fridge.worker with a worker token shows here, and
// API Tokens makes the worker token. The worker is registered through the real API, as the SDK does it.
test.use({ storageState: AUTH_STATE });

test('a registered worker shows on the Compute Workers page', async ({ page }) => {
    const cookie = await adminLogin();
    const name = `e2e-worker-${Date.now()}`;
    const made = await apiFetch(cookie, '/tokens', { method: 'POST', body: JSON.stringify({ name, scope: 'worker' }) });
    try {
        const registered = await fetch(`${API}/workers/register`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${made.token}` },
            body: JSON.stringify({
                name,
                hostname: 'e2e-host',
                sdk_version: '9.9.9',
                kinds: ['map_refit'],
                capabilities: { memory_bytes: 32 * 1024 ** 3, cpus: 8, gpu: false },
            }),
        });
        expect(registered.status).toBe(200);

        await page.goto('/settings');
        await page.getByTestId('settings-workers-card').click();
        await expect(page).toHaveURL(/\/settings\/workers$/);
        const row = page.getByTestId('worker-row').filter({ hasText: name });
        await expect(row).toBeVisible();
        await expect(row.getByTestId('worker-status')).toHaveText('ONLINE');
        await expect(row).toContainText('Compute worker');
        await expect(row).toContainText('e2e-host · owner admin · 32 GB RAM, 8 CPUs · SDK 9.9.9');
        await expect(row).toContainText('Takes: map_refit');
        await expect(page.getByTestId('worker-command')).toContainText('python -m fridge.worker');
    } finally {
        await apiFetch(cookie, `/tokens/${made.id}`, { method: 'DELETE' });
    }
});

test('API Tokens makes a worker token and shows how to run the worker', async ({ page }) => {
    const cookie = await adminLogin();
    const name = `e2e-worker-token-${Date.now()}`;
    await page.goto('/settings/tokens');
    await page.getByRole('button', { name: 'Create New Token' }).click();
    await page.getByPlaceholder(/my-laptop/).fill(name);
    await page.getByTestId('token-scope').selectOption('worker');
    await page.getByRole('button', { name: 'Create Token' }).click();
    const command = page.getByTestId('worker-token-command');
    await expect(command).toContainText('FRIDGE_API_TOKEN=');
    await expect(command).toContainText('python -m fridge.worker');
    const tokens: Array<{ id: number; name: string; scope: string }> = await apiFetch(cookie, '/tokens');
    const made = tokens.find(token => token.name === name);
    try {
        expect(made?.scope).toBe('worker');
        await expect(page.locator('h3', { hasText: name }).getByTestId('token-worker-badge')).toHaveText('Worker');
    } finally {
        if (made) await apiFetch(cookie, `/tokens/${made.id}`, { method: 'DELETE' });
    }
});
