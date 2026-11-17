import { test, expect, type Locator, type Page, type Request } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { fillStable } from './helpers';
import {
    REASON, type TestUser, allSteps, createMediaDataset, createUser, openDataset, openSettings, share, signIn, stamp,
} from './access-helpers';

// The image field a dataset's grids and sample view show. Nobody's pick of an image field in a grid or in the sample
// view changes the dataset's default any more, admins and managers included: the defaults are a deliberate setting in
// Dataset Settings → General beside Media Fields (Grid opens on, Sample view opens on), needing edit like the other
// display settings. A person's own pick is theirs, remembered per dataset in their browser, like the grid's sort. A
// remembered pick doesn't pin anyone to the default: picking the field that is the current default forgets the pick
// (for that select), so the user follows the default again. Both View selects mark it: `<field> (default)`.

const S = stamp();
let cookie = '';
let mainId = 0;  // media fields filepath and alt_path; defaults filepath
let otherId = 0;  // the same shape, to show a pick on the first doesn't reach it
const users: Record<'manage' | 'edit' | 'view', TestUser> = {} as Record<'manage' | 'edit' | 'view', TestUser>;

test.beforeAll(async () => {
    test.setTimeout(120_000);
    cookie = await adminLogin();
    mainId = await createMediaDataset(cookie, `e2e-dmf-main-${S}`);
    otherId = await createMediaDataset(cookie, `e2e-dmf-other-${S}`);
    for (const level of ['manage', 'edit', 'view'] as const) {
        users[level] = await createUser(cookie, `e2e_dmf_${level}_${S}`);
        for (const id of [mainId, otherId]) await share(cookie, id, { user_id: users[level].id }, level);
    }
});

test.afterAll(async () => {
    test.setTimeout(150_000);
    await allSteps([
        ...[mainId, otherId].map(id => async () => { if (id) await deleteDatasetAndWait(cookie, id, 120_000); }),
        ...Object.values(users).map(user => () => apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' })),
    ]);
});

const gridSelect = (page: Page) => page.getByTestId('panel-grid').getByRole('combobox', { name: 'Grid view media field' });
const gridSelectIn = (panel: Locator) => panel.getByRole('combobox', { name: 'Grid view media field' });
const sampleView = (page: Page) => page.getByRole('dialog').filter({ has: page.getByTestId('modal-tag') });
const sampleSelect = (page: Page) => sampleView(page).getByRole('combobox', { name: 'Sample view media field' });
const sampleImage = (page: Page) => sampleView(page).getByRole('img', { name: 'Sample' });
const HINT = 'The media field everyone sees first. Choosing another in a grid or a sample\'s view changes only your own view.';

/** The sample image's address names a field other than filepath; filepath's has none. */
const SRC: Record<string, RegExp> = {
    filepath: /^[^?]*(\?(?!(.*&)?field=).*)?$/,
    alt_path: /[?&]field=alt_path(&|$)/,
    third_path: /[?&]field=third_path(&|$)/,
};

/** The View select's option texts: the dataset's default marked. */
const marked = (fields: string[], current: string) => fields.map(field => (field === current ? `${field} (default)` : field));

/** A grid tile's image request (a thumbnail: it has a width) for `field`. */
const tileOf = (field: string) => (request: Request) => {
    const url = new URL(request.url());
    return /^\/api\/samples\/\d+\/media$/.test(url.pathname) && url.searchParams.has('w') && (url.searchParams.get('field') ?? 'filepath') === field;
};

/** Every request that would change the dataset (anything but a read of /api/datasets/{id}). */
function datasetWrites(page: Page, datasetId: number): string[] {
    const sent: string[] = [];
    page.on('request', request => {
        if (request.method() !== 'GET' && new URL(request.url()).pathname === `/api/datasets/${datasetId}`) {
            sent.push(`${request.method()} ${request.postData() ?? ''}`);
        }
    });
    return sent;
}

/** The stored defaults, [grid, sample view]. */
async function storedDefaults(datasetId: number): Promise<[string, string]> {
    const dataset = await apiFetch(cookie, `/datasets/${datasetId}`);
    return [dataset.grid_media_field, dataset.modal_media_field];
}

async function openSample(page: Page) {
    await page.getByTestId('grid-cell').first().click();
    await expect(page.getByTestId('modal-tag')).toBeVisible({ timeout: 10_000 });
}

async function closeSample(page: Page) {
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('modal-tag')).toHaveCount(0);
}

