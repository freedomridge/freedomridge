import { test, expect, type Locator, type Page } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expandField, expectTotal } from './helpers';
import { stamp } from './access-helpers';

// A list column can hold "never set" (contract g10, section 8.8 and section 9 points 4 and 6). A dataset with a list
// field kept in a column of its own (`pests`, made with array-fields as the app makes it): two samples added without
// the field (never set), two given `[]`, two with elements. The browser test of 8.8 is three tests on that dataset, so
// that each step meets its own rule: the grid and the sample's view open (a field never set is left out of the
// sample's view, as a metadata key the sample doesn't have; a list and an empty list are shown); the sidebar's
// "(no value)" tick for the field is there and shows exactly the count the sidebar has for it, the samples never set;
// `is empty` in the query builder shows the others, the empty lists. A second test exports the CSV through the Export
// dialog: a list never set is an empty cell, an empty list `[]`, for the list column and for a list in the metadata;
// a sample's tags, which this piece doesn't touch, still write an empty cell when it has none.
//
// Written from the contract, not from the code. Readings (also in the report to the contract's owner):
// - "The count beside it" is the count the sidebar has for the field's rows with no value (field-stats' `null_count`,
//   which decides whether the tick is shown): the page shows no number beside the tick, and the control isn't to be
//   redesigned. The tick applied, the grid shows that many samples, and they are the samples never set.
// - An empty list's cell is checked by its value (`[]`), not by whether it is quoted.
test.use({ storageState: AUTH_STATE });

const FIELD = 'pests';
// name: the list sent (undefined: added without the field)
const SAMPLES: Record<string, string[] | undefined> = {
    unsetA: undefined,
    unsetB: undefined,
    emptyA: [],
    emptyB: [],
    aphid: ['aphid'],
    both: ['aphid', 'mite'],
};
const NEVER_SET = ['unsetA', 'unsetB'];
const EMPTY_LISTS = ['emptyA', 'emptyB'];
// A 1x1 PNG for every tile, so each keeps its <img> (and its sample id) instead of failing to load.
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64');

type FieldEntry = { name: string; null_count?: number };

