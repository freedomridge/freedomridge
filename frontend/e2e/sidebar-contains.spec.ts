import { test, expect, type Page } from '@playwright/test';
import { adminLogin, apiFetch } from './api';
import { AUTH_STATE, expectTotal, expandField, expandAllFields } from './helpers';
import { convertSimpleFiltersToComplex, describeFilter, getSimpleFilterStages, isTextFilter, type TextFilter } from '../lib/filter-utils';
import type { ComplexFilter, Filters } from '../lib/api';

test.use({ storageState: AUTH_STATE, viewport: { width: 1600, height: 1100 } });

async function expectPreview(page: Page, count: number) {
    await expect(page.getByTestId('grid-view-count')).toHaveText(`${count.toLocaleString('en-US')} image${count === 1 ? '' : 's'}`);
}

async function pickExact(page: Page, value: string) {
    await page.getByTestId('panel-filters').getByPlaceholder(/color/i).fill(value);
    await page.locator('[data-searchable-dropdown]').getByText(value, { exact: true }).click();
}

async function expectPersistedFilters(page: Page, datasetId: number, simpleFilters: Filters, appliedFilters: ComplexFilter[]) {
    await expect.poll(() => page.evaluate(id => {
        const state = JSON.parse(localStorage.getItem(`fridge-filters-${id}`) || 'null');
        return state && { simpleFilters: state.simpleFilters, appliedFilters: state.viewStages.map((stage: { filter: ComplexFilter }) => stage.filter) };
    }, datasetId)).toEqual({ simpleFilters, appliedFilters });
}

async function restoreDraft(page: Page, datasetId: number, simpleFilters: Filters) {
    // Written before the reloaded app starts, once: written into the live page instead, the app's
    // debounced save of its current (empty) draft could land after it and reload unfiltered.
    const marker = `e2e-restored-draft-${datasetId}-${Date.now()}`;
    await page.addInitScript(({ id, filters, marker }) => {
        if (sessionStorage.getItem(marker)) return;
        sessionStorage.setItem(marker, '1');
        localStorage.setItem(`fridge-filters-${id}`, JSON.stringify({ simpleFilters: filters, viewStages: [] }));
    }, { id: datasetId, filters: simpleFilters, marker });
    await page.reload();
}

test('text drafts compile to one OR, exclusion negates the union, and chip labels preserve intent', () => {
    const selection: TextFilter = { _op: 'text', values: ['red', 'blue'], contains: ['dar', '%_'] };
    const expected = { logic: 'OR', filters: [
        { field: 'color', operator: 'in', value: ['red', 'blue'] },
        { field: 'color', operator: 'contains', value: 'dar' },
        { field: 'color', operator: 'contains', value: '%_' },
    ] };
    const included = convertSimpleFiltersToComplex({ color: selection });
    expect(included).toEqual(expected);
    expect(describeFilter(included)).toBe('color: red, blue or contains "dar" or contains "%_"');
    const excluded = convertSimpleFiltersToComplex({ exclude_color: selection });
    expect(excluded).toEqual({ logic: 'NOT', filters: [expected] });
    expect(describeFilter(excluded)).toBe('not (color: red, blue or contains "dar" or contains "%_")');
    expect(getSimpleFilterStages({ exclude_color: selection })).toEqual([
        { type: 'filter', description: describeFilter(excluded), filter: excluded },
    ]);
    expect(convertSimpleFiltersToComplex({ color: { _op: 'text', values: [], contains: [] } }))
        .toEqual({ logic: 'AND', filters: [] });
    expect(convertSimpleFiltersToComplex({ color: [] })).toEqual({ logic: 'AND', filters: [] });
    expect(convertSimpleFiltersToComplex({ color: 'red' })).toEqual({ field: 'color', operator: 'contains', value: 'red' });
    expect(convertSimpleFiltersToComplex({ color: ['red'] })).toEqual({ field: 'color', operator: 'in', value: ['red'] });
    expect(isTextFilter({ _op: 'text' })).toBe(false);
    expect(isTextFilter({ _op: 'text', values: [1], contains: [] })).toBe(false);
    expect(convertSimpleFiltersToComplex({ brightness_min: 5 })).toEqual({ field: 'brightness', operator: '>=', value: 5 });
    expect(convertSimpleFiltersToComplex({ exclude_color: ['red'] })).toEqual({ field: 'color', operator: 'not in', value: ['red'] });
});

