import { test, expect, type Page } from '@playwright/test';
import { adminLogin, apiFetch, promoteField } from './api';
import { AUTH_STATE, expectTotal, expandField, fulfillFieldStats } from './helpers';

test.use({ storageState: AUTH_STATE, viewport: { width: 1600, height: 1100 } });

test.afterEach(async ({ page }) => {
    await page.unrouteAll({ behavior: 'wait' });
});

async function withColors(run: (datasetId: number) => Promise<void>) {
    const cookie = await adminLogin();
    const dataset = await apiFetch(cookie, '/datasets', {
        method: 'POST', body: JSON.stringify({ name: `e2e-review-registry-${Date.now()}` }),
    });
    try {
        await apiFetch(cookie, `/datasets/${dataset.id}/samples`, {
            method: 'POST', body: JSON.stringify({ samples: ['red', 'reddish', 'infrared', 'blue', 'green'].map(color => ({
                filepath: `s3://registry-review/${color}.jpg`, metadata: { color },
            })) }),
        });
        await run(dataset.id);
    } finally {
        await apiFetch(cookie, `/datasets/${dataset.id}`, { method: 'DELETE' });
    }
}

async function incompleteColorPreview(page: Page, datasetId: number, values: string[], fieldType = 'string') {
    for (const endpoint of ['field-stats', 'schema']) {
        // An error passes through as is: a filtered, expanded field's own request can still be in
        // flight when the test deletes the dataset (404 {detail}).
        await page.route(`**/api/datasets/${datasetId}/${endpoint}**`, route => fulfillFieldStats(route, field => field.name === 'color'
            ? { ...field, type: fieldType, declared_type: 'string', values, values_complete: false }
            : field));
    }
}

test('incomplete categorical sidebar selections stay exact and support multiple values', async ({ page }) => {
    await withColors(async datasetId => {
        await incompleteColorPreview(page, datasetId, ['red', 'blue']);
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 5);
        await expandField(page, 'color');
        const picker = page.getByTestId('panel-filters').getByPlaceholder(/color/i);
        await picker.fill('red');
        await page.locator('[data-searchable-dropdown]').getByText('red', { exact: true }).click();
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 1);
        await page.getByRole('button', { name: 'Clear All' }).click();
        await expectTotal(page, 5);
        await picker.fill('red');
        await page.locator('[data-searchable-dropdown]').getByText('red', { exact: true }).click();
        await picker.fill('blue');
        await page.locator('[data-searchable-dropdown]').getByText('blue', { exact: true }).click();
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 2);
    });
});

for (const operator of ['=', 'in']) {
    test(`cacheless query-builder strings offer server-backed ${operator} selections`, async ({ page }) => {
        await withColors(async datasetId => {
            await incompleteColorPreview(page, datasetId, []);
            await page.goto(`/datasets/${datasetId}`);
            await expectTotal(page, 5);
            await page.getByRole('button', { name: 'Add Filter' }).click();
            await page.getByPlaceholder('Search fields to filter by...').fill('color');
            await page.locator('[data-radix-popper-content-wrapper]').getByRole('button', { name: /^color/i }).first().click();
            const editor = page.locator('[data-radix-popper-content-wrapper]');
            const operators = editor.locator('select').nth(1);
            await expect(operators.locator(`option[value="${operator}"]`)).toHaveCount(1);
            await operators.selectOption(operator);
            const picker = editor.getByPlaceholder(operator === 'in' ? '+ color' : 'Select color...', { exact: true });
            await expect(picker).toBeVisible();
            await picker.fill('red');
            await page.locator('[data-searchable-dropdown]').getByText('red', { exact: true }).click();
            await editor.getByRole('button', { name: 'Apply', exact: true }).click();
            await expectTotal(page, 1);
        });
    });
}

