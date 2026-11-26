import { randomUUID } from 'node:crypto';
import fs from 'fs';
import path from 'path';
import { test, expect, type Locator, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// The Export dialog's CSV never holds a cell a spreadsheet would run as a formula, every text cell is quoted, and the
// rest of the file is as it was (contract "a CSV the app writes can't run as a formula", sections 1 to 3; surface
// 4.2). Written from the contract, not from the code.
//
// 1. A dataset with one sample per case of tests/fixtures/csv-formula-cells.json (section 3's table and what section 1
//    states outright), the case's value in the metadata field `cell`; a number field `amount` (negative numbers) on
//    every sample; a field named `=x` for the header row; one sample tagged `=t` (a list column, 2.1: every cell). It
//    is exported with the dialog (Whole dataset, Select all), and the downloaded file is parsed by RFC 4180's rules
//    (quoted cells, doubled quotes, line breaks inside quotes), keeping each cell's text as written. Each written cell
//    is compared with the contract's, quotes included: a text quoted, a number bare, null and a missing field empty.
// 2. The fixture's whole rows: a dataset whose first exported column is a number field and whose second is a text
//    field. The dialog writes the columns in the order their fields are ticked (filepath is ticked when it opens), so
//    filepath is unticked and the two fields ticked in order. Its raw lines are compared, since what matters is what a
//    reader taking `;` for the separator sees.
//
// The dialog lists the fields of the page's schema (the field-stats call), which lists a new dataset's fields once its
// statistics are counted; "Select all" ticks the fields listed when it is pressed. So each dataset's fields are
// awaited in the schema before the page opens, and in the dialog before anything is ticked, and every tick is checked
// after. Dataset names are unique per run, so repeated runs of this file (--repeat-each) can go side by side.
//
// What must not change (1.5) is checked by the parsed values, as the file is today: the header's fields (the system
// fields then the metadata fields sorted by name, what Select all exports), one row per sample in id order, the name
// `<dataset>_<rows>_all.csv`, each cell's value. Those checks are hard; the written cells are soft, so one run reports
// every case.
//
// Readings of the contract (also in the report to its owner):
// - A sample without the field writes an empty cell, as null does (1.5 names null; absence is read the same).
// - An empty list (a sample's `tags` when it has none) is checked by its value only: whether its empty joined text is
//   written `""` or as an empty cell is not settled by "a cell is written bare only when ... it is empty".
// - The fixture's readings are listed in tests/csv-cells-spec.test.cjs and in each such case's `note`.
// - A trailing line break after the last row is neither required nor refused.
test.use({ storageState: AUTH_STATE });

type Case = { case: string; value: unknown; cell: string; contract: string; where: 'both' | 'export' | 'header'; note?: string };
type WholeRow = { case: string; values: unknown[]; line: string; contract: string };
const { cases, rows: WHOLE_ROWS }: { cases: Case[]; rows: WholeRow[] } = JSON.parse(
    fs.readFileSync(path.join(__dirname, '..', 'tests', 'fixtures', 'csv-formula-cells.json'), 'utf-8'));

type Cell = { raw: string; value: string };

/** The file's records by RFC 4180's rules, each cell with its text as written and its value. */
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
                if (i >= text.length) throw new Error(`a quoted cell never closes: ${JSON.stringify(text.slice(start, start + 60))}`);
                if (text[i] === '"') {
                    if (text[i + 1] === '"') { value += '"'; i += 2; continue; }
                    i += 1;
                    break;
                }
                value += text[i];
                i += 1;
            }
            if (i < text.length && !',\r\n'.includes(text[i])) {
                throw new Error(`text after a closing quote: ${JSON.stringify(text.slice(start, i + 20))}`);
            }
        } else {
            while (i < text.length && !',\r\n'.includes(text[i])) {
                if (text[i] === '"') throw new Error(`a quote inside an unquoted cell: ${JSON.stringify(text.slice(start, i + 20))}`);
                value += text[i];
                i += 1;
            }
        }
        record.push({ raw: text.slice(start, i), value });
        if (i >= text.length) { records.push(record); break; }
        if (text[i] === ',') { i += 1; continue; }
        i += text.startsWith('\r\n', i) ? 2 : 1;
        records.push(record);
        record = [];
        if (i >= text.length) break;  // a line break after the last row
    }
    return records;
}

