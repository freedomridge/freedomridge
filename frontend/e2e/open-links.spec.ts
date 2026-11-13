import fs from 'fs';
import path from 'path';
import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal, fillStable, gotoDataset } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset, deleteDatasetAndWait, promoteField, readState } from './api';

// Links that open a dataset on a filter, a saved view or a sample (frontend/lib/links.ts). The page opens the link
// once and replaces the filters it restored with the link's, in the entry it opened (Back doesn't return to them).
// The address then holds the page's own link to what it shows (e2e/live-address.spec.ts), also when the link couldn't
// be opened. The frozen links of tests/link-fixtures.json that carry a `count` are opened here against the shared
// fixture dataset.
test.use({ storageState: AUTH_STATE });

type Stage = { type: string; description: string; filter: object };
const FIXTURES: {
    q: Array<{ name: string; q: string; filters: object[]; count?: number }>;
    f: Array<{ name: string; f: string; stages: Stage[]; count?: number }>;
} = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'tests', 'link-fixtures.json'), 'utf-8'));

const UNKNOWN_VERSION = "This link was made by a different version of Freedom Ridge and can't be opened here.";
const chips = (page: Page) => page.getByTestId('query-stage-chip');
// The address's parameters once they are `expected` (the page writes them after it has opened the link).
async function expectAddress(page: Page, id: number, expected: Record<string, unknown>) {
    await expect(page).toHaveURL(new RegExp(`/datasets/${id}\\?`));
    await expect.poll(() => Object.fromEntries(new URL(page.url()).searchParams)).toEqual(expected);
}
// Forget the filters this browser kept for a dataset, from another page so the dataset page's own save can't
// write them back.
async function forgetFilters(page: Page, id: number) {
    await page.goto('/');
    await page.evaluate((key) => localStorage.removeItem(key), `fridge-filters-${id}`);
}

let cookie = '';
let datasetId = 0;  // the shared fixture
const sampleIds: Record<number, number> = {};  // its sample ids by idx
test.beforeAll(async () => {
    cookie = await adminLogin();
    datasetId = readState().datasetId;
    for (const sample of (await apiFetch(cookie, `/datasets/${datasetId}/samples?limit=50`)).items) {
        sampleIds[sample.metadata.idx] = sample.id;
    }
});

test('a q= link opens on its chips and count in a fresh browser, keeps them in the address, and a reload keeps it', async ({ page }) => {
    const fixture = FIXTURES.q.find(f => f.count !== undefined)!;
    await page.goto(`/datasets/${datasetId}?q=${encodeURIComponent(fixture.q)}`);
    await expectTotal(page, fixture.count!);
    await expect(chips(page)).toHaveCount(fixture.filters.length);
    await expect(chips(page).first()).toHaveText(/split = val/);
    // The address is the page's own link to the chips: the same filters, written as the query language writes them.
    await expectAddress(page, datasetId, { q: 'split = "val" AND NOT (idx >= 9 OR note exists)' });
    const address = page.url();
    await page.reload();
    await expectTotal(page, fixture.count!);
    await expect(chips(page)).toHaveCount(fixture.filters.length);
    expect(page.url()).toBe(address);
});

for (const fixture of FIXTURES.f.filter(f => f.count !== undefined)) {
    test(`a frozen f= link opens on its chips and count: ${fixture.name}`, async ({ page }) => {
        await page.goto(`/datasets/${datasetId}?f=${fixture.f}`);
        await expectTotal(page, fixture.count!);
        await expect(chips(page)).toHaveCount(fixture.stages.length);
        for (const [i, stage] of fixture.stages.entries()) await expect(chips(page).nth(i)).toContainText(stage.description);
        // The address holds the same chips: as text when they can be, else encoded.
        await expect.poll(() => [...new URL(page.url()).searchParams.keys()]).toEqual([expect.stringMatching(/^[qf]$/)]);
    });
}

test('an f= link of a version this build does not know says so and opens as without the link', async ({ page }) => {
    await page.goto(`/datasets/${datasetId}?f=2.${FIXTURES.f[0].f.slice(2)}`);
    await expect(page.getByText(UNKNOWN_VERSION)).toBeVisible({ timeout: 20_000 });
    await expectTotal(page, 12);
    await expect(chips(page)).toHaveCount(0);
    await expectAddress(page, datasetId, { q: '' });  // what the page shows: no filters
});

