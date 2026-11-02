import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset } from './helpers';

// Opening a sample must show the WHOLE image (fit-to-view at zoom=1). The bug: the
// zoom wrapper was unsized, so the image rendered at viewport WIDTH and any image
// with a taller aspect than the viewport opened with its top/bottom cropped out of
// view (users read this as "the modal is zoomed in").
//
// The fixture's filepaths don't resolve to real files, so serve a generated PNG for
// every media request. It must be LARGER than the viewport with a taller aspect than
// the viewer box (2000x2000 square in the wide viewer) — that's the shape that
// reproduced the crop (unsized wrapper => width-fit => vertical overflow); a tiny
// image shrink-wraps and hides the bug.
test.use({ storageState: AUTH_STATE });

test('sample modal opens with the full image contained in the viewport', async ({ page }) => {
    // Generate a real 2000x2000 PNG in the (still-blank) page.
    const dataUrl: string = await page.evaluate(() => {
        const c = document.createElement('canvas');
        c.width = 2000;
        c.height = 2000;
        const x = c.getContext('2d')!;
        x.fillStyle = '#3a7d44';
        x.fillRect(0, 0, 2000, 2000);
        return c.toDataURL('image/png');
    });
    const png = Buffer.from(dataUrl.split(',')[1], 'base64');
    await page.route('**/media**', (route) =>
        route.fulfill({ contentType: 'image/png', body: png }));

    await gotoDataset(page);
    const cell = page.getByTestId('grid-cell').first();
    await cell.waitFor({ timeout: 20_000 });
    await cell.click();

    const img = page.locator('img[alt="Sample"]').last();
    await img.waitFor({ timeout: 15_000 });
    // Wait until the image decoded and the fit sizing applied.
    await page.waitForFunction(() => {
        const im = [...document.querySelectorAll('img[alt="Sample"]')].pop() as HTMLImageElement | undefined;
        return !!im && im.naturalWidth > 0 && im.getBoundingClientRect().width > 0;
    });
    await page.waitForTimeout(300); // one paint for the wrapper resize

    const { imgRect, vpRect } = await page.evaluate(() => {
        const im = [...document.querySelectorAll('img[alt="Sample"]')].pop() as HTMLImageElement;
        // The clipping viewport is the nearest overflow-hidden ancestor.
        let el: HTMLElement | null = im.parentElement;
        while (el && getComputedStyle(el).overflow !== 'hidden') el = el.parentElement;
        return {
            imgRect: im.getBoundingClientRect().toJSON(),
            vpRect: el!.getBoundingClientRect().toJSON(),
        };
    });

    // Whole image inside the viewport (1px tolerance for rounding).
    expect(imgRect.top).toBeGreaterThanOrEqual(vpRect.top - 1);
    expect(imgRect.bottom).toBeLessThanOrEqual(vpRect.bottom + 1);
    expect(imgRect.left).toBeGreaterThanOrEqual(vpRect.left - 1);
    expect(imgRect.right).toBeLessThanOrEqual(vpRect.right + 1);
});
