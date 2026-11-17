import { test, expect, type Locator, type Page } from '@playwright/test';
import { API, adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, fillStable } from './helpers';
import {
    REASON, type TestUser, accessList, addMember, allSteps, createGroup, createSmallDataset, createUser, deleteGroup,
    openDataset, openSettings, share, signIn, stamp,
} from './access-helpers';

// Who has access to a dataset, and groups (issue 12). A manager of a dataset lists, changes, removes and adds its
// shares (users and groups, each at a level) in Dataset Settings → General → Access, where every change applies at
// once, and sets what every signed-in user gets with Visibility (saved with Save Changes). Admins make groups and their
// members under Settings → User Management; each member gets the group's level wherever the group has a share.

test.use({ storageState: AUTH_STATE });

const S = stamp();
let cookie = '';
let datasetId = 0;
let adminEmail = '';
let adminId = 0;
const groupIds: number[] = [];
const users: Record<'mgr' | 'alice' | 'bob', TestUser> = {} as Record<'mgr' | 'alice' | 'bob', TestUser>;
// Names that sort the same way however they are compared: group rows come first by name, then users by username.
const teamA = `team_a_e2e_${S}`;
const teamB = `team_b_e2e_${S}`;

test.beforeAll(async () => {
    test.setTimeout(120_000);
    cookie = await adminLogin();
    const admin = ((await apiFetch(cookie, '/users')) as Array<{ id: number; username: string; email: string }>).find(user => user.username === 'admin')!;
    [adminEmail, adminId] = [admin.email, admin.id];
    datasetId = await createSmallDataset(cookie, `e2e-access-groups-${S}`);
    for (const name of ['mgr', 'alice', 'bob'] as const) {
        users[name] = await createUser(cookie, `${name}_e2e_${S}`, 'power_user', `${name}-${S}@example.test`);
    }
    await share(cookie, datasetId, { user_id: users.mgr.id }, 'manage');
    await share(cookie, datasetId, { user_id: users.alice.id }, 'view');
    groupIds.push((await createGroup(cookie, teamA)).id, (await createGroup(cookie, teamB)).id);
    await share(cookie, datasetId, { group: teamA }, 'tag');
});