// A link made before a field was removed (or renamed) names a field the dataset no longer has: it says which, applies
// nothing, and the address then says what is shown.
test('a q= link naming a field the dataset no longer has says which field, and applies nothing', async ({ page }) => {
    await page.goto(`/datasets/${datasetId}?q=${encodeURIComponent('split = train AND removed_field > 2')}`);
    await expect(page.getByText(/^This link's filter can't be used here\. Line 1, column \d+: unknown field 'removed_field'/)).toBeVisible({ timeout: 20_000 });
    await expectTotal(page, 12);
    await expect(chips(page)).toHaveCount(0);
    await expectAddress(page, datasetId, { q: '' });
});

test('a link with an empty query opens with no filters, over the filters this browser kept', async ({ page }) => {
    await page.goto('/');
    await page.evaluate(({ key, stage }) => localStorage.setItem(key, JSON.stringify({
        viewStages: [stage], filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
        brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
    })), { key: `fridge-filters-${datasetId}`, stage: { type: 'filter', description: 'idx ≥ 9', filter: { field: 'idx', operator: '>=', value: 9 } } });
    await page.goto(`/datasets/${datasetId}?q=`);
    await expectTotal(page, 12);
    await expect(chips(page)).toHaveCount(0);
    await expectAddress(page, datasetId, { q: '' });
    await forgetFilters(page, datasetId);
});

test('a link over local filters replaces them in the entry it opened, and a reload keeps the link\'s filters', async ({ page }) => {
    // The filters this browser kept for the dataset (idx ≥ 9: 3 samples), written from another page so the
    // dataset page's own save can't overwrite them.
    await page.goto('/');
    await page.evaluate(({ key, stage }) => localStorage.setItem(key, JSON.stringify({
        viewStages: [stage], filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
        brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
    })), { key: `fridge-filters-${datasetId}`, stage: { type: 'filter', description: 'idx ≥ 9', filter: { field: 'idx', operator: '>=', value: 9 } } });
    // While the link is opened no panel queries the kept filters: they'd be asked for and shown only to be replaced.
    const keptFilterAsked: string[] = [];
    const countKept = (request: { url(): string; postData(): string | null }) => {
        if ((request.postData() ?? '').includes('"value":9')) keptFilterAsked.push(new URL(request.url()).pathname);
    };
    page.on('request', countKept);
    await page.goto(`/datasets/${datasetId}?q=${encodeURIComponent('split = train')}`);
    const opened = await page.evaluate(() => window.history.length);
    await expectTotal(page, 6);
    await expect(chips(page)).toHaveCount(1);
    await expect(chips(page)).toHaveText(/split = train/);
    await expectAddress(page, datasetId, { q: 'split = "train"' });
    expect(keptFilterAsked).toEqual([]);
    page.off('request', countKept);

    // The link's filters took the place of the kept ones in the entry the link opened: no entry for those, so the
    // address never names filters it doesn't show. Back leaves the dataset.
    await page.waitForTimeout(800);  // history pushes are debounced (400 ms)
    expect(await page.evaluate(() => window.history.length)).toBe(opened);
    await page.goBack();
    await expect(page).toHaveURL(/\/$/);
    await page.goForward();
    await expectTotal(page, 6);
    await expect(chips(page)).toHaveText(/split = train/);
    await expectAddress(page, datasetId, { q: 'split = "train"' });

    await page.reload();
    await expectTotal(page, 6);
    await expect(chips(page)).toHaveText(/split = train/);
    await expectAddress(page, datasetId, { q: 'split = "train"' });
});