test('query-builder contains keeps the typed substring rather than picking an exact value', async ({ page }) => {
    await withColors(async datasetId => {
        await incompleteColorPreview(page, datasetId, []);
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 5);
        await page.getByRole('button', { name: 'Add Filter' }).click();
        await page.getByPlaceholder('Search fields to filter by...').fill('color');
        await page.locator('[data-radix-popper-content-wrapper]').getByRole('button', { name: /^color/i }).first().click();
        const editor = page.locator('[data-radix-popper-content-wrapper]');
        await editor.locator('select').nth(1).selectOption('contains');
        await editor.getByPlaceholder('Value...', { exact: true }).fill('red');
        await editor.getByRole('button', { name: 'Apply', exact: true }).click();
        await expectTotal(page, 3);
    });
});

for (const failure of ['network', 'http']) {
    test(`a ${failure} value-search failure is not presented as a timeout or exhaustive no-match result`, async ({ page }) => {
        await withColors(async datasetId => {
            await incompleteColorPreview(page, datasetId, ['red']);
            await page.route(`**/fields/color/autocomplete`, route => failure === 'network'
                ? route.abort() : route.fulfill({ status: 500, json: { detail: 'Search unavailable' } }));
            await page.goto(`/datasets/${datasetId}`);
            await expectTotal(page, 5);
            await expandField(page, 'color');
            await page.getByTestId('panel-filters').getByPlaceholder(/color/i).fill('missing-from-preview');
            const dropdown = page.locator('[data-searchable-dropdown]');
            await expect(dropdown.getByTestId('autocomplete-search-failed')).toBeVisible();
            await expect(dropdown.getByText('No matches', { exact: true })).toHaveCount(0);
        });
    });
}

test('value-search typing is debounced and changing the query cancels an in-flight request', async ({ page }) => {
    await withColors(async datasetId => {
        await incompleteColorPreview(page, datasetId, ['green']);
        const requested: string[] = [];
        const aborted: string[] = [];
        let releaseSlow = () => {};
        const slow = new Promise<void>(resolve => { releaseSlow = resolve; });
        page.on('requestfailed', request => {
            if (request.url().endsWith('/fields/color/autocomplete')) aborted.push(request.postDataJSON().q);
        });
        await page.route(`**/fields/color/autocomplete`, async route => {
            const query = route.request().postDataJSON().q;
            requested.push(query);
            if (query === 'red') await slow;
            await route.fulfill({ json: { values: [{ value: query, count: 1 }], exhaustive: true } }).catch(() => {});
        });
        try {
            await page.goto(`/datasets/${datasetId}`);
            await expectTotal(page, 5);
            await expandField(page, 'color');
            const picker = page.getByTestId('panel-filters').getByPlaceholder(/color/i);
            await picker.pressSequentially('red', { delay: 40 });
            await expect.poll(() => requested).toEqual(['red']);
            await picker.fill('blue');
            await expect.poll(() => aborted).toContain('red');
            await expect(page.locator('[data-searchable-dropdown]').getByText('blue', { exact: true })).toBeVisible();
            expect(requested).toEqual(['red', 'blue']);
        } finally {
            releaseSlow();
        }
    });
});

test('changing draft filters restarts a pending value search instead of leaving a stuck spinner', async ({ page }) => {
    await withColors(async datasetId => {
        await incompleteColorPreview(page, datasetId, ['red'], 'enum');
        let requests = 0;
        await page.route(`**/fields/color/autocomplete`, async route => {
            requests += 1;
            if (requests === 1) await new Promise(resolve => setTimeout(resolve, 1500));
            await route.fulfill({ json: { values: [{ value: 'blue', count: 1 }], exhaustive: true } }).catch(() => {});
        });
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 5);
        const sidebar = page.getByTestId('panel-filters');
        await expandField(page, 'media_type');
        await expandField(page, 'color');
        await sidebar.getByRole('checkbox', { name: 'image', exact: true }).check();
        const picker = sidebar.getByPlaceholder(/color/i);
        await picker.click();
        await page.locator('[data-searchable-dropdown]').getByText('red', { exact: true }).click();
        await picker.fill('blue');
        await expect.poll(() => requests).toBe(1);
        await sidebar.getByRole('checkbox', { name: 'image', exact: true }).press('Space');
        await expect.poll(() => requests).toBe(2);
        await expect(page.locator('[data-searchable-dropdown]').getByText('blue', { exact: true })).toBeVisible();
        await expect(page.locator('[data-searchable-dropdown]').getByText('Searching...', { exact: true })).toHaveCount(0);
    });
});

