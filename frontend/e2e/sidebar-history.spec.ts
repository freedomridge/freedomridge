import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal, expandField } from './helpers';
import { adminLogin, apiFetch, promoteField } from './api';

// sidebar, history, grid and export polish.
test.use({ storageState: AUTH_STATE });

async function addIdxStage(page: import('@playwright/test').Page, op: string, value: string) {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('idx');
    await page.locator('[data-radix-popper-content-wrapper] button:has(span:text-is("idx"))').click();
    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.locator('select').nth(1).selectOption(op);
    await editor.getByPlaceholder('Value...').fill(value);
    await editor.getByRole('button', { name: 'Apply' }).click();
}

test('Back/Forward restore the previous filters without reloading the page', async ({ page }) => {
    // Regression: every Back was a full reload that landed on the CURRENT (persisted) filters.
    await gotoDataset(page);
    await expectTotal(page, 12);
    await addIdxStage(page, '>=', '9');
    await expectTotal(page, 3);
    await page.waitForTimeout(800);  // history pushes are debounced (400 ms)
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
    await page.waitForTimeout(800);

    await page.evaluate(() => { (window as unknown as { __noReload: number }).__noReload = 1; });
    await page.goBack();
    await expectTotal(page, 3);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);
    await page.goForward();
    await expectTotal(page, 12);
    expect(await page.evaluate(() => (window as unknown as { __noReload?: number }).__noReload)).toBe(1);
});

test('an applied sidebar filter names its value on the chip', async ({ page }) => {
    // Regression: the chip said only "Filter (split)", and Apply clears the draft — the value showed nowhere.
    await gotoDataset(page);
    const sidebar = page.getByTestId('panel-filters');
    await expandField(page, 'split');
    await sidebar.locator('label', { hasText: /^train/ }).first().click();
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 6);
    await expect(page.getByTestId('query-stage-chip')).toHaveText(/split: train/);
});

test('Ctrl/Cmd-click toggles a tile instead of opening it', async ({ page }) => {
    // Regression: only Shift was inspected; Ctrl/Cmd-click opened the modal.
    await gotoDataset(page);
    const cell = page.getByTestId('grid-cell').nth(2);
    await cell.click({ modifiers: ['ControlOrMeta'] });
    await expect(page.getByTestId('grid-selection-count')).toContainText('1 selected');
    await expect(page.getByTitle('Next sample (Right arrow)')).toHaveCount(0);
    await cell.click({ modifiers: ['ControlOrMeta'] });
    await expect(page.getByTestId('grid-selection-count')).toHaveCount(0);
});

test('Export is offered with nothing filtered and exports the whole dataset', async ({ page }) => {
    // Regression: the Export button only appeared once a filter, a draft or a selection existed.
    await gotoDataset(page);
    await page.getByTitle('Export samples as CSV').click();
    await expect(page.getByText('Whole dataset')).toBeVisible();
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: /^Export/ }).last().click();
    expect((await download).suggestedFilename()).toMatch(/_12_all\.csv$/);
});

