import { test, expect, type Locator } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal, expandField } from './helpers';

// Panel-wiring tests: each panel's primary interaction, proven end-to-end
// against fixture-known data (12 samples: split=train for idx 0-5, val for
// 6-11). Filter BREADTH (every operator) is covered at the API layer by
// fridge.test_sdk — here we prove each UI input modality reaches the server
// and scopes the grid. All tests are read-only against the fixture.
test.use({ storageState: AUTH_STATE });

test('filter sidebar: enum checkbox scopes the grid via Apply', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, 12);

    const sidebar = page.getByTestId('panel-filters');
    await expandField(page, 'split');
    await sidebar.getByRole('checkbox', { name: 'train' }).check();
    // The draft says what it is: one chip per sidebar field.
    await expect(page.getByTestId('draft-filters')).toContainText('split');
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 6);
    await expect(page.getByTestId('grid-view-count')).toHaveText(/^\s*6 images\s*$/);

    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
});

test('histogram: click a category bar to stage a filter, Apply scopes grid', async ({ page }) => {
    await gotoDataset(page);

    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('histogram');
    const histogram = page.getByTestId('panel-histogram');
    await histogram.getByTestId('histogram-field-select').selectOption('split');

    const t0 = Date.now();
    await histogram.getByTitle(/^train • \d+ samples/).click();
    await expect(histogram.getByTestId('histogram-filter-chip')).toHaveText(/Filtering split/);
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 6);
    console.log(`[timing] histogram bar -> applied grid: ${Date.now() - t0}ms`);

    // Clicking the staged chip clears the staged filter again
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
});

test('histogram: a numeric bar stages a readable range and Apply shows exactly the bar', async ({ page }) => {
    // Regression: the chip read "In: [object Object]" with 16-digit floats.
    await gotoDataset(page);

    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('histogram');
    const histogram = page.getByTestId('panel-histogram');
    await histogram.getByTestId('histogram-field-select').selectOption('score');

    const bar = histogram.getByTitle(/^\d+ samples? • .* • Click to Filter/).first();
    const title = (await bar.getAttribute('title')) || '';
    const barCount = parseInt(title, 10);
    expect(barCount).toBeGreaterThan(0);
    await bar.click();

    const chip = histogram.getByTestId('histogram-filter-chip');
    await expect(chip).toHaveText(/Filtering score: [\d.]+ - [\d.]+/);
    await expect(chip).not.toContainText('[object Object]');
    await expect(chip).not.toHaveText(/\d\.\d{5,}/);

    await page.getByTestId('apply-filters').click();
    await expectTotal(page, barCount);

    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
});

test('query builder: add a numeric stage, chip appears, removal restores', async ({ page }) => {
    await gotoDataset(page);

    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('idx');
    await page.locator('[data-radix-popper-content-wrapper] button:has(span:text-is("idx"))').click();

    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.locator('select').nth(1).selectOption('>=');
    await editor.getByPlaceholder('Value...').fill('9');
    await editor.getByRole('button', { name: 'Apply' }).click();

    // The chip names the value, not just the field (it said "Filter (idx)").
    await expect(page.getByTestId('query-stage-chip')).toHaveText(/idx ≥ 9/);
    await expectTotal(page, 3); // idx 9, 10, 11

    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(0);
});

test('embeddings: scatter renders every fixture point', async ({ page }) => {
    await gotoDataset(page);
    const t0 = Date.now();
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^12(\s|\/|$).*points/, { timeout: 20_000 });
    console.log(`[timing] embeddings scatter ready: ${Date.now() - t0}ms`);
});