/** A dataset with the list field made first, then these samples; its statistics counted. */
async function makeDataset(cookie: string, prefix: string, samples: object[]): Promise<{ id: number; ids: number[] }> {
    const id = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `${prefix}-${stamp()}` }) })).id;
    await apiFetch(cookie, `/datasets/${id}/array-fields`, { method: 'POST', body: JSON.stringify({ field_name: FIELD }) });
    const { ids } = await apiFetch(cookie, `/datasets/${id}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    expect(ids).toHaveLength(samples.length);
    await apiFetch(cookie, `/datasets/${id}/refresh-stats`, { method: 'POST' });
    return { id, ids };
}

async function fieldEntries(cookie: string, id: number): Promise<FieldEntry[]> {
    return apiFetch(cookie, `/datasets/${id}/field-stats`, { method: 'POST', body: JSON.stringify({}) });
}

test.describe('the grid, the sample view, the sidebar and the query builder', () => {
    let cookie = '';
    let datasetId = 0;
    const nameOf: Record<number, string> = {};
    const idOf: Record<string, number> = {};

    test.beforeAll(async () => {
        test.setTimeout(120_000);
        cookie = await adminLogin();
        const names = Object.keys(SAMPLES);
        const made = await makeDataset(cookie, 'e2e-list-column', names.map(name => ({
            filepath: `s3://e2e-list-column/${name}.jpg`,
            metadata: { plot: name },
            ...(SAMPLES[name] === undefined ? {} : { array_fields: { [FIELD]: SAMPLES[name] } }),
        })));
        datasetId = made.id;
        made.ids.forEach((id, i) => { nameOf[id] = names[i]; idOf[names[i]] = id; });
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

    /** The sample's view of one sample, opened from its tile: its list of properties. */
    async function openView(page: Page, name: string): Promise<Locator> {
        await page.getByTestId('grid-cell').filter({ has: page.locator(`img[src*="/samples/${idOf[name]}/media"]`) }).click();
        const dialog = page.getByRole('dialog');
        await expect(dialog.getByText('Properties')).toBeVisible({ timeout: 10_000 });
        await expect(dialog.locator('dt').filter({ hasText: /^plot$/i })).toBeVisible();  // its metadata is shown
        return dialog;
    }

    async function openDataset(page: Page): Promise<number> {
        await page.route('**/samples/*/media**', route => route.fulfill({ contentType: 'image/png', body: PNG }));
        const total = Object.keys(SAMPLES).length;
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, total);
        return total;
    }

    test('the grid opens, and the sample view shows a list and an empty list and leaves a field never set out', async ({ page }) => {
        test.setTimeout(120_000);
        const total = await openDataset(page);
        await expect(page.getByTestId('grid-cell')).toHaveCount(total);
        await expect.poll(() => shownSamples(page)).toEqual(Object.keys(SAMPLES).sort());
        const field = (view: Locator) => view.locator('dt').filter({ hasText: new RegExp(`^${FIELD}$`, 'i') });
        // Asked first, as main shows them: a list, and an empty list.
        for (const name of ['both', 'emptyA']) {
            const view = await openView(page, name);
            await expect(field(view), name).toHaveCount(1);
            await page.keyboard.press('Escape');
            await expect(view).toHaveCount(0);
        }
        // A field never set is left out, as a metadata key the sample doesn't have.
        const view = await openView(page, 'unsetA');
        await expect(field(view), 'a field never set').toHaveCount(0);
    });

    test("the sidebar's no-value tick shows exactly the count the sidebar has for the field: the samples never set", async ({ page }) => {
        test.setTimeout(120_000);
        // The sidebar's count of the rows with no value, once the statistics are counted.
        await expect.poll(async () => (await fieldEntries(cookie, datasetId)).find(f => f.name === FIELD)?.null_count, {
            timeout: 60_000,
            message: "the sidebar's count of the rows never set",
        }).toBe(NEVER_SET.length);
        const total = await openDataset(page);
        await expandField(page, FIELD);
        const tick = page.getByTestId(`field-section-${FIELD}`).locator('label', { hasText: '(no value)' });
        await expect(tick).toBeVisible({ timeout: 15_000 });
        await tick.click();
        await expect(page.getByTestId('draft-filters')).toBeVisible();
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, NEVER_SET.length);
        await expect.poll(() => shownSamples(page)).toEqual(NEVER_SET);
        await page.getByRole('button', { name: 'Clear All' }).click();
        await expectTotal(page, total);
    });

    test('is empty in the query builder shows the samples whose list is empty, not the ones never set', async ({ page }) => {
        test.setTimeout(120_000);
        await openDataset(page);
        await page.getByRole('button', { name: 'Add Filter' }).click();
        await page.getByPlaceholder('Search fields to filter by...').fill(FIELD);
        const editor = page.locator('[data-radix-popper-content-wrapper]');
        await editor.getByRole('button', { name: new RegExp(`^${FIELD}`) }).first().click();
        const operator = editor.getByTestId('filter-rule').locator('select').nth(1);
        await operator.selectOption('is_empty');
        await editor.getByTestId('filter-card-apply').click();
        await expectTotal(page, EMPTY_LISTS.length);
        await expect(page.getByTestId('grid-cell')).toHaveCount(EMPTY_LISTS.length);
        await expect.poll(() => shownSamples(page)).toEqual(EMPTY_LISTS);
    });
});

type Cell = { raw: string; value: string };

