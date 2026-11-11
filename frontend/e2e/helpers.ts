import path from 'path';
import { BrowserContext, Locator, Page, Route, expect } from '@playwright/test';
import { readState } from './api';

export const AUTH_STATE = path.join(__dirname, '.auth-state.json');

/**
 * Wait until React has hydrated the element, so typing into it reaches React state.
 *
 * `page.goto()` resolves when the server HTML is parsed — BEFORE hydration. A `fill()` in that
 * window writes only to the DOM node; React never sees an event, so its state stays ''. A
 * controlled form like /login submits from state, so the request goes out EMPTY and the backend
 * answers a perfectly correct 401 — the failure reads as "bad credentials", not as a race.
 * (Diagnosed from a Playwright trace: the login POST body was literally
 * `{"username":"","password":""}`.) It only surfaces under load, when a busy dev server is slow
 * to hydrate.
 *
 * Note that asserting `toHaveValue` after `fill` does NOT catch this: that reads the DOM node,
 * which happily holds the text React never learned about. The only reliable signal is React
 * having claimed the node, which it marks with `__reactFiber$…` / `__reactProps$…` expandos.
 */
export async function waitForHydration(locator: Locator) {
    await expect
        .poll(() => locator.evaluate(el => Object.keys(el).some(k => k.startsWith('__react'))), { timeout: 20_000 })
        .toBe(true);
}

/**
 * Fill a React-controlled input, waiting for hydration first so the value reaches React state.
 * Re-fills until it sticks, which also covers a re-render landing between fill and submit.
 */
export async function fillStable(locator: Locator, value: string) {
    await waitForHydration(locator);
    await expect(async () => {
        await locator.fill(value);
        await expect(locator).toHaveValue(value, { timeout: 1_000 });
    }).toPass({ timeout: 15_000 });
}

/** Open the shared fixture dataset, or a spec's own (assumes an authenticated storageState). */
export async function gotoDataset(page: Page, datasetId = readState().datasetId) {
    await page.goto(`/datasets/${datasetId}`);
    await expect(page.getByTestId('total-samples')).toBeVisible({ timeout: 20_000 });
}

/** The applied (server-truth) sample count from the sub-header. */
export async function appliedTotal(page: Page): Promise<number> {
    const text = await page.getByTestId('total-samples').textContent();
    return parseInt((text || '').replace(/[^0-9]/g, ''), 10);
}

/** The header toggle of a filter-sidebar field's section. */
export function fieldToggle(page: Page, name: string): Locator {
    return page.getByTestId('panel-filters').getByTestId(`field-toggle-${name}`);
}

/**
 * Opens `datasetId` as a dataset past the sidebar's expand-all line (more than 100,000 images): its
 * sections start collapsed, except fields that filter the view. The e2e datasets are small, so the
 * page is told so in the dataset's answer (`sidebar_expand_all`, which the server computes).
 */
export async function asLargeDataset(page: Page | BrowserContext, datasetId: number) {
    await page.route(new RegExp(`/api/datasets/${datasetId}$`), async (route: Route) => {
        if (route.request().method() !== 'GET') return route.continue();
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...(await response.json()), sidebar_expand_all: false } });
    });
}

/**
 * Expand a filter-sidebar field's section if it is collapsed. On a small dataset every section starts
 * expanded; on a large one only the fields that filter the view, so a test expands a field before
 * using its inputs.
 */
export async function expandField(page: Page, name: string) {
    const toggle = fieldToggle(page, name);
    await expect(toggle).toBeVisible({ timeout: 20_000 });
    await expect(async () => {
        if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
        await expect(toggle).toHaveAttribute('aria-expanded', 'true', { timeout: 1_000 });
    }).toPass({ timeout: 15_000 });
}

/** Expand every field section of the filter sidebar (`name`: a field to wait for first). */
export async function expandAllFields(page: Page, name: string) {
    await expandField(page, name);
    const toggles = page.getByTestId('panel-filters').locator('[data-testid^="field-toggle-"]');
    for (const toggle of await toggles.all()) {
        if (await toggle.getAttribute('aria-expanded') === 'false') await toggle.click();
    }
    await expect(toggles.and(page.locator('[aria-expanded="false"]'))).toHaveCount(0);
}

type FieldEntry = Record<string, unknown> & { name: string };

/**
 * Answer an intercepted field-stats request with its real answer, every field entry passed through
 * `rewrite`. Handles the sidebar's JSON list and a facet request's NDJSON stream (one entry per line);
 * an error answer (say a 404 once the test deleted its dataset) passes through as is.
 */
export async function fulfillFieldStats(route: Route, rewrite: (field: FieldEntry) => FieldEntry) {
    const response = await route.fetch();
    if (!response.ok()) return route.fulfill({ response });
    const text = await response.text();
    if ((response.headers()['content-type'] ?? '').includes('ndjson')) {
        const lines = text.split('\n').filter(line => line.trim()).map(line => JSON.stringify(rewrite(JSON.parse(line))));
        return route.fulfill({ response, body: lines.map(line => `${line}\n`).join('') });
    }
    return route.fulfill({ response, json: (JSON.parse(text) as FieldEntry[]).map(rewrite) });
}

/** Wait until the applied total equals `n`. */
export async function expectTotal(page: Page, n: number) {
    await expect(page.getByTestId('total-samples')).toHaveText(
        // "images" on a dataset with object rows: the sub-header counts the image grain there.
        new RegExp(`^\\s*${n} (samples|images)\\s*$`), { timeout: 15_000 });
}

/**
 * A Select All over 50,000 samples, applied as a server token. The 12-row fixture never needs one, so the two
 * responses it gets (its count, its token) are faked. `grid`: the grid panel to press it in, when there are several.
 */
export async function bigSelectAll(page: Page, grid: Page | Locator = page) {
    await page.route('**/samples/query', async route => {
        if (route.request().postDataJSON()?.limit === 1) {
            await route.fulfill({ json: { items: [], total: 60_000 } });  // Select All's count probe
        } else {
            await route.continue();
        }
    });
    await page.route('**/selection_token', route => route.fulfill({ json: { token: 'e2e-expired-token', count: 60_000 } }));
    await grid.getByTestId('grid-select-all').click();
    await expect(grid.getByTestId('grid-selection-count')).toContainText('60,000', { timeout: 10_000 });
    await page.unroute('**/samples/query');
    await page.unroute('**/selection_token');
}
