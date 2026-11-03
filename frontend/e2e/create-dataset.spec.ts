import { test, expect } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE } from './helpers';

test.use({ storageState: AUTH_STATE });

test('New Dataset creates a private dataset and shows the duplicate-name error', async ({ page }) => {
    const cookie = await adminLogin();
    const stamp = Date.now();
    const taken = await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name: `e2e-create-taken-${stamp}` }),
    });
    const name = `e2e-create-${stamp}`;
    try {
        await page.goto('/');
        await page.getByRole('button', { name: 'New Dataset', exact: true }).click();
        const dialog = page.getByRole('dialog');
        const nameInput = dialog.getByLabel('Name', { exact: true });
        const submit = dialog.getByRole('button', { name: 'Create Dataset', exact: true });
        await expect(nameInput).toBeFocused();
        await expect(submit).toBeDisabled();
        await nameInput.fill('   ');
        await expect(submit).toBeDisabled();

        await nameInput.fill(` ${taken.name} `);
        await submit.click();
        await expect(dialog.getByRole('alert')).toHaveText('Dataset with this name already exists');
        await expect(nameInput).toHaveValue(` ${taken.name} `);

        await nameInput.fill(` ${name} `);
        await dialog.getByLabel('Description (optional)').fill('Created by the e2e suite');
        await submit.click();
        await expect(dialog).toBeHidden();
        await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();

        const datasets: Array<{ name: string; description: string | null; is_global: boolean }> =
            await apiFetch(cookie, '/datasets');
        expect(datasets.find(dataset => dataset.name === name))
            .toMatchObject({ description: 'Created by the e2e suite', is_global: false });
    } finally {
        const datasets: Array<{ id: number; name: string }> = await apiFetch(cookie, '/datasets');
        const created = datasets.find(dataset => dataset.name === name);
        if (created) await deleteDatasetAndWait(cookie, created.id);
        await deleteDatasetAndWait(cookie, taken.id);
    }
});

test('the empty-state Create Dataset button opens the same form', async ({ page }) => {
    // The suite's fixture dataset always exists, so stub an empty list to reach the empty state.
    await page.route('**/api/datasets', route => route.fulfill({ json: [] }));
    await page.goto('/');
    await page.getByRole('button', { name: 'Create Dataset', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByRole('heading', { name: 'Create Dataset', exact: true })).toBeVisible();
    await expect(dialog.getByLabel('Name', { exact: true })).toBeFocused();
});
