import fs from 'fs';
import path from 'path';
import { test, expect, type Browser, type Page } from '@playwright/test';
import { AUTH_STATE, bigSelectAll, expandField, expectTotal, gotoDataset } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset, deleteDatasetAndWait, promoteField, readState } from './api';

// The dataset page's address follows the applied filters and the grid's sort (issue #391): each history entry gets
// the address of its state, so a reload, Back and Forward, and the address opened in another browser show the same
// thing. A state no address can hold (a big Select All, an expired selection) names no filters in the address.
test.use({ storageState: AUTH_STATE });

type Stage = { type: string; description: string; filter: object };
const FIXTURES: { f: Array<{ name: string; f: string; stages: Stage[]; count?: number }> } =
    JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'tests', 'link-fixtures.json'), 'utf-8'));

const TOTAL = 12;  // the fixture datasets
const IDX_6 = 'idx >= 6';
const IDX_6_AND_9 = 'idx >= 6 AND idx >= 9';
const chips = (page: Page) => page.getByTestId('query-stage-chip');
const parameters = (page: Page) => Object.fromEntries(new URL(page.url()).searchParams);
// The address's parameters, once they are `expected`: the address is written after its entry, asynchronously.
async function expectAddress(page: Page, expected: Record<string, unknown>) {
    await expect.poll(() => parameters(page)).toEqual(expected);
}

async function addIdxStage(page: Page, op: string, value: string) {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('idx');
    await page.locator('[data-radix-popper-content-wrapper] button:has(span:text-is("idx"))').click();
    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.locator('select').nth(1).selectOption(op);
    await editor.getByPlaceholder('Value...').fill(value);
    await editor.getByRole('button', { name: 'Apply' }).click();
}

// Every pushState and replaceState from here on, Next's own included (its history patch calls through to these), each
// marked with whether it was given an address.
async function recordHistoryWrites(page: Page) {
    await page.addInitScript(() => {
        const writes: string[] = [];
        (window as unknown as { __writes: string[] }).__writes = writes;
        for (const name of ['pushState', 'replaceState'] as const) {
            const write = window.history[name].bind(window.history);
            window.history[name] = (data: unknown, unused: string, url?: string | URL | null) => {
                writes.push(url ? `${name} url` : name);
                write(data, unused, url);
            };
        }
    });
}
const historyWrites = (page: Page) => page.evaluate(() => (window as unknown as { __writes: string[] }).__writes.splice(0));

// The address opened in a new browser context: signed in, nothing else kept.
async function openFresh(browser: Browser, address: string, check: (page: Page) => Promise<void>, sameAddress = true) {
    const context = await browser.newContext({ storageState: AUTH_STATE });
    try {
        const page = await context.newPage();
        await page.goto(address);
        await check(page);
        if (sameAddress) expect(page.url()).toBe(address);
    } finally {
        await context.close();
    }
}

test('an applied filter goes in the address, which a reload and another browser open on', async ({ page, browser }) => {
    await recordHistoryWrites(page);
    // An address write never makes the router fetch the page again.
    const fetched: string[] = [];
    page.on('request', (request) => {
        const url = new URL(request.url());
        if (url.pathname.startsWith('/datasets/') && (url.searchParams.has('_rsc') || request.headers()['rsc'] === '1')) fetched.push(request.url());
    });
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });  // no filters
    await page.waitForTimeout(500);
    await historyWrites(page);

    // An applied filter: one entry, and its address (ours, then Next's own copy of it).
    await addIdxStage(page, '>=', '6');
    await expectTotal(page, 6);
    await expectAddress(page, { q: IDX_6 });
    await page.waitForTimeout(500);
    const writes = await historyWrites(page);
    expect(writes.filter((write) => write.startsWith('pushState'))).toHaveLength(1);
    expect(writes.length).toBeLessThanOrEqual(3);

    // A draft (a ticked sample) is an entry, for Back, but not in the address: one push, no address written.
    await page.getByTestId('grid-cell').first().click({ modifiers: ['ControlOrMeta'] });
    await expect(page.getByTestId('grid-selection-count')).toContainText('1 selected');
    await page.waitForTimeout(1_000);
    expect(await historyWrites(page)).toEqual(['pushState']);
    await expectAddress(page, { q: IDX_6 });
    // A value picked in the sidebar is a draft that isn't even an entry: nothing until it is applied.
    await expandField(page, 'split');
    await page.getByTestId('panel-filters').locator('label', { hasText: /^val/ }).first().click();
    await expect(page.getByTestId('apply-filters')).toBeVisible();
    await page.waitForTimeout(1_000);
    expect(await historyWrites(page)).toEqual([]);
    await expectAddress(page, { q: IDX_6 });

    const address = page.url();
    await page.reload();
    await expectTotal(page, 6);
    await expect(chips(page)).toHaveCount(1);
    expect(page.url()).toBe(address);
    expect(fetched).toEqual([]);

    await openFresh(browser, address, async (fresh) => {
        await expectTotal(fresh, 6);
        await expect(chips(fresh)).toHaveText([/idx/]);
    });
});

