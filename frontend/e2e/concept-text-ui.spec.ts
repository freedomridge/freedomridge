import zlib from 'node:zlib';
import { test, expect, type Locator, type Page } from '@playwright/test';
import { adminLogin, apiFetch, createFixtureDataset, deleteDatasetAndWait, readState } from './api';
import { AUTH_STATE, expectTotal, fillStable } from './helpers';
import { stamp } from './access-helpers';

// A concept filter's text form (docs/FILTER_QUERY.md, "Concept scores"): `concept "equipment" > 0.62` keeps the samples
// whose score passes, `not concept "equipment" > 0.62` hides them. So a view of ordinary filters and concept filters is
// a readable ?q= link (Copy link and the address); such a link opens on the SAME chip the Concepts panel makes, which
// the panel then shows as its concept's filter; a filter card's Query box takes a concept part; a stage that is one
// concept test, from a view made outside the app, is that chip too, and opening such a view doesn't make it look
// changed; a stage naming a concept the dataset no longer has (an old link, a saved view, the default view) is left
// out, with a notice naming it, and the view then differs from what is applied.
//
// The 12-sample fixture: idx 0-5 is split=train, 6-11 val, with embeddings in two separable groups. "equipment" is
// seeded from train (train scores high, val low); "blue sky" from val.
test.use({ storageState: AUTH_STATE, permissions: ['clipboard-read', 'clipboard-write'] });

type Concept = { id: number; name: string; threshold: number; score_min: number; score_max: number };

const S = stamp();
let cookie = '';
let datasetId = 0;
let equipment: Concept;
let blueSky: Concept;
let trainIds: number[] = [];

async function makeConcept(name: string, seedIds: number[]): Promise<Concept> {
    const made = await apiFetch(cookie, `/datasets/${datasetId}/concepts`, {
        method: 'POST',
        body: JSON.stringify({ name, embedding_field: 'e2e_clip', kind: 'seed', seed_ids: seedIds }),
    });
    if (made.job_id) {
        await expect.poll(async () => (await apiFetch(cookie, `/jobs/${made.job_id}`)).state, { timeout: 60_000 }).toBe('succeeded');
    }
    const listed = (await apiFetch(cookie, `/datasets/${datasetId}/concepts`)) as Concept[];
    return listed.find(concept => concept.name === name)!;
}

test.beforeAll(async () => {
    test.setTimeout(180_000);
    cookie = await adminLogin();
    datasetId = await createFixtureDataset(cookie, `e2e-concept-text-${S}`);
    const found = await apiFetch(cookie, `/datasets/${datasetId}/samples/query`, { method: 'POST', body: JSON.stringify({ limit: 12, offset: 0 }) });
    const byIdx = (found.items as Array<{ id: number; metadata: { idx: number } }>).sort((a, b) => a.metadata.idx - b.metadata.idx);
    trainIds = byIdx.slice(0, 6).map(sample => sample.id);
    equipment = await makeConcept('equipment', trainIds);
    blueSky = await makeConcept('blue sky', byIdx.slice(6).map(sample => sample.id));
});

test.afterAll(async () => {
    test.setTimeout(120_000);
    if (datasetId) await deleteDatasetAndWait(cookie, datasetId, 120_000);
});

/** The threshold the panel applies: the concept's saved one when inside its score range, else 85% of the way up. */
const panelThreshold = (concept: Concept) => (concept.threshold >= concept.score_min && concept.threshold <= concept.score_max
    ? concept.threshold
    : Math.round((concept.score_min + 0.85 * (concept.score_max - concept.score_min)) * 1e4) / 1e4);
const number = (value: number) => JSON.stringify(value);