test.afterAll(async () => {
    test.setTimeout(150_000);
    await allSteps([
        async () => { if (datasetId) await deleteDatasetAndWait(cookie, datasetId, 120_000); },
        ...groupIds.map(id => () => deleteGroup(cookie, id)),
        ...Object.values(users).map(user => () => apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' })),
    ]);
});

async function openAccess(page: Page): Promise<Locator> {
    await openDataset(page, datasetId);
    const access = (await openSettings(page)).getByRole('region', { name: 'Access' });
    await expect(access).toBeVisible();
    return access;
}

/** What the picker offers, as [its optgroup's label, the option's label]. (Playwright's roles don't reach into optgroups.) */
const offered = (picker: Locator) => picker.evaluate(select => Array.from((select as HTMLSelectElement).options).map(option => [
    option.parentElement instanceof HTMLOptGroupElement ? option.parentElement.label : '',
    option.label,
]));

const accessPosted = (page: Page) => page.waitForResponse(r => new URL(r.url()).pathname === `/api/datasets/${datasetId}/access`
    && r.request().method() === 'POST');

test('a manager sees who has access, with levels and without email addresses, and changes it at once', async ({ browser }) => {
    test.setTimeout(120_000);
    const { context, page } = await signIn(browser, users.mgr);
    // Only a manager's own row at Manage asks first: nothing here does (an unexpected confirm would be dismissed).
    const questions: string[] = [];
    page.on('dialog', dialog => { questions.push(dialog.message()); void dialog.dismiss(); });
    try {
        let access = await openAccess(page);
        await expect(access.getByRole('heading', { name: 'Shared with', level: 4 })).toBeVisible();
        await expect(access.getByText(
            'Changes here apply at once. Each person has the highest of their own level, their groups\' and the everyone level.',
        )).toBeVisible();
        let list = access.getByRole('list', { name: 'Shared with' });
        const rows = list.getByRole('listitem');
        // Groups first, then users by username; the admin has the creator's edit share.
        await expect(rows).toHaveCount(4);
        await expect(rows.nth(0)).toContainText('Group');
        await expect(rows.nth(0)).toContainText(teamA);
        await expect(rows.nth(1)).toContainText('admin');
        await expect(rows.nth(1)).toContainText('administrator: Manage everywhere');
        await expect(rows.nth(2)).toContainText(users.alice.username);
        await expect(rows.nth(2)).not.toContainText('Manage everywhere');
        await expect(rows.nth(3)).toContainText(users.mgr.username);
        await expect(list.getByRole('combobox', { name: `Level for group ${teamA}` })).toHaveValue('tag');
        await expect(list.getByRole('combobox', { name: 'Level for admin' })).toHaveValue('edit');  // the creator's share, as stored
        await expect(list.getByRole('combobox', { name: `Level for ${users.alice.username}` })).toHaveValue('view');
        await expect(list.getByRole('combobox', { name: `Level for ${users.mgr.username}` })).toHaveValue('manage');
        await expect(list.getByRole('combobox', { name: 'Level for admin' }).getByRole('option')).toHaveText(['View', 'Tag', 'Edit', 'Manage']);
        // A manager who isn't an admin sees names only: no email address, no empty brackets.
        for (const row of [1, 2, 3]) {
            await expect(rows.nth(row)).not.toContainText('@');
            await expect(rows.nth(row)).not.toContainText(/[()]/);
        }

        // The picker offers what has no share yet; a new share is edit unless another level is chosen.
        const picker = access.getByRole('combobox', { name: 'User or group to share with' });
        const newLevel = access.getByRole('combobox', { name: 'Level for the new share' });
        const shareButton = access.getByRole('button', { name: 'Share', exact: true });
        await expect(picker).toHaveValue('');
        await expect.poll(() => offered(picker)).toContainEqual(['Users', users.bob.username]);
        const choices = await offered(picker);
        expect(choices[0]).toEqual(['', 'Add a user or group…']);
        expect(choices).toContainEqual(['Groups', teamB]);
        for (const shared of [teamA, users.alice.username, users.mgr.username, 'admin']) {
            expect(choices.map(choice => choice[1]), `${shared} has a share: not offered`).not.toContain(shared);
        }
        await expect(newLevel).toHaveValue('edit');
        await expect(shareButton).toBeDisabled();

        // Change a level: it applies at once and is there when the dialog is opened again.
        const aliceLevel = () => list.getByRole('combobox', { name: `Level for ${users.alice.username}` });
        const changed = accessPosted(page);
        await aliceLevel().selectOption('tag');
        expect((await changed).status()).toBe(201);
        await expect(aliceLevel()).toHaveValue('tag');
        await page.keyboard.press('Escape');
        await expect(access).toBeHidden();
        access = (await openSettings(page)).getByRole('region', { name: 'Access' });
        list = access.getByRole('list', { name: 'Shared with' });
        await expect(aliceLevel()).toHaveValue('tag');
        expect((await accessList(cookie, datasetId)).grants.find(row => row.user_id === users.alice.id)?.level).toBe('tag');

        // Remove a share: the row goes and the user is offered again.
        const removeAlice = list.getByRole('button', { name: `Remove ${users.alice.username}` });
        await expect(removeAlice).toHaveText('Remove');
        await expect(removeAlice).toHaveAttribute('title', `Remove ${users.alice.username}'s access`);
        await removeAlice.click();
        await expect(aliceLevel()).toHaveCount(0);
        await expect.poll(() => offered(access.getByRole('combobox', { name: 'User or group to share with' })))
            .toContainEqual(['Users', users.alice.username]);
        await expect.poll(async () => (await accessList(cookie, datasetId)).grants.some(row => row.user_id === users.alice.id)).toBe(false);

        // Add a group at the default level, then a user at a chosen one.
        const pickerNow = access.getByRole('combobox', { name: 'User or group to share with' });
        const levelNow = access.getByRole('combobox', { name: 'Level for the new share' });
        const shareNow = access.getByRole('button', { name: 'Share', exact: true });
        await expect(levelNow).toHaveValue('edit');
        await pickerNow.selectOption({ label: teamB });
        await expect(shareNow).toBeEnabled();
        await shareNow.click();
        await expect(list.getByRole('combobox', { name: `Level for group ${teamB}` })).toHaveValue('edit');
        await expect(pickerNow).toHaveValue('');
        await expect(rows.nth(1)).toContainText(teamB);  // groups by name
        await levelNow.selectOption('view');
        await pickerNow.selectOption({ label: users.bob.username });
        await shareNow.click();
        await expect(list.getByRole('combobox', { name: `Level for ${users.bob.username}` })).toHaveValue('view');
        await expect(pickerNow).toHaveValue('');
        await expect(levelNow).toHaveValue('view');  // the level keeps its value
        const after = await accessList(cookie, datasetId);
        expect(after.group_grants.find(row => row.name === teamB)?.level).toBe('edit');
        expect(after.grants.find(row => row.user_id === users.bob.id)?.level).toBe('view');

        // A group's share is removed by its own button.
        await list.getByRole('button', { name: `Remove group ${teamB}` }).click();
        await expect(list.getByRole('combobox', { name: `Level for group ${teamB}` })).toHaveCount(0);
        await expect.poll(async () => (await accessList(cookie, datasetId)).group_grants.some(row => row.name === teamB)).toBe(false);
        expect(questions, 'no confirm for another row').toEqual([]);
    } finally {
        await context.close();
        await allSteps([
            () => share(cookie, datasetId, { user_id: users.alice.id }, 'view'),
            () => apiFetch(cookie, `/datasets/${datasetId}/access/${users.bob.id}`, { method: 'DELETE' }).catch(() => undefined),
            () => apiFetch(cookie, `/datasets/${datasetId}/access/groups/${groupIds[1]}`, { method: 'DELETE' }).catch(() => undefined),
        ]);
    }
});

test('a guest\'s row says a guest gets view at most', async ({ browser }) => {
    const guest = await createUser(cookie, `guest_e2e_${S}`, 'guest');
    await share(cookie, datasetId, { user_id: guest.id }, 'edit');
    const { context, page } = await signIn(browser, users.mgr);
    try {
        const list = (await openAccess(page)).getByRole('list', { name: 'Shared with' });
        await expect(list.getByRole('listitem').filter({ hasText: guest.username })).toContainText('guest: View at most');
        await expect(list.getByRole('combobox', { name: `Level for ${guest.username}` })).toHaveValue('edit');
        await expect(list.getByRole('listitem').filter({ hasText: users.alice.username })).not.toContainText('View at most');
    } finally {
        await context.close();
        await apiFetch(cookie, `/users/${guest.id}`, { method: 'DELETE' });
    }
});

test('while a change is on its way the list and the picker wait for it', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.mgr);
    let release = () => {};
    const held = new Promise<void>(resolve => { release = resolve; });
    try {
        const access = await openAccess(page);
        const list = access.getByRole('list', { name: 'Shared with' });
        await page.route(`**/api/datasets/${datasetId}/access`, async route => {
            if (route.request().method() === 'POST') await held;
            await route.continue();
        });
        const aliceLevel = list.getByRole('combobox', { name: `Level for ${users.alice.username}` });
        await aliceLevel.selectOption('tag');
        await expect(list.getByRole('combobox', { name: 'Level for admin' })).toBeDisabled();
        await expect(list.getByRole('button', { name: `Remove ${users.mgr.username}` })).toBeDisabled();
        await expect(access.getByRole('combobox', { name: 'User or group to share with' })).toBeDisabled();
        release();
        await expect(aliceLevel).toHaveValue('tag');
        await expect(list.getByRole('combobox', { name: 'Level for admin' })).toBeEnabled();
        await expect(access.getByRole('combobox', { name: 'User or group to share with' })).toBeEnabled();
    } finally {
        release();
        await context.close();
        await share(cookie, datasetId, { user_id: users.alice.id }, 'view');
    }
});

