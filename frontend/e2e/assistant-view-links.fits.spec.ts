import { test, expect, type Page, type Route } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expectTotal, fillStable } from './helpers';
import { type TestUser, allSteps, createUser, share, signIn, stamp } from './access-helpers';
import { loadRegionFixture, regionsNotOptedIn } from './regions-fixture';

// Opening an assistant view's link (m1-contract-finding-tools.md section 2, the UI contract of the view links). The
// assistant answers by building a working view on the server and giving a link to it: a ?q= link when every stage is
// filter text that fits an address, else /datasets/<id>?w=<handle>. A link opens the dataset on exactly that view's
// samples, as the chips those filters would be had a person made them; a w= link works only for its person; it never
// stays in the address and Copy link never copies it; nothing that arrives by a link moves the map.
// The views are made honestly: the MCP server switched on, an MCP token, and the tools called over /mcp in the
// 2026-07-28 Streamable HTTP shape (see mcp-door.fits.spec.ts). The assistant's settings are the server's own, so this
// spec runs in the one-at-a-time project after every other spec (playwright.config.ts: *.fits.spec.ts) and puts them
// back as it found them.
test.use({ storageState: AUTH_STATE, permissions: ['clipboard-read', 'clipboard-write'] });

const S = stamp();
const PREFIX = `vl${S}_`;
let cookie = '';
let origin = '';
let mcpToken = '';
let found: { mcp_enabled: boolean };
let regionsLoaded: string | null = 'not read';
let small = 0;  // 12 images: idx 0-5 train in Kansas, 6-11 val in Nebraska, a concept, a private field
let big = 0;  // 2,000 images with distinct serial numbers, the first 700 in Kansas
let gear: { id: number; name: string; threshold: number };
const PLANTED = ['A', 'B', 'C'].map(letter => `PLANT${S}${letter}`);
const answers: string[] = [];  // every tool answer this spec received, as text
const people: TestUser[] = [];
const tokenIds: number[] = [];

const SOMEONE_ELSES = 'This link belongs to someone else\'s conversation with the assistant. Ask them to save a view and share that.';
const NO_VIEW = 'This link\'s view doesn\'t exist or has expired: the assistant keeps the views it makes for 30 days after they were last used.';
const TOO_LARGE = 'Can\'t copy a link: the selection is too large for a link. Save a view and copy its link instead.';
const COPIED = 'Link to these filters copied';
const NO_FILTERS = 'No filters: showing every image.';

async function embedded(datasetId: number, ids: number[], group: (i: number) => number) {
    await apiFetch(cookie, `/datasets/${datasetId}/embeddings`, {
        method: 'POST',
        body: JSON.stringify(ids.map((sampleId, i) => {
            const vector = Array.from({ length: 8 }, () => 0);
            vector[group(i)] = 1;
            return { sample_id: sampleId, vector, field_name: 'e2e_clip' };
        })),
    });
}

async function makeConcept(datasetId: number, name: string, seedIds: number[]) {
    const made = await apiFetch(cookie, `/datasets/${datasetId}/concepts`, { method: 'POST', body: JSON.stringify({ name, embedding_field: 'e2e_clip', kind: 'seed', seed_ids: seedIds }) });
    if (made.job_id) await expect.poll(async () => (await apiFetch(cookie, `/jobs/${made.job_id}`)).state, { timeout: 60_000 }).toBe('succeeded');
    return ((await apiFetch(cookie, `/datasets/${datasetId}/concepts`)) as Array<{ id: number; name: string; threshold: number }>).find(concept => concept.name === name)!;
}

