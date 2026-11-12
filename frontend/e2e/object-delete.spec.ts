import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// Deleting an image deletes its object samples (migration 0030). The web app has no sample delete: the API's bulk
// delete is how one is made, and the dataset page shows the result: the Objects grid without the image's objects,
// and job history counting them apart from the images the job was asked to delete.
test.use({ storageState: AUTH_STATE });

type Sample = { id: number; metadata: Record<string, unknown> };

async function objectDataset(cookie: string, name: string): Promise<Record<string, number>> {
    const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name, is_global: true }) });
    const images = ['corn', 'soy', 'wheat'].map(crop => ({ filepath: `s3://${name}/${crop}.jpg`, metadata: { crop } }));
    await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples: images }) });
    const found = await apiFetch(cookie, `/datasets/${ds.id}/samples/query`, {
        method: 'POST',
        body: JSON.stringify({ filters: { logic: 'AND', filters: [] }, limit: 10, offset: 0 }),
    });
    const image = Object.fromEntries(found.items.map((s: Sample) => [s.metadata.crop, s.id]));
    const object = (key: string, parent: number) => ({
        filepath: `s3://${name}/${key}`, media_type: 'object', metadata: { parent_id: parent, label: 'weed' },
    });
    const objects = [
        object('corn.jpg#0', image.corn), object('corn.jpg#1', image.corn),
        object('soy.jpg#0', image.soy), object('wheat.jpg#0', image.wheat),
    ];
    await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples: objects }) });
    return { id: ds.id, ...image };
}

async function objectCount(page: Page, text: string) {
    await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();
    await expect(page.getByTestId('grid-view-count')).toHaveText(new RegExp(`^${text}$`), { timeout: 20_000 });
}

test('deleting images deletes their objects: the Objects grid loses them and job history counts them', async ({ page }) => {
    test.setTimeout(90_000);
    const cookie = await adminLogin();
    const dataset = await objectDataset(cookie, `e2e-object-delete-${Date.now()}-${Math.floor(Math.random() * 1e6)}`);
    try {
        await page.goto(`/datasets/${dataset.id}`);
        await expect(page.getByTestId('grid-view-count')).toHaveText(/^3 images$/, { timeout: 20_000 });
        await objectCount(page, '4 objects');

        const submitted = await apiFetch(cookie, `/datasets/${dataset.id}/samples/bulk-delete`, {
            method: 'POST', body: JSON.stringify({ sample_ids: [dataset.corn, dataset.soy] }),
        });
        let job = await apiFetch(cookie, `/jobs/${submitted.job_id}`);
        for (const deadline = Date.now() + 30_000; job.state !== 'succeeded' && Date.now() < deadline;) {
            expect(['queued', 'running']).toContain(job.state);
            await new Promise(resolve => setTimeout(resolve, 250));
            job = await apiFetch(cookie, `/jobs/${submitted.job_id}`);
        }
        expect(job.state).toBe('succeeded');
        expect(job.result).toMatchObject({ deleted_count: 2, objects_deleted: 3 });

        await page.reload();
        await expect(page.getByTestId('grid-view-count')).toHaveText(/^(1 image|1 object)$/, { timeout: 20_000 });
        await objectCount(page, '1 object');  // wheat's
        await page.getByTestId('open-job-history').click();
        await expect(page.getByTestId('job-history-dialog')).toContainText(
            '2 samples deleted, and 3 objects with their images.',
        );
    } finally {
        await deleteDatasetAndWait(cookie, dataset.id);
    }
});
