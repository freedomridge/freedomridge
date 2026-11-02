import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';

test.use({ storageState: AUTH_STATE });

for (const button of ['Create Dataset', 'New Dataset']) {
    test(`${button} opens a working creation form and refreshes the list`, async ({ page }) => {
        let created = false;
        let attempts = 0;
        const dataset = {
            id: 987654, name: 'My dataset', description: 'Test description',
            created_at: new Date().toISOString(), is_global: false,
        };
        await page.route('**/api/datasets', async route => {
            if (route.request().method() === 'POST') {
                attempts++;
                expect(route.request().postDataJSON()).toEqual({ name: dataset.name, description: dataset.description });
                if (attempts === 1) {
                    await route.fulfill({ status: 400, json: { detail: 'Dataset with this name already exists' } });
                    return;
                }
                created = true;
                await route.fulfill({ json: dataset });
            } else {
                await route.fulfill({ json: created ? [dataset] : [] });
            }
        });
        await page.route('**/api/datasets/987654/versions', route => route.fulfill({ json: [] }));
        await page.goto('/');
        await page.getByRole('button', { name: button, exact: true }).click();
        const dialog = page.getByRole('dialog');
        const submit = dialog.getByRole('button', { name: 'Create Dataset', exact: true });
        await expect(submit).toBeDisabled();
        await dialog.getByLabel('Name', { exact: true }).fill('   ');
        await expect(submit).toBeDisabled();
        await dialog.getByLabel('Name', { exact: true }).fill(' My dataset ');
        await dialog.getByLabel('Description (optional)').fill(dataset.description);
        await submit.click();
        await expect(dialog.getByRole('alert')).toHaveText('Dataset with this name already exists');
        await expect(dialog.getByLabel('Name', { exact: true })).toHaveValue(' My dataset ');
        await submit.click();
        await expect(dialog).toBeHidden();
        await expect(page.getByRole('heading', { name: dataset.name, exact: true })).toBeVisible();
        expect(attempts).toBe(2);
    });
}