test.beforeAll(async () => {
    test.setTimeout(240_000);
    cookie = await adminLogin();
    found = await apiFetch(cookie, '/admin/assistant');
    regionsLoaded = regionsNotOptedIn ?? await loadRegionFixture(cookie);
    await apiFetch(cookie, '/admin/assistant', { method: 'PATCH', body: JSON.stringify({ mcp_enabled: true }) });
    const made = await apiFetch(cookie, '/tokens', { method: 'POST', body: JSON.stringify({ name: `${PREFIX}mcp`, scope: 'mcp' }) });
    tokenIds.push(made.id);
    mcpToken = made.token;

    small = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `${PREFIX}small` }) })).id;
    const samples = Array.from({ length: 12 }, (_, i) => ({
        filepath: `s3://e2e-view-links/${S}/${i}.jpg`,
        metadata: { idx: i, split: i < 6 ? 'train' : 'val', secret_code: PLANTED[i % 3] },
        location: { type: 'Point', coordinates: i < 6 ? [-99 + i * 0.3, 38.5] : [-100 + i * 0.1, 41.5] },
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${small}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    await embedded(small, ids, i => (i < 6 ? 0 : 1));
    await apiFetch(cookie, `/datasets/${small}/refresh-stats`, { method: 'POST' });
    gear = await makeConcept(small, `${PREFIX}gear`, ids.slice(0, 6));
    // Kept from the assistant (Dataset Settings → Fields, "Keep this field's values from the assistant").
    await apiFetch(cookie, `/datasets/${small}/fields/secret_code/description`, { method: 'PATCH', body: JSON.stringify({ values_private: true }) });

    big = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `${PREFIX}big` }) })).id;
    const serials = new Set<string>();
    while (serials.size < 2000) serials.add(`SN-${Math.random().toString(16).slice(2, 10).padEnd(8, '0')}`);
    const many = [...serials].map((serial, i) => ({
        filepath: `s3://e2e-view-links-big/${S}/${i}.jpg`,
        metadata: { idx: i, serial_number: serial },
        location: { type: 'Point', coordinates: i < 700 ? [-101 + (i % 70) * 0.08, 37.5 + Math.floor(i / 70) * 0.2] : [-100 + (i % 50) * 0.05, 41.2] },
    }));
    await apiFetch(cookie, `/datasets/${big}/samples`, { method: 'POST', body: JSON.stringify({ samples: many }) });
    await apiFetch(cookie, `/datasets/${big}/refresh-stats`, { method: 'POST' });
    origin = '';
});

test.afterAll(async () => {
    test.setTimeout(240_000);
    await allSteps([
        () => apiFetch(cookie, '/admin/assistant', { method: 'PATCH', body: JSON.stringify({ mcp_enabled: found.mcp_enabled }) }),
        ...tokenIds.map(id => () => apiFetch(cookie, `/tokens/${id}`, { method: 'DELETE' }).catch(() => undefined)),
        ...[small, big].filter(Boolean).map(id => () => deleteDatasetAndWait(cookie, id, 180_000)),
        ...people.map(user => () => apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' }).catch(() => undefined)),
    ]);
});

test.beforeEach(({ baseURL }) => {
    origin = baseURL!;
});

const MCP_VERSION = '2026-07-28';
const META = {
    'io.modelcontextprotocol/protocolVersion': MCP_VERSION,
    'io.modelcontextprotocol/clientInfo': { name: 'freedomridge-e2e', version: '1.0.0' },
    'io.modelcontextprotocol/clientCapabilities': {},
};
type View = { view: string; dataset: number; count: number; filter: string; stages: Array<{ n: number; text: string }>; link: string; link_note?: string };

/** A tool called over /mcp with `token` (the administrator's MCP token unless given); its answer, kept as text. */
async function tool<T = View>(name: string, args: Record<string, unknown>, token = mcpToken): Promise<T> {
    const res = await fetch(`${origin}/mcp`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json', Accept: 'application/json, text/event-stream', 'MCP-Protocol-Version': MCP_VERSION,
            'Mcp-Method': 'tools/call', 'Mcp-Name': name, Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args, _meta: META } }),
    });
    const text = await res.text();
    answers.push(text);
    expect(res.status, `${name}: ${text.slice(0, 300)}`).toBe(200);
    const result = JSON.parse(text).result as { isError: boolean; structuredContent: T };
    expect(result.isError, `${name}: ${text.slice(0, 300)}`).toBe(false);
    return result.structuredContent;
}

/** A view with a value list of `field` added: the list made by field_values over `view`, then refine_view in_list. */
async function withList(view: View, field: string, token = mcpToken) {
    const values = await tool<{ list: string; values: unknown[]; distinct: number }>('field_values', { view: view.view, field, limit: 1 }, token);
    return { values, view: await tool('refine_view', { view: view.view, in_list: { field, list: values.list } }, token) };
}

/** No tool answer held a value kept from the assistant. */
function expectNothingPlanted() {
    for (const value of PLANTED) expect(answers.some(answer => answer.includes(value)), `a tool answer held ${value}`).toBe(false);
}

const chips = (page: Page) => page.getByTestId('query-stage-chip');
const handleOf = (link: string) => new URL(link).searchParams.get('w');
const viewRequests = (page: Page) => {
    const seen: number[] = [];
    page.on('request', request => {
        if (new URL(request.url()).pathname.startsWith('/api/assistant/views/')) seen.push(Date.now());
    });
    return seen;
};