const chips = (page: Page) => page.getByTestId('query-stage-chip');
const q = (page: Page) => new URL(page.url()).searchParams.get('q');
const clipboard = (page: Page) => page.evaluate(() => navigator.clipboard.readText());
const encodeStages = (stages: unknown[]) => `1.${zlib.deflateRawSync(Buffer.from(JSON.stringify({ stages }))).toString('base64url')}`;
const conceptNode = (concept: Concept, operator: 'concept_select' | 'concept_exclude', op: string, threshold: number) => ({
    field: '_concept', operator, value: { concept_id: concept.id, op, threshold },
});

async function open(page: Page, query: string, total: number) {
    await page.goto(`/datasets/${datasetId}${query}`);
    await expectTotal(page, total);
}

/** The dataset with nothing kept from earlier in the test (the filters the app keeps for it in this browser). */
async function openClean(page: Page) {
    await page.evaluate(id => localStorage.removeItem(`fridge-filters-${id}`), datasetId);
    await open(page, '', 12);
}

async function showConcepts(page: Page) {
    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('concepts');
}

const cardOf = (page: Page, name: string) => page.getByTestId('concept-card').filter({ hasText: name });
const press = (card: Locator, label: 'off' | 'exclude' | 'show only' | 'sources') => card.getByRole('button', { name: label, exact: true }).click();

async function addQuery(page: Page, text: string) {
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    const box = page.getByTestId('filter-card-query');
    await fillStable(box, text);
    await box.press('Control+Enter');
}

/** Clicking a concept chip opens no card: no Builder/Query switch appears. */
async function opensNothing(page: Page, chip: Locator) {
    await chip.click();
    await expect(page.getByTestId('filter-mode-query')).toHaveCount(0);
}

test('the panel\'s exclude and show only are a readable q= link, in the address and by Copy link, which opens on the same chip', async ({ page, browser }) => {
    const threshold = number(panelThreshold(equipment));
    await open(page, '', 12);
    await showConcepts(page);
    const card = cardOf(page, 'equipment');
    await press(card, 'exclude');
    await expect(chips(page)).toHaveText([/Exclude: equipment/]);
    await expectTotal(page, 6);
    await expect(card).toContainText('⊘ excl');
    await expect.poll(() => q(page)).toBe(`not concept "equipment" > ${threshold}`);

    await press(card, 'show only');
    await expect(chips(page)).toHaveText([/Only: equipment/]);
    await expectTotal(page, 6);
    await expect(card).toContainText('◉ only');
    await expect.poll(() => q(page)).toBe(`concept "equipment" > ${threshold}`);

    // With an ordinary filter too, in chip order.
    await addQuery(page, 'idx >= 3');
    await expectTotal(page, 3);
    const written = `concept "equipment" > ${threshold} AND idx >= 3`;
    await expect.poll(() => q(page)).toBe(written);
    await page.getByTestId('copy-link').click();
    await expect(page.getByText(/^Link to these filters copied/)).toBeVisible();
    const link = new URL(await clipboard(page));
    expect(link.searchParams.get('q')).toBe(written);
    expect([...link.searchParams.keys()]).toEqual(['q']);

    const fresh = await browser.newContext({ storageState: AUTH_STATE });
    try {
        const other = await fresh.newPage();
        await other.goto(link.href);
        await expectTotal(other, 3);
        await expect(chips(other)).toHaveCount(2);
        await expect(chips(other).first()).toHaveText(/Only: equipment/);
        await expect(other).toHaveURL(link.href);
        await showConcepts(other);
        await expect(cardOf(other, 'equipment')).toContainText('◉ only');
    } finally {
        await fresh.close();
    }
});

