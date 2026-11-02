import { test, expect, type Page, type Route } from '@playwright/test';
import { adminLogin, apiFetch } from './api';
import { AUTH_STATE, asLargeDataset, expectTotal, expandField, fieldToggle, fulfillFieldStats } from './helpers';

test.use({ storageState: AUTH_STATE, viewport: { width: 1600, height: 1100 } });

// Lightning mode: with a filter applied, the sidebar shows every field at
// once with its dataset-wide values, and a field's filtered values are computed while its section is
// expanded. These are a large dataset's sections, which start collapsed (a small dataset's all start
// expanded: sidebar-sections.spec.ts).
test.describe('sidebar facets on open', () => {
    let cookie = '';
    let datasetId = 0;
    test.beforeAll(async () => {
        cookie = await adminLogin();
        datasetId = (await apiFetch(cookie, '/datasets', {
            method: 'POST', body: JSON.stringify({ name: `e2e-sidebar-on-open-${Date.now()}` }),
        })).id;
        await apiFetch(cookie, `/datasets/${datasetId}/samples`, {
            method: 'POST', body: JSON.stringify({ samples: Array.from({ length: 30 }, (_, index) => ({
                filepath: `s3://sidebar-on-open/${index + 1}.jpg`,
                metadata: {
                    batch: index + 1 > 27 ? 'late' : 'early', brightness: (index + 1) * 10, color: `c${(index + 1) % 3}`, level: index + 1,
                },
            })) }),
        });
        await apiFetch(cookie, `/datasets/${datasetId}/refresh-stats`, { method: 'POST' });
    });
    test.afterAll(async () => {
        if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
    });
    test.beforeEach(async ({ context }) => {
        await asLargeDataset(context, datasetId);
    });

    async function applyLate(page: Page) {
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        await expandField(page, 'batch');
        await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'late', exact: true }).check();
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 3);
        return page.getByTestId('panel-filters');
    }

    test('renders every field at once and fills in an expanded field', async ({ page }) => {
        // Hold every filtered-values request until the test releases it.
        const held: Route[] = [];
        const opened: Array<{ fields: string[]; filters: unknown }> = [];
        await page.route(`**/datasets/${datasetId}/field-stats`, async route => {
            const body = route.request().postDataJSON();
            if (body?.fields) {
                opened.push(body);
                held.push(route);
                return;
            }
            await route.continue();
        });
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        const sidebar = page.getByTestId('panel-filters');
        await expandField(page, 'batch');
        await sidebar.getByRole('checkbox', { name: 'late', exact: true }).check();
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 3);

        // Every field is there with its dataset values; only the expanded (and filtered) batch asks for
        // its filtered values. The collapsed fields ask for nothing.
        const brightness = sidebar.getByTestId('facet-status-brightness');
        await expect(brightness).toHaveAttribute('data-provenance', 'pending');
        await expect(brightness).toHaveText(/Dataset values · expand for filtered values/);
        await expect(sidebar.getByTestId('facet-status-color')).toHaveAttribute('data-provenance', 'pending');
        await expect.poll(() => opened.map(body => body.fields)).toEqual([['batch']]);

        // Expanding brightness shows its inputs with the dataset values, and asks for its filtered values only.
        await expect(fieldToggle(page, 'brightness')).toHaveAttribute('aria-expanded', 'false');
        await fieldToggle(page, 'brightness').click();
        await expect(fieldToggle(page, 'brightness')).toHaveAttribute('aria-expanded', 'true');
        await expect(brightness).toHaveText(/Updating filtered values/);
        await expect.poll(() => opened.length).toBe(2);
        expect(opened[1].fields).toEqual(['brightness']);
        expect(JSON.stringify(opened[1].filters)).toContain('"late"');
        await expect(sidebar.getByText('10 - 300', { exact: true })).toBeVisible();
        for (const route of held.splice(0)) await route.continue();
        await expect(brightness).toHaveAttribute('data-provenance', 'exact');
        await expect(brightness).toHaveText(/Filtered values/);
        await expect(sidebar.getByText('280 - 300', { exact: true })).toBeVisible();
        // Other fields were not computed and still say so.
        await expect(sidebar.getByTestId('facet-status-color')).toHaveText(/Dataset values · expand for filtered values/);
        await expect(fieldToggle(page, 'color')).toHaveAttribute('aria-expanded', 'false');
        for (const color of ['c0', 'c1', 'c2']) {
            await expect(sidebar.getByRole('checkbox', { name: color, exact: true })).toHaveCount(0);
        }

        // Collapsing hides the inputs; expanding again shows the finished result without asking again.
        await fieldToggle(page, 'brightness').click();
        await expect(sidebar.getByText('280 - 300', { exact: true })).toHaveCount(0);
        await expect(brightness).toHaveText(/Dataset values · expand for filtered values/);
        await fieldToggle(page, 'brightness').click();
        await expect(sidebar.getByText('280 - 300', { exact: true })).toBeVisible();
        await expect(brightness).toHaveText(/Filtered values/);
        expect(opened).toHaveLength(2);
    });

    test('lightning off computes every field for the filter at once', async ({ page }) => {
        const opened: string[][] = [];
        page.on('request', request => {
            if (request.url().endsWith(`/datasets/${datasetId}/field-stats`) && request.postDataJSON()?.fields) {
                opened.push(request.postDataJSON().fields);
            }
        });
        await page.goto(`/datasets/${datasetId}`);
        await expectTotal(page, 30);
        const sidebar = page.getByTestId('panel-filters');
        await expandField(page, 'batch');
        await sidebar.getByRole('checkbox', { name: 'late', exact: true }).check();
        await page.getByTestId('apply-filters').click();
        await expectTotal(page, 3);
        await page.getByTitle('Fast mode: ON').click();
        await expect(sidebar.getByTestId('facet-status-brightness')).toHaveCount(0);
        // Sections still collapse; an expanded one already has the filtered values.
        await expect(fieldToggle(page, 'brightness')).toHaveAttribute('aria-expanded', 'false');
        await expandField(page, 'brightness');
        await expect(sidebar.getByText('280 - 300', { exact: true })).toBeVisible();
        await expect(sidebar.getByTestId('facet-status-brightness')).toHaveCount(0);
        // Lightning off never asks for one field's filtered values (only batch did, before the switch).
        expect(opened.every(fields => fields.length === 1 && fields[0] === 'batch')).toBe(true);
    });

    test('an opened field follows changes to the field made elsewhere', async ({ page }) => {
        const sidebar = await applyLate(page);
        const level = sidebar.getByTestId('facet-status-level');
        await expandField(page, 'level');
        await expect(level).toHaveText(/Filtered values/);
        await expect(sidebar.getByText('28 - 30', { exact: true })).toBeVisible();
        // Show level as a dropdown in the dataset settings: the opened field refetches with the sidebar.
        await page.getByRole('button', { name: 'Dataset Settings', exact: true }).click();
        const dialog = page.getByRole('dialog');
        await dialog.getByRole('button', { name: 'Indexes', exact: true }).click();
        await dialog.locator('tr', { hasText: 'level' }).getByRole('combobox').selectOption('enum');
        await dialog.getByRole('button', { name: 'Close', exact: true }).click();
        for (const value of ['28', '29', '30']) {
            await expect(sidebar.getByRole('checkbox', { name: value, exact: true })).toBeVisible();
        }
        await expect(sidebar.getByRole('checkbox', { name: '27', exact: true })).toHaveCount(0);
        await expect(sidebar.getByText('28 - 30', { exact: true })).toHaveCount(0);
        await expect(level).toHaveText(/Filtered values/);
    });

    test('says why a field keeps its dataset values, and how to get filtered ones', async ({ page }) => {
        // The lightning gate keeps color dataset-wide for this (stand-in broad) filter.
        await page.route(`**/datasets/${datasetId}/field-stats`, async route => {
            if (!route.request().postDataJSON()?.fields) return route.continue();
            await fulfillFieldStats(route, field => ({ ...field, provenance: 'cached', values: ['c0', 'c1', 'c2'] }));
        });
        const sidebar = await applyLate(page);
        const color = sidebar.getByTestId('facet-status-color');
        await expandField(page, 'color');
        await expect(color).toHaveText(/Dataset values · not recomputed for this filter/);
        await expect(color.locator('span')).toHaveAttribute('title', /Promote the field, or turn fast mode off/);
        // media_type cannot be promoted: no promote advice.
        const mediaType = sidebar.getByTestId('facet-status-media_type');
        await expect(mediaType).toHaveText('Dataset values');
        await expect(mediaType.locator('span')).toHaveAttribute('title', /Turn fast mode off for filtered values/);
    });

    test('a failed refresh of an opened field shows the dataset values', async ({ page }) => {
        let calls = 0;
        await page.route(`**/datasets/${datasetId}/field-stats`, async route => {
            if (!route.request().postDataJSON()?.fields?.includes('brightness')) return route.continue();
            calls += 1;
            if (calls > 1) return route.fulfill({ status: 500, json: { detail: 'boom' } });
            // An answer that ran out of time but carried values: the retry below fails.
            await fulfillFieldStats(route, field => ({ ...field, provenance: 'out_of_budget' }));
        });
        const sidebar = await applyLate(page);
        const brightness = sidebar.getByTestId('facet-status-brightness');
        await expandField(page, 'brightness');
        await expect(brightness).toHaveText(/filtered values took too long/);
        await brightness.getByRole('button', { name: 'Retry', exact: true }).click();
        await expect(brightness).toHaveText(/Dataset values · filtered values failed/);
        await expect(sidebar.getByText('10 - 300', { exact: true })).toBeVisible();
        await expect(sidebar.getByText('280 - 300', { exact: true })).toHaveCount(0);
    });
});