/** Opens a link's path on this page's origin, from another page (so Back has somewhere to go). */
async function openLink(page: Page, link: string, total?: number) {
    const url = new URL(link);
    await page.goto('/');
    await page.goto(url.pathname + url.search);
    if (total !== undefined) await expectTotal(page, total);
}

/** The address once the page has made it its own: the link's w= gone. */
async function settledAddress(page: Page) {
    await expect.poll(() => new URL(page.url()).searchParams.has('w'), { timeout: 15_000 }).toBe(false);
    return new URL(page.url());
}

async function copyLink(page: Page) {
    await page.getByRole('button', { name: 'Copy link', exact: true }).click();
}

const clipboard = (page: Page) => page.evaluate(() => navigator.clipboard.readText());

test('a q= link of ordinary filters, a region and a concept opens on the tool\'s count and address, one chip per stage, the chips a person makes for those filters', async ({ page }) => {
    test.skip(regionsLoaded !== null, regionsLoaded ?? '');
    const view = await tool('make_view', { dataset: small, filter: `location within "Kansas" AND concept "${gear.name}" > ${gear.threshold} AND split = "train" AND idx >= 1` });
    expect(new URL(view.link).searchParams.has('w')).toBe(false);
    expect(view.link_note).toBeUndefined();
    const text = new URL(view.link).searchParams.get('q')!;
    expect(text).toBe(view.filter);
    expect(view.stages.map(stage => stage.text)).toEqual(['location within "US-KS"', `concept "${gear.name}" > ${gear.threshold}`, 'split = "train"', 'idx >= 1']);

    await openLink(page, view.link, view.count);
    await expect(chips(page)).toHaveCount(4);
    await expect(chips(page).nth(0).getByRole('button', { name: 'In Kansas, United States of America', exact: true })).toBeVisible();
    await expect(chips(page).nth(1)).toHaveText(new RegExp(`Only: ${gear.name}$`));
    await expect(chips(page).nth(2)).toHaveText('split = train');
    await expect(chips(page).nth(3)).toHaveText('idx ≥ 1');
    await expect.poll(() => new URL(page.url()).searchParams.get('q')).toBe(text);
    const fromLink = await chips(page).allInnerTexts();

    // The same filters made by hand, one card each: the same chips and count.
    await page.goto(`/datasets/${small}?q=`);
    await expectTotal(page, 12);
    for (const stage of view.stages) {
        await page.getByRole('button', { name: 'Add Filter' }).click();
        await page.getByTestId('filter-mode-query').click();
        await fillStable(page.getByTestId('filter-card-query'), stage.text);
        await page.getByTestId('filter-card-query').press('Control+Enter');
        await expect(chips(page)).toHaveCount(stage.n);
    }
    await expectTotal(page, view.count);
    expect(await chips(page).allInnerTexts()).toEqual(fromLink);
});