/** A dataset of these samples, once the schema the dialog lists its fields from holds every one of `fields`. */
async function makeDataset(cookie: string, name: string, samples: object[], fields: string[]): Promise<{ id: number; ids: number[] }> {
    const id = (await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name, description: 'CSV formula cells fixture' }),
    })).id;
    const { ids } = await apiFetch(cookie, `/datasets/${id}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    await apiFetch(cookie, `/datasets/${id}/refresh-stats`, { method: 'POST' });
    await expect.poll(async () => {
        const listed: { name: string }[] = await apiFetch(cookie, `/datasets/${id}/field-stats`, { method: 'POST', body: JSON.stringify({}) });
        return fields.filter(field => !listed.some(f => f.name === field));
    }, { timeout: 60_000, message: 'the fields the schema has yet to list' }).toEqual([]);
    return { id, ids };
}

/** The checkbox of a field in the Export dialog. */
const fieldBox = (dialog: Locator, name: string) => dialog.getByRole('checkbox', { name, exact: true });

/**
 * The dataset's whole-dataset export, its fields chosen by `choose` once the dialog lists every one of `fields`: the
 * file's name and text.
 */
async function exportFile(page: Page, datasetId: number, total: number, fields: string[], choose: (dialog: Locator) => Promise<void>) {
    await page.goto(`/datasets/${datasetId}`);
    await expectTotal(page, total);
    await page.getByTitle('Export samples as CSV').click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('Whole dataset')).toBeVisible();
    for (const field of fields) await expect(fieldBox(dialog, field)).toBeVisible({ timeout: 15_000 });
    await choose(dialog);
    const downloading = page.waitForEvent('download');
    await dialog.getByRole('button', { name: /Export CSV/ }).click();
    const download = await downloading;
    const chunks: Buffer[] = [];
    for await (const chunk of await download.createReadStream()) chunks.push(Buffer.from(chunk));
    return { name: download.suggestedFilename(), text: Buffer.concat(chunks).toString('utf-8') };
}

/** A soft check that also goes into a report printed at the end, so a run lists every cell, held and expected. */
function reporter() {
    const lines: string[] = [];
    const check = (what: string, held: string, expected: string) => {
        lines.push(`${held === expected ? 'ok  ' : 'FAIL'} ${what}: held ${JSON.stringify(held)}, expected ${JSON.stringify(expected)}`);
        expect.soft(held, what).toBe(expected);
    };
    return { check, print: (title: string) => console.log([title, ...lines].join('\n')) };
}

test.describe('every case, one sample each', () => {
    const PREFIX = 'e2e-csv-formula-cells';
    let dsName = '';  // made in beforeAll: one dataset per run of the file
    const HEADER_FIELD = '=x';
    const HEADER_CASE = cases.find(c => c.where === 'header' && c.value === HEADER_FIELD)!;
    const TAG = '=t';
    const TAG_CELL = `"'=t"`;  // a list column's one element, as section 3's list rows write it

    type Row = { name: string; metadata: Record<string, unknown>; tags: string[]; cell: string };
    const ROWS: Row[] = [
        ...cases.filter(c => c.where !== 'header').map(c => ({ name: c.case, metadata: { cell: c.value }, tags: [] as string[], cell: c.cell })),
        { name: 'no cell field (read as null)', metadata: {}, tags: [TAG], cell: '' },
    ];
    const amount = (i: number) => -(i + 1) / 2;  // -0.5, -1, -1.5 ...: a number field's values, written bare
    const filepath = (i: number) => `s3://e2e-csv-cells/r${String(i).padStart(3, '0')}.jpg`;

    // Today's columns, in today's order: Select all exports the system fields, then the metadata fields sorted by name.
    const COLUMNS = ['filepath', 'id', 'media_type', 'tags', 'created_at', 'last_modified_at', ...[HEADER_FIELD, 'amount', 'cell'].sort()];
    const column = (name: string) => COLUMNS.indexOf(name);

    let cookie = '';
    let datasetId = 0;
    let ids: number[] = [];

    test.beforeAll(async () => {
        test.setTimeout(120_000);  // an ingest, and its statistics counted, on a busy server
        cookie = await adminLogin();
        dsName = `${PREFIX}-${randomUUID().slice(0, 8)}`;
        ({ id: datasetId, ids } = await makeDataset(cookie, dsName, ROWS.map((row, i) => ({
            filepath: filepath(i),
            tags: row.tags,
            metadata: { ...row.metadata, amount: amount(i), [HEADER_FIELD]: 'x' },
        })), [HEADER_FIELD, 'amount', 'cell']));
    });

    test.afterAll(async () => {
        if (datasetId) await deleteDatasetAndWait(cookie, datasetId);
    });

    test('the fixture holds what it says: every value as planted, one sample per row', async () => {
        // Guards the guard: a value the server changed on the way in would make a cell check pass or fail for the wrong
        // reason.
        expect(ids).toHaveLength(ROWS.length);
        const body = { limit: ROWS.length + 1, fields: `filepath,metadata.cell,metadata.${HEADER_FIELD}`, slim: true };
        const { items, total } = await apiFetch(cookie, `/datasets/${datasetId}/samples/query`, { method: 'POST', body: JSON.stringify(body) });
        expect(total).toBe(ROWS.length);
        const held = new Map<number, Record<string, unknown>>(items.map((item: { id: number; metadata: Record<string, unknown> }) => [item.id, item.metadata]));
        ROWS.forEach((row, i) => {
            expect(held.get(ids[i])?.cell, row.name).toEqual(row.metadata.cell);
            expect(held.get(ids[i])?.[HEADER_FIELD], row.name).toBe('x');
        });
    });

    test('the Export dialog writes no cell that starts a formula, quotes every text, and the file is otherwise as it was', async ({ page }) => {
        test.setTimeout(120_000);  // a page, a dialog and a file of a hundred rows, on a busy server
        const file = await exportFile(page, datasetId, ROWS.length, COLUMNS, async dialog => {
            await dialog.getByRole('button', { name: 'Select all' }).click();
            for (const name of COLUMNS) await expect(fieldBox(dialog, name), `${name} ticked by Select all`).toBeChecked();
        });

        // --- What must not change (1.5): names, order, rows and values a program parses ---
        expect(file.name).toBe(`${dsName}_${ROWS.length}_all.csv`);
        const [header, ...rows] = parseCsv(file.text);
        // The `=x` column is found whatever its cell holds; the cell itself is checked below.
        expect(header.map(c => (c.value === `'${HEADER_FIELD}` ? HEADER_FIELD : c.value)), 'the header: same fields, same order')
            .toEqual(COLUMNS);
        expect(rows, 'one row per sample').toHaveLength(ROWS.length);
        rows.forEach((row, i) => {
            expect(row, `row ${i + 1}: one cell per column`).toHaveLength(COLUMNS.length);
            expect(row[column('id')].value, `row ${i + 1}: the samples in id order`).toBe(String(ids[i]));
            expect(row[column('filepath')].value).toBe(filepath(i));
            expect(row[column('media_type')].value).toBe('image');
            expect(row[column('amount')].value).toBe(String(amount(i)));
            expect(row[column(HEADER_FIELD)].value).toBe('x');
        });

        // --- Every cell as written ---
        const { check, print } = reporter();
        COLUMNS.forEach((name, n) => check(`header ${name}`, header[n].raw, name === HEADER_FIELD ? HEADER_CASE.cell : `"${name}"`));
        rows.forEach((row, i) => {
            const at = (name: string) => row[column(name)];
            check(`cell: ${ROWS[i].name}`, at('cell').raw, ROWS[i].cell);
            check(`row ${i + 1}: filepath`, at('filepath').raw, `"${filepath(i)}"`);
            check(`row ${i + 1}: id (a number)`, at('id').raw, String(ids[i]));
            check(`row ${i + 1}: media_type`, at('media_type').raw, '"image"');
            check(`row ${i + 1}: amount (a number field)`, at('amount').raw, String(amount(i)));
            check(`row ${i + 1}: =x`, at(HEADER_FIELD).raw, '"x"');
            for (const name of ['created_at', 'last_modified_at']) check(`row ${i + 1}: ${name}`, at(name).raw, `"${at(name).value}"`);
            if (ROWS[i].tags.length) check(`row ${i + 1}: tags`, at('tags').raw, TAG_CELL);
            else check(`row ${i + 1}: tags (none, by value)`, at('tags').value, '');
        });
        print('Export dialog cells:');
    });
});

