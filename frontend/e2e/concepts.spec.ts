import { test, expect, type Locator, type Page } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal, expandField } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset } from './api';

// Embedding "concepts": seed/probe a concept from a selection of example
// images, tune the threshold (live count of what gets cut), apply it as a reversible exclude
// that scopes the whole view. Concept BREADTH (scoring math, exclude/select, probe fit) is
// covered at the API layer by fridge.test_sdk; here we prove the UI modalities reach the
// server and scope the grid.
//
// Fixture: two separable groups — idx 0-5 (== split 'train') on axis 0, idx 6-11 ('val') on
// axis 1. A concept built from train scores train high (= score_max) and val low (= score_min);
// the slider is ranged to [score_min, score_max], so at its MIN it excludes the 6 train (score
// above min) and at its MAX it excludes nothing. Locators are scoped by concept name so the
// tests don't collide on the spec's dataset.
test.use({ storageState: AUTH_STATE });

// Concept scoring runs as dataset jobs, whose end refreshes every page open on the dataset: this spec
// uses its own copy of the fixture, not the shared one other specs are browsing.
let cookie = '';
let datasetId = 0;
test.beforeAll(async () => {
    cookie = await adminLogin();
    datasetId = await createFixtureDataset(cookie, `e2e-concepts-${Date.now()}`);
});
test.afterAll(async () => {
    if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
});

// Set a controlled range input the way React expects (native value setter + input event).
async function setRange(slider: Locator, value: number) {
    await slider.evaluate((el, v) => {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!;
        setter.call(el, String(v));
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }, value);
}

// Select the 'train' group (the example images) and open the Concepts panel.
async function selectTrainAndOpenPanel(page: Page) {
    await gotoDataset(page, datasetId);
    await expectTotal(page, 12);
    await expandField(page, 'split');
    await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'train' }).check();
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 6);
    await page.getByTestId('grid-select-all').click();
    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('concepts');
    await expect(page.getByTestId('concept-create')).toContainText('(6)'); // selection propagated
}

test('concepts: seed from a group, tune, exclude it, grid scopes; persists on reload', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page);

    await page.getByTestId('concept-name-input').fill('seedlike');
    await page.getByTestId('concept-create').click();
    const card = page.getByTestId('concept-card').filter({ hasText: 'seedlike' });
    await expect(card).toBeVisible({ timeout: 10_000 });

    // The slider is ranged to the concept's actual score band; at its max it excludes nothing,
    // at its min it excludes the seeded group (the 6 train samples).
    const slider = card.getByTestId('concept-threshold');
    const lo = Number(await slider.getAttribute('min'));
    const hi = Number(await slider.getAttribute('max'));
    await setRange(slider, hi);
    await expect(card.getByTestId('concept-hidden-count')).toContainText('0 would be hidden');
    await setRange(slider, lo);
    await expect(card.getByTestId('concept-hidden-count')).toContainText('6 would be hidden');

    // Clear the train filter (full 12 again), then apply the exclude → the 6 'val' remain.
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
    await card.getByTestId('concept-apply').click();
    await expectTotal(page, 6);
    await expect(page.getByTestId('query-stage-chip').filter({ hasText: 'seedlike' })).toBeVisible();

    // Reload: the saved concept is still in the library (DB-persisted, panel reopens to it).
    await page.reload();
    await expect(page.getByTestId('concept-card').filter({ hasText: 'seedlike' })).toBeVisible({ timeout: 15_000 });
});

test('concepts: probe (learned boundary) from a selection discriminates the groups', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page);

    await page.getByTestId('concept-kind').selectOption('probe');
    await page.getByTestId('concept-name-input').fill('trainprobe');
    await page.getByTestId('concept-create').click();

    const card = page.getByTestId('concept-card').filter({ hasText: 'trainprobe' });
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card).toContainText('probe'); // kind badge
    // Probe trained on train (group A) vs the rest → at the slider's min it excludes the 6 train.
    const slider = card.getByTestId('concept-threshold');
    await setRange(slider, Number(await slider.getAttribute('min')));
    await expect(card.getByTestId('concept-hidden-count')).toContainText('6 would be hidden', { timeout: 10_000 });
});