test.describe('Full categorical fixture', () => {
    let cookie = '';
    let datasetId = 0;
    test.beforeAll(async () => {
        test.setTimeout(180_000);
        cookie = await adminLogin();
        datasetId = (await apiFetch(cookie, '/datasets', {
            method: 'POST', body: JSON.stringify({ name: `e2e-sidebar-c1-${Date.now()}` }),
        })).id;
        const colors = ['red', 'reddish', 'infrared', 'blue', 'green'];
        for (let offset = 0; offset < 111000; offset += 5000) {
            await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
                method: 'POST', body: JSON.stringify({ samples: Array.from({ length: Math.min(5000, 111000 - offset) }, (_, index) => ({
                    filepath: `s3://sidebar-c1/${offset + index}.jpg`,
                    metadata: { color: colors[(offset + index) % colors.length] },
                })) }),
            });
        }
        await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
    });
    test.afterAll(async () => {
        if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
    });

    test('red + Enter matches 66,600 while ticking red matches exactly 22,200', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 111000);
        await expandField(page, 'color');
        const picker = page.getByTestId('panel-filters').getByPlaceholder(/color/i);
        await picker.fill('red');
        const dropdown = page.locator('[data-searchable-dropdown]');
        await expect(dropdown.getByRole('button').first()).toHaveText('Contains "red"');
        await picker.press('Enter');
        await expectPreview(page, 66600);
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 66600);
        await expect(page.getByTestId('query-stage-chip')).toContainText('color: contains "red"');
        await page.getByRole('button', { name: 'Clear All' }).click();
        await expectTotal(page, 111000);
        await pickExact(page, 'red');
        await expectPreview(page, 22200);
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 22200);
        await expect(page.getByTestId('query-stage-chip')).toContainText('color: red');
        await expect(page.getByTestId('query-stage-chip')).not.toContainText('contains');
    });
});