/** Open (or reload) a dataset and check its grid opens on `field`: the select shows it and the tiles are asked for it. */
async function opensOn(page: Page, datasetId: number, field: string, how: 'goto' | 'reload' = 'goto') {
    const tiles = page.waitForRequest(tileOf(field));
    if (how === 'reload') await page.reload();
    else await page.goto(`/datasets/${datasetId}`);
    await expect(page.getByTestId('grid-select-all')).toBeVisible({ timeout: 30_000 });
    await tiles;
    await expect(gridSelect(page)).toHaveValue(field);
}

/** Open a sample and check the sample view is on `field`. */
async function sampleOpensOn(page: Page, field: string) {
    await openSample(page);
    await expect(sampleSelect(page)).toHaveValue(field);
    await expect(sampleImage(page)).toHaveAttribute('src', SRC[field]);
}

for (const [who, key] of [['an admin', undefined], ['a manager', 'manage'], ['an editor', 'edit'], ['a viewer', 'view']] as const) {
    test(`${who}'s pick in the grid and in the sample view changes only what they see, and sends nothing to the dataset`, async ({ browser }) => {
        const { context, page } = await signIn(browser, key ? users[key] : undefined);
        const writes = datasetWrites(page, mainId);
        try {
            await opensOn(page, mainId, 'filepath');
            const grid = gridSelect(page);
            await expect(grid).toBeEnabled();
            await expect(grid).not.toHaveAttribute('title');
            await expect(grid.getByRole('option')).toHaveText(['filepath (default)', 'alt_path']);
            await expect(grid).toHaveValue('filepath');  // the value is the plain field name
            const altTiles = page.waitForRequest(tileOf('alt_path'));
            await grid.selectOption('alt_path');
            await altTiles;

            // The sample view is picked separately: it still opens on its own default.
            await sampleOpensOn(page, 'filepath');
            const sample = sampleSelect(page);
            await expect(sample).toBeEnabled();
            await expect(sample).not.toHaveAttribute('title');
            await expect(sample.getByRole('option')).toHaveText(['filepath (default)', 'alt_path']);
            await sample.selectOption('alt_path');
            await expect(sampleImage(page)).toHaveAttribute('src', SRC.alt_path);
            // ArrowUp and ArrowDown, with the focus outside the select, step through the fields: picks too.
            await sampleView(page).getByRole('heading').first().click();
            await page.keyboard.press('ArrowUp');
            await expect(sample).toHaveValue('filepath');
            await expect(sampleImage(page)).toHaveAttribute('src', SRC.filepath);
            await page.keyboard.press('ArrowDown');
            await expect(sample).toHaveValue('alt_path');
            await expect(sampleImage(page)).toHaveAttribute('src', SRC.alt_path);
            await closeSample(page);
            await expect(grid).toHaveValue('alt_path');  // the sample view's picks didn't touch the grid

            expect(page.url(), 'the address carries no media field').not.toContain('alt_path');
            expect(writes, 'nothing sent to the dataset').toEqual([]);
            expect(await storedDefaults(mainId)).toEqual(['filepath', 'filepath']);
        } finally {
            await context.close();
        }
    });
}

