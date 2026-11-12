import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset } from './api';

// "Update this view" (Saved Views): the filters opened from a saved view (Saved Views, the default view, a ?view=
// link) can replace that view's, once they differ from it, by its owner or an admin, after a confirm naming it. Its
// name, link and sharing stay. Anyone else, or a mirror's copy of the primary's view, is offered only Save as new.
test.use({ storageState: AUTH_STATE });

const AT_LEAST_2 = { field: 'idx', operator: '>=', value: 2 };
const UNDER_8 = { field: 'idx', operator: '<', value: 8 };
let cookie = '';
let datasetId = 0;

test.beforeAll(async () => {
    cookie = await adminLogin();
    // Its own copy of the fixture, open to every user (the second test signs in as another one).
    datasetId = await createFixtureDataset(cookie, `e2e-update-view-${Date.now()}`, true);
});
test.afterAll(async () => {
    if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
});

// A public view of idx 2 to 7: 6 of the fixture's 12 samples.
async function makeView(name: string): Promise<{ id: number; slug: string; name: string }> {
    return apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({
            name,
            is_public: true,
            content: {
                filters: { logic: 'AND', filters: [AT_LEAST_2, UNDER_8] },
                viewStages: [
                    { type: 'filter', description: 'idx ≥ 2', filter: AT_LEAST_2 },
                    { type: 'filter', description: 'idx < 8', filter: UNDER_8 },
                ],
            },
        }),
    });
}

async function removeLastChip(page: Page) {
    const chip = page.getByTestId('query-stage-chip').last();
    await chip.hover();
    await chip.getByRole('button').click();
}

test('Update this view replaces a saved view\'s filters after a confirm naming it; its link and sharing stay', async ({ page }) => {
    const view = await makeView(`e2e-update-${Date.now()}`);
    await page.goto(`/datasets/${datasetId}?view=${view.slug}`);
    await expectTotal(page, 6);
    // The filters are the view's: nothing to update.
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await expect(page.getByText(view.name)).toBeVisible();
    await expect(page.getByTestId('view-source')).toHaveCount(0);
    await page.getByRole('button', { name: 'Close Views' }).click();

    await removeLastChip(page);
    await expectTotal(page, 10);
    // Kept with the filters across a reload, once they are saved in this browser (debounced).
    await expect.poll(() => page.evaluate(key => {
        const saved = JSON.parse(localStorage.getItem(key) ?? '{}');
        return [saved.viewStages?.length, saved.sourceView?.name];
    }, `fridge-filters-${datasetId}`)).toEqual([1, view.name]);
    await page.reload();
    await expectTotal(page, 10);
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await expect(page.getByTestId('view-source')).toContainText(`“${view.name}”`);
    const confirm = new Promise<string>(resolve => page.once('dialog', dialog => {
        resolve(dialog.message());
        void dialog.accept();
    }));
    const saved = page.waitForResponse(r => r.url().endsWith(`/api/datasets/${datasetId}/views/${view.id}/content`) && r.request().method() === 'PATCH');
    await page.getByTestId('view-update').click();
    expect(await confirm).toContain(`“${view.name}”`);
    expect((await saved).status()).toBe(200);
    await expect(page.getByTestId('view-source')).toHaveCount(0);

    const updated = await apiFetch(cookie, `/views/${view.slug}`);
    expect(updated.content.viewStages.map((stage: { description: string }) => stage.description)).toEqual(['idx ≥ 2']);
    expect([updated.id, updated.name, updated.slug, updated.is_public]).toEqual([view.id, view.name, view.slug, true]);
    expect(updated.updated_at).toBeTruthy();
    // Its link opens on the new filters.
    await page.goto(`/datasets/${datasetId}?view=${view.slug}`);
    await expectTotal(page, 10);
});

