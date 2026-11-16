import { test, expect, type Locator, type Page } from '@playwright/test';
import { adminLogin, apiFetch, createFixtureDataset, deleteDatasetAndWait } from './api';
import { fillStable } from './helpers';
import {
    LEVELS, REASON, type Level, type TestUser, accessList, allSteps, atLeast, capitalized, createGroup, createSmallDataset,
    createUser, deleteGroup, openDataset, openSettings, share, signIn, stamp,
} from './access-helpers';

// Per-dataset access levels in the UI (issue 12). A user's level in a dataset (view < tag < edit < manage) is the
// highest of their own share, their groups' shares and a global dataset's everyone level; a guest gets view at most and
// an admin has manage everywhere. A control the level doesn't allow is shown disabled and says why: the server's own
// refusal, "This needs <level> access to the dataset", in its title (its accessible description).
//
// One private copy of the fixture, shared at each level with a user of its own, plus a user whose level comes only
// from a group, a guest shared at edit and the admin. The table below names each gated control, where it is and the
// level it needs; each user's test walks it.

const S = stamp();
let cookie = '';
let datasetId = 0;
let datasetName = '';
let groupId = 0;
const users: Record<string, TestUser> = {};

test.beforeAll(async () => {
    test.setTimeout(180_000);
    cookie = await adminLogin();
    datasetName = `e2e-access-levels-${S}`;
    datasetId = await createFixtureDataset(cookie, datasetName);
    // Something for each gated control to act on: a version, another user's (the admin's) public view, a map re-fit
    // schedule (off, so it never runs), a concept.
    await apiFetch(cookie, `/datasets/${datasetId}/versions`, { method: 'POST', body: JSON.stringify({ name: 'v1' }) });
    const samples = await apiFetch(cookie, `/datasets/${datasetId}/samples/query`, { method: 'POST', body: JSON.stringify({ limit: 6 }) });
    await apiFetch(cookie, `/datasets/${datasetId}/concepts`, {
        method: 'POST',
        body: JSON.stringify({
            name: `e2e-admins-concept-${S}`, embedding_field: 'e2e_clip', kind: 'seed',
            seed_ids: samples.items.map((sample: { id: number }) => sample.id),
        }),
    });
    await apiFetch(cookie, `/datasets/${datasetId}/views`, {
        method: 'POST',
        body: JSON.stringify({ name: `e2e-admins-view-${S}`, is_public: true, content: { filters: {} } }),
    });
    await apiFetch(cookie, `/datasets/${datasetId}/schedules`, {
        method: 'POST',
        body: JSON.stringify({ job_type: 'map_refit', target: 'e2e_clip', enabled: false }),
    });
    for (const level of LEVELS) {
        users[level] = await createUser(cookie, `e2e_al_${level}_${S}`);
        await share(cookie, datasetId, { user_id: users[level].id }, level);
    }
    users.member = await createUser(cookie, `e2e_al_member_${S}`);
    const group = await createGroup(cookie, `e2e-al-editors-${S}`, [users.member]);
    groupId = group.id;
    await share(cookie, datasetId, { group: group.name }, 'edit');
    users.guest = await createUser(cookie, `e2e_al_guest_${S}`, 'guest');
    await share(cookie, datasetId, { user_id: users.guest.id }, 'edit');
});