test('a map region goes in the address encoded, which reopens on the same chips and count', async ({ page, browser }) => {
    const fixture = FIXTURES.f.find((f) => f.name === 'map region, with a plain filter')!;
    // The filters this browser kept for the dataset, written from another page so the dataset page's own save can't
    // overwrite them; then the dataset, opened without an address of its own.
    await page.goto('/');
    await page.evaluate(({ key, stages }) => localStorage.setItem(key, JSON.stringify({
        viewStages: stages, filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
        brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
    })), { key: `fridge-filters-${readState().datasetId}`, stages: fixture.stages });
    await gotoDataset(page);
    await expectTotal(page, fixture.count!);
    await expectAddress(page, { f: expect.stringMatching(/^1\.[A-Za-z0-9_-]+$/) });

    const address = page.url();
    await page.reload();
    await expectTotal(page, fixture.count!);
    await expect(chips(page)).toHaveCount(fixture.stages.length);
    expect(page.url()).toBe(address);
    await openFresh(browser, address, async (fresh) => {
        await expectTotal(fresh, fixture.count!);
        await expect(chips(fresh)).toHaveCount(fixture.stages.length);
        for (const [i, stage] of fixture.stages.entries()) await expect(chips(fresh).nth(i)).toContainText(stage.description);
    });
});

test('Back and Forward step through the filters with their addresses; a reload in the middle stays there', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    await addIdxStage(page, '>=', '6');
    await expectTotal(page, 6);
    await expectAddress(page, { q: IDX_6 });
    await addIdxStage(page, '>=', '9');
    await expectTotal(page, 3);
    await expectAddress(page, { q: IDX_6_AND_9 });

    await page.evaluate(() => { (window as unknown as { __noReload: number }).__noReload = 1; });
    await page.goBack();
    await expectTotal(page, 6);
    await expect(chips(page)).toHaveCount(1);
    await expectAddress(page, { q: IDX_6 });
    await page.goBack();
    await expectTotal(page, TOTAL);
    await expect(chips(page)).toHaveCount(0);
    await expectAddress(page, { q: '' });
    await page.goForward();
    await expectTotal(page, 6);
    await expectAddress(page, { q: IDX_6 });
    expect(await page.evaluate(() => (window as unknown as { __noReload?: number }).__noReload)).toBe(1);

    await page.reload();
    await expectTotal(page, 6);
    await expect(chips(page)).toHaveCount(1);
    await expectAddress(page, { q: IDX_6 });
    await page.goForward();
    await expectTotal(page, 3);
    await expect(chips(page)).toHaveCount(2);
    await expectAddress(page, { q: IDX_6_AND_9 });
});

