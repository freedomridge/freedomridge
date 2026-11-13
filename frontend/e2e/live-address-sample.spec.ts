import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal, gotoDataset } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset, deleteDatasetAndWait, readState } from './api';

// The sample whose view is open is in the dataset page's address too (`sample=<id>`, issue #391): opening one is a
// history entry of its own, next and previous rewrite it, Back closes the view and Forward opens it again, and a reload
// reopens it. Closing it goes Back to the entry it was opened over, and only from there: a view the page closes itself
// (a search showing its results) never goes Back, which would undo what follows.
test.use({ storageState: AUTH_STATE });

const TOTAL = 12;  // the fixture datasets
const IDX_6 = 'idx >= 6';
const IDX_6_AND_9 = 'idx >= 6 AND idx >= 9';
const chips = (page: Page) => page.getByTestId('query-stage-chip');
const dialog = (page: Page) => page.getByRole('dialog');
const heading = (page: Page) => page.getByRole('heading', { name: /^Sample \d+$/ });
const historyLength = (page: Page) => page.evaluate(() => window.history.length);
// The address's parameters, once they are `expected`: the address is written after its entry, asynchronously.
async function expectAddress(page: Page, expected: Record<string, unknown>) {
    await expect.poll(() => Object.fromEntries(new URL(page.url()).searchParams)).toEqual(expected);
}
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

// Every pushState and replaceState from here on, Next's own included (its history patch calls through to these).
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

// Every history.back() the page makes from here on (Playwright's own Back isn't one).
async function countBacks(page: Page) {
    await page.addInitScript(() => {
        const counted = window as unknown as { __backs: number };
        counted.__backs = 0;
        const back = window.history.back.bind(window.history);
        window.history.back = () => {
            counted.__backs += 1;
            back();
        };
    });
}
const backs = (page: Page) => page.evaluate(() => (window as unknown as { __backs: number }).__backs);

// The view's ✕.
const closeButton = (page: Page) => dialog(page).locator('button:has(path[d="M6 18L18 6M6 6l12 12"])');

test('opening a sample is one entry naming it; next and previous add none; Back closes it, Forward and a reload reopen it', async ({ page }) => {
    await recordHistoryWrites(page);
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    await page.waitForTimeout(500);
    await historyWrites(page);
    const before = await historyLength(page);
    await page.evaluate(() => { (window as unknown as { __noReload: number }).__noReload = 1; });

    await page.getByTestId('grid-cell').nth(1).click();
    const first = await shownId(page);
    await expectAddress(page, { q: '', sample: first });
    expect(await historyLength(page)).toBe(before + 1);
    await page.waitForTimeout(500);
    expect((await historyWrites(page)).filter((write) => write.startsWith('pushState'))).toHaveLength(1);

    // Stepping quickly through samples rewrites the entry once, when the stepping pauses: browsers refuse or drop
    // history calls past about a hundred in a short while.
    for (let i = 0; i < 5; i += 1) await page.keyboard.press('ArrowRight');
    await expect(heading(page)).not.toHaveText(`Sample ${first}`);
    await page.waitForTimeout(800);
    const stepped = await shownId(page);
    await expectAddress(page, { q: '', sample: stepped });
    const writes = await historyWrites(page);
    expect(writes.filter((write) => write.startsWith('pushState'))).toEqual([]);
    expect(writes.length).toBeLessThanOrEqual(3);  // the entry, its address, and the router's copy of it
    await page.keyboard.press('ArrowLeft');
    await expect(heading(page)).not.toHaveText(`Sample ${stepped}`);
    const shown = await shownId(page);
    await expectAddress(page, { q: '', sample: shown });
    expect(await historyLength(page)).toBe(before + 1);

    await page.goBack();
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: '' });
    await page.goForward();
    await expect(heading(page)).toHaveText(`Sample ${shown}`, { timeout: 10_000 });
    await expectAddress(page, { q: '', sample: shown });
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible();
    expect(await page.evaluate(() => (window as unknown as { __noReload?: number }).__noReload)).toBe(1);

    // A reload reopens the view, in the grid it was opened from.
    await page.reload();
    await expect(heading(page)).toHaveText(`Sample ${shown}`, { timeout: 20_000 });
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTitle('Previous sample (Left arrow)')).toBeVisible();
    await expectAddress(page, { q: '', sample: shown });
    // Closed, it goes Back to the entry it was opened over.
    await page.keyboard.press('Escape');
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: '' });
    await expectTotal(page, TOTAL);
    expect(await historyLength(page)).toBe(before + 1);
});