test('a pick is remembered in this browser per dataset, the grid\'s and the sample view\'s apart; another dataset and another browser keep the defaults', async ({ browser }) => {
    test.setTimeout(120_000);
    const { context, page } = await signIn(browser, users.edit);
    try {
        await opensOn(page, mainId, 'filepath');
        await gridSelect(page).selectOption('alt_path');
        await opensOn(page, mainId, 'alt_path', 'reload');
        // Leaving the dataset and coming back is the same.
        await page.goto('/');
        await opensOn(page, mainId, 'alt_path');

        // The sample view: its default until picked there, then its own pick after a reload.
        await sampleOpensOn(page, 'filepath');
        await sampleSelect(page).selectOption('alt_path');
        await closeSample(page);  // the address carries an open sample, which would open again over the grid
        await opensOn(page, mainId, 'alt_path', 'reload');
        await sampleOpensOn(page, 'alt_path');
        await closeSample(page);

        // Another dataset is unaffected.
        await opensOn(page, otherId, 'filepath');
        await sampleOpensOn(page, 'filepath');
        await closeSample(page);

        // Another browser, another user: the dataset's defaults.
        const viewer = await signIn(browser, users.view);
        try {
            await opensOn(viewer.page, mainId, 'filepath');
            await sampleOpensOn(viewer.page, 'filepath');
        } finally {
            await viewer.context.close();
        }

        // Picking the default after alt_path forgets the pick: the reload opens on the default.
        await opensOn(page, mainId, 'alt_path');
        await gridSelect(page).selectOption('filepath');
        await opensOn(page, mainId, 'filepath', 'reload');
        expect(await storedDefaults(mainId)).toEqual(['filepath', 'filepath']);
    } finally {
        await context.close();
    }
});