test.afterAll(async () => {
    test.setTimeout(180_000);
    // The dataset first: its samples and shares name these users (who tagged, who granted).
    await allSteps([
        async () => { if (datasetId) await deleteDatasetAndWait(cookie, datasetId, 120_000); },
        async () => { if (groupId) await deleteGroup(cookie, groupId); },
        ...Object.values(users).map(user => () => apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' })),
    ]);
});

type Area =
    | 'sample view' | 'dataset page' | 'Saved Views' | 'Jobs › History' | 'Jobs › Schedules' | 'Settings › General'
    | 'Settings › Fields' | 'Settings › Indexes' | 'Settings › Versions' | 'Settings › Visualization' | 'Concepts panel'
    | 'home page';
type Scope = Page | Locator;

type Control = {
    area: Area;
    control: string;
    needs: Exclude<Level, 'view'>;
    find: (scope: Scope) => Locator;
    /** False where something else also keeps it disabled at a level that allows it (nothing picked or typed yet). */
    allowedMeansEnabled?: false;
    /** Its ordinary tooltip where the level allows it, when the contract names one. */
    allowedTitle?: string;
    /** Where the reason is when it is not on the control itself: the Create Version fieldset's title. */
    reasonOn?: (scope: Scope) => Locator;
    /** The default-view star gets its name from its title, so the reason is its name. */
    reasonIsName?: true;
    /** Shown only to some users: Share View is hidden from a public demo's guests (can_share_views). */
    shownWhen?: (user: { canShareViews: boolean }) => boolean;
};

// Every gated control this fixture can show, in the order a test reaches them. The contract's other gated controls
// need a setup this fixture doesn't have (a mirror, object rows, overlay or class-probability fields, a duplicates
// collapse, vectors without a map, a stopped job or deletion).
const CONTROLS: Control[] = [
    { area: 'sample view', control: 'Tag', needs: 'tag', find: s => s.getByTestId('modal-tag') },
    {
        area: 'dataset page', control: 'Tag (the grid, with every sample selected)', needs: 'tag',
        find: s => s.getByTestId('grid-tag-button'), allowedTitle: 'Tag selected samples',
    },
    {
        area: 'dataset page', control: 'Promote Field (Create Index) on idx', needs: 'edit',
        find: s => s.getByTestId('panel-filters').getByTestId('field-section-idx').getByRole('button', { name: 'Promote Field (Create Index)' }),
    },
    { area: 'Saved Views', control: 'default star of another user\'s public view', needs: 'edit', find: s => s.getByTestId('view-default-toggle'), reasonIsName: true },
    {
        area: 'Saved Views', control: 'Share View of another user\'s public view', needs: 'edit',
        find: s => s.getByRole('button', { name: 'Share View' }), shownWhen: user => user.canShareViews,
    },
    { area: 'Saved Views', control: 'Delete View of another user\'s public view', needs: 'edit', find: s => s.getByRole('button', { name: 'Delete View' }) },
    { area: 'Jobs › History', control: 'Retry of a failed vector index build', needs: 'edit', find: s => s.getByTestId('job-retry-index') },
    { area: 'Jobs › Schedules', control: '+ Add schedule', needs: 'edit', find: s => s.getByTestId('add-schedule') },
    { area: 'Jobs › Schedules', control: 'map re-fit On/Off', needs: 'edit', find: s => s.getByTestId('schedule-toggle') },
    { area: 'Jobs › Schedules', control: 'map re-fit Run now', needs: 'edit', find: s => s.getByTestId('schedule-run-now') },
    { area: 'Jobs › Schedules', control: 'map re-fit Edit', needs: 'edit', find: s => s.getByTestId('schedule-edit') },
    { area: 'Jobs › Schedules', control: 'map re-fit Delete', needs: 'edit', find: s => s.getByTestId('schedule-delete') },
    { area: 'Settings › General', control: 'Name', needs: 'manage', find: s => s.getByRole('textbox', { name: 'Name', exact: true }) },
    { area: 'Settings › General', control: 'Description', needs: 'edit', find: s => s.getByRole('textbox', { name: 'Description', exact: true }) },
    { area: 'Settings › General', control: 'Media field to add', needs: 'edit', find: s => s.getByRole('combobox', { name: 'Media field to add' }) },
    {
        area: 'Settings › General', control: 'Add (a media field; also waits for one to be picked)', needs: 'edit',
        find: s => s.getByRole('button', { name: 'Add', exact: true }), allowedMeansEnabled: false,
    },
    { area: 'Settings › General', control: '× on the filepath media field', needs: 'edit', find: s => s.getByRole('button', { name: '×', exact: true }) },
    { area: 'Settings › General', control: 'Grid opens on (the default media field)', needs: 'edit', find: s => s.getByRole('combobox', { name: 'Grid opens on' }) },
    {
        area: 'Settings › General', control: 'Sample view opens on (the default media field)', needs: 'edit',
        find: s => s.getByRole('combobox', { name: 'Sample view opens on' }),
    },
    { area: 'Settings › General', control: 'Visibility', needs: 'manage', find: s => s.getByRole('combobox', { name: 'Visibility' }) },
    { area: 'Settings › General', control: 'Save Changes', needs: 'edit', find: s => s.getByRole('button', { name: 'Save Changes' }) },
    { area: 'Settings › Fields', control: 'Declared type for idx', needs: 'edit', find: s => s.getByRole('combobox', { name: 'Declared type for idx' }) },
    {
        area: 'Settings › Fields', control: 'Save type for idx (also waits for a change)', needs: 'edit',
        find: s => s.getByRole('button', { name: 'Save type for idx' }), allowedMeansEnabled: false,
    },
    { area: 'Settings › Indexes', control: 'Display for idx', needs: 'edit', find: s => s.getByRole('combobox', { name: 'Display for idx' }) },
    {
        area: 'Settings › Indexes', control: 'Indexed (No) for idx', needs: 'edit',
        find: s => s.getByRole('row', { name: /^idx number/ }).getByRole('button', { name: 'No', exact: true }),
    },
    {
        area: 'Settings › Versions', control: 'Create Version (its version name; the reason on the fieldset)', needs: 'edit',
        find: s => s.getByRole('textbox', { name: 'Version name (e.g., v2.0)' }),
        reasonOn: s => s.getByRole('group', { name: 'Create Version' }),
    },
    { area: 'Settings › Versions', control: 'Revert to this snapshot', needs: 'edit', find: s => s.getByRole('button', { name: 'Revert to this snapshot' }) },
    { area: 'Settings › Versions', control: 'Delete (a version)', needs: 'manage', find: s => s.getByRole('button', { name: 'Delete', exact: true }) },
    { area: 'Settings › Visualization', control: 'Save Changes', needs: 'edit', find: s => s.getByRole('button', { name: 'Save Changes' }) },
    { area: 'Concepts panel', control: 'From selection (12), named', needs: 'edit', find: s => s.getByTestId('concept-create') },
    {
        area: 'Concepts panel', control: 'From text (also waits for a prompt)', needs: 'edit',
        find: s => s.getByTestId('concept-create-text'), allowedMeansEnabled: false,
    },
    { area: 'Concepts panel', control: '✕ on a concept', needs: 'edit', find: s => s.getByTestId('concept-delete') },
    { area: 'Concepts panel', control: 'Auto-score new images hourly on a concept', needs: 'edit', find: s => s.getByTestId('concept-auto-rescore') },
    // The home page's scope is this dataset's card.
    { area: 'home page', control: 'Delete dataset (this dataset\'s card)', needs: 'manage', find: s => s.getByRole('button', { name: 'Delete dataset' }) },
];

async function checkControl(scope: Scope, control: Control, level: Level, user: { canShareViews: boolean }) {
    const what = `${control.area} › ${control.control}, at ${level} (needs ${control.needs})`;
    const locator = control.find(scope);
    if (control.shownWhen && !control.shownWhen(user)) {
        await expect.soft(locator, `${what}: not shown to this user`).toHaveCount(0);
        return;
    }
    const reason = REASON[control.needs];
    if (atLeast(level, control.needs)) {
        if (control.allowedMeansEnabled === false) await expect.soft(locator, `${what}: shown`).toHaveCount(1);
        else await expect.soft(locator, `${what}: enabled`).toBeEnabled();
        await expect.soft(control.reasonOn ? control.reasonOn(scope) : locator, `${what}: no refusal as its title`)
            .not.toHaveAttribute('title', reason);
        if (control.allowedTitle) await expect.soft(locator, `${what}: its ordinary title`).toHaveAttribute('title', control.allowedTitle);
        return;
    }
    await expect.soft(locator, `${what}: disabled`).toBeDisabled();
    if (control.reasonOn) {
        await expect.soft(control.reasonOn(scope), `${what}: the reason as the fieldset's title`).toHaveAttribute('title', reason);
        await expect.soft(control.reasonOn(scope), `${what}: the reason as the fieldset's accessible description`)
            .toHaveAccessibleDescription(reason);
    } else if (control.reasonIsName) {
        await expect.soft(locator, `${what}: the reason as its title`).toHaveAttribute('title', reason);
        await expect.soft(locator, `${what}: the reason as its name`).toHaveAccessibleName(reason);
    } else {
        await expect.soft(locator, `${what}: the reason as its title`).toHaveAttribute('title', reason);
        await expect.soft(locator, `${what}: the reason as its accessible description`).toHaveAccessibleDescription(reason);
    }
}

/** A failed vector index build in the Jobs history, so Retry shows (the level, not the job list, is under test). */
async function aFailedIndexBuildInTheHistory(page: Page) {
    await page.route(`**/api/datasets/${datasetId}/jobs?*`, route => route.fulfill({
        json: {
            items: [{
                id: `e2e-failed-ann-${S}`, kind: 'ann_index_build', state: 'failed', dataset_id: datasetId, field: 'e2e_clip',
                lane: 'user', placement: 'server', started_by: 'admin', progress: { done: 0, total: null, message: null },
                created_at: '2026-11-11T12:00:00Z', started_at: '2026-11-11T12:00:05Z', finished_at: '2026-11-11T12:01:00Z',
                attempt: 1, cancel_requested: false, error: { type: 'RuntimeError', message: 'e2e: the build failed' },
            }],
            next_cursor: null,
        },
    }));
}

/** Open an area and return what its controls are found in. */
async function openArea(page: Page, area: Area, level: Level): Promise<Scope> {
    const settings = page.getByRole('dialog', { name: 'Dataset Settings' });
    switch (area) {
        case 'sample view':
            await openDataset(page, datasetId);
            await page.getByTestId('grid-cell').first().click();
            await expect(page.getByTestId('modal-tag')).toBeVisible({ timeout: 10_000 });
            return page;
        case 'dataset page':
            await page.keyboard.press('Escape');
            await expect(page.getByTestId('modal-tag')).toHaveCount(0);
            await page.getByTestId('grid-select-all').click();
            await expect(page.getByTestId('grid-selection-count')).toBeVisible({ timeout: 10_000 });
            return page;
        case 'Saved Views':
            await page.getByRole('button', { name: 'Saved Views' }).click();
            await expect(page.getByTestId('view-copy-link')).toHaveCount(1);
            return page;
        case 'Jobs › History': {
            await page.getByRole('button', { name: 'Close Views' }).click();
            await page.getByTestId('open-job-history').click();
            const dialog = page.getByTestId('job-history-dialog');
            await expect(dialog.getByTestId('job-history-item')).toHaveCount(1);
            return dialog;
        }
        case 'Jobs › Schedules': {
            const dialog = page.getByTestId('job-history-dialog');
            await dialog.getByTestId('jobs-schedules-tab').click();
            const schedules = dialog.getByTestId('dataset-schedules');
            await expect(schedules.getByTestId('schedule-row')).toHaveCount(1);
            return schedules;
        }
        case 'Settings › General': {
            await page.keyboard.press('Escape');
            await expect(page.getByTestId('job-history-dialog')).toHaveCount(0);
            const dialog = await openSettings(page);
            await expect(dialog.getByRole('region', { name: 'Access' })).toBeVisible();
            return dialog;
        }
        case 'Settings › Fields':
        case 'Settings › Indexes':
        case 'Settings › Versions':
        case 'Settings › Visualization': {
            const tab = area.split(' › ')[1];
            await settings.getByRole('button', { name: tab, exact: true }).click();
            if (tab === 'Fields') await expect(settings.getByRole('combobox', { name: 'Declared type for idx' })).toBeVisible();
            if (tab === 'Indexes') await expect(settings.getByRole('row', { name: /^idx number/ })).toBeVisible();
            if (tab === 'Versions') await expect(settings.getByRole('link', { name: 'Browse snapshot' })).toBeVisible();
            return settings;
        }
        case 'Concepts panel':
            await page.keyboard.press('Escape');
            await expect(settings).toHaveCount(0);
            await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('concepts');
            await expect(page.getByTestId('concept-create')).toContainText('(12)');
            // A new concept also needs a name: given where the level allows making one.
            if (atLeast(level, 'edit')) await fillStable(page.getByTestId('concept-name-input'), `e2e-concept-${S}`);
            return page;
        case 'home page': {
            await page.goto('/');
            const link = page.getByRole('link', { name: new RegExp(datasetName) });
            await expect(link).toBeVisible({ timeout: 20_000 });
            // The card holds the dataset's link and its Delete dataset button.
            return link.locator('..');
        }
    }
}

/** What the General tab says that isn't a control: the view-only footer, and the Access section for this level. */
async function checkGeneralTab(dialog: Locator, level: Level) {
    const footer = dialog.getByText('View only: changing these settings needs edit access to the dataset');
    if (atLeast(level, 'edit')) await expect.soft(footer, `the view-only footer is not shown at ${level}`).toHaveCount(0);
    else await expect.soft(footer, `the view-only footer is shown at ${level}`).toBeVisible();

    const access = dialog.getByRole('region', { name: 'Access' });
    const own = access.getByTestId('dataset-access-own');
    if (level === 'manage') {
        await expect.soft(access.getByRole('list', { name: 'Shared with' }), 'a manager sees who has access').toBeVisible();
        await expect.soft(access.getByRole('combobox', { name: 'User or group to share with' }), 'a manager has the picker').toBeVisible();
        await expect.soft(own, 'a manager is not told their own level instead').toHaveCount(0);
        return;
    }
    await expect.soft(own, `at ${level}: their own level`).toContainText(`You have ${capitalized(level)} access to this dataset.`);
    await expect.soft(own, `at ${level}: who can change access`).toContainText('A manager of this dataset or an admin can change who has access.');
    await expect.soft(access.getByRole('list', { name: 'Shared with' }), `at ${level}: no list of shares`).toHaveCount(0);
    await expect.soft(access.getByRole('combobox', { name: 'User or group to share with' }), `at ${level}: no picker`).toHaveCount(0);
    await expect.soft(access.getByRole('button', { name: 'Share', exact: true }), `at ${level}: no Share button`).toHaveCount(0);
}

/** The Versions tab: browsing and comparing are open to every level; the revert choices only where a revert is. */
async function checkVersionsTab(dialog: Locator, level: Level) {
    await expect.soft(dialog.getByRole('link', { name: 'Browse snapshot' }), `Browse snapshot at ${level}`).toBeVisible();
    await expect.soft(dialog.getByRole('button', { name: 'Diff vs Current' }), `Diff vs Current at ${level}`).toBeEnabled();
    const choice = dialog.getByRole('checkbox', { name: /^A revert puts the dataset back to exactly the version/ });
    if (atLeast(level, 'edit')) await expect.soft(choice, `the revert choices at ${level}`).toBeVisible();
    else await expect.soft(choice, `no revert choices at ${level}`).toHaveCount(0);
}

const WHO: Array<{ who: string; key?: string; level: Level }> = [
    { who: 'a user shared at view', key: 'view', level: 'view' },
    { who: 'a user shared at tag', key: 'tag', level: 'tag' },
    { who: 'a user shared at edit', key: 'edit', level: 'edit' },
    { who: 'a user shared at manage', key: 'manage', level: 'manage' },
    { who: 'a user with edit only through a group', key: 'member', level: 'edit' },
    { who: 'a guest shared at edit (a guest gets view at most)', key: 'guest', level: 'view' },
    { who: 'an admin (manage in every dataset)', level: 'manage' },
];

for (const { who, key, level } of WHO) {
    test(`${who}: each gated control is enabled at ${level} or disabled with the reason`, async ({ browser }) => {
        test.setTimeout(180_000);
        const { context, page } = await signIn(browser, key ? users[key] : undefined);
        try {
            const me = await (await page.request.get('/api/auth/me')).json();
            const user = { canShareViews: me.can_share_views !== false };
            await aFailedIndexBuildInTheHistory(page);
            // The level the server reports for this user is the one the table expects.
            expect((await (await page.request.get(`/api/datasets/${datasetId}`)).json()).access_level).toBe(level);

            const areas = [...new Set(CONTROLS.map(control => control.area))];
            for (const area of areas) {
                const scope = await openArea(page, area, level);
                for (const control of CONTROLS.filter(c => c.area === area)) await checkControl(scope, control, level, user);
                if (area === 'Settings › General') await checkGeneralTab(scope as Locator, level);
                if (area === 'Settings › Versions') await checkVersionsTab(scope as Locator, level);
            }
        } finally {
            await context.close();
        }
    });
}

test('at tag, a tag added in the sample view is saved', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.tag);
    const tag = `e2e_al_tagged_${S}`;
    const tagged = async () => (await apiFetch(cookie, `/datasets/${datasetId}/samples/query`, {
        method: 'POST',
        body: JSON.stringify({ filters: { field: 'tags', operator: 'in', value: [tag] }, limit: 20 }),
    })).items.length;
    try {
        await openDataset(page, datasetId);
        await page.getByTestId('grid-cell').first().click();
        await page.getByTestId('modal-tag').click();
        await expect(page.getByText('Tag — 1 Sample')).toBeVisible();
        await fillStable(page.getByPlaceholder('Enter tag name...'), tag);
        await page.getByRole('button', { name: 'Add', exact: true }).click();
        await page.getByRole('button', { name: 'Apply Changes' }).click();
        await expect(page.getByText('Tag — 1 Sample')).toHaveCount(0);
        await expect(page.getByRole('dialog').getByText(tag)).toBeVisible({ timeout: 10_000 });
        await expect.poll(tagged).toBe(1);
    } finally {
        await context.close();
    }
});

