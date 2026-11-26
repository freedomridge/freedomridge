import { test, expect, type Page, type Route } from '@playwright/test';
import { adminLogin, apiFetch, readState } from './api';
import { expandField, expectTotal } from './helpers';
import { type TestUser, allSteps, createUser, signIn, stamp } from './access-helpers';

// Asking who is signed in is paced (the contract "three page fixes", section 3.5). A request of a page's own that fails
// may make the app ask the server who is signed in (GET /api/auth/me), but failures don't multiply the asking: however
// many requests fail, and however often a polling request fails, the app asks at most once in 5 seconds. A navigation
// inside the app still asks at once, each time.
//
// The first two tests can only fail on a build whose failed requests make the app ask who is signed in: main asks
// nothing when a request fails, so on main they pass whatever the pace. The third fails on main, which asks nothing
// when the person follows a link inside the app.
//
// Read here:
// - Each test signs in as its own new power user and opens the shared fixture dataset. Counting starts once the page
//   has settled (its sample count shown, then 3 s), after the first load's own asks; every window ends well before the
//   app's periodic check (about a minute after the page opened).
// - The polled request is the dataset's job list (GET /api/datasets/{id}/jobs/active, every 5 s; its history at
//   /jobs is failed too). Answered 500 its failure carries the status; failed as a network error it carries none.
//   20 s of it may ask at most 5 times: one per 5 s, and one of slack.
// - "The network down": every /api request but GET /api/auth/me fails as a network error for 3 s, while the person
//   applies a filter (an action that asks the server several things at once); at most 2 asks in those 3 s. The page
//   has recovered when it shows the filtered count without a reload.
// - "Each navigation asks at once": the header's Datasets link, then about a second later its Settings link (both
//   within 2 s). Each is followed by an ask within a second; the asks in the whole window are at most 3, the two
//   navigations' and one paced ask the page's own failing requests may make (the fixture's thumbnails fail to load).
let cookie = '';
const people: TestUser[] = [];

test.beforeAll(async () => {
    cookie = await adminLogin();
});

test.afterAll(async () => {
    await allSteps(people.map(person => () => apiFetch(cookie, `/users/${person.id}`, { method: 'DELETE' })));
});

async function person(): Promise<TestUser> {
    const user = await createUser(cookie, `e2e_pace_${stamp()}`);
    people.push(user);
    return user;
}

const fixture = () => `/datasets/${readState().datasetId}`;

/** When the page sent each GET /api/auth/me (ms, the test's clock). */
function whoAsks(page: Page): number[] {
    const asks: number[] = [];
    page.on('request', request => {
        if (request.method() === 'GET' && new URL(request.url()).pathname === '/api/auth/me') asks.push(Date.now());
    });
    return asks;
}

const within = (asks: number[], from: number, to: number) => asks.filter(at => at >= from && at <= to);
const offsets = (asks: number[], from: number) => asks.map(at => `${at - from} ms`).join(', ') || 'none';

async function openSettled(page: Page) {
    await page.goto(fixture());
    await expect(page.getByTestId('total-samples')).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(3_000);
}

async function expectStillSignedIn(page: Page) {
    expect(new URL(page.url()).pathname, 'still on the dataset page').toBe(fixture());
    await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toHaveCount(0);
}

const JOBS = /\/api\/datasets\/\d+\/jobs(\/active)?(\?.*)?$/;
const FAILURES: Array<[string, (route: Route) => Promise<void>]> = [
    ['answered 500', route => route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } })],
    ['failing as a network error', route => route.abort('failed')],
];

for (const [name, fail] of FAILURES) {
    test(`a polled request ${name} for 20 s asks who is signed in at most once in 5 s, and the person stays signed in`, async ({ browser }) => {
        test.setTimeout(90_000);
        const user = await person();
        const { context, page } = await signIn(browser, user);
        try {
            const asks = whoAsks(page);
            await openSettled(page);
            let failed = 0;
            await page.route(JOBS, route => {
                failed += 1;
                return fail(route);
            });
            const start = Date.now();
            await page.waitForTimeout(20_000);
            const end = Date.now();
            await page.unroute(JOBS);

            expect(failed, 'the page kept polling its jobs').toBeGreaterThanOrEqual(3);
            const asked = within(asks, start, end);
            expect(asked.length, `asks in the 20 s, after its start: ${offsets(asked, start)}`).toBeLessThanOrEqual(5);
            await expectStillSignedIn(page);
            await expect(page.getByTestId('total-samples')).toBeVisible();
        } finally {
            await context.close();
        }
    });
}

test('every request failing at once for 3 s (the network down) asks who is signed in at most twice; the person stays signed in and the page recovers', async ({ browser }) => {
    test.setTimeout(90_000);
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        const asks = whoAsks(page);
        await openSettled(page);
        await expectTotal(page, 12);
        await expandField(page, 'split');

        const down = (url: URL) => url.pathname.startsWith('/api/') && url.pathname !== '/api/auth/me';
        let failed = 0;
        await page.route(down, route => {
            failed += 1;
            return route.abort('failed');
        });
        const start = Date.now();
        await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'train' }).check();
        await page.getByTestId('apply-filters').click();
        await page.waitForTimeout(Math.max(0, start + 3_000 - Date.now()));
        const end = Date.now();
        await page.unroute(down);

        expect(failed, 'requests failed while the network was down').toBeGreaterThanOrEqual(3);
        const asked = within(asks, start, end);
        expect(asked.length, `asks in the 3 s, after their start: ${offsets(asked, start)}`).toBeLessThanOrEqual(2);
        await expectTotal(page, 6);
        await expectStillSignedIn(page);
    } finally {
        await context.close();
    }
});

test('two navigations inside the app within 2 s each ask who is signed in at once', async ({ browser }) => {
    test.setTimeout(90_000);
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        const asks = whoAsks(page);
        await openSettled(page);

        const first = Date.now();
        await page.getByRole('link', { name: 'Datasets', exact: true }).click();
        await expect.poll(() => new URL(page.url()).pathname).toBe('/');
        await page.waitForTimeout(Math.max(0, first + 1_100 - Date.now()));
        const second = Date.now();
        await page.getByRole('link', { name: 'Settings', exact: true }).click();
        await expect.poll(() => new URL(page.url()).pathname).toBe('/settings');
        await page.waitForTimeout(Math.max(0, second + 1_500 - Date.now()));

        expect(second - first, 'the two navigations are within 2 s').toBeLessThan(2_000);
        const all = within(asks, first, second + 1_500);
        const seen = `asks after the first navigation: ${offsets(all, first)}`;
        expect(within(asks, first, first + 1_000).length, `the Datasets link asks within a second; ${seen}`).toBeGreaterThanOrEqual(1);
        expect(within(asks, second, second + 1_000).length, `the Settings link asks within a second; ${seen}`).toBeGreaterThanOrEqual(1);
        expect(all.length, seen).toBeLessThanOrEqual(3);
    } finally {
        await context.close();
    }
});