test('concepts: ± buttons and arrow keys step the threshold', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page);
    await page.getByTestId('concept-name-input').fill('stepper');
    await page.getByTestId('concept-create').click();
    const card = page.getByTestId('concept-card').filter({ hasText: 'stepper' });
    await expect(card).toBeVisible({ timeout: 10_000 });

    const slider = card.getByTestId('concept-threshold');
    const v0 = Number(await slider.inputValue());
    await card.getByTestId('concept-threshold-down').click(); // step down → lower threshold
    const v1 = Number(await slider.inputValue());
    expect(v1).toBeLessThan(v0);

    await slider.focus();
    await page.keyboard.press('ArrowRight'); // arrow key steps it back up
    expect(Number(await slider.inputValue())).toBeGreaterThan(v1);

    // by-samples mode: stepping is server-computed (async round-trip), still moves the cut
    await card.getByTestId('concept-step-mode-samples').click();
    const v2 = Number(await slider.inputValue());
    await card.getByTestId('concept-threshold-down').click();
    await expect.poll(async () => Number(await slider.inputValue())).not.toBe(v2);
});

test('concepts: marking returns focus to the card so arrows keep working', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page);
    await page.getByTestId('concept-name-input').fill('kbfocus');
    await page.getByTestId('concept-create').click();
    const card = page.getByTestId('concept-card').filter({ hasText: 'kbfocus' });
    await expect(card).toBeVisible({ timeout: 10_000 });

    const item = card.getByTestId('concept-boundary-item').first();
    await expect(item).toBeVisible({ timeout: 10_000 });
    await item.getByTestId('concept-mark-neg').click(); // − ("not the concept")

    // The bug: focus moved to the (transient) band button; the fix returns it to the card,
    // so keyboard stepping keeps working without re-clicking the panel.
    await expect(card).toBeFocused();
    const slider = card.getByTestId('concept-threshold');
    const before = Number(await slider.inputValue());
    await page.keyboard.press('ArrowLeft');
    expect(Number(await slider.inputValue())).toBeLessThan(before);
});

test('concepts: collapse hides the body + delete; expand restores', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page);
    await page.getByTestId('concept-name-input').fill('collapseme');
    await page.getByTestId('concept-create').click();
    const card = page.getByTestId('concept-card').filter({ hasText: 'collapseme' });
    await expect(card.getByTestId('concept-threshold')).toBeVisible({ timeout: 10_000 });
    await expect(card.getByTestId('concept-delete')).toBeVisible();

    await card.getByTestId('concept-collapse-toggle').click();
    await expect(card.getByTestId('concept-threshold')).toHaveCount(0); // body hidden
    await expect(card.getByTestId('concept-delete')).toHaveCount(0); // ✕ hidden when collapsed
    await expect(card).toContainText('collapseme'); // still listed

    await card.getByTestId('concept-collapse-toggle').click();
    await expect(card.getByTestId('concept-threshold')).toBeVisible();
});

test('concepts: show-only flips the applied chip exclude⇄only', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page);
    await page.getByTestId('concept-name-input').fill('showonly');
    await page.getByTestId('concept-create').click();
    const card = page.getByTestId('concept-card').filter({ hasText: 'showonly' });
    await expect(card).toBeVisible({ timeout: 10_000 });

    await card.getByTestId('concept-apply').click();
    const chip = page.getByTestId('query-stage-chip').filter({ hasText: 'showonly' });
    await expect(chip).toContainText('Exclude');
    await expect(card.getByTestId('concept-hidden-count')).toContainText('would be hidden');
    await card.getByTestId('concept-show-only').click();
    await expect(chip).toContainText('Only');
    await expect(card.getByTestId('concept-hidden-count')).toContainText('would be shown'); // mode-aware label
});