test('a view shared by someone else offers only Save as new; the new view is the viewer\'s to update', async ({ browser }) => {
    test.setTimeout(90_000);
    const view = await makeView(`e2e-not-mine-${Date.now()}`);
    const username = `e2e_update_view_${Date.now()}`;
    const password = 'E2e-Update-View-Pw-2026!';
    const user = await apiFetch(cookie, '/users', { method: 'POST', body: JSON.stringify({ username, password, role: 'power_user' }) });
    const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    try {
        const page = await ctx.newPage();
        expect((await page.request.post('/api/auth/login', { data: { username, password } })).ok()).toBeTruthy();
        expect((await page.request.post('/api/auth/change-password', {
            data: { current_password: password, new_password: `${password}-2` },
        })).ok()).toBeTruthy();

        await page.goto(`/datasets/${datasetId}?view=${view.slug}`);
        await expectTotal(page, 6);
        await removeLastChip(page);
        await expectTotal(page, 10);
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await expect(page.getByTestId('view-source-read-only')).toContainText(`“${view.name}”`);
        await expect(page.getByTestId('view-source-read-only')).toContainText("isn't yours to change");
        await expect(page.getByTestId('view-update')).toHaveCount(0);
        // Nor does the server take it.
        const refused = await page.request.patch(`/api/datasets/${datasetId}/views/${view.id}/content`, { data: { content: { filters: {} } } });
        expect(refused.status()).toBe(403);

        // Saved as new, the filters are that view's, which is theirs.
        const mine = `e2e-mine-${Date.now()}`;
        await page.getByPlaceholder('Enter view name...').fill(mine);
        await page.getByRole('button', { name: 'Save View' }).click();
        await expect(page.getByRole('button', { name: new RegExp(mine) })).toBeVisible();
        await expect(page.getByTestId('view-source')).toHaveCount(0);
        await page.getByRole('button', { name: 'Close Views' }).click();
        await removeLastChip(page);
        await expectTotal(page, 12);
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await expect(page.getByTestId('view-source')).toContainText(`“${mine}”`);
        await expect(page.getByTestId('view-update')).toBeVisible();
        expect((await apiFetch(cookie, `/views/${view.slug}`)).content.viewStages).toHaveLength(2);  // theirs unchanged
    } finally {
        await ctx.close();
        await apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' });
    }
});

test('an update from filters opened before someone else changed the view is refused; the view can be opened again', async ({ page }) => {
    const view = await makeView(`e2e-changed-${Date.now()}`);
    await page.goto(`/datasets/${datasetId}?view=${view.slug}`);
    await expectTotal(page, 6);
    await removeLastChip(page);
    await expectTotal(page, 10);
    // Meanwhile its filters are replaced elsewhere (the API without the value it read: the last writer wins).
    const theirs = { filters: { logic: 'AND', filters: [UNDER_8] }, viewStages: [{ type: 'filter', description: 'idx < 8', filter: UNDER_8 }] };
    await apiFetch(cookie, `/datasets/${datasetId}/views/${view.id}/content`, { method: 'PATCH', body: JSON.stringify({ content: theirs }) });

    await page.getByRole('button', { name: 'Saved Views' }).click();
    page.once('dialog', dialog => void dialog.accept());
    const refused = page.waitForResponse(r => r.url().endsWith(`/views/${view.id}/content`) && r.request().method() === 'PATCH');
    await page.getByTestId('view-update').click();
    expect((await refused).status()).toBe(409);
    await expect(page.getByTestId('view-update-conflict')).toContainText('changed since you opened it');
    await expect(page.getByTestId('view-update')).toHaveCount(0);
    expect((await apiFetch(cookie, `/views/${view.slug}`)).content.viewStages.map((s: { description: string }) => s.description)).toEqual(['idx < 8']);
    // Save as a new view goes to the name; Open it again applies the view as it is now.
    await page.getByTestId('view-save-as-new').click();
    await expect(page.getByPlaceholder('Enter view name...')).toBeFocused();
    await page.getByTestId('view-reopen').click();
    await expectTotal(page, 8);
    await expect(page.getByTestId('view-source')).toHaveCount(0);
});
