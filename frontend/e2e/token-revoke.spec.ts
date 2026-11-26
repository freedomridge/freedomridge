import { test, expect, type Locator, type Page } from '@playwright/test';
import { adminLogin, apiFetch } from './api';
import { type TestUser, allSteps, createUser, signIn, stamp } from './access-helpers';

// A revoked token is no longer offered for revoking (the contract "three page fixes", section 2). On Settings → API
// Tokens, after Revoke succeeded the token's card stayed listed with its Revoke button. Now a revoked token's card
// goes at once, with no reload, and a token revoked elsewhere (an administrator's revoke of the account's tokens, a
// password change or reset with "also revoke") isn't listed either; a token that has only expired is still listed.
// GET /api/tokens is unchanged: it still answers revoked tokens, inactive. A failed Revoke leaves the card and says
// "Failed to revoke token" (today it says nothing); a later Revoke that succeeds removes the card and the message.
//
// Read here:
// - Each test signs in as its own new power user, so the list holds only the tokens the test made. Tokens are made
//   through the API; a card is found by its token's name.
// - "Revoked elsewhere" is tested three ways: DELETE /api/users/{id}/tokens (an administrator's revoke of every token
//   of an account; deactivating an account revokes the same way, but a deactivated account can't open the page), the
//   person's own POST /api/auth/change-password with revoke_tokens (the Change Password dialog's "Also revoke my API
//   tokens", which keeps the session that asked), and an administrator's POST /api/users/{id}/reset-password with
//   revoke_tokens (which also ends the person's sessions, so the test signs in again and sets a password first).
// - "A token that has only expired is still listed": the server won't make an expired token (a token lives at least a
//   day), so the test answers the page's GET /api/tokens with the real list, one active token's expiry moved into the
//   past. The page decides what to show from that answer (contract 2.3).
const FAILED = 'Failed to revoke token';

let cookie = '';
const people: TestUser[] = [];

test.beforeAll(async () => {
    cookie = await adminLogin();
});

test.afterAll(async () => {
    await allSteps(people.map(person => () => apiFetch(cookie, `/users/${person.id}`, { method: 'DELETE' })));
});

async function person(): Promise<TestUser> {
    const user = await createUser(cookie, `e2e_tokens_${stamp()}`);
    people.push(user);
    return user;
}

async function makeToken(page: Page, name: string): Promise<number> {
    const answer = await page.request.post('/api/tokens', { data: { name, expires_days: 30 } });
    expect(answer.ok(), `make token ${name}`).toBeTruthy();
    return (await answer.json()).id;
}

async function listedTokens(page: Page): Promise<Array<{ id: number; name: string; is_active: boolean }>> {
    const answer = await page.request.get('/api/tokens');
    expect(answer.ok()).toBeTruthy();
    return answer.json();
}

/** The card of the token named `name`: the innermost block with its name and one Revoke button. */
function card(page: Page, name: string): Locator {
    return page.locator('div')
        .filter({ has: page.getByRole('heading', { name, exact: true }) })
        .filter({ has: page.getByRole('button', { name: /Revoke/ }) })
        .last();
}

async function openTokens(page: Page) {
    await page.goto('/settings/tokens');
    await expect(page.getByRole('heading', { name: 'API Tokens', level: 1 })).toBeVisible({ timeout: 20_000 });
}

/** Press a card's Revoke and accept the page's confirm. */
async function revokeInPage(page: Page, name: string) {
    page.once('dialog', dialog => void dialog.accept());
    await card(page, name).getByRole('button', { name: /Revoke/ }).click();
}