async function openFieldSettings(page: Page, datasetId: number) {
    await page.goto(`/datasets/${datasetId}`);
    await expectTotal(page, 5);
    await page.getByRole('button', { name: 'Dataset Settings', exact: true }).click();
    const dialog = page.getByRole('dialog');
    const tab = dialog.getByRole('button', { name: 'Fields', exact: true });
    await expect(tab).toBeVisible();
    await tab.click();
    return dialog;
}

test('measured fields with incompatible bounds stay visible without a pending promise', async ({ page }) => {
    await withColors(async datasetId => {
        await page.route(`**/api/datasets/${datasetId}/field-stats**`, route => fulfillFieldStats(route, field => field.name === 'color'
            ? { ...field, type: 'number', declared_type: 'number', min: null, max: null, stats_pending: false }
            : field));
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 5);
        const sidebar = page.getByTestId('panel-filters');
        const field = sidebar.getByTestId('field-section-color');
        await expect(field).toBeVisible();
        await expect(field.getByText('No compatible range available', { exact: true })).toBeVisible();
        await expect(sidebar.getByTestId('pending-field')).toHaveCount(0);
    });
});

test('embedding colors keep digit-only string identifiers categorical while numbers use a scale', async ({ page }) => {
    await withColors(async datasetId => {
        const cookie = await adminLogin();
        const samples = await apiFetch(cookie, `/datasets/${datasetId}/samples/query`, {
            method: 'POST', body: JSON.stringify({ limit: 10 }),
        });
        const identifiers = ['001', '01', '1', '010', '10'];
        const sampleIds = samples.items.map((sample: { id: number }) => sample.id);
        for (const [index, sampleId] of sampleIds.entries()) {
            await apiFetch(cookie, `/datasets/${datasetId}/samples/${sampleId}`, {
                method: 'PATCH', body: JSON.stringify({ metadata: { color: identifiers[index], score: index } }),
            });
        }
        await apiFetch(cookie, `/datasets/${datasetId}/embeddings`, {
            method: 'POST', body: JSON.stringify(sampleIds.map((sampleId: number) => ({
                sample_id: sampleId, vector: [1, 0], field_name: 'registry_color_test',
            }))),
        });
        await apiFetch(cookie, `/datasets/${datasetId}/embeddings/coordinates?field_name=registry_color_test`, {
            method: 'POST', body: JSON.stringify(sampleIds.map((sampleId: number, index: number) => ({
                sample_id: sampleId, x: index, y: index % 2,
            }))),
        });
        await apiFetch(cookie, `/datasets/${datasetId}/embeddings/recompute-viz-rank?field_name=registry_color_test`, {
            method: 'POST',
        });
        await incompleteColorPreview(page, datasetId, []);
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 5);
        const panel = page.getByTestId('panel-embeddings');
        await expect(panel.getByTestId('embeddings-point-count')).toHaveText(/^5.*points/);
        const colorPicker = panel.locator('select').filter({ has: page.locator('option[value=""]') });
        await colorPicker.selectOption('color');
        for (const identifier of identifiers) await expect(panel.locator(`span[title="${identifier}"]`)).toBeVisible();
        const colors = await Promise.all(identifiers.slice(0, 3).map(identifier => panel.locator(`span[title="${identifier}"]`)
            .evaluate(element => getComputedStyle(element.previousElementSibling!).backgroundColor)));
        expect(new Set(colors).size).toBe(3);
        await colorPicker.selectOption('score');
        await expect(panel.getByText('4.00', { exact: true })).toBeVisible();
        await expect(panel.getByText('0.00', { exact: true })).toBeVisible();
        for (const scores of [['N/A', 'Infinity', '', null, true], [0, 'N/A', 4, 'Infinity', '']]) {
            for (const [index, sampleId] of sampleIds.entries()) {
                await apiFetch(cookie, `/datasets/${datasetId}/samples/${sampleId}`, {
                    method: 'PATCH', body: JSON.stringify({ metadata: { score: scores[index] } }),
                });
            }
            await page.reload();
            await expectTotal(page, 5);
            await expect(panel.getByTestId('embeddings-point-count')).toHaveText(/^5.*points/);
            await colorPicker.selectOption('score');
            if (scores[0] === 'N/A') {
                await expect(panel.getByText('No compatible numeric values', { exact: true })).toBeVisible();
            } else {
                await expect(panel.getByText('4.00', { exact: true })).toBeVisible();
                await expect(panel.getByText('0.00', { exact: true })).toBeVisible();
                await expect(panel.getByText('No compatible numeric values', { exact: true })).toHaveCount(0);
            }
            await expect(panel.getByText(/^(Infinity|-Infinity|NaN)$/)).toHaveCount(0);
            await expect.poll(() => panel.getByTestId('embeddings-canvas').evaluate(canvasElement => {
                const canvas = canvasElement as HTMLCanvasElement;
                const pixel = canvas.getContext('2d')!.getImageData(
                    Math.round(20 + (canvas.width - 40) / 4), canvas.height - 20, 1, 1,
                ).data;
                return pixel[0] === pixel[1] && pixel[0] > 30 && pixel[2] > pixel[0];
            })).toBe(true);
        }
    });
});

