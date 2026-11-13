import fs from 'fs';
import path from 'path';
import { test, expect, type Browser, type Page } from '@playwright/test';
import { AUTH_STATE, bigSelectAll, expectTotal, gotoDataset } from './helpers';
import { adminLogin, apiFetch, readState } from './api';

// Copy link (frontend/lib/links.ts): the filter bar's button copies the applied filters (query text in q= when every
// chip can be text, else every chip encoded in f=), a saved view's copies ?view=<slug>, and the sample view's adds
// sample=<id>. Each link is opened in a fresh browser, with nothing kept from this one, and must show the same thing.
test.use({ storageState: AUTH_STATE, permissions: ['clipboard-read', 'clipboard-write'] });

type Stage = { type: string; description: string; filter: object };
const FIXTURES: {
    q: Array<{ name: string; q: string; filters: object[]; count?: number }>;
    f: Array<{ name: string; f: string; stages: Stage[]; count?: number }>;
} = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'tests', 'link-fixtures.json'), 'utf-8'));

const chips = (page: Page) => page.getByTestId('query-stage-chip');
const clipboard = (page: Page) => page.evaluate(() => navigator.clipboard.readText());

let cookie = '';
let datasetId = 0;  // the shared fixture
test.beforeAll(async () => {
    cookie = await adminLogin();
    datasetId = readState().datasetId;
});

// The applied filters, as the app keeps them for the dataset, written from another page so the dataset page's own
// save can't overwrite them.
async function openWithStages(page: Page, stages: Stage[]) {
    await page.goto('/');
    await page.evaluate(({ key, stages }) => localStorage.setItem(key, JSON.stringify({
        viewStages: stages, filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
        brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
    })), { key: `fridge-filters-${datasetId}`, stages });
    await gotoDataset(page);
}

// The link opened in a new browser context: signed in, nothing else kept. The address then holds the page's own link
// to what it shows (e2e/live-address.spec.ts).
async function openFresh(browser: Browser, link: string, check: (page: Page) => Promise<void>) {
    const context = await browser.newContext({ storageState: AUTH_STATE });
    try {
        const page = await context.newPage();
        await page.goto(link);
        await check(page);
        await expect(page).toHaveURL(new RegExp(`/datasets/${datasetId}\\?`));
    } finally {
        await context.close();
    }
}

test('plain filters copy as q=, without the draft, and open on the same chips and count', async ({ page, browser }) => {
    const fixture = FIXTURES.q.find(f => f.name === 'AND, OR, NOT and brackets')!;
    await openWithStages(page, fixture.filters.map((filter, i) => ({ type: 'filter', description: `chip ${i}`, filter })));
    await expectTotal(page, fixture.count!);
    // A checkbox selection is a draft: not applied, so not in the link.
    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toContainText(String(fixture.count));

    await page.getByTestId('copy-link').click();
    await expect(page.getByText('Link to these filters copied. It holds the applied filters only, not the draft.')).toBeVisible();
    const link = new URL(await clipboard(page));
    expect(`${link.origin}${link.pathname}`).toBe(new URL(`/datasets/${datasetId}`, page.url()).href);
    expect(link.searchParams.get('q')).toBe('split = "val" AND NOT (idx >= 9 OR note exists)');
    expect([...link.searchParams.keys()]).toEqual(['q']);

    await openFresh(browser, link.href, async (fresh) => {
        await expectTotal(fresh, fixture.count!);
        await expect(chips(fresh)).toHaveCount(fixture.filters.length);
        await expect(chips(fresh).first()).toHaveText(/split = val/);
        await expect(fresh).toHaveURL(link.href);  // the same chips, the same link
    });
});

test('a map-region chip copies every chip as f=, which opens on the same chips and count', async ({ page, browser }) => {
    const fixture = FIXTURES.f.find(f => f.name === 'map region, with a plain filter')!;
    await openWithStages(page, fixture.stages);
    await expectTotal(page, fixture.count!);
    await page.getByTestId('copy-link').click();
    await expect(page.getByText('Link to these filters copied', { exact: true })).toBeVisible();
    const link = new URL(await clipboard(page));
    expect(link.searchParams.get('f')).toMatch(/^1\.[A-Za-z0-9_-]+$/);
    expect(link.searchParams.get('q')).toBeNull();

    await openFresh(browser, link.href, async (fresh) => {
        await expectTotal(fresh, fixture.count!);
        await expect(chips(fresh)).toHaveCount(fixture.stages.length);
        for (const [i, stage] of fixture.stages.entries()) await expect(chips(fresh).nth(i)).toContainText(stage.description);
    });
});