test('an Access list that fails to load says why', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.mgr);
    try {
        await page.route(`**/api/datasets/${datasetId}/access`, route => (route.request().method() === 'GET'
            ? route.fulfill({ status: 500, json: { detail: 'e2e: the access list is unavailable' } })
            : route.continue()));
        const access = await openAccess(page);
        await expect(access.getByRole('alert')).toHaveText('e2e: the access list is unavailable');
    } finally {
        await context.close();
    }
});

test('at view, a user saves a view of their own and may share and delete it; the default star needs edit', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.alice);
    const name = `e2e-alices-view-${S}`;
    try {
        await openDataset(page, datasetId);
        await page.getByRole('button', { name: 'Saved Views' }).click();
        await fillStable(page.getByPlaceholder('Enter view name...'), name);
        await page.getByRole('button', { name: 'Save View' }).click();
        await expect(page.getByRole('button', { name: new RegExp(name) })).toBeVisible();
        await expect(page.getByTestId('view-copy-link')).toHaveCount(1);  // the only view in this dataset
        for (const action of ['Share View', 'Delete View']) {
            const button = page.getByRole('button', { name: action });
            await expect(button, `${action} of their own view at view`).toBeEnabled();
            await expect(button).not.toHaveAttribute('title', REASON.edit);
        }
        const star = page.getByTestId('view-default-toggle');
        await expect(star).toBeDisabled();
        await expect(star).toHaveAccessibleName(REASON.edit);
    } finally {
        await context.close();
        const views = (await apiFetch(cookie, `/datasets/${datasetId}/views`)) as Array<{ id: number; name: string }>;
        for (const view of views.filter(row => row.name === name)) await apiFetch(cookie, `/views/${view.id}`, { method: 'DELETE' });
    }
});

test('an admin sees each user\'s email address in the Access list', async ({ browser }) => {
    const { context, page } = await signIn(browser);
    try {
        const list = (await openAccess(page)).getByRole('list', { name: 'Shared with' });
        await expect(list.getByRole('listitem').filter({ hasText: users.mgr.username })).toContainText(`${users.mgr.username} (${users.mgr.email})`);
        const adminRow = list.getByRole('listitem').filter({ hasText: 'administrator: Manage everywhere' });
        await expect(adminRow).toContainText(`admin (${adminEmail})`);
    } finally {
        await context.close();
    }
});

