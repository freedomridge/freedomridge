import { test, expect } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal, appliedTotal } from './helpers';

test.use({ storageState: AUTH_STATE });

// Regression for the datetime "Add Filter" flow, which shipped broken twice:
//   - it defaulted to '=' → an exact-timestamp match returns 0 rows ("the filter does nothing"),
//   - and the pre-filled date was display-only → the value reached the backend empty and no-op'd
//     to the WHOLE dataset.
// Adding a datetime filter and applying the DEFAULT must return a real SUBSET — which proves the
// operator is a range op AND the value is actually committed. A backend-only test can't catch this
// (the bug is the frontend's default operator/value), so it has to drive the UI end-to-end.
test('query builder: a datetime filter defaults to a working range and returns a subset', async ({ page }) => {
    await gotoDataset(page);
    await expectTotal(page, 12);

    await page.getByRole('button', { name: 'Add Filter' }).click();
    await page.getByPlaceholder('Search fields to filter by...').fill('sampled_at');
    await page.locator('[data-radix-popper-content-wrapper]').getByRole('button', { name: /^sampled_at/i }).first().click();

    // The editor opens with a committed default value; apply it as-is (no manual date entry).
    await page.getByRole('button', { name: 'Apply' }).click();

    // The filtered total must SETTLE to a real subset — not 0 (the '=' bug) and not 12 (the
    // empty-value no-op bug).
    //
    // Poll for the settled value rather than waiting for "not 12": the header renders
    // `globalSamplesData?.total || 0`, so while the newly-keyed count query is in flight it reads
    // "0 samples". That transient 0 satisfies "not 12", so the old assertion could return the
    // instant the filter was applied and read the loading state as the answer — which is exactly
    // how this test flaked under a loaded dev server.
    //
    // This still fails on both original bugs: with '=' the total stays 0 forever and the poll
    // times out; with an empty (no-op) value it settles at 12 and the upper bound fails.
    await expect.poll(() => appliedTotal(page), { timeout: 20_000 }).toBeGreaterThan(0);
    const n = await appliedTotal(page);
    expect(n).toBeLessThan(12);
});
