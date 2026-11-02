import { test, expect } from '@playwright/test';
import { AUTH_STATE, expectTotal } from './helpers';
import { adminLogin, apiFetch } from './api';

// The grid's partial-coverage warning named fields with missing values
// but gave no way to see WHICH samples or to close it. Clicking it now makes the view one ordinary
// filter: `<field> not_exists` per gap (embeddings.<f> / hashes.<f> / location), OR'ed, its rules
// edited and removed in the stage editor like any other; ✕ hides the warning until a reload.
test.use({ storageState: AUTH_STATE });

let cookie = '';
let dsId = 0;
test.beforeAll(async () => {
    cookie = await adminLogin();
    dsId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-coverage-${Date.now()}` }) })).id;
    // 10 samples: 7 hashed (c7-c9 not), 8 located (2 not), 6 with 2x2 patch embeddings (c3-c6 not).
    // The gaps barely overlap, so their union (7) and each one alone (4 / 3 / 2) differ.
    const samples = Array.from({ length: 10 }, (_, i) => ({
        filepath: `s3://e2e-coverage/c${i}.jpg`,
        metadata: { n: i },
        ...(i < 7 ? { hashes: { e2e_phash: (i + 1).toString(16).padStart(16, '0') } } : {}),
        ...(i < 8 ? { location: { type: 'Point', coordinates: [-100.0 + i * 0.01, 40.0 + i * 0.01] } } : {}),
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${dsId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    // Patch embeddings are stored one row per tile ('e2e_patch_patch_2x2_r0c1'); coverage folds them
    // into one 'e2e_patch_patch_2x2' gap, and the filter must fold them the same way.
    const tiles = (ids as number[]).filter((_, i) => i < 3 || i > 6).flatMap(sampleId => [0, 1].flatMap(r => [0, 1].map(c => ({
        sample_id: sampleId, vector: [0.1, 0.2, 0.3, 0.4], field_name: 'e2e_patch', patch_scale: '2x2', patch_row: r, patch_col: c,
    }))));
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/patches`, { method: 'POST', body: JSON.stringify(tiles) });
});
test.afterAll(async () => {
    if (dsId) await apiFetch(cookie, `/datasets/${dsId}`, { method: 'DELETE' });
});

test('the warning opens one filter of every sample behind it', async ({ page }) => {
    await page.goto(`/datasets/${dsId}`);
    await expectTotal(page, 10);
    const warning = page.getByTestId('grid-coverage-warning');
    await expect(warning).toContainText('4 samples missing e2e_patch_patch_2x2 +2 more');  // + e2e_phash, location
    await warning.click();
    await expect(page.getByTestId('query-stage-chip')).toHaveText(
        ['embeddings.e2e_patch_patch_2x2: no value or hashes.e2e_phash: no value or location: no value']);
    await expectTotal(page, 7);  // c3-c9: missing ANY of them
});

test('its rules edit like any other filter: remove one, switch to exists, change the field', async ({ page }) => {
    await page.goto(`/datasets/${dsId}`);
    await expectTotal(page, 10);
    await page.getByTestId('grid-coverage-warning').click();
    await expectTotal(page, 7);
    const chip = page.getByTestId('query-stage-chip');
    const rules = page.getByTestId('filter-rule');
    const apply = page.getByRole('button', { name: 'Apply' });

    await chip.click();
    await expect(rules).toHaveCount(3);
    await rules.first().hover();
    await rules.first().getByTitle('Remove rule').click();
    await apply.click();
    await expect(chip).toHaveText('hashes.e2e_phash: no value or location: no value');
    await expectTotal(page, 3);  // c7-c9

    await chip.click();
    await rules.first().locator('select').nth(1).selectOption('exists');
    await apply.click();
    await expect(chip).toHaveText('hashes.e2e_phash: has a value or location: no value');
    await expectTotal(page, 9);  // hashed c0-c6, or no location c8-c9

    await chip.click();
    await rules.nth(1).locator('select').first().selectOption('embeddings.e2e_patch_patch_2x2');
    await apply.click();
    await expect(chip).toHaveText('hashes.e2e_phash: has a value or embeddings.e2e_patch_patch_2x2: no value');
    await expectTotal(page, 7);  // c0-c6
});

test('closing the warning hides it in every grid panel until the page reloads', async ({ page }) => {
    await page.goto(`/datasets/${dsId}`);
    await expectTotal(page, 10);
    await page.getByTestId('panel-type-select').last().selectOption('grid');  // a second grid panel
    await expect(page.getByTestId('grid-coverage-warning')).toHaveCount(2);
    await page.getByTestId('grid-coverage-dismiss').first().click();
    await expect(page.getByTestId('grid-coverage-warning')).toHaveCount(0);
    await expectTotal(page, 10);  // closing it changes nothing about the view
    await page.reload();
    await expect(page.getByTestId('grid-coverage-warning').first()).toBeVisible();
});