test('at edit, a description is saved, and the save sends only what edit may change', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.edit);
    const description = `Described at edit ${S}`;
    try {
        await openDataset(page, datasetId);
        let dialog = await openSettings(page);
        await fillStable(dialog.getByRole('textbox', { name: 'Description', exact: true }), description);
        const patch = page.waitForRequest(r => new URL(r.url()).pathname === `/api/datasets/${datasetId}` && r.method() === 'PATCH');
        await dialog.getByRole('button', { name: 'Save Changes' }).click();
        const sent = (await patch).postDataJSON();
        expect(sent.description).toBe(description);
        for (const manageOnly of ['name', 'is_global', 'everyone_level']) {
            expect(sent, `an editor's save sends no ${manageOnly}`).not.toHaveProperty(manageOnly);
        }
        await expect(dialog).toBeHidden();
        expect((await apiFetch(cookie, `/datasets/${datasetId}`)).description).toBe(description);
        dialog = await openSettings(page);
        await expect(dialog.getByRole('textbox', { name: 'Description', exact: true })).toHaveValue(description);
    } finally {
        await context.close();
    }
});

test('a save the server refuses shows the server\'s words, and the dialog catches up: the editor was lowered to view meanwhile', async ({ browser }) => {
    const editor = await createUser(cookie, `e2e_al_editor_${S}`);
    await share(cookie, datasetId, { user_id: editor.id }, 'edit');
    const { context, page } = await signIn(browser, editor);
    try {
        await openDataset(page, datasetId);
        const dialog = await openSettings(page);
        const before = (await apiFetch(cookie, `/datasets/${datasetId}`)).description;
        await fillStable(dialog.getByRole('textbox', { name: 'Description', exact: true }), `Refused ${S}`);
        await share(cookie, datasetId, { user_id: editor.id }, 'view');
        const saved = page.waitForResponse(r => new URL(r.url()).pathname === `/api/datasets/${datasetId}` && r.request().method() === 'PATCH');
        await dialog.getByRole('button', { name: 'Save Changes' }).click();
        expect((await saved).status()).toBe(403);
        await expect(dialog.getByRole('alert').filter({ hasText: REASON.edit })).toBeVisible();
        await expect(dialog).toBeVisible();
        // Without a reload the level is read again: the dialog follows it, and the server's words stay.
        const description = dialog.getByRole('textbox', { name: 'Description', exact: true });
        const save = dialog.getByRole('button', { name: 'Save Changes' });
        for (const control of [description, save]) {
            await expect(control).toBeDisabled();
            await expect(control).toHaveAttribute('title', REASON.edit);
        }
        await expect(dialog.getByRole('region', { name: 'Access' }).getByTestId('dataset-access-own'))
            .toContainText('You have View access to this dataset.');
        await expect(dialog.getByText('View only: changing these settings needs edit access to the dataset')).toBeVisible();
        await expect(dialog.getByRole('alert').filter({ hasText: REASON.edit })).toBeVisible();
        expect((await apiFetch(cookie, `/datasets/${datasetId}`)).description).toBe(before);
    } finally {
        await context.close();
        await apiFetch(cookie, `/users/${editor.id}`, { method: 'DELETE' });
    }
});

