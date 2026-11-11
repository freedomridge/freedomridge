import { test, expect } from '@playwright/test';
import { readState } from './api';
import { AUTH_STATE, gotoDataset } from './helpers';

// A mirror's own fields (`@local.<name>`, docs/FOLLOWERS.md). The e2e server has no primary to follow, so the
// fixture dataset is shown as a following mirror whose tags come from the primary only, with one field of its own on
// its first sample.
test.use({ storageState: AUTH_STATE });

test.afterEach(async ({ page }) => {
    await page.unrouteAll({ behavior: 'ignoreErrors' });
});

async function showAsMirror(page: import('@playwright/test').Page, datasetId: number) {
    await page.route(new RegExp(`/api/datasets/${datasetId}$`), async route => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...(await response.json()), mirror: {
            state: 'following', primary_url: 'https://primary.example.org/api', origin_dataset_id: 9,
            tags: 'primary', views: 'primary', concept_views: [],
        } } });
    });
    await page.route(new RegExp(`/api/datasets/${datasetId}/field-stats$`), async route => {
        if (route.request().postDataJSON()?.stream) return route.continue();  // a facet request: as it is
        const response = await route.fetch();
        const fields = await response.json();
        await route.fulfill({ response, json: [
            ...fields.map((f: { name: string }) => ({ ...f, owner: 'primary' })),
            { name: '@local.weeds', type: 'number', origin: 'local', owner: 'self', min: 3, max: 3, sample_count: 1 },
        ] });
    });
    await page.route(new RegExp(`/api/datasets/${datasetId}/samples/query$`), async route => {
        const response = await route.fetch();
        const page_ = await response.json();
        const items = (page_.items ?? []).map((item: object, index: number) => (
            index === 0 ? { ...item, local_metadata: { weeds: 3 }, local_tags: ['checked'] } : { ...item, local_metadata: {}, local_tags: [] }
        ));
        await route.fulfill({ response, json: { ...page_, items } });
    });
}

test("a mirror's own fields: their own sidebar group, the sample view, and this server's tags", async ({ page }) => {
    const { datasetId } = readState();
    await showAsMirror(page, datasetId);
    await gotoDataset(page);

    // The sidebar: this server's fields first, then the primary's.
    const sidebar = page.locator('aside').filter({ hasText: 'Filters' }).first();
    await expect(sidebar.getByText('On this server')).toBeVisible();
    await expect(sidebar.getByText('From the primary')).toBeVisible();
    const order = await sidebar.evaluate(el => {
        const text = el.textContent ?? '';
        return [text.indexOf('On this server'), text.indexOf('@local.weeds'), text.indexOf('From the primary')];
    });
    expect(order[0]).toBeLessThan(order[1]);
    expect(order[1]).toBeLessThan(order[2]);

    // The first sample's view lists them apart, by their full names.
    await page.getByTestId('grid-cell').first().click();
    const own = page.getByTestId('own-fields');
    await expect(own).toContainText('On this server');
    await expect(own).toContainText('@local.weeds');
    await expect(own).toContainText('@local.tags');
    await page.keyboard.press('Escape');

    // Tagging on this mirror: the primary's tags aren't editable here (from the primary only), this server's are.
    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toBeVisible({ timeout: 10_000 });
    await page.getByTestId('grid-tag-button').click();
    const primary = page.getByRole('button', { name: "The primary's tags" });
    const mine = page.getByRole('button', { name: "This server's tags" });
    await expect(primary).toBeDisabled();
    await expect(mine).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByRole('heading', { name: /This server's tags/ })).toBeVisible();
});