for (const [label, filter, more] of [['600', 'idx < 600', 598], ['2,000', '', 1998]] as const) {
    test(`a w= link of a view with a list of ${label} values opens on the tool's count, one chip per stage; no w= stays, Copy link follows the address, Back leaves in one step, a reload keeps the filters`, async ({ page }) => {
        const base = await tool('make_view', { dataset: big, filter });
        const { view } = await withList(base, 'serial_number');
        const handle = handleOf(view.link);
        expect(handle, view.link).toMatch(/^wv_/);
        expect(view.link_note).toMatch(/30 days/);
        expect(view.stages.at(-1)!.text).toMatch(/^serial_number in \(/);
        const asked = viewRequests(page);

        await openLink(page, view.link, view.count);
        await expect(chips(page)).toHaveCount(view.stages.length);
        if (filter) await expect(chips(page).first()).toHaveText('idx < 600');
        await expect(chips(page).last()).toHaveText(new RegExp(`^serial_number: SN-[0-9a-f]{8}, SN-[0-9a-f]{8} \\+${more}$`));
        expect(asked, 'the view read once').toHaveLength(1);

        // The address is the page's own: f= when the filters fit one, else the bare dataset address; never w=.
        const address = await settledAddress(page);
        expect(address.pathname).toBe(`/datasets/${big}`);
        expect(address.searchParams.has('q'), 'too long for text').toBe(false);
        await copyLink(page);
        if (address.searchParams.has('f')) {
            await expect(page.getByText(COPIED)).toBeVisible();
            expect(await clipboard(page)).toBe(address.toString());
        } else {
            expect([...address.searchParams.keys()].filter(key => key !== 'sort' && key !== 'dir'), 'only the sort').toEqual([]);
            await expect(page.getByText(TOO_LARGE, { exact: true })).toBeVisible();
        }
        expect(await page.evaluate(() => navigator.clipboard.readText()).catch(() => '')).not.toContain('w=');

        // A reload keeps the filters without asking for the view again.
        await page.reload();
        await expectTotal(page, view.count);
        await expect(chips(page)).toHaveCount(view.stages.length);
        expect(asked, 'not asked again on a reload').toHaveLength(1);

        // Back goes to the page before, in one step; Forward returns to the filters.
        await page.goBack();
        await expect(page).toHaveURL(new URL('/', origin).toString());
        await page.goForward();
        await expectTotal(page, view.count);
        await expect(chips(page)).toHaveCount(view.stages.length);
        expect(new URL(page.url()).searchParams.has('w')).toBe(false);
        expect(asked, 'not asked again on Back and Forward').toHaveLength(1);
    });
}

test('a w= link of a view filtering a field kept from the assistant opens on the person\'s own values, which no tool answer held; the address and Copy link then give the filters as text', async ({ page }) => {
    const base = await tool('make_view', { dataset: small, filter: 'split = "train"' });
    const { values, view } = await withList(base, 'secret_code');
    expect(values.values).toEqual([]);
    expect(values.distinct).toBe(3);
    expect(view.stages.map(stage => stage.text)).toEqual(['split = "train"', 'secret_code in (3 values kept from the assistant)']);
    expect(handleOf(view.link)).toMatch(/^wv_/);
    expect(view.link_note).toMatch(/30 days/);
    expectNothingPlanted();

    await openLink(page, view.link, view.count);
    await expect(chips(page)).toHaveCount(2);
    await expect(chips(page).first()).toHaveText('split = train');
    const secret = chips(page).nth(1);
    await expect(secret).toHaveText(/^secret_code: PLANT\S+, PLANT\S+ \+1$/);
    const shown = (await secret.innerText()).match(/PLANT\S+?(?=,| )/g) ?? [];
    expect(shown.length).toBe(2);
    for (const value of shown) expect(PLANTED).toContain(value);

    // The page's own address: the filters as text, the person's values with them; Copy link copies it.
    const address = await settledAddress(page);
    const text = address.searchParams.get('q');
    expect(text).toMatch(/^split = "train" AND secret_code in \(/);
    for (const value of PLANTED) expect(text).toContain(`"${value}"`);
    await copyLink(page);
    await expect(page.getByText(COPIED)).toBeVisible();
    expect(await clipboard(page)).toBe(address.toString());

    // A chip like any other: a click opens its filter card.
    await secret.click();
    await expect(page.getByTestId('filter-mode-query')).toBeVisible();
    await page.keyboard.press('Escape');

    await page.reload();
    await expectTotal(page, view.count);
    await expect(chips(page)).toHaveCount(2);
    await page.goBack();
    await expect(page).toHaveURL(new URL('/', origin).toString());
    expectNothingPlanted();
});

type MapRequest = { zoom: number; bbox: number[] };

test('with the Map panel kept showing, a w= link whose view holds a region doesn\'t move the map, and the region is outlined', async ({ page }) => {
    test.setTimeout(180_000);
    test.skip(regionsLoaded !== null, regionsLoaded ?? '');
    const requests: MapRequest[] = [];
    page.on('request', request => {
        if (request.method() !== 'POST' || !new URL(request.url()).pathname.endsWith(`/datasets/${big}/map_points`)) return;
        const body = request.postDataJSON() ?? {};
        if (Array.isArray(body.bbox)) requests.push({ zoom: body.zoom, bbox: body.bbox });
    });
    const settled = async () => {
        let last = -1;
        await expect.poll(() => {
            const still = requests.length === last && requests.length > 0;
            last = requests.length;
            return still;
        }, { intervals: [1_500], timeout: 20_000 }).toBe(true);
    };
    const zoomToData = page.getByTestId('panel-map').getByRole('button', { name: 'Zoom to Data' });
    /** This browser keeps a layout with the Map showing: chosen on the dataset's page, then left. */
    const keepMapShowing = async () => {
        await page.goto(`/datasets/${big}?q=`);
        await expectTotal(page, 2000);
        const select = page.getByTestId('panel-type-select').nth(2);
        await select.selectOption('embeddings');
        await select.selectOption('map');
        const noToken = page.getByTestId('panel-map').getByText(/NEXT_PUBLIC_MAPBOX_TOKEN/);
        await expect(noToken.or(zoomToData).first()).toBeVisible({ timeout: 20_000 });
        test.skip(await noToken.isVisible(), 'No Mapbox token configured');
        await settled();
        await page.goto('/');
    };

    const regional = await tool('make_view', { dataset: big, filter: 'location within "Kansas"' });
    expect(regional.count).toBe(700);
    const { view } = await withList(regional, 'serial_number');
    expect(handleOf(view.link)).toMatch(/^wv_/);

    // The page with no filters, opened on the kept layout: where the map rests.
    await keepMapShowing();
    let from = requests.length;
    await page.goto(`/datasets/${big}?q=`);
    await expect(zoomToData).toBeVisible({ timeout: 20_000 });
    await expect.poll(() => requests.length, { timeout: 20_000 }).toBeGreaterThan(from);
    await settled();
    const unfiltered = requests.slice(from);
    const home = unfiltered.at(-1)!;

    await keepMapShowing();
    from = requests.length;
    await page.goto(new URL(view.link).pathname + new URL(view.link).search);
    await expectTotal(page, view.count);
    await expect(zoomToData).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId('map-region-outline')).toHaveText(['US-KS included']);
    // For the time a move would take to show (the regions contract: about 2 s plus an 0.8 s animation, then the ask).
    const deadline = Date.now() + 4_000;
    while (Date.now() < deadline) {
        const moved = requests.slice(from).find(r => !unfiltered.some(u => u.zoom === r.zoom && JSON.stringify(u.bbox) === JSON.stringify(r.bbox)));
        expect(moved ?? null, 'the map moved to').toBeNull();
        await page.waitForTimeout(250);
    }
    const last = requests.slice(from).at(-1);
    if (last) expect(last, 'where the map rests').toEqual(home);
});

test('with the live address off, a w= link opens the same chips, then the address is the bare dataset address; Copy link still copies the filters\' link', async ({ page }) => {
    await page.route('**/runtime-config', async (route: Route) => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...(await response.json()), liveAddress: false } });
    });
    const { view } = await withList(await tool('make_view', { dataset: small, filter: 'split = "train"' }), 'secret_code');
    await openLink(page, view.link, view.count);
    await expect(chips(page)).toHaveCount(2);
    await expect.poll(() => page.url()).toBe(new URL(`/datasets/${small}`, origin).toString());
    await copyLink(page);
    await expect(page.getByText(COPIED)).toBeVisible();
    const copied = new URL(await clipboard(page));
    expect(copied.pathname).toBe(`/datasets/${small}`);
    expect(copied.searchParams.has('w')).toBe(false);
    expect(copied.searchParams.get('q') ?? copied.searchParams.get('f')).toBeTruthy();
    expectNothingPlanted();
});

