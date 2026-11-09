import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { API, adminLogin, apiFetch } from './api';

// Settings → Followers (docs/FOLLOWERS.md): an admin adds a follower and sees its token once, follows a dataset
// and switches its tags to both ways, then removes the follower, whose token then stops working. The token itself
// reaches the follower endpoints and nothing else.
test.use({ storageState: AUTH_STATE });

test('an admin adds a follower, chooses what it follows, and removes it', async ({ page }) => {
    const cookie = await adminLogin();
    const stamp = Date.now();
    const dataset = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-followed-${stamp}` }) });
    const name = `e2e-follower-${stamp}`;
    try {
        await page.goto('/settings');
        await page.getByTestId('settings-followers-card').click();
        await expect(page).toHaveURL(/\/settings\/followers$/);

        await page.getByLabel('Follower name').fill(name);
        await page.getByRole('button', { name: 'Add', exact: true }).click();
        const shown = page.getByTestId('follower-token');
        await expect(shown).toContainText(`${name}'s token is shown only now`);
        const token = (await shown.locator('code').innerText()).split(' ').pop()!;
        const asFollower = (path: string) => fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${token}` } });
        expect((await asFollower('/follower/config')).status).toBe(200);
        expect((await asFollower('/datasets')).status).toBe(403);

        // The page refreshes every 30 s; reload to see the check-in the config call above made.
        await page.reload();
        const row = page.getByTestId('follower-row').filter({ hasText: name });
        await expect(row.getByTestId('follower-seen')).toContainText('Last checked in');
        await row.getByLabel('Dataset to follow').selectOption(String(dataset.id));
        await row.getByRole('button', { name: 'Follow', exact: true }).click();
        const followed = row.getByTestId('followed-dataset').filter({ hasText: dataset.name });
        await expect(followed).toBeVisible();
        await followed.getByLabel('Tags').selectOption('both');
        await expect.poll(async () => {
            const listed = await apiFetch(cookie, '/followers');
            return listed.find((entry: { name: string }) => entry.name === name)?.datasets[0]?.tags;
        }).toBe('both');
        // Serve images: on, and tags keep the value just set (each change sends every option).
        // The box shows the saved option: it's ticked once the server has it.
        const serve = followed.getByLabel('Serve images');
        await serve.click();
        await expect(serve).toBeChecked();
        await expect.poll(async () => {
            const listed = await apiFetch(cookie, '/followers');
            const entry = listed.find((item: { name: string }) => item.name === name)?.datasets[0];
            return entry && [entry.tags, entry.images];
        }).toEqual(['both', true]);
        const config = await (await asFollower('/follower/config')).json();
        expect(config.datasets.map((entry: { dataset_id: number; tags: string; images: boolean }) =>
            [entry.dataset_id, entry.tags, entry.images])).toEqual([[dataset.id, 'both', true]]);

        await row.getByRole('button', { name: 'Remove follower…' }).click();
        await row.getByRole('button', { name: 'Remove', exact: true }).click();
        await expect(page.getByTestId('follower-row').filter({ hasText: name })).toHaveCount(0);
        expect((await asFollower('/follower/config')).status).toBe(401);
    } finally {
        const listed = await apiFetch(cookie, '/followers');
        const left = listed.find((entry: { name: string }) => entry.name === name);
        if (left) await apiFetch(cookie, `/followers/${left.id}`, { method: 'DELETE' });
        await apiFetch(cookie, `/datasets/${dataset.id}`, { method: 'DELETE' });
    }
});
