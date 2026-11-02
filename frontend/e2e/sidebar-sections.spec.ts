import { test, expect, type Page, type Route } from '@playwright/test';
import { adminLogin, apiFetch } from './api';
import { AUTH_STATE, asLargeDataset, expectTotal, expandField, fieldToggle } from './helpers';

test.use({ storageState: AUTH_STATE, viewport: { width: 1600, height: 1100 } });

// Collapsible field sections: every sidebar field is a
// section. On a large dataset (more than 100,000 images) sections start collapsed, except indexed fields
// and fields that filter the view; on a smaller one every section starts expanded (the last describe
// below). The fields the user expands or collapses are remembered per dataset. Expanding and the filtered-values requests
// are covered in sidebar-on-open.spec.ts.
test.describe('sidebar field sections', () => {
    let cookie = '';
    let datasetId = 0;
    let otherId = 0;
    test.beforeAll(async () => {
        cookie = await adminLogin();
        const create = async (name: string, count: number) => {
            const id = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name }) })).id;
            await apiFetch(cookie, `/datasets/${id}/samples`, {
                method: 'POST', body: JSON.stringify({ samples: Array.from({ length: count }, (_, index) => ({
                    filepath: `s3://${name}/${index + 1}.jpg`,
                    metadata: { batch: index + 1 > count - 3 ? 'late' : 'early', brightness: (index + 1) * 10, color: `c${(index + 1) % 3}`, level: index + 1 },
                })) }),
            });
            await apiFetch(cookie, `/datasets/${id}/refresh-stats`, { method: 'POST' });
            return id;
        };
        datasetId = await create(`e2e-sidebar-sections-${Date.now()}`, 30);
        otherId = await create(`e2e-sidebar-sections-other-${Date.now()}`, 6);
        await apiFetch(cookie, `/datasets/${otherId}/fields/brightness/promote`, { method: 'POST' });
        // Promotion answers 202 and keeps building when the index runner is busy: wait for the index.
        await expect.poll(async () => {
            const schema: Array<{ name: string; is_promoted?: boolean }> = await apiFetch(cookie, `/datasets/${otherId}/schema`);
            return schema.find(field => field.name === 'brightness')?.is_promoted;
        }, { timeout: 60_000 }).toBe(true);
    });
    test.afterAll(async () => {
        for (const id of [datasetId, otherId]) if (id) await apiFetch(cookie, `/datasets/${id}`, { method: 'DELETE' });
    });
    // These tests are of a large dataset's sections (a small one's are in the next describe).
    test.beforeEach(async ({ context }) => {
        await asLargeDataset(context, datasetId);
        await asLargeDataset(context, otherId);
    });

    // The fields of every filtered-values (on-expand) request the page sends.
    function recordFacetRequests(page: Page) {
        const fields: string[][] = [];
        page.on('request', request => {
            if (request.url().endsWith('/field-stats') && request.postDataJSON()?.fields) fields.push(request.postDataJSON().fields);
        });
        return fields;
    }

    async function addLevelStage(page: Page, value: string) {
        await page.getByRole('button', { name: 'Add Filter' }).click();
        await page.getByPlaceholder('Search fields to filter by...').fill('level');
        const editor = page.locator('[data-radix-popper-content-wrapper]');
        await editor.locator('button:has(span:text-is("level"))').click();
        await editor.locator('select').nth(1).selectOption('>=');
        await editor.getByPlaceholder('Value...').fill(value);
        await editor.getByRole('button', { name: 'Apply' }).click();
    }

    test('fields start collapsed under a header with their name and type; headers work from the keyboard', async ({ page }) => {
        const facets = recordFacetRequests(page);
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        const sidebar = page.getByTestId('panel-filters');
        const schema: Array<{ name: string; type: string }> = await apiFetch(cookie, `/datasets/${datasetId}/schema`);
        for (const name of ['batch', 'brightness', 'color', 'level']) {
            const toggle = fieldToggle(page, name);
            await expect(toggle).toHaveAttribute('aria-expanded', 'false');
            await expect(toggle).toHaveText(new RegExp(`${name}${schema.find(field => field.name === name)!.type}$`));
        }
        await expect(sidebar.getByRole('checkbox')).toHaveCount(0);
        await expect(sidebar.getByText('10 - 300', { exact: true })).toHaveCount(0);

        // Enter expands, Space collapses; the focused header shows a focus ring.
        const brightness = fieldToggle(page, 'brightness');
        await brightness.focus();
        await page.keyboard.press('Enter');
        await expect(brightness).toHaveAttribute('aria-expanded', 'true');
        await expect(brightness).toBeFocused();
        const controls = await brightness.getAttribute('aria-controls');
        await expect(sidebar.locator(`[id="${controls}"]`).getByText('10 - 300', { exact: true })).toBeVisible();
        expect(await brightness.evaluate(el => el.matches(':focus-visible') && getComputedStyle(el).boxShadow !== 'none')).toBe(true);
        await page.keyboard.press('Space');
        await expect(brightness).toHaveAttribute('aria-expanded', 'false');
        await expect(sidebar.getByText('10 - 300', { exact: true })).toHaveCount(0);
        // No stage applied: nothing asks for filtered values.
        expect(facets).toEqual([]);
    });

    test('an indexed field starts expanded and remembers collapse and re-expansion across reloads', async ({ page }) => {
        const schema: Array<{ name: string; is_promoted?: boolean }> = await apiFetch(cookie, `/datasets/${otherId}/schema`);
        expect(schema.find(field => field.name === 'brightness')?.is_promoted).toBe(true);
        expect(schema.find(field => field.name === 'color')?.is_promoted).toBe(false);
        await page.goto(`/datasets/${otherId}`);
        await expectTotal(page, 6);
        const brightness = fieldToggle(page, 'brightness');
        await expect(brightness).toHaveAttribute('aria-expanded', 'true');
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');

        await brightness.click();
        await expect(brightness).toHaveAttribute('aria-expanded', 'false');
        await page.reload();
        await expectTotal(page, 6);
        await expect(brightness).toHaveAttribute('aria-expanded', 'false');
        // Filtering another field must not change the indexed field's remembered collapse.
        await addLevelStage(page, '4');
        await expectTotal(page, 3);
        await expect(brightness).toHaveAttribute('aria-expanded', 'false');
        await expect(fieldToggle(page, 'level')).toHaveAttribute('aria-expanded', 'true');

        await brightness.click();
        await expect(brightness).toHaveAttribute('aria-expanded', 'true');
        expect(await page.evaluate(id => JSON.parse(localStorage.getItem(`fridge-sidebar-collapsed-${id}`) || '[]'), otherId))
            .not.toContain('brightness');
        await page.waitForTimeout(500);  // the applied stage is saved after 300 ms
        await page.reload();
        await expectTotal(page, 3);
        await expect(brightness).toHaveAttribute('aria-expanded', 'true');
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');
    });

    test('a field that filters the view starts expanded and is the only one computed', async ({ page }) => {
        const facets = recordFacetRequests(page);
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        await addLevelStage(page, '28');
        await expectTotal(page, 3);
        const sidebar = page.getByTestId('panel-filters');
        await expect(fieldToggle(page, 'level')).toHaveAttribute('aria-expanded', 'true');
        await expect(sidebar.getByTestId('field-section-level')).toHaveAttribute('data-filtered', 'true');
        await expect(fieldToggle(page, 'level')).toContainText('filtered');
        await expect(sidebar.getByTestId('facet-status-level')).toHaveText(/Filtered values/);
        await expect(sidebar.getByText('28 - 30', { exact: true })).toBeVisible();
        for (const name of ['batch', 'brightness', 'color']) {
            await expect(fieldToggle(page, name)).toHaveAttribute('aria-expanded', 'false');
            await expect(sidebar.getByTestId(`field-section-${name}`)).not.toHaveAttribute('data-filtered');
        }
        await expect.poll(() => facets).toEqual([['level']]);
        await page.waitForTimeout(500);
        expect(facets).toEqual([['level']]);

        // The stage goes: the field stays open for this page (the user did not collapse it), unmarked;
        // it was never expanded by the user, so it is not remembered.
        await page.getByRole('button', { name: 'Clear All' }).click();
        await expectTotal(page, 30);
        await expect(fieldToggle(page, 'level')).toHaveAttribute('aria-expanded', 'true');
        await expect(sidebar.getByTestId('field-section-level')).not.toHaveAttribute('data-filtered');
        await page.waitForTimeout(500);  // the cleared filter state is saved after 300 ms
        await page.reload();
        await expectTotal(page, 30);
        await expect(fieldToggle(page, 'level')).toHaveAttribute('aria-expanded', 'false');
    });

    test('a field opened by its filter stays open while the user edits it, and keeps focus', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        const sidebar = page.getByTestId('panel-filters');
        // A draft on batch, with batch not remembered: after a reload it is open only because it filters.
        await expandField(page, 'batch');
        await sidebar.getByRole('checkbox', { name: 'late', exact: true }).check();
        await fieldToggle(page, 'batch').click();
        await page.waitForTimeout(500);  // the draft is saved after 300 ms
        await page.reload();
        const late = sidebar.getByRole('checkbox', { name: 'late', exact: true });
        await expect(late).toBeChecked();
        await expect(fieldToggle(page, 'batch')).toHaveAttribute('aria-expanded', 'true');
        // Unticking its last value removes the filter, but not the section the user is working in.
        await late.focus();
        await page.keyboard.press('Space');
        await expect(late).not.toBeChecked();
        await expect(sidebar.getByTestId('field-section-batch')).not.toHaveAttribute('data-filtered');
        await expect(fieldToggle(page, 'batch')).toHaveAttribute('aria-expanded', 'true');
        await expect(late).toBeFocused();
        await expect(sidebar.getByRole('checkbox', { name: 'early', exact: true })).toBeVisible();
        // The header Reset keeps the section open too, and leaves focus on the field's header.
        await page.keyboard.press('Space');
        await expect(late).toBeChecked();
        await sidebar.getByTestId('field-section-batch').getByRole('button', { name: 'Reset', exact: true }).click();
        await expect(late).not.toBeChecked();
        await expect(fieldToggle(page, 'batch')).toHaveAttribute('aria-expanded', 'true');
        await expect(fieldToggle(page, 'batch')).toBeFocused();
        // A number field's Reset (which disables itself) leaves focus on the field's header as well.
        await expandField(page, 'brightness');
        const brightness = sidebar.getByTestId('field-section-brightness');
        await brightness.getByRole('slider').first().focus();
        await page.keyboard.press('ArrowRight');
        const reset = brightness.getByRole('button', { name: 'Reset', exact: true });
        await expect(reset).toBeEnabled();
        await reset.click();
        await expect(reset).toBeDisabled();
        await expect(fieldToggle(page, 'brightness')).toBeFocused();
    });

    test('the expanded fields share one filtered-values request per filter change', async ({ page }) => {
        const facets = recordFacetRequests(page);
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        for (const name of ['brightness', 'color', 'batch']) await expandField(page, name);
        await addLevelStage(page, '28');
        await expectTotal(page, 3);
        await expect.poll(() => facets.length).toBe(1);
        expect([...facets[0]].sort()).toEqual(['batch', 'brightness', 'color', 'level']);
        const sidebar = page.getByTestId('panel-filters');
        await expect(sidebar.getByTestId('facet-status-brightness')).toHaveText(/Filtered values/);
        await expect(sidebar.getByText('280 - 300', { exact: true })).toBeVisible();
        await page.waitForTimeout(500);
        expect(facets).toHaveLength(1);
        // A reload with the stage restored asks once too.
        facets.length = 0;
        await page.reload();
        await expectTotal(page, 3);
        await expect.poll(() => facets.length).toBe(1);
        expect([...facets[0]].sort()).toEqual(['batch', 'brightness', 'color', 'level']);
        await page.waitForTimeout(500);
        expect(facets).toHaveLength(1);
        // Expanding one more field later asks for that field alone.
        facets.length = 0;
        await expandField(page, 'created_at');
        await expect.poll(() => facets).toEqual([['created_at']]);
    });

    test('two tabs\' expansions merge, and names the dataset no longer has are dropped', async ({ page, context }) => {
        const key = `fridge-sidebar-expanded-${datasetId}`;
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        await page.evaluate(([storageKey]) => localStorage.setItem(storageKey, '["gone"]'), [key]);
        const other = await context.newPage();
        await other.goto(`/datasets/${datasetId}`);
        await expectTotal(other, 30);
        await expandField(page, 'brightness');
        await expandField(other, 'color');
        expect(JSON.parse(await page.evaluate(([storageKey]) => localStorage.getItem(storageKey) || '[]', [key])).sort())
            .toEqual(['brightness', 'color']);
        await page.reload();
        await expectTotal(page, 30);
        await expect(fieldToggle(page, 'brightness')).toHaveAttribute('aria-expanded', 'true');
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'true');
        await other.close();
    });

    test('a promoted range shows its filtered values without waiting for the sidebar answer', async ({ page }) => {
        const held: Route[] = [];
        await page.route(`**/datasets/${otherId}/field-stats`, async route => {
            const body = route.request().postDataJSON();
            if (!body?.fields && JSON.stringify(body?.filters ?? null).includes('"level"')) {
                held.push(route);
                return;
            }
            await route.continue();
        });
        await page.goto(`/datasets/${otherId}`);
        await expectTotal(page, 6);
        await expandField(page, 'brightness');  // promoted
        await expandField(page, 'color');
        await addLevelStage(page, '4');
        await expectTotal(page, 3);
        await expect.poll(() => held.length).toBe(1);
        const sidebar = page.getByTestId('panel-filters');
        await expect(sidebar.getByTestId('facet-status-brightness')).toHaveText('Filtered values');
        await expect(sidebar.getByText('40 - 60', { exact: true })).toBeVisible();
        await expect(sidebar.getByTestId('facet-status-color')).toHaveText('Updating filtered values…');
        for (const route of held.splice(0)) await route.continue();
        await expect(sidebar.getByTestId('facet-status-color')).toHaveText(/Filtered values/);
    });

    test('a facet answer that stops early leaves no field waiting: each one left says it failed', async ({ page }) => {
        // The server's request failed after it started answering: the stream ends after one line.
        await page.route(`**/datasets/${datasetId}/field-stats`, async route => {
            if (!route.request().postDataJSON()?.fields) return route.continue();
            const response = await route.fetch();
            const [first] = (await response.text()).split('\n');
            await route.fulfill({ response, body: `${first}\n` });
        });
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        for (const name of ['brightness', 'color', 'batch']) await expandField(page, name);
        await addLevelStage(page, '28');
        await expectTotal(page, 3);
        const sidebar = page.getByTestId('panel-filters');
        const statuses = ['brightness', 'color', 'batch', 'level'].map(name => sidebar.getByTestId(`facet-status-${name}`));
        await expect.poll(async () => {
            const texts = await Promise.all(statuses.map(status => status.textContent()));
            return texts.filter(text => /Updating filtered values/.test(text ?? '')).length;
        }).toBe(0);
        const failed = await Promise.all(statuses.map(status => status.textContent()));
        expect(failed.filter(text => /filtered values failed/.test(text ?? ''))).toHaveLength(3);  // all but the one line
        await expect(sidebar.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(3);
    });

    test('while the first stage\'s sidebar request runs, no field claims filtered values', async ({ page }) => {
        const held: Route[] = [];
        await page.route(`**/datasets/${datasetId}/field-stats`, async route => {
            const body = route.request().postDataJSON();
            if (!body?.fields && JSON.stringify(body?.filters ?? null).includes('"level"')) {
                held.push(route);
                return;
            }
            await route.continue();
        });
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        await addLevelStage(page, '25');
        await expectTotal(page, 6);
        await expect.poll(() => held.length).toBe(1);
        const sidebar = page.getByTestId('panel-filters');
        // The default view's answer is still shown: its values are not this filter's.
        await expect(sidebar.getByTestId('facet-status-brightness')).toHaveText('Updating filtered values…');
        await expect(sidebar.getByTestId('facet-status-level')).toHaveText('Updating filtered values…');
        await expect(sidebar.getByText('Filtered values', { exact: true })).toHaveCount(0);
        for (const route of held.splice(0)) await route.continue();
        await expect(sidebar.getByTestId('facet-status-brightness')).toHaveText(/Dataset values · expand for filtered values/);
        await expect(sidebar.getByTestId('facet-status-level')).toHaveText(/Filtered values/);
    });

    test('a draft filter keeps its field expanded; collapsing it holds until the page reloads', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        const sidebar = page.getByTestId('panel-filters');
        await expandField(page, 'batch');
        await sidebar.getByRole('checkbox', { name: 'late', exact: true }).check();
        await expect(page.getByText('Unapplied filters')).toBeVisible();
        await expect(sidebar.getByTestId('field-section-batch')).toHaveAttribute('data-filtered', 'true');
        // Collapsing a filtered field is the user's choice for this page...
        await fieldToggle(page, 'batch').click();
        await expect(fieldToggle(page, 'batch')).toHaveAttribute('aria-expanded', 'false');
        await expect(sidebar.getByRole('checkbox', { name: 'late', exact: true })).toHaveCount(0);
        // ...but after a reload the draft still filters, so the field starts expanded with it ticked.
        await page.waitForTimeout(500);  // the draft is saved after 300 ms
        await page.reload();
        await expect(fieldToggle(page, 'batch')).toHaveAttribute('aria-expanded', 'true');
        await expect(sidebar.getByRole('checkbox', { name: 'late', exact: true })).toBeChecked();
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');
    });

    test('the expanded fields are remembered per dataset across reloads', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        await expandField(page, 'brightness');
        await expandField(page, 'color');
        await page.reload();
        await expectTotal(page, 30);
        await expect(fieldToggle(page, 'brightness')).toHaveAttribute('aria-expanded', 'true');
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'true');
        await expect(page.getByTestId('panel-filters').getByRole('checkbox', { name: 'c1', exact: true })).toBeVisible();
        await expect(fieldToggle(page, 'batch')).toHaveAttribute('aria-expanded', 'false');
        await expect(fieldToggle(page, 'level')).toHaveAttribute('aria-expanded', 'false');

        // Another dataset has its own (empty) list; its indexed field still opens by default.
        await page.goto(`/datasets/${otherId}`);
        await expectTotal(page, 6);
        await expect(fieldToggle(page, 'brightness')).toHaveAttribute('aria-expanded', 'true');
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');

        // Collapsing is remembered too.
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'true');
        await fieldToggle(page, 'color').click();
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');
        await page.reload();
        await expectTotal(page, 30);
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');
        await expect(fieldToggle(page, 'brightness')).toHaveAttribute('aria-expanded', 'true');
    });
});