test.describe('sidebar text combinations', () => {
    let cookie = '';
    let datasetId = 0;
    test.beforeAll(async () => {
        cookie = await adminLogin();
        datasetId = (await apiFetch(cookie, '/datasets', {
            method: 'POST', body: JSON.stringify({ name: `e2e-sidebar-contains-${Date.now()}` }),
        })).id;
        const colors = ['red', 'reddish', 'infrared', 'blue', 'green', 'RED', null, undefined, '100%_done', '100xxdone'];
        await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
            method: 'POST', body: JSON.stringify({ samples: colors.map((color, index) => ({
                filepath: `s3://sidebar-contains/${index}.jpg`, tags: ['training'],
                metadata: { color, score: index % 2, detections: [{ label: 'car' }] },
            })) }),
        });
        await apiFetch(cookie, `/datasets/${datasetId}/fields/score/display-type`, {
            method: 'POST', body: JSON.stringify({ display_type: 'enum' }),
        });
        await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
    });
    test.afterAll(async () => {
        if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
    });

    for (const excluded of [false, true]) {
        test(`${excluded ? 'legacy excluded' : 'included'} exact values and multiple contains terms survive preview, apply, edit and reload`, async ({ page }) => {
            await page.goto(`/datasets/${datasetId}`);
            await expectTotal(page, 10);
            const sidebar = page.getByTestId('panel-filters');
            await expandField(page, 'color');
            await expect(sidebar.getByRole('combobox', { name: /match mode/ })).toHaveCount(0);
            const draft: Record<string, TextFilter> = {
                [excluded ? 'exclude_color' : 'color']: { _op: 'text', values: ['green'], contains: ['red', 'blu'] },
            };
            const label = 'color: green or contains "red" or contains "blu"';
            if (excluded) {
                await restoreDraft(page, datasetId, draft);
                await expect(sidebar.getByText(`Additional filter: not (${label})`, { exact: true })).toBeVisible();
                await expect(sidebar.getByRole('button', { name: 'Remove color exclusion' })).toBeVisible();
                await expect(sidebar.getByRole('checkbox', { name: 'green', exact: true })).not.toBeChecked();
            } else {
                await pickExact(page, 'green');
                const picker = sidebar.getByPlaceholder(/color/i);
                await picker.fill('red');
                await page.locator('[data-searchable-dropdown]').getByRole('button', { name: 'Contains "red"', exact: true }).click();
                await picker.fill('blu');
                await picker.press('Enter');
            }
            await expectPreview(page, excluded ? 4 : 6);
            await expectPersistedFilters(page, datasetId, draft, []);
            await page.reload();
            await expectPreview(page, excluded ? 4 : 6);
            if (excluded) {
                await expect(sidebar.getByText(`Additional filter: not (${label})`, { exact: true })).toBeVisible();
                await expect(sidebar.getByRole('checkbox', { name: 'Contains "red"', exact: true })).toHaveCount(0);
            } else {
                await expect(sidebar.getByRole('checkbox', { name: 'Contains "red"', exact: true })).toBeChecked();
            }
            await page.getByTestId('apply-filters').click();
            await expectTotal(page, excluded ? 4 : 6);
            const chip = page.getByTestId('query-stage-chip');
            await expect(chip).toContainText(excluded ? `not (${label})` : label);
            await chip.click();
            const editor = page.locator('[data-radix-popper-content-wrapper]');
            await expect(editor.getByPlaceholder('Value...', { exact: true })).toHaveCount(2);
            if (excluded) await expect(editor.getByRole('button', { name: 'NOT', exact: true }).first()).toHaveClass(/bg-zinc-700/);
            await editor.getByRole('button', { name: 'Apply', exact: true }).click();
            await expectTotal(page, excluded ? 4 : 6);
            await expectPersistedFilters(page, datasetId, {}, [convertSimpleFiltersToComplex(draft)]);
            await page.reload();
            await expectTotal(page, excluded ? 4 : 6);
            await expect(chip).toContainText(excluded ? `not (${label})` : label);
        });
    }

    test('a legacy excluded value stays separate from include picks and can be removed on its own', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 10);
        const sidebar = page.getByTestId('panel-filters');
        await restoreDraft(page, datasetId, { exclude_color: ['red'] });
        await expectPreview(page, 9);
        const line = sidebar.getByText('Additional filter: color: not red', { exact: true });
        await expect(line).toBeVisible();
        await expect(sidebar.getByRole('combobox', { name: /match mode/ })).toHaveCount(0);
        await sidebar.getByTestId('field-toggle-color').click();
        await expect(sidebar.getByTestId('field-toggle-color')).toHaveAttribute('aria-expanded', 'false');
        await expect(line).toBeVisible();
        await expandField(page, 'color');
        await sidebar.getByRole('checkbox', { name: 'red', exact: true }).check();
        await expectPreview(page, 0);
        await expectPersistedFilters(page, datasetId, { exclude_color: ['red'], color: ['red'] }, []);
        await page.reload();
        await expectPreview(page, 0);
        await expect(line).toBeVisible();
        await sidebar.getByRole('button', { name: 'Remove color exclusion' }).click();
        await expect(line).toHaveCount(0);
        await expectPreview(page, 1);
        await expectPersistedFilters(page, datasetId, { color: ['red'] }, []);
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 1);
    });

    test('contains terms are removable and literal, while checkbox values remain case-sensitive exact matches', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 10);
        const sidebar = page.getByTestId('panel-filters');
        await expandField(page, 'color');
        const picker = sidebar.getByPlaceholder(/color/i);
        await picker.fill('   ');
        await expect(page.locator('[data-searchable-dropdown]').getByRole('button', { name: /^Contains/ })).toHaveCount(0);
        await picker.press('Enter');
        await expect(page.getByTestId('draft-filters')).toHaveCount(0);
        await picker.fill('%_');
        await picker.press('Enter');
        await expectPreview(page, 1);
        await picker.fill('%_');
        await picker.press('Enter');
        await expect(sidebar.getByRole('checkbox', { name: 'Contains "%_"', exact: true })).toHaveCount(1);
        await sidebar.getByRole('checkbox', { name: 'Contains "%_"', exact: true }).click();
        await expect(sidebar.getByRole('checkbox', { name: 'Contains "%_"', exact: true })).toHaveCount(0);
        await expectPreview(page, 10);
        await sidebar.getByRole('checkbox', { name: 'red', exact: true }).check();
        await expectPreview(page, 1);
        await sidebar.getByRole('checkbox', { name: 'red', exact: true }).uncheck();
        await picker.fill('red');
        await picker.press('Enter');
        await expectPreview(page, 4);
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 4);
    });

    test('histogram exact-value toggles preserve sidebar contains terms', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 10);
        const sidebar = page.getByTestId('panel-filters');
        await expandField(page, 'color');
        await sidebar.getByPlaceholder(/color/i).fill('red');
        await sidebar.getByPlaceholder(/color/i).press('Enter');
        await expectPreview(page, 4);
        await sidebar.getByTestId('panel-type-select').selectOption('histogram');
        const histogram = page.getByTestId('panel-histogram');
        await histogram.getByTestId('histogram-field-select').selectOption('color');
        await histogram.getByTitle(/^blue • \d+ samples/).click();
        await expectPreview(page, 5);
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 5);
        await expect(page.getByTestId('query-stage-chip')).toContainText('color: blue or contains "red"');
    });

    test('histogram inclusion and exclusion stay visible when returning to the sidebar', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 10);
        const sidebar = page.getByTestId('panel-filters');
        await expandField(page, 'color');
        await sidebar.getByPlaceholder(/color/i).fill('red');
        await sidebar.getByPlaceholder(/color/i).press('Enter');
        await sidebar.getByTestId('panel-type-select').selectOption('histogram');
        const histogram = page.getByTestId('panel-histogram');
        await histogram.getByTestId('histogram-field-select').selectOption('color');
        const blue = histogram.getByTitle(/^blue • \d+ samples/);
        await blue.click();
        await expectPreview(page, 5);
        await blue.click();
        await expectPreview(page, 4);
        await histogram.getByTestId('panel-type-select').selectOption('filters');
        await expect(sidebar.getByText('Additional filter: color: not blue', { exact: true })).toBeVisible();
        await expect(sidebar.getByRole('checkbox', { name: 'blue', exact: true })).not.toBeChecked();
        await expect(sidebar.getByRole('checkbox', { name: 'Contains "red"', exact: true })).toBeChecked();
        await expectPreview(page, 4);
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 4);
        await expect(page.getByTestId('query-stage-chip')).toContainText('color: contains "red"; color: not blue');
    });

    test('contains replaces the no-value inclusion without changing a legacy exclusion', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 10);
        const sidebar = page.getByTestId('panel-filters');
        await restoreDraft(page, datasetId, { exclude_color: ['green'] });
        await expandField(page, 'color');
        const color = sidebar.getByTestId('field-section-color');
        await color.getByRole('checkbox', { name: '(no value)', exact: true }).check();
        await expectPreview(page, 2);
        await expectPersistedFilters(page, datasetId, { exclude_color: ['green'], color: { _op: 'is_null' } }, []);
        await color.getByPlaceholder(/color/i).fill('red');
        await color.getByPlaceholder(/color/i).press('Enter');
        await expect(sidebar.getByText('Additional filter: color: not green', { exact: true })).toBeVisible();
        await expectPreview(page, 4);
        await expectPersistedFilters(page, datasetId, { exclude_color: ['green'], color: { _op: 'text', values: [], contains: ['red'] } }, []);
    });

    test('numeric enums, native arrays and derived label lists do not offer contains', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 10);
        const sidebar = page.getByTestId('panel-filters');
        for (const name of ['score', 'tags', 'detections.label']) await expandField(page, name);
        await expect(sidebar.getByLabel('score match mode')).toHaveCount(0);
        await expect(sidebar.getByRole('checkbox', { name: '0', exact: true })).toBeVisible();
        await expect(sidebar.getByLabel('tags match mode')).toHaveCount(0);
        await expect(sidebar.getByLabel('detections.label match mode')).toHaveCount(0);
        await expect(sidebar.getByRole('checkbox', { name: 'car', exact: true })).toBeVisible();
    });

    test('the contains action stays inside a short viewport when its input is near the bottom', async ({ page }) => {
        await page.setViewportSize({ width: 1280, height: 600 });
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 10);
        // Every section expanded: the sidebar scrolls, so the input can sit at the bottom of the viewport.
        await expandAllFields(page, 'color');
        const picker = page.getByTestId('panel-filters').getByPlaceholder(/color/i);
        await picker.evaluate(element => element.scrollIntoView({ block: 'end' }));
        await picker.fill('red');
        const dropdown = page.locator('[data-searchable-dropdown]');
        await expect(dropdown.getByRole('button', { name: 'Contains "red"', exact: true })).toBeVisible();
        const inputBounds = await picker.boundingBox();
        const dropdownBounds = await dropdown.boundingBox();
        expect(inputBounds).not.toBeNull();
        expect(dropdownBounds).not.toBeNull();
        expect(inputBounds!.y).toBeGreaterThan(300);
        expect(dropdownBounds!.y).toBeGreaterThanOrEqual(0);
        expect(dropdownBounds!.y + dropdownBounds!.height).toBeLessThanOrEqual(inputBounds!.y);
        expect(dropdownBounds!.x).toBeGreaterThanOrEqual(0);
        expect(dropdownBounds!.x + dropdownBounds!.width).toBeLessThanOrEqual(1280);
        await dropdown.getByRole('button', { name: 'Contains "red"', exact: true }).click();
        await expectPreview(page, 4);
    });
});
