import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal, gotoDataset } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset, deleteDatasetAndWait, promoteField } from './api';

// FRIDGE_LIVE_ADDRESS=off (app/runtime-config/route.ts): the dataset page's address stays /datasets/<id>. The history
// entries are written as with it on (e2e/live-address.spec.ts, live-address-sample.spec.ts), so Back, Forward and a
// reload still restore the filters and the open sample; a link still opens, then leaves the address; Copy link still
// copies the whole link. A request for the setting that fails counts as off.
test.use({ storageState: AUTH_STATE, permissions: ['clipboard-read', 'clipboard-write'] });

const TOTAL = 12;  // the fixture datasets
const chips = (page: Page) => page.getByTestId('query-stage-chip');
const dialog = (page: Page) => page.getByRole('dialog');
const heading = (page: Page) => page.getByRole('heading', { name: /^Sample \d+$/ });
const clipboard = (page: Page) => page.evaluate(() => navigator.clipboard.readText());

// The setting off, as the frontend server answers with FRIDGE_LIVE_ADDRESS=off: its real answer (the map's token
// kept), with liveAddress false.
async function liveAddressOff(page: Page) {
    await page.route('**/runtime-config', async (route) => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...(await response.json()), liveAddress: false } });
    });
}

// Every address with parameters written from here on, Next's own included (its history patch calls through to these).
async function recordParameterWrites(page: Page) {
    await page.addInitScript(() => {
        const written: string[] = [];
        (window as unknown as { __written: string[] }).__written = written;
        for (const name of ['pushState', 'replaceState'] as const) {
            const write = window.history[name].bind(window.history);
            window.history[name] = (data: unknown, unused: string, url?: string | URL | null) => {
                if (url && new URL(String(url), location.href).search) written.push(String(url));
                write(data, unused, url);
            };
        }
    });
}
const parameterWrites = (page: Page) => page.evaluate(() => (window as unknown as { __written: string[] }).__written);

