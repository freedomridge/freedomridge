import { test, expect } from '@playwright/test';
import { adminLogin, apiFetch } from './api';
import { AUTH_STATE, expectTotal, expandField } from './helpers';

test.use({ storageState: AUTH_STATE });

test('new PATCH fields appear without refresh and tags beyond the preview remain searchable', async ({ page }) => {
    const cookie = await adminLogin();
    const dataset = await apiFetch(cookie, '/datasets', {
        method: 'POST', body: JSON.stringify({ name: `e2e-registry-${Date.now()}` }),
    });
    try {
        const { ids } = await apiFetch(cookie, `/datasets/${dataset.id}/samples`, {
            method: 'POST',
            body: JSON.stringify({ samples: Array.from({ length: 250 }, (_, index) => ({
                filepath: `s3://registry/${index}.jpg`,
                tags: [`shared-prefix-${String(index).padStart(3, '0')}`],
                metadata: { serial: `serial-${index}` },
            })) }),
        });
        await apiFetch(cookie, `/datasets/${dataset.id}/samples/${ids[0]}`, {
            method: 'PATCH', body: JSON.stringify({ metadata: { new_measurement: 123 } }),
        });
        const schema = await apiFetch(cookie, `/datasets/${dataset.id}/schema`);
        expect(schema.find((field: { name: string }) => field.name === 'new_measurement')).toMatchObject({
            type: 'number', declared_type: 'number',
        });
        await page.goto(`/datasets/${dataset.id}`);
        await expectTotal(page, 250);
        // The runner may finish measurement before the page loads; either state shows the field.
        // Match the sidebar's field label (div/span/label), not <option>s of other dropdowns listing the field.
        await expect(page.locator(':is(div,span,label):text-is("new_measurement")').first()).toBeVisible();
        await expandField(page, 'tags');
        const tagInput = page.getByPlaceholder('+ filter by tags');
        await tagInput.fill('shared-prefix-24');
        const dropdown = page.locator('[data-searchable-dropdown]');
        await expect(dropdown.getByText('shared-prefix-249', { exact: true })).toBeVisible();
    } finally {
        await apiFetch(cookie, `/datasets/${dataset.id}`, { method: 'DELETE' });
    }
});

test('an incomplete preview never hides matching server results', async ({ page }) => {
    const cookie = await adminLogin();
    const dataset = await apiFetch(cookie, '/datasets', {
        method: 'POST', body: JSON.stringify({ name: `e2e-preview-${Date.now()}` }),
    });
    try {
        await apiFetch(cookie, `/datasets/${dataset.id}/samples`, {
            method: 'POST', body: JSON.stringify({ samples: [
                { filepath: 's3://preview/a.jpg', metadata: { serial: 'shared-cached' } },
                { filepath: 's3://preview/b.jpg', metadata: { serial: 'shared-beyond-preview' } },
            ] }),
        });
        await page.route(`**/api/datasets/${dataset.id}/field-stats**`, async route => {
            const response = await route.fetch();
            const fields = await response.json();
            await route.fulfill({ json: fields.map((field: { name: string }) => field.name === 'serial'
                ? { ...field, type: 'string', values: ['shared-cached'], values_complete: false }
                : field) });
        });
        await page.goto(`/datasets/${dataset.id}`);
        await expectTotal(page, 2);
        await expandField(page, 'serial');
        await page.getByPlaceholder('Filter by serial...').fill('shared');
        await expect(page.locator('[data-searchable-dropdown]').getByText('shared-beyond-preview', { exact: true })).toBeVisible();
    } finally {
        await apiFetch(cookie, `/datasets/${dataset.id}`, { method: 'DELETE' });
    }
});