test('a pick of the current default is forgotten, so the user follows a default an editor changes later; a pick of another field is kept; the mark moves', async ({ browser }) => {
    test.setTimeout(180_000);
    const fields = ['filepath', 'alt_path', 'third_path'];
    const datasetId = await createMediaDataset(cookie, `e2e-dmf-defaults-${S}`, fields);  // defaults: filepath
    try {
        for (const level of ['manage', 'edit', 'view'] as const) await share(cookie, datasetId, { user_id: users[level].id }, level);
        // Each in a browser of their own (a pick is the browser's memory).
        const keeper = await signIn(browser, users.manage);
        const forgetter = await signIn(browser, users.view);
        const follower = await signIn(browser);
        const editor = await signIn(browser, users.edit);
        try {
            // The forgetter picks alt_path in the grid, then the default again: forgotten. Their sample view keeps
            // third_path (the two selects forget apart).
            await opensOn(forgetter.page, datasetId, 'filepath');
            await expect(gridSelect(forgetter.page).getByRole('option')).toHaveText(marked(fields, 'filepath'));
            await gridSelect(forgetter.page).selectOption('alt_path');
            await gridSelect(forgetter.page).selectOption('filepath');
            await sampleOpensOn(forgetter.page, 'filepath');
            await expect(sampleSelect(forgetter.page).getByRole('option')).toHaveText(marked(fields, 'filepath'));
            await sampleSelect(forgetter.page).selectOption('third_path');
            await closeSample(forgetter.page);

            // The keeper's grid: alt_path, then third_path (the last pick wins). Their sample view: third_path, then the
            // arrow keys up onto the default: forgotten.
            await opensOn(keeper.page, datasetId, 'filepath');
            await gridSelect(keeper.page).selectOption('alt_path');
            await gridSelect(keeper.page).selectOption('third_path');
            await sampleOpensOn(keeper.page, 'filepath');
            await sampleSelect(keeper.page).selectOption('third_path');
            await sampleView(keeper.page).getByRole('heading').first().click();
            await keeper.page.keyboard.press('ArrowUp');
            await expect(sampleSelect(keeper.page)).toHaveValue('alt_path');
            await keeper.page.keyboard.press('ArrowUp');
            await expect(sampleSelect(keeper.page)).toHaveValue('filepath');
            await expect(sampleImage(keeper.page)).toHaveAttribute('src', SRC.filepath);
            await closeSample(keeper.page);
            await opensOn(follower.page, datasetId, 'filepath');

            // An editor, who hasn't picked, sets both defaults to alt_path: their own page follows at once, marks too.
            await opensOn(editor.page, datasetId, 'filepath');
            const dialog = await openSettings(editor.page);
            await expect(dialog.getByRole('combobox', { name: 'Grid opens on' }).getByRole('option')).toHaveText(fields);  // not marked
            await dialog.getByRole('combobox', { name: 'Grid opens on' }).selectOption('alt_path');
            await dialog.getByRole('combobox', { name: 'Sample view opens on' }).selectOption('alt_path');
            const altTiles = editor.page.waitForRequest(tileOf('alt_path'));
            await dialog.getByRole('button', { name: 'Save Changes' }).click();
            await expect(dialog).toBeHidden();
            await altTiles;
            await expect(gridSelect(editor.page)).toHaveValue('alt_path');
            await expect(gridSelect(editor.page).getByRole('option')).toHaveText(marked(fields, 'alt_path'));
            await sampleOpensOn(editor.page, 'alt_path');
            await expect(sampleSelect(editor.page).getByRole('option')).toHaveText(marked(fields, 'alt_path'));
            await closeSample(editor.page);
            expect(await storedDefaults(datasetId)).toEqual(['alt_path', 'alt_path']);

            // On their next load: the follower and the forgetter's grid follow the new default (before this rule the
            // forgetter stayed on filepath); the keeper keeps third_path in the grid, and sees which is the default.
            await opensOn(follower.page, datasetId, 'alt_path', 'reload');
            await sampleOpensOn(follower.page, 'alt_path');
            await closeSample(follower.page);
            await opensOn(forgetter.page, datasetId, 'alt_path', 'reload');
            await expect(gridSelect(forgetter.page).getByRole('option')).toHaveText(marked(fields, 'alt_path'));
            await sampleOpensOn(forgetter.page, 'third_path');
            await expect(sampleSelect(forgetter.page).getByRole('option')).toHaveText(marked(fields, 'alt_path'));
            await closeSample(forgetter.page);
            await opensOn(keeper.page, datasetId, 'third_path', 'reload');
            await expect(gridSelect(keeper.page).getByRole('option')).toHaveText(marked(fields, 'alt_path'));
            await sampleOpensOn(keeper.page, 'alt_path');
        } finally {
            await allSteps([keeper, forgetter, follower, editor].map(session => () => session.context.close()));
        }
    } finally {
        await deleteDatasetAndWait(cookie, datasetId, 120_000);
    }
});

test('a remembered field that is no longer a media field gives way to the default, and applies again once added back', async ({ browser }) => {
    test.setTimeout(120_000);
    const datasetId = await createMediaDataset(cookie, `e2e-dmf-gone-${S}`);
    try {
        await share(cookie, datasetId, { user_id: users.edit.id }, 'edit');
        const { context, page } = await signIn(browser, users.edit);
        try {
            await opensOn(page, datasetId, 'filepath');
            await gridSelect(page).selectOption('alt_path');
            await sampleOpensOn(page, 'filepath');
            await sampleSelect(page).selectOption('alt_path');
            await closeSample(page);

            // alt_path stops being a media field (the defaults are filepath): the pick is ignored, and with one media
            // field the selects aren't shown.
            await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ media_fields: ['filepath'] }) });
            const tiles = page.waitForRequest(tileOf('filepath'));
            await page.reload();
            await expect(page.getByTestId('grid-select-all')).toBeVisible({ timeout: 30_000 });
            await tiles;
            await expect(page.getByRole('combobox', { name: 'Grid view media field' })).toHaveCount(0);
            await openSample(page);
            await expect(sampleImage(page)).toHaveAttribute('src', SRC.filepath);
            await expect(page.getByRole('combobox', { name: 'Sample view media field' })).toHaveCount(0);
            await closeSample(page);

            // Back in media_fields: the remembered pick applies again.
            await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify({ media_fields: ['filepath', 'alt_path'] }) });
            await opensOn(page, datasetId, 'alt_path', 'reload');
            await sampleOpensOn(page, 'alt_path');
        } finally {
            await context.close();
        }
    } finally {
        await deleteDatasetAndWait(cookie, datasetId, 120_000);
    }
});

