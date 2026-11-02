import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// Index advisor (administrators): Settings → System status reviews the indexes no dataset owns, and
// Dataset Settings → Indexes reviews one dataset's. A review is a real read-only job run by the
// backend's own runner; the suggestion table is also checked against a fixed review.
test.use({ storageState: AUTH_STATE });

let cookie = '';
let dsId = 0;

test.beforeAll(async () => {
    cookie = await adminLogin();
    dsId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-index-advisor-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` }) })).id;
    const samples = Array.from({ length: 12 }, (_, i) => ({
        filepath: `s3://e2e-index-advisor/s${i}.jpg`,
        metadata: { country: i % 2 ? 'US' : 'CA', score: i },
    }));
    await apiFetch(cookie, `/datasets/${dsId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
});

test.afterAll(async () => {
    await apiFetch(cookie, '/admin/index-advisor/schedule', {
        method: 'PUT',
        body: JSON.stringify({ enabled: false, frequency: 'weekly', weekday: 0, time: '03:00' }),
    });
    if (dsId) await deleteDatasetAndWait(cookie, dsId, 120_000);
});

async function openIndexes(page: Page, datasetId: number) {
    await page.goto(`/datasets/${datasetId}`);
    await page.getByRole('button', { name: 'Dataset Settings', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: 'Indexes', exact: true }).click();
    return dialog;
}

test('system status reviews the core indexes and finds the overlapping metadata GIN indexes', async ({ page }) => {
    test.setTimeout(120_000);
    await page.goto('/settings/status');
    const advisor = page.getByTestId('status-indexes').getByTestId('index-advisor-system');
    await expect(advisor).toBeVisible();
    await advisor.getByTestId('index-review-now').click();
    await expect(advisor.getByTestId('index-review-status')).toContainText('Last reviewed', { timeout: 60_000 });
    // The live case: GIN jsonb_path_ops on samples.metadata next to GIN jsonb_ops on the same column.
    const overlap = advisor.locator('[data-testid="index-advisor-overlap"][data-index="idx_samples_metadata_gin"]');
    await expect(overlap).toContainText('Would be replaced by: idx_samples_metadata ');
    await expect(overlap).toContainText('supports every operator this one does');
    await expect(advisor.getByTestId('index-advisor-needed')).toContainText('indexes are needed');
    await expect(advisor.getByTestId('index-advisor-possibly-unused')).toBeVisible();

    // The weekly system-wide review.
    const schedule = advisor.getByTestId('index-advisor-schedule');
    await schedule.getByLabel('Weekly review').selectOption('on');
    await schedule.getByLabel('Review weekday').selectOption('3');
    await schedule.getByLabel('Review time').fill('04:30');
    await schedule.getByRole('button', { name: 'Save' }).click();
    await expect(schedule).toContainText('next ');
    const saved = await apiFetch(cookie, '/admin/index-advisor');
    expect(saved.schedule).toMatchObject({ enabled: true, weekday: 3, time: '04:30' });
});

test('dataset settings reviews the dataset and lists the review in its job history', async ({ page }) => {
    test.setTimeout(120_000);
    const dialog = await openIndexes(page, dsId);
    const advisor = dialog.getByTestId('index-advisor-dataset');
    await expect(advisor).toContainText('Not reviewed yet');
    await expect(advisor.getByTestId('index-advisor-schedule')).toContainText('Weekly review: off');
    await advisor.getByTestId('index-review-now').click();
    await expect(advisor.getByTestId('index-review-status')).toContainText('Last reviewed', { timeout: 60_000 });
    await expect(advisor.getByTestId('index-advisor-suggested')).toContainText('Suggested indexes');
    await expect(advisor.getByTestId('index-advisor-possibly-unused')).toBeVisible();
    await expect(advisor.getByTestId('index-advisor-overlapping')).toBeVisible();
    await page.keyboard.press('Escape');

    // A review owns no dataset lane, but it is listed with the dataset's jobs.
    await page.getByTestId('open-job-history').click();
    const history = page.getByTestId('job-history-dialog');
    await expect(history.getByTestId('job-history-item').filter({ hasText: 'Reviewing indexes' }).first()).toContainText('Succeeded');
});

test('a suggestion and a possibly unused index are explained with their numbers', async ({ page }) => {
    const now = new Date();
    const monthAgo = new Date(now.getTime() - 30 * 86_400_000).toISOString();
    await page.route(`**/api/datasets/${dsId}/index-advisor`, route => route.fulfill({
        json: {
            review: {
                scope: 'dataset',
                dataset_id: dsId,
                reviewed_at: now.toISOString(),
                window_days: 7,
                stats_since: monthAgo,
                enough_history: true,
                telemetry_since: monthAgo.slice(0, 10),
                suggested: [{
                    field: 'country',
                    type: 'string',
                    reason: 'slow',
                    usages: { filter: { uses: 40, mean_ms: 3050, p50_ms: 4096, p95_ms: 8192, reference_ms: 50, slower: true } },
                    excess_seconds: 120,
                    build_seconds: 60,
                    pays_back: true,
                }],
                build_seconds: 60,
                possibly_unused: [{
                    name: `idx_samples_meta_${dsId}_old`,
                    table: 'samples',
                    kind: 'field',
                    method: 'btree',
                    dataset_id: dsId,
                    field: 'old',
                    definition: '',
                    size_bytes: 120 * 1024 * 1024,
                    scans: 0,
                    last_scan: null,
                    observed_since: monthAgo,
                    valid: true,
                    needed: [],
                    invalid: false,
                    idle_days: 30,
                }],
                too_new: [],
                overlapping: [],
                building: [],
            },
            review_job: { id: 'j', finished_at: now.toISOString() },
            active_job: null,
            last_failure: null,
            schedule: null,
            window_days: 7,
        },
    }));
    const dialog = await openIndexes(page, dsId);
    const advisor = dialog.getByTestId('index-advisor-dataset');
    const suggestion = advisor.getByTestId('index-advisor-suggestion');
    await expect(suggestion).toContainText('country');
    await expect(suggestion).toContainText('filter 40');
    await expect(suggestion).toContainText('4.1 s, slowest 8.2 s');
    await expect(suggestion).toContainText('50 ms');
    await expect(suggestion).toContainText('2 min');
    await expect(suggestion).toContainText('a build takes ~60 s');
    const unused = advisor.getByTestId('index-advisor-unused');
    await expect(unused).toContainText(`idx_samples_meta_${dsId}_old`);
    await expect(unused).toContainText('120.0 MB');
    await expect(unused).toContainText('never');
    await expect(advisor.getByTestId('index-advisor-not-enough-history')).toHaveCount(0);
});

test('an empty possibly-unused list says why, without claiming every index was used', async ({ page }) => {
    const now = new Date();
    const monthAgo = new Date(now.getTime() - 30 * 86_400_000).toISOString();
    await page.route('**/api/admin/index-advisor', route => route.fulfill({
        json: {
            review: {
                scope: 'system', dataset_id: null, reviewed_at: now.toISOString(), window_days: 7, idle_days_required: 14,
                stats_since: monthAgo, history_since: monthAgo, enough_history: true, possibly_unused: [],
                too_new: [{ name: 'idx_recent', table: 'samples', kind: 'other', method: 'btree', dataset_id: null, field: null,
                    definition: '', size_bytes: 8192, scans: 0, last_scan: null, observed_since: now.toISOString(), valid: true, needed: [] }],
                overlapping: [], building: [], needed: [], missing_core: [],
            },
            review_job: { id: 'j', finished_at: now.toISOString() }, active_job: null, last_failure: null,
            schedule: { enabled: false, frequency: 'weekly', weekday: 0, time: '06:00', timezone: 'UTC', next_run_at: null, last_run_at: null },
            window_days: 7,
        },
    }));
    await page.goto('/settings/status');
    const advisor = page.getByTestId('status-indexes').getByTestId('index-advisor-system');
    await expect(advisor.getByTestId('index-advisor-none-unused')).toHaveText(
        'No index here has gone 14 days without use; 1 are watched for too short a time to judge (needed indexes are never listed).',
    );
    await expect(advisor.getByTestId('index-advisor-too-new')).toContainText('idx_recent');
});

test('a vector index is listed but never offered for removal', async ({ page }) => {
    const now = new Date();
    const monthAgo = new Date(now.getTime() - 30 * 86_400_000).toISOString();
    const entry = (name: string, kind: string, method: string) => ({
        name, table: kind === 'ann' ? 'embeddings' : 'samples', kind, method, dataset_id: null, field: null, definition: `CREATE INDEX ${name}`,
        size_bytes: 27 * 1024 ** 3, scans: 0, last_scan: null, observed_since: monthAgo, valid: true, needed: [], invalid: false, idle_days: 30,
    });
    await page.route('**/api/admin/index-advisor', route => route.fulfill({
        json: {
            review: {
                scope: 'system', dataset_id: null, reviewed_at: now.toISOString(), window_days: 7, idle_days_required: 14,
                stats_since: monthAgo, history_since: monthAgo, enough_history: true,
                possibly_unused: [entry('hnsw_emb_dinov3', 'ann', 'hnsw'), entry('idx_hand_made', 'other', 'btree')],
                too_new: [], overlapping: [], building: [], needed: [], missing_core: [],
            },
            review_job: { id: 'j', finished_at: now.toISOString() }, active_job: null, last_failure: null, removed: [],
            schedule: { enabled: false, frequency: 'weekly', weekday: 0, time: '06:00', timezone: 'UTC', next_run_at: null, last_run_at: null },
            window_days: 7,
        },
    }));
    await page.goto('/settings/status');
    const advisor = page.getByTestId('status-indexes').getByTestId('index-advisor-system');
    const vector = advisor.locator('[data-testid="index-advisor-unused"][data-index="hnsw_emb_dinov3"]');
    await expect(vector.getByTestId('index-remove-managed')).toHaveText('Managed by the embedding index controls');
    await expect(vector.getByTestId('index-remove')).toHaveCount(0);
    await expect(advisor.locator('[data-testid="index-advisor-unused"][data-index="idx_hand_made"]').getByTestId('index-remove')).toHaveText('Remove');
});

test('a restore another administrator already did refreshes the list instead of offering Restore again', async ({ page }) => {
    const now = new Date();
    const monthAgo = new Date(now.getTime() - 30 * 86_400_000).toISOString();
    let restoredElsewhere = false;
    const removal = {
        id: 1, index_name: 'idx_gone', table: 'samples', kind: 'other', field: null, definition: 'CREATE INDEX idx_gone ON public.samples USING btree (id)',
        size_bytes: 8192, scans: 0, last_scan: null, observed_since: monthAgo, reason: 'unused', removed_by: 'admin',
        removed_at: now.toISOString(), dropped_at: now.toISOString(), state: 'removed', restore_failed: null,
    };
    await page.route('**/api/admin/index-advisor', route => route.fulfill({
        json: {
            review: {
                scope: 'system', dataset_id: null, reviewed_at: now.toISOString(), window_days: 7, idle_days_required: 14,
                stats_since: monthAgo, history_since: monthAgo, enough_history: true,
                possibly_unused: [], too_new: [], overlapping: [], building: [], needed: [], missing_core: [],
            },
            review_job: { id: 'j', finished_at: now.toISOString() }, active_job: null, last_failure: null,
            removed: restoredElsewhere ? [] : [removal], pending_actions: [],
            schedule: { enabled: false, frequency: 'weekly', weekday: 0, time: '06:00', timezone: 'UTC', next_run_at: null, last_run_at: null },
            window_days: 7,
        },
    }));
    await page.route('**/api/admin/indexes/idx_gone/restore**', route => {
        restoredElsewhere = true;  // someone else's restore finished first
        return route.fulfill({ status: 404, json: { detail: 'idx_gone was not removed by the index advisor' } });
    });
    await page.goto('/settings/status');
    const advisor = page.getByTestId('status-indexes').getByTestId('index-advisor-system');
    await advisor.locator('[data-testid="index-advisor-removal"][data-index="idx_gone"]').getByTestId('index-restore').click();
    await expect(advisor.getByTestId('index-advisor-removal')).toHaveCount(0);
});

test('only administrators see the index review', async ({ page }) => {
    await page.route('**/api/auth/me', route => route.fulfill({ json: { id: 99, username: 'power', role: 'power_user' } }));
    const dialog = await openIndexes(page, dsId);
    await expect(dialog.getByText('Indexed fields use database indexes for fast filtering')).toBeVisible();
    await expect(dialog.getByTestId('index-advisor-dataset')).toHaveCount(0);
});

test('remove the overlapping core GIN index with one confirmed click, then restore it', async ({ page }) => {
    test.setTimeout(180_000);
    const before = await apiFetch(cookie, '/admin/index-advisor');
    await page.goto('/settings/status');
    const advisor = page.getByTestId('status-indexes').getByTestId('index-advisor-system');
    if (!before.review) {
        await advisor.getByTestId('index-review-now').click();
        await expect(advisor.getByTestId('index-review-status')).toContainText('Last reviewed', { timeout: 60_000 });
    }
    const overlap = advisor.locator('[data-testid="index-advisor-overlap"][data-index="idx_samples_metadata_gin"]');
    await overlap.getByTestId('index-remove').click();
    const confirm = overlap.getByTestId('index-remove-confirm');
    await expect(confirm).toContainText('DROP INDEX CONCURRENTLY');
    await expect(confirm).toContainText('A core index: new installations still create it.');
    await confirm.getByTestId('index-remove-confirm-button').click();
    const removal = advisor.locator('[data-testid="index-advisor-removal"][data-index="idx_samples_metadata_gin"]');
    await expect(removal.getByTestId('index-removal-state')).toHaveText('Removed', { timeout: 60_000 });
    await expect(removal).toContainText('CREATE INDEX idx_samples_metadata_gin ON public.samples USING gin (metadata jsonb_path_ops)');
    await expect(removal).toContainText('by admin');

    await removal.getByTestId('index-restore').click();
    await expect(removal).toHaveCount(0, { timeout: 120_000 });
    // Back exactly as it was: a review started after the restore finds the same overlap again.
    const restoredAt = Date.now();
    await apiFetch(cookie, '/admin/index-advisor/review?wait=false', { method: 'POST' });
    await expect.poll(async () => {
        const state = await apiFetch(cookie, '/admin/index-advisor');
        if (!state.review || new Date(state.review.reviewed_at).getTime() < restoredAt) return 'waiting';
        return state.review.overlapping.some((pair: { redundant: { name: string } }) => pair.redundant.name === 'idx_samples_metadata_gin')
            ? 'overlap found' : 'missing';
    }, { timeout: 60_000, intervals: [1_000] }).toBe('overlap found');
});

test('index a suggested field, and remove a field index from the Indexes tab online', async ({ page }) => {
    test.setTimeout(180_000);
    const fieldSuggestion = {
        review: {
            scope: 'dataset', dataset_id: dsId, reviewed_at: new Date().toISOString(), window_days: 7,
            stats_since: new Date(Date.now() - 30 * 86_400_000).toISOString(), enough_history: true, telemetry_since: null,
            suggested: [{
                field: 'country', type: 'string', reason: 'slow', excess_seconds: 120, build_seconds: null, pays_back: null,
                usages: { filter: { uses: 40, mean_ms: 3050, p50_ms: 4096, p95_ms: 8192, reference_ms: 50, slower: true } },
            }],
            build_seconds: null, possibly_unused: [], too_new: [], overlapping: [], building: [],
        },
        review_job: null, active_job: null, last_failure: null, schedule: null, removed: [], window_days: 7,
    };
    await page.route(`**/api/datasets/${dsId}/index-advisor`, route => route.fulfill({ json: fieldSuggestion }));
    const dialog = await openIndexes(page, dsId);
    const suggestion = dialog.getByTestId('index-advisor-dataset').getByTestId('index-advisor-suggestion');
    await suggestion.getByTestId('index-field').click();
    await expect(suggestion.getByTestId('index-field')).toHaveText('Indexed', { timeout: 60_000 });
    const schema = await apiFetch(cookie, `/datasets/${dsId}/schema`);
    expect(schema.find((field: { name: string }) => field.name === 'country').is_promoted).toBe(true);

    // The toggle's remove-index is an online drop job now; it reports done when the job ends.
    await page.unroute(`**/api/datasets/${dsId}/index-advisor`);
    const row = dialog.locator('tr', { hasText: 'country' }).first();
    await row.getByTitle('Click to remove index').click();
    await expect(dialog.getByText('Done — "country" demoted')).toBeVisible({ timeout: 60_000 });
    const after = await apiFetch(cookie, `/datasets/${dsId}/schema`);
    expect(after.find((field: { name: string }) => field.name === 'country').is_promoted).toBeFalsy();
});