test('a manager opens the dataset to every signed-in user at a level, saved with Save Changes', async ({ browser }) => {
    const before = await apiFetch(cookie, `/datasets/${datasetId}`);
    const { context, page } = await signIn(browser, users.mgr);
    try {
        let access = await openAccess(page);
        const visibility = () => access.getByRole('combobox', { name: 'Visibility' });
        const everyone = () => access.getByRole('combobox', { name: 'Everyone\'s level' });
        await expect(visibility()).toHaveValue('private');
        await expect(access.getByText('Only admins and the users and groups it is shared with can open this dataset.')).toBeVisible();
        await expect(everyone()).toHaveCount(0);

        // Shown as soon as Everyone signed in is picked, before saving.
        await visibility().selectOption('everyone');
        await expect(everyone()).toBeVisible();
        await expect(everyone().getByRole('option')).toHaveText(['View', 'Tag', 'Edit']);
        await expect(everyone()).toHaveValue('view');  // its stored level: View, for a dataset never made global
        await everyone().selectOption('tag');
        await expect(access.getByText(
            'Every signed-in user, demo guests included, can open this dataset at Tag (a guest at most View).',
        )).toBeVisible();
        const patch = page.waitForRequest(r => new URL(r.url()).pathname === `/api/datasets/${datasetId}` && r.method() === 'PATCH');
        await page.getByRole('dialog', { name: 'Dataset Settings' }).getByRole('button', { name: 'Save Changes' }).click();
        expect((await patch).postDataJSON()).toMatchObject({ is_global: true, everyone_level: 'tag' });
        await expect(access).toBeHidden();
        const saved = await apiFetch(cookie, `/datasets/${datasetId}`);
        expect([saved.is_global, saved.everyone_level]).toEqual([true, 'tag']);

        access = (await openSettings(page)).getByRole('region', { name: 'Access' });
        await expect(visibility()).toHaveValue('everyone');
        await expect(everyone()).toHaveValue('tag');

        // And private again.
        await visibility().selectOption('private');
        await expect(everyone()).toHaveCount(0);
        await page.getByRole('dialog', { name: 'Dataset Settings' }).getByRole('button', { name: 'Save Changes' }).click();
        await expect(access).toBeHidden();
        expect((await apiFetch(cookie, `/datasets/${datasetId}`)).is_global).toBe(false);
    } finally {
        await context.close();
        await apiFetch(cookie, `/datasets/${datasetId}`, {
            method: 'PATCH',
            body: JSON.stringify({ is_global: before.is_global, everyone_level: before.everyone_level }),
        });
    }
});

test('a dataset a manager opens to everyone without picking a level is open at View: a user with no share views it, its edit controls disabled', async ({ browser }) => {
    test.setTimeout(150_000);
    const opened = await createSmallDataset(cookie, `e2e-access-open-${S}`);
    const outsider = await createUser(cookie, `carol_e2e_${S}`);
    try {
        await share(cookie, opened, { user_id: users.mgr.id }, 'manage');
        const manager = await signIn(browser, users.mgr);
        try {
            await openDataset(manager.page, opened);
            const access = (await openSettings(manager.page)).getByRole('region', { name: 'Access' });
            const visibility = access.getByRole('combobox', { name: 'Visibility' });
            const everyone = access.getByRole('combobox', { name: 'Everyone\'s level' });
            await expect(visibility).toHaveValue('private');
            await visibility.selectOption('everyone');
            // Never made global, the dataset holds View: the select shows it, and saving without touching it keeps it.
            await expect(everyone).toHaveValue('view');
            await expect(access.getByText(
                'Every signed-in user, demo guests included, can open this dataset at View (a guest at most View).',
            )).toBeVisible();
            const patch = manager.page.waitForRequest(r => new URL(r.url()).pathname === `/api/datasets/${opened}` && r.method() === 'PATCH');
            await manager.page.getByRole('dialog', { name: 'Dataset Settings' }).getByRole('button', { name: 'Save Changes' }).click();
            expect((await patch).postDataJSON()).toMatchObject({ is_global: true });
            await expect(access).toBeHidden();
        } finally {
            await manager.context.close();
        }
        const saved = await apiFetch(cookie, `/datasets/${opened}`);
        expect([saved.is_global, saved.everyone_level]).toEqual([true, 'view']);

        const { context, page } = await signIn(browser, outsider);
        try {
            const read = await page.request.get(`/api/datasets/${opened}`);
            expect(read.status()).toBe(200);
            expect((await read.json()).access_level).toBe('view');
            await openDataset(page, opened);
            await page.getByTestId('grid-select-all').click();
            await expect(page.getByTestId('grid-selection-count')).toBeVisible({ timeout: 10_000 });
            const tag = page.getByTestId('grid-tag-button');
            await expect(tag).toBeDisabled();
            await expect(tag).toHaveAttribute('title', REASON.tag);
            const settings = await openSettings(page);
            const description = settings.getByRole('textbox', { name: 'Description', exact: true });
            await expect(description).toBeDisabled();
            await expect(description).toHaveAttribute('title', REASON.edit);
            await expect(settings.getByRole('region', { name: 'Access' }).getByTestId('dataset-access-own'))
                .toContainText('You have View access to this dataset.');
        } finally {
            await context.close();
        }
    } finally {
        await allSteps([
            () => deleteDatasetAndWait(cookie, opened, 120_000),
            () => apiFetch(cookie, `/users/${outsider.id}`, { method: 'DELETE' }),
        ]);
    }
});

/** The confirms a manager's own row at Manage asks first (contract A3). */
const lowerOwn = (level: string) => `Lower your own access to this dataset from Manage to ${level}?\n\n`
    + 'Unless one of your groups gives you Manage, you will no longer be able to change who has access, delete versions or '
    + 'delete the dataset, and you can\'t give yourself Manage again.';
const removeOwn = 'Remove your own share of this dataset?\n\n'
    + 'Unless one of your groups or the everyone level gives you access, you will no longer be able to open it. Even then, '
    + 'unless a group gives you Manage, you will no longer be able to change who has access, and you can\'t give yourself '
    + 'Manage again.';

