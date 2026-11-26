import { test, expect, type Page, type Route } from '@playwright/test';
import { adminLogin, apiFetch, createFixtureDataset, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expectTotal } from './helpers';
import { stamp } from './access-helpers';

// A view holding a stage for a deleted concept is checked against the dataset's concept list before its stages are
// applied (app/datasets/[id]/page.tsx openedStages). On a slow server, or when the network drops that request once, the
// app waits for the list: no request ever reads the deleted concept, and the view opens without that stage, saying so.
// The 12-sample fixture (e2e/api.ts): idx 0-5 is train, 6-11 val.
test.use({ storageState: AUTH_STATE });

type Concept = { id: number; name: string };

const S = stamp();
let cookie = '';
let datasetId = 0;
let trainIds: number[] = [];

test.beforeAll(async () => {
    test.setTimeout(180_000);
    cookie = await adminLogin();
    datasetId = await createFixtureDataset(cookie, `e2e-concept-slow-list-${S}`);
    const found = await apiFetch(cookie, `/datasets/${datasetId}/samples/query`, { method: 'POST', body: JSON.stringify({ limit: 12, offset: 0 }) });
    trainIds = (found.items as Array<{ id: number; metadata: { idx: number } }>)
        .sort((a, b) => a.metadata.idx - b.metadata.idx).slice(0, 6).map(sample => sample.id);
});

test.afterAll(async () => {
    test.setTimeout(120_000);
    if (datasetId) await deleteDatasetAndWait(cookie, datasetId, 120_000);
});

/** A public view: `idx >= 3`, and `idx < 2 OR <a concept>` for a concept then deleted. */
async function viewNamingGoneConcept(name: string) {
    const made = await apiFetch(cookie, `/datasets/${datasetId}/concepts`, {
        method: 'POST',
        body: JSON.stringify({ name: `${name}-concept`, embedding_field: 'e2e_clip', kind: 'seed', seed_ids: trainIds }),
    });
    if (made.job_id) {
        await expect.poll(async () => (await apiFetch(cookie, `/jobs/${made.job_id}`)).state, { timeout: 60_000 }).toBe('succeeded');
    }
    const concept = ((await apiFetch(cookie, `/datasets/${datasetId}/concepts`)) as Concept[]).find(c => c.name === `${name}-concept`)!;
    const larger = {
        type: 'filter',
        description: `idx < 2 or ${concept.name}`,
        filter: {
            logic: 'OR',
            filters: [
                { field: 'idx', operator: '<', value: 2 },
                { field: '_concept', operator: 'concept_select', value: { concept_id: concept.id, op: '>', threshold: 0.9 } },
            ],
        },
    };
    const view = await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({
            name,
            is_public: true,
            content: { viewStages: [{ type: 'filter', description: 'idx >= 3', filter: { field: 'idx', operator: '>=', value: 3 } }, larger] },
        }),
    });
    await apiFetch(cookie, `/datasets/${datasetId}/concepts/${concept.id}`, { method: 'DELETE' });
    return { view, concept, notice: (from: string) => `From ${from}, “${larger.description}” was left out: it filters by a concept this dataset no longer has.` };
}

/**
 * Hold the dataset's concept list back `delay` ms (the first `drop` requests fail as a dropped connection does), and
 * record every request that names `conceptId` and every refused answer to the app's filter reads.
 */
async function slowList(page: Page, conceptId: number, { delay = 2500, drop = 0 } = {}) {
    let dropped = 0;
    await page.route(`**/api/datasets/${datasetId}/concepts`, async (route: Route) => {
        if (route.request().method() !== 'GET') return route.continue();
        await new Promise(resolve => setTimeout(resolve, delay));
        if (dropped < drop) {
            dropped += 1;
            return route.abort('failed');
        }
        return route.continue();
    });
    const named: string[] = [];
    const refused: string[] = [];
    page.on('request', request => {
        if (request.url().includes('/api/') && (request.postData() ?? request.url()).includes(`"concept_id":${conceptId}`)) {
            named.push(`${request.method()} ${new URL(request.url()).pathname}`);
        }
    });
    page.on('response', response => {
        if (response.url().includes('/api/datasets/') && response.status() >= 400) refused.push(`${response.status()} ${new URL(response.url()).pathname}`);
    });
    return { named, refused, dropped: () => dropped };
}

test('Saved Views: a view naming a deleted concept waits for a slow concept list, and reads nothing it names', async ({ page }) => {
    test.setTimeout(120_000);
    const name = `e2e-slow-saved-${S}`;
    const { view, concept, notice } = await viewNamingGoneConcept(name);
    try {
        const seen = await slowList(page, concept.id);
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 12);
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await page.getByRole('button', { name: new RegExp(`^${name}`) }).click();
        await expectTotal(page, 9);
        await expect(page.getByTestId('query-stage-chip')).toHaveText([/idx >= 3/]);
        await expect(page.getByText(notice(`the view “${name}”`), { exact: true })).toBeVisible();
        expect(seen.named).toEqual([]);
        expect(seen.refused).toEqual([]);
    } finally {
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});

test('a dropped concept-list request is asked again before a view is applied', async ({ page }) => {
    test.setTimeout(120_000);
    const name = `e2e-dropped-saved-${S}`;
    const { view, concept, notice } = await viewNamingGoneConcept(name);
    try {
        const seen = await slowList(page, concept.id, { delay: 500, drop: 1 });
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 12);
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await page.getByRole('button', { name: new RegExp(`^${name}`) }).click();
        await expectTotal(page, 9);
        await expect(page.getByText(notice(`the view “${name}”`), { exact: true })).toBeVisible();
        expect(seen.dropped()).toBe(1);
        expect(seen.named).toEqual([]);
        expect(seen.refused).toEqual([]);
    } finally {
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});

test('a ?view= link and the default view wait for a slow concept list too', async ({ browser }) => {
    test.setTimeout(120_000);
    const name = `e2e-slow-link-${S}`;
    const { view, concept, notice } = await viewNamingGoneConcept(name);
    try {
        for (const [where, path, from] of [
            ['link', `/datasets/${datasetId}?view=${view.slug}`, `the view “${name}”`],
            ['default view', `/datasets/${datasetId}`, `the default view “${name}”`],
        ] as const) {
            if (where === 'default view') {
                await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: view.id }) });
            }
            const context = await browser.newContext({ storageState: AUTH_STATE });
            try {
                const page = await context.newPage();
                const seen = await slowList(page, concept.id);
                await page.goto(path);
                await expectTotal(page, 9);
                await expect(page.getByTestId('query-stage-chip')).toHaveText([/idx >= 3/]);
                await expect(page.getByText(notice(from), { exact: true })).toBeVisible();
                expect(seen.named, where).toEqual([]);
                expect(seen.refused, where).toEqual([]);
            } finally {
                await context.close();
            }
        }
    } finally {
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ default_view_id: null }) }).catch(() => undefined);
        await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});
