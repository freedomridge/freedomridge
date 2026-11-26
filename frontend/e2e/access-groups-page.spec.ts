import { test, expect, type Locator, type Page } from '@playwright/test';
import { API, adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, fillStable } from './helpers';
import {
    type TestUser, allSteps, createGroup, createSmallDataset, createUser, deleteGroup, openDataset, openSettings, share,
    signIn, stamp,
} from './access-helpers';

// Groups under User Management (issue 12, follow-up). Each group shows the datasets it is shared with and its level in
// each, read-only (levels change in the dataset's own Access section); an admin sees every dataset. Several members can
// be added at once from a checklist each group opens: closed until asked for, filtered by typing, with no cap, and
// ticks that survive the filter.
test.use({ storageState: AUTH_STATE });

const S = stamp();
const PREFIX = `gp${S}_`;  // every person this spec makes: a filter on it shows only them
let cookie = '';
const people: Record<string, { id: number; username: string }> = {};
const groupIds: number[] = [];
const datasetIds: number[] = [];
let mgr: TestUser;

/** An active account: a person to add. (Its first-login password change doesn't matter here.) */
async function createPerson(name: string) {
    const username = `${PREFIX}${name}`;
    const { id } = await apiFetch(cookie, '/users', {
        method: 'POST',
        body: JSON.stringify({ username, password: 'E2e-Groups-Page-Pw-2026!', role: 'power_user' }),
    });
    return { id, username };
}

async function newGroup(name: string) {
    const group = await createGroup(cookie, `gp-${S}-${name}`);
    groupIds.push(group.id);
    return group;
}

test.beforeAll(async () => {
    test.setTimeout(120_000);
    cookie = await adminLogin();
    for (const name of ['amy', 'ben', 'cara', 'dan']) people[name] = await createPerson(name);
    mgr = await createUser(cookie, `${PREFIX}mgr`);
});

