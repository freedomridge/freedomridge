import fs from 'fs';
import path from 'path';
import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal, fillStable } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset, deleteDatasetAndWait, promoteField, readState } from './api';

// Links that open a dataset on a filter, a saved view or a sample (frontend/lib/links.ts). The page opens the link
// once, replaces the filters it restored with the link's, and removes the link from the address. The frozen links
// of tests/link-fixtures.json that carry a `count` are opened here against the shared fixture dataset.
test.use({ storageState: AUTH_STATE });

type Stage = { type: string; description: string; filter: object };
const FIXTURES: {
    q: Array<{ name: string; q: string; filters: object[]; count?: number }>;
    f: Array<{ name: string; f: string; stages: Stage[]; count?: number }>;
} = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'tests', 'link-fixtures.json'), 'utf-8'));

const UNKNOWN_VERSION = "This link was made by a different version of Freedom Ridge and can't be opened here.";
const clean = (id: number) => new RegExp(`/datasets/${id}$`);
const chips = (page: Page) => page.getByTestId('query-stage-chip');
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

test('a q= link opens on its chips and count in a fresh browser, leaves the address, and a reload keeps it', async ({ page }) => {
    const fixture = FIXTURES.q.find(f => f.count !== undefined)!;
    await page.goto(`/datasets/${datasetId}?q=${encodeURIComponent(fixture.q)}`);
    await expectTotal(page, fixture.count!);
    await expect(chips(page)).toHaveCount(fixture.filters.length);
    await expect(chips(page).first()).toHaveText(/split = val/);
    await expect(page).toHaveURL(clean(datasetId));
    await page.waitForTimeout(500);  // the filters are saved for the next visit after 300 ms
    await page.reload();
    await expectTotal(page, fixture.count!);
    await expect(chips(page)).toHaveCount(fixture.filters.length);
});

for (const fixture of FIXTURES.f.filter(f => f.count !== undefined)) {
    test(`a frozen f= link opens on its chips and count: ${fixture.name}`, async ({ page }) => {
        await page.goto(`/datasets/${datasetId}?f=${fixture.f}`);
        await expectTotal(page, fixture.count!);
        await expect(chips(page)).toHaveCount(fixture.stages.length);
        for (const [i, stage] of fixture.stages.entries()) await expect(chips(page).nth(i)).toContainText(stage.description);
        await expect(page).toHaveURL(clean(datasetId));
    });
}

test('an f= link of a version this build does not know says so and opens as without the link', async ({ page }) => {
    await page.goto(`/datasets/${datasetId}?f=2.${FIXTURES.f[0].f.slice(2)}`);
    await expect(page.getByText(UNKNOWN_VERSION)).toBeVisible({ timeout: 20_000 });
    await expectTotal(page, 12);
    await expect(chips(page)).toHaveCount(0);
    await expect(page).toHaveURL(clean(datasetId));
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
    await expect(page).toHaveURL(clean(datasetId));
    await forgetFilters(page, datasetId);
});

test('a link over local filters replaces them; Back returns to them with a clean address, and a reload there keeps them', async ({ page }) => {
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
    await expectTotal(page, 6);
    await expect(chips(page)).toHaveCount(1);
    await expect(chips(page)).toHaveText(/split = train/);
    await expect(page).toHaveURL(clean(datasetId));
    expect(keptFilterAsked).toEqual([]);
    page.off('request', countKept);

    await page.waitForTimeout(800);  // history pushes are debounced (400 ms)
    await page.evaluate(() => { (window as unknown as { __noReload: number }).__noReload = 1; });
    await page.goBack();
    await expectTotal(page, 3);
    await expect(chips(page)).toHaveText(/idx ≥ 9/);
    await expect(page).toHaveURL(clean(datasetId));
    expect(await page.evaluate(() => (window as unknown as { __noReload?: number }).__noReload)).toBe(1);

    await page.waitForTimeout(500);  // the restored filters are saved after 300 ms
    await page.reload();
    await expectTotal(page, 3);
    await expect(chips(page)).toHaveText(/idx ≥ 9/);
    await expect(page).toHaveURL(clean(datasetId));
});

