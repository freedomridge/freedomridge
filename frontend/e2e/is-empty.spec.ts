import { test, expect, type Locator, type Page } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expectTotal } from './helpers';
import { stamp } from './access-helpers';

// `is null` means no value, and an empty list has a test of its own (contract g9, section 7.7). The dataset's samples
// hold a list someone added (`inferred_tags`: two empty lists, two with elements, a JSON null and no key). The query
// builder offers `is empty` (and `is not empty`) for the list field where it offers `is null`, and the grid then shows
// exactly the two samples whose list is empty.
//
// Written from the contract, not from the code. `is null` is asked first through the same builder, as main answers it
// (the JSON null and the sample with no key: an empty list is a value), so a failure on main is `is empty`'s: main
// offers no such operator.
test.use({ storageState: AUTH_STATE });

const FIELD = 'inferred_tags';
// name: the list (undefined: no key)
const SAMPLES: Record<string, string[] | null | undefined> = {
    emptyA: [],
    emptyB: [],
    weed: ['weed'],
    two: ['weed', 'rock'],
    nulled: null,
    nokey: undefined,
};
const EMPTY_LISTS = ['emptyA', 'emptyB'];
const NO_LIST = ['nokey', 'nulled'];
// A 1x1 PNG for every tile, so each keeps its <img> (and its sample id) instead of failing to load.
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64');

let cookie = '';
let datasetId = 0;
const nameOf: Record<number, string> = {};

test.beforeAll(async () => {
    cookie = await adminLogin();
    datasetId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-is-empty-${stamp()}` }) })).id;
    const names = Object.keys(SAMPLES);
    const { ids } = await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: names.map(name => ({
            filepath: `s3://e2e-is-empty/${name}.jpg`,
            metadata: SAMPLES[name] === undefined ? {} : { [FIELD]: SAMPLES[name] },
        })) }),
    });
    expect(ids).toHaveLength(names.length);
    ids.forEach((id: number, i: number) => { nameOf[id] = names[i]; });
});

test.afterAll(async () => {
    if (datasetId) await deleteDatasetAndWait(cookie, datasetId);
});

// The samples the grid shows, by name, from each tile's media address.
async function shownSamples(page: Page): Promise<string[]> {
    const sources = await page.getByTestId('grid-cell').locator('img[src*="/media"]')
        .evaluateAll(images => images.map(image => image.getAttribute('src') ?? ''));
    return sources.map(src => nameOf[Number(/\/samples\/(\d+)\/media/.exec(src)?.[1])] ?? src).sort();
}

/** The Add Filter card on the list field: its rule's operator select. */
async function operatorFor(page: Page): Promise<{ editor: Locator; operator: Locator }> {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill(FIELD);
    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.getByRole('button', { name: new RegExp(`^${FIELD}`) }).first().click();
    return { editor, operator: editor.getByTestId('filter-rule').locator('select').nth(1) };
}

test('the query builder offers is empty for a list field and the grid shows the samples whose list is empty', async ({ page }) => {
    test.setTimeout(120_000);
    await page.route('**/samples/*/media**', route => route.fulfill({ contentType: 'image/png', body: PNG }));
    await page.goto(`/datasets/${datasetId}`);
    await expectTotal(page, Object.keys(SAMPLES).length);

    // Asked first, as main answers it: `is null` through the builder shows the samples with no list.
    const first = await operatorFor(page);
    await first.operator.selectOption('is_null');
    await first.editor.getByTestId('filter-card-apply').click();
    await expectTotal(page, NO_LIST.length);
    await expect.poll(() => shownSamples(page)).toEqual(NO_LIST);
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, Object.keys(SAMPLES).length);

    // The list field's operators hold the two new tests beside `is null`; `is empty` shows the empty lists only.
    const { editor, operator } = await operatorFor(page);
    const offered = await operator.locator('option').evaluateAll(options => options.map(option => (option as HTMLOptionElement).value));
    expect(offered).toEqual(expect.arrayContaining(['is_null', 'is_empty', 'is_not_empty']));
    await operator.selectOption('is_empty');
    await editor.getByTestId('filter-card-apply').click();

    await expectTotal(page, EMPTY_LISTS.length);
    await expect(page.getByTestId('grid-cell')).toHaveCount(EMPTY_LISTS.length);
    await expect.poll(() => shownSamples(page)).toEqual(EMPTY_LISTS);
});