// A dataset of at most 100,000 images: every section starts expanded and, in
// lightning mode with a filter, every field is computed, in one request; an explicit collapse is kept.
test.describe('sidebar field sections of a small dataset', () => {
    let cookie = '';
    let datasetId = 0;
    test.beforeAll(async () => {
        cookie = await adminLogin();
        const name = `e2e-sidebar-sections-small-${Date.now()}`;
        datasetId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name }) })).id;
        await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
            method: 'POST', body: JSON.stringify({ samples: Array.from({ length: 30 }, (_, index) => ({
                filepath: `s3://${name}/${index + 1}.jpg`,
                metadata: { batch: index + 1 > 27 ? 'late' : 'early', brightness: (index + 1) * 10, color: `c${(index + 1) % 3}`, level: index + 1 },
            })) }),
        });
        await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
    });
    test.afterAll(async () => {
        if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
    });

    test('every section opens expanded, and a filter computes every field in one request', async ({ page }) => {
        expect((await apiFetch(cookie, `/datasets/${datasetId}`)).sidebar_expand_all).toBe(true);
        const facets: string[][] = [];
        page.on('request', request => {
            if (request.url().endsWith(`/datasets/${datasetId}/field-stats`) && request.postDataJSON()?.fields) facets.push(request.postDataJSON().fields);
        });
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        const sidebar = page.getByTestId('panel-filters');
        for (const name of ['batch', 'brightness', 'color', 'level']) await expect(fieldToggle(page, name)).toHaveAttribute('aria-expanded', 'true');
        await expect(sidebar.getByRole('checkbox', { name: 'c1', exact: true })).toBeVisible();
        await expect(sidebar.getByText('10 - 300', { exact: true })).toBeVisible();

        await sidebar.getByRole('checkbox', { name: 'late', exact: true }).check();
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 3);
        for (const name of ['brightness', 'color', 'level']) {
            await expect(sidebar.getByTestId(`facet-status-${name}`)).toHaveText(/Filtered values/);
        }
        await expect(sidebar.getByText('280 - 300', { exact: true })).toBeVisible();
        // One request for every field; no section moved when the filter changed.
        expect(facets).toHaveLength(1);
        expect(facets[0]).toEqual(expect.arrayContaining(['batch', 'brightness', 'color', 'level']));
        for (const name of ['batch', 'brightness', 'color', 'level']) await expect(fieldToggle(page, name)).toHaveAttribute('aria-expanded', 'true');
    });

    test('each visit reads the dataset answer again, also after in-app navigation', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        await expect(fieldToggle(page, 'brightness')).toHaveAttribute('aria-expanded', 'true');
        await page.evaluate(() => { (window as unknown as { samePage: boolean }).samePage = true; });
        // The dataset crosses the line (presented so) while the user is elsewhere in the app.
        await asLargeDataset(page, datasetId);
        await page.getByRole('link', { name: '← Back' }).click();
        await page.locator(`a[href="/datasets/${datasetId}"]`).first().click();
        await expectTotal(page, 30);
        await expect(fieldToggle(page, 'brightness')).toHaveAttribute('aria-expanded', 'false');
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');
        // In-app navigation: the page (and its cached answers) never reloaded.
        expect(await page.evaluate(() => (window as unknown as { samePage?: boolean }).samePage)).toBe(true);
    });

    test('a section the user collapses stays collapsed across reloads; expanding it again is kept too', async ({ page }) => {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        const sidebar = page.getByTestId('panel-filters');
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'true');
        await fieldToggle(page, 'color').click();
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');
        await expect(sidebar.getByRole('checkbox', { name: 'c1', exact: true })).toHaveCount(0);
        await page.reload();
        await expectTotal(page, 30);
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');
        await expect(fieldToggle(page, 'brightness')).toHaveAttribute('aria-expanded', 'true');

        // A filter on another field computes the open fields only, and the collapsed one stays shut.
        const facets: string[][] = [];
        page.on('request', request => {
            if (request.url().endsWith(`/datasets/${datasetId}/field-stats`) && request.postDataJSON()?.fields) facets.push(request.postDataJSON().fields);
        });
        await sidebar.getByRole('checkbox', { name: 'late', exact: true }).check();
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 3);
        await expect(sidebar.getByTestId('facet-status-brightness')).toHaveText(/Filtered values/);
        await expect(sidebar.getByTestId('facet-status-color')).toHaveText(/Dataset values · expand for filtered values/);
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');
        expect(facets.flat()).not.toContain('color');

        await fieldToggle(page, 'color').click();
        await expect(sidebar.getByTestId('facet-status-color')).toHaveText(/Filtered values/);
        await page.waitForTimeout(500);  // the applied stage is saved after 300 ms
        await page.reload();
        await expectTotal(page, 3);
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'true');
    });
});