test.describe('scratch dataset', () => {
    let cookie = '';
    let dsId = 0;
    test.beforeAll(async () => {
        cookie = await adminLogin();
        dsId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-sidebar-${Date.now()}` }) })).id;
        await apiFetch(cookie, `/datasets/${dsId}/array-fields`, { method: 'POST', body: JSON.stringify({ field_name: 'categories' }) });
        // 25 distinct `place` values (> 20 → the sidebar's searchable value list), 10 containing "Forest".
        const samples = Array.from({ length: 25 }, (_, i) => ({
            filepath: `s3://e2e-sidebar/p${i}.jpg`,
            tags: ['fixture'],
            array_fields: { categories: ['example'] },
            metadata: { place: i < 10 ? `Forest_${i}` : `River_${i}`, rank: 100 - i },
        }));
        await apiFetch(cookie, `/datasets/${dsId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
        await apiFetch(cookie, `/datasets/${dsId}/refresh-stats`, { method: 'POST' });
        await promoteField(cookie, dsId, 'rank');
    });
    test.afterAll(async () => {
        if (dsId) await apiFetch(cookie, `/datasets/${dsId}`, { method: 'DELETE' });
    });

    test('the sort dropdown leaves out array fields it cannot sort by', async ({ page }) => {
        await page.goto(`/datasets/${dsId}`);
        await expectTotal(page, 25);
        const options = page.getByTestId('grid-sort').locator('option');
        await expect(options.filter({ hasText: /^rank$/ })).toHaveCount(1);
        await expect(options.filter({ hasText: /^(tags|categories)$/ })).toHaveCount(0);
    });

    test('typed text + Enter intentionally adds contains while a listed value stays exact', async ({ page }) => {
        await page.goto(`/datasets/${dsId}`);
        await expectTotal(page, 25);
        await expandField(page, 'place');
        const input = page.getByPlaceholder('+ filter by place');
        await input.fill('Forest');
        await input.press('Enter');
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 10);
        await page.getByRole('button', { name: 'Clear All' }).click();
        await expectTotal(page, 25);
        await input.fill('Forest_3');
        await page.locator('[data-searchable-dropdown]').getByText('Forest_3', { exact: true }).click();
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 1);
    });

    test('the sort choice survives a reload', async ({ page }) => {
        // Regression: a reload went back to "Default (ID)" while filters and selection were restored.
        await page.goto(`/datasets/${dsId}`);
        await expectTotal(page, 25);
        await page.getByTestId('grid-sort').selectOption('rank');
        await page.reload();
        await expectTotal(page, 25);
        await expect(page.getByTestId('grid-sort')).toHaveValue('rank');
    });
});

test('a dataset you cannot access shows one clear message', async ({ browser }) => {
    // Regression: every panel repeated "Couldn't load data: Access denied for this dataset".
    const cookie = await adminLogin();
    const ds = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-private-${Date.now()}`, is_global: false }) });
    const username = `e2e_noaccess_${Date.now()}`;
    const password = 'E2e-No-Access-Pw-2026!';
    const user = await apiFetch(cookie, '/users', { method: 'POST', body: JSON.stringify({ username, password, role: 'guest' }) });
    try {
        const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
        const page = await ctx.newPage();
        const login = await page.request.post('/api/auth/login', { data: { username, password } });
        expect(login.ok()).toBeTruthy();
        // A new account must change its password first; until then every dataset is refused for that reason.
        const changed = await page.request.post('/api/auth/change-password', {
            data: { current_password: password, new_password: `${password}-2` },
        });
        expect(changed.ok()).toBeTruthy();
        const denied = await page.request.get(`/api/datasets/${ds.id}`);
        expect(denied.status()).toBe(403);
        expect((await denied.json()).detail).toBe('Access denied for this dataset');
        // Record what the page adds, so an error toast that expires before the check still counts.
        await page.addInitScript(() => {
            const added: string[] = [];
            (window as unknown as { __added: string[] }).__added = added;
            new MutationObserver(records => records.forEach(r => r.addedNodes.forEach(n => added.push(n.textContent ?? ''))))
                .observe(document, { childList: true, subtree: true });
        });
        await page.goto(`/datasets/${ds.id}`);
        await expect(page.getByTestId('dataset-unavailable')).toHaveText(/don't have access/);
        await page.waitForTimeout(1_000);  // the panels' requests answer within ~150 ms of the dataset's
        const added = await page.evaluate(() => (window as unknown as { __added: string[] }).__added);
        expect(added.filter(text => /Access denied for this dataset/.test(text))).toEqual([]);
        await ctx.close();
    } finally {
        await apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' });
        await apiFetch(cookie, `/datasets/${ds.id}`, { method: 'DELETE' });
    }
});
