import { test, expect } from '@playwright/test';
import { AUTH_STATE, expectTotal } from './helpers';
import { apiFetch } from './api';
import { FIELD, POLYGON, WARNING_IN_VIEW, countToasts, expectWarns, lassoDataset } from './stale-lasso-helpers';

// The stale-lasso tests that lay the map out again for real (a reduce, a managed re-fit), in order on one dataset. A fit
// keeps the API busy for seconds, so this file runs in the `fits` project, after every other spec and one file at a
// time (playwright.config.ts). stale-lasso-helpers.ts says what a stale lasso is.
test.use({ storageState: AUTH_STATE });

const { ds, applyPolygonStage, inside, currentLayout } = lassoDataset('e2e-stale-lasso-fits');

test('a lasso saved before the upgrade stays as it is until its map is laid out again, then warns and keeps filtering', async ({ page }) => {
    test.setTimeout(180_000);
    const toasts = await countToasts(page);
    // No layout id: drawn before the upgrade, on the map as it was then.
    await applyPolygonStage(page);
    await expectTotal(page, 30);
    await expect(page.getByTestId('query-stage-chip')).not.toHaveAttribute('data-stale-lasso', 'true');
    expect(await toasts()).toBe(0);

    // The map is laid out again (Run UMAP reduction, or the API).
    await apiFetch(ds.cookie, `/datasets/${ds.id}/embeddings/reduce`, { method: 'POST', body: JSON.stringify({ field_name: FIELD }) });
    expect(await currentLayout()).toBeTruthy();
    const now = await inside();
    await page.reload();
    await expectWarns(page);
    await expect.poll(toasts, { timeout: 15_000 }).toBe(1);
    await expectTotal(page, now);  // still filtering, on the map as it is now
    // Excluded, it is the same lasso: still amber, and no second toast.
    await page.getByTestId('query-stage-chip').click();
    await expectTotal(page, 60 - now);
    await expectWarns(page);
    expect(await toasts()).toBe(1);

    // One drawn on the current layout doesn't warn.
    await applyPolygonStage(page, await currentLayout());
    await expectTotal(page, now);
    await expect(page.getByTestId('query-stage-chip')).not.toHaveAttribute('data-stale-lasso', 'true');
});

test('after a re-fit, a lasso drawn on the map before it warns and keeps filtering, in a view too, until the view is updated', async ({ page }) => {
    test.setTimeout(300_000);
    const toasts = await countToasts(page);
    await page.goto(`/datasets/${ds.id}`);
    await expect(page.getByTestId('embeddings-point-count')).toHaveText(/^60(\s|\/|$).*points/, { timeout: 30_000 });

    // A lasso on the map's current layout, applied as a view stage and saved in a view.
    const before = await currentLayout();
    await applyPolygonStage(page, before);
    await expectTotal(page, await inside());
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(1);
    await expect(page.getByTestId('query-stage-chip')).not.toHaveAttribute('data-stale-lasso', 'true');
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByPlaceholder('Enter view name...').fill('lasso-view');
    await page.getByRole('button', { name: 'Save View' }).click();
    await expect(page.getByText('lasso-view')).toBeVisible();
    await page.getByRole('button', { name: 'Close Views' }).click();

    // Re-fit the map (Run now; the backend's runner fits it for real).
    const schedule = await apiFetch(ds.cookie, `/datasets/${ds.id}/schedules`, {
        method: 'POST', body: JSON.stringify({ job_type: 'map_refit', target: FIELD, enabled: false }),
    });
    await apiFetch(ds.cookie, `/datasets/${ds.id}/schedules/${schedule.id}/run`, { method: 'POST' });
    await expect.poll(async () => {
        const items = (await apiFetch(ds.cookie, `/datasets/${ds.id}/schedules`)).items;
        return items[0]?.last_job?.state;
    }, { timeout: 180_000, intervals: [1_000] }).toBe('succeeded');
    expect(await currentLayout()).not.toBe(before);
    const now = await inside();

    // The lasso is kept and warns once; the grid and the scatter load the new map, filtered by it. Its filters are the
    // view just saved, which its owner may update.
    await page.reload();
    await expectWarns(page, WARNING_IN_VIEW);
    await expect.poll(toasts, { timeout: 15_000 }).toBe(1);
    await expectTotal(page, now);
    await expect(page.getByTestId('map-refitted-at')).toContainText('Map re-fitted on');
    await expect(page.getByText('Failed to load embeddings')).toHaveCount(0);

    // The saved view's lasso is kept and warns the same way when the view is opened.
    await page.getByTestId('query-stage-chip').hover();
    await page.getByTestId('query-stage-chip').getByRole('button').click();  // removed by the user, as only the user does
    await expect(page.getByTestId('query-stage-chip')).toHaveCount(0);
    await expectTotal(page, 60);
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByRole('button', { name: /lasso-view/ }).click();
    await page.getByRole('button', { name: 'Close Views' }).click();
    await expectWarns(page, WARNING_IN_VIEW);
    await expectTotal(page, now);

    // And a lasso drawn on the new layout doesn't warn. Its filters still come from the view, which they now differ
    // from: Update this view saves them in it, and the view opens without the warning.
    const layout = await currentLayout();
    await applyPolygonStage(page, layout, POLYGON, true);
    await expectTotal(page, now);
    await expect(page.getByTestId('query-stage-chip')).not.toHaveAttribute('data-stale-lasso', 'true');
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await expect(page.getByTestId('view-source')).toContainText('“lasso-view”');
    page.once('dialog', dialog => void dialog.accept());
    await page.getByTestId('view-update').click();
    await expect(page.getByTestId('view-source')).toHaveCount(0);
    const views = await apiFetch(ds.cookie, `/datasets/${ds.id}/views`);
    expect(views.find((v: { name: string }) => v.name === 'lasso-view').content.viewStages[0].filter.layout_id).toBe(layout);
    await page.getByRole('button', { name: 'Close Views' }).click();
    await page.getByTestId('query-stage-chip').hover();
    await page.getByTestId('query-stage-chip').getByRole('button').click();
    await expectTotal(page, 60);
    await page.getByRole('button', { name: 'Saved Views' }).click();
    await page.getByRole('button', { name: /lasso-view/ }).click();
    await page.getByRole('button', { name: 'Close Views' }).click();
    await expectTotal(page, now);
    await expect(page.getByTestId('query-stage-chip')).not.toHaveAttribute('data-stale-lasso', 'true');
    expect(await toasts()).toBe(0);  // since the page loaded with the redrawn lasso
});

test('tagging a selection picked under a lasso drawn on an earlier layout says so, with the count', async ({ page }) => {
    // Around every point, so the count doesn't hang on where the map's layout put them.
    const everything = [[-1e6, -1e6], [1e6, -1e6], [1e6, 1e6], [-1e6, 1e6]];
    await applyPolygonStage(page, 'an-earlier-layout', everything);
    await expectWarns(page);
    await expectTotal(page, 60);
    await page.getByTestId('grid-select-all').click();
    await expect(page.getByTestId('grid-selection-count')).toBeVisible({ timeout: 10_000 });
    await page.getByTestId('grid-tag-button').click();
    const warning = page.getByTestId('tag-stale-lasso-warning');
    await expect(warning).toContainText(`earlier layout of the ${FIELD} map`);
    await expect(warning).toContainText('Applying changes 60 samples');
});