test('a q= link with a concept part opens on the panel\'s chip, which the panel then turns around and takes off', async ({ page }) => {
    const saved = number(equipment.threshold);
    // A bare name takes the saved threshold with ">", and the address writes it out.
    await open(page, `?q=${encodeURIComponent('concept equipment')}`, 6);
    await expect(chips(page)).toHaveText([/Only: equipment/]);
    await expect.poll(() => q(page)).toBe(`concept "equipment" > ${saved}`);
    await opensNothing(page, chips(page).first());
    await showConcepts(page);
    const card = cardOf(page, 'equipment');
    await expect(card).toContainText('◉ only');
    await press(card, 'exclude');
    await expect(chips(page)).toHaveText([/Exclude: equipment/]);
    await expectTotal(page, 6);
    await expect.poll(() => q(page)).toBe(`not concept "equipment" > ${saved}`);
    await press(card, 'off');
    await expect(chips(page)).toHaveCount(0);
    await expectTotal(page, 12);

    // NOT (...) around the one concept part is the same exclude chip.
    await open(page, `?q=${encodeURIComponent(`NOT (concept "equipment" > ${saved})`)}`, 6);
    await expect(chips(page)).toHaveText([/Exclude: equipment/]);
    await expect.poll(() => q(page)).toBe(`not concept "equipment" > ${saved}`);
});

test('a "<" comparison from a link survives the panel turning the chip around', async ({ page }) => {
    await open(page, `?q=${encodeURIComponent('concept "equipment" < 0.75')}`, 6);  // the val samples score low
    await expect(chips(page)).toHaveText([/Only: equipment/]);
    await showConcepts(page);
    const card = cardOf(page, 'equipment');
    await expect(card).toContainText('◉ only');
    await press(card, 'exclude');
    await expect(chips(page)).toHaveText([/Exclude: equipment/]);
    await expectTotal(page, 6);
    await expect.poll(() => q(page)).toBe('not concept "equipment" < 0.75');
    await press(card, 'show only');
    await expect.poll(() => q(page)).toBe('concept "equipment" < 0.75');
    await expectTotal(page, 6);
});

test('a filter card\'s Query box takes a concept part: alone it is the panel\'s chip, inside a larger part an ordinary one', async ({ page }) => {
    await open(page, '', 12);
    await addQuery(page, 'not concept "blue sky" > 0.99');
    await expect(chips(page)).toHaveText([/Exclude: blue sky/]);
    await expectTotal(page, 6);  // the val samples score 1 for blue sky
    await opensNothing(page, chips(page).first());
    await expect.poll(() => q(page)).toBe('not concept "blue sky" > 0.99');
    await showConcepts(page);
    await expect(cardOf(page, 'blue sky')).toContainText('⊘ excl');

    await openClean(page);
    await addQuery(page, 'split = "train" OR concept equipment >= 0.95');
    await expect(chips(page)).toHaveText(['split = train or concept equipment ≥ 0.95']);
    await expectTotal(page, 6);
    await chips(page).first().click();
    await page.getByTestId('filter-mode-query').click();
    await expect(page.getByTestId('filter-card-query')).toHaveValue('(split = "train" OR concept "equipment" >= 0.95)');
    await page.getByRole('button', { name: 'Cancel' }).click();

    await openClean(page);
    await addQuery(page, 'split = "train" AND not concept "equipment" > 0.5');
    await expect(chips(page)).toHaveText(['split = train; not concept equipment > 0.5']);
    await expectTotal(page, 0);
});