// The language writes `embeddings.<field> exists` (the coverage warning's filter) but doesn't read it back, so a q=
// link would fail to open: the chips go in f=.
test('a chip written as text that the dataset does not read back copies as f=', async ({ page, browser }) => {
    const stages: Stage[] = [
        { type: 'filter', description: 'idx ≥ 6', filter: { field: 'idx', operator: '>=', value: 6 } },
        { type: 'filter', description: 'e2e_clip embedding', filter: { field: 'embeddings.e2e_clip', operator: 'exists', value: null } },
    ];
    await openWithStages(page, stages);
    await expectTotal(page, 6);
    await page.getByTestId('copy-link').click();
    await expect(page.getByText('Link to these filters copied', { exact: true })).toBeVisible();
    const link = new URL(await clipboard(page));
    expect(link.searchParams.get('q')).toBeNull();
    expect(link.searchParams.get('f')).toMatch(/^1\./);

    await openFresh(browser, link.href, async (fresh) => {
        await expectTotal(fresh, 6);
        await expect(chips(fresh)).toHaveCount(2);
    });
});

test('a big Select All chip copies nothing and says which chip stops the link', async ({ page }) => {
    // The refusal is a message, never an unhandled rejection (next dev showed one as an error overlay).
    const pageErrors: string[] = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    await gotoDataset(page);
    await bigSelectAll(page);
    await page.getByTestId('apply-filters').click();
    await expect(chips(page)).toHaveCount(1);
    const description = (await chips(page).first().textContent())!.trim();
    await page.evaluate(() => navigator.clipboard.writeText('nothing copied'));

    await page.getByTestId('copy-link').click();
    await expect(page.getByText(`Can't copy a link: “${description}” is a temporary selection, kept for 2 hours only. Save a view and copy its link instead.`)).toBeVisible();
    await page.waitForTimeout(500);
    expect(await clipboard(page)).toBe('nothing copied');
    expect(pageErrors).toEqual([]);
});

test('a saved view\'s Copy link opens the view', async ({ page, browser }) => {
    const name = `e2e-copy-link-${Date.now()}`;
    const filter = { field: 'idx', operator: '<', value: 3 };
    const view = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({ name, content: { filters: { logic: 'AND', filters: [filter] }, viewStages: [{ type: 'filter', description: 'idx < 3', filter }] } }),
    });
    try {
        await gotoDataset(page);
        await page.getByRole('button', { name: 'Saved Views' }).click();
        const row = page.locator('div.group').filter({ hasText: name });
        await row.hover();
        await row.getByTestId('view-copy-link').click();
        await expect(page.getByText(`Link to the view “${name}” copied`)).toBeVisible();
        const link = await clipboard(page);
        expect(link).toBe(new URL(`/datasets/${datasetId}?view=${view.slug}`, page.url()).href);

        await openFresh(browser, link, async (fresh) => {
            await expectTotal(fresh, 3);
            await expect(chips(fresh)).toHaveText([/idx < 3/]);
        });
    } finally {
        await page.request.delete(`/api/views/${view.id}`);
    }
});

test('the sample view\'s Copy link reopens that sample on the applied filters', async ({ page, browser }) => {
    await openWithStages(page, [{ type: 'filter', description: 'idx ≥ 6', filter: { field: 'idx', operator: '>=', value: 6 } }]);
    await expectTotal(page, 6);
    await page.getByTestId('grid-cell').nth(1).click();
    const heading = page.getByRole('heading', { name: /^Sample \d+$/ });
    await expect(heading).toBeVisible({ timeout: 10_000 });
    const id = (await heading.textContent())!.replace('Sample ', '');

    await page.getByTestId('modal-copy-link').click();
    await expect(page.getByText('Link to this sample copied', { exact: true })).toBeVisible();
    const link = new URL(await clipboard(page));
    expect(link.searchParams.get('q')).toBe('idx >= 6');
    expect(link.searchParams.get('sample')).toBe(id);

    await openFresh(browser, link.href, async (fresh) => {
        await expect(fresh.getByRole('heading', { name: `Sample ${id}` })).toBeVisible({ timeout: 20_000 });
        await expect(fresh.getByTitle('Previous sample (Left arrow)')).toBeVisible({ timeout: 15_000 });
        await expect(fresh.getByTitle('Next sample (Right arrow)')).toBeVisible();
        await expectTotal(fresh, 6);
    });
});

// Safari's rule (a write only in the click, so the ClipboardItem path) can't be run here; what follows when a browser
// refuses each way can.
test('a refused clipboard write falls back to writeText; refused both ways, the link is shown to copy by hand', async ({ page }) => {
    await page.addInitScript(() => {
        const refuse = () => Promise.reject(new DOMException('Refused', 'NotAllowedError'));
        const flags = window as unknown as { refuseText: boolean };
        flags.refuseText = false;
        const writeText = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.write = refuse;
        navigator.clipboard.writeText = (text: string) => (flags.refuseText ? refuse() : writeText(text));
    });
    await gotoDataset(page);
    const link = `${new URL(`/datasets/${datasetId}`, page.url()).href}?q=`;  // no filters: an empty query
    await page.getByTestId('copy-link').click();
    await expect(page.getByText('Link to this dataset copied', { exact: true })).toBeVisible();
    expect(await clipboard(page)).toBe(link);

    await page.evaluate(() => { (window as unknown as { refuseText: boolean }).refuseText = true; });
    await page.getByTestId('copy-link').click();
    await expect(page.getByText(`This browser didn't let the page copy. The link: ${link}`, { exact: true })).toBeVisible();
});
