import { test, expect, type Page, type Route } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, asLargeDataset, expandField, expectTotal, fulfillFieldStats } from './helpers';
import { stamp } from './access-helpers';

// The sidebar lists a field's values whichever kind of row has the field, and says so when it can't (contract g12,
// sections 6.4, 7.6, 7.7, 7.11, 13, 14 and 15). Written from the contract, not from the code.
//
// A dataset with objects: 120 images, each with a `serial` of its own (sn-000 to sn-119); a car on every third image
// (i % 3 == 0: 40 images), a truck on the next ones (i % 3 == 1), a person on every fifth. `label` is a field only the
// objects have. Where a test needs the picker to ask the server (not answer from the list it holds), the field
// statistics the page reads are rewritten so `serial` holds no list (fulfillFieldStats). The picker's states are given
// by answering the suggestions' route in the test (7.11), as e2e/autocomplete-exhaustive.spec.ts does; a typed term and
// Enter then filter the real grid.
// A dataset without objects: six images, `colour` red x3, blue x2, green x1.
test.use({ storageState: AUTH_STATE });

// A route that fetches the real answer (fulfillFieldStats, asLargeDataset) may still be fetching a request the page
// sent after a test's last assertion: let the routes go, ignoring what such a callback meets once the test is over.
test.afterEach(async ({ page }) => {
    await page.unrouteAll({ behavior: 'ignoreErrors' });
});

const S = stamp();
const IMAGES = 120;
const serial = (i: number) => `sn-${String(i).padStart(3, '0')}`;
const labelsOf = (i: number) => [...(i % 3 === 0 ? ['car'] : []), ...(i % 3 === 1 ? ['truck'] : []), ...(i % 5 === 0 ? ['person'] : [])];
const COLOURS = ['red', 'red', 'red', 'blue', 'blue', 'green'];

// The sentences, exact (14).
const SOME_VALUES = 'Could not search all values in time: these are some of them. Type to search; Enter filters by what you typed.';
const SOME_VALUES_CONTAINS = 'Could not search all values in time: these are some of them. Type to search; Enter or Contains filters by what you typed.';
const NOTHING_TYPED_NOT_ALL = 'Could not search all values in time. Type a value and press Enter to filter by it.';
const TYPED_NOT_ALL = 'Could not search all values in time. Press Enter to filter by the exact value you typed.';  // main's
const TYPED_NOT_ALL_CONTAINS = 'Could not search all values in time. Choose Contains to apply a substring filter.';  // main's

type Answer = { values: Array<{ value: string; count: number | null }>; exhaustive: boolean };
type FieldEntry = Record<string, unknown> & { name: string };

let cookie = '';
let objectsId = 0;
let plainId = 0;

async function statisticsCounted(datasetId: number, names: string[], kinds: string[]) {
    await expect.poll(async () => {
        const fields = await apiFetch(cookie, `/datasets/${datasetId}/field-stats`) as Array<{ name: string; stats_pending?: boolean; values?: string[] }>;
        const media = fields.find(field => field.name === 'media_type');
        return fields.every(field => !field.stats_pending) && kinds.every(kind => media?.values?.includes(kind))
            && names.every(name => fields.some(field => field.name === name));
    }, { timeout: 60_000, message: `field statistics of dataset ${datasetId}` }).toBe(true);
}

test.beforeAll(async () => {
    test.setTimeout(180_000);
    cookie = await adminLogin();
    ({ id: objectsId } = await apiFetch(cookie, '/datasets', {
        method: 'POST', body: JSON.stringify({ name: `e2e-sidebar-field-values-${S}` }),
    }));
    const images = Array.from({ length: IMAGES }, (_, i) => ({
        filepath: `s3://e2e-sidebar-field-values-${S}/${serial(i)}.jpg`, metadata: { serial: serial(i) },
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${objectsId}/samples`, { method: 'POST', body: JSON.stringify({ samples: images }) });
    const objects = images.flatMap((item, i) => labelsOf(i).map(label => ({
        filepath: `${item.filepath}#${label}`, media_type: 'object', metadata: { parent_id: (ids as number[])[i], label },
    })));
    await apiFetch(cookie, `/datasets/${objectsId}/samples`, { method: 'POST', body: JSON.stringify({ samples: objects }) });
    await apiFetch(cookie, `/datasets/${objectsId}/refresh-stats`, { method: 'POST' });

    ({ id: plainId } = await apiFetch(cookie, '/datasets', {
        method: 'POST', body: JSON.stringify({ name: `e2e-sidebar-field-values-plain-${S}` }),
    }));
    const plain = COLOURS.map((colour, i) => ({ filepath: `s3://e2e-sidebar-field-values-plain-${S}/${i}.jpg`, metadata: { colour } }));
    await apiFetch(cookie, `/datasets/${plainId}/samples`, { method: 'POST', body: JSON.stringify({ samples: plain }) });
    await apiFetch(cookie, `/datasets/${plainId}/refresh-stats`, { method: 'POST' });

    await statisticsCounted(objectsId, ['serial', 'label'], ['image', 'object']);
    await statisticsCounted(plainId, ['colour'], ['image']);
});

