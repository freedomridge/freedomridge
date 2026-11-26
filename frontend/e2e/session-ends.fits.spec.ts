import { randomUUID } from 'node:crypto';
import { test, expect, type Locator, type Page } from '@playwright/test';
import { adminLogin, apiFetch, readState } from './api';
import { fillStable } from './helpers';
import { type TestUser, allSteps, createSmallDataset, createUser, signIn, stamp } from './access-helpers';

// A deactivated account's open tab reaches the sign-in page (the contract "three page fixes", section 3). After an
// administrator deactivated an account, that person's still-open tab showed "Loading..." on every page and never
// reached the sign-in page. The server answers a deactivated account's session 403 "Account is inactive" (not 401),
// and the app now treats that answer as the end of the session. Once a session ends while a tab is open (the account
// deactivated or deleted, the session expired, the person signed out in another tab), the tab reaches the sign-in
// page at its next navigation inside the app or its next full page load, and signing in again from there works. A
// signed-in, active person is never sent there by a request that merely failed (a network error, a 500, a 403 for a
// page they may not see).
//
// Read here:
// - "Its next navigation": the test makes it, as the contract's section 4 says, with a link inside the app (the
//   header's Datasets link, or Settings' API Tokens card), or by reloading the page (a full page load); the app's own
//   periodic check (about a minute) is not waited for, and no test asks a place that fetches on its own (a dialog) to
//   redirect by itself. The sign-in page must show within 15 s of that navigation: its form visible, no "Loading..."
//   left.
// - "The session expired": the server can't be made to expire a session from a test, so the tab's session cookie is
//   swapped for one the server doesn't know. The server answers that exactly as an expired session (the same lookup,
//   the same 401).
// - "Lands on the page the person was on": after signing in from the sign-in page reached this way, the tab shows the
//   page the person was on or the page the link they followed leads to (the app's expired-session way names the page
//   the address bar showed when the request was refused, which during a link's navigation is the link's page).
// - Each test signs in as its own new power user in its own browser. Deactivating an account is the server's own state
//   (other specs list accounts), so this spec runs in the one-at-a-time project (playwright.config.ts: *.fits.spec.ts).

let cookie = '';
const people: TestUser[] = [];
const datasets: number[] = [];

test.beforeAll(async () => {
    cookie = await adminLogin();
});

test.afterAll(async () => {
    const known = new Set(((await apiFetch(cookie, '/users')) as Array<{ id: number }>).map(user => user.id));
    await allSteps([
        ...people.filter(person => known.has(person.id)).map(person => () => apiFetch(cookie, `/users/${person.id}`, { method: 'DELETE' })),
        ...datasets.map(id => () => apiFetch(cookie, `/datasets/${id}`, { method: 'DELETE' })),
    ]);
});

async function person(): Promise<TestUser> {
    const user = await createUser(cookie, `e2e_ends_${stamp()}`);
    people.push(user);
    return user;
}

const fixture = () => `/datasets/${readState().datasetId}`;

async function openFixture(page: Page) {
    await page.goto(fixture());
    await expect(page.getByTestId('total-samples')).toBeVisible({ timeout: 30_000 });
}

async function deactivate(user: TestUser) {
    await apiFetch(cookie, `/users/${user.id}`, { method: 'PATCH', body: JSON.stringify({ is_active: false }) });
}

const where = (page: Page) => {
    const url = new URL(page.url());
    return `${url.pathname}${url.search}`;
};

const shownText = (page: Page) => page.evaluate(() => document.body.innerText.replace(/\s+/g, ' ').trim().slice(0, 400)).catch(() => '');

/**
 * Follow a link inside the app, unless the tab has already reached the sign-in page by itself: an open dataset page's
 * own requests (its thumbnails, its jobs) can be refused first, and the app then loads the sign-in page before the
 * test clicks (seen on main for a deleted account and an expired session); the click would wait for its link forever.
 */
async function follow(page: Page, link: Locator) {
    const signInButton = page.getByRole('button', { name: 'Sign in', exact: true });
    if (await signInButton.isVisible()) return;
    await link.click({ noWaitAfter: true, timeout: 10_000 }).catch(async error => {
        if (!(await signInButton.isVisible())) throw error;
    });
}

/** The tab reaches the sign-in page within 15 s: its form shows and no "Loading..." is left. */
async function expectSignInPage(page: Page, label: string) {
    const signInButton = page.getByRole('button', { name: 'Sign in', exact: true });
    try {
        await expect(signInButton).toBeVisible({ timeout: 15_000 });
    } catch {
        throw new Error(`${label}: no sign-in page within 15 s; at ${where(page)} the page showed "${await shownText(page)}"`);
    }
    await expect(page).toHaveURL(/\/login(\?|$)/);
    await expect(page.getByLabel('Username')).toBeVisible();
    await expect(page.getByText('Loading...', { exact: true })).toHaveCount(0);
}

/** The tab stays where it is, signed in: after `ms`, it is not on the sign-in page. */
async function expectStaysSignedIn(page: Page, path: RegExp, label: string, ms = 7_000) {
    await page.waitForTimeout(ms);
    expect(new URL(page.url()).pathname, `${label}: the tab is still on its page; it showed "${await shownText(page)}"`).toMatch(path);
    await expect(page.getByRole('button', { name: 'Sign in', exact: true }), label).toHaveCount(0);
}

/** Sign in on the sign-in page as `user`; answers the path the tab lands on. */
async function signInOnPage(page: Page, user: TestUser): Promise<string> {
    await fillStable(page.getByLabel('Username'), user.username);
    await fillStable(page.getByLabel('Password'), user.password);
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
    const me = await page.request.get('/api/auth/me');
    expect(me.ok(), 'signed in').toBeTruthy();
    expect((await me.json()).username).toBe(user.username);
    return new URL(page.url()).pathname;
}