test('closing the view goes Back to the entry it was opened over, which Back and Forward then step past', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    await addIdxStage(page, '>=', '6');
    await expectTotal(page, 6);
    await expectAddress(page, { q: IDX_6 });
    const length = await historyLength(page);

    await page.getByTestId('grid-cell').first().click();
    const id = await shownId(page);
    await expectAddress(page, { q: IDX_6, sample: id });
    await closeButton(page).click();
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: IDX_6 });
    expect(await historyLength(page)).toBe(length + 1);  // the view's entry, ahead of this one for Forward

    // Back goes to the filters before, not to the view; Forward to the filter, then to the view.
    await page.goBack();
    await expectTotal(page, TOTAL);
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: '' });
    await page.goForward();
    await expectTotal(page, 6);
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: IDX_6 });
    await page.goForward();
    await expect(heading(page)).toHaveText(`Sample ${id}`, { timeout: 10_000 });
    await expectAddress(page, { q: IDX_6, sample: id });

    // Escape, then a change: it follows the filter's entry, in place of the view's.
    await page.keyboard.press('Escape');
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: IDX_6 });
    await addIdxStage(page, '>=', '9');
    await expectTotal(page, 3);
    await expectAddress(page, { q: IDX_6_AND_9 });
    await page.goBack();
    await expectTotal(page, 6);
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: IDX_6 });
});

test('a sample opened while the close before it is still going Back gets its entry after that Back', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    const length = await historyLength(page);
    await page.getByTestId('grid-cell').nth(1).click();
    const first = await shownId(page);
    await expectAddress(page, { q: '', sample: first });

    // Escape and a click on another sample in one go: the close's Back hasn't arrived when the second opens.
    await page.evaluate(() => {
        document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        (document.querySelectorAll('[data-testid="grid-cell"]')[4].firstElementChild as HTMLElement).click();
    });
    await expect(heading(page)).not.toHaveText(`Sample ${first}`);
    const second = await shownId(page);
    await expectAddress(page, { q: '', sample: second });
    expect(await historyLength(page)).toBe(length + 1);
    await page.goBack();
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: '' });
    await page.goForward();
    await expect(heading(page)).toHaveText(`Sample ${second}`, { timeout: 10_000 });
});

test('a sample selected in the view stays selected when the view closes at once, and Back steps through both', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    const length = await historyLength(page);
    await page.getByTestId('grid-cell').nth(2).click();
    const id = await shownId(page);
    await expectAddress(page, { q: '', sample: id });

    // The selection's own entry isn't due yet (it waits for the changes to settle): closing writes it first, and the
    // view's entry is then no longer the current one, so the close doesn't go Back over the selection.
    await page.keyboard.press(' ');
    await page.keyboard.press('Escape');
    await expect(dialog(page)).toHaveCount(0);
    await expect(page.getByTestId('grid-selection-count')).toContainText('1 selected');
    await expectAddress(page, { q: '' });
    await page.waitForTimeout(1_000);  // nothing arrives later to undo it
    await expect(page.getByTestId('grid-selection-count')).toContainText('1 selected');
    await expectAddress(page, { q: '' });
    expect(await historyLength(page)).toBe(length + 2);

    // Back: the view as it was before the selection; Back again: the dataset before the view.
    await page.goBack();
    await expect(heading(page)).toHaveText(`Sample ${id}`, { timeout: 10_000 });
    await expectAddress(page, { q: '', sample: id });
    await page.goBack();
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: '' });
    await expect(page.getByTestId('grid-selection-count')).toHaveCount(0);  // nothing selected
});