test('a sample link opens its view, walking the grid as loaded; an unknown sample says so', async ({ page }) => {
    const id = sampleIds[3];
    await page.goto(`/datasets/${datasetId}?sample=${id}`);
    await expect(page.getByRole('heading', { name: `Sample ${id}` })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTitle('Previous sample (Left arrow)')).toBeVisible();
    await expect(page).toHaveURL(clean(datasetId));
    await expectTotal(page, 12);

    // With a filter, next and previous stay inside it: idx 6 is the first of idx ≥ 6.
    const first = sampleIds[6];
    await page.goto(`/datasets/${datasetId}?q=${encodeURIComponent('idx >= 6')}&sample=${first}`);
    await expect(page.getByRole('heading', { name: `Sample ${first}` })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTitle('Previous sample (Left arrow)')).toHaveCount(0);
    await page.getByTitle('Next sample (Right arrow)').click();
    await expect(page.getByRole('heading', { name: `Sample ${sampleIds[7]}` })).toBeVisible();
    await expect(page).toHaveURL(clean(datasetId));

    await page.goto(`/datasets/${datasetId}?sample=999999999`);
    await expect(page.getByText("The linked sample isn't in this dataset.")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page).toHaveURL(clean(datasetId));
});

// A dataset with a default view and saved views: its own copy of the fixture, since a default view changes how
// the dataset opens for every spec browsing it.
test.describe('a dataset with a default view', () => {
    let dsId = 0;
    let firstThree = '';
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
        await expect(page).toHaveURL(clean(dsId));

        await forgetFilters(page, dsId);
        await page.goto(`/datasets/${dsId}?view=no-such-view`);
        await expect(page.getByText("This view no longer exists or isn't shared with you.")).toBeVisible({ timeout: 20_000 });
        await expectTotal(page, 6);
        await expect(page.getByTestId('default-view-label')).toBeVisible();
        await expect(page).toHaveURL(clean(dsId));
    });

    test('a filter link shows only its own chips; a sample link alone opens the sample over the default view', async ({ page }) => {
        await page.goto(`/datasets/${dsId}?q=${encodeURIComponent('idx < 2')}`);
        await expectTotal(page, 2);
        await expect(chips(page)).toHaveCount(1);
        await expect(page.getByTestId('default-view-label')).toHaveCount(0);
        await expect(page).toHaveURL(clean(dsId));

        await forgetFilters(page, dsId);
        await page.goto(`/datasets/${dsId}?sample=${ids[7]}`);
        await expect(page.getByRole('heading', { name: `Sample ${ids[7]}` })).toBeVisible({ timeout: 20_000 });
        await expectTotal(page, 6);
        await expect(page.getByTestId('default-view-label')).toBeVisible();
        await expect(page).toHaveURL(clean(dsId));
    });

    test('a sort link sets the grid\'s sort', async ({ page }) => {
        await page.goto(`/datasets/${dsId}?sort=idx&dir=desc`);
        await expect(page.getByTestId('grid-sort')).toHaveValue('idx', { timeout: 20_000 });
        await expect(page.getByTestId('grid-sort').locator('xpath=..').getByRole('button', { name: '↓' })).toBeVisible();
        await expect(page).toHaveURL(clean(dsId));
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

    test('a link goes through the login page and lands on the linked state', async ({ page }) => {
        const link = `/datasets/${datasetId}?q=${encodeURIComponent('split = train AND idx != 3')}`;
        await page.goto(link);
        // A visit without a session reads as an expired one (expired=true) when /api/auth/me answers 401 first.
        await expect(page).toHaveURL(/\/login\?(expired=true&)?next=/, { timeout: 15_000 });
        expect(new URL(page.url()).searchParams.get('next')).toBe(link);
        await signIn(page);
        await expectTotal(page, 5);
        await expect(chips(page)).toHaveCount(2);
        await expect(page).toHaveURL(clean(datasetId));
    });

    test('after login, a next that leaves the app lands on the home page', async ({ page, baseURL }) => {
        for (const next of ['//evil.example', '/\\evil.example', 'https://evil.example']) {
            await page.goto(`/login?next=${encodeURIComponent(next)}`);
            await signIn(page);
            await expect(page).toHaveURL(`${baseURL}/`);
        }
    });
});
