import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, bigSelectAll, expectTotal, gotoDataset } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset, readState } from './api';

// An applied selection that can't be kept across a reload or Back: a big Select All (a server token, kept 2 hours)
// and an id list over 50,000. Its chip used to vanish and the grid show more samples, with no word why. It stays,
// greyed and marked expired, applying nothing, until it is removed; nothing sends it to the server, a saved view
// or a link. Until the page reloads or history is restored, the live chip still filters.
test.use({ storageState: AUTH_STATE, permissions: ['clipboard-read', 'clipboard-write'] });

const TOTAL = 12;  // the shared fixture
const chips = (page: Page) => page.getByTestId('query-stage-chip');
const expired = (page: Page) => page.getByTestId('expired-selection-chip');
const storageKey = (datasetId = readState().datasetId) => `fridge-filters-${datasetId}`;
const saved = (page: Page, datasetId?: number) => page.evaluate((key) => localStorage.getItem(key) ?? '', storageKey(datasetId));

// Every API request from now on, its address and body, to check none carries the expired selection.
function recordRequests(page: Page): string[] {
    const sent: string[] = [];
    page.on('request', (request) => {
        if (new URL(request.url()).pathname.startsWith('/api/')) sent.push(`${request.url()} ${request.postData() ?? ''}`);
    });
    return sent;
}

const carrying = (sent: string[], ...marks: string[]) => sent.filter((line) => marks.some((mark) => line.includes(mark)));

// The Back tests add an idx stage, then go Back to the state without it. A request that carries the idx stage is for
// the state before Back (its selection still live), sent late: not one of those the test is about.
const decoded = (line: string) => { try { return decodeURIComponent(line); } catch { return line; } };  // a body, or a query string
const forStateBeforeBack = (line: string) => ['"field":"idx"', '"operator":">="'].every((part) => decoded(line).includes(part));
const afterBack = (sent: string[]) => sent.filter((line) => !forStateBeforeBack(line));

async function addIdxStage(page: Page, op: string, value: string) {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('idx');
    await page.locator('[data-radix-popper-content-wrapper] button:has(span:text-is("idx"))').click();
    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.locator('select').nth(1).selectOption(op);
    await editor.getByPlaceholder('Value...').fill(value);
    await editor.getByRole('button', { name: 'Apply' }).click();
}

async function expectExpired(page: Page, description: string, liveChips = 0) {
    await expect(expired(page)).toHaveCount(1);
    await expect(expired(page)).toContainText(description);
    await expect(expired(page)).toContainText('expired');
    await expect(expired(page)).toHaveAttribute('title', /couldn't be kept across a reload.*Select the samples again/);
    await expect(chips(page)).toHaveCount(liveChips);
    await expect(page.locator('[title="Click to toggle include/exclude"]')).toHaveCount(0);
}

async function removeExpired(page: Page) {
    await expired(page).hover();
    await expired(page).locator('button').click();
    await expect(expired(page)).toHaveCount(0);
}

// A History entry is written 400 ms after a change: wait for the one holding `stages` applied stages.
async function historyHolds(page: Page, stages: number, expiredChips: number) {
    await expect.poll(() => page.evaluate(() => [
        window.history.state?.viewStages?.length ?? -1,
        window.history.state?.expiredSelections?.length ?? -1,
    ])).toEqual([stages, expiredChips]);
}

test('a big Select All chip stays expired after a reload: it filters nothing, copies no link, and can be removed', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await bigSelectAll(page);
    await page.getByTestId('apply-filters').click();
    await expect(chips(page)).toHaveCount(1);
    const description = (await chips(page).first().textContent())!.trim();
    await expectTotal(page, 0);  // live until the reload: the faked token selects nothing on the server
    await expect(expired(page)).toHaveCount(0);
    await expect.poll(() => saved(page)).toContain('"expired":true');  // what is saved is its chip, expired
    expect(await saved(page)).not.toContain('e2e-expired-token');
    await historyHolds(page, 0, 1);  // and so does the entry a reload reopens

    const sent = recordRequests(page);
    await page.reload();
    await expectTotal(page, TOTAL);
    await expectExpired(page, description);
    await expect(page.getByTestId('no-filters')).toHaveCount(0);

    await page.evaluate(() => navigator.clipboard.writeText('nothing copied'));
    await page.getByTestId('copy-link').click();
    await expect(page.getByText(`Can't copy a link: “${description}” is an expired selection. Select the samples again, or remove it.`)).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('nothing copied');

    await removeExpired(page);
    await expect(page.getByTestId('no-filters')).toBeVisible();
    await expectTotal(page, TOTAL);
    expect(carrying(sent, 'e2e-expired-token', 'in_selection', '"expired"', description)).toEqual([]);
    await expect.poll(() => saved(page)).not.toContain('"expired":true');
    await historyHolds(page, 0, 0);
    await page.reload();
    await expectTotal(page, TOTAL);
    await expect(page.getByTestId('no-filters')).toBeVisible();
    await expect(expired(page)).toHaveCount(0);
});