test('several grids: each keeps its own pick while open, and a grid that appears later opens on the last pick in any grid', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.edit);
    try {
        await opensOn(page, otherId, 'filepath');
        const grids = page.getByTestId('panel-grid');
        await expect(grids).toHaveCount(1);
        await gridSelectIn(grids.nth(0)).selectOption('alt_path');
        // Another panel switched to Image Grid.
        await page.getByTestId('panel-type-select').last().selectOption('grid');
        await expect(grids).toHaveCount(2);
        await expect(gridSelectIn(grids.nth(1))).toHaveValue('alt_path');
        await gridSelectIn(grids.nth(1)).selectOption('filepath');
        await expect(gridSelectIn(grids.nth(0))).toHaveValue('alt_path');
    } finally {
        await context.close();
    }
});

test('below edit the two default selects are disabled and say why', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.view);
    try {
        await openDataset(page, mainId);
        const dialog = await openSettings(page);
        for (const name of ['Grid opens on', 'Sample view opens on']) {
            const select = dialog.getByRole('combobox', { name });
            await expect(select, name).toBeDisabled();
            await expect(select).toHaveAttribute('title', REASON.edit);
            await expect(select).toHaveAccessibleDescription(REASON.edit);
            await expect(select).toHaveValue('filepath');
        }
        await expect(dialog.getByText(HINT, { exact: true })).toBeVisible();
    } finally {
        await context.close();
    }
});

test('an editor sets the defaults with Save Changes, which other users then open on; Cancel sends nothing', async ({ browser }) => {
    test.setTimeout(150_000);
    const datasetId = await createMediaDataset(cookie, `e2e-dmf-settings-${S}`);
    try {
        for (const level of ['edit', 'view'] as const) await share(cookie, datasetId, { user_id: users[level].id }, level);
        const { context, page } = await signIn(browser, users.edit);
        const writes = datasetWrites(page, datasetId);
        try {
            await opensOn(page, datasetId, 'filepath');
            let dialog = await openSettings(page);
            const grid = () => dialog.getByRole('combobox', { name: 'Grid opens on' });
            const sample = () => dialog.getByRole('combobox', { name: 'Sample view opens on' });
            await expect(dialog.getByText(HINT, { exact: true })).toBeVisible();
            for (const select of [grid(), sample()]) {
                await expect(select).toBeEnabled();
                await expect(select).not.toHaveAttribute('title');
                await expect(select).toHaveValue('filepath');
                await expect(select.getByRole('option')).toHaveText(['filepath', 'alt_path']);
            }

            // A change, then Cancel: nothing is sent.
            await grid().selectOption('alt_path');
            await dialog.getByRole('button', { name: 'Cancel' }).click();
            await expect(dialog).toBeHidden();
            expect(writes).toEqual([]);
            expect(await storedDefaults(datasetId)).toEqual(['filepath', 'filepath']);

            dialog = await openSettings(page);
            await expect(grid()).toHaveValue('filepath');
            await grid().selectOption('alt_path');
            await sample().selectOption('alt_path');
            expect(writes, 'changing the selects sends nothing by itself').toEqual([]);
            const patch = page.waitForRequest(r => new URL(r.url()).pathname === `/api/datasets/${datasetId}` && r.method() === 'PATCH');
            await dialog.getByRole('button', { name: 'Save Changes' }).click();
            expect((await patch).postDataJSON()).toMatchObject({ grid_media_field: 'alt_path', modal_media_field: 'alt_path' });
            await expect(dialog).toBeHidden();
            expect(await storedDefaults(datasetId)).toEqual(['alt_path', 'alt_path']);

            // Another user, who hasn't picked, opens on them.
            const viewer = await signIn(browser, users.view);
            try {
                await opensOn(viewer.page, datasetId, 'alt_path');
                await sampleOpensOn(viewer.page, 'alt_path');
            } finally {
                await viewer.context.close();
            }
        } finally {
            await context.close();
        }
    } finally {
        await deleteDatasetAndWait(cookie, datasetId, 120_000);
    }
});