// The id of the sample the view shows.
async function shownId(page: Page): Promise<string> {
    await expect(heading(page)).toBeVisible({ timeout: 10_000 });
    return (await heading(page).textContent())!.replace('Sample ', '');
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

// Its own dataset: a sortable field and a public view.
let cookie = '';
let dsId = 0;
let publicView = '';
test.beforeAll(async () => {
    test.setTimeout(120_000);
    cookie = await adminLogin();
    dsId = await createFixtureDataset(cookie, `e2e-live-address-off-${Date.now()}`);
    await promoteField(cookie, dsId, 'idx');  // the grid sorts by indexed fields
    const filter = { field: 'idx', operator: '>=', value: 9 };
    publicView = (await apiFetch(cookie, `/datasets/${dsId}/views`, {
        method: 'POST',
        body: JSON.stringify({
            name: `e2e-public-${Date.now()}`, is_public: true,
            content: { filters: { logic: 'AND', filters: [filter] }, viewStages: [{ type: 'filter', description: 'idx ≥ 9', filter }] },
        }),
    })).slug;
});
test.afterAll(async () => {
    if (dsId) await deleteDatasetAndWait(cookie, dsId, 120_000);
});

const bare = (page: Page) => new URL(`/datasets/${dsId}`, page.url()).href;

test('a filter, a sort and a sample leave the address bare; Back, Forward and a reload still restore them', async ({ page }) => {
    await liveAddressOff(page);
    await recordParameterWrites(page);
    await gotoDataset(page, dsId);
    await expectTotal(page, TOTAL);

    await addIdxStage(page, '>=', '6');
    await expectTotal(page, 6);
    await page.getByTestId('grid-sort').selectOption('idx');
    await page.getByTestId('grid-cell').first().click();
    const id = await shownId(page);
    await page.keyboard.press('Escape');  // Back to the filter's entry, the view's one Forward
    await expect(dialog(page)).toHaveCount(0);
    await page.waitForTimeout(1_000);  // an address made for any of these would be written by now
    expect(page.url()).toBe(bare(page));
    expect(await parameterWrites(page)).toEqual([]);

    await page.goForward();
    await expect(heading(page)).toHaveText(`Sample ${id}`, { timeout: 10_000 });
    await page.goBack();
    await expect(dialog(page)).toHaveCount(0);
    await expectTotal(page, 6);
    await page.goBack();
    await expectTotal(page, TOTAL);
    await expect(chips(page)).toHaveCount(0);
    await page.goForward();
    await expectTotal(page, 6);
    await expect(chips(page)).toHaveCount(1);
    expect(page.url()).toBe(bare(page));

    // A reload reopens the entry it is on (the address it recorded is the bare one shown). The address names no sort:
    // the grids keep the one they had.
    await page.reload();
    await expectTotal(page, 6);
    await expect(chips(page)).toHaveCount(1);
    await expect(page.getByTestId('grid-sort')).toHaveValue('idx');
    await page.goForward();
    await expect(heading(page)).toHaveText(`Sample ${id}`, { timeout: 20_000 });
    await page.reload();
    await expect(heading(page)).toHaveText(`Sample ${id}`, { timeout: 20_000 });
    await expectTotal(page, 6);
    // And so does Back into the page after leaving it.
    await page.goto('/');
    await page.goBack();
    await expect(heading(page)).toHaveText(`Sample ${id}`, { timeout: 20_000 });
    await expectTotal(page, 6);
    await page.waitForTimeout(1_000);
    expect(page.url()).toBe(bare(page));
    expect(await parameterWrites(page)).toEqual([]);
});

test('a q= or view= link opens on its chips and count, then the address drops it; a reload keeps them', async ({ page }) => {
    await liveAddressOff(page);
    await page.goto(`/datasets/${dsId}?q=${encodeURIComponent('idx >= 6 AND idx < 9')}`);
    await expectTotal(page, 3);
    await expect(chips(page)).toHaveCount(2);
    await expect.poll(() => page.url()).toBe(bare(page));
    await page.reload();
    await expectTotal(page, 3);
    await expect(chips(page)).toHaveCount(2);
    expect(page.url()).toBe(bare(page));

    await page.goto(`/datasets/${dsId}?view=${publicView}`);
    await expectTotal(page, 3);
    await expect(chips(page)).toHaveText([/idx ≥ 9/]);
    await expect.poll(() => page.url()).toBe(bare(page));
});

test('Copy link still copies the whole link, from the bar and from the sample\'s view', async ({ page }) => {
    await liveAddressOff(page);
    await gotoDataset(page, dsId);
    await addIdxStage(page, '>=', '6');
    await expectTotal(page, 6);
    await page.getByTestId('copy-link').click();
    await expect(page.getByText(/^Link to these filters copied/)).toBeVisible();
    const link = new URL(await clipboard(page));
    expect(`${link.origin}${link.pathname}`).toBe(bare(page));
    expect(Object.fromEntries(link.searchParams)).toEqual({ q: 'idx >= 6' });

    await page.getByTestId('grid-cell').first().click();
    const id = await shownId(page);
    await page.getByTestId('modal-copy-link').click();
    await expect(page.getByText(/^Link to this sample copied/)).toBeVisible();
    const sampleLink = new URL(await clipboard(page));
    expect(Object.fromEntries(sampleLink.searchParams)).toEqual({ q: 'idx >= 6', sample: id });
    expect(page.url()).toBe(bare(page));
});

test('when the setting can\'t be read, the live address is off', async ({ page }) => {
    await page.route('**/runtime-config', (route) => route.fulfill({ status: 500, body: 'unavailable' }));
    await page.goto(`/datasets/${dsId}?q=${encodeURIComponent('idx >= 6')}`);
    await expectTotal(page, 6);
    await expect(chips(page)).toHaveCount(1);
    await expect.poll(() => page.url()).toBe(bare(page));
    await addIdxStage(page, '>=', '9');
    await expectTotal(page, 3);
    await page.waitForTimeout(1_000);
    expect(page.url()).toBe(bare(page));
    await page.reload();
    await expectTotal(page, 3);
    await expect(chips(page)).toHaveCount(2);
});