test('typed mistakes in the Query box show the server\'s words, and nothing changes', async ({ page }) => {
    const tryText = async (text: string, problem: string) => {
        const box = page.getByTestId('filter-card-query');
        await fillStable(box, text);
        await box.press('Control+Enter');
        await expect(page.getByTestId('filter-card-problem'), text).toHaveText(problem);
        await expect(box).toBeVisible();
    };
    await open(page, '', 12);
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    await tryText('concept "equpment" > 0.5', 'Line 1, column 9: unknown concept \'equpment\' (did you mean \'equipment\'?)');
    await tryText('concept equipment = 0.5', 'Line 1, column 19: \'=\' does not apply to the concept \'equipment\'; use one of: >, >=, <, <=');
    await tryText('concept equipment > lots', 'Line 1, column 21: \'lots\' is not a number, and a concept\'s threshold is a number');
    await tryText('concept equipment >', 'Line 1, column 20: expected the concept\'s threshold after \'>\', such as 0.5, found the end of the query');
    await tryText('concept "equipment" 0.5',
        'Line 1, column 21: expected a comparison after concept "equipment", found \'0.5\': a concept\'s score is compared, as in concept "equipment" > 0.5');
    await tryText('concept equipment > 1e999', 'Line 1, column 21: \'1e999\' is out of range for a number');
    await expect(chips(page)).toHaveCount(0);
    await expectTotal(page, 12);

    // The shared fixture has no concepts, and no field named concept.
    await page.goto(`/datasets/${readState().datasetId}`);
    await expect(page.getByTestId('total-samples')).toBeVisible({ timeout: 20_000 });
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByTestId('filter-mode-query').click();
    await tryText('concept x > 0.5', 'Line 1, column 9: unknown concept \'x\'; this dataset has no concepts');
    await tryText('concept > 0.5', 'Line 1, column 1: unknown field \'concept\'; a concept\'s score is written concept "name" > 0.5');
});

/** A view made outside the app (as the SDK saves one): an ordinary stage and a bare concept stage. */
async function outsideView(isPublic: boolean) {
    return apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({
            name: `e2e-outside-${isPublic ? 'public' : 'private'}-${S}`,
            is_public: isPublic,
            content: {
                viewStages: [
                    { type: 'filter', description: 'idx >= 3', filter: { field: 'idx', operator: '>=', value: 3 } },
                    { type: 'filter', description: 'not concept "equipment" > 0.9', filter: conceptNode(equipment, 'concept_exclude', '>', 0.9) },
                ],
            },
        }),
    });
}

test('a view made outside the app with a bare concept stage opens on the panel\'s chip, and doesn\'t look changed until it is', async ({ page, browser }) => {
    const view = await outsideView(true);
    const viewSource = page.getByTestId('view-source');
    try {
        // From Saved Views.
        await open(page, '', 12);
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await page.getByRole('button', { name: new RegExp(`^${view.name}`) }).click();
        await expectTotal(page, 6);  // idx 3-11, without the train ones the concept scores over 0.9
        await expect(chips(page)).toHaveCount(2);
        await expect(chips(page).nth(1)).toHaveText(/Exclude: equipment/);
        await expect.poll(() => [...new URL(page.url()).searchParams.keys()]).toEqual(['view']);
        expect(new URL(page.url()).searchParams.get('view')).toBe(view.slug);
        await expect(page.getByRole('button', { name: 'Close Views' })).toBeVisible();
        await expect(viewSource).toHaveCount(0);
        await expect(page.getByTestId('view-update')).toHaveCount(0);
        await page.getByRole('button', { name: 'Close Views' }).click();
        await showConcepts(page);
        await expect(cardOf(page, 'equipment')).toContainText('⊘ excl');

        // From its link, in a fresh browser.
        const fresh = await browser.newContext({ storageState: AUTH_STATE });
        try {
            const other = await fresh.newPage();
            await other.goto(`/datasets/${datasetId}?view=${view.slug}`);
            await expectTotal(other, 6);
            await expect(chips(other).nth(1)).toHaveText(/Exclude: equipment/);
            await expect.poll(() => [...new URL(other.url()).searchParams.keys()]).toEqual(['view']);
            await other.getByRole('button', { name: 'Saved Views' }).click();
            await expect(other.getByRole('button', { name: 'Close Views' })).toBeVisible();
            await expect(other.getByTestId('view-source')).toHaveCount(0);
        } finally {
            await fresh.close();
        }

        // Changed: the view now differs, and says so.
        await addQuery(page, 'idx >= 0');
        await expectTotal(page, 6);
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await expect(viewSource).toContainText(`The filters differ from the view “${view.name}” they came from.`);
        await expect(page.getByTestId('view-update')).toBeEnabled();
        await expect.poll(() => new URL(page.url()).searchParams.has('view')).toBe(false);
        expect(new URL(page.url()).searchParams.has('q') || new URL(page.url()).searchParams.has('f')).toBe(true);
    } finally {
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});

test('the dataset\'s default view holding a bare concept test opens on the chip with its star; Default view brings it back', async ({ browser }) => {
    const view = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({
            name: `e2e-outside-default-${S}`,
            is_public: true,
            content: { viewStages: [{ type: 'filter', description: 'made by a script', filter: conceptNode(blueSky, 'concept_select', '>', 0.99) }] },
        }),
    });
    const fresh = await browser.newContext({ storageState: AUTH_STATE });
    try {
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: view.id }) });
        const page = await fresh.newPage();
        await open(page, '', 6);
        await expect(chips(page)).toHaveText([/★Only: blue sky/]);
        await expect(chips(page).getByTestId('default-view-star')).toHaveCount(1);
        await page.getByRole('button', { name: 'Show all images' }).click();
        await expectTotal(page, 12);
        await expect(chips(page)).toHaveCount(0);
        await page.getByRole('button', { name: 'Default view', exact: true }).click();
        await expectTotal(page, 6);
        await expect(chips(page)).toHaveText([/★Only: blue sky/]);
    } finally {
        await fresh.close();
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});

