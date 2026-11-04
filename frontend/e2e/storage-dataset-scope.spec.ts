import { test, expect, type APIRequestContext } from '@playwright/test';
import { API, adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE } from './helpers';

test.use({ storageState: AUTH_STATE });

const REFUSED = 'Bucket not authorized for this dataset';

/** The media endpoint's answer for a sample: refused for its bucket, or something else (here the
 *  fetch itself fails, since the config points at a closed local port, never at real S3). */
async function refusedBucket(request: APIRequestContext, sampleId: number): Promise<boolean> {
    const response = await request.get(`/api/samples/${sampleId}/media`);
    if (response.status() !== 403) return false;
    return (await response.json()).detail === REFUSED;
}

test('a storage config limited to chosen datasets serves only those, and opens to all again', async ({ page }) => {
    const cookie = await adminLogin();
    const stamp = Date.now();
    const bucket = `e2e-scope-${stamp}`;
    const configName = `e2e-scope-${stamp}`;
    const [allowed, other] = await Promise.all(['allowed', 'other'].map(async kind => {
        const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-scope-${kind}-${stamp}` }) });
        const { ids } = await apiFetch(cookie, `/datasets/${ds.id}/samples`, {
            method: 'POST',
            body: JSON.stringify({ samples: [{ filepath: `s3://${bucket}/${kind}.jpg` }] }),
        });
        return { id: ds.id as number, name: ds.name as string, sampleId: ids[0] as number };
    }));
    let configId: number | undefined;
    try {
        // No config covers the bucket yet: both datasets are refused it.
        expect(await refusedBucket(page.request, allowed.sampleId)).toBe(true);
        expect(await refusedBucket(page.request, other.sampleId)).toBe(true);

        await page.goto('/settings/storage');
        await page.getByRole('button', { name: 'Add S3 Bucket' }).click();
        await page.getByPlaceholder('My S3 Bucket').fill(configName);
        await page.getByPlaceholder('my-bucket-name').fill(bucket);
        // A closed local port: a fetch through this config fails at once instead of reaching S3.
        await page.getByPlaceholder('https://minio.example.com').fill('http://127.0.0.1:9');

        const scope = page.getByTestId('storage-dataset-scope');
        await expect(scope.getByLabel('All datasets')).toBeChecked();
        await scope.getByLabel('Only these datasets').check();
        // Only a config serving every dataset can be the default.
        await expect(page.getByLabel(/Set as default storage/)).toBeDisabled();
        await expect(scope.getByText('No datasets selected')).toBeVisible();
        await scope.getByPlaceholder('Filter datasets').fill(allowed.name);
        await scope.getByLabel(allowed.name).check();
        await page.getByRole('button', { name: 'Create' }).click();

        const card = page.locator('div.rounded-xl', { hasText: configName });
        await expect(card.getByTestId('storage-config-datasets')).toHaveText(`Datasets: ${allowed.name}`);
        const created = (await apiFetch(cookie, '/storage/configs')).find((c: { name: string }) => c.name === configName);
        configId = created.id;
        expect(created).toMatchObject({ restricted: true, dataset_ids: [allowed.id] });

        // The listed dataset gets past the bucket check; the other is still refused.
        expect(await refusedBucket(page.request, allowed.sampleId)).toBe(false);
        expect(await refusedBucket(page.request, other.sampleId)).toBe(true);

        // Opening it to every dataset applies at once, and clears the list.
        await page.getByRole('button', { name: `Edit ${configName}` }).click();
        await expect(scope.getByLabel(allowed.name)).toBeChecked();
        await scope.getByLabel('All datasets').check();
        await page.getByRole('button', { name: 'Update' }).click();
        await expect(card.getByTestId('storage-config-datasets')).toHaveText('Datasets: all');
        expect(await apiFetch(cookie, `/storage/configs/${configId}`)).toMatchObject({ restricted: false, dataset_ids: [] });
        expect(await refusedBucket(page.request, other.sampleId)).toBe(false);
    } finally {
        if (configId !== undefined) {
            const deleted = await fetch(`${API}/storage/configs/${configId}`, { method: 'DELETE', headers: { Cookie: cookie } });
            expect(deleted.status).toBe(204);
        }
        await Promise.all([allowed, other].map(ds => deleteDatasetAndWait(cookie, ds.id)));
    }
});