test('a change right after Back replaces the entry Back restored, so a reload keeps it', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await addIdxStage(page, '>=', '6');
    await expectAddress(page, { q: IDX_6 });
    await addIdxStage(page, '>=', '9');
    await expectAddress(page, { q: IDX_6_AND_9 });

    await page.goBack();
    await expect(chips(page)).toHaveCount(1);
    await chips(page).first().getByRole('button').click();  // removed at once, while the restore settles
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    await page.reload();
    await expectTotal(page, TOTAL);
    await expect(chips(page)).toHaveCount(0);
    await expectAddress(page, { q: '' });
});

test('an entry left before its address arrived gets it when Back or Forward returns to it', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    // The filter's text is slow to come: the address is still being made when the entry is left.
    await page.route('**/filter_query/format', async (route) => {
        await new Promise((resolve) => setTimeout(resolve, 1_000));
        await route.continue();
    });
    await addIdxStage(page, '>=', '9');
    await expect.poll(() => page.evaluate(() => window.history.state?.viewStages?.length)).toBe(1);
    expect(parameters(page)).toEqual({ q: '' });  // the entry holds the address before it, for now
    await page.goBack();
    await expectTotal(page, TOTAL);
    await page.goForward();
    await expectTotal(page, 3);
    await expectAddress(page, { q: 'idx >= 9' });
    // And it is the entry's own: a reload reopens it.
    expect(await page.evaluate(() => window.history.state.address === `${location.pathname}${location.search}`)).toBe(true);
    await page.reload();
    await expectTotal(page, 3);
    await expectAddress(page, { q: 'idx >= 9' });
});

test('a big Select All takes the filters out of the address; a reload shows its chip expired', async ({ page, browser }) => {
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    await bigSelectAll(page);
    await page.getByTestId('apply-filters').click();
    await expect(chips(page)).toHaveCount(1);
    const description = (await chips(page).first().textContent())!.trim();
    // No link holds it (its selection is on the server for 2 hours): the address names no filters, never the ones
    // before it.
    await expectAddress(page, {});
    const address = page.url();

    // The reload reopens this entry (the address is the one it holds): the chip, expired.
    await page.reload();
    await expectTotal(page, TOTAL);
    await expect(page.getByTestId('expired-selection-chip')).toContainText(description);
    expect(page.url()).toBe(address);

    // Elsewhere, the address opens the dataset as it would without a link.
    await openFresh(browser, address, async (fresh) => {
        await expectTotal(fresh, TOTAL);
        await expect(chips(fresh)).toHaveCount(0);
        await expect(fresh.getByTestId('expired-selection-chip')).toHaveCount(0);
        await expectAddress(fresh, { q: '' });
    }, false);

    // Removed, the state is one the address holds again.
    await page.getByTestId('expired-selection-chip').hover();
    await page.getByTestId('expired-selection-chip').locator('button').click();
    await expectAddress(page, { q: '' });
});