test('a tag the server refuses shows the server\'s words in the tag dialog, and the grid catches up: the tagger was lowered to view meanwhile', async ({ browser }) => {
    const tagger = await createUser(cookie, `e2e_al_tagger_${S}`);
    await share(cookie, datasetId, { user_id: tagger.id }, 'tag');
    const { context, page } = await signIn(browser, tagger);
    const tag = `e2e_al_refused_${S}`;
    try {
        await openDataset(page, datasetId);
        await page.getByTestId('grid-select-all').click();
        const gridTag = page.getByTestId('grid-tag-button');
        await gridTag.click();
        await expect(page.getByText(/Tag — 12 Samples/)).toBeVisible();
        await fillStable(page.getByPlaceholder('Enter tag name...'), tag);
        await page.getByRole('button', { name: 'Add', exact: true }).click();
        await share(cookie, datasetId, { user_id: tagger.id }, 'view');
        await page.getByRole('button', { name: 'Apply Changes' }).click();
        await expect(page.getByText(REASON.tag, { exact: true })).toBeVisible();
        await expect(page.getByText(/Power User/)).toHaveCount(0);  // the message before levels
        // Without a reload the level is read again, and the grid's Tag follows it.
        await expect(gridTag).toBeDisabled();
        await expect(gridTag).toHaveAttribute('title', REASON.tag);
        const tagged = await apiFetch(cookie, `/datasets/${datasetId}/samples/query`, {
            method: 'POST',
            body: JSON.stringify({ filters: { field: 'tags', operator: 'in', value: [tag] }, limit: 20 }),
        });
        expect(tagged.items).toHaveLength(0);
    } finally {
        await context.close();
        await apiFetch(cookie, `/users/${tagger.id}`, { method: 'DELETE' });
    }
});

