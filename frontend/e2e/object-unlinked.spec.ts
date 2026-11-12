import { test, expect } from '@playwright/test';
import { AUTH_STATE, fieldToggle } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// Objects with no image (backend/unlinked_objects.py). In the Objects grid, "N objects have no image" opens them as a
// view, and an admin deletes those whose image is gone (a bulk delete job), the dialog naming what stays. parent_id
// isn't listed in the sidebar.
test.use({ storageState: AUTH_STATE });

type Sample = { id: number; metadata: Record<string, unknown> };

async function objectDataset(cookie: string, name: string): Promise<number> {
    const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name, is_global: true }) });
    await apiFetch(cookie, `/datasets/${ds.id}/samples`, {
        method: 'POST', body: JSON.stringify({ samples: [{ filepath: `s3://${name}/corn.jpg`, metadata: { crop: 'corn' } }] }),
    });
    const found = await apiFetch(cookie, `/datasets/${ds.id}/samples/query`, {
        method: 'POST',
        body: JSON.stringify({ filters: { logic: 'AND', filters: [] }, limit: 10, offset: 0 }),
    });
    const image = (found.items as Sample[])[0].id;
    const object = (key: string, metadata: Record<string, unknown>) => ({
        filepath: `s3://${name}/${key}`, media_type: 'object', metadata: { label: 'weed', ...metadata },
    });
    const objects = [
        object('corn.jpg#0', { parent_id: image }),
        object('lost#0', { parent_id: 999999999 }),  // names no sample: its image is gone
        object('junk#0', { parent_id: 'not an id' }),
    ];
    await apiFetch(cookie, `/datasets/${ds.id}/samples`, { method: 'POST', body: JSON.stringify({ samples: objects }) });
    return ds.id;
}

test('the Objects grid names the objects with no image, shows them, and deletes those whose image is gone', async ({ page }) => {
    test.setTimeout(90_000);
    const cookie = await adminLogin();
    const id = await objectDataset(cookie, `e2e-object-unlinked-${Date.now()}-${Math.floor(Math.random() * 1e6)}`);
    try {
        await page.goto(`/datasets/${id}`);
        const count = page.getByTestId('grid-view-count');
        await expect(count).toHaveText(/^1 image$/, { timeout: 20_000 });
        await expect(page.getByTestId('grid-unlinked-objects')).toHaveCount(0);  // image grain: not shown
        await expect(fieldToggle(page, 'label')).toBeVisible({ timeout: 20_000 });
        await expect(fieldToggle(page, 'parent_id')).toHaveCount(0);

        await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();
        await expect(count).toHaveText(/^3 objects$/, { timeout: 20_000 });
        const warning = page.getByTestId('grid-unlinked-objects');
        await expect(warning).toHaveText('⚠ 2 objects have no image', { timeout: 20_000 });
        await warning.click();
        await expect(count).toHaveText(/^2 objects$/, { timeout: 20_000 });

        let message = '';
        page.once('dialog', dialog => { message = dialog.message(); void dialog.accept(); });
        await page.getByTestId('grid-unlinked-objects-delete').click();
        await expect.poll(() => message).toContain('Delete the 1 object whose image is gone?');
        expect(message).toContain('1 other object with no image stays (1 with a parent_id that is not a sample id).');
        await expect.poll(async () => {
            const found = await apiFetch(cookie, `/datasets/${id}/objects/unlinked`, { method: 'POST' });
            return [found.kinds.image_gone, found.total];
        }, { timeout: 30_000 }).toEqual([0, 1]);

        await page.reload();
        await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();
        await expect(page.getByTestId('grid-unlinked-objects')).toHaveText('⚠ 1 object has no image', { timeout: 20_000 });
        await expect(page.getByTestId('grid-unlinked-objects-delete')).toHaveCount(0);  // none whose image is gone
    } finally {
        await deleteDatasetAndWait(cookie, id);
    }
});