test('another person\'s w= link, an unknown handle and a handle of another dataset\'s view: the exact words, and the dataset without those filters', async ({ page, browser }) => {
    const { view } = await withList(await tool('make_view', { dataset: small, filter: 'split = "train"' }), 'secret_code');
    const elsewhere = (await withList(await tool('make_view', { dataset: big, filter: 'idx < 600' }), 'serial_number')).view;
    const other = await createUser(cookie, `${PREFIX}other`);
    people.push(other);
    await share(cookie, small, { user_id: other.id }, 'view');

    // Someone else, signed in.
    const { context, page: theirs } = await signIn(browser, other);
    try {
        await openLink(theirs, view.link);
        await expect(theirs.getByText(SOMEONE_ELSES, { exact: true })).toBeVisible();
        await expectTotal(theirs, 12);
        await expect(chips(theirs)).toHaveCount(0);
        await expect(theirs.getByText(NO_FILTERS)).toBeVisible();
        const address = await settledAddress(theirs);
        expect([...address.searchParams.entries()]).toEqual([['q', '']]);
    } finally {
        await context.close();
    }

    // An unknown handle: the server's words, as they come.
    await openLink(page, `${origin}/datasets/${small}?w=wv_e2eNoSuchView${S}`);
    await expect(page.getByText(NO_VIEW, { exact: true })).toBeVisible();
    await expectTotal(page, 12);
    await expect(chips(page)).toHaveCount(0);
    expect((await settledAddress(page)).searchParams.has('w')).toBe(false);

    // The handle of a view of another dataset, opened under this one's id: the app's words, the same.
    await openLink(page, `${origin}/datasets/${small}?w=${handleOf(elsewhere.link)}`);
    await expect(page.getByText(NO_VIEW, { exact: true })).toBeVisible();
    await expectTotal(page, 12);
    await expect(chips(page)).toHaveCount(0);
    expect((await settledAddress(page)).searchParams.has('w')).toBe(false);
});