test('a non-admin\'s add-schedule never opens on Index review: a note when there is no map to re-fit', async ({ browser }) => {
    test.setTimeout(150_000);
    const noMaps = await createSmallDataset(cookie, `e2e-al-no-maps-${S}`);
    let aMap = 0;
    try {
        aMap = await createFixtureDataset(cookie, `e2e-al-a-map-${S}`);
        for (const id of [noMaps, aMap]) await share(cookie, id, { user_id: users.edit.id }, 'edit');
        const { context, page } = await signIn(browser, users.edit);
        const addSchedule = async (id: number) => {
            await openDataset(page, id);
            await page.getByTestId('open-job-history').click();
            const dialog = page.getByTestId('job-history-dialog');
            await dialog.getByTestId('jobs-schedules-tab').click();
            const schedules = dialog.getByTestId('dataset-schedules');
            await schedules.getByTestId('add-schedule').click();
            return schedules;
        };
        try {
            let schedules = await addSchedule(noMaps);
            const note = schedules.getByTestId('schedule-nothing-to-add');
            await expect(note).toContainText('This dataset has no maps to re-fit.');
            await expect(schedules.getByTestId('schedule-form')).toHaveCount(0);
            await note.getByRole('button', { name: 'Close' }).click();
            await expect(note).toHaveCount(0);

            // This spec's fixture: its one map already has a re-fit schedule.
            schedules = await addSchedule(datasetId);
            await expect(schedules.getByTestId('schedule-nothing-to-add')).toContainText('Every map already has a schedule.');

            // A map without a schedule: the form opens on a map re-fit of it.
            schedules = await addSchedule(aMap);
            const form = schedules.getByTestId('schedule-form');
            await expect(form.getByRole('combobox', { name: 'Job' }).getByRole('option', { selected: true })).toHaveText('Map re-fit');
            await expect(form.getByTestId('schedule-target')).toHaveValue('e2e_clip');
        } finally {
            await context.close();
        }
    } finally {
        await allSteps([
            () => deleteDatasetAndWait(cookie, noMaps, 120_000),
            async () => { if (aMap) await deleteDatasetAndWait(cookie, aMap, 120_000); },
        ]);
    }
});