test('an f= link with a bare concept stage opens on the chip, and the address becomes q=', async ({ page }) => {
    const stages = [
        { type: 'filter', description: 'idx >= 0', filter: { field: 'idx', operator: '>=', value: 0 } },
        { type: 'filter', description: 'by a script', filter: conceptNode(equipment, 'concept_exclude', '>', 0.9) },
    ];
    await open(page, `?f=${encodeStages(stages)}`, 6);
    await expect(chips(page).nth(1)).toHaveText(/Exclude: equipment/);
    await expect.poll(() => q(page)).toBe('idx >= 0 AND not concept "equipment" > 0.9');
});

test('a link naming a concept the dataset doesn\'t have, or no longer has, opens without its filters and says why', async ({ page }) => {
    // Never had one: the contract's own example.
    await open(page, `?q=${encodeURIComponent('split = "val" AND not concept "gone" > 0.5')}`, 12);
    await expect(page.getByText('This link\'s filter can\'t be used here. Line 1, column 31: unknown concept \'gone\'', { exact: true })).toBeVisible();
    await expect(chips(page)).toHaveCount(0);

    // Deleted after the links were made: a q= link and an old f= link (the only form before) both name it.
    const scrap = await makeConcept(`scrap-${S}`, trainIds);
    const qLink = `?q=${encodeURIComponent(`not concept "scrap-${S}" > 0.9`)}`;
    const fLink = `?f=${encodeStages([{ type: 'filter', description: 'old link', filter: conceptNode(scrap, 'concept_exclude', '>', 0.9) }])}`;
    await apiFetch(cookie, `/datasets/${datasetId}/concepts/${scrap.id}`, { method: 'DELETE' });

    await open(page, qLink, 12);
    await expect(page.getByText(new RegExp(`^This link's filter can't be used here\\. Line 1, column 13: unknown concept 'scrap-${S}'`))).toBeVisible();
    await expect(chips(page)).toHaveCount(0);

    await open(page, fLink, 12);
    await expect(page.getByText(leftOut('this link', ['old link']), { exact: true })).toBeVisible();
    await expect(chips(page)).toHaveCount(0);
    await expect.poll(() => q(page)).toBe('');
});

// A stage naming a concept the dataset no longer has, from a saved view or the default view: the contract's section of
// that name. The concept is made, named by the view's stages, then deleted.
const leftOut = (from: string, labels: string[]) => (labels.length === 1
    ? `From ${from}, “${labels[0]}” was left out: it filters by a concept this dataset no longer has.`
    : `From ${from}, ${labels.map(label => `“${label}”`).join(', ')} were left out: they filter by concepts this dataset no longer has.`);
