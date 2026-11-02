import { test, expect } from '@playwright/test';
import { AUTH_STATE, expectTotal, expandField } from './helpers';
import { adminLogin, apiFetch } from './api';

// regression (server half): the value typeahead's server search matched only the
// START of a value, so on a field with more distinct values than the cached list holds, "Forest"
// found nothing on "area/Forest_7" ("No matches") although the picker filters by substring.
test.use({ storageState: AUTH_STATE });

let cookie = '';
let dsId = 0;
test.beforeAll(async () => {
    cookie = await adminLogin();
    dsId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-typeahead-${Date.now()}` }) })).id;
    const samples = Array.from({ length: 120 }, (_, i) => ({
        filepath: `s3://e2e-typeahead/t${i}.jpg`,
        metadata: { site: i < 60 ? `area/Forest_${i}` : `area/River_${i}` },
    }));
    await apiFetch(cookie, `/datasets/${dsId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    await apiFetch(cookie, `/datasets/${dsId}/refresh-stats`, { method: 'POST' });
    const schema = await apiFetch(cookie, `/datasets/${dsId}/schema`);
    expect(schema.find((field: { name: string }) => field.name === 'site')).toMatchObject({
        type: 'string', declared_type: 'string', values_complete: false,
    });
});
test.afterAll(async () => {
    if (dsId) await apiFetch(cookie, `/datasets/${dsId}`, { method: 'DELETE' });
});

test('typing text from the middle of a value finds it', async ({ page }) => {
    await page.goto(`/datasets/${dsId}`);
    await expectTotal(page, 120);
    await expandField(page, 'site');
    const input = page.getByTestId('panel-filters').getByPlaceholder('Filter by site...');
    await input.click();
    await input.fill('River_97');
    const dropdown = page.locator('[data-searchable-dropdown]');
    // Each row is the value plus its count; match the value text itself.
    await expect(dropdown.getByText('area/River_97', { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(dropdown.getByText('No matches')).toHaveCount(0);
    await dropdown.getByText('area/River_97', { exact: true }).click();
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 1);
});

test('high-cardinality strings retain contains in the query builder', async ({ page }) => {
    await page.goto(`/datasets/${dsId}`);
    await expectTotal(page, 120);
    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('site');
    await page.locator('[data-radix-popper-content-wrapper]').getByRole('button', { name: /^site/i }).first().click();
    const editor = page.locator('[data-radix-popper-content-wrapper]');
    await editor.locator('select').nth(1).selectOption('contains');
    await editor.getByPlaceholder('Value...').fill('River');
    await editor.getByRole('button', { name: 'Apply', exact: true }).click();
    await expectTotal(page, 60);
});