test.afterAll(async () => {
    test.setTimeout(180_000);
    if (objectsId) await deleteDatasetAndWait(cookie, objectsId, 90_000);
    if (plainId) await deleteDatasetAndWait(cookie, plainId, 90_000);
});

/** The field statistics the page reads, with `serial` holding no list of values: its picker asks the server. */
async function serialHoldsNoList(page: Page) {
    await page.route('**/field-stats', (route: Route) => fulfillFieldStats(route, (field: FieldEntry) => (
        field.name === 'serial' ? { ...field, values: [], values_complete: false } : field
    )));
}

/** The suggestions for `serial` answered by the test, keyed on the typed term (`answers`, read at each request). */
async function answerSerial(page: Page, answers: Map<string, Answer>) {
    await page.route('**/fields/serial/autocomplete', (route: Route) => {
        const q = String((route.request().postDataJSON() as { q?: unknown } | null)?.q ?? '');
        const answer = answers.get(q);
        return answer ? route.fulfill({ json: answer }) : route.continue();
    });
}

const dropdown = (page: Page) => page.locator('[data-searchable-dropdown]');
const sectionBox = (page: Page, name: string) => page.getByTestId('panel-filters').getByTestId(`field-section-${name}`).getByRole('textbox');

test('6.4: a click on a field only objects have lists its values, and ticking one shows the images that have such an object', async ({ page }) => {
    test.setTimeout(120_000);
    await asLargeDataset(page, objectsId);  // sections start collapsed: the click opens `label`
    await page.goto(`/datasets/${objectsId}`);
    await expectTotal(page, IMAGES);
    await expandField(page, 'label');
    const section = page.getByTestId('panel-filters').getByTestId('field-section-label');
    for (const value of ['car', 'truck', 'person']) {
        await expect(section.getByRole('checkbox', { name: value, exact: true })).toBeVisible({ timeout: 20_000 });
    }
    await section.getByRole('checkbox', { name: 'car', exact: true }).check();
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 40);
});

test('15 and 7.6: rows ticked in the grid do not narrow the picker, and a value of an object field ticked but not applied does not empty another field\'s suggestions', async ({ page }) => {
    test.setTimeout(120_000);
    await serialHoldsNoList(page);
    await page.goto(`/datasets/${objectsId}`);
    await expectTotal(page, IMAGES);

    // Two rows ticked in the grid: serial's picker still offers the values of rows not ticked (point 15).
    const cells = page.getByTestId('grid-cell');
    await cells.nth(0).getByTestId('cell-checkbox').click({ force: true });
    await cells.nth(1).getByTestId('cell-checkbox').click({ force: true });
    await expect(page.getByTestId('grid-selection-count')).toContainText('2 selected');
    const serialBox = sectionBox(page, 'serial');
    await serialBox.fill('sn-11');
    for (let i = 110; i < 120; i += 1) {
        await expect(dropdown(page).getByText(serial(i), { exact: true })).toBeVisible({ timeout: 20_000 });
    }
    await serialBox.press('Escape');

    // A draft on `label`, not applied: serial's picker offers the serials of the images that have a car (7.6).
    const labelBox = sectionBox(page, 'label');
    await labelBox.fill('car');
    await labelBox.press('Enter');
    await expect(page.getByTestId('field-section-label').getByText('Contains "car"', { exact: true })).toBeVisible();
    await serialBox.fill('sn-00');
    await expect(dropdown(page).getByText('sn-003', { exact: true })).toBeVisible({ timeout: 20_000 });
    for (const value of ['sn-000', 'sn-006', 'sn-009']) {
        await expect(dropdown(page).getByText(value, { exact: true })).toBeVisible();
    }
    await expect(dropdown(page).getByText('sn-001', { exact: true })).toHaveCount(0);  // a truck's image
});

