import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch } from './api';

// Settings → Compute Workers → On-demand workers (docs/ON_DEMAND_WORKERS.md): an admin saves the launcher's settings
// and asks AWS about them. This server has no AWS credentials, so AWS's answer is a refusal, shown as it is.
test.use({ storageState: AUTH_STATE });

test('an admin sets up on-demand workers and sees what AWS says about them', async ({ page }) => {
    const cookie = await adminLogin();
    const before = (await apiFetch(cookie, '/compute-launcher')).settings;
    try {
        await page.goto('/settings/workers');
        const card = page.getByTestId('on-demand-workers');
        await expect(card).toBeVisible();
        await expect(card.getByLabel('Server URL')).toHaveValue(/\/api$/);  // offered from this page's address

        // Turning it on needs a region: the server says so.
        await card.getByLabel('Start workers on demand').check();
        await card.getByLabel('AWS region').fill('');
        await card.getByRole('button', { name: 'Save' }).click();
        await expect(card.getByText('Give the AWS region and the server URL to turn it on')).toBeVisible();

        await card.getByLabel('AWS region').fill('us-east-1');
        await card.getByLabel('Machines at once').fill('2');
        await card.getByRole('button', { name: 'Save' }).click();
        await expect.poll(async () => (await apiFetch(cookie, '/compute-launcher')).settings)
            .toMatchObject({ enabled: true, region: 'us-east-1', max_machines: 2, instance_type: 'r7i.2xlarge' });
        await expect(card.getByTestId('on-demand-hours')).toContainText('of 40 machine-hours used this month');

        await card.getByRole('button', { name: 'Check with AWS' }).click();
        await expect(card.getByText(/^AWS would refuse it: /)).toBeVisible({ timeout: 30_000 });
    } finally {
        await apiFetch(cookie, '/compute-launcher', {
            method: 'PUT',
            body: JSON.stringify({ ...before, secret_access_key: null }),
        });
    }
});