test('removing a media field that is a default: its select shows the first field left at once; Save stores it, Cancel keeps everything', async ({ browser }) => {
    test.setTimeout(150_000);
    const datasetId = await createMediaDataset(cookie, `e2e-dmf-remove-${S}`, ['filepath', 'alt_path', 'third_path']);
    await apiFetch(cookie, `/datasets/${datasetId}`, {
        method: 'PATCH',
        body: JSON.stringify({ grid_media_field: 'alt_path', modal_media_field: 'third_path' }),
    });
    const { context, page } = await signIn(browser);
    const writes = datasetWrites(page, datasetId);
    try {
        await openDataset(page, datasetId);
        let dialog = await openSettings(page);
        const grid = () => dialog.getByRole('combobox', { name: 'Grid opens on' });
        const sample = () => dialog.getByRole('combobox', { name: 'Sample view opens on' });
        const removeChip = () => dialog.getByRole('button', { name: '×', exact: true });
        await expect(grid()).toHaveValue('alt_path');
        await expect(sample()).toHaveValue('third_path');
        await expect(removeChip()).toHaveCount(3);

        // alt_path's chip is the second (the media fields in order).
        await removeChip().nth(1).click();
        await expect(removeChip()).toHaveCount(2);
        for (const select of [grid(), sample()]) await expect(select.getByRole('option')).toHaveText(['filepath', 'third_path']);
        await expect(grid()).toHaveValue('filepath');  // was alt_path: the first field left
        await expect(sample()).toHaveValue('third_path');  // another field: kept

        await dialog.getByRole('button', { name: 'Cancel' }).click();
        await expect(dialog).toBeHidden();
        expect(writes).toEqual([]);
        const unchanged = await apiFetch(cookie, `/datasets/${datasetId}`);
        expect([unchanged.media_fields, unchanged.grid_media_field, unchanged.modal_media_field])
            .toEqual([['filepath', 'alt_path', 'third_path'], 'alt_path', 'third_path']);

        dialog = await openSettings(page);
        await expect(removeChip()).toHaveCount(3);
        await removeChip().nth(1).click();
        await expect(grid()).toHaveValue('filepath');
        const patch = page.waitForRequest(r => new URL(r.url()).pathname === `/api/datasets/${datasetId}` && r.method() === 'PATCH');
        await dialog.getByRole('button', { name: 'Save Changes' }).click();
        expect((await patch).postDataJSON()).toMatchObject({
            media_fields: ['filepath', 'third_path'], grid_media_field: 'filepath', modal_media_field: 'third_path',
        });
        await expect(dialog).toBeHidden();
        const saved = await apiFetch(cookie, `/datasets/${datasetId}`);
        expect([saved.media_fields, saved.grid_media_field, saved.modal_media_field]).toEqual([['filepath', 'third_path'], 'filepath', 'third_path']);
    } finally {
        await context.close();
        await deleteDatasetAndWait(cookie, datasetId, 120_000);
    }
});