test('Space in a view opened from the duplicates panel, or Back to one, selects that sample, not a grid row opened before', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    const gridCell = page.getByTestId('grid-cell').first();
    await gridCell.click();
    const gridRow = await shownId(page);
    await page.keyboard.press('Escape');
    await expect(dialog(page)).toHaveCount(0);

    // The fixture has no hashes: one duplicate group, of two samples other than that grid row.
    const { items } = await apiFetch(await adminLogin(), `/datasets/${readState().datasetId}/samples?limit=50`);
    const twins = items.map((sample: { id: number }) => sample.id).filter((id: number) => String(id) !== gridRow).slice(0, 2);
    await page.route('**/hashes/stats**', (route) => route.fulfill({ json: {
        total_samples: TOTAL, fields: { phash: { samples_with_hashes: TOTAL, exact_duplicate_groups: 1, exact_duplicate_samples: 2 } },
    } }));
    await page.route('**/hashes/duplicates**', (route) => route.fulfill({
        json: { groups: [{ sample_ids: twins, size: 2 }], total_groups: 1, total_duplicate_samples: 2 },
    }));
    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('duplicates');
    const tile = page.getByTestId('panel-duplicates').locator('div.aspect-square.cursor-pointer').first();
    const selected = /(^|\s)ring-2(\s|$)/;  // a duplicates tile's mark for a selected sample
    await tile.dispatchEvent('click');  // the tile, not its selection circle, which covers the middle of a small one
    const twin = await shownId(page);
    expect(twin).not.toBe(gridRow);
    await page.keyboard.press(' ');
    await expect(tile).toHaveClass(selected);
    await expect(gridCell).toHaveAttribute('data-selected', 'false');
    await page.keyboard.press('Escape');
    await expect(dialog(page)).toHaveCount(0);
    await expect(page.getByTestId('grid-selection-count')).toContainText('1 selected');

    // A grid row opened and closed again, then Back to the view opened from the duplicates (before its selection).
    await gridCell.click();
    await expect(heading(page)).toHaveText(`Sample ${gridRow}`);
    await page.keyboard.press('Escape');
    await expect(dialog(page)).toHaveCount(0);
    await page.goBack();
    await expect(heading(page)).toHaveText(`Sample ${twin}`, { timeout: 10_000 });
    await expect(tile).not.toHaveClass(selected);
    await page.keyboard.press(' ');
    await expect(tile).toHaveClass(selected);
    await expect(gridCell).toHaveAttribute('data-selected', 'false');
});

test('a patch search from the view keeps its results in place of the view\'s entry, and Back from them returns to the dataset', async ({ page }) => {
    await countBacks(page);
    // The fixture has no images and no patch embeddings: a picture for every media request, one patch scale, and the
    // search's answer, three of the fixture's samples.
    const dataUrl: string = await page.evaluate(() => {
        const canvas = document.createElement('canvas');
        canvas.width = 400;
        canvas.height = 300;
        canvas.getContext('2d')!.fillRect(0, 0, 400, 300);
        return canvas.toDataURL('image/png');
    });
    const png = Buffer.from(dataUrl.split(',')[1], 'base64');
    await page.route('**/media**', (route) => route.fulfill({ contentType: 'image/png', body: png }));
    await page.route('**/embeddings/patch-scales**', (route) => route.fulfill({ json: [{ scale: '2x2' }] }));
    const { items } = await apiFetch(await adminLogin(), `/datasets/${readState().datasetId}/samples?limit=3`);
    await page.route('**/api/search/patches', (route) => route.fulfill({
        json: items.map((sample: { id: number }, i: number) => ({
            sample_id: sample.id, patch_row: 0, patch_col: 0, distance: i / 10, filepath: '', metadata: {}, media_type: 'image', tags: [],
        })),
    }));

    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    const length = await historyLength(page);
    await page.getByTestId('grid-cell').first().click();
    const id = await shownId(page);
    await expectAddress(page, { q: '', sample: id });
    await dialog(page).locator('select').filter({ has: page.locator('option[value="2x2"]') }).selectOption('2x2');
    await dialog(page).locator('div.absolute.cursor-pointer[style*="width: 50%"]').first().click();
    await dialog(page).getByRole('button', { name: 'Search Similar Patches' }).click();

    await expect(dialog(page)).toHaveCount(0);
    await expect(chips(page)).toHaveText([/Patch Search/]);
    await expectTotal(page, 3);
    // A Back from the close would by now have put the filters before the search over its results.
    await page.waitForTimeout(1_500);
    await expect(chips(page)).toHaveText([/Patch Search/]);
    await expectTotal(page, 3);
    await expectAddress(page, { f: expect.stringMatching(/^1\./) });
    // The page closed the view without going Back, and the results' entry took the view's place.
    expect(await backs(page)).toBe(0);
    expect(await historyLength(page)).toBe(length + 1);

    await page.goBack();
    await expectTotal(page, TOTAL);
    await expect(chips(page)).toHaveCount(0);
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: '' });
    await page.goForward();
    await expectTotal(page, 3);
    await expect(dialog(page)).toHaveCount(0);
});