/** Answer the next browser dialog and hand back its text. */
const answerNext = (page: Page, accept: boolean) => new Promise<string>(resolve => page.once('dialog', dialog => {
    resolve(dialog.message());
    void (accept ? dialog.accept() : dialog.dismiss());
}));

/** Every write to the dataset's shares the page sends. */
function shareWrites(page: Page): string[] {
    const sent: string[] = [];
    page.on('request', request => {
        if (request.method() !== 'GET' && new URL(request.url()).pathname.startsWith(`/api/datasets/${datasetId}/access`)) {
            sent.push(`${request.method()} ${new URL(request.url()).pathname}`);
        }
    });
    return sent;
}

test('a share change refused for the level shows the server\'s words, and the page catches up to the lowered level', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.mgr);
    try {
        await openDataset(page, datasetId);
        const dialog = await openSettings(page);
        const access = dialog.getByRole('region', { name: 'Access' });
        const aliceLevel = access.getByRole('combobox', { name: `Level for ${users.alice.username}` });
        await expect(aliceLevel).toHaveValue('view');
        // Meanwhile an admin lowers the manager to edit, so the open dialog is stale.
        await share(cookie, datasetId, { user_id: users.mgr.id }, 'edit');

        const refused = accessPosted(page);
        await aliceLevel.selectOption('tag');
        expect((await refused).status()).toBe(403);
        const error = access.getByTestId('dataset-access-error');
        await expect(error).toHaveText(REASON.manage);
        // Without a reload the level is read again, and the section and the controls follow it...
        await expect(access.getByTestId('dataset-access-own')).toContainText('You have Edit access to this dataset.');
        await expect(access.getByRole('list', { name: 'Shared with' })).toHaveCount(0);
        for (const control of [access.getByRole('combobox', { name: 'Visibility' }), dialog.getByRole('textbox', { name: 'Name', exact: true })]) {
            await expect(control).toBeDisabled();
            await expect(control).toHaveAttribute('title', REASON.manage);
        }
        await expect(dialog.getByRole('textbox', { name: 'Description', exact: true })).toBeEnabled();  // edit still allows it
        // ...and the server's words stay in the Access section, below the user's own level, until the dialog is closed.
        await expect(error).toHaveText(REASON.manage);
        await expect(access.getByRole('alert')).toHaveCount(1);
        await expect(error).toHaveRole('alert');
        const own = await access.getByTestId('dataset-access-own').elementHandle();
        expect(await error.evaluate((alert, lines) => Boolean(lines && lines.compareDocumentPosition(alert) & Node.DOCUMENT_POSITION_FOLLOWING), own))
            .toBe(true);
        expect((await accessList(cookie, datasetId)).grants.find(row => row.user_id === users.alice.id)?.level).toBe('view');
        await page.keyboard.press('Escape');
        await expect(dialog).toBeHidden();
        const reopened = (await openSettings(page)).getByRole('region', { name: 'Access' });
        await expect(reopened.getByTestId('dataset-access-own')).toContainText('You have Edit access to this dataset.');
        await expect(reopened.getByRole('alert')).toHaveCount(0);
    } finally {
        await context.close();
        await share(cookie, datasetId, { user_id: users.mgr.id }, 'manage');
        await share(cookie, datasetId, { user_id: users.alice.id }, 'view');
    }
});

test('a refusal that isn\'t about the level shows the server\'s words, and the next change clears them', async ({ browser }) => {
    // The manager keeps manage: what fails is a group deleted after the dialog was opened, still offered in its picker.
    const gone = await createGroup(cookie, `team_gone_e2e_${S}`);
    const { context, page } = await signIn(browser, users.mgr);
    try {
        const access = await openAccess(page);
        const picker = access.getByRole('combobox', { name: 'User or group to share with' });
        await expect.poll(() => offered(picker)).toContainEqual(['Groups', gone.name]);
        await deleteGroup(cookie, gone.id);
        await picker.selectOption({ label: gone.name });
        const refused = accessPosted(page);
        await access.getByRole('button', { name: 'Share', exact: true }).click();
        expect((await refused).status()).toBe(404);
        await expect(access.getByTestId('dataset-access-error')).toHaveText('Group not found');
        await expect(access.getByRole('list', { name: 'Shared with' })).toBeVisible();  // still a manager

        const aliceLevel = access.getByRole('combobox', { name: `Level for ${users.alice.username}` });
        const accepted = accessPosted(page);
        await aliceLevel.selectOption('tag');
        expect((await accepted).status()).toBe(201);
        await expect(access.getByTestId('dataset-access-error')).toHaveCount(0);
        await expect(access.getByRole('alert')).toHaveCount(0);
        await expect(aliceLevel).toHaveValue('tag');
    } finally {
        await context.close();
        await deleteGroup(cookie, gone.id);
        await share(cookie, datasetId, { user_id: users.alice.id }, 'view');
    }
});