test('declared field type can be corrected explicitly without converting sample values', async ({ page }) => {
    await withColors(async datasetId => {
        const dialog = await openFieldSettings(page, datasetId);
        await expect(dialog.getByText('This does not convert or rewrite sample values.', { exact: false })).toBeVisible();
        await expect(dialog.getByRole('combobox')).toHaveCount(1);
        const select = dialog.getByLabel('Declared type for color');
        await expect(select).toHaveValue('string');
        await select.selectOption('number');
        const saved = page.waitForResponse(response => response.url().endsWith(`/fields/color/type`)
            && response.request().method() === 'PATCH');
        await dialog.getByRole('button', { name: 'Save type for color' }).click();
        expect((await saved).ok()).toBeTruthy();
        await expect(select).toHaveValue('number');
        await expect(dialog.getByRole('button', { name: 'Save type for color' })).toBeDisabled();
        const cookie = await adminLogin();
        const schema = await apiFetch(cookie, `/datasets/${datasetId}/schema`);
        expect(schema.find((field: { name: string }) => field.name === 'color').declared_type).toBe('number');
        const samples = await apiFetch(cookie, `/datasets/${datasetId}/samples/query`, {
            method: 'POST', body: JSON.stringify({ limit: 10 }),
        });
        expect(samples.items.map((sample: { metadata: { color: string } }) => sample.metadata.color).sort())
            .toEqual(['blue', 'green', 'infrared', 'red', 'reddish']);
    });
});

test('type correction requires removing an existing index first', async ({ page }) => {
    await withColors(async datasetId => {
        await promoteField(await adminLogin(), datasetId, 'color');
        const dialog = await openFieldSettings(page, datasetId);
        await expect(dialog.getByLabel('Declared type for color')).toBeDisabled();
        await expect(dialog.getByRole('button', { name: 'Save type for color' })).toBeDisabled();
        await expect(dialog.getByText(/Remove this field.*index/)).toBeVisible();
    });
});

test('type correction reports server rejection without claiming success', async ({ page }) => {
    await withColors(async datasetId => {
        await page.route(`**/datasets/${datasetId}/fields/color/type`, route => route.fulfill({
            status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'Remove the index first' }),
        }));
        const dialog = await openFieldSettings(page, datasetId);
        await dialog.getByLabel('Declared type for color').selectOption('number');
        await dialog.getByRole('button', { name: 'Save type for color' }).click();
        await expect(dialog.getByRole('alert')).toContainText('Remove the index first');
        const schema = await apiFetch(await adminLogin(), `/datasets/${datasetId}/schema`);
        expect(schema.find((field: { name: string }) => field.name === 'color').declared_type).toBe('string');
    });
});