test.describe('signed out', () => {
    test.use({ storageState: { cookies: [], origins: [] } });

    test('a w= link goes through the sign-in page, then opens', async ({ page }) => {
        const { view } = await withList(await tool('make_view', { dataset: small, filter: 'split = "train"' }), 'secret_code');
        const link = new URL(view.link);
        await page.goto(link.pathname + link.search);
        await expect(page).toHaveURL(/\/login\?(expired=true&)?next=/, { timeout: 15_000 });
        expect(new URL(page.url()).searchParams.get('next')).toBe(link.pathname + link.search);
        await fillStable(page.getByLabel('Username'), 'admin');
        await fillStable(page.getByLabel('Password'), 'admin123');
        await page.getByRole('button', { name: 'Sign in' }).click();
        await expectTotal(page, view.count);
        await expect(chips(page)).toHaveCount(2);
        expect((await settledAddress(page)).searchParams.has('w')).toBe(false);
    });
});

test('a stage naming a concept deleted after the view was made is left out, with the notice in the app\'s words; the other stages open', async ({ page }) => {
    const brief = await makeConcept(small, `${PREFIX}brief`, (await apiFetch(cookie, `/datasets/${small}/samples/query`, { method: 'POST', body: JSON.stringify({ limit: 12, offset: 0 }) })).items
        .filter((sample: { metadata: { idx: number } }) => sample.metadata.idx < 6).map((sample: { id: number }) => sample.id));
    const { view: listed } = await withList(await tool('make_view', { dataset: small, filter: 'split = "train"' }), 'secret_code');
    const view = await tool('refine_view', { view: listed.view, add: `concept "${brief.name}" > ${brief.threshold}` });
    expect(handleOf(view.link)).toMatch(/^wv_/);
    await apiFetch(cookie, `/datasets/${small}/concepts/${brief.id}`, { method: 'DELETE' });

    await openLink(page, view.link, listed.count);
    await expect(page.getByText(`From this link, “concept #${brief.id} > ${brief.threshold}” was left out: it filters by a concept this dataset no longer has.`, { exact: true })).toBeVisible();
    await expect(chips(page)).toHaveCount(2);
    await expect(chips(page).first()).toHaveText('split = train');
    await expect(chips(page).nth(1)).toHaveText(/^secret_code: /);
    expectNothingPlanted();
});

test('a dropped request is asked again, then the link screen offers Try again and Open the dataset without it, the link still in the address', async ({ page }) => {
    const { view } = await withList(await tool('make_view', { dataset: small, filter: 'split = "train"' }), 'secret_code');
    const link = new URL(view.link);
    const asked: number[] = [];
    const drop = async (route: Route) => {
        asked.push(Date.now());
        await route.abort('failed');
    };
    await page.route('**/api/assistant/views/**', drop);

    await page.goto('/');
    await page.goto(link.pathname + link.search);
    const screen = page.getByTestId('link-opening');
    await expect(screen.getByRole('alert')).toHaveText('Couldn\'t open the link: Failed to fetch', { timeout: 15_000 });
    expect(asked, 'three asks in all').toHaveLength(3);
    expect(asked[1] - asked[0], 'half a second apart').toBeGreaterThanOrEqual(400);
    expect(asked[2] - asked[1], 'then a second').toBeGreaterThanOrEqual(900);
    await expect(screen.getByRole('button', { name: 'Try again', exact: true })).toBeVisible();
    await expect(screen.getByRole('button', { name: 'Open the dataset without it', exact: true })).toBeVisible();
    await expect(page.getByTestId('total-samples')).toHaveCount(0);
    expect(new URL(page.url()).searchParams.get('w')).toBe(handleOf(view.link));

    // A reload asks again, and fails the same way.
    await page.reload();
    await expect(screen.getByRole('alert')).toHaveText('Couldn\'t open the link: Failed to fetch', { timeout: 15_000 });
    expect(asked).toHaveLength(6);

    // Answered now: Try again opens the link.
    await page.unroute('**/api/assistant/views/**', drop);
    await screen.getByRole('button', { name: 'Try again', exact: true }).click();
    await expectTotal(page, view.count);
    await expect(chips(page)).toHaveCount(2);
    expect((await settledAddress(page)).searchParams.has('w')).toBe(false);

    // Dropped again: Open the dataset without it opens it as without a link, with no message.
    await page.route('**/api/assistant/views/**', drop);
    await page.goto('/');
    await page.goto(link.pathname + link.search);
    await expect(screen.getByRole('alert')).toHaveText('Couldn\'t open the link: Failed to fetch', { timeout: 15_000 });
    await screen.getByRole('button', { name: 'Open the dataset without it', exact: true }).click();
    await expect(page.getByTestId('total-samples')).toHaveText(/^\s*\d+ (samples|images)\s*$/, { timeout: 15_000 });
    await expect(page.getByText(SOMEONE_ELSES)).toHaveCount(0);
    await expect(page.getByText(NO_VIEW)).toHaveCount(0);
    expect((await settledAddress(page)).searchParams.has('w')).toBe(false);
});

