import { test, expect, type Page } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, fillStable } from './helpers';
import { stamp } from './access-helpers';

// On a dataset with objects, a filter is read on an image together with its objects (contract g5, section 6.4). The
// owner's five images, on a dataset of their own: 1 red with a car and a truck, 2 red with a truck, 3 blue with a car,
// 4 with no colour and a car, 5 red with no objects. One filter card holding `colour = "red" AND label = "car"` shows
// the red images that have a car (image 1) and, on the Objects grid, their cars (image 1's car), with the counts.
// A card with an object field alone is asked first, as main already answers it, so a failure of the second card is
// the reading's, not the page's.
test.use({ storageState: AUTH_STATE });

const S = stamp();
const TEXT = 'colour = "red" AND label = "car"';
const CONTROL = 'label = "car"';
// [name, colour (null: none), its objects' labels]
const IMAGES: Array<[string, string | null, string[]]> = [
    ['img-1', 'red', ['car', 'truck']],
    ['img-2', 'red', ['truck']],
    ['img-3', 'blue', ['car']],
    ['img-4', null, ['car']],
    ['img-5', 'red', []],
];
let cookie = '';
let datasetId = 0;

const filepath = (name: string) => `s3://e2e-objects-filter-reading-${S}/${name}.jpg`;
const nameOf = (path: string) => path.slice(path.lastIndexOf('/') + 1).replace('.jpg', '');

test.beforeAll(async () => {
    test.setTimeout(120_000);
    cookie = await adminLogin();
    ({ id: datasetId } = await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name: `e2e-objects-filter-reading-${S}` }),
    }));
    const images = IMAGES.map(([name, colour]) => ({ filepath: filepath(name), metadata: colour ? { colour } : {} }));
    const { ids } = await apiFetch(cookie, `/datasets/${datasetId}/samples`, { method: 'POST', body: JSON.stringify({ samples: images }) });
    const objects = IMAGES.flatMap(([name, , labels], i) => labels.map(label => ({
        filepath: `${filepath(name)}#${label}`, media_type: 'object', metadata: { parent_id: (ids as number[])[i], label },
    })));
    await apiFetch(cookie, `/datasets/${datasetId}/samples`, { method: 'POST', body: JSON.stringify({ samples: objects }) });
    await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
    // The page reads the field statistics as it opens (the Query tab knows the fields from them): wait until they are
    // counted, object rows included.
    await expect.poll(async () => {
        const fields = await apiFetch(cookie, `/datasets/${datasetId}/field-stats`) as Array<{ name: string; stats_pending?: boolean; values?: string[] }>;
        const media = fields.find(field => field.name === 'media_type');
        return fields.every(field => !field.stats_pending) && ['image', 'object'].every(kind => media?.values?.includes(kind))
            && ['colour', 'label'].every(name => fields.some(field => field.name === name));
    }, { timeout: 60_000, message: 'field statistics' }).toBe(true);
});

test.afterAll(async () => {
    test.setTimeout(120_000);
    if (datasetId) await deleteDatasetAndWait(cookie, datasetId, 90_000);
});

/** The grids' queries as the page sends them (the ones that list samples), newest last. */
function gridQueries(page: Page): Array<Record<string, unknown>> {
    const sent: Array<Record<string, unknown>> = [];
    page.on('request', request => {
        if (request.method() !== 'POST' || !request.url().includes('/samples/query')) return;
        const body = request.postDataJSON() as Record<string, unknown> | null;
        if (body && typeof body.limit === 'number' && body.limit > 1) sent.push(body);
    });
    return sent;
}

/** The rows the grid of one kind shows: its newest query (the one led by that kind's predicate), sent again. */
async function shown(sent: Array<Record<string, unknown>>, grain: 'image' | 'object'): Promise<string[]> {
    const operator = grain === 'object' ? '=' : '!=';
    const mine = sent.filter(body => {
        const first = ((body.filters as { filters?: Array<Record<string, unknown>> })?.filters ?? [])[0];
        return first?.field === 'media_type' && first?.operator === operator;
    });
    expect(mine.length, `the ${grain} grid sent no query`).toBeGreaterThan(0);
    const answer = await apiFetch(cookie, `/datasets/${datasetId}/samples/query`, { method: 'POST', body: JSON.stringify(mine[mine.length - 1]) });
    return (answer.items as Array<{ filepath: string }>).map(item => nameOf(item.filepath)).sort();
}

async function applyCard(page: Page, text: string) {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    const box = page.getByTestId('filter-card-query');
    await fillStable(box, text);
    await box.press('Control+Enter');
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);
}

async function grain(page: Page, name: 'Images' | 'Objects') {
    await page.getByTestId('grain-toggle').getByRole('button', { name }).click();
}

/** The grid shows `count` (its text, e.g. "1 image"), that many cells, and these rows. */
async function expectGrid(page: Page, sent: Array<Record<string, unknown>>, kind: 'image' | 'object', rows: string[]) {
    const count = page.getByTestId('grid-view-count');
    const text = `${rows.length} ${kind}${rows.length === 1 ? '' : 's'}`;
    await expect(count).toHaveText(new RegExp(`^${text}$`), { timeout: 20_000 });
    await expect(page.getByTestId('grid-cell')).toHaveCount(rows.length, { timeout: 20_000 });
    await expect.poll(() => shown(sent, kind), { message: `${kind} grid rows`, timeout: 15_000 }).toEqual([...rows].sort());
}

test('one filter card with colour = "red" AND label = "car" shows the red images that have a car, and their cars', async ({ page }) => {
    test.setTimeout(150_000);
    const sent = gridQueries(page);
    await page.goto(`/datasets/${datasetId}`);
    await expectGrid(page, sent, 'image', ['img-1', 'img-2', 'img-3', 'img-4', 'img-5']);

    // The control: an object field alone.
    await applyCard(page, CONTROL);
    await expectGrid(page, sent, 'image', ['img-1', 'img-3', 'img-4']);
    await grain(page, 'Objects');
    await expectGrid(page, sent, 'object', ['img-1#car', 'img-3#car', 'img-4#car']);
    await grain(page, 'Images');
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(0);
    await expectGrid(page, sent, 'image', ['img-1', 'img-2', 'img-3', 'img-4', 'img-5']);

    // One card, an image field and an object field: image 1 (red, with a car); on the Objects grid, its car.
    await applyCard(page, TEXT);
    await expectGrid(page, sent, 'image', ['img-1']);
    await grain(page, 'Objects');
    await expectGrid(page, sent, 'object', ['img-1#car']);
});