test('a sample link opens its view, walking the grid as loaded; an unknown sample says so', async ({ page }) => {
    const id = sampleIds[3];
    await page.goto(`/datasets/${datasetId}?sample=${id}`);
    await expect(page.getByRole('heading', { name: `Sample ${id}` })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTitle('Previous sample (Left arrow)')).toBeVisible();
    await expectAddress(page, datasetId, { q: '', sample: String(id) });  // the filters shown, and the sample open
    await expectTotal(page, 12);

    // With a filter, next and previous stay inside it: idx 6 is the first of idx ≥ 6.
    const first = sampleIds[6];
    await page.goto(`/datasets/${datasetId}?q=${encodeURIComponent('idx >= 6')}&sample=${first}`);
    await expect(page.getByRole('heading', { name: `Sample ${first}` })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTitle('Previous sample (Left arrow)')).toHaveCount(0);
    await page.getByTitle('Next sample (Right arrow)').click();
    await expect(page.getByRole('heading', { name: `Sample ${sampleIds[7]}` })).toBeVisible();
    await expectAddress(page, datasetId, { q: 'idx >= 6', sample: String(sampleIds[7]) });

    await page.goto(`/datasets/${datasetId}?sample=999999999`);
    await expect(page.getByText("The linked sample isn't in this dataset.")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expectAddress(page, datasetId, { q: 'idx >= 6' });  // the filters this browser kept, from the visit before; no sample
});

// A dataset with a default view and saved views: its own copy of the fixture, since a default view changes how
// the dataset opens for every spec browsing it.
test.describe('a dataset with a default view', () => {
    let dsId = 0;
    let firstThree = '';
    let laterHalf = '';  // the default view
    const ids: Record<number, number> = {};
    test.beforeAll(async () => {
        test.setTimeout(120_000);
        dsId = await createFixtureDataset(cookie, `e2e-open-links-${Date.now()}`);
        const view = async (name: string, filter: object, description: string) => apiFetch(cookie, `/datasets/${dsId}/views`, {
            method: 'POST',
            body: JSON.stringify({
                name, is_public: true,
                content: { filters: { logic: 'AND', filters: [filter] }, viewStages: [{ type: 'filter', description, filter }] },
            }),
        });
        const later = await view(`later-half-${Date.now()}`, { field: 'idx', operator: '>=', value: 6 }, 'idx ≥ 6');
        laterHalf = later.slug;
        firstThree = (await view(`first-three-${Date.now()}`, { field: 'idx', operator: '<', value: 3 }, 'idx < 3')).slug;
        await apiFetch(cookie, `/datasets/${dsId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: later.id }) });
        await promoteField(cookie, dsId, 'idx');  // the grid sorts by indexed fields
        for (const sample of (await apiFetch(cookie, `/datasets/${dsId}/samples?limit=50`)).items) ids[sample.metadata.idx] = sample.id;
    });
    test.afterAll(async () => {
        if (!dsId) return;
        // Deleting the default view's row during the deletion updates the sealed dataset row (ON DELETE SET NULL),
        // which the deletion refuses; so the default view is cleared first.
        await apiFetch(cookie, `/datasets/${dsId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: null }) });
        await deleteDatasetAndWait(cookie, dsId, 120_000);
    });

    test('a view link opens the view, not the default view; an unknown view says so and opens on the default view', async ({ page }) => {
        await page.goto(`/datasets/${dsId}?view=${firstThree}`);
        await expectTotal(page, 3);
        await expect(chips(page)).toHaveCount(1);
        await expect(chips(page)).toHaveText(/idx < 3/);
        await expect(page.getByTestId('default-view-label')).toHaveCount(0);
        await expectAddress(page, dsId, { view: firstThree });  // a public view, as it was opened

        // A view it can't open ends on the address of what it shows: the default view, a public view too.
        await forgetFilters(page, dsId);
        await page.goto(`/datasets/${dsId}?view=no-such-view`);
        await expect(page.getByText("This view no longer exists or isn't shared with you.")).toBeVisible({ timeout: 20_000 });
        await expectTotal(page, 6);
        await expect(page.getByTestId('default-view-label')).toBeVisible();
        await expectAddress(page, dsId, { view: laterHalf });
    });

    test('a filter link shows only its own chips; a sample link alone opens the sample over the default view', async ({ page }) => {
        await page.goto(`/datasets/${dsId}?q=${encodeURIComponent('idx < 2')}`);
        await expectTotal(page, 2);
        await expect(chips(page)).toHaveCount(1);
        await expect(page.getByTestId('default-view-label')).toHaveCount(0);
        await expectAddress(page, dsId, { q: 'idx < 2' });

        await forgetFilters(page, dsId);
        await page.goto(`/datasets/${dsId}?sample=${ids[7]}`);
        await expect(page.getByRole('heading', { name: `Sample ${ids[7]}` })).toBeVisible({ timeout: 20_000 });
        await expectTotal(page, 6);
        await expect(page.getByTestId('default-view-label')).toBeVisible();
        await expectAddress(page, dsId, { view: laterHalf, sample: String(ids[7]) });
    });

    test('opened bare, the address is the default view at once; a reload of "no filters" doesn\'t bring it back', async ({ page }) => {
        // Every address written, Next's own included (its history patch calls through to replaceState).
        await page.addInitScript(() => {
            const written: string[] = [];
            (window as unknown as { __written: string[] }).__written = written;
            const replace = window.history.replaceState.bind(window.history);
            window.history.replaceState = (data: unknown, unused: string, url?: string | URL | null) => {
                if (url) written.push(String(url));
                replace(data, unused, url);
            };
        });
        await gotoDataset(page, dsId);
        await expectTotal(page, 6);
        await expectAddress(page, dsId, { view: laterHalf });
        // Not first the saved filters' address (no filters, ?q=) and then the default view's.
        const written = await page.evaluate(() => (window as unknown as { __written: string[] }).__written);
        expect(written.filter((url) => url.includes('q='))).toEqual([]);

        // A link that says "no filters" opens without the default view, and so does a reload of its entry.
        await page.goto(`/datasets/${dsId}?q=`);
        await expectTotal(page, 12);
        await expect(page.getByTestId('default-view-label')).toHaveCount(0);
        await expectAddress(page, dsId, { q: '' });
        await page.reload();
        await expectTotal(page, 12);
        await expect(page.getByTestId('default-view-label')).toHaveCount(0);
        await expectAddress(page, dsId, { q: '' });
    });

    test('a sort link sets the grid\'s sort', async ({ page }) => {
        await page.goto(`/datasets/${dsId}?sort=idx&dir=desc`);
        await expect(page.getByTestId('grid-sort')).toHaveValue('idx', { timeout: 20_000 });
        await expect(page.getByTestId('grid-sort').locator('xpath=..').getByRole('button', { name: '↓' })).toBeVisible();
        await expectAddress(page, dsId, { view: laterHalf, sort: 'idx', dir: 'desc' });
        expect(JSON.parse(await page.evaluate((key) => localStorage.getItem(key) ?? '{}', `fridge:gridSort:${dsId}`))).toEqual({ by: 'idx', order: 'desc' });
    });
});