test('a manager lowering their own share is asked first: dismissed, nothing is sent; accepted, the section switches to their level', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.mgr);
    const sent = shareWrites(page);
    try {
        let access = await openAccess(page);
        const own = () => access.getByRole('combobox', { name: `Level for ${users.mgr.username}` });
        const dismissed = answerNext(page, false);
        await own().selectOption('edit');
        expect(await dismissed).toBe(lowerOwn('Edit'));
        await expect(own()).toHaveValue('manage');
        // Opened again, the list is the server's: still Manage, and nothing was sent.
        await page.keyboard.press('Escape');
        await expect(access).toBeHidden();
        access = (await openSettings(page)).getByRole('region', { name: 'Access' });
        await expect(own()).toHaveValue('manage');
        expect(sent).toEqual([]);
        expect((await accessList(cookie, datasetId)).grants.find(row => row.user_id === users.mgr.id)?.level).toBe('manage');

        const accepted = answerNext(page, true);
        const lowered = accessPosted(page);
        await own().selectOption('edit');
        expect(await accepted).toBe(lowerOwn('Edit'));
        expect((await lowered).status()).toBe(201);
        await expect(access.getByTestId('dataset-access-own')).toContainText('You have Edit access to this dataset.');
        await expect(access.getByRole('list', { name: 'Shared with' })).toHaveCount(0);
        const visibility = access.getByRole('combobox', { name: 'Visibility' });
        await expect(visibility).toBeDisabled();
        await expect(visibility).toHaveAttribute('title', REASON.manage);
    } finally {
        await context.close();
        await share(cookie, datasetId, { user_id: users.mgr.id }, 'manage');
    }
});

test('a manager removing their own share is asked first: dismissed, the row stays; accepted, a group\'s level is what is left', async ({ browser }) => {
    const self = await createUser(cookie, `self_e2e_${S}`);
    const team = await createGroup(cookie, `team_self_e2e_${S}`, [self]);
    await share(cookie, datasetId, { user_id: self.id }, 'manage');
    await share(cookie, datasetId, { group: team.name }, 'view');
    const { context, page } = await signIn(browser, self);
    const sent = shareWrites(page);
    try {
        let access = await openAccess(page);
        const remove = () => access.getByRole('list', { name: 'Shared with' }).getByRole('button', { name: `Remove ${self.username}` });
        const dismissed = answerNext(page, false);
        await remove().click();
        expect(await dismissed).toBe(removeOwn);
        await page.keyboard.press('Escape');
        await expect(access).toBeHidden();
        access = (await openSettings(page)).getByRole('region', { name: 'Access' });
        await expect(access.getByRole('combobox', { name: `Level for ${self.username}` })).toHaveValue('manage');
        expect(sent).toEqual([]);

        const accepted = answerNext(page, true);
        const removed = page.waitForResponse(r => new URL(r.url()).pathname === `/api/datasets/${datasetId}/access/${self.id}`
            && r.request().method() === 'DELETE');
        await remove().click();
        expect(await accepted).toBe(removeOwn);
        expect((await removed).status()).toBe(204);
        await expect(access.getByTestId('dataset-access-own')).toContainText('You have View access to this dataset.');
        await expect(access.getByRole('list', { name: 'Shared with' })).toHaveCount(0);
        expect((await accessList(cookie, datasetId)).grants.some(row => row.user_id === self.id)).toBe(false);
    } finally {
        await context.close();
        await allSteps([
            () => deleteGroup(cookie, team.id),
            () => apiFetch(cookie, `/users/${self.id}`, { method: 'DELETE' }),
        ]);
    }
});

test('nobody else is asked: an admin changing their own row, or a manager through a group changing their own row below Manage', async ({ browser }) => {
    const viaGroup = await createUser(cookie, `gmgr_e2e_${S}`);
    const team = await createGroup(cookie, `team_mgrs_e2e_${S}`, [viaGroup]);
    await share(cookie, datasetId, { user_id: adminId }, 'manage');
    await share(cookie, datasetId, { user_id: viaGroup.id }, 'edit');
    await share(cookie, datasetId, { group: team.name }, 'manage');
    try {
        for (const [who, user, from, to] of [['an admin', undefined, 'manage', 'edit'], ['a manager through a group', viaGroup, 'edit', 'view']] as const) {
            const { context, page } = await signIn(browser, user);
            const questions: string[] = [];
            page.on('dialog', dialog => { questions.push(dialog.message()); void dialog.dismiss(); });
            try {
                const access = await openAccess(page);
                const own = access.getByRole('combobox', { name: `Level for ${user ? user.username : 'admin'}` });
                await expect(own).toHaveValue(from);
                const changed = accessPosted(page);
                await own.selectOption(to);
                expect((await changed).status(), `${who}: sent without asking`).toBe(201);
                await expect(own).toHaveValue(to);
                expect(questions, `${who}: no confirm`).toEqual([]);
            } finally {
                await context.close();
            }
        }
    } finally {
        await allSteps([
            () => share(cookie, datasetId, { user_id: adminId }, 'edit'),
            () => deleteGroup(cookie, team.id),
            () => apiFetch(cookie, `/users/${viaGroup.id}`, { method: 'DELETE' }),
        ]);
    }
});