test('at manage, a share added in the Access section reaches the server', async ({ browser }) => {
    const spare = await createUser(cookie, `e2e_al_spare_${S}`);
    const { context, page } = await signIn(browser, users.manage);
    try {
        await openDataset(page, datasetId);
        const access = (await openSettings(page)).getByRole('region', { name: 'Access' });
        await access.getByRole('combobox', { name: 'User or group to share with' }).selectOption({ label: spare.username });
        await access.getByRole('combobox', { name: 'Level for the new share' }).selectOption('tag');
        await access.getByRole('button', { name: 'Share', exact: true }).click();
        await expect(access.getByRole('combobox', { name: `Level for ${spare.username}` })).toHaveValue('tag');
        const grant = (await accessList(cookie, datasetId)).grants.find(row => row.user_id === spare.id);
        expect(grant?.level).toBe('tag');
    } finally {
        await context.close();
        // Their share goes with them.
        await apiFetch(cookie, `/users/${spare.id}`, { method: 'DELETE' });
    }
});

test('a manager lowering a user from edit to view applies on that user\'s next page load', async ({ browser }) => {
    test.setTimeout(120_000);
    const subject = await createUser(cookie, `e2e_al_subject_${S}`);
    await share(cookie, datasetId, { user_id: subject.id }, 'edit');
    const manager = await signIn(browser, users.manage);
    const them = await signIn(browser, subject);
    const promote = them.page.getByTestId('panel-filters').getByTestId('field-section-idx')
        .getByRole('button', { name: 'Promote Field (Create Index)' });
    try {
        await openDataset(them.page, datasetId);
        await expect(promote).toBeEnabled();

        await openDataset(manager.page, datasetId);
        const access = (await openSettings(manager.page)).getByRole('region', { name: 'Access' });
        const level = access.getByRole('combobox', { name: `Level for ${subject.username}` });
        await expect(level).toHaveValue('edit');
        await level.selectOption('view');
        await expect(level).toHaveValue('view');
        await expect.poll(async () => (await accessList(cookie, datasetId)).grants.find(row => row.user_id === subject.id)?.level).toBe('view');

        await openDataset(them.page, datasetId);
        await expect(promote).toBeDisabled();
        await expect(promote).toHaveAttribute('title', REASON.edit);
        const dialog = await openSettings(them.page);
        const description = dialog.getByRole('textbox', { name: 'Description', exact: true });
        await expect(description).toBeDisabled();
        await expect(description).toHaveAttribute('title', REASON.edit);
        await expect(dialog.getByTestId('dataset-access-own')).toContainText('You have View access to this dataset.');
    } finally {
        await manager.context.close();
        await them.context.close();
        await apiFetch(cookie, `/users/${subject.id}`, { method: 'DELETE' });
    }
});