test("revoking a token in the page removes its card at once and keeps the other's; a reload shows the same", async ({ browser }) => {
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        const gone = `e2e-revoked-${stamp()}`;
        const kept = `e2e-kept-${stamp()}`;
        await makeToken(page, gone);
        await makeToken(page, kept);
        await openTokens(page);
        await expect(page.getByRole('heading', { name: gone, exact: true })).toBeVisible();
        await expect(page.getByRole('heading', { name: kept, exact: true })).toBeVisible();

        const revoked = page.waitForResponse(response => response.request().method() === 'DELETE'
            && /\/api\/tokens\/\d+$/.test(new URL(response.url()).pathname));
        await revokeInPage(page, gone);
        expect((await revoked).ok(), 'the server revoked it').toBeTruthy();
        // At once, with no reload: the revoked card (its name and its Revoke) is gone; the other stays with its Revoke.
        await expect(page.getByRole('heading', { name: gone, exact: true })).toHaveCount(0, { timeout: 5_000 });
        await expect(card(page, kept).getByRole('button', { name: /Revoke/ })).toBeVisible();
        await expect(page.getByRole('button', { name: /Revoke/ })).toHaveCount(1);

        await page.reload();
        await expect(page.getByRole('heading', { name: kept, exact: true })).toBeVisible({ timeout: 20_000 });
        await expect(page.getByRole('heading', { name: gone, exact: true })).toHaveCount(0);
        await expect(page.getByRole('button', { name: /Revoke/ })).toHaveCount(1);
    } finally {
        await context.close();
    }
});

test("a token an administrator revoked (every token of the account) is not listed after a reload", async ({ browser }) => {
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        const before = `e2e-before-${stamp()}`;
        await makeToken(page, before);
        await openTokens(page);
        await expect(page.getByRole('heading', { name: before, exact: true })).toBeVisible();

        expect((await apiFetch(cookie, `/users/${user.id}/tokens`, { method: 'DELETE' })).revoked).toBe(1);
        const after = `e2e-after-${stamp()}`;
        await makeToken(page, after);

        await page.reload();
        await expect(page.getByRole('heading', { name: after, exact: true })).toBeVisible({ timeout: 20_000 });
        await expect(page.getByRole('heading', { name: before, exact: true })).toHaveCount(0);
        await expect(page.getByRole('button', { name: /Revoke/ })).toHaveCount(1);
    } finally {
        await context.close();
    }
});

test('a token revoked by a password change with "also revoke" is not listed after a reload', async ({ browser }) => {
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        const before = `e2e-before-${stamp()}`;
        await makeToken(page, before);
        await openTokens(page);
        await expect(page.getByRole('heading', { name: before, exact: true })).toBeVisible();

        const changed = await page.request.post('/api/auth/change-password', {
            data: { current_password: user.password, new_password: `${user.password}-3`, revoke_tokens: true },
        });
        expect(changed.ok(), 'the password changed').toBeTruthy();
        user.password = `${user.password}-3`;
        const after = `e2e-after-${stamp()}`;
        await makeToken(page, after);

        await page.reload();
        await expect(page.getByRole('heading', { name: after, exact: true })).toBeVisible({ timeout: 20_000 });
        await expect(page.getByRole('heading', { name: before, exact: true })).toHaveCount(0);
        await expect(page.getByRole('button', { name: /Revoke/ })).toHaveCount(1);
    } finally {
        await context.close();
    }
});

test('pin: GET /api/tokens still answers a token revoked in the page, inactive', async ({ browser }) => {
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        const name = `e2e-revoked-${stamp()}`;
        const id = await makeToken(page, name);
        await openTokens(page);
        const revoked = page.waitForResponse(response => response.request().method() === 'DELETE'
            && new URL(response.url()).pathname === `/api/tokens/${id}`);
        await revokeInPage(page, name);
        expect((await revoked).ok()).toBeTruthy();

        const row = (await listedTokens(page)).find(token => token.id === id);
        expect(row, 'still answered').toBeTruthy();
        expect(row?.name).toBe(name);
        expect(row?.is_active).toBe(false);
    } finally {
        await context.close();
    }
});