test('an admin creates, renames, fills, empties and deletes a group under User Management', async ({ page }) => {
    test.setTimeout(120_000);
    const first = `e2e-group-${S}`;
    const renamed = `e2e-group-renamed-${S}`;
    const groupId = async (name: string) => ((await apiFetch(cookie, '/groups')) as Array<{ id: number; name: string; members: Array<{ id: number }> }>)
        .find(group => group.name === name);
    try {
        await page.goto('/users');
        const groups = page.getByRole('region', { name: 'Groups' });
        await expect(groups.getByText('Share a dataset with a group in its Dataset Settings: each member gets the group\'s level there.')).toBeVisible();
        const nameBox = groups.getByRole('textbox', { name: 'New group name' });
        const create = groups.getByRole('button', { name: 'Create Group' });
        await expect(nameBox).toHaveAttribute('placeholder', 'New group name');
        await expect(create).toBeDisabled();
        await fillStable(nameBox, first);
        await create.click();
        await expect(groups.getByRole('listitem', { name: `Group ${first}` })).toBeVisible();
        await expect(nameBox).toHaveValue('');
        expect(await groupId(first)).toBeTruthy();

        // The same name again is refused, with the server's words.
        await fillStable(nameBox, first);
        await nameBox.press('Enter');
        await expect(groups.getByRole('alert')).toHaveText('A group with this name already exists');

        // Rename.
        await groups.getByRole('button', { name: `Rename group ${first}` }).click();
        const newName = groups.getByRole('textbox', { name: `New name for group ${first}` });
        await expect(newName).toHaveValue(first);
        await expect(newName).toBeFocused();
        await fillStable(newName, renamed);
        await groups.getByRole('listitem', { name: `Group ${first}` }).getByRole('button', { name: 'Save', exact: true }).click();
        const item = groups.getByRole('listitem', { name: `Group ${renamed}` });
        await expect(item).toBeVisible();
        await expect(groups.getByRole('listitem', { name: `Group ${first}` })).toHaveCount(0);
        expect(await groupId(renamed)).toBeTruthy();

        // A member in (from the checklist the group opens), then out.
        const members = item.getByRole('list', { name: `Members of ${renamed}` });
        await expect(members.getByRole('listitem')).toHaveText(['No members yet.']);
        const open = item.getByRole('button', { name: `Add members to ${renamed}`, exact: true });
        await open.click();
        await fillStable(item.getByRole('textbox', { name: `Find a user to add to ${renamed}` }), users.bob.username);
        const add = item.getByRole('button', { name: `Add the selected users to ${renamed}` });
        await expect(add).toHaveText('Add');
        await expect(add).toBeDisabled();
        await item.getByRole('group', { name: `Users to add to ${renamed}` }).getByRole('checkbox', { name: users.bob.username, exact: true }).check();
        await expect(add).toHaveText('Add 1');
        await add.click();
        await expect(members.getByRole('listitem').filter({ hasText: users.bob.username })).toBeVisible();
        await expect(open).toHaveAttribute('aria-expanded', 'false');
        expect((await groupId(renamed))?.members.map(member => member.id)).toEqual([users.bob.id]);
        const remove = members.getByRole('button', { name: `Remove ${users.bob.username} from ${renamed}` });
        await expect(remove).toHaveText('×');
        await expect(remove).toHaveAttribute('title', `Remove ${users.bob.username} from the group`);
        await remove.click();
        await expect(members.getByRole('listitem')).toHaveText(['No members yet.']);
        expect((await groupId(renamed))?.members).toEqual([]);

        // Delete: the confirm says what goes with it; dismissed, nothing changes; accepted, the group and its shares go.
        await share(cookie, datasetId, { group: renamed }, 'view');
        const questions: string[] = [];
        page.once('dialog', dialog => { questions.push(dialog.message()); void dialog.dismiss(); });
        await item.getByRole('button', { name: `Delete group ${renamed}` }).click();
        await expect.poll(() => questions.length).toBe(1);
        expect(questions[0]).toBe(
            `Delete the group "${renamed}"? Its members keep their accounts, and every dataset share the group has goes with it.`,
        );
        await expect(item).toBeVisible();
        expect(await groupId(renamed)).toBeTruthy();
        page.once('dialog', dialog => void dialog.accept());
        await item.getByRole('button', { name: `Delete group ${renamed}` }).click();
        await expect(item).toHaveCount(0);
        expect(await groupId(renamed)).toBeUndefined();
        expect((await accessList(cookie, datasetId)).group_grants.some(row => row.name === renamed)).toBe(false);
    } finally {
        for (const name of [first, renamed]) {
            const left = await groupId(name);
            if (left) await deleteGroup(cookie, left.id);
        }
    }
});

