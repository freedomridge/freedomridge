import { test, expect, type Locator, type Page } from '@playwright/test';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE, expectTotal, fillStable } from './helpers';
import { REASON, type Level, type TestUser, allSteps, createUser, openDataset, share, signIn, stamp } from './access-helpers';

// Field descriptions (docs/DATASET_MANAGEMENT.md, "Describe a field"). Each field can have a description (what it
// means) and a unit. Everyone who can open the dataset reads them: on a field's name in the filter sidebar (hover and
// keyboard focus), and in Dataset Settings → Fields, which lists every field the sidebar lists. Edit and up change them
// there; below edit the controls are disabled and say why. Built-in fields come with a default, marked as one until
// someone writes their own; clearing a written one brings the default back. A field of the user's own named as one the
// SDK computes (brightness, width) has no description or unit until someone writes one, and none again once cleared
// (m1-contract-computed-field-descriptions). Saving a description changes nothing else about the field.
test.use({ storageState: AUTH_STATE });

type Field = { name: string; type: string; description: string | null; unit: string | null; description_source: 'set' | 'default' | null };

const S = stamp();
let cookie = '';
let datasetId = 0;
let otherId = 0;  // another dataset with a field of the same name
const users: Partial<Record<Level | 'guest', TestUser>> = {};
const DEFAULT_LINE = 'Default description: nobody has written one for this field yet.';
const VIEW_ONLY = 'View only: changing these settings needs edit access to the dataset';