test("a token revoked by an administrator's password reset with \"also revoke\" is not listed", async ({ browser }) => {
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        const before = `e2e-before-${stamp()}`;
        await makeToken(page, before);
        await openTokens(page);
        await expect(page.getByRole('heading', { name: before, exact: true })).toBeVisible();

        // The reset ends the person's sessions and asks them for a new password: sign in again and set one.
        const reset = `${user.password}-reset`;
        expect((await apiFetch(cookie, `/users/${user.id}/reset-password`, {
            method: 'POST',
            body: JSON.stringify({ password: reset, revoke_tokens: true }),
        })).revoked_tokens?.full).toBe(1);
        expect((await page.request.post('/api/auth/login', { data: { username: user.username, password: reset } })).ok()).toBeTruthy();
        user.password = `${reset}-2`;
        expect((await page.request.post('/api/auth/change-password', {
            data: { current_password: reset, new_password: user.password },
        })).ok()).toBeTruthy();
        const after = `e2e-after-${stamp()}`;
        await makeToken(page, after);

        await openTokens(page);
        await expect(page.getByRole('heading', { name: after, exact: true })).toBeVisible({ timeout: 20_000 });
        await expect(page.getByRole('heading', { name: before, exact: true })).toHaveCount(0);
        await expect(page.getByRole('button', { name: /Revoke/ })).toHaveCount(1);
    } finally {
        await context.close();
    }
});

test('pin: a token that has only expired is still listed', async ({ browser }) => {
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        const expired = `e2e-expired-${stamp()}`;
        const id = await makeToken(page, expired);
        const current = `e2e-current-${stamp()}`;
        await makeToken(page, current);
        await page.route(/\/api\/tokens$/, async route => {
            if (route.request().method() !== 'GET') return route.continue();
            const response = await route.fetch();
            const tokens = (await response.json()) as Array<{ id: number; expires_at?: string; is_active: boolean }>;
            return route.fulfill({
                response,
                json: tokens.map(token => (token.id === id ? { ...token, expires_at: '2020-01-02T03:04:05', is_active: true } : token)),
            });
        });
        await openTokens(page);
        await expect(page.getByRole('heading', { name: current, exact: true })).toBeVisible();
        await expect(card(page, expired).getByRole('button', { name: /Revoke/ })).toBeVisible();
    } finally {
        await context.close();
    }
});

test('a failed Revoke leaves the card and says "Failed to revoke token"; a later Revoke that succeeds removes the card and the message', async ({ browser }) => {
    const user = await person();
    const { context, page } = await signIn(browser, user);
    try {
        const name = `e2e-held-${stamp()}`;
        const id = await makeToken(page, name);
        await openTokens(page);
        await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
        await expect(page.getByText(FAILED)).toHaveCount(0);

        await page.route(/\/api\/tokens\/\d+$/, route => (route.request().method() === 'DELETE'
            ? route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } })
            : route.continue()));
        const refused = page.waitForResponse(response => response.request().method() === 'DELETE'
            && new URL(response.url()).pathname === `/api/tokens/${id}`);
        await revokeInPage(page, name);
        expect((await refused).status()).toBe(500);

        await expect(card(page, name).getByRole('button', { name: /Revoke/ }), 'the card stays').toBeVisible();
        expect((await listedTokens(page)).find(token => token.id === id)?.is_active, 'the token still works').toBe(true);
        await expect(page.getByText(FAILED), 'the page says the revoke failed').toBeVisible({ timeout: 5_000 });
        await expect(card(page, name).getByRole('button', { name: /Revoke/ }), 'the card is still there').toBeVisible();

        // The server answers again: Revoke succeeds and both go.
        await page.unroute(/\/api\/tokens\/\d+$/);
        const revoked = page.waitForResponse(response => response.request().method() === 'DELETE'
            && new URL(response.url()).pathname === `/api/tokens/${id}`);
        await revokeInPage(page, name);
        expect((await revoked).ok()).toBeTruthy();
        await expect(page.getByRole('heading', { name, exact: true })).toHaveCount(0, { timeout: 5_000 });
        await expect(page.getByText(FAILED)).toHaveCount(0);
    } finally {
        await context.close();
    }
});