const idxStage = (operator: string, value: number) => ({ type: 'filter', description: `idx ${operator} ${value}`, filter: { field: 'idx', operator, value } });
/** The Concepts panel's chip as the app saves it (frontend/tests/link-fixtures.json, "concept filter"). */
const panelStage = (concept: Concept) => ({
    type: 'filter',
    description: `Exclude: ${concept.name}`,
    filter: conceptNode(concept, 'concept_exclude', '>', 0.62),
    metadata: { concept: true, conceptId: concept.id, conceptName: concept.name, op: '>', threshold: 0.62, mode: 'exclude' },
});
/** A concept test inside a larger filter. */
const largerStage = (concept: Concept) => ({
    type: 'filter',
    description: `idx < 2 or ${concept.name}`,
    filter: { logic: 'OR', filters: [{ field: 'idx', operator: '<', value: 2 }, conceptNode(concept, 'concept_select', '>', 0.9)] },
});

/** A public view holding the stages `stagesOf` makes for a concept, which is then deleted. */
async function viewNamingGoneConcept(name: string, stagesOf: (concept: Concept) => unknown[]) {
    const concept = await makeConcept(`${name}-concept`, trainIds);
    const view = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({ name, is_public: true, content: { viewStages: stagesOf(concept) } }),
    });
    await apiFetch(cookie, `/datasets/${datasetId}/concepts/${concept.id}`, { method: 'DELETE' });
    return { view, concept };
}

async function chooseView(page: Page, name: string) {
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByRole('button', { name: new RegExp(`^${name}`) }).click();
    await expect(page.getByRole('button', { name: 'Close Views' })).toBeVisible();
}

/** With the Saved Views list open: the applied filters differ from the view, which may be updated. */
async function expectDiffersFrom(page: Page, name: string) {
    await expect(page.getByTestId('view-source')).toContainText(`The filters differ from the view “${name}” they came from.`);
    await expect(page.getByRole('button', { name: 'Update this view', exact: true })).toBeEnabled();
}