// Its own dataset: a sortable field and saved views.
test.describe('a dataset with views and a sortable field', () => {
    let cookie = '';
    let dsId = 0;
    let publicView = '';
    let privateView = '';
    test.beforeAll(async () => {
        test.setTimeout(120_000);
        cookie = await adminLogin();
        dsId = await createFixtureDataset(cookie, `e2e-live-address-${Date.now()}`);
        await promoteField(cookie, dsId, 'idx');  // the grid sorts by indexed fields
        const view = async (name: string, isPublic: boolean, stages: Stage[]) => (await apiFetch(cookie, `/datasets/${dsId}/views`, {
            method: 'POST',
            body: JSON.stringify({
                name, is_public: isPublic,
                content: { filters: { logic: 'AND', filters: stages.map((stage) => stage.filter) }, viewStages: stages },
            }),
        })).slug;
        publicView = await view(`e2e-public-${Date.now()}`, true, [
            { type: 'filter', description: 'idx ≥ 6', filter: { field: 'idx', operator: '>=', value: 6 } },
            { type: 'filter', description: 'idx ≥ 9', filter: { field: 'idx', operator: '>=', value: 9 } },
        ]);
        privateView = await view(`e2e-private-${Date.now()}`, false, [
            { type: 'filter', description: 'idx < 3', filter: { field: 'idx', operator: '<', value: 3 } },
        ]);
    });
    test.afterAll(async () => {
        if (dsId) await deleteDatasetAndWait(cookie, dsId, 120_000);
    });

    test('a sort change rewrites the address, not the history; Back keeps the sort, and a reload opens on it', async ({ page }) => {
        await recordHistoryWrites(page);
        await gotoDataset(page, dsId);
        await expectTotal(page, TOTAL);
        await expectAddress(page, { q: '' });
        await addIdxStage(page, '>=', '6');
        await expectTotal(page, 6);
        await expectAddress(page, { q: IDX_6 });
        await page.waitForTimeout(500);
        await historyWrites(page);

        const length = await page.evaluate(() => window.history.length);
        await page.getByTestId('grid-sort').selectOption('idx');
        await expectAddress(page, { q: IDX_6, sort: 'idx', dir: 'asc' });
        await page.getByTestId('grid-sort').locator('xpath=..').getByRole('button', { name: '↑' }).click();
        await expectAddress(page, { q: IDX_6, sort: 'idx', dir: 'desc' });
        await page.waitForTimeout(500);
        expect(await page.evaluate(() => window.history.length)).toBe(length);
        expect((await historyWrites(page)).filter((write) => write.startsWith('pushState'))).toEqual([]);

        // The sort isn't history: Back keeps it, and the entry it restores gets it in its address.
        await page.goBack();
        await expectTotal(page, TOTAL);
        await expectAddress(page, { q: '', sort: 'idx', dir: 'desc' });

        // A reload opens on the address's sort, whatever another tab chose since.
        await page.evaluate((key) => localStorage.setItem(key, JSON.stringify({ order: 'asc' })), `fridge:gridSort:${dsId}`);
        await page.reload();
        await expectTotal(page, TOTAL);
        await expect(page.getByTestId('grid-sort')).toHaveValue('idx');
        await expect(page.getByTestId('grid-sort').locator('xpath=..').getByRole('button', { name: '↓' })).toBeVisible();
        await expectAddress(page, { q: '', sort: 'idx', dir: 'desc' });
    });

    test('a sort chosen in another tab reaches neither this tab\'s address nor, on a reload, its grids', async ({ page, context }) => {
        await gotoDataset(page, dsId);
        await expectTotal(page, TOTAL);
        await expectAddress(page, { q: '' });
        const other = await context.newPage();  // the same browser: the sort remembered for the dataset is shared
        try {
            await gotoDataset(other, dsId);
            await expectTotal(other, TOTAL);
            await other.getByTestId('grid-sort').selectOption('idx');
            await expectAddress(other, { q: '', sort: 'idx', dir: 'asc' });

            await addIdxStage(page, '>=', '6');
            await expectTotal(page, 6);
            await expectAddress(page, { q: IDX_6 });
            await expect(page.getByTestId('grid-sort')).toHaveValue('');
            await page.reload();
            await expectTotal(page, 6);
            await expect(page.getByTestId('grid-sort')).toHaveValue('');
            await expectAddress(page, { q: IDX_6 });
        } finally {
            await other.close();
        }
    });

    test('a public view as it was opened is its view in the address; changed, or private, its filters', async ({ page }) => {
        await page.goto(`/datasets/${dsId}?view=${publicView}`);
        await expectTotal(page, 3);
        await expect(chips(page)).toHaveCount(2);
        await expectAddress(page, { view: publicView });
        const address = page.url();
        await page.reload();
        await expectTotal(page, 3);
        expect(page.url()).toBe(address);

        await chips(page).nth(1).getByRole('button').click();
        await expectTotal(page, 6);
        await expectAddress(page, { q: IDX_6 });

        // A private view wouldn't open for others: the address holds its filters.
        await page.goto(`/datasets/${dsId}?view=${privateView}`);
        await expectTotal(page, 3);
        await expect(chips(page)).toHaveText([/idx < 3/]);
        await expectAddress(page, { q: 'idx < 3' });
    });
});