test('a big Select All chip stays expired after Back', async ({ page }) => {
    await gotoDataset(page);
    await bigSelectAll(page);
    await page.getByTestId('apply-filters').click();
    await expect(chips(page)).toHaveCount(1);
    const description = (await chips(page).first().textContent())!.trim();
    await expectTotal(page, 0);
    await historyHolds(page, 0, 1);  // the entry for this state holds the chip expired
    await addIdxStage(page, '>=', '9');
    await expect(chips(page)).toHaveCount(2);
    await historyHolds(page, 1, 1);

    // The state before Back holds the live token, and the sidebar may still owe a request for it: let those leave
    // before recording, and don't count one that carries this state's idx stage.
    await page.waitForLoadState('networkidle');
    const sent = recordRequests(page);
    await page.evaluate(() => { (window as unknown as { __noReload: number }).__noReload = 1; });
    await page.goBack();
    await expectTotal(page, TOTAL);
    await expectExpired(page, description);
    expect(await page.evaluate(() => (window as unknown as { __noReload?: number }).__noReload)).toBe(1);

    await removeExpired(page);
    await expectTotal(page, TOTAL);
    expect(carrying(afterBack(sent), 'e2e-expired-token', 'in_selection', '"expired"', description)).toEqual([]);
});

test.describe('an applied id list over 50,000', () => {
    let ids: number[] = [];
    const description = 'Map (50003 Selected)';

    // Three of the fixture's samples among 50,000 ids no sample has: a selection too long to save. The app never
    // saves such a stage, so it is put in place as the app keeps an applied stage, written from another page so
    // the dataset page's own save can't overwrite it. On the dataset page it is applied, as in the session that
    // made it.
    async function openWithLongSelection(page: Page) {
        if (!ids.length) {
            const answer = await page.request.post(`/api/datasets/${readState().datasetId}/samples/query`, { data: { limit: 3, offset: 0 } });
            const { items } = await answer.json();
            ids = [...items.map((sample: { id: number }) => sample.id), ...Array.from({ length: 50_000 }, (_, i) => 1_000_000_000 + i)];
        }
        const stage = { type: 'selection', description, filter: { field: 'id', operator: 'in', value: ids }, metadata: { count: ids.length, source: 'Map' } };
        await page.goto('/');
        await page.evaluate(({ key, stage }) => localStorage.setItem(key, JSON.stringify({
            viewStages: [stage], filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
            brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Map',
        })), { key: storageKey(), stage });
        await gotoDataset(page);
        await expectTotal(page, 3);  // the live chip filters
        await expect(chips(page)).toHaveCount(1);
        await expect(expired(page)).toHaveCount(0);
    }

    test('stays expired after a reload, out of a saved view, and can be removed', async ({ page }) => {
        await openWithLongSelection(page);
        await expect.poll(() => saved(page)).toContain('"expired":true');
        expect(await saved(page)).not.toContain(String(ids[ids.length - 1]));

        const sent = recordRequests(page);
        await page.reload();
        await expectTotal(page, TOTAL);
        await expectExpired(page, description);

        const name = `e2e-expired-${Date.now()}`;
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await page.getByPlaceholder('Enter view name...').fill(name);
        const posted = page.waitForRequest(r => /\/datasets\/\d+\/views$/.test(r.url()) && r.method() === 'POST');
        await page.getByRole('button', { name: 'Save View' }).click();
        const body = (await posted).postDataJSON();
        expect(body.content.viewStages).toEqual([]);
        const list = await (await page.request.get(`/api/datasets/${readState().datasetId}/views`)).json();
        for (const view of list.filter((v: { name: string }) => v.name === name)) await page.request.delete(`/api/views/${view.id}`);
        await page.getByRole('button', { name: 'Close Views' }).click();

        await removeExpired(page);
        await expectTotal(page, TOTAL);
        expect(carrying(sent, String(ids[ids.length - 1]), '"expired"', description)).toEqual([]);
    });

    test('stays expired after Back', async ({ page }) => {
        await openWithLongSelection(page);
        await historyHolds(page, 0, 1);
        await addIdxStage(page, '>=', '9');
        await expect(chips(page)).toHaveCount(2);
        await historyHolds(page, 1, 1);

        // As in the Select All test above: the state before Back holds the live list, and the sidebar may still owe
        // a request for it.
        await page.waitForLoadState('networkidle');
        const sent = recordRequests(page);
        await page.goBack();
        await expectTotal(page, TOTAL);
        await expectExpired(page, description);
        await removeExpired(page);
        await expectTotal(page, TOTAL);
        expect(carrying(afterBack(sent), String(ids[ids.length - 1]), '"expired"', description)).toEqual([]);
    });
});