test.afterAll(async () => {
    test.setTimeout(180_000);
    await allSteps([
        ...datasetIds.map(id => () => deleteDatasetAndWait(cookie, id, 120_000)),
        ...groupIds.map(id => () => deleteGroup(cookie, id)),
        ...[...Object.values(people), mgr].filter(Boolean).map(user => () => apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' })),
    ]);
});

const region = (page: Page) => page.getByRole('region', { name: 'Groups' });
const groupItem = (page: Page, name: string) => region(page).getByRole('listitem', { name: `Group ${name}`, exact: true });
const toggleOf = (item: Locator, name: string) => item.getByRole('button', { name: `Add members to ${name}`, exact: true });
const filterOf = (item: Locator, name: string) => item.getByRole('textbox', { name: `Find a user to add to ${name}` });
const checklistOf = (item: Locator, name: string) => item.getByRole('group', { name: `Users to add to ${name}` });
const addOf = (item: Locator, name: string) => item.getByRole('button', { name: `Add the selected users to ${name}` });
const cancelOf = (item: Locator, name: string) => item.getByRole('button', { name: `Cancel adding members to ${name}` });
const tick = (item: Locator, name: string, username: string) => checklistOf(item, name).getByRole('checkbox', { name: username, exact: true });

/** Group writes and people reads the page sends. */
function groupRequests(page: Page) {
    const sent: string[] = [];
    page.on('request', request => {
        const path = new URL(request.url()).pathname;
        if ((request.method() !== 'GET' && path.startsWith('/api/groups')) || (path === '/api/users' && new URL(request.url()).searchParams.get('people'))) {
            sent.push(`${request.method()} ${path}${new URL(request.url()).search}`);
        }
    });
    return sent;
}

async function membersOf(groupId: number): Promise<string[]> {
    const group = ((await apiFetch(cookie, '/groups')) as Array<{ id: number; members: Array<{ username: string }> }>)
        .find(row => row.id === groupId);
    return (group?.members ?? []).map(member => member.username);
}

test('each group lists the datasets it is shared with, by name and level, each a link; an admin sees them all; read-only', async ({ page, browser }) => {
    test.setTimeout(150_000);
    const alpha = await createSmallDataset(cookie, `gp-${S}-alpha`);
    const beta = await createSmallDataset(cookie, `gp-${S}-beta`);
    datasetIds.push(alpha, beta);
    // beta is private and the admin has no share in it (not even the creator's).
    const adminId = ((await apiFetch(cookie, '/users')) as Array<{ id: number; username: string }>).find(user => user.username === 'admin')!.id;
    expect((await fetch(`${API}/datasets/${beta}/access/${adminId}`, { method: 'DELETE', headers: { Cookie: cookie } })).status).toBe(204);
    const shared = await newGroup('shared');
    const none = await newGroup('none');
    await share(cookie, beta, { group: shared.name }, 'edit');
    await share(cookie, alpha, { group: shared.name }, 'view');

    // The API carries the same list, by dataset name.
    const groups = (await apiFetch(cookie, '/groups')) as Array<{ id: number; datasets: Array<{ id: number; name: string; level: string; deleting: boolean }> }>;
    expect(groups.find(group => group.id === shared.id)?.datasets).toEqual([
        { id: alpha, name: `gp-${S}-alpha`, level: 'view', deleting: false },
        { id: beta, name: `gp-${S}-beta`, level: 'edit', deleting: false },
    ]);
    expect(groups.find(group => group.id === none.id)?.datasets).toEqual([]);

    await page.goto('/users');
    await expect(region(page).getByText('Share a dataset with a group in its Dataset Settings: each member gets the group\'s level there.', { exact: true })).toBeVisible();
    await expect(region(page).getByText(
        'To change a group\'s level in a dataset or remove its share, use the Access section in that dataset\'s Dataset Settings.',
        { exact: true },
    )).toBeVisible();
    const list = groupItem(page, shared.name).getByRole('list', { name: `Datasets shared with ${shared.name}` });
    await expect(list.getByRole('listitem')).toHaveText([`gp-${S}-alpha View`, `gp-${S}-beta Edit`]);
    await expect(list.getByRole('link', { name: `gp-${S}-alpha`, exact: true })).toHaveAttribute('href', `/datasets/${alpha}`);
    await expect(list.getByRole('link', { name: `gp-${S}-beta`, exact: true })).toHaveAttribute('href', `/datasets/${beta}`);
    // Read-only: nothing to change there.
    await expect(list.getByRole('combobox')).toHaveCount(0);
    await expect(list.getByRole('button')).toHaveCount(0);
    const empty = groupItem(page, none.name).getByRole('list', { name: `Datasets shared with ${none.name}` });
    await expect(empty.getByRole('listitem')).toHaveText(['Not shared with any dataset.']);
    await expect(empty.getByRole('link')).toHaveCount(0);

    // The link opens the dataset, in the same tab.
    await list.getByRole('link', { name: `gp-${S}-beta`, exact: true }).click();
    await expect(page).toHaveURL(url => url.pathname === `/datasets/${beta}`);  // the page may add its own ?q=
    await expect(page.getByTestId('grid-select-all')).toBeVisible({ timeout: 30_000 });

    // Still admins only.
    const { context, page: other } = await signIn(browser, mgr);
    try {
        const refused = await other.request.get('/api/groups');
        expect(refused.status()).toBe(403);
        expect((await refused.json()).detail).toBe('Requires admin privileges');
    } finally {
        await context.close();
    }
});

test('a manager\'s change of the group\'s level, or removing its share, shows on the Groups page on its next load', async ({ page, browser }) => {
    test.setTimeout(120_000);
    const set = await createSmallDataset(cookie, `gp-${S}-gamma`);
    datasetIds.push(set);
    const group = await newGroup('changed');
    await share(cookie, set, { group: group.name }, 'view');
    await share(cookie, set, { user_id: mgr.id }, 'manage');
    const list = groupItem(page, group.name).getByRole('list', { name: `Datasets shared with ${group.name}` });
    await page.goto('/users');
    await expect(list.getByRole('listitem')).toHaveText([`gp-${S}-gamma View`]);

    const manager = await signIn(browser, mgr);
    try {
        await openDataset(manager.page, set);
        const access = (await openSettings(manager.page)).getByRole('region', { name: 'Access' });
        await access.getByRole('combobox', { name: `Level for group ${group.name}` }).selectOption('manage');
        await expect(access.getByRole('combobox', { name: `Level for group ${group.name}` })).toHaveValue('manage');
        await page.reload();
        await expect(list.getByRole('listitem')).toHaveText([`gp-${S}-gamma Manage`]);

        await access.getByRole('button', { name: `Remove group ${group.name}` }).click();
        await expect(access.getByRole('combobox', { name: `Level for group ${group.name}` })).toHaveCount(0);
        await page.reload();
        await expect(list.getByRole('listitem')).toHaveText(['Not shared with any dataset.']);
    } finally {
        await manager.context.close();
    }
});

test('Add members is closed until asked for; three ticked are added with one Add, and the checklist closes', async ({ page }) => {
    const group = await newGroup('three');
    const sent = groupRequests(page);
    await page.goto('/users');
    const item = groupItem(page, group.name);
    const toggle = toggleOf(item, group.name);
    await expect(toggle).toHaveText('Add members');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(page.getByRole('checkbox'), 'every group closed: no checkboxes').toHaveCount(0);
    await expect(filterOf(item, group.name)).toHaveCount(0);
    await expect(item.getByRole('status')).toHaveCount(0);
    const onLoad = sent.length;  // one people read (two under Strict Mode)

    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const filter = filterOf(item, group.name);
    await expect(filter).toBeFocused();
    await expect(filter).toHaveValue('');
    await expect(filter).toHaveAttribute('placeholder', 'Find a user');
    await expect(item.getByRole('status')).toHaveText('0 selected');
    const add = addOf(item, group.name);
    await expect(add).toHaveText('Add');
    await expect(add).toBeDisabled();
    expect(sent.length, 'opening a checklist sends nothing').toBe(onLoad);

    // This spec's people, by username.
    await fillStable(filter, PREFIX);
    const shown = checklistOf(item, group.name).getByRole('checkbox');
    const mine = ['amy', 'ben', 'cara', 'dan', 'mgr'].map(name => `${PREFIX}${name}`);
    await expect(shown).toHaveCount(mine.length);
    for (const [index, username] of mine.entries()) await expect(shown.nth(index)).toHaveAccessibleName(username);
    for (const name of ['amy', 'ben', 'cara']) await tick(item, group.name, people[name].username).check();
    await expect(item.getByRole('status')).toHaveText('3 selected');
    await expect(add).toHaveText('Add 3');
    await add.click();

    const members = item.getByRole('list', { name: `Members of ${group.name}` });
    for (const name of ['amy', 'ben', 'cara']) await expect(members.getByRole('listitem').filter({ hasText: people[name].username })).toBeVisible();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(item.getByRole('checkbox')).toHaveCount(0);
    await expect(toggle).toBeFocused();
    await expect(region(page).getByRole('alert')).toHaveCount(0);
    expect(sent.filter(line => line.startsWith('PUT '))).toHaveLength(3);
    expect(await membersOf(group.id)).toEqual(['amy', 'ben', 'cara'].map(name => people[name].username));

    // Opened again: an empty filter, nothing ticked, and the new members no longer offered.
    await toggle.click();
    await expect(filter).toHaveValue('');
    await expect(item.getByRole('status')).toHaveText('0 selected');
    await fillStable(filter, PREFIX);
    await expect(shown).toHaveCount(2);
    for (const name of ['amy', 'ben', 'cara']) await expect(tick(item, group.name, people[name].username)).toHaveCount(0);
    await expect(tick(item, group.name, people.dan.username)).toBeVisible();
});

test('the filter narrows the list ignoring case; a ticked name it hides is still counted and added; no match says so', async ({ page }) => {
    const group = await newGroup('filtered');
    await page.goto('/users');
    const item = groupItem(page, group.name);
    await toggleOf(item, group.name).click();
    const filter = filterOf(item, group.name);
    const status = item.getByRole('status');
    const add = addOf(item, group.name);
    const shown = checklistOf(item, group.name).getByRole('checkbox');

    await fillStable(filter, `${PREFIX}AMY`.toUpperCase());
    await expect(shown).toHaveCount(1);
    await tick(item, group.name, people.amy.username).check();
    await fillStable(filter, `  ${PREFIX}ben  `);  // leading and trailing spaces don't count
    await expect(shown).toHaveCount(1);
    await expect(status).toHaveText('1 selected, 1 hidden by the filter');
    await expect(add).toHaveText('Add 1');
    await tick(item, group.name, people.ben.username).check();
    await expect(status).toHaveText('2 selected, 1 hidden by the filter');
    await expect(add).toHaveText('Add 2');

    await fillStable(filter, '  zzz-no-one  ');
    await expect(shown).toHaveCount(0);
    await expect(checklistOf(item, group.name)).toContainText('No user matches "zzz-no-one".');
    await expect(status).toHaveText('2 selected, 2 hidden by the filter');

    // Cleared, both are ticked again.
    await fillStable(filter, PREFIX);
    await expect(status).toHaveText('2 selected');
    await expect(tick(item, group.name, people.amy.username)).toBeChecked();
    await expect(tick(item, group.name, people.ben.username)).toBeChecked();
    // Hidden by the filter, both are still added.
    await fillStable(filter, `${PREFIX}dan`);
    await add.click();
    await expect(toggleOf(item, group.name)).toHaveAttribute('aria-expanded', 'false');
    expect(await membersOf(group.id)).toEqual([people.amy.username, people.ben.username]);
});

test('Cancel and the toggle forget the ticks; each group opens its own checklist, with its own ticks', async ({ page }) => {
    const first = await newGroup('first');
    const second = await newGroup('second');
    const sent = groupRequests(page);
    await page.goto('/users');
    const one = groupItem(page, first.name);
    const two = groupItem(page, second.name);
    await expect(toggleOf(one, first.name)).toBeVisible();
    const onLoad = sent.length;

    await toggleOf(one, first.name).click();
    await expect(toggleOf(two, second.name)).toHaveAttribute('aria-expanded', 'false');
    await expect(two.getByRole('checkbox')).toHaveCount(0);
    await fillStable(filterOf(one, first.name), PREFIX);
    await tick(one, first.name, people.amy.username).check();
    await expect(one.getByRole('status')).toHaveText('1 selected');
    await cancelOf(one, first.name).click();
    await expect(toggleOf(one, first.name)).toHaveAttribute('aria-expanded', 'false');
    await expect(toggleOf(one, first.name)).toBeFocused();
    await toggleOf(one, first.name).click();
    await expect(filterOf(one, first.name)).toHaveValue('');
    await expect(one.getByRole('status')).toHaveText('0 selected');

    // The toggle closes it too, and forgets as Cancel does.
    await fillStable(filterOf(one, first.name), PREFIX);
    await tick(one, first.name, people.ben.username).check();
    await toggleOf(one, first.name).click();
    await expect(toggleOf(one, first.name)).toHaveAttribute('aria-expanded', 'false');
    await toggleOf(one, first.name).click();
    await expect(one.getByRole('status')).toHaveText('0 selected');

    // Two open at once, each with its own filter and ticks.
    await toggleOf(two, second.name).click();
    await fillStable(filterOf(one, first.name), PREFIX);
    await fillStable(filterOf(two, second.name), `${PREFIX}c`);
    await tick(one, first.name, people.amy.username).check();
    await expect(checklistOf(two, second.name).getByRole('checkbox')).toHaveCount(1);
    await tick(two, second.name, people.cara.username).check();
    await expect(one.getByRole('status')).toHaveText('1 selected');
    await expect(two.getByRole('status')).toHaveText('1 selected');
    await expect(tick(two, second.name, people.amy.username)).toHaveCount(0);  // hidden there by its own filter
    await fillStable(filterOf(two, second.name), PREFIX);
    await expect(tick(two, second.name, people.amy.username)).not.toBeChecked();
    await cancelOf(one, first.name).click();
    await cancelOf(two, second.name).click();
    expect(sent.length, 'nothing sent').toBe(onLoad);
    expect(await membersOf(first.id)).toEqual([]);
    expect(await membersOf(second.id)).toEqual([]);
});

test('a partial failure: the others are added, the one not added stays ticked, and the card\'s alert names it', async ({ page }) => {
    const group = await newGroup('partial');
    const frank = await createPerson('frank');
    let frankGone = false;
    try {
        await page.goto('/users');
        const item = groupItem(page, group.name);
        await toggleOf(item, group.name).click();
        await fillStable(filterOf(item, group.name), PREFIX);
        for (const user of [people.amy, frank, people.cara]) await tick(item, group.name, user.username).check();
        await expect(addOf(item, group.name)).toHaveText('Add 3');
        // Deleted meanwhile: the server answers that one 404 "User not found".
        await apiFetch(cookie, `/users/${frank.id}`, { method: 'DELETE' });
        frankGone = true;
        await addOf(item, group.name).click();

        const alert = item.getByRole('alert');
        await expect(alert).toHaveText(`Could not add ${frank.username}: User not found`);
        await expect(region(page).getByRole('alert'), 'the only alert in Groups').toHaveCount(1);
        const members = item.getByRole('list', { name: `Members of ${group.name}` });
        await expect(members.getByRole('listitem').filter({ hasText: people.amy.username })).toBeVisible();
        await expect(members.getByRole('listitem').filter({ hasText: people.cara.username })).toBeVisible();
        await expect(toggleOf(item, group.name)).toHaveAttribute('aria-expanded', 'true');
        await expect(tick(item, group.name, frank.username)).toBeChecked();
        await expect(tick(item, group.name, people.amy.username)).toHaveCount(0);
        await expect(item.getByRole('status')).toHaveText('1 selected');
        await expect(addOf(item, group.name)).toHaveText('Add 1');
        await expect(addOf(item, group.name)).toBeEnabled();
        expect(await membersOf(group.id)).toEqual([people.amy.username, people.cara.username]);

        // Closing the checklist keeps the alert; the next Groups action clears it.
        await cancelOf(item, group.name).click();
        await expect(alert).toHaveText(`Could not add ${frank.username}: User not found`);
        await members.getByRole('button', { name: `Remove ${people.cara.username} from ${group.name}` }).click();
        await expect(members.getByRole('listitem').filter({ hasText: people.cara.username })).toHaveCount(0);
        await expect(region(page).getByRole('alert')).toHaveCount(0);
    } finally {
        if (!frankGone) await apiFetch(cookie, `/users/${frank.id}`, { method: 'DELETE' });
    }
});

test('members aren\'t offered; with hundreds of people every one is offered with no cap, and typing finds one at once', async ({ page }) => {
    test.setTimeout(180_000);
    const group = await newGroup('crowd');
    expect((await fetch(`${API}/groups/${group.id}/members/${people.dan.id}`, { method: 'PUT', headers: { Cookie: cookie } })).status).toBe(204);
    const crowd: Array<{ id: number; username: string }> = [];
    try {
        for (let i = 0; i < 40; i += 1) crowd.push(await createPerson(`n${String(i).padStart(2, '0')}`));
        await page.goto('/users');
        const item = groupItem(page, group.name);
        await toggleOf(item, group.name).click();
        const shown = checklistOf(item, group.name).getByRole('checkbox');
        // Everyone who can be offered, with no cap: at least these 40 and this spec's other people.
        await fillStable(filterOf(item, group.name), PREFIX);
        await expect(shown).toHaveCount(40 + 4);  // amy, ben, cara, mgr; dan is a member
        await expect(tick(item, group.name, people.dan.username)).toHaveCount(0);
        await fillStable(filterOf(item, group.name), '');
        expect(await shown.count()).toBeGreaterThanOrEqual(44);
        // Typing one name finds it straight away.
        await fillStable(filterOf(item, group.name), `${PREFIX}n27`);
        await expect(shown).toHaveCount(1, { timeout: 2_000 });
        await expect(shown.first()).toHaveAccessibleName(`${PREFIX}n27`);
    } finally {
        await allSteps(crowd.map(user => () => apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' })));
    }
});

/**
 * A dataset whose deletion stopped before it finished: it stays sealed (`deleting`) until the deletion is resumed. The
 * deletion is cancelled as soon as it is admitted, which on a busy server can come after a small dataset is already
 * gone; then a larger one is made and stopped the same way. Only this setup is repeated, never what a test checks.
 */
async function stoppedDeletion(name: string, prepare: (id: number) => Promise<unknown>): Promise<number> {
    for (const size of [3_000, 30_000, 120_000]) {
        const { id } = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name }) });
        for (let from = 0; from < size; from += 10_000) {
            const samples = Array.from({ length: Math.min(10_000, size - from) }, (_, i) => ({ filepath: `s3://e2e-sealed/img_${from + i}.jpg` }));
            await apiFetch(cookie, `/datasets/${id}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
        }
        await prepare(id);
        const { job_id: jobId } = await apiFetch(cookie, `/datasets/${id}`, { method: 'DELETE' });
        await apiFetch(cookie, `/jobs/${jobId}/cancel`, { method: 'POST' }).catch(() => undefined);  // refused once it has ended
        await expect.poll(async () => (await apiFetch(cookie, `/jobs/${jobId}`)).state, { timeout: 60_000 }).toMatch(/^(cancelled|failed|succeeded)$/);
        if ((await apiFetch(cookie, `/jobs/${jobId}`)).state !== 'succeeded') {
            datasetIds.push(id);  // afterAll resumes the deletion and waits for it
            return id;
        }
    }
    throw new Error(`Every deletion of ${name} finished before it could be stopped`);
}

test('a dataset being deleted stays listed for its group, marked in words; the API says it is deleting', async ({ page }) => {
    test.setTimeout(180_000);
    const group = await newGroup('sealed');
    const kept = await createSmallDataset(cookie, `gp-${S}-kept`);
    datasetIds.push(kept);
    await share(cookie, kept, { group: group.name }, 'edit');
    const sealed = await stoppedDeletion(`gp-${S}-sealed`, id => share(cookie, id, { group: group.name }, 'view'));
    const listed = (await apiFetch(cookie, '/datasets')) as Array<{ id: number; deleting: boolean }>;
    expect(listed.find(dataset => dataset.id === sealed)?.deleting, 'the deletion stopped before it finished').toBe(true);

    const groups = (await apiFetch(cookie, '/groups')) as Array<{ id: number; datasets: Array<{ id: number; deleting: boolean }> }>;
    expect(groups.find(row => row.id === group.id)?.datasets).toEqual([
        { id: kept, name: `gp-${S}-kept`, level: 'edit', deleting: false },
        { id: sealed, name: `gp-${S}-sealed`, level: 'view', deleting: true },
    ]);
    await page.goto('/users');
    const list = groupItem(page, group.name).getByRole('list', { name: `Datasets shared with ${group.name}` });
    await expect(list.getByRole('listitem')).toHaveText([`gp-${S}-kept Edit`, `gp-${S}-sealed View (being deleted)`]);
    await expect(list.getByRole('link', { name: `gp-${S}-sealed`, exact: true })).toHaveAttribute('href', `/datasets/${sealed}`);
    // (afterAll resumes the deletion and waits for it.)
});

test('Escape closes the checklist holding the focus, as Cancel does; another open one keeps its ticks', async ({ page }) => {
    const first = await newGroup('esc-one');
    const second = await newGroup('esc-two');
    const sent = groupRequests(page);
    await page.goto('/users');
    const one = groupItem(page, first.name);
    const two = groupItem(page, second.name);
    await expect(toggleOf(one, first.name)).toBeVisible();
    const onLoad = sent.length;

    await toggleOf(one, first.name).click();
    await fillStable(filterOf(one, first.name), PREFIX);
    await tick(one, first.name, people.amy.username).check();
    await toggleOf(two, second.name).click();
    await fillStable(filterOf(two, second.name), PREFIX);
    await tick(two, second.name, people.ben.username).check();

    // The focus in the second's filter: Escape closes only the second.
    await filterOf(two, second.name).focus();
    await page.keyboard.press('Escape');
    await expect(toggleOf(two, second.name)).toHaveAttribute('aria-expanded', 'false');
    await expect(two.getByRole('checkbox')).toHaveCount(0);
    await expect(toggleOf(two, second.name)).toBeFocused();
    await expect(toggleOf(one, first.name)).toHaveAttribute('aria-expanded', 'true');
    await expect(tick(one, first.name, people.amy.username)).toBeChecked();
    await expect(one.getByRole('status')).toHaveText('1 selected');

    // The focus on a checkbox in the first: Escape closes it.
    await tick(one, first.name, people.amy.username).focus();
    await page.keyboard.press('Escape');
    await expect(toggleOf(one, first.name)).toHaveAttribute('aria-expanded', 'false');
    await expect(toggleOf(one, first.name)).toBeFocused();

    // Forgotten, as with Cancel; nothing sent.
    await toggleOf(two, second.name).click();
    await expect(filterOf(two, second.name)).toHaveValue('');
    await expect(two.getByRole('status')).toHaveText('0 selected');
    expect(sent.length, 'nothing sent').toBe(onLoad);
    expect(await membersOf(second.id)).toEqual([]);
});

test('if reading the people fails, the open checklist shows the server\'s words in its card', async ({ page }) => {
    test.setTimeout(60_000);
    const open = await newGroup('people-down');
    const closed = await newGroup('people-down-closed');
    // That one request fails, as a server error (the app retries it, so the words come after its retries).
    await page.route(url => url.pathname === '/api/users' && url.searchParams.get('people') === 'true', route => route.fulfill({
        status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'e2e: people broke' }),
    }));
    await page.goto('/users');
    const item = groupItem(page, open.name);
    await toggleOf(item, open.name).click();
    await expect(item.getByRole('alert')).toHaveText('e2e: people broke', { timeout: 20_000 });
    await expect(region(page).getByRole('alert'), 'the only alert in Groups').toHaveCount(1);
    await expect(groupItem(page, closed.name).getByRole('alert')).toHaveCount(0);
    await expect(item.getByRole('checkbox')).toHaveCount(0);
    await expect(item.getByText('No one else to add.')).toHaveCount(0);
    await expect(item.getByText(/^No user matches/)).toHaveCount(0);
    await expect(item.getByRole('status')).toHaveText('0 selected');
    await expect(addOf(item, open.name)).toBeDisabled();
    // The users table reads its own list.
    await expect(page.getByRole('row').filter({ hasText: people.amy.username })).toHaveCount(1);
    await cancelOf(item, open.name).click();
    await expect(toggleOf(item, open.name)).toHaveAttribute('aria-expanded', 'false');
});

test('thirty ticked are all added with one press, with at most six requests in flight at once', async ({ page }) => {
    test.setTimeout(180_000);
    const group = await newGroup('thirty');
    const crowd: Array<{ id: number; username: string }> = [];
    try {
        for (let i = 0; i < 30; i += 1) crowd.push(await createPerson(`t${String(i).padStart(2, '0')}`));
        // Each add takes a while, as on a busy server, so the requests overlap. Counted where it is exact: a request
        // is waiting from when it reaches the route until its answer is handed back to the page.
        let waiting = 0;
        let mostWaiting = 0;
        await page.route('**/api/groups/*/members/*', async route => {
            if (route.request().method() !== 'PUT') return route.continue();
            waiting += 1;
            mostWaiting = Math.max(mostWaiting, waiting);
            await new Promise(resolve => setTimeout(resolve, 300));
            const response = await route.fetch();
            waiting -= 1;
            return route.fulfill({ response });
        });
        // Every add the page sends. (Not a concurrency count: the page's request events see each answer a little after
        // the page does, so they overcount what is in flight; measured, 9 or 10 where the route saw 6.)
        let puts = 0;
        page.on('request', request => {
            if (request.method() === 'PUT' && /\/api\/groups\/\d+\/members\/\d+$/.test(new URL(request.url()).pathname)) puts += 1;
        });

        await page.goto('/users');
        const item = groupItem(page, group.name);
        await toggleOf(item, group.name).click();
        await fillStable(filterOf(item, group.name), `${PREFIX}t`);
        await expect(checklistOf(item, group.name).getByRole('checkbox')).toHaveCount(30);
        for (const user of crowd) await tick(item, group.name, user.username).check();
        await expect(addOf(item, group.name)).toHaveText('Add 30');
        await addOf(item, group.name).click();
        await expect(toggleOf(item, group.name)).toHaveAttribute('aria-expanded', 'false', { timeout: 60_000 });
        expect(puts, 'one request per person').toBe(30);
        expect(mostWaiting, `at most six waiting for an answer at once (the most: ${mostWaiting})`).toBeLessThanOrEqual(6);
        expect(await membersOf(group.id)).toEqual(crowd.map(user => user.username));
        await expect(region(page).getByRole('alert')).toHaveCount(0);
    } finally {
        await allSteps(crowd.map(user => () => apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' })));
    }
});