test('Find similar from the view shows the search\'s results in place of the view\'s entry, and Back from them returns to the dataset', async ({ page }) => {
    await countBacks(page);
    const { items } = await apiFetch(await adminLogin(), `/datasets/${readState().datasetId}/samples?limit=3`);
    await page.route((url) => url.pathname.endsWith('/api/search'), (route) => route.fulfill({
        json: items.map((sample: { id: number }) => ({ id: sample.id })),
    }));
    await gotoDataset(page);
    await expectTotal(page, TOTAL);
    await expectAddress(page, { q: '' });
    const length = await historyLength(page);
    await page.getByTestId('grid-cell').nth(3).click();
    const id = await shownId(page);
    await expectAddress(page, { q: '', sample: id });

    await page.getByTestId('modal-find-similar').click();
    await expect(heading(page)).toHaveCount(0);
    await expectAddress(page, { q: '' });
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    await expect(chips(page)).toHaveText([/Similarity Search/]);
    await expectTotal(page, 3);
    await expectAddress(page, { f: expect.stringMatching(/^1\./) });
    // The page closed the view without going Back, and the results' entry took the view's place.
    expect(await backs(page)).toBe(0);
    expect(await historyLength(page)).toBe(length + 1);

    await page.goBack();
    await expectTotal(page, TOTAL);
    await expect(chips(page)).toHaveCount(0);
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: '' });
});

test('a pasted sample link keeps the sample in the address while its view is open; closed, the address leaves it out', async ({ page }) => {
    const { items } = await apiFetch(await adminLogin(), `/datasets/${readState().datasetId}/samples?limit=50`);
    const id = String(items.find((sample: { metadata: { idx: number } }) => sample.metadata.idx === 7).id);
    await page.goto(`/datasets/${readState().datasetId}?q=${encodeURIComponent(IDX_6)}&sample=${id}`);
    await expect(heading(page)).toHaveText(`Sample ${id}`, { timeout: 20_000 });
    await expectTotal(page, 6);
    await expectAddress(page, { q: IDX_6, sample: id });
    await expect(page.getByTitle('Next sample (Right arrow)')).toBeVisible({ timeout: 15_000 });
    await page.waitForTimeout(800);  // the history writes of the opening have settled
    const length = await historyLength(page);

    // Nothing of the page's is behind the entry the link opened: closing rewrites it.
    await page.keyboard.press('Escape');
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: IDX_6 });
    await expectTotal(page, 6);
    expect(await historyLength(page)).toBe(length);
    await page.reload();
    await expectTotal(page, 6);
    await expect(dialog(page)).toHaveCount(0);
    await expectAddress(page, { q: IDX_6 });
});

// Its own dataset: a sample of it is deleted.
test.describe('a dataset with a sample deleted', () => {
    let cookie = '';
    let dsId = 0;
    test.beforeAll(async () => {
        test.setTimeout(120_000);
        cookie = await adminLogin();
        dsId = await createFixtureDataset(cookie, `e2e-live-address-sample-${Date.now()}`);
    });
    test.afterAll(async () => {
        if (dsId) await deleteDatasetAndWait(cookie, dsId, 120_000);
    });

    test('Forward to the view of a sample deleted since says so, opens no view, and leaves the sample out of the address', async ({ page }) => {
        await gotoDataset(page, dsId);
        await expectTotal(page, TOTAL);
        await expectAddress(page, { q: '' });
        await page.getByTestId('grid-cell').first().click();
        const id = await shownId(page);
        await expectAddress(page, { q: '', sample: id });
        await page.keyboard.press('Escape');
        await expect(dialog(page)).toHaveCount(0);
        await expectAddress(page, { q: '' });

        await apiFetch(cookie, `/datasets/${dsId}/samples/${id}`, { method: 'DELETE' });
        await page.goForward();
        await expect(page.getByText("The linked sample isn't in this dataset.")).toBeVisible({ timeout: 20_000 });
        await expect(dialog(page)).toHaveCount(0);
        await expectAddress(page, { q: '' });
    });
});
