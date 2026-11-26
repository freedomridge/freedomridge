import { test, expect, type Locator, type Page } from '@playwright/test';
import { API, adminLogin, apiFetch } from './api';
import { AUTH_STATE, fillStable } from './helpers';
import { stamp } from './access-helpers';

// Field descriptions on a mirror (docs/DATASET_MANAGEMENT.md, docs/FOLLOWERS.md): the primary's fields arrive with the
// primary's descriptions; they and the built-in fields can't be described here, their controls disabled up front with
// the reason; the mirror's own @local. fields can.
// The instance follows itself, as primaries.spec.ts does. One instance may follow a given primary once, and
// primaries.spec.ts follows this one too, so this spec runs in the one-at-a-time project after every other spec
// (playwright.config.ts: *.fits.spec.ts) rather than beside it.
test.use({ storageState: AUTH_STATE });

const READ_ONLY = 'This dataset is a read-only mirror of a dataset on another Freedom Ridge instance: change it there';
type Field = { name: string; description: string | null; unit: string | null; description_source: string | null; sample_count?: number | null };

test('on a mirror, the primary\'s and built-in fields show their descriptions with every control disabled, saying why; its own @local. fields can be described', async ({ page }) => {
    test.setTimeout(360_000);
    const cookie = await adminLogin();
    const S = stamp();
    const schema = async (id: number): Promise<Field[]> => apiFetch(cookie, `/datasets/${id}/field-stats`, { method: 'POST', body: JSON.stringify({}) });
    const source = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-fd-primary-${S}` }) });
    await apiFetch(cookie, `/datasets/${source.id}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: [{ filepath: `s3://e2e-fd-mirror/${S}.jpg`, metadata: { crop: 'corn', digital_gain: 1.5 } }] }),
    });
    await apiFetch(cookie, `/datasets/${source.id}/refresh-stats`, { method: 'POST' });
    await expect.poll(async () => (await schema(source.id)).map(field => field.name), { timeout: 60_000 }).toContain('digital_gain');
    const gain = 'Sensor gain applied before readout; higher in dark scenes.';
    await apiFetch(cookie, `/datasets/${source.id}/fields/digital_gain/description`, { method: 'PATCH', body: JSON.stringify({ description: gain, unit: 'dB' }) });

    const follower = await apiFetch(cookie, '/followers', { method: 'POST', body: JSON.stringify({ name: `e2e-fd-${S}` }) });
    let linkId: number | undefined;
    let mirrorId: number | undefined;
    try {
        await apiFetch(cookie, `/followers/${follower.id}/datasets/${source.id}`, { method: 'PUT', body: JSON.stringify({ tags: 'both', views: 'primary' }) });
        linkId = (await apiFetch(cookie, '/primaries', { method: 'POST', body: JSON.stringify({ url: API, token: follower.token }) })).id;
        // The job runner syncs about once a minute.
        await expect.poll(async () => {
            const listed = await apiFetch(cookie, '/primaries');
            const link = listed.primaries.find((entry: { id: number }) => entry.id === linkId);
            const mirror = link?.mirrors?.find((entry: { origin_dataset_id: number; samples: number }) => entry.origin_dataset_id === source.id && entry.samples >= 1);
            mirrorId = mirror?.dataset_id;
            return mirrorId !== undefined;
        }, { timeout: 200_000, intervals: [3_000] }).toBe(true);
        const mirror = mirrorId!;
        await expect.poll(async () => (await schema(mirror)).find(field => field.name === 'digital_gain')?.description, { timeout: 120_000, intervals: [3_000] })
            .toBe(gain);

        // A field of the mirror's own.
        const [sample] = (await apiFetch(cookie, `/datasets/${mirror}/samples?limit=1`)).items;
        await apiFetch(cookie, `/datasets/${mirror}/samples/bulk-set`, { method: 'POST', body: JSON.stringify({ sample_ids: [sample.id], metadata: { '@local.weeds': 3 } }) });
        await expect.poll(async () => (await schema(mirror)).map(field => field.name), { timeout: 60_000, intervals: [2_000] }).toContain('@local.weeds');
        // The mirror's own statistics, so each field's section has its toggle (a field whose statistics are pending
        // shows its name without one).
        await apiFetch(cookie, `/datasets/${mirror}/refresh-stats`, { method: 'POST' });
        await expect.poll(async () => (await schema(mirror)).filter(field => ['digital_gain', '@local.weeds'].includes(field.name) && (field.sample_count ?? 0) > 0).length,
            { timeout: 90_000, intervals: [2_000] }).toBe(2);

        const dialog = await openFields(page, mirror);
        // The primary's description and unit, as they arrived.
        await expect(dialog.getByRole('textbox', { name: 'Description of digital_gain', exact: true })).toHaveValue(gain);
        await expect(dialog.getByRole('textbox', { name: 'Unit of digital_gain', exact: true })).toHaveValue('dB');

        // A field of the primary and a built-in one: the three controls disabled up front, each saying why, for an
        // administrator too. Nothing can be typed in them and nothing is sent.
        const patches: string[] = [];
        page.on('request', request => {
            if (request.method() === 'PATCH' && new URL(request.url()).pathname.includes('/fields/')) patches.push(new URL(request.url()).pathname);
        });
        for (const field of ['digital_gain', 'id']) {
            const controls = [
                dialog.getByRole('textbox', { name: `Description of ${field}`, exact: true }),
                dialog.getByRole('textbox', { name: `Unit of ${field}`, exact: true }),
                dialog.getByRole('button', { name: `Save description and unit of ${field}`, exact: true }),
            ];
            for (const control of controls) {
                await expect.soft(control, field).toBeDisabled();
                await expect.soft(control, field).toHaveAttribute('title', READ_ONLY);
                await expect.soft(control, field).toHaveAccessibleDescription(READ_ONLY);
            }
            for (const box of controls.slice(0, 2)) {
                const before = await box.inputValue();
                await box.click({ force: true });
                await page.keyboard.type('x');
                await expect.soft(box, `${field}: nothing typed`).toHaveValue(before);
            }
            await controls[2].click({ force: true });
        }
        await expect(dialog.getByRole('textbox', { name: 'Description of digital_gain', exact: true })).toHaveValue(gain);
        await expect(dialog.getByRole('alert')).toHaveCount(0);
        expect(patches, 'nothing sent').toEqual([]);
        const kept = (await schema(mirror)).find(field => field.name === 'digital_gain')!;
        expect([kept.description, kept.unit]).toEqual([gain, 'dB']);

        // Its own field: enabled, with no reason given, and saved.
        for (const control of [
            dialog.getByRole('textbox', { name: 'Description of @local.weeds', exact: true }),
            dialog.getByRole('textbox', { name: 'Unit of @local.weeds', exact: true }),
        ]) {
            await expect(control).toBeEnabled();
            await expect(control).not.toHaveAttribute('title', /./);
        }
        await fillStable(dialog.getByRole('textbox', { name: 'Description of @local.weeds', exact: true }), 'Weeds counted on this server.');
        expect((await saveField(page, dialog, '@local.weeds')).status()).toBe(200);
        await expect(dialog.getByRole('textbox', { name: 'Description of @local.weeds', exact: true })).toHaveValue('Weeds counted on this server.');
        const own = (await schema(mirror)).find(field => field.name === '@local.weeds')!;
        expect([own.description, own.description_source]).toEqual(['Weeds counted on this server.', 'set']);

        // The sidebar carries the primary's description, and the mirror's own.
        await page.keyboard.press('Escape');
        await expect(page.getByTestId('field-toggle-digital_gain')).toHaveAccessibleDescription(`${gain} Unit: dB`);
        await expect(page.getByTestId('field-toggle-@local.weeds')).toHaveAccessibleDescription('Weeds counted on this server.');
    } finally {
        if (linkId !== undefined) await apiFetch(cookie, `/primaries/${linkId}`, { method: 'DELETE' });
        if (mirrorId !== undefined) await apiFetch(cookie, `/datasets/${mirrorId}`, { method: 'DELETE' });
        await apiFetch(cookie, `/followers/${follower.id}`, { method: 'DELETE' });
        await apiFetch(cookie, `/datasets/${source.id}`, { method: 'DELETE' });
    }
});

async function openFields(page: Page, id: number): Promise<Locator> {
    await page.goto(`/datasets/${id}`);
    await expect(page.getByTestId('grid-select-all')).toBeVisible({ timeout: 30_000 });
    await page.getByRole('button', { name: 'Dataset Settings', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Dataset Settings' });
    await dialog.getByRole('button', { name: 'Fields', exact: true }).click();
    await expect(dialog.getByRole('textbox', { name: 'Description of id', exact: true })).toBeVisible();
    return dialog;
}

async function saveField(page: Page, dialog: Locator, field: string) {
    const answered = page.waitForResponse(r => r.request().method() === 'PATCH' && new URL(r.url()).pathname.endsWith(`/fields/${encodeURIComponent(field)}/description`));
    await dialog.getByRole('button', { name: `Save description and unit of ${field}`, exact: true }).click();
    return answered;
}