test('the Groups member picker offers only active people; the users table still lists every account', async ({ page }) => {
    test.setTimeout(120_000);
    // Three accounts that aren't people to share with: a deactivated user, a demo guest and a follower's machine account.
    const inactive = await createUser(cookie, `inactive_e2e_${S}`);
    await apiFetch(cookie, `/users/${inactive.id}`, { method: 'PATCH', body: JSON.stringify({ is_active: false }) });
    const demoLogin = await fetch(`${API}/auth/demo-login`, { method: 'POST' });
    expect(demoLogin.status, 'a demo guest (the e2e backend runs with DEMO_MODE)').toBe(200);
    const demo: string = (await demoLogin.json()).username;
    const follower = await apiFetch(cookie, '/followers', { method: 'POST', body: JSON.stringify({ name: `e2e-people-${S}` }) });
    const machine = `follower:${follower.name}`;
    const everyone = (await apiFetch(cookie, '/users')) as Array<{ id: number; username: string }>;
    const ids = Object.fromEntries(everyone.map(user => [user.username, user.id]));
    const group = await createGroup(cookie, `team_people_e2e_${S}`);
    const notPeople = [inactive.username, demo, machine];
    try {
        // The server: people=true leaves them out; the plain list keeps them.
        const people = ((await apiFetch(cookie, '/users?people=true')) as Array<{ username: string }>).map(user => user.username);
        for (const name of notPeople) {
            expect(everyone.map(user => user.username), `${name}: in the plain list`).toContain(name);
            expect(people, `${name}: not a person to share with`).not.toContain(name);
        }
        expect(people).toContain(users.bob.username);

        await page.goto('/users');
        for (const name of notPeople) await expect(page.getByRole('row').filter({ hasText: name }), `${name}: in the users table`).toHaveCount(1);
        const item = page.getByRole('region', { name: 'Groups' }).getByRole('listitem', { name: `Group ${group.name}`, exact: true });
        await item.getByRole('button', { name: `Add members to ${group.name}`, exact: true }).click();
        const filter = item.getByRole('textbox', { name: `Find a user to add to ${group.name}` });
        const checklist = item.getByRole('group', { name: `Users to add to ${group.name}` });
        await fillStable(filter, users.bob.username);
        await expect(checklist.getByRole('checkbox', { name: users.bob.username, exact: true })).toBeVisible();
        // Each filtered for by its exact name: no checkbox, and the checklist says nobody matches.
        for (const name of notPeople) {
            await fillStable(filter, name);
            await expect(checklist.getByRole('checkbox', { name, exact: true }), `${name}: not offered as a member`).toHaveCount(0);
            await expect(checklist).toContainText(`No user matches "${name}".`);
        }

        // The dataset share picker leaves them out too.
        const picker = (await openAccess(page)).getByRole('combobox', { name: 'User or group to share with' });
        await expect.poll(() => offered(picker)).toContainEqual(['Users', users.bob.username]);
        const shareable = (await offered(picker)).map(option => option[1]);
        for (const name of notPeople) expect(shareable, `${name}: not offered for a share`).not.toContain(name);
    } finally {
        await allSteps([
            () => deleteGroup(cookie, group.id),
            // Its account stays, renamed and inactive, for what it wrote; then it is deleted like the others.
            () => apiFetch(cookie, `/followers/${follower.id}`, { method: 'DELETE' }),
            ...[inactive.id, ids[demo], ids[machine]].map(id => () => apiFetch(cookie, `/users/${id}`, { method: 'DELETE' })),
        ]);
    }
});

test('a user who isn\'t an admin has no Groups', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.alice);
    try {
        await page.goto('/users');
        await expect(page.getByText('Access Denied')).toBeVisible();
        await expect(page.getByRole('region', { name: 'Groups' })).toHaveCount(0);
        await expect(page.getByRole('button', { name: 'Create Group' })).toHaveCount(0);
    } finally {
        await context.close();
    }
});

test('a user removed from a group loses what the group gave on their next page load', async ({ browser, page }) => {
    test.setTimeout(120_000);
    const member = await createUser(cookie, `gm_e2e_${S}`);
    const team = await createGroup(cookie, `team_c_e2e_${S}`);
    await addMember(cookie, team.id, member);
    await share(cookie, datasetId, { user_id: member.id }, 'view');
    await share(cookie, datasetId, { group: team.name }, 'edit');
    const them = await signIn(browser, member);
    try {
        await openDataset(them.page, datasetId);
        let dialog = await openSettings(them.page);
        await expect(dialog.getByTestId('dataset-access-own')).toContainText('You have Edit access to this dataset.');
        await expect(dialog.getByRole('textbox', { name: 'Description', exact: true })).toBeEnabled();

        // An admin takes them out of the group in User Management.
        await page.goto('/users');
        const members = page.getByRole('region', { name: 'Groups' }).getByRole('list', { name: `Members of ${team.name}` });
        await members.getByRole('button', { name: `Remove ${member.username} from ${team.name}` }).click();
        await expect(members.getByRole('listitem')).toHaveText(['No members yet.']);

        await openDataset(them.page, datasetId);
        dialog = await openSettings(them.page);
        await expect(dialog.getByTestId('dataset-access-own')).toContainText('You have View access to this dataset.');
        const description = dialog.getByRole('textbox', { name: 'Description', exact: true });
        await expect(description).toBeDisabled();
        await expect(description).toHaveAttribute('title', REASON.edit);
    } finally {
        await them.context.close();
        await allSteps([
            () => deleteGroup(cookie, team.id),
            () => apiFetch(cookie, `/users/${member.id}`, { method: 'DELETE' }),
        ]);
    }
});
