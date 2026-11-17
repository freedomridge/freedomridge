// Shared by the access-level specs: test users at a level in a dataset, signed in in their own browser context.
import { randomUUID } from 'node:crypto';
import { expect, type Browser, type BrowserContext, type Locator, type Page } from '@playwright/test';
import { API, apiFetch } from './api';
import { AUTH_STATE } from './helpers';

export type Level = 'view' | 'tag' | 'edit' | 'manage';
export const LEVELS: Level[] = ['view', 'tag', 'edit', 'manage'];

/** The server's refusal, which a control the level doesn't allow carries as its reason. */
export const REASON: Record<Exclude<Level, 'view'>, string> = {
    tag: 'This needs tag access to the dataset',
    edit: 'This needs edit access to the dataset',
    manage: 'This needs manage access to the dataset',
};

export const atLeast = (have: Level, need: Level) => LEVELS.indexOf(have) >= LEVELS.indexOf(need);
export const capitalized = (level: Level) => level[0].toUpperCase() + level.slice(1);

export type TestUser = { id: number; username: string; password: string; email?: string };

/** A short unique suffix: the specs run in parallel against one server. */
export const stamp = () => `${Date.now().toString(36)}${randomUUID().slice(0, 6)}`;

/** A new account with its first-login password change done, ready to sign in. */
export async function createUser(cookie: string, username: string, role: 'power_user' | 'guest' = 'power_user', email?: string): Promise<TestUser> {
    const first = 'E2e-Access-Levels-Pw-2026!';
    const { id } = await apiFetch(cookie, '/users', { method: 'POST', body: JSON.stringify({ username, password: first, role, email }) });
    const login = await fetch(`${API}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password: first }),
    });
    if (!login.ok) throw new Error(`login as ${username} failed: ${login.status}`);
    const session = (login.headers.get('set-cookie') || '').match(/session_id=([^;]+)/)?.[1];
    const password = `${first}-2`;
    await apiFetch(`session_id=${session}`, '/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({ current_password: first, new_password: password }),
    });
    return { id, username, password, email };
}

/** A browser context signed in as `user`, or as the admin (the global setup's session) without one. */
export async function signIn(browser: Browser, user?: TestUser): Promise<{ context: BrowserContext; page: Page }> {
    const context = await browser.newContext({ storageState: user ? { cookies: [], origins: [] } : AUTH_STATE });
    const page = await context.newPage();
    if (user) {
        const login = await page.request.post('/api/auth/login', { data: { username: user.username, password: user.password } });
        expect(login.ok(), `sign in as ${user.username}`).toBeTruthy();
    }
    return { context, page };
}

/** Share a dataset with a user (by id) or a group (by name), at `level` (the server's default, edit, without one). */
export async function share(cookie: string, datasetId: number, target: { user_id: number } | { group: string }, level?: Level) {
    return apiFetch(cookie, `/datasets/${datasetId}/access`, { method: 'POST', body: JSON.stringify({ ...target, level }) });
}

/** A group with these members. */
export async function createGroup(cookie: string, name: string, members: TestUser[] = []): Promise<{ id: number; name: string }> {
    const group = await apiFetch(cookie, '/groups', { method: 'POST', body: JSON.stringify({ name }) });
    for (const member of members) await addMember(cookie, group.id, member);
    return group;
}

/** Add a member to a group (204, no body). */
export async function addMember(cookie: string, groupId: number, member: TestUser) {
    const res = await fetch(`${API}/groups/${groupId}/members/${member.id}`, { method: 'PUT', headers: { Cookie: cookie } });
    if (res.status !== 204) throw new Error(`PUT /groups/${groupId}/members/${member.id} -> ${res.status}: ${await res.text()}`);
}

/** Delete a group (204, no body); already gone is fine. */
export async function deleteGroup(cookie: string, groupId: number) {
    const res = await fetch(`${API}/groups/${groupId}`, { method: 'DELETE', headers: { Cookie: cookie } });
    if (res.status !== 204 && res.status !== 404) throw new Error(`DELETE /groups/${groupId} -> ${res.status}: ${await res.text()}`);
}

/** A small private dataset of three images, with its statistics. */
export async function createSmallDataset(cookie: string, name: string): Promise<number> {
    const { id } = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name }) });
    await apiFetch(cookie, `/datasets/${id}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: [0, 1, 2].map(i => ({ filepath: `s3://e2e-access/img_${i}.jpg`, metadata: { idx: i } })) }),
    });
    await apiFetch(cookie, `/datasets/${id}/refresh-stats`, { method: 'POST' });
    return id;
}

/**
 * A private dataset of three images with several media fields: `filepath`, and each other field a metadata key holding
 * another image path for the same sample.
 */
export async function createMediaDataset(cookie: string, name: string, mediaFields = ['filepath', 'alt_path']): Promise<number> {
    const { id } = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name, media_fields: mediaFields }) });
    const others = mediaFields.filter(field => field !== 'filepath');
    await apiFetch(cookie, `/datasets/${id}/samples`, {
        method: 'POST',
        body: JSON.stringify({
            samples: [0, 1, 2].map(i => ({
                filepath: `s3://e2e-media/main_${i}.jpg`,
                metadata: Object.fromEntries(others.map(field => [field, `s3://e2e-media/${field}_${i}.jpg`])),
            })),
        }),
    });
    await apiFetch(cookie, `/datasets/${id}/refresh-stats`, { method: 'POST' });
    return id;
}

/** The access list as an admin reads it. */
export async function accessList(cookie: string, datasetId: number): Promise<{
    is_global: boolean;
    everyone_level: Level;
    grants: Array<{ user_id: number; username: string; level: Level }>;
    group_grants: Array<{ group_id: number; name: string; level: Level }>;
}> {
    return apiFetch(cookie, `/datasets/${datasetId}/access`);
}

/** Runs every step even when one throws, then throws the first error: cleanup that must not stop halfway. */
export async function allSteps(steps: Array<() => Promise<unknown>>) {
    const errors: unknown[] = [];
    for (const step of steps) {
        try {
            await step();
        } catch (error) {
            errors.push(error);
        }
    }
    if (errors.length) throw errors[0];
}

/** Open a dataset and wait for it to load: gated controls stay disabled until the dataset (and its level) is read. */
export async function openDataset(page: Page, datasetId: number) {
    await page.goto(`/datasets/${datasetId}`);
    await expect(page.getByTestId('grid-select-all')).toBeVisible({ timeout: 30_000 });
}

/** Dataset Settings, on its General tab. */
export async function openSettings(page: Page): Promise<Locator> {
    await page.getByRole('button', { name: 'Dataset Settings', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Dataset Settings' });
    await expect(dialog).toBeVisible();
    return dialog;
}