test('2.1, 7.7 and 14: the query builder\'s picker says each state in its sentence, and Enter takes a typed term when the answer was not complete', async ({ page }) => {
    test.setTimeout(120_000);
    const answers = new Map<string, Answer>();
    await serialHoldsNoList(page);
    await answerSerial(page, answers);
    await page.goto(`/datasets/${objectsId}`);
    await expectTotal(page, IMAGES);
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('serial');
    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.getByRole('button', { name: /^serial/i }).first().click();
    const input = page.getByPlaceholder('Select serial...');

    // Nothing typed, the list not complete.
    answers.set('', { values: [], exhaustive: false });
    await input.click();
    await expect(dropdown(page).getByTestId('autocomplete-not-exhaustive')).toHaveText(NOTHING_TYPED_NOT_ALL, { timeout: 15_000 });
    // A term, nothing, complete.
    answers.set('zzz', { values: [], exhaustive: true });
    await input.fill('zzz');
    await expect(dropdown(page).getByTestId('autocomplete-no-matches')).toHaveText('No matches', { timeout: 15_000 });
    // Nothing typed, nothing, complete: the field has no value for this view.
    answers.set('', { values: [], exhaustive: true });
    await input.fill('');
    await expect(dropdown(page).getByTestId('autocomplete-no-value')).toHaveText('No value for this view.', { timeout: 15_000 });
    // A term, nothing, not complete: main's line, as it is.
    answers.set('yyy', { values: [], exhaustive: false });
    await input.fill('yyy');
    await expect(dropdown(page).getByTestId('autocomplete-not-exhaustive')).toHaveText(TYPED_NOT_ALL, { timeout: 15_000 });
    // Some values, not all: the values, and the sentence. Enter takes what was typed, not the one value listed.
    answers.set('sn-011', { values: [{ value: 'sn-0110', count: null }], exhaustive: false });
    await input.fill('sn-011');
    await expect(dropdown(page).getByTestId('autocomplete-some-values')).toHaveText(SOME_VALUES, { timeout: 15_000 });
    await expect(dropdown(page).getByText('sn-0110', { exact: true })).toBeVisible();
    await input.press('Enter');
    await editor.getByRole('button', { name: 'Apply' }).click();
    await expectTotal(page, 1);
});

test('14: the sidebar\'s picker, which offers Contains, says so in its sentences, and Enter filters the grid by the typed term', async ({ page }) => {
    test.setTimeout(120_000);
    const answers = new Map<string, Answer>();
    await serialHoldsNoList(page);
    await answerSerial(page, answers);
    await page.goto(`/datasets/${objectsId}`);
    await expectTotal(page, IMAGES);
    const box = sectionBox(page, 'serial');

    answers.set('', { values: [], exhaustive: false });
    await box.click();
    await expect(dropdown(page).getByTestId('autocomplete-not-exhaustive')).toHaveText(NOTHING_TYPED_NOT_ALL, { timeout: 15_000 });
    answers.set('sn-02', { values: [], exhaustive: false });
    await box.fill('sn-02');
    await expect(dropdown(page).getByTestId('autocomplete-not-exhaustive')).toHaveText(TYPED_NOT_ALL_CONTAINS, { timeout: 15_000 });
    answers.set('sn-01', { values: [{ value: 'sn-010', count: null }], exhaustive: false });
    await box.fill('sn-01');
    await expect(dropdown(page).getByTestId('autocomplete-some-values')).toHaveText(SOME_VALUES_CONTAINS, { timeout: 15_000 });
    await expect(dropdown(page).getByText('sn-010', { exact: true })).toBeVisible();
    await box.press('Enter');  // Contains "sn-01": sn-010 to sn-019
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 10);
});

test('13 and 15: on a dataset without objects, the picker of a field with an applied rule offers only the values the view has', async ({ page }) => {
    test.setTimeout(120_000);
    await page.goto(`/datasets/${plainId}`);
    await expectTotal(page, COLOURS.length);
    const section = page.getByTestId('panel-filters').getByTestId('field-section-colour');
    await section.getByRole('checkbox', { name: 'red', exact: true }).check();
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 3);
    // The section lists the view's values: the picker no longer holds blue, so a term asks the server.
    await expect(section.getByRole('checkbox', { name: 'red', exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(section.getByRole('checkbox', { name: 'blue', exact: true })).toHaveCount(0, { timeout: 20_000 });
    await section.getByRole('textbox').fill('blu');
    await expect(dropdown(page).getByTestId('autocomplete-no-matches')).toHaveText('No matches', { timeout: 15_000 });
    await expect(dropdown(page).getByText('blue', { exact: true })).toHaveCount(0);
});