test('observed (the contract is silent): a w= link after the dataset is closed to the assistant, and after the view\'s person is deactivated', async ({ page, browser }, testInfo) => {
    test.setTimeout(180_000);
    const seen: string[] = [];
    const note = async (label: string, target: Page) => {
        const total = await target.getByTestId('total-samples').textContent({ timeout: 2_000 }).catch(() => null);
        const shown = await chips(target).allInnerTexts().catch(() => []);
        const words = await target.getByText(/someone else|doesn't exist|Couldn't open|left out/).allInnerTexts().catch(() => []);
        const text = await target.evaluate(() => document.body.innerText.replace(/\s+/g, ' ').slice(0, 240)).catch(() => '');
        seen.push(`${label}: url=${target.url().replace(/^https?:\/\/[^/]+/, '')} total=${total?.trim()} chips=${JSON.stringify(shown)} words=${JSON.stringify(words)} page="${text}"`);
    };
    /** Until the link has opened (the address loses w=) or its screen says why not; at most 15 s. */
    const outcome = async (target: Page) => {
        await expect.poll(async () => !new URL(target.url()).searchParams.has('w') || await target.getByTestId('link-opening').getByRole('alert').isVisible(), { timeout: 15_000 })
            .toBe(true).catch(() => undefined);
    };
    const statusOf = async (handle: string, session: string) => (await fetch(`${new URL(origin).origin}/api/assistant/views/${encodeURIComponent(handle)}`, { headers: { Cookie: session } })).status;
    /**
     * Opens `path` in `target` and waits for the sign-in page there, settled: the address on /login for a second and
     * its Sign in button shown. The navigation waits only for the server's answer (commit), so the app's own way to the
     * sign-in page can't abort it, and the next navigation starts only once the app is done.
     */
    const reachesSignIn = async (target: Page, path: string, label: string) => {
        await target.goto(path, { waitUntil: 'commit' });
        let previous = '';
        try {
            await expect.poll(async () => {
                const now = target.url();
                const settled = now === previous && new URL(now).pathname === '/login';
                previous = now;
                return settled;
            }, { timeout: 20_000, intervals: [1_000] }).toBe(true);
        } catch {
            const text = await target.evaluate(() => document.body.innerText.replace(/\s+/g, ' ').slice(0, 240)).catch(() => '');
            throw new Error(`${label}: no sign-in page within 20 s; at ${target.url().replace(/^https?:\/\/[^/]+/, '')} the page showed "${text}"`);
        }
        await expect(target.getByRole('button', { name: 'Sign in', exact: true }), label).toBeVisible();
        await target.waitForLoadState('load');
    };

    // The dataset closed to the assistant after the view was made.
    const { view } = await withList(await tool('make_view', { dataset: small, filter: 'split = "train"' }), 'secret_code');
    await apiFetch(cookie, `/datasets/${small}`, { method: 'PATCH', body: JSON.stringify({ assistant_closed: true }) });
    try {
        await openLink(page, view.link);
        await outcome(page);
        await note('closed dataset, its person opens the link', page);
        const refine = await fetch(`${origin}/mcp`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream', 'MCP-Protocol-Version': MCP_VERSION, 'Mcp-Method': 'tools/call', 'Mcp-Name': 'make_view', Authorization: `Bearer ${mcpToken}` },
            body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'make_view', arguments: { dataset: small, filter: '' }, _meta: META } }),
        });
        const refused = JSON.parse(await refine.text()).result?.structuredContent?.error?.code;
        seen.push(`closed dataset, a tool on it: ${refused}`);
        expect(refused).toBe('refused');
    } finally {
        await apiFetch(cookie, `/datasets/${small}`, { method: 'PATCH', body: JSON.stringify({ assistant_closed: false }) });
    }

    // A person whose own view's link is opened after they were deactivated, and again after they are reactivated.
    const pia = await createUser(cookie, `${PREFIX}pia`);
    people.push(pia);
    await share(cookie, small, { user_id: pia.id }, 'view');
    await apiFetch(cookie, `/admin/assistant/users/${pia.id}`, { method: 'PUT', body: JSON.stringify({ allowed: true }) });
    const login = await fetch(`${new URL(origin).origin}/api/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: pia.username, password: pia.password }) });
    const piaSession = `session_id=${(login.headers.get('set-cookie') || '').match(/session_id=([^;]+)/)?.[1]}`;
    await expect.poll(async () => (await apiFetch(piaSession, '/auth/me')).assistant?.mcp, { timeout: 15_000 }).toBe(true);
    const piaToken = await apiFetch(piaSession, '/tokens', { method: 'POST', body: JSON.stringify({ name: `${PREFIX}pia`, scope: 'mcp' }) });
    const piaView = (await withList(await tool('make_view', { dataset: small, filter: 'split = "val"' }, piaToken.token), 'secret_code', piaToken.token)).view;
    const handle = handleOf(piaView.link)!;
    seen.push(`her view's endpoint before: ${await statusOf(handle, piaSession)}`);
    const { context, page: hers } = await signIn(browser, pia);
    try {
        const off = await fetch(`${new URL(origin).origin}/api/users/${pia.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', Cookie: cookie }, body: JSON.stringify({ is_active: false }) });
        seen.push(`deactivating her: ${off.status} ${(await off.text()).slice(0, 200)}`);
        expect(off.ok).toBe(true);
        seen.push(`her view's endpoint, deactivated, her old session: ${await statusOf(handle, piaSession)}`);
        // Her open browser, required now (the contract "three page fixes" 3.1: a deactivated account's next full page
        // load reaches the sign-in page): a page of the app reaches it, opening her view's link there reaches it too and
        // shows nothing of the view, and so does the dataset without a link. One address at a time, each waited for at
        // the sign-in page, so that no navigation of the test races the app's own way there.
        await reachesSignIn(hers, '/', 'deactivated, her open browser loads a page of the app');
        await note('deactivated, her open browser loads a page of the app', hers);
        const link = new URL(piaView.link);
        await reachesSignIn(hers, link.pathname + link.search, 'deactivated, her open browser opens her link');
        await note('deactivated, her open browser opens her link', hers);
        await expect(chips(hers), 'nothing of her view is shown').toHaveCount(0);
        await expect(hers.getByTestId('total-samples'), 'nothing of her view is shown').toHaveCount(0);
        await expect(hers.getByTestId('grid-cell'), 'nothing of her view is shown').toHaveCount(0);
        seen.push(`deactivated, her old session's auth/me: ${(await fetch(`${new URL(origin).origin}/api/auth/me`, { headers: { Cookie: piaSession } })).status}`);
        await reachesSignIn(hers, `/datasets/${small}`, 'deactivated, her open browser opens the dataset without a link');
        await note('deactivated, her open browser opens the dataset without a link', hers);
        const on = await fetch(`${new URL(origin).origin}/api/users/${pia.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', Cookie: cookie }, body: JSON.stringify({ is_active: true }) });
        seen.push(`reactivating her: ${on.status} ${(await on.text()).slice(0, 200)}`);
        const again = await fetch(`${new URL(origin).origin}/api/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: pia.username, password: pia.password }) });
        seen.push(`reactivated, signing in: ${again.status}`);
        if (again.ok) {
            const fresh = `session_id=${(again.headers.get('set-cookie') || '').match(/session_id=([^;]+)/)?.[1]}`;
            seen.push(`her view's endpoint, reactivated: ${await statusOf(handle, fresh)}`);
        }
    } finally {
        await context.close();
    }
    // An administrator opening her link: someone else's.
    await openLink(page, piaView.link);
    await expect(page.getByText(SOMEONE_ELSES, { exact: true })).toBeVisible();
    await note('an administrator opens her link', page);
    expectNothingPlanted();

    testInfo.annotations.push({ type: 'observed', description: seen.join('\n') });
    console.log(`\n[observed]\n${seen.join('\n')}\n`);
});