// The dataset opens on its default view only when this browser kept no filters for it. A selection that expired is
// one: its chip shows over the whole dataset, so the count is the dataset's, not the default view's.
test.describe('a dataset with a default view', () => {
    const IDX_AT_LEAST_6 = { field: 'idx', operator: '>=', value: 6 };
    let cookie = '';
    let datasetId = 0;
    let viewId = 0;
    test.beforeAll(async () => {
        cookie = await adminLogin();
        datasetId = await createFixtureDataset(cookie, `e2e-expired-default-view-${Date.now()}`);
        viewId = (await apiFetch(cookie, `/datasets/${datasetId}/views`, {
            method: 'POST',
            body: JSON.stringify({
                name: `e2e-expired-later-half-${Date.now()}`,
                is_public: true,
                content: {
                    filters: { logic: 'AND', filters: [IDX_AT_LEAST_6] },
                    viewStages: [{ type: 'filter', description: 'idx ≥ 6', filter: IDX_AT_LEAST_6 }],
                },
            }),
        })).id;
    });
    test.afterAll(async () => {
        if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
    });

    test('an expired Select All opens without the default view, which the next visit without one opens on', async ({ page }) => {
        // The selection is the only filter, applied before the dataset had a default view.
        await gotoDataset(page, datasetId);
        await expectTotal(page, TOTAL);
        await bigSelectAll(page);
        await page.getByTestId('apply-filters').click();
        await expect(chips(page)).toHaveCount(1);
        const description = (await chips(page).first().textContent())!.trim();
        await expect.poll(() => saved(page, datasetId)).toContain('"expired":true');
        await historyHolds(page, 0, 1);  // the entry a reload reopens holds it too
        // An administrator makes the view the dataset's default.
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: viewId }) });

        await page.reload();
        await expectTotal(page, TOTAL);
        await expectExpired(page, description);
        await expect(page.getByTestId('default-view-label')).toHaveCount(0);

        // Removing the chip leaves the whole dataset: the default view is decided as the dataset opens.
        await removeExpired(page);
        await expectTotal(page, TOTAL);
        await expect(chips(page)).toHaveCount(0);
        await expect(page.getByTestId('default-view-label')).toHaveCount(0);

        // With no expired chip kept, the next visit opens on the default view. (A reload reopens this entry, whose
        // address says "no filters".)
        await expect.poll(() => saved(page, datasetId)).not.toContain('"expired":true');
        await gotoDataset(page, datasetId);
        await expectTotal(page, 6);
        await expect(page.getByTestId('default-view-label')).toBeVisible();
        await expect(chips(page)).toHaveCount(1);
        await expect(expired(page)).toHaveCount(0);
    });
});