test('a saved view holding stages for a deleted concept opens without them, from Saved Views and its link, says which, and differs', async ({ page, browser }) => {
    test.setTimeout(120_000);
    const name = `e2e-with-scrap-${S}`;
    const { view, concept } = await viewNamingGoneConcept(name, made => [idxStage('>=', 3), panelStage(made), largerStage(made)]);
    const notice = leftOut(`the view “${name}”`, [`Exclude: ${concept.name}`, `idx < 2 or ${concept.name}`]);
    try {
        // From Saved Views.
        await open(page, '', 12);
        await chooseView(page, name);
        await expectTotal(page, 9);
        await expect(chips(page)).toHaveText([/idx >= 3/]);
        await expect(page.getByText(notice, { exact: true })).toBeVisible();
        await expect.poll(() => [...new URL(page.url()).searchParams.entries()]).toEqual([['q', 'idx >= 3']]);
        await expectDiffersFrom(page, name);

        // From its ?view= link, in a fresh browser.
        const fresh = await browser.newContext({ storageState: AUTH_STATE });
        try {
            const other = await fresh.newPage();
            await other.goto(`/datasets/${datasetId}?view=${view.slug}`);
            await expectTotal(other, 9);
            await expect(chips(other)).toHaveText([/idx >= 3/]);
            await expect(other.getByText(notice, { exact: true })).toBeVisible();
            await expect.poll(() => [...new URL(other.url()).searchParams.entries()]).toEqual([['q', 'idx >= 3']]);
            await other.getByRole('button', { name: 'Saved Views' }).click();
            await expectDiffersFrom(other, name);
        } finally {
            await fresh.close();
        }
    } finally {
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});

test('the default view holding stages for a deleted concept opens without them and says so once, until the view changes', async ({ browser }) => {
    test.setTimeout(120_000);
    const name = `e2e-default-scrap-${S}`;
    const { view, concept } = await viewNamingGoneConcept(name, made => [idxStage('>=', 3), panelStage(made), largerStage(made)]);
    const notice = leftOut(`the default view “${name}”`, [`Exclude: ${concept.name}`, `idx < 2 or ${concept.name}`]);
    const fresh = await browser.newContext({ storageState: AUTH_STATE });
    try {
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: view.id }) });
        const page = await fresh.newPage();
        await open(page, '', 9);
        await expect(chips(page)).toHaveText([/★idx >= 3/]);
        await expect(page.getByText(notice, { exact: true })).toBeVisible();
        await expect.poll(() => [...new URL(page.url()).searchParams.entries()]).toEqual([['q', 'idx >= 3']]);
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await expectDiffersFrom(page, name);
        await page.getByRole('button', { name: 'Close Views' }).click();
        await page.getByRole('button', { name: 'Show all images' }).click();
        await expectTotal(page, 12);
        await page.getByRole('button', { name: 'Default view', exact: true }).click();
        await expectTotal(page, 9);
        await expect(chips(page)).toHaveText([/★idx >= 3/]);

        // The next visit, in the same browser: the same version of the view, so no notice again.
        await page.goto('/');
        await open(page, '', 9);
        await expect(chips(page)).toHaveText([/★idx >= 3/]);
        await page.waitForLoadState('networkidle');
        await expect.soft(page.getByText(notice, { exact: true }), 'the next visit: the notice is not shown again').toHaveCount(0);

        // The view changed (a new version of its filters, still naming the deleted concept): the next visit says so again.
        await apiFetch(cookie, `/datasets/${datasetId}/views/${view.id}/content`, {
            method: 'PATCH',
            body: JSON.stringify({ content: { viewStages: [idxStage('>=', 4), panelStage(concept), largerStage(concept)] } }),
        });
        await page.goto('/');
        await page.goto(`/datasets/${datasetId}`);
        await expect(page.getByText(notice, { exact: true })).toBeVisible({ timeout: 15_000 });
    } finally {
        await fresh.close();
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: null }) }).catch(() => undefined);
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});

test('a view whose every stage names a deleted concept opens on every sample, says so, and differs from the view', async ({ browser }) => {
    test.setTimeout(120_000);
    const name = `e2e-only-scrap-${S}`;
    const { view, concept } = await viewNamingGoneConcept(name, made => [panelStage(made)]);
    const fresh = await browser.newContext({ storageState: AUTH_STATE });
    try {
        const page = await fresh.newPage();
        await page.goto(`/datasets/${datasetId}?view=${view.slug}`);
        await expectTotal(page, 12);
        await expect(page.getByText(leftOut(`the view “${name}”`, [`Exclude: ${concept.name}`]), { exact: true })).toBeVisible();
        await expect(chips(page)).toHaveCount(0);
        await expect.poll(() => [...new URL(page.url()).searchParams.entries()]).toEqual([['q', '']]);
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await expectDiffersFrom(page, name);
    } finally {
        await fresh.close();
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});

test('a stage holding a deleted concept inside a larger filter goes whole, with the same notice', async ({ page }) => {
    test.setTimeout(120_000);
    const name = `e2e-larger-scrap-${S}`;
    const { view, concept } = await viewNamingGoneConcept(name, made => [idxStage('>=', 3), largerStage(made)]);
    try {
        await open(page, '', 12);
        await chooseView(page, name);
        await expectTotal(page, 9);  // idx 3-11: neither "idx < 2" nor the concept test is left of that stage
        await expect(chips(page)).toHaveText([/idx >= 3/]);
        await expect(page.getByText(leftOut(`the view “${name}”`, [`idx < 2 or ${concept.name}`]), { exact: true })).toBeVisible();
        await expect.poll(() => [...new URL(page.url()).searchParams.entries()]).toEqual([['q', 'idx >= 3']]);
        await expectDiffersFrom(page, name);
    } finally {
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});
