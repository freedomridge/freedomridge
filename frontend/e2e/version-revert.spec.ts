import { randomUUID } from 'node:crypto';
import { test, expect, type Page } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expectTotal } from './helpers';

// Dataset Settings → Versions: a revert puts the dataset back to exactly the version, so a sample added since is
// removed, and restores the dataset's settings only when asked to. It first asks, with what it deletes counted.
test.use({ storageState: AUTH_STATE });

let cookie = '';
let datasetId = 0;

const images = (from: number, count: number) => JSON.stringify({
    samples: Array.from({ length: count }, (_, i) => ({ filepath: `s3://e2e-version-revert/img_${from + i}.jpg` })),
});

test.beforeAll(async () => {
    cookie = await adminLogin();
    const created = await apiFetch(cookie, '/datasets', {
        method: 'POST', body: JSON.stringify({ name: `e2e-version-revert-${randomUUID()}`, description: 'as recorded' }),
    });
    datasetId = created.id;
    await apiFetch(cookie, `/datasets/${datasetId}/samples`, { method: 'POST', body: images(0, 3) });
    await apiFetch(cookie, `/datasets/${datasetId}/versions`, { method: 'POST', body: JSON.stringify({ name: 'before' }) });
    await apiFetch(cookie, `/datasets/${datasetId}/samples`, { method: 'POST', body: images(3, 1) });
    await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ description: 'changed since' }) });
    await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
});

test.afterAll(async () => {
    if (datasetId) await deleteDatasetAndWait(cookie, datasetId, 120_000);
});

test('a revert removes the sample added since, and restores the settings when asked', async ({ page }) => {
    const questions: string[] = [];
    page.on('dialog', dialog => {
        questions.push(dialog.message());
        void dialog.accept();
    });
    await page.goto(`/datasets/${datasetId}`);
    await expectTotal(page, 4);
    await page.getByTitle('Dataset Settings').click();
    await page.getByRole('button', { name: 'Versions', exact: true }).click();
    await page.getByLabel(/Also restore the dataset's settings/).check();
    await page.getByRole('button', { name: 'Revert to this snapshot' }).click();

    await expectTotal(page, 3);
    // The question counted what the revert deletes before asking.
    expect(questions).toHaveLength(1);
    expect(questions[0]).toContain('It deletes 1 sample and 0 public saved views added since');
    expect(questions[0]).toContain('and restores 0 deleted samples and 0 deleted saved views.');
    expect(questions[0]).toContain('Dataset settings are restored too');
    expect((await apiFetch(cookie, `/datasets/${datasetId}`)).description).toBe('as recorded');
});

async function openVersions(page: Page) {
    await page.goto(`/datasets/${datasetId}`);
    await page.getByTitle('Dataset Settings').click();
    await page.getByRole('button', { name: 'Versions', exact: true }).click();
}

test('a delete the server refuses says why', async ({ page }) => {
    const messages: string[] = [];
    page.on('dialog', dialog => {
        messages.push(dialog.message());
        void dialog.accept();
    });
    await page.route(`**/api/datasets/${datasetId}/versions/*`, route => (
        route.request().method() === 'DELETE'
            ? route.fulfill({ status: 403, json: { detail: 'Insufficient permissions' } })
            : route.continue()
    ));
    await openVersions(page);
    await page.getByRole('button', { name: 'Delete', exact: true }).first().click();

    // The question, then the refusal: it used to end silently, with the version still listed.
    await expect.poll(() => messages.length).toBe(2);
    expect(messages[1]).toBe('The version was not deleted: Insufficient permissions');
});

test('what a guest cannot do with versions is disabled and says why, and browsing stays open', async ({ browser }) => {
    // A real guest, shared at edit: a guest gets view at most, so making a version and reverting (edit) and deleting
    // one (manage) are refused, each with the level it needs.
    const username = `e2e_version_guest_${randomUUID().slice(0, 8)}`;
    const password = 'E2e-Version-Guest-Pw-2026!';
    const guest = await apiFetch(cookie, '/users', { method: 'POST', body: JSON.stringify({ username, password, role: 'guest' }) });
    const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    try {
        await apiFetch(cookie, `/datasets/${datasetId}/access`, { method: 'POST', body: JSON.stringify({ user_id: guest.id, level: 'edit' }) });
        const page = await context.newPage();
        expect((await page.request.post('/api/auth/login', { data: { username, password } })).ok()).toBeTruthy();
        expect((await page.request.post('/api/auth/change-password', {
            data: { current_password: password, new_password: `${password}-2` },
        })).ok()).toBeTruthy();
        expect((await (await page.request.get(`/api/datasets/${datasetId}`)).json()).access_level).toBe('view');
        await openVersions(page);
        await expect(page.getByTestId('grid-select-all')).toBeVisible();  // the dataset, and so the level, is read

        await expect(page.getByPlaceholder('Version name (e.g., v2.0)')).toBeDisabled();
        await expect(page.getByRole('group').filter({ hasText: 'Create Version' }))
            .toHaveAttribute('title', 'This needs edit access to the dataset');
        const remove = page.getByRole('button', { name: 'Delete', exact: true }).first();
        await expect(remove).toBeDisabled();
        await expect(remove).toHaveAttribute('title', 'This needs manage access to the dataset');
        const revert = page.getByRole('button', { name: 'Revert to this snapshot' }).first();
        await expect(revert).toBeDisabled();
        await expect(revert).toHaveAttribute('title', 'This needs edit access to the dataset');
        await expect(page.getByLabel(/Also restore the dataset's settings/)).toHaveCount(0);
        await expect(page.getByRole('link', { name: 'Browse snapshot' }).first()).toBeVisible();
    } finally {
        await context.close();
        // Their share goes with them.
        await apiFetch(cookie, `/users/${guest.id}`, { method: 'DELETE' });
    }
});