test('the dataset list shows a user only the datasets they have a level in', async ({ browser }) => {
    test.setTimeout(150_000);
    const lister = await createUser(cookie, `e2e_al_lister_${S}`);
    const made: number[] = [];
    let listGroup = 0;
    const name = (what: string) => `e2e-al-list-${what}-${S}`;
    try {
        made.push(await createSmallDataset(cookie, name('own')));
        await share(cookie, made[0], { user_id: lister.id }, 'view');
        made.push(await createSmallDataset(cookie, name('group')));
        const group = await createGroup(cookie, `e2e-al-listers-${S}`, [lister]);
        listGroup = group.id;
        await share(cookie, made[1], { group: group.name }, 'tag');
        made.push(await createSmallDataset(cookie, name('everyone')));
        await apiFetch(cookie, `/datasets/${made[2]}`, { method: 'PATCH', body: JSON.stringify({ is_global: true, everyone_level: 'view' }) });
        made.push(await createSmallDataset(cookie, name('hidden')));

        const { context, page } = await signIn(browser, lister);
        try {
            await page.goto('/');
            for (const what of ['own', 'group', 'everyone']) {
                await expect(page.getByRole('link', { name: new RegExp(name(what)) }), `${what}: listed`).toBeVisible({ timeout: 20_000 });
            }
            await expect(page.getByRole('link', { name: new RegExp(name('hidden')) }), 'not shared: not listed').toHaveCount(0);
            const listed = ((await (await page.request.get('/api/datasets')).json()) as Array<{ name: string; access_level: Level }>)
                .filter(row => row.name.endsWith(S));
            expect(Object.fromEntries(listed.map(row => [row.name, row.access_level]))).toEqual({
                [name('own')]: 'view', [name('group')]: 'tag', [name('everyone')]: 'view',
            });
        } finally {
            await context.close();
        }
    } finally {
        await allSteps([
            ...made.map(id => () => deleteDatasetAndWait(cookie, id, 120_000)),
            async () => { if (listGroup) await deleteGroup(cookie, listGroup); },
            () => apiFetch(cookie, `/users/${lister.id}`, { method: 'DELETE' }),
        ]);
    }
});