test('embeddings: drawing a lasso never repaints the points', async ({ page }) => {
    // Each lasso vertex used to repaint the whole scatter (170 ms a frame at 428k points), so a
    // shift-drag on a big dataset crawled. The outline is on its own overlay canvas now.
    await gotoDataset(page);
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^12(\s|\/|$).*points/, { timeout: 20_000 });
    await page.evaluate(() => new Promise<void>(resolve => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
    const box = await page.getByTestId('embeddings-canvas').boundingBox();
    if (!box) throw new Error('embeddings canvas has no bounding box');
    const x = (fx: number) => box.x + box.width * fx;
    const y = (fy: number) => box.y + box.height * fy;

    await page.evaluate(() => {
        const counter = window as unknown as { pointPaints: number };
        counter.pointPaints = 0;
        const fillRect = CanvasRenderingContext2D.prototype.fillRect;
        CanvasRenderingContext2D.prototype.fillRect = function (this: CanvasRenderingContext2D, ...rect: [number, number, number, number]) {
            if (this.canvas.dataset.testid === 'embeddings-canvas') counter.pointPaints += 1;
            return fillRect.apply(this, rect);
        };
    });
    await page.mouse.move(x(0.1), y(0.1));
    await page.keyboard.down('Shift');
    await page.mouse.down();
    await page.mouse.move(x(0.6), y(0.1), { steps: 10 });
    await page.mouse.move(x(0.6), y(0.6), { steps: 10 });
    await page.waitForTimeout(250);  // let the animation frames run
    const paints = await page.evaluate(() => (window as unknown as { pointPaints: number }).pointPaints);
    await page.mouse.up();
    await page.keyboard.up('Shift');
    expect(paints).toBe(0);
});

test('embeddings: lasso selection scopes the grid via Apply', async ({ page }) => {
    await gotoDataset(page);
    await expect(page.getByTestId('embeddings-point-count')).toBeVisible({ timeout: 20_000 });

    // Shift+drag a lasso around the left half of the canvas. Assertions are
    // behavior-relative (selected N -> grid shows N) so they don't depend on
    // the projection's exact screen placement.
    const canvas = page.getByTestId('embeddings-canvas');
    const box = await canvas.boundingBox();
    if (!box) throw new Error('embeddings canvas has no bounding box');
    const x = (fx: number) => box.x + box.width * fx;
    const y = (fy: number) => box.y + box.height * fy;

    await page.mouse.move(x(0.03), y(0.03));
    await page.keyboard.down('Shift');
    await page.mouse.down();
    await page.mouse.move(x(0.55), y(0.03), { steps: 8 });
    await page.mouse.move(x(0.55), y(0.97), { steps: 8 });
    await page.mouse.move(x(0.03), y(0.97), { steps: 8 });
    await page.mouse.move(x(0.03), y(0.03), { steps: 8 });
    await page.mouse.up();
    await page.keyboard.up('Shift');

    const selectedChip = page.getByTestId('embeddings-selected-count');
    await expect(selectedChip).toBeVisible({ timeout: 10_000 });
    const selected = parseInt((await selectedChip.textContent() || '').replace(/[^0-9]/g, ''), 10);
    expect(selected).toBeGreaterThan(0);

    await expect(page.getByText('Embeddings region')).toBeVisible();
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, selected);
});

test('sample modal: opens from a grid cell, shows properties, closes', async ({ page }) => {
    await gotoDataset(page);

    await page.getByTestId('grid-cell').first().click();
    const modal = page.getByRole('dialog');
    await expect(modal.getByText('Properties')).toBeVisible({ timeout: 10_000 });
    await expect(modal.getByText(/^Sample \d+/).first()).toBeVisible();
    await expect(modal.getByText('split', { exact: true })).toBeVisible(); // metadata key

    await page.keyboard.press('Escape');
    await expect(modal).toHaveCount(0);
});

test('saved views UI: save, clear, reload view restores the same samples', async ({ page }) => {
    await gotoDataset(page);

    // Stage + apply a filter, save it as a view
    await expandField(page, 'split');
    await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'train' }).check();
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 6);

    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByPlaceholder('Enter view name...').fill('ui-smoke-view');
    await page.getByRole('button', { name: 'Save View' }).click();
    await expect(page.getByText('ui-smoke-view')).toBeVisible();
    await page.getByRole('button', { name: 'Close Views' }).click();

    // Clear everything, then recover via the saved view
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);

    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByRole('button', { name: /ui-smoke-view/ }).click();
    await expectTotal(page, 6);
    await expect(page.getByTestId('query-stage-chip').first()).toBeVisible();
});

