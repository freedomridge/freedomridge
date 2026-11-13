import { test, expect } from '@playwright/test';

// A segmentation mask is painted once, at its own resolution, and the browser scales it: zooming and panning the
// sample view used to repaint every display pixel of it on each mouse move, which made moving around a masked
// image crawl. Mocked API, as snapshot-browser.spec.ts: the snapshot browser opens a sample with its mask shown.

// Index masks with class 1 on the left half and background (0) on the right: one the image's size (40×30), and a
// square one (20×20) that is stretched onto the 40×30 image, as a model's output mask often is.
const SQUARE_MASK = 'iVBORw0KGgoAAAANSUhEUgAAABQAAAAUCAYAAACNiR0NAAAAIElEQVR4nGNgZGD4TwxmIBaPGjhq4KiBowaOGjhSDQQAMouPSEj81ToAAAAASUVORK5CYII=';
const MASK = 'iVBORw0KGgoAAAANSUhEUgAAACgAAAAeCAYAAABe3VzdAAAAMElEQVR4nO3OoQEAAAgDoPn/0XqBbZFAJ5NsU9oEBQUFBQUFBQUFBQUFBQUFBQU/BzzEreUSx5VPAAAAAElFTkSuQmCC';
const IMAGE = 'iVBORw0KGgoAAAANSUhEUgAAACgAAAAeCAIAAADRv8uKAAAAJ0lEQVR4nO3NMQEAAAwCIKMb3RjbAQVIj0QsFovFYrFYLBaLxX/jAbISCGrs/it7AAAAAElFTkSuQmCC';

const sample = {
    id: 7,
    version_id: 41,
    filepath: 'image.png',
    media_type: 'image',
    tags: [],
    metadata: { segmentation: { maskPath: 'mask.png' } },
};

const snapshot = {
    id: 41,
    name: 'masks',
    sample_count: 1,
    include_metadata: true,
    snapshot_format: 3,
    created_at: '2026-11-13T12:00:00Z',
    dataset_snapshot: {
        name: 'Masks',
        media_fields: ['filepath'],
        grid_media_field: 'filepath',
        modal_media_field: 'filepath',
        segmentation_config: { default_mask_targets: { '1': 'weed' }, color_scheme: { fields: { segmentation: { idColors: { '1': '#ff0000' } } } } },
        fields: [{ field_name: 'segmentation', field_type: 'dict' }],
    },
};

let maskPng = MASK;

test.beforeEach(async ({ page }) => {
    maskPng = MASK;
    // Count every canvas paint of pixel data, the mask's repaint.
    await page.addInitScript(() => {
        const put = CanvasRenderingContext2D.prototype.putImageData as (...args: unknown[]) => void;
        (window as unknown as { __maskPaints: number }).__maskPaints = 0;
        CanvasRenderingContext2D.prototype.putImageData = function (this: CanvasRenderingContext2D, ...args: unknown[]) {
            (window as unknown as { __maskPaints: number }).__maskPaints += 1;
            return put.apply(this, args);
        } as typeof CanvasRenderingContext2D.prototype.putImageData;
    });
    await page.route('**/api/**', async route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/api/auth/me') {
            await route.fulfill({ json: { id: 1, username: 'viewer', role: 'guest' } });
        } else if (url.pathname === '/api/datasets/1/versions/41') {
            await route.fulfill({ json: snapshot });
        } else if (url.pathname === '/api/datasets/1/versions/41/views') {
            await route.fulfill({ json: { views: [] } });
        } else if (url.pathname === '/api/datasets/1/versions/41/samples') {
            await route.fulfill({ json: { items: [sample], total: 1 } });
        } else if (url.pathname === '/api/samples/7/media') {
            const mask = url.searchParams.get('field') === 'segmentation.maskPath';
            await route.fulfill({ contentType: 'image/png', body: Buffer.from(mask ? maskPng : IMAGE, 'base64') });
        } else {
            await route.fulfill({ status: 404, json: { detail: 'No live data in this test' } });
        }
    });
});

async function openMasked(page: import('@playwright/test').Page) {
    await page.goto('/datasets/1/versions/41');
    await page.getByRole('button', { name: 'Open saved sample 7' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    const mask = dialog.locator('canvas.cursor-crosshair');
    // The canvas is the image's size, whatever the mask's.
    await expect.poll(() => mask.evaluate((canvas: HTMLCanvasElement) => `${canvas.width}x${canvas.height}`)).toBe('40x30');
    // Class 1's colour on the image's left half, nothing on its right.
    const pixel = (x: number, y: number) => mask.evaluate((canvas: HTMLCanvasElement, [px, py]) =>
        Array.from(canvas.getContext('2d')!.getImageData(px, py, 1, 1).data), [x, y]);
    await expect.poll(async () => (await pixel(5, 15)).slice(0, 3)).toEqual([255, 0, 0]);
    expect((await pixel(18, 2)).slice(0, 3)).toEqual([255, 0, 0]);
    expect((await pixel(22, 27))[3]).toBe(0);
    expect((await pixel(35, 15))[3]).toBe(0);
    return mask;
}

test('a mask of another shape is stretched onto its image, as before', async ({ page }) => {
    maskPng = SQUARE_MASK;
    await openMasked(page);
});

test('a mask is painted once, and zooming and panning do not repaint it', async ({ page }) => {
    const mask = await openMasked(page);

    const paints = await page.evaluate(() => (window as unknown as { __maskPaints: number }).__maskPaints);
    const box = (await mask.boundingBox())!;
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    await page.mouse.move(cx, cy);
    for (let i = 0; i < 4; i++) await page.mouse.wheel(0, -200);
    await page.mouse.down();
    for (let i = 1; i <= 10; i++) await page.mouse.move(cx + i * 8, cy + i * 4);
    await page.mouse.up();
    await page.waitForTimeout(300);
    expect(await page.evaluate(() => (window as unknown as { __maskPaints: number }).__maskPaints)).toBe(paints);
});