test.describe('signed out', () => {
    test.use({ storageState: { cookies: [], origins: [] } });

    async function signIn(page: Page) {
        await fillStable(page.getByLabel('Username'), 'admin');
        await fillStable(page.getByLabel('Password'), 'admin123');
        await page.getByRole('button', { name: 'Sign in' }).click();
        await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
    }

    test('a link goes through the login page and lands on the linked state, which Back and Forward keep', async ({ page }) => {
        const link = `/datasets/${datasetId}?q=${encodeURIComponent('split = train AND idx != 3')}`;
        const shown = { q: 'split = "train" AND idx != 3' };
        await page.goto(link);
        // A visit without a session reads as an expired one (expired=true) when /api/auth/me answers 401 first.
        await expect(page).toHaveURL(/\/login\?(expired=true&)?next=/, { timeout: 15_000 });
        expect(new URL(page.url()).searchParams.get('next')).toBe(link);
        await signIn(page);
        await expectTotal(page, 5);
        await expect(chips(page)).toHaveCount(2);
        await expectAddress(page, datasetId, shown);

        // Back to the login page, then to the entry the signed-out visit made. It wasn't the page's yet (the link
        // hadn't opened), so it opens as the link, not on the filters this browser kept.
        await page.goBack();
        await expect(page).toHaveURL(/\/login/);
        await page.goBack();
        await expectTotal(page, 5);
        await expect(chips(page)).toHaveCount(2);
        await expectAddress(page, datasetId, shown);
        await page.goForward();
        await expect(page).toHaveURL(/\/login/);
    });

    test('after login, a next that leaves the app lands on the home page', async ({ page, baseURL }) => {
        for (const next of ['//evil.example', '/\\evil.example', 'https://evil.example']) {
            await page.goto(`/login?next=${encodeURIComponent(next)}`);
            await signIn(page);
            await expect(page).toHaveURL(`${baseURL}/`);
        }
    });
});