test('concepts: delete requires confirmation', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page);
    await page.getByTestId('concept-name-input').fill('delme');
    await page.getByTestId('concept-create').click();
    const card = page.getByTestId('concept-card').filter({ hasText: 'delme' });
    await expect(card).toBeVisible({ timeout: 10_000 });

    await card.getByTestId('concept-delete').click(); // arm
    await expect(card).toContainText('delete?');
    await card.getByTestId('concept-delete-cancel').click(); // cancel → still there
    await expect(card).toBeVisible();

    await card.getByTestId('concept-delete').click();
    await card.getByTestId('concept-delete-confirm').click(); // confirm → gone
    await expect(page.getByTestId('concept-card').filter({ hasText: 'delme' })).toHaveCount(0, { timeout: 10_000 });
});

test('concepts: a grid selection surfaces refine-from-selection controls', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page); // filters train + Select All → a live selection
    await page.getByTestId('concept-name-input').fill('selrefine');
    await page.getByTestId('concept-create').click();
    const card = page.getByTestId('concept-card').filter({ hasText: 'selrefine' });
    await expect(card).toBeVisible({ timeout: 10_000 });
    await expect(card.getByTestId('concept-selection-add')).toBeVisible();
    await expect(card.getByTestId('concept-selection-remove')).toBeVisible();
});

test('concepts: sources view scopes the grid to the defining images', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page); // train filter + Select All → the 6 example images
    await page.getByTestId('concept-name-input').fill('srcview');
    await page.getByTestId('concept-create').click();
    const card = page.getByTestId('concept-card').filter({ hasText: 'srcview' });
    await expect(card).toBeVisible({ timeout: 10_000 });

    // Clear the train filter (full 12 again), then switch the view to "sources" → the grid
    // scopes to the concept's defining images (the 6 seeds), as a removable chip.
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
    await card.getByTestId('concept-mode-sources').click();
    await expectTotal(page, 6);
    await expect(page.getByTestId('query-stage-chip').filter({ hasText: 'srcview' })).toContainText('Sources');
});

test('concepts: re-tuning the cut only previews until you apply it', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page);
    await page.getByTestId('concept-name-input').fill('tunecut');
    await page.getByTestId('concept-create').click();
    const card = page.getByTestId('concept-card').filter({ hasText: 'tunecut' });
    await expect(card).toBeVisible({ timeout: 10_000 });

    const slider = card.getByTestId('concept-threshold');
    const lo = Number(await slider.getAttribute('min'));
    const hi = Number(await slider.getAttribute('max'));

    // Clear the train filter (full 12), tune to the min cut, and apply exclude → 6 remain.
    await page.getByRole('button', { name: 'Clear All' }).click();
    await expectTotal(page, 12);
    await setRange(slider, lo);
    await card.getByTestId('concept-apply').click();
    await expectTotal(page, 6);

    // Re-tune toward the max: the grid must NOT move until we explicitly apply the new cut.
    await setRange(slider, hi);
    await expect(card.getByTestId('concept-hidden-count')).toContainText('0 would be hidden');
    await expectTotal(page, 6); // unchanged — tuning only previews

    // "reset" snaps the slider back to the applied cut and clears the apply/reset affordances.
    await card.getByTestId('concept-reset-cut').click();
    expect(Number(await slider.inputValue())).toBeLessThan(hi);
    await expect(card.getByTestId('concept-apply-cut')).toHaveCount(0);

    // Re-tune again and publish it this time → the new cut (hide nothing) goes live.
    await setRange(slider, hi);
    await card.getByTestId('concept-apply-cut').click();
    await expectTotal(page, 12);
});

test('concepts: "score N new" is hidden when every embedded image is already scored', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenPanel(page);
    await page.getByTestId('concept-name-input').fill('fullyscored');
    await page.getByTestId('concept-create').click();
    const card = page.getByTestId('concept-card').filter({ hasText: 'fullyscored' });
    await expect(card).toBeVisible({ timeout: 10_000 });
    // The fixture's samples are all embedded and get scored at create → nothing new to score,
    // so the rescore affordance stays hidden (no more always-on "rescore").
    await expect(card.getByTestId('concept-rescore')).toHaveCount(0);
});
