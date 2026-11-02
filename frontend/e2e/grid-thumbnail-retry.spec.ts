import { test, expect, type Page, type Route } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal } from './helpers';

// Grid tiles whose thumbnail load fails transiently (a cancelled lazy load mid-scroll, a VPN
// hiccup, a dropped cold fetch) used to stay blank until the tile was unmounted and remounted —
// users learned that "switching the view fixes it". The tile must retry on its own, and only after
// the retries are exhausted ping the session watchdog (once per tile, not once per blip).
test.use({ storageState: AUTH_STATE });

const TINY_JPEG = Buffer.from(
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAADAAQDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDUooor7I/LD//Z',
    'base64',
);
const MEDIA = '**/api/samples/*/media**';
const tileKey = (route: Route) => route.request().url().replace(/[?&]retry=\d+/, '');

async function tilesLoaded(page: Page) {
    return page.evaluate(() => {
        const imgs = [...document.querySelectorAll<HTMLImageElement>('[data-testid="grid-cell"] img')];
        return { total: imgs.length, loaded: imgs.filter(i => i.complete && i.naturalWidth > 0).length,
            retried: imgs.filter(i => i.dataset.retryAttempt).length };
    });
}

test('a tile whose first thumbnail load fails retries and ends up loaded, without a remount', async ({ page }) => {
    const seen = new Map<string, number>();
    await page.route(MEDIA, route => {
        const k = tileKey(route); const n = (seen.get(k) ?? 0) + 1; seen.set(k, n);
        // First attempt for every tile fails (what a cancelled/dropped load looks like to <img>).
        if (n === 1) return route.abort('failed');
        return route.fulfill({ status: 200, contentType: 'image/jpeg', body: TINY_JPEG });
    });
    await gotoDataset(page);
    await expectTotal(page, 12);
    await expect.poll(async () => { const t = await tilesLoaded(page); return t.total > 0 && t.loaded === t.total; },
        { timeout: 20_000, message: 'every tile should recover via retry' }).toBe(true);
    const t = await tilesLoaded(page);
    expect(t.retried).toBe(t.total);            // every tile needed (exactly one) retry
    expect([...seen.values()].every(n => n === 2)).toBe(true);
});

test('a tile that keeps failing gives up after the retries and pings the session check once', async ({ page }) => {
    await page.addInitScript(() => {
        (window as unknown as { __checks: number }).__checks = 0;
        window.addEventListener('fridge:check-session', () => { (window as unknown as { __checks: number }).__checks += 1; });
    });
    const seen = new Map<string, number>();
    await page.route(MEDIA, route => { const k = tileKey(route); seen.set(k, (seen.get(k) ?? 0) + 1); return route.abort('failed'); });
    await gotoDataset(page);
    await expectTotal(page, 12);
    // 3 retries at 0.8 s + 2 s + 5 s, then the one watchdog event per tile.
    await expect.poll(() => page.evaluate(() => (window as unknown as { __checks: number }).__checks),
        { timeout: 25_000 }).toBeGreaterThan(0);
    await page.waitForTimeout(3_000); // nothing further should arrive
    const checks = await page.evaluate(() => (window as unknown as { __checks: number }).__checks);
    const tiles = await page.getByTestId('grid-cell').count();
    expect(checks).toBe(tiles);                 // once per tile, not once per failed attempt
    expect([...seen.values()].every(n => n === 4)).toBe(true); // 1 initial + 3 retries, no more
    expect((await tilesLoaded(page)).loaded).toBe(0);
    // ...and each tile SAYS it is unavailable, instead of a blank square with a 16 px broken-image
    // glyph that looked like a tile still loading.
    await expect(page.getByTestId('media-unavailable')).toHaveCount(tiles);
});