test('removing every media field empties both default selects, and Save shows the server\'s refusal', async ({ browser }) => {
    const { context, page } = await signIn(browser);
    try {
        await openDataset(page, otherId);
        const dialog = await openSettings(page);
        const removeChip = dialog.getByRole('button', { name: '×', exact: true });
        await expect(removeChip).toHaveCount(2);
        await removeChip.first().click();
        await removeChip.first().click();
        await expect(removeChip).toHaveCount(0);
        for (const name of ['Grid opens on', 'Sample view opens on']) {
            await expect(dialog.getByRole('combobox', { name }).getByRole('option'), `${name}: no options`).toHaveCount(0);
        }
        const patch = page.waitForRequest(r => new URL(r.url()).pathname === `/api/datasets/${otherId}` && r.method() === 'PATCH');
        await dialog.getByRole('button', { name: 'Save Changes' }).click();
        const sent = (await patch).postDataJSON();
        expect(sent).not.toHaveProperty('grid_media_field');
        expect(sent).not.toHaveProperty('modal_media_field');
        await expect(dialog.getByRole('alert').filter({ hasText: 'media_fields must list at least one field' })).toBeVisible();
        const unchanged = await apiFetch(cookie, `/datasets/${otherId}`);
        expect([unchanged.media_fields, unchanged.grid_media_field, unchanged.modal_media_field]).toEqual([['filepath', 'alt_path'], 'filepath', 'filepath']);
    } finally {
        await context.close();
    }
});

test('without browser storage the page still opens and a pick applies for the visit', async ({ browser }) => {
    const { context, page } = await signIn(browser, users.edit);
    // Storage that refuses: every localStorage method throws (sessionStorage still works).
    await context.addInitScript(() => {
        for (const name of ['getItem', 'setItem', 'removeItem', 'clear', 'key'] as const) {
            const real = Storage.prototype[name] as (...args: unknown[]) => unknown;
            Object.defineProperty(Storage.prototype, name, {
                configurable: true,
                writable: true,
                value: function (this: Storage, ...args: unknown[]) {
                    if (this === window.localStorage) throw new DOMException('The operation is insecure.', 'SecurityError');
                    return real.apply(this, args);
                },
            });
        }
    });
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    try {
        await opensOn(page, mainId, 'filepath');
        expect(await page.evaluate(() => { try { window.localStorage.setItem('e2e', '1'); return 'stored'; } catch { return 'refused'; } }))
            .toBe('refused');
        const altTiles = page.waitForRequest(tileOf('alt_path'));
        await gridSelect(page).selectOption('alt_path');
        await altTiles;
        await sampleOpensOn(page, 'filepath');
        await sampleSelect(page).selectOption('alt_path');
        await expect(sampleImage(page)).toHaveAttribute('src', SRC.alt_path);
        await closeSample(page);
        // Nothing was remembered.
        await opensOn(page, mainId, 'filepath', 'reload');
        expect(errors, 'no page errors').toEqual([]);
    } finally {
        await context.close();
    }
});

test('with window.localStorage itself refused, the login page, the home page and the dataset page still work', async ({ browser }) => {
    const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    // Blocked site data, as browsers do it: reading the property throws.
    await context.addInitScript(() => {
        Object.defineProperty(window, 'localStorage', {
            configurable: true,
            get() { throw new DOMException('The operation is insecure.', 'SecurityError'); },
        });
    });
    const page = await context.newPage();
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    try {
        await page.goto('/login');
        await fillStable(page.getByLabel('Username'), users.edit.username);
        await fillStable(page.getByLabel('Password'), users.edit.password);
        await page.getByRole('button', { name: 'Sign in' }).click();
        await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
        await page.goto('/');
        await expect(page.getByRole('link', { name: new RegExp(`e2e-dmf-main-${S}`) })).toBeVisible({ timeout: 20_000 });
        await opensOn(page, mainId, 'filepath');
        const altTiles = page.waitForRequest(tileOf('alt_path'));
        await gridSelect(page).selectOption('alt_path');
        await altTiles;
        await opensOn(page, mainId, 'filepath', 'reload');  // nothing remembered
        expect(errors, 'no page errors').toEqual([]);
    } finally {
        await context.close();
    }
});