test('map panel: opens with its controls', async ({ page }) => {
    await gotoDataset(page);

    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('map');
    const map = page.getByTestId('panel-map');
    // Without NEXT_PUBLIC_MAPBOX_TOKEN the panel renders an explicit notice
    // instead of the map — skip in that environment (e.g. CI without the
    // secret) rather than fail.
    const noToken = map.getByText(/NEXT_PUBLIC_MAPBOX_TOKEN/);
    const zoomBtn = map.getByRole('button', { name: 'Zoom to Data' });
    await expect(noToken.or(zoomBtn).first()).toBeVisible({ timeout: 20_000 });
    test.skip(await noToken.isVisible(), 'No Mapbox token configured');
    await expect(zoomBtn).toBeVisible();
    await expect(map.getByRole('button', { name: 'Resize Map' })).toBeVisible();
});

test('map panel: the map fills its panel after the separator is dragged', async ({ page }) => {
    // mapbox-gl resizes its canvas on window resizes only: a dragged separator left the canvas at
    // its old width, with an empty strip beside it.
    // Window error events: Chrome reports "ResizeObserver loop completed with undelivered
    // notifications" only there, not to pageerror or the console.
    await page.addInitScript(() => {
        const target = window as unknown as { windowErrors: string[] };
        target.windowErrors = [];
        window.addEventListener('error', event => target.windowErrors.push(event.message));
    });
    await gotoDataset(page);
    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('map');
    const map = page.getByTestId('panel-map');
    const noToken = map.getByText(/NEXT_PUBLIC_MAPBOX_TOKEN/);
    await expect(noToken.or(map.getByRole('button', { name: 'Zoom to Data' })).first()).toBeVisible({ timeout: 20_000 });
    test.skip(await noToken.isVisible(), 'No Mapbox token configured');
    const canvas = map.locator('canvas.mapboxgl-canvas');
    const container = map.locator('.mapboxgl-map');
    const width = async (locator: Locator) => (await locator.boundingBox())?.width ?? 0;
    await expect.poll(() => width(canvas)).toBeGreaterThan(0);
    const before = await width(container);

    const handle = await page.locator('[data-separator]').first().boundingBox();
    if (!handle) throw new Error('the first separator has no bounding box');
    const x = handle.x + handle.width / 2;
    const y = handle.y + handle.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + 200, y, { steps: 10 });
    await page.mouse.up();

    await expect.poll(() => width(container)).toBeGreaterThan(before + 150);
    await expect.poll(async () => Math.abs((await width(canvas)) - (await width(container)))).toBeLessThan(2);
    expect(await page.evaluate(() => (window as unknown as { windowErrors: string[] }).windowErrors)).toEqual([]);
});

test('panel layout: opens at 20/50/30 and a panel stops at its 10 percent minimum', async ({ page }) => {
    // react-resizable-panels 4 reads a bare number as pixels: minSize={10} let a panel be
    // dragged down to 10px. The layout's sizes and minimum are percentages.
    await gotoDataset(page);
    await expect(page.getByTestId('panel-embeddings')).toBeVisible();
    const percents = async () => {
        const widths: number[] = [];
        for (const type of ['filters', 'grid', 'embeddings']) {
            const box = await page.getByTestId(`panel-${type}`).boundingBox();
            if (!box) throw new Error(`panel-${type} has no bounding box`);
            widths.push(box.width);
        }
        const total = widths.reduce((sum, width) => sum + width, 0);
        return widths.map(width => (100 * width) / total);
    };
    const opened = await percents();
    [20, 50, 30].forEach((want, index) => expect(Math.abs(opened[index] - want)).toBeLessThan(1.5));

    const handle = await page.locator('[data-separator]').first().boundingBox();
    const filters = await page.getByTestId('panel-filters').boundingBox();
    if (!handle || !filters) throw new Error('the first separator or the filters panel has no bounding box');
    const y = handle.y + handle.height / 2;
    await page.mouse.move(handle.x + handle.width / 2, y);
    await page.mouse.down();
    await page.mouse.move(filters.x + 2, y, { steps: 10 });
    await page.mouse.up();
    expect((await percents())[0]).toBeGreaterThan(9);
});
