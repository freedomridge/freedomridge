import { test, expect, type Locator } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch } from './api';

// Class-probability overlays (docs/SDK.md "Class probabilities"): a field drawn as a class map with its default
// rule in the grid and the sample view, its rule changed live in the sample view, and saved as the default.
// The sample and its settings are real; the two images are made here and served for its media requests.
test.use({ storageState: AUTH_STATE });

// The overlay pixel at a fraction of the canvas's width, halfway down.
async function pixel(canvas: Locator, fraction: number): Promise<number[]> {
    return canvas.evaluate((element, at) => {
        const node = element as HTMLCanvasElement;
        const context = node.getContext('2d')!;
        return Array.from(context.getImageData(Math.floor(node.width * at), Math.floor(node.height / 2), 1, 1).data);
    }, fraction);
}

test('a class-probability field draws its class map, follows a rule live, and saves it as the default', async ({ page }) => {
    const cookie = await adminLogin();
    const stamp = Date.now();
    const dataset = await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name: `e2e-probabilities-${stamp}` }),
    });
    try {
        const probabilities = { type: 'class_probabilities', probPath: `probs-${stamp}.png`, classes: 2 };
        await apiFetch(cookie, `/datasets/${dataset.id}/samples`, {
            method: 'POST',
            body: JSON.stringify({ samples: [{ filepath: `field-${stamp}.jpg`, metadata: { weeds: probabilities } }] }),
        });
        await apiFetch(cookie, `/datasets/${dataset.id}`, {
            method: 'PATCH',
            body: JSON.stringify({
                segmentation_config: {
                    mask_targets: { weeds: { '0': 'soil', '1': 'weed' } },
                    color_scheme: { fields: { weeds: { colorByAttribute: 'label', idColors: { '1': '#ff0000' } } } },
                },
            }),
        });

        // Two pixels: the left is soil for certain (255 | 0), the right a weed at 200/255 = 0.78 (55 | 200).
        const [image, planes] = await page.evaluate(() => {
            const png = (width: number, height: number, gray: number[]) => {
                const canvas = document.createElement('canvas');
                canvas.width = width;
                canvas.height = height;
                const context = canvas.getContext('2d')!;
                const data = context.createImageData(width, height);
                gray.forEach((value, i) => data.data.set([value, value, value, 255], i * 4));
                context.putImageData(data, 0, 0);
                return canvas.toDataURL('image/png').split(',')[1];
            };
            return [png(40, 20, new Array(800).fill(90)), png(4, 1, [255, 55, 0, 200])];
        });
        await page.route('**/api/samples/*/media**', route => {
            const field = new URL(route.request().url()).searchParams.get('field');
            route.fulfill({ contentType: 'image/png', body: Buffer.from(field === 'weeds.probPath' ? planes : image, 'base64') });
        });

        await page.goto(`/datasets/${dataset.id}`);
        const sidebar = page.getByTestId('panel-filters');
        const row = sidebar.locator('label[title="weeds"]').locator('..');
        await expect(row).toBeVisible();
        await row.getByTitle('Show on image', { exact: true }).click();

        // The grid draws the default rule's class map (argmax): nothing over soil, the weed colour over the weed.
        const cell = page.getByTestId('grid-cell').first();
        const tile = cell.getByTestId('class-probability-overlay');
        await expect.poll(() => pixel(tile, 0.75)).toEqual([255, 0, 0, 255]);
        expect((await pixel(tile, 0.25))[3]).toBe(0);

        await cell.click();
        const modal = page.getByRole('dialog');
        const overlay = modal.getByTestId('class-probability-overlay');
        await expect.poll(() => pixel(overlay, 0.75)).toEqual([255, 0, 0, 255]);

        // A threshold redraws at once: 0.78 passes 0.5, then fails 0.9 and, with no fallback, shows nothing.
        const editor = modal.getByTestId('class-rule-editor-weeds');
        await editor.getByLabel('weeds rule').selectOption('rules');
        await expect.poll(() => pixel(overlay, 0.75)).toEqual([255, 0, 0, 255]);
        await editor.getByLabel('Threshold 1 minimum').fill('0.9');
        await expect.poll(async () => (await pixel(overlay, 0.75))[3]).toBe(0);
        // One class's probability as a heatmap: the weed's 0.78 shows, soil's 0 doesn't.
        await editor.getByLabel('What weeds shows').selectOption('1');
        await expect.poll(async () => (await pixel(overlay, 0.75))[3]).toBe(200);
        expect((await pixel(overlay, 0.25))[3]).toBe(0);
        await editor.getByLabel('What weeds shows').selectOption('map');

        // Nothing was written until it's saved as the default; then the dataset has it.
        expect((await apiFetch(cookie, `/datasets/${dataset.id}`)).segmentation_config.class_rules).toBeUndefined();
        await editor.getByRole('button', { name: 'Save as default' }).click();
        await expect.poll(async () => (await apiFetch(cookie, `/datasets/${dataset.id}`)).segmentation_config.class_rules?.weeds)
            .toEqual({ type: 'rules', rules: [{ classes: [1], min: 0.9, class: 1 }], fallback: null });
        await expect(editor.getByRole('button', { name: 'Save as default' })).toHaveCount(0);
    } finally {
        await apiFetch(cookie, `/datasets/${dataset.id}`, { method: 'DELETE' });
    }
});