test.describe('whole rows: a number field first, then a text field', () => {
    const PREFIX = 'e2e-csv-formula-rows';
    let dsName = '';  // made in beforeAll: one dataset per run of the file
    const FIELDS = ['lead', 'payload'];  // a number field, then a text field
    let cookie = '';
    let datasetId = 0;

    test.beforeAll(async () => {
        test.setTimeout(120_000);  // an ingest, and its statistics counted, on a busy server
        cookie = await adminLogin();
        dsName = `${PREFIX}-${randomUUID().slice(0, 8)}`;
        ({ id: datasetId } = await makeDataset(cookie, dsName, WHOLE_ROWS.map((row, i) => ({
            filepath: `s3://e2e-csv-rows/r${i}.jpg`,
            metadata: Object.fromEntries(FIELDS.map((field, n) => [field, row.values[n]])),
        })), FIELDS));
    });

    test.afterAll(async () => {
        if (datasetId) await deleteDatasetAndWait(cookie, datasetId);
    });

    test('each row is the line the contract gives, under a header of quoted names', async ({ page }) => {
        expect(WHOLE_ROWS.every(row => typeof row.values[0] === 'number' && typeof row.values[1] === 'string')).toBe(true);
        const file = await exportFile(page, datasetId, WHOLE_ROWS.length, ['filepath', ...FIELDS], async dialog => {
            // Columns come in the order their fields are ticked; filepath is ticked when the dialog opens.
            await expect(fieldBox(dialog, 'filepath')).toBeChecked();
            await fieldBox(dialog, 'filepath').uncheck();
            for (const field of FIELDS) await fieldBox(dialog, field).check();
            await expect(fieldBox(dialog, 'filepath')).not.toBeChecked();
            for (const field of FIELDS) await expect(fieldBox(dialog, field)).toBeChecked();
        });
        expect(file.name).toBe(`${dsName}_${WHOLE_ROWS.length}_all.csv`);
        // The values a program parses, as today: the number first, the text second.
        expect(parseCsv(file.text).map(record => record.map(c => c.value)))
            .toEqual([FIELDS, ...WHOLE_ROWS.map(row => row.values.map(String))]);
        // The lines as written (none of these values holds a line break).
        const lines = file.text.split(/\r?\n/);
        if (lines[lines.length - 1] === '') lines.pop();
        expect(lines).toHaveLength(WHOLE_ROWS.length + 1);
        const { check, print } = reporter();
        check('header line', lines[0], FIELDS.map(field => `"${field}"`).join(','));
        WHOLE_ROWS.forEach((row, i) => check(row.case, lines[i + 1], row.line));
        print('Export dialog lines:');
    });
});
