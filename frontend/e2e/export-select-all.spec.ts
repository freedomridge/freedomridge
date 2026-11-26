import { randomUUID } from 'node:crypto';
import { test, expect, type Download, type Locator, type Page } from '@playwright/test';
import { AUTH_STATE, expectTotal } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// Export's "Select all" means every field, also the ones that arrive after the click (contract "Export's Select all
// means every field", section 1, one test per rule; surface section 3). Written from the contract, not from the code.
//
// The Export dialog lists the fields of the page's field list, which the page builds from the answer to
// POST /api/datasets/<id>/field-stats (the requests without `stream`; the streamed ones are the sidebar's per-field
// values). The page asks for it again when one of the dataset's jobs ends. So a field list that arrives in two parts
// is made like this:
// - every field-stats answer is shortened while the test holds the late fields back: `b_late` and `z_late` are taken
//   out of it (one sorts between the two first fields, one after them), and the dialog lists the system fields,
//   `a_first` and `m_first`;
// - after the clicks under test, the test stops holding and ends a job of the dataset's through the API (a bulk
//   metadata write that writes each sample's `a_first` back as it was, as a script or another tab could), and the
//   page's next field list, now whole, brings the two late fields into the open dialog.
// Each test waits for the late fields to be listed before it checks their boxes, and checks that they were not listed
// before. The page notices a finished job when it next reads the dataset's job history, every 30 s while idle.
//
// What each test checks is what a user sees: the tick boxes, the "Fields (N selected)" count, and the header and cells
// of the downloaded file. Where a test checks behaviour that is new (rules 1 and 3), the boxes and the count are soft
// checks, so a run reports them and still exports; the file's header is a hard check.
//
// Readings of the contract (also in the report to its owner):
// - "The button that puts the selection back to its start" (rule 2) is the dialog's Reset.
// - Rule 5 keeps the file's order "as it has today". Where a field that arrived after Select all goes among the
//   columns is not settled by that, so the late columns are found by name; the other columns keep today's order
//   (Select all: the system fields, then the metadata fields sorted by name; a hand tick: the order of the ticks).
// - Rule 4's "the dialog opens with the same fields ticked as today": `filepath` alone.
test.use({ storageState: AUTH_STATE });

const SYSTEM = ['filepath', 'id', 'media_type', 'tags', 'created_at', 'last_modified_at'];
const FIRST = ['a_first', 'm_first'];  // listed from the start
const LATE = ['b_late', 'z_late'];  // held back until the test lets them arrive
const PART_ONE = [...SYSTEM, ...FIRST];  // the dialog's list before the late fields arrive, in today's order
const EVERY = [...SYSTEM, ...[...FIRST, ...LATE].sort()];  // the whole list, in today's Select all order

const SAMPLES = [
    { tags: ['blue', 'red'], metadata: { a_first: 3, m_first: 'plain', b_late: 'with, comma', z_late: 1.5 } },
    { tags: [], metadata: { a_first: -2, m_first: 'say "hi"', b_late: 'b1', z_late: 0 } },
    { tags: ['solo'], metadata: { a_first: 10, b_late: '=1+1', z_late: 2.25 } },  // no m_first: an empty cell
];
const filepath = (i: number) => `s3://e2e-export-select-all/r${i}.jpg`;
// Each sample's cells as today's file writes them (texts quoted, numbers bare, a list joined with `;`, nothing for no
// value, an apostrophe before a formula sign). id, created_at and last_modified_at come from the server.
const CELLS: Record<string, string>[] = [
    { filepath: `"${filepath(0)}"`, media_type: '"image"', tags: '"blue;red"', a_first: '3', m_first: '"plain"', b_late: '"with, comma"', z_late: '1.5' },
    { filepath: `"${filepath(1)}"`, media_type: '"image"', tags: '', a_first: '-2', m_first: '"say ""hi"""', b_late: '"b1"', z_late: '0' },
    { filepath: `"${filepath(2)}"`, media_type: '"image"', tags: '"solo"', a_first: '10', m_first: '', b_late: `"'=1+1"`, z_late: '2.25' },
];

type Fixture = { id: number; ids: number[]; name: string };
let cookie = '';
const made: number[] = [];

test.beforeAll(async () => {
    cookie = await adminLogin();
});