async function fixture(name: string): Promise<number> {
    const { id } = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name }) });
    await apiFetch(cookie, `/datasets/${id}/array-fields`, { method: 'POST', body: JSON.stringify({ field_name: 'keywords' }) });
    const samples = [0, 1, 2, 3].map(i => ({
        filepath: `s3://e2e-field-descriptions/${name}/${i}.jpg`,
        tags: ['seed'],
        array_fields: { keywords: [`k${i % 2}`] },
        metadata: {
            crop: i % 2 ? 'corn' : 'soy',
            digital_gain: i * 1.5,
            brightness: i * 10,  // named as an SDK metric, but the user's own: no description by its name
            width: 640 + i,  // likewise, and no unit by its name
            detections: [{ label: i % 2 ? 'weed' : 'rock', bbox: [0.1, 0.1, 0.2, 0.2], confidence: 0.9 }],
        },
    }));
    await apiFetch(cookie, `/datasets/${id}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    await apiFetch(cookie, `/datasets/${id}/refresh-stats`, { method: 'POST' });
    return id;
}

/** The fields as the app's schema call lists them (what the sidebar and the Fields tab show). */
async function schema(id = datasetId): Promise<Field[]> {
    return apiFetch(cookie, `/datasets/${id}/field-stats`, { method: 'POST', body: JSON.stringify({}) });
}
const fieldOf = async (name: string, id = datasetId) => (await schema(id)).find(field => field.name === name)!;

test.beforeAll(async () => {
    test.setTimeout(180_000);
    cookie = await adminLogin();
    datasetId = await fixture(`e2e-field-descriptions-${S}`);
    otherId = await fixture(`e2e-field-descriptions-other-${S}`);
    for (const level of ['view', 'tag', 'edit', 'manage'] as const) {
        users[level] = await createUser(cookie, `e2e_fd_${level}_${S}`);
        await share(cookie, datasetId, { user_id: users[level]!.id }, level);
    }
    users.guest = await createUser(cookie, `e2e_fd_guest_${S}`, 'guest');
    await share(cookie, datasetId, { user_id: users.guest.id }, 'edit');  // a guest gets view at most
    // The field-stats cache is filled by refresh-stats' job: wait until the schema lists the fixture's fields.
    await expect.poll(async () => (await schema()).map(field => field.name), { timeout: 60_000 }).toContain('digital_gain');
});

test.afterAll(async () => {
    test.setTimeout(180_000);
    await allSteps([
        ...[datasetId, otherId].map(id => async () => { if (id) await deleteDatasetAndWait(cookie, id, 120_000); }),
        ...Object.values(users).map(user => () => apiFetch(cookie, `/users/${user!.id}`, { method: 'DELETE' })),
    ]);
});

async function describeByApi(field: string, body: Record<string, unknown>, id = datasetId) {
    return apiFetch(cookie, `/datasets/${id}/fields/${encodeURIComponent(field)}/description`, { method: 'PATCH', body: JSON.stringify(body) });
}

async function openFields(page: Page, id = datasetId): Promise<Locator> {
    await openDataset(page, id);
    await page.getByRole('button', { name: 'Dataset Settings', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Dataset Settings' });
    await dialog.getByRole('button', { name: 'Fields', exact: true }).click();
    await expect(dialog.getByRole('textbox', { name: 'Description of id', exact: true })).toBeVisible();
    return dialog;
}

const descriptionBox = (scope: Locator, field: string) => scope.getByRole('textbox', { name: `Description of ${field}`, exact: true });
const unitBox = (scope: Locator, field: string) => scope.getByRole('textbox', { name: `Unit of ${field}`, exact: true });
const saveButton = (scope: Locator, field: string) => scope.getByRole('button', { name: `Save description and unit of ${field}`, exact: true });

/** Every description PATCH the page sends, by body. */
function descriptionPatches(page: Page): Array<Record<string, unknown>> {
    const sent: Array<Record<string, unknown>> = [];
    page.on('request', request => {
        if (request.method() === 'PATCH' && /\/fields\/[^/]+\/description$/.test(new URL(request.url()).pathname)) sent.push(request.postDataJSON());
    });
    return sent;
}

async function save(page: Page, dialog: Locator, field: string) {
    const answered = page.waitForResponse(r => r.request().method() === 'PATCH' && new URL(r.url()).pathname.endsWith(`/fields/${encodeURIComponent(field)}/description`));
    await saveButton(dialog, field).click();
    return answered;
}

test('Fields lists every field the sidebar\'s data lists, in its order, each with a description, a unit, a Save and the one box for values_private', async ({ page }) => {
    const fields = await schema();
    const names = fields.map(field => field.name);
    // Built-in, array, label, metric and metadata fields alike.
    for (const name of ['id', 'filepath', 'created_at', 'last_modified_at', 'media_type', 'tags', 'keywords', 'detections.label', 'brightness', 'width', 'crop', 'digital_gain']) {
        expect(names).toContain(name);
    }
    const dialog = await openFields(page);
    // The tab's intro starts with this; the assistant's settings add a sentence after it (assistant-settings.fits.spec.ts).
    await expect(dialog.getByText(/^Say what each field means and its unit: the filters show it on the field's name\./)).toBeVisible();
    await expect(dialog.getByText('No metadata fields to edit.')).toHaveCount(0);
    const boxes = dialog.getByRole('textbox', { name: /^Description of / });
    await expect(boxes).toHaveCount(names.length);
    for (const [index, name] of names.entries()) {
        await expect(boxes.nth(index)).toHaveAccessibleName(`Description of ${name}`);
        await expect(unitBox(dialog, name)).toHaveCount(1);
        await expect(saveButton(dialog, name)).toHaveText('Save description');
    }
    await expect(descriptionBox(dialog, 'id')).toHaveAttribute('placeholder', 'What this field means');
    await expect(unitBox(dialog, 'id')).toHaveAttribute('placeholder', 'Unit');
    // Only metadata fields keep the type controls.
    for (const name of ['crop', 'digital_gain', 'brightness', 'width']) await expect(dialog.getByRole('combobox', { name: `Declared type for ${name}` })).toHaveCount(1);
    for (const name of ['id', 'tags', 'keywords', 'detections.label']) await expect(dialog.getByRole('combobox', { name: `Declared type for ${name}` })).toHaveCount(0);
    // values_private has one control, each field's "Keep this field's values from the assistant" (the assistant's
    // settings), and no other: every checkbox in the tab is one of those, one per field, and nothing says "private".
    const keep = 'Keep this field\'s values from the assistant';
    await expect(dialog.getByRole('checkbox', { name: keep, exact: true })).toHaveCount(names.length);
    await expect(dialog.getByRole('checkbox')).toHaveCount(names.length);
    for (const name of names) {
        await expect(dialog.getByRole('group', { name, exact: true }).getByRole('checkbox', { name: keep, exact: true }), name).toHaveCount(1);
    }
    await expect(dialog.getByRole('switch')).toHaveCount(0);
    await expect(dialog.getByText(/private/i)).toHaveCount(0);
    // The sidebar's fields are among them.
    await page.keyboard.press('Escape');
    for (const name of ['tags', 'crop', 'digital_gain', 'keywords']) {
        await expect(page.getByTestId(`field-toggle-${name}`), `${name} in the sidebar`).toHaveCount(1);
        expect(names, `${name} in Fields`).toContain(name);
    }
});

test('Save waits for a change and sends only what changed; the box then shows what was saved', async ({ page }) => {
    const sent = descriptionPatches(page);
    const dialog = await openFields(page);
    const box = descriptionBox(dialog, 'digital_gain');
    const unit = unitBox(dialog, 'digital_gain');
    const button = saveButton(dialog, 'digital_gain');
    await expect(box).toHaveValue('');
    await expect(button).toBeDisabled();
    await fillStable(box, 'something');
    await expect(button).toBeEnabled();
    await fillStable(box, '');
    await expect(button, 'back to what it was showing').toBeDisabled();

    // The description alone: trimmed by the server, a line break kept; "Saving…" while it runs.
    await page.route('**/fields/digital_gain/description', async route => {
        await new Promise(resolve => setTimeout(resolve, 600));
        await route.continue();
    });
    await fillStable(box, '  Sensor gain applied before readout;\nhigher in dark scenes.  ');
    const saving = save(page, dialog, 'digital_gain');
    await expect(button).toHaveText('Saving…');
    expect((await saving).status()).toBe(200);
    await page.unroute('**/fields/digital_gain/description');
    expect(sent).toEqual([{ description: '  Sensor gain applied before readout;\nhigher in dark scenes.  ' }]);
    await expect(box).toHaveValue('Sensor gain applied before readout;\nhigher in dark scenes.');
    await expect(button).toHaveText('Save description');
    await expect(button).toBeDisabled();
    await expect(dialog).toBeVisible();  // it stays open

    // Then the unit alone.
    await fillStable(unit, 'dB');
    expect((await save(page, dialog, 'digital_gain')).status()).toBe(200);
    expect(sent.at(-1)).toEqual({ unit: 'dB' });
    await expect(button).toBeDisabled();
    const stored = await fieldOf('digital_gain');
    expect([stored.description, stored.unit, stored.description_source]).toEqual(['Sensor gain applied before readout;\nhigher in dark scenes.', 'dB', 'set']);
});

test('a default is shown as a default, to sight and to assistive reading; writing over it makes it the field\'s own, clearing brings it back', async ({ page }) => {
    const sent = descriptionPatches(page);
    const original = await fieldOf('media_type');
    expect([original.description_source, original.unit]).toEqual(['default', null]);
    const dialog = await openFields(page);
    const box = descriptionBox(dialog, 'media_type');
    await expect(box).toHaveValue(original.description!);
    await expect(box).toHaveCSS('font-style', 'italic');
    await expect(box).toHaveAccessibleDescription(DEFAULT_LINE);
    await expect(unitBox(dialog, 'media_type')).toHaveValue('');
    // A field someone described has no such line, nor one with no description at all.
    await expect(descriptionBox(dialog, 'crop')).toHaveAccessibleDescription('');

    await fillStable(box, 'What kind of file the sample is.');
    expect((await save(page, dialog, 'media_type')).status()).toBe(200);
    expect(sent.at(-1)).toEqual({ description: 'What kind of file the sample is.' });  // the unit untouched
    await expect(box).toHaveValue('What kind of file the sample is.');
    await expect(box).toHaveAccessibleDescription('');
    await expect(box).not.toHaveCSS('font-style', 'italic');
    let stored = await fieldOf('media_type');
    expect([stored.description_source, stored.unit]).toEqual(['set', null]);

    // Cleared: the default is back, with its line.
    await fillStable(box, '');
    expect((await save(page, dialog, 'media_type')).status()).toBe(200);
    expect(Object.keys(sent.at(-1)!)).toEqual(['description']);
    expect(['', null]).toContain(sent.at(-1)!.description);
    await expect(box).toHaveValue(original.description!);
    await expect(box).toHaveAccessibleDescription(DEFAULT_LINE);
    stored = await fieldOf('media_type');
    expect([stored.description, stored.description_source]).toEqual([original.description, 'default']);

    // A unit is saved the same way; no field has one by its name, so cleared, it is none.
    const unit = unitBox(dialog, 'media_type');
    await fillStable(unit, 'mm');
    expect((await save(page, dialog, 'media_type')).status()).toBe(200);
    expect((await fieldOf('media_type')).unit).toBe('mm');
    await fillStable(unit, '');
    expect((await save(page, dialog, 'media_type')).status()).toBe(200);
    await expect(unit).toHaveValue('');
    expect((await fieldOf('media_type')).unit).toBeNull();
});

test('a field of your own named as one the SDK computes shows no description or unit, and none again once a written one is cleared', async ({ page }) => {
    for (const name of ['brightness', 'width']) {
        const field = await fieldOf(name);
        expect([field.description, field.unit, field.description_source], name).toEqual([null, null, null]);
    }
    const dialog = await openFields(page);
    for (const name of ['brightness', 'width']) {
        await expect(descriptionBox(dialog, name), name).toHaveValue('');
        await expect(descriptionBox(dialog, name), name).toHaveAccessibleDescription('');
        await expect(descriptionBox(dialog, name), name).not.toHaveCSS('font-style', 'italic');
        await expect(unitBox(dialog, name), name).toHaveValue('');
    }
    // Written, then cleared: nothing, not a description by its name.
    const box = descriptionBox(dialog, 'width');
    await fillStable(box, 'Print width, in centimetres.');
    expect((await save(page, dialog, 'width')).status()).toBe(200);
    expect((await fieldOf('width')).description_source).toBe('set');
    await fillStable(box, '');
    expect((await save(page, dialog, 'width')).status()).toBe(200);
    await expect(box).toHaveValue('');
    await expect(box).toHaveAccessibleDescription('');
    const cleared = await fieldOf('width');
    expect([cleared.description, cleared.unit, cleared.description_source]).toEqual([null, null, null]);
    // And the filter sidebar says nothing on their names.
    await page.keyboard.press('Escape');
    for (const name of ['brightness', 'width']) {
        await expect(page.getByTestId(`field-toggle-${name}`), name).toHaveAccessibleDescription('');
    }
});

test('a unit alone shows nothing in the sidebar; the boxes stop at 1,000 and 40 characters; a refusal is shown in the box', async ({ page }) => {
    const sent = descriptionPatches(page);
    const dialog = await openFields(page);
    // A unit alone.
    await fillStable(unitBox(dialog, 'crop'), 'kg');
    expect((await save(page, dialog, 'crop')).status()).toBe(200);
    expect(sent.at(-1)).toEqual({ unit: 'kg' });
    const crop = await fieldOf('crop');
    expect([crop.description, crop.unit, crop.description_source]).toEqual([null, 'kg', null]);
    await expect(descriptionBox(dialog, 'crop')).toHaveAccessibleDescription('');

    // The limits: typing stops there, and what is there is saved.
    const box = descriptionBox(dialog, 'keywords');
    await expect(box).toHaveAttribute('maxlength', '1000');
    await box.fill('d'.repeat(1_005));
    await expect.poll(async () => (await box.inputValue()).length).toBe(1_000);
    const unit = unitBox(dialog, 'keywords');
    await expect(unit).toHaveAttribute('maxlength', '40');
    await unit.fill('u'.repeat(45));
    await expect.poll(async () => (await unit.inputValue()).length).toBe(40);
    expect((await save(page, dialog, 'keywords')).status()).toBe(200);
    const keywords = await fieldOf('keywords');
    expect([keywords.description?.length, keywords.unit?.length]).toEqual([1_000, 40]);

    // A control character: the server refuses, the box says why and keeps what was typed; typing clears it.
    const gain = unitBox(dialog, 'detections.label');
    await gain.fill('m\u0007s');
    expect((await save(page, dialog, 'detections.label')).status()).toBe(422);
    const alert = dialog.getByRole('alert').filter({ hasText: 'A unit may not hold control characters' });
    await expect(alert).toHaveText('Value error, A unit may not hold control characters');
    await expect(gain).toHaveValue('m\u0007s');
    await gain.fill('ms');
    await expect(alert).toHaveCount(0);

    // Nothing for a unit alone on the sidebar's crop.
    await page.keyboard.press('Escape');
    const toggle = page.getByTestId('field-toggle-crop');
    await expect(toggle).toHaveAccessibleDescription('');
    await toggle.hover();
    await expect(page.getByRole('tooltip')).toHaveCount(0);
});

test('the sidebar shows a field\'s description on hover and on keyboard focus, with its unit; not on a click; a save shows at once', async ({ page }) => {
    const description = 'Sensor gain applied before readout; higher in dark scenes.';
    await describeByApi('digital_gain', { description, unit: 'dB' });
    await openDataset(page, datasetId);
    const toggle = page.getByTestId('field-toggle-digital_gain');
    const tip = page.getByTestId('field-description-digital_gain');
    await expect(toggle).toHaveAccessibleDescription(`${description} Unit: dB`);
    await expect(page.getByText(description)).toHaveCount(0);  // not in the page until a tip opens
    await expect(page.getByRole('tooltip')).toHaveCount(0);

    // Hover.
    await toggle.hover();
    await expect(tip).toHaveRole('tooltip');
    await expect(tip).toContainText(description);
    await expect(tip).toContainText('Unit: dB');
    await page.mouse.move(0, 0);
    await expect(page.getByRole('tooltip')).toHaveCount(0);

    // Keyboard: Tab onto the name shows it; Escape hides it.
    await page.getByTestId('field-toggle-crop').focus();
    for (let i = 0; i < 40 && !(await toggle.evaluate(el => el === document.activeElement)); i += 1) await page.keyboard.press('Tab');
    await expect(toggle).toBeFocused();
    await expect(tip).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('tooltip')).toHaveCount(0);
    await page.keyboard.press('Shift+Tab');
    await expect(page.getByRole('tooltip')).toHaveCount(0);

    // A click focuses it but shows nothing once the pointer leaves.
    await toggle.click();
    await page.mouse.move(0, 0);
    await expect(page.getByRole('tooltip')).toHaveCount(0);

    // A default: the built-in tags, and the System group's ID, whose name is a tab stop because it is described. A
    // field of your own named as an SDK metric has none.
    const tags = await fieldOf('tags');
    await expect(page.getByTestId('field-toggle-tags')).toHaveAccessibleDescription(tags.description!);
    await expect(page.getByTestId('field-toggle-width')).toHaveAccessibleDescription('');
    const id = await fieldOf('id');
    await expect(page.getByTestId('panel-filters').getByText('ID', { exact: true })).toHaveAccessibleDescription(id.description!);

    // Saved in Dataset Settings: the tip shows the new text without a reload.
    await page.getByRole('button', { name: 'Dataset Settings', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Dataset Settings' });
    await dialog.getByRole('button', { name: 'Fields', exact: true }).click();
    await fillStable(descriptionBox(dialog, 'digital_gain'), 'Gain before readout.');
    expect((await save(page, dialog, 'digital_gain')).status()).toBe(200);
    await page.keyboard.press('Escape');
    await expect(toggle).toHaveAccessibleDescription('Gain before readout. Unit: dB');
    await toggle.hover();
    await expect(tip).toContainText('Gain before readout.');
});

test('saving a description changes nothing else: the type, an applied filter, and another dataset\'s field of the same name', async ({ page }) => {
    const before = await fieldOf('brightness');
    const otherBefore = await fieldOf('brightness', otherId);
    await page.goto(`/datasets/${datasetId}?q=${encodeURIComponent('brightness >= 20')}`);
    await expectTotal(page, 2);
    await page.getByRole('button', { name: 'Dataset Settings', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Dataset Settings' });
    await dialog.getByRole('button', { name: 'Fields', exact: true }).click();
    await fillStable(descriptionBox(dialog, 'brightness'), 'How bright the picture is, 0 to 255.');
    expect((await save(page, dialog, 'brightness')).status()).toBe(200);
    await expect(dialog.getByRole('combobox', { name: 'Declared type for brightness' })).toHaveValue('number');
    await page.keyboard.press('Escape');
    await expectTotal(page, 2);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);
    const after = await fieldOf('brightness');
    expect([after.type, after.description_source]).toEqual([before.type, 'set']);
    const otherAfter = await fieldOf('brightness', otherId);
    expect([otherAfter.description, otherAfter.unit, otherAfter.description_source])
        .toEqual([otherBefore.description, otherBefore.unit, otherBefore.description_source]);
    expect((await fieldOf('digital_gain', otherId)).description_source).toBeNull();
});

for (const [who, key, canEdit] of [
    ['a viewer', 'view', false], ['a tagger', 'tag', false], ['a guest shared at edit', 'guest', false],
    ['an editor', 'edit', true], ['a manager', 'manage', true], ['an admin', undefined, true],
] as const) {
    test(`${who} ${canEdit ? 'may change descriptions' : 'sees them, can\'t change them, and is told why'}; the sidebar tip is the same`, async ({ browser }) => {
        const { context, page } = await signIn(browser, key ? users[key] : undefined);
        try {
            const tags = await fieldOf('tags');
            const dialog = await openFields(page);
            await expect(descriptionBox(dialog, 'tags')).toHaveValue(tags.description!);
            await expect(dialog.getByText(DEFAULT_LINE).first()).toBeVisible();
            for (const field of ['width', 'crop', 'tags']) {
                for (const control of [descriptionBox(dialog, field), unitBox(dialog, field), saveButton(dialog, field)]) {
                    if (canEdit) {
                        await expect(control).not.toHaveAttribute('title');
                    } else {
                        await expect(control).toBeDisabled();
                        await expect(control).toHaveAttribute('title', REASON.edit);
                        await expect(control).toHaveAccessibleDescription(REASON.edit);
                    }
                }
                if (canEdit) {
                    await expect(descriptionBox(dialog, field)).toBeEnabled();
                    await expect(unitBox(dialog, field)).toBeEnabled();
                    await expect(saveButton(dialog, field)).toBeDisabled();  // until something changes
                }
            }
            if (canEdit) await expect(descriptionBox(dialog, 'tags')).toHaveAccessibleDescription(DEFAULT_LINE);
            if (canEdit) await expect(dialog.getByText(VIEW_ONLY)).toHaveCount(0);
            else await expect(dialog.getByText(VIEW_ONLY)).toBeVisible();
            await page.keyboard.press('Escape');
            await expect(page.getByTestId('field-toggle-tags')).toHaveAccessibleDescription(tags.description!);
            await page.getByTestId('field-toggle-tags').hover();
            await expect(page.getByTestId('field-description-tags')).toContainText(tags.description!);
        } finally {
            await context.close();
        }
    });
}

test('a level lost between opening the tab and saving: the server\'s refusal is shown in the box, with what was typed', async ({ browser }) => {
    const editor = await createUser(cookie, `e2e_fd_lowered_${S}`);
    await share(cookie, datasetId, { user_id: editor.id }, 'edit');
    const { context, page } = await signIn(browser, editor);
    try {
        const dialog = await openFields(page);
        await fillStable(descriptionBox(dialog, 'detections.label'), 'What each detection is.');
        await share(cookie, datasetId, { user_id: editor.id }, 'view');
        expect((await save(page, dialog, 'detections.label')).status()).toBe(403);
        await expect(dialog.getByRole('alert').filter({ hasText: REASON.edit })).toHaveText(REASON.edit);
        await expect(descriptionBox(dialog, 'detections.label')).toHaveValue('What each detection is.');
        expect((await fieldOf('detections.label')).description).toBeNull();
    } finally {
        await context.close();
        await apiFetch(cookie, `/users/${editor.id}`, { method: 'DELETE' });
    }
});
