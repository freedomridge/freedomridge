import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE, gotoDataset, expectTotal, expandField } from './helpers';
import { adminLogin, apiFetch, createFixtureDataset } from './api';

// Long actions (concept scoring, index builds, duplicate rebuilds) answer within five seconds
// with the final result, or 202 {job_id} while a server job continues. The 12-sample fixture
// always finishes inside five seconds, so this spec asks the real endpoint for its immediate
// 202 (`wait=false`, as the SDK's wait=False does): the job, its history and its result are all
// real. Only the page's polling of that job is held, to observe the waiting state.
test.use({ storageState: AUTH_STATE });

// Concept scoring runs as dataset jobs, whose end refreshes every page open on the dataset: this spec
// uses its own copy of the fixture, not the shared one other specs are browsing.
let cookie = '';
let datasetId = 0;
test.beforeAll(async () => {
    cookie = await adminLogin();
    datasetId = await createFixtureDataset(cookie, `e2e-long-action-${Date.now()}`);
});
test.afterAll(async () => {
    if (datasetId) await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
});

async function selectTrainAndOpenConcepts(page: Page) {
    await gotoDataset(page, datasetId);
    await expectTotal(page, 12);
    await expandField(page, 'split');  // sections start collapsed
    await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'train' }).check();
    await page.getByTestId('apply-filters').click();
    await expectTotal(page, 6);
    await page.getByTestId('grid-select-all').click();
    await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('concepts');
    await expect(page.getByTestId('concept-create')).toContainText('(6)');
}

test('a long concept action is followed through Jobs and refreshes the panel when it finishes', async ({ page }) => {
    test.setTimeout(120_000);
    await selectTrainAndOpenConcepts(page);
    const name = `longjob${Date.now()}`;

    // The real create endpoint, answering 202 at once.
    await page.route('**/api/datasets/*/concepts', route => {
        if (route.request().method() !== 'POST') return route.continue();
        return route.continue({ url: `${route.request().url()}?wait=false` });
    });
    let release: () => void = () => {};
    const released = new Promise<void>(resolve => { release = resolve; });
    let followed = 0;
    await page.route(/\/api\/jobs\/[0-9a-f-]{36}$/, async route => {
        followed += 1;
        await released;
        await route.continue();
    });

    const accepted = page.waitForResponse(response =>
        /\/api\/datasets\/\d+\/concepts\?wait=false$/.test(response.url()) && response.request().method() === 'POST');
    await page.getByTestId('concept-name-input').fill(name);
    // A 202 refreshes the Jobs indicator at once instead of at its next five-second poll.
    const indicatorRefresh = page.waitForRequest(request => /\/jobs\/active/.test(request.url()));
    await page.getByTestId('concept-create').click();
    const response = await accepted;
    expect(response.status()).toBe(202);
    const { job_id: jobId } = await response.json();
    await indicatorRefresh;

    // The page follows the job: the action stays in progress, nothing is reported finished yet.
    await expect(page.getByTestId('concept-create')).toContainText('Creating…');
    await expect.poll(() => followed).toBeGreaterThan(0);

    // Job history lists the real job: its name, its submitter and how it ended.
    await page.getByTestId('open-job-history').click();
    const history = page.getByTestId('job-history-dialog');
    const item = history.locator(`[data-job-id="${jobId}"]`);
    await expect(item).toContainText('Scoring concept', { timeout: 30_000 });
    await expect(item).toContainText('Succeeded', { timeout: 30_000 });
    await expect(item).toContainText('by admin');
    await page.keyboard.press('Escape');

    // When the page sees the job finish, the panel shows the scored concept and the button resets.
    release();
    const card = page.getByTestId('concept-card').filter({ hasText: name });
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('concept-create')).not.toContainText('Creating…');
    await expect(card.getByTestId('concept-scoring')).toHaveCount(0);
    const slider = card.getByTestId('concept-threshold');
    await expect(slider).toBeVisible();
    // Scored: the preview counts real scores (the six seeds score highest).
    await slider.evaluate((el) => {
        const input = el as HTMLInputElement;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!;
        setter.call(input, input.min);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await expect(card.getByTestId('concept-hidden-count')).toContainText('6 would be hidden');

    await card.getByTestId('concept-delete').click();
    await card.getByTestId('concept-delete-confirm').click();
    await expect(card).toHaveCount(0);
});

test('a view refused while its concept is rescored says so and recovers by itself when the job ends', async ({ page }) => {
    test.setTimeout(90_000);
    await gotoDataset(page, datasetId);
    await expectTotal(page, 12);
    // The server's answer for any query filtering by a concept whose scores are being rebuilt
    // (backend: require_concept_scores → 409), here for every filtered read the page makes:
    // the header count, the grid's count and pages, and the sidebar's statistics.
    const refusal = 'Concept "water" is being rescored; use it again when its scoring job finishes';
    const refused = /\/api\/datasets\/\d+\/(samples\/query|field-stats)$/;
    await page.route(refused, route => route.fulfill({ status: 409, json: { detail: refusal } }));
    await expandField(page, 'split');  // sections start collapsed
    await page.getByTestId('panel-filters').getByRole('checkbox', { name: 'train' }).check();
    await page.getByTestId('apply-filters').click();
    const unavailable = page.getByTestId('grid-unavailable');
    await expect(unavailable).toContainText('is being rescored', { timeout: 15_000 });
    await expect(page.getByTestId('total-samples')).toHaveText('Waiting for concept scoring…');
    await expect(page.getByText('Loading samples...')).toHaveCount(0);
    // The job ends: no click, no reload. Every refused query is retried and recovers.
    const sidebarRecovered = page.waitForResponse(
        response => /\/field-stats$/.test(response.url()) && response.status() === 200,
        { timeout: 30_000 },
    );
    await page.unroute(refused);
    await expect(page.getByTestId('total-samples')).toHaveText(/^\s*6 (samples|images)\s*$/, { timeout: 30_000 });
    await expect(unavailable).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByTestId('grid-cell')).toHaveCount(6, { timeout: 30_000 });
    await sidebarRecovered;
});