test('a deactivated account\'s open dataset page reaches the sign-in page at its next request, and signing in again as an active account works', async ({ browser }) => {
    test.setTimeout(90_000);
    const user = await person();
    const other = await person();
    const { context, page } = await signIn(browser, user);
    try {
        await openFixture(page);
        await deactivate(user);

        await follow(page, page.getByRole('link', { name: 'Datasets', exact: true }));
        await expectSignInPage(page, 'after the Datasets link');

        const landed = await signInOnPage(page, other);
        expect(['/', fixture()], 'lands on the page the person was on, or the one the link leads to').toContain(landed);
    } finally {
        await context.close();
    }
});

test('a deactivated account\'s open dataset page reaches the sign-in page when it is reloaded, not "Loading..."', async ({ browser }) => {
    test.setTimeout(90_000);
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        await openFixture(page);
        await deactivate(user);

        await page.reload();
        await expectSignInPage(page, 'after a reload');
    } finally {
        await context.close();
    }
});

test('a deactivated account\'s open settings page reaches the sign-in page at its next request', async ({ browser }) => {
    test.setTimeout(90_000);
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        await page.goto('/settings');
        await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible({ timeout: 20_000 });
        await deactivate(user);

        await follow(page, page.getByRole('link', { name: /^API Tokens/ }));
        await expectSignInPage(page, 'after the API Tokens link');
    } finally {
        await context.close();
    }
});

test('a deleted account\'s open dataset page reaches the sign-in page at its next request', async ({ browser }) => {
    test.setTimeout(90_000);
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        await openFixture(page);
        await apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' });

        await follow(page, page.getByRole('link', { name: 'Datasets', exact: true }));
        await expectSignInPage(page, 'after the Datasets link');
    } finally {
        await context.close();
    }
});

test('signed out in another tab: the open dataset page reaches the sign-in page at its next request', async ({ browser }) => {
    test.setTimeout(90_000);
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        await openFixture(page);

        const other = await context.newPage();
        await other.goto('/settings');
        await expect(other.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible({ timeout: 20_000 });
        await other.getByRole('button', { name: 'User menu' }).click();
        await other.getByRole('menuitem', { name: 'Logout' }).click();
        await expect(other).toHaveURL(/\/login/, { timeout: 15_000 });

        await follow(page, page.getByRole('link', { name: 'Datasets', exact: true }));
        await expectSignInPage(page, 'after the Datasets link');
    } finally {
        await context.close();
    }
});

test('an expired session: the open dataset page reaches the sign-in page at its next request, and signing in lands on the page the person was on', async ({ browser, baseURL }) => {
    test.setTimeout(90_000);
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        await openFixture(page);
        await context.addCookies([{ name: 'session_id', value: randomUUID(), url: baseURL || 'http://localhost:3000', httpOnly: true, sameSite: 'Lax' }]);

        await follow(page, page.getByRole('link', { name: 'Datasets', exact: true }));
        await expectSignInPage(page, 'after the Datasets link');

        const landed = await signInOnPage(page, user);
        expect(['/', fixture()], 'lands on the page the person was on, or the one the link leads to').toContain(landed);
    } finally {
        await context.close();
    }
});

test('pin: an active person whose request fails (a 500, a network error) is not sent to the sign-in page', async ({ browser }) => {
    test.setTimeout(120_000);
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        await openFixture(page);

        // The dataset list the Datasets link opens answers 500 once.
        let failedList = false;
        await page.route(/\/api\/datasets(\?.*)?$/, route => {
            if (failedList || route.request().method() !== 'GET') return route.continue();
            failedList = true;
            return route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } });
        });
        const list = page.waitForResponse(response => response.status() === 500 && new URL(response.url()).pathname === '/api/datasets');
        await follow(page, page.getByRole('link', { name: 'Datasets', exact: true }));
        await list;
        await expectStaysSignedIn(page, /^\/$/, 'a 500 for the dataset list');
        await page.unroute(/\/api\/datasets(\?.*)?$/);

        // Who is signed in, asked again as API Tokens opens, answers 500 once, then fails as a network error once.
        for (const [label, fail] of [
            ['a 500 for who is signed in', 500],
            ['a network error for who is signed in', 'network'],
        ] as const) {
            await page.goto('/settings');
            await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible({ timeout: 20_000 });
            let failed = false;
            await page.route(/\/api\/auth\/me(\?.*)?$/, route => {
                if (failed) return route.continue();
                failed = true;
                return fail === 'network' ? route.abort('failed') : route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } });
            });
            await follow(page, page.getByRole('link', { name: /^API Tokens/ }));
            await expect.poll(() => failed, { message: `${label}: the request was made` }).toBe(true);
            await expectStaysSignedIn(page, /^\/settings\/tokens$/, label);
            await page.unroute(/\/api\/auth\/me(\?.*)?$/);
        }
    } finally {
        await context.close();
    }
});

test('pin: an active person opening a dataset they may not see gets its refusal, not the sign-in page', async ({ browser }) => {
    test.setTimeout(90_000);
    const user = await person();
    const closed = await createSmallDataset(cookie, `e2e-ends-closed-${stamp()}`);
    datasets.push(closed);
    const { context, page } = await signIn(browser, user);
    try {
        await page.goto(`/datasets/${closed}`);
        await expect(page.getByText("You don't have access to this dataset.")).toBeVisible({ timeout: 20_000 });
        await expectStaysSignedIn(page, new RegExp(`^/datasets/${closed}$`), 'a 403 for the dataset', 3_000);
        await expect(page.getByText("You don't have access to this dataset.")).toBeVisible();
    } finally {
        await context.close();
    }
});