/** The file's records by RFC 4180's rules (quoted cells, doubled quotes), each cell with its text and its value. */
function parseCsv(text: string): Cell[][] {
    const records: Cell[][] = [];
    let record: Cell[] = [];
    let i = 0;
    for (;;) {
        const start = i;
        let value = '';
        if (text[i] === '"') {
            i += 1;
            for (;;) {
                if (i >= text.length) throw new Error('a quoted cell never closes');
                if (text[i] === '"') {
                    if (text[i + 1] === '"') { value += '"'; i += 2; continue; }
                    i += 1;
                    break;
                }
                value += text[i];
                i += 1;
            }
        } else {
            while (i < text.length && !',\r\n'.includes(text[i])) { value += text[i]; i += 1; }
        }
        record.push({ raw: text.slice(start, i), value });
        if (i >= text.length) { records.push(record); break; }
        if (text[i] === ',') { i += 1; continue; }
        i += text.startsWith('\r\n', i) ? 2 : 1;
        records.push(record);
        record = [];
        if (i >= text.length) break;
    }
    return records;
}

test.describe('the Export dialog', () => {
    let cookie = '';
    let datasetId = 0;
    // name: (the list column sent, undefined for never set; the metadata list, undefined for no key)
    const ROWS: Record<string, [string[] | undefined, string[] | undefined]> = {
        unset: [undefined, undefined],
        empty: [[], []],
        listed: [['aphid', 'mite'], ['x', 'y']],
    };
    const LIST = 'found';  // a list in the metadata

    test.beforeAll(async () => {
        test.setTimeout(120_000);
        cookie = await adminLogin();
        const made = await makeDataset(cookie, 'e2e-list-column-csv', Object.entries(ROWS).map(([name, [column, list]]) => ({
            filepath: `s3://e2e-list-column-csv/${name}.jpg`,
            metadata: { plot: name, ...(list === undefined ? {} : { [LIST]: list }) },
            ...(column === undefined ? {} : { array_fields: { [FIELD]: column } }),
        })));
        datasetId = made.id;
        await expect.poll(async () => {
            const listed = await fieldEntries(cookie, datasetId);
            return [FIELD, LIST].filter(field => !listed.some(f => f.name === field));
        }, { timeout: 60_000, message: 'the fields the schema has yet to list' }).toEqual([]);
    });

    test.afterAll(async () => {
        if (datasetId) await deleteDatasetAndWait(cookie, datasetId);
    });

    test('a list never set writes an empty cell and an empty list writes [], in a list column and in the metadata', async ({ page }) => {
        test.setTimeout(120_000);
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, Object.keys(ROWS).length);
        await page.getByTitle('Export samples as CSV').click();
        const dialog = page.getByRole('dialog');
        await expect(dialog.getByText('Whole dataset')).toBeVisible();
        const box = (name: string) => dialog.getByRole('checkbox', { name, exact: true });
        // Columns come in the order their fields are ticked; filepath is ticked when the dialog opens.
        await expect(box('filepath')).toBeChecked();
        for (const name of [FIELD, LIST, 'tags']) {
            await expect(box(name)).toBeVisible({ timeout: 15_000 });
            await box(name).check();
        }
        const downloading = page.waitForEvent('download');
        await dialog.getByRole('button', { name: /Export CSV/ }).click();
        const download = await downloading;
        const chunks: Buffer[] = [];
        for await (const chunk of await download.createReadStream()) chunks.push(Buffer.from(chunk));
        const [header, ...rows] = parseCsv(Buffer.concat(chunks).toString('utf-8'));
        expect(header.map(cell => cell.value)).toEqual(['filepath', FIELD, LIST, 'tags']);
        const byName: Record<string, string[]> = Object.fromEntries(
            rows.map(row => [/\/([^/]+)\.jpg$/.exec(row[0].value)?.[1] ?? row[0].value, row.map(cell => cell.value)]));
        expect(Object.keys(byName).sort()).toEqual(Object.keys(ROWS).sort());

        // Asked first, as main writes them: a list with elements, and a sample with no tags (an empty cell).
        expect(byName.listed.slice(1)).toEqual(['aphid;mite', 'x;y', '']);
        expect(byName.unset[3]).toBe('');
        // Never set (no list, no key): an empty cell. An empty list: [].
        expect(byName.unset.slice(1, 3), 'a list never set').toEqual(['', '']);
        expect(byName.empty.slice(1, 3), 'an empty list').toEqual(['[]', '[]']);
        expect(byName.empty[3], "a sample's tags when it has none, as before").toBe('');
    });
});