test.afterEach(async () => {
    while (made.length) await deleteDatasetAndWait(cookie, made.pop()!);
});

/** A dataset of its own (names are unique per run, so repeated runs can go side by side), once the server lists all
 * four metadata fields. */
async function makeDataset(): Promise<Fixture> {
    const name = `e2e-export-select-all-${randomUUID().slice(0, 8)}`;
    const id: number = (await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name, description: 'Export Select all fixture' }),
    })).id;
    made.push(id);
    const { ids } = await apiFetch(cookie, `/datasets/${id}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: SAMPLES.map((s, i) => ({ filepath: filepath(i), ...s })) }),
    });
    await apiFetch(cookie, `/datasets/${id}/refresh-stats`, { method: 'POST' });
    await expect.poll(async () => {
        const listed: { name: string }[] = await apiFetch(cookie, `/datasets/${id}/field-stats`, { method: 'POST', body: JSON.stringify({}) });
        return [...FIRST, ...LATE].filter(field => !listed.some(f => f.name === field));
    }, { timeout: 60_000, message: 'the fields the server has yet to list' }).toEqual([]);
    return { id, ids, name };
}

/**
 * Shortens every field list the page is sent for this dataset to the first part, until `release`. The streamed
 * field-stats requests (the sidebar's values of a field) go through as they are.
 */
async function holdBackLateFields(page: Page, datasetId: number) {
    let holding = true;
    let shortened = 0;
    await page.route(`**/api/datasets/${datasetId}/field-stats`, async route => {
        const body = route.request().postDataJSON() as { stream?: boolean; fields?: string[] } | null;
        if (!holding || body?.stream || body?.fields) return route.fallback();
        let response;
        try {
            response = await route.fetch();
        } catch {
            await route.abort().catch(() => {});  // the page gave up on it
            return;
        }
        if (!response.ok()) return route.fulfill({ response }).catch(() => {});
        const listed: { name: string }[] = await response.json();
        shortened += 1;
        await route.fulfill({ response, json: listed.filter(f => !LATE.includes(f.name)) }).catch(() => {});
    });
    return {
        release: () => { holding = false; },
        shortened: () => shortened,
    };
}

/**
 * Ends a job of the dataset's: a bulk metadata write, run as a job, that writes each sample's `a_first` back as it
 * was. The page asks for its field list again when it sees the job has ended.
 */
async function endAJob(ds: Fixture) {
    const { job_id: jobId } = await apiFetch(cookie, `/datasets/${ds.id}/samples/bulk-set`, {
        method: 'POST',
        body: JSON.stringify({
            updates: ds.ids.map((sampleId, i) => ({ sample_id: sampleId, metadata: { a_first: SAMPLES[i].metadata.a_first } })),
            background: true,
        }),
    });
    expect(jobId, 'the write runs as a job').toBeTruthy();
    await expect.poll(async () => (await apiFetch(cookie, `/jobs/${jobId}`)).state, { timeout: 60_000, message: 'the job ends' })
        .toBe('succeeded');
}

const fieldBox = (dialog: Locator, name: string) => dialog.getByRole('checkbox', { name, exact: true });
const fieldCount = (dialog: Locator) => dialog.getByText(/^Fields \(\d+ selected\)$/);

/** The dataset's page with the Export dialog open, once it lists `listed` (and none of `unlisted`). */
async function openDialog(page: Page, ds: Fixture, listed: string[], unlisted: string[] = []) {
    await page.goto(`/datasets/${ds.id}`);
    await expectTotal(page, SAMPLES.length);
    await page.getByTitle('Export samples as CSV').click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('Whole dataset')).toBeVisible();
    for (const field of listed) await expect(fieldBox(dialog, field)).toBeVisible({ timeout: 15_000 });
    await expect(dialog.getByRole('checkbox')).toHaveCount(listed.length);
    for (const field of unlisted) await expect(fieldBox(dialog, field), `${field} is not listed yet`).toHaveCount(0);
    return dialog;
}

/** Lets the late fields arrive: stops holding them back, ends a job, and waits until the dialog lists them. */
async function letTheRestArrive(dialog: Locator, ds: Fixture, held: { release: () => void; shortened: () => number }) {
    expect(held.shortened(), 'the page was sent a shortened field list').toBeGreaterThan(0);
    held.release();
    await endAJob(ds);
    for (const field of LATE) await expect(fieldBox(dialog, field), `${field} arrives`).toBeVisible({ timeout: 75_000 });
    await expect(dialog.getByRole('checkbox')).toHaveCount(EVERY.length);
}

/** Presses Export CSV and reads the downloaded file. */
async function exportFile(page: Page, dialog: Locator) {
    const downloading = page.waitForEvent('download');
    await dialog.getByRole('button', { name: /Export CSV/ }).click();
    const download: Download = await downloading;
    const chunks: Buffer[] = [];
    for await (const chunk of await download.createReadStream()) chunks.push(Buffer.from(chunk));
    return { name: download.suggestedFilename(), text: Buffer.concat(chunks).toString('utf-8') };
}

/** The file's records by RFC 4180's rules (quoted cells, doubled quotes), each cell as written. */
function parseCsv(text: string): string[][] {
    const records: string[][] = [];
    let record: string[] = [];
    let i = 0;
    for (;;) {
        const start = i;
        if (text[i] === '"') {
            i += 1;
            while (i < text.length && !(text[i] === '"' && text[i + 1] !== '"')) i += text[i] === '"' ? 2 : 1;
            i += 1;
        } else {
            while (i < text.length && !',\r\n'.includes(text[i])) i += 1;
        }
        record.push(text.slice(start, i));
        if (i >= text.length) { records.push(record); break; }
        if (text[i] === ',') { i += 1; continue; }
        i += text.startsWith('\r\n', i) ? 2 : 1;
        records.push(record);
        record = [];
        if (i >= text.length) break;  // a line break after the last row
    }
    return records;
}

const unquote = (cell: string) => (cell.startsWith('"') ? cell.slice(1, -1).replace(/""/g, '"') : cell);

/** Each sample's cells as today's file writes them, the server's id and timestamps included. */
async function expectedCells(ds: Fixture): Promise<Record<string, string>[]> {
    const { items } = await apiFetch(cookie, `/datasets/${ds.id}/samples/query`, {
        method: 'POST',
        body: JSON.stringify({ limit: SAMPLES.length + 1, fields: 'created_at,last_modified_at', slim: true }),
    });
    const byId = new Map<number, { created_at: string; last_modified_at: string }>(items.map((item: { id: number; created_at: string; last_modified_at: string }) => [item.id, item]));
    return CELLS.map((cells, i) => ({
        ...cells,
        id: String(ds.ids[i]),
        created_at: `"${byId.get(ds.ids[i])?.created_at}"`,
        last_modified_at: `"${byId.get(ds.ids[i])?.last_modified_at}"`,
    }));
}

/** The file's header names, and each row's cell of each column as written. */
function columns(text: string) {
    const [header, ...rows] = parseCsv(text);
    const names = header.map(unquote);
    return { names, rows: rows.map(row => Object.fromEntries(names.map((name, n) => [name, row[n]]))) };
}

/** The file holds exactly `fields`; the ones not in `anyPlace` in this order, those anywhere. Every cell as today. */
async function expectFile(file: { name: string; text: string }, ds: Fixture, fields: string[], anyPlace: string[] = []) {
    expect(file.name).toBe(`${ds.name}_${SAMPLES.length}_all.csv`);
    const { names, rows } = columns(file.text);
    expect([...names].sort(), 'the file holds a column for each ticked field, and no other').toEqual([...fields].sort());
    expect(names.filter(name => !anyPlace.includes(name)), 'the columns in today\'s order')
        .toEqual(fields.filter(name => !anyPlace.includes(name)));
    const cells = await expectedCells(ds);
    expect(rows, 'one row per sample').toHaveLength(SAMPLES.length);
    rows.forEach((row, i) => {
        expect(Object.keys(row), `row ${i + 1}: one cell per column`).toHaveLength(names.length);
        for (const name of names) expect(row[name], `row ${i + 1}, ${name}`).toBe(cells[i][name]);
    });
}

test('rule 1: after Select all every listed field is ticked, and a field that appears later is ticked when it appears', async ({ page }) => {
    test.setTimeout(180_000);
    const ds = await makeDataset();
    const held = await holdBackLateFields(page, ds.id);
    const dialog = await openDialog(page, ds, PART_ONE, LATE);

    await dialog.getByRole('button', { name: 'Select all' }).click();
    for (const field of PART_ONE) await expect(fieldBox(dialog, field), `${field} ticked by Select all`).toBeChecked();
    await expect(fieldCount(dialog)).toHaveText(`Fields (${PART_ONE.length} selected)`);

    await letTheRestArrive(dialog, ds, held);
    for (const field of LATE) {
        await expect.soft(fieldBox(dialog, field), `${field}, which arrived after Select all, is ticked when it appears`).toBeChecked();
    }
    for (const field of PART_ONE) await expect.soft(fieldBox(dialog, field), `${field} stays ticked`).toBeChecked();
    await expect.soft(fieldCount(dialog), 'the count follows').toHaveText(`Fields (${EVERY.length} selected)`);

    await expectFile(await exportFile(page, dialog), ds, EVERY, LATE);
});

test('rule 2: unticking a field, or Reset, after Select all ends it: the ticks are what the user set, and a later field is not ticked', async ({ page }) => {
    test.setTimeout(300_000);
    const ds = await makeDataset();

    // Unticking one field after Select all, before the rest arrives.
    let held = await holdBackLateFields(page, ds.id);
    let dialog = await openDialog(page, ds, PART_ONE, LATE);
    await dialog.getByRole('button', { name: 'Select all' }).click();
    for (const field of PART_ONE) await expect(fieldBox(dialog, field)).toBeChecked();
    await fieldBox(dialog, 'm_first').uncheck();
    const kept = PART_ONE.filter(field => field !== 'm_first');
    for (const field of kept) await expect(fieldBox(dialog, field), `${field} stays ticked when another is unticked`).toBeChecked();
    await expect(fieldBox(dialog, 'm_first')).not.toBeChecked();
    await expect(fieldCount(dialog)).toHaveText(`Fields (${kept.length} selected)`);

    await letTheRestArrive(dialog, ds, held);
    for (const field of LATE) await expect(fieldBox(dialog, field), `${field} arrived after an untick: not ticked`).not.toBeChecked();
    await expect(fieldBox(dialog, 'm_first')).not.toBeChecked();
    for (const field of kept) await expect(fieldBox(dialog, field)).toBeChecked();
    await expect(fieldCount(dialog)).toHaveText(`Fields (${kept.length} selected)`);
    await expectFile(await exportFile(page, dialog), ds, kept);
    await expect(dialog).toBeHidden();

    // Reset after Select all, before the rest arrives: the selection is back to its start.
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    held = await holdBackLateFields(page, ds.id);
    dialog = await openDialog(page, ds, PART_ONE, LATE);
    await dialog.getByRole('button', { name: 'Select all' }).click();
    for (const field of PART_ONE) await expect(fieldBox(dialog, field)).toBeChecked();
    await dialog.getByRole('button', { name: 'Reset' }).click();
    await expect(fieldBox(dialog, 'filepath')).toBeChecked();
    for (const field of PART_ONE.slice(1)) await expect(fieldBox(dialog, field), `${field} unticked by Reset`).not.toBeChecked();
    await expect(fieldCount(dialog)).toHaveText('Fields (1 selected)');

    await letTheRestArrive(dialog, ds, held);
    for (const field of LATE) await expect(fieldBox(dialog, field), `${field} arrived after Reset: not ticked`).not.toBeChecked();
    await expect(fieldBox(dialog, 'filepath')).toBeChecked();
    for (const field of PART_ONE.slice(1)) await expect(fieldBox(dialog, field)).not.toBeChecked();
    await expect(fieldCount(dialog)).toHaveText('Fields (1 selected)');
    await expectFile(await exportFile(page, dialog), ds, ['filepath']);
});

test('rule 3: pressing Select all again starts rule 1 again', async ({ page }) => {
    test.setTimeout(180_000);
    const ds = await makeDataset();
    const held = await holdBackLateFields(page, ds.id);
    const dialog = await openDialog(page, ds, PART_ONE, LATE);

    await dialog.getByRole('button', { name: 'Select all' }).click();
    await fieldBox(dialog, 'm_first').uncheck();  // ends rule 1 (rule 2)
    await expect(fieldCount(dialog)).toHaveText(`Fields (${PART_ONE.length - 1} selected)`);
    await dialog.getByRole('button', { name: 'Select all' }).click();
    for (const field of PART_ONE) await expect(fieldBox(dialog, field), `${field} ticked by the second Select all`).toBeChecked();
    await expect(fieldCount(dialog)).toHaveText(`Fields (${PART_ONE.length} selected)`);

    await letTheRestArrive(dialog, ds, held);
    for (const field of LATE) {
        await expect.soft(fieldBox(dialog, field), `${field}, which arrived after the second Select all, is ticked when it appears`).toBeChecked();
    }
    for (const field of PART_ONE) await expect.soft(fieldBox(dialog, field), `${field} stays ticked`).toBeChecked();
    await expect.soft(fieldCount(dialog), 'the count follows').toHaveText(`Fields (${EVERY.length} selected)`);

    await expectFile(await exportFile(page, dialog), ds, EVERY, LATE);
});

test('rule 4: without Select all nothing changes: the same ticks on opening, ticking by hand as today, a later field unticked', async ({ page }) => {
    test.setTimeout(180_000);
    const ds = await makeDataset();
    const held = await holdBackLateFields(page, ds.id);
    const dialog = await openDialog(page, ds, PART_ONE, LATE);

    // As today: the dialog opens with filepath ticked.
    await expect(fieldBox(dialog, 'filepath')).toBeChecked();
    for (const field of PART_ONE.slice(1)) await expect(fieldBox(dialog, field), `${field} unticked on opening`).not.toBeChecked();
    await expect(fieldCount(dialog)).toHaveText('Fields (1 selected)');
    await fieldBox(dialog, 'a_first').check();
    await expect(fieldBox(dialog, 'a_first')).toBeChecked();
    await expect(fieldCount(dialog)).toHaveText('Fields (2 selected)');

    await letTheRestArrive(dialog, ds, held);
    for (const field of LATE) await expect(fieldBox(dialog, field), `${field} arrived without Select all: not ticked`).not.toBeChecked();
    for (const field of ['filepath', 'a_first']) await expect(fieldBox(dialog, field)).toBeChecked();
    for (const field of PART_ONE.filter(f => f !== 'filepath' && f !== 'a_first')) await expect(fieldBox(dialog, field)).not.toBeChecked();
    await expect(fieldCount(dialog)).toHaveText('Fields (2 selected)');

    await expectFile(await exportFile(page, dialog), ds, ['filepath', 'a_first']);
});

test('rule 5: the file holds the ticked fields in today\'s order and form (a field list complete before the dialog opens)', async ({ page }) => {
    test.setTimeout(120_000);
    const ds = await makeDataset();

    // Select all: the system fields, then the metadata fields sorted by name.
    let dialog = await openDialog(page, ds, EVERY);
    await dialog.getByRole('button', { name: 'Select all' }).click();
    for (const field of EVERY) await expect(fieldBox(dialog, field), `${field} ticked by Select all`).toBeChecked();
    await expect(fieldCount(dialog)).toHaveText(`Fields (${EVERY.length} selected)`);
    let file = await exportFile(page, dialog);
    await expect(dialog).toBeHidden();
    const cells = await expectedCells(ds);
    expect(file.name).toBe(`${ds.name}_${SAMPLES.length}_all.csv`);
    const lines = (text: string) => text.replace(/\r?\n$/, '').split(/\r?\n/);
    expect(lines(file.text), 'the whole file, line by line').toEqual([
        EVERY.map(name => `"${name}"`).join(','),
        ...cells.map(row => EVERY.map(name => row[name]).join(',')),
    ]);

    // Ticked by hand: the columns in the order of the ticks, after filepath (ticked when the dialog opens).
    dialog = await openDialog(page, ds, EVERY);
    await fieldBox(dialog, 'z_late').check();
    await fieldBox(dialog, 'm_first').check();
    await expect(fieldCount(dialog)).toHaveText('Fields (3 selected)');
    file = await exportFile(page, dialog);
    const ticked = ['filepath', 'z_late', 'm_first'];
    expect(lines(file.text), 'the whole file, line by line').toEqual([
        ticked.map(name => `"${name}"`).join(','),
        ...cells.map(row => ticked.map(name => row[name]).join(',')),
    ]);
});
