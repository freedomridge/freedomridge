import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch, deleteDatasetAndWait } from './api';

// Jobs panel → Schedules: add a map re-fit schedule, switch it off, edit it, run it now (a real re-fit
// by the backend's own runner, in its memory-capped child), see the map panel say when the map was
// re-fitted, and delete the schedule.
test.use({ storageState: AUTH_STATE });

const FIELD = 'e2e_map';
let cookie = '';
let dsId = 0;

test.beforeAll(async () => {
    cookie = await adminLogin();
    dsId = (await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-schedules-${Date.now()}` }) })).id;
    const samples = Array.from({ length: 60 }, (_, i) => ({
        filepath: `s3://e2e-schedules/s${i}.jpg`,
        metadata: { n: i },
        embeddings: { [FIELD]: Array.from({ length: 8 }, (_, d) => Math.sin(i * 0.37 + d) + (i % 3) * 2) },
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${dsId}/samples`, { method: 'POST', body: JSON.stringify({ samples }) });
    // An existing map, as a legacy fit leaves it: every point placed, ranks built.
    const coordinates = (ids as number[]).map((sampleId, i) => ({ sample_id: sampleId, x: -100 - i, y: i % 7 }));
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/coordinates?field_name=${FIELD}`, {
        method: 'POST', body: JSON.stringify(coordinates),
    });
    await apiFetch(cookie, `/datasets/${dsId}/embeddings/recompute-viz-rank?field_name=${FIELD}&full=true`, { method: 'POST' });
});

test.afterAll(async () => {
    if (dsId) await deleteDatasetAndWait(cookie, dsId, 120_000);
});

test('add, switch off, edit, run now and delete a map re-fit schedule', async ({ page }) => {
    test.setTimeout(240_000);
    await page.goto(`/datasets/${dsId}`);
    await page.getByTestId('open-job-history').click();
    const dialog = page.getByTestId('job-history-dialog');
    await expect(dialog.getByTestId('job-history')).toBeVisible();  // History stays the first tab
    await dialog.getByTestId('jobs-schedules-tab').click();
    const schedules = dialog.getByTestId('dataset-schedules');
    await expect(schedules).toContainText('No schedules yet.');

    await schedules.getByTestId('add-schedule').click();
    const form = schedules.getByTestId('schedule-form');
    await expect(form.getByTestId('schedule-target')).toHaveValue(FIELD);
    await expect(form.getByTestId('schedule-pause-note')).toContainText(
        'about a minute per million points on the map',
    );
    await expect(form.getByLabel('Start window in hours')).toHaveValue('3');
    // Defaults: weekly on Sunday at 03:00, only when >= 20,000 new samples, no minimum gap (available).
    await expect(form.getByLabel('Weekly on')).toBeChecked();
    await expect(form.getByLabel('Weekday')).toHaveValue('0');
    await expect(form.getByLabel('Time')).toHaveValue('03:00');
    await expect(form.getByLabel('Minimum new samples')).toHaveValue('20000');
    await expect(form.getByLabel('Limit how often it re-fits')).not.toBeChecked();
    await expect(form.getByLabel('Minimum days between runs')).toBeDisabled();
    await form.getByTestId('schedule-save').click();
    const row = schedules.getByTestId('schedule-row');
    await expect(row).toHaveCount(1);
    await expect(row).toContainText(`Map re-fit · ${FIELD}`);
    await expect(row.getByTestId('schedule-rule')).toHaveText('Weekly on Sunday at 03:00 · only when ≥ 20,000 new samples');
    await expect(row.getByTestId('schedule-last-next')).toContainText(/Last: never · Next check: \w+ \d+, 03:00/);
    await expect(row.getByTestId('schedule-toggle')).toHaveText('On');

    await row.getByTestId('schedule-toggle').click();
    await expect(row.getByTestId('schedule-toggle')).toHaveText('Off');
    await expect(row.getByTestId('schedule-last-next')).toContainText('Next check: off');

    await row.getByTestId('schedule-edit').click();
    const edit = row.getByTestId('schedule-form');
    await edit.getByLabel('Daily').check();
    await edit.getByLabel('Time').fill('04:15');
    await edit.getByLabel('Minimum new samples').fill('500');
    await edit.getByLabel('Limit how often it re-fits').check();
    await edit.getByLabel('Minimum days between runs').fill('7');
    await edit.getByTestId('schedule-save').click();
    await expect(row.getByTestId('schedule-rule')).toHaveText(
        'Daily at 04:15 · only when ≥ 500 new samples · at most every 7 days',
    );
    await expect(row.getByTestId('schedule-toggle')).toHaveText('Off');  // editing keeps it off
    await row.getByTestId('schedule-toggle').click();
    await expect(row.getByTestId('schedule-toggle')).toHaveText('On');

    // Run now ignores the conditions: the backend's runner re-fits the map for real.
    await row.getByTestId('schedule-run-now').click();
    await expect(row).toContainText('Queued; it shows under Running.');
    await expect.poll(async () => {
        const items = (await apiFetch(cookie, `/datasets/${dsId}/schedules`)).items;
        return items[0]?.last_job?.state;
    }, { timeout: 180_000, intervals: [1_000] }).toBe('succeeded');
    await dialog.getByTestId('jobs-history-tab').click();
    const refit = dialog.getByTestId('job-history-item').filter({ hasText: 'Re-fitting the map' }).first();
    await expect(refit).toContainText('Succeeded');
    await expect(refit.getByTestId('map-refit-outcome')).toHaveText('Map re-fitted: 60 points');
    await dialog.getByTestId('jobs-schedules-tab').click();
    await expect(row.getByTestId('schedule-last-next')).toContainText('(succeeded)');
    // With a 7-day gap, the next check is 04:15 on the seventh calendar day after the re-fit, not tomorrow.
    // Days count in the schedule's zone, as the backend counts them: 7 x 24 h lands a day off around a
    // clock change. The form wrote the schedule in the browser's zone; its dates show in that zone.
    const listed = (await apiFetch(cookie, `/datasets/${dsId}/schedules`)).items[0];
    const state = await apiFetch(cookie, `/datasets/${dsId}/embeddings/map-state?field_name=${FIELD}`);
    const [y, m, d] = new Intl.DateTimeFormat('en-CA', { timeZone: listed.timezone })
        .format(new Date(state.refitted_at)).split('-').map(Number);
    const inAWeek = new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', month: 'short', day: 'numeric' })
        .format(new Date(Date.UTC(y, m - 1, d + 7)));
    await expect(row.getByTestId('schedule-last-next')).toContainText(`Next check: ${inAWeek}, 04:15`);
    expect(listed.last_check).toBeNull();  // no "at most every 7 days" skips recorded
    await expect(row).toContainText('Map re-fitted');
    await page.keyboard.press('Escape');

    expect(state.managed).toBe(true);
    expect(state.points_at_refit).toBe(60);
    await page.reload();
    await expect(page.getByTestId('map-refitted-at')).toContainText('Map re-fitted on', { timeout: 30_000 });

    await page.getByTestId('open-job-history').click();
    await dialog.getByTestId('jobs-schedules-tab').click();
    await row.getByTestId('schedule-delete').click();
    await row.getByTestId('schedule-delete-confirm').click();
    await expect(schedules).toContainText('No schedules yet.');
});
