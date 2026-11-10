import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { API, adminLogin, apiFetch } from './api';

// Settings → Primaries (docs/FOLLOWERS.md): this instance follows itself. Its admin adds a follower and shares a
// dataset with it; the follower token goes into Settings → Primaries; the job runner's sync copies the dataset over
// HTTP into a read-only mirror with a badge; stopping following leaves the copy, no longer following.
test.use({ storageState: AUTH_STATE });

test('an instance follows a primary and mirrors the dataset it shares', async ({ page }) => {
    test.slow();  // the job runner queues a sync once a minute
    const cookie = await adminLogin();
    const stamp = Date.now();
    const source = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `e2e-primary-${stamp}` }) });
    await apiFetch(cookie, `/datasets/${source.id}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: [{ filepath: `s3://e2e/${stamp}-a.jpg`, metadata: { crop: 'corn' } }] }),
    });
    const follower = await apiFetch(cookie, '/followers', { method: 'POST', body: JSON.stringify({ name: `e2e-self-${stamp}` }) });
    let mirrorId: number | undefined;
    try {
        await apiFetch(cookie, `/followers/${follower.id}/datasets/${source.id}`, {
            method: 'PUT',
            body: JSON.stringify({ tags: 'both', views: 'primary' }),
        });

        await page.goto('/settings');
        await page.getByTestId('settings-primaries-card').click();
        await expect(page).toHaveURL(/\/settings\/primaries$/);
        await page.getByLabel('Primary address').fill(API);
        await page.getByLabel('Follower token').fill('not-the-token');
        await page.getByRole('button', { name: 'Follow', exact: true }).click();
        await expect(page.getByText('The primary refused that token')).toBeVisible();
        await page.getByLabel('Follower token').fill(follower.token);
        await page.getByRole('button', { name: 'Follow', exact: true }).click();
        const row = page.getByTestId('primary-row').filter({ hasText: API });
        await expect(row).toContainText(`Following as e2e-self-${stamp}`);

        const mirror = row.getByTestId('mirror-row').filter({ hasText: source.name });
        await expect(mirror.getByTestId('mirror-state')).toHaveText('Following', { timeout: 150_000 });
        await expect(mirror).toContainText('1 samples');
        const href = await mirror.getByRole('link').getAttribute('href');
        mirrorId = Number(href!.split('/').pop());

        await mirror.getByRole('link').click();
        await expect(page.getByTestId('mirror-badge')).toHaveText(`Mirror of ${new URL(API).hostname}`);
        // Its samples are the primary's: no tagging here.
        const refused = await fetch(`${API}/datasets/${mirrorId}/samples`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Cookie: cookie },
            body: JSON.stringify({ samples: [{ filepath: `s3://e2e/${stamp}-local.jpg` }] }),
        });
        expect(refused.status).toBe(409);
        expect((await refused.json()).detail).toContain('read-only mirror');
        // Tags sync both ways: a tag edit here is made on the primary.
        const [local] = (await apiFetch(cookie, `/datasets/${mirrorId}/samples?limit=1`)).items;
        const tagged = await apiFetch(cookie, `/datasets/${mirrorId}/samples/bulk-tag`, {
            method: 'POST',
            body: JSON.stringify({ sample_ids: [local.id], add_tags: ['from-the-follower'] }),
        });
        expect(tagged.forwarded_to).toBe(new URL(API).hostname);
        const [there] = (await apiFetch(cookie, `/datasets/${source.id}/samples?limit=1`)).items;
        expect(there.tags).toContain('from-the-follower');

        await page.goto('/settings/primaries');
        await row.getByRole('button', { name: 'Stop following…' }).click();
        await row.getByRole('button', { name: 'Stop following', exact: true }).click();
        await expect(page.getByTestId('primary-row').filter({ hasText: API })).toHaveCount(0);
        // The primary (this instance) was told, so it no longer keeps changes for the follower.
        await expect(page.getByTestId('stopped-notice')).toContainText('The primary knows');
        const left = (await apiFetch(cookie, '/followers')).find((entry: { id: number }) => entry.id === follower.id);
        expect(left.left_at).not.toBeNull();
        expect(left.departures.map((entry: { dataset_id: number }) => entry.dataset_id)).toEqual([source.id]);
        await expect(page.getByText('Copies no longer following')).toBeVisible();
        await expect(page.getByTestId('mirror-row').filter({ hasText: source.name }).getByTestId('mirror-state'))
            .toHaveText('No longer following');
    } finally {
        const listed = await apiFetch(cookie, '/primaries');
        for (const primary of listed.primaries.filter((entry: { url: string }) => entry.url === API)) {
            await apiFetch(cookie, `/primaries/${primary.id}`, { method: 'DELETE' });
        }
        if (mirrorId !== undefined) await apiFetch(cookie, `/datasets/${mirrorId}`, { method: 'DELETE' });
        await apiFetch(cookie, `/followers/${follower.id}`, { method: 'DELETE' });
        await apiFetch(cookie, `/datasets/${source.id}`, { method: 'DELETE' });
    }
});

// After its primary was restored into a new database, a copy is held and asks its admin whether to resume into it.
// A restore can't be made here, so the list the page reads is the held copy's; the answer goes to the server as is.
test('a copy held after its primary was restored asks whether to resume', async ({ page }) => {
    const held = {
        dataset_id: 9001, name: 'coco (lab)', origin_dataset_id: 7, state: 'stopped', tags: 'primary', views: 'primary',
        images: false, copy_total: 0, copy_left: 0, samples: 42000, concept_views: [], last_synced_at: null,
        last_error: 'The primary was restored into a new database: choose on the Primaries page whether this copy resumes',
        retry_after: null, stopped_at: new Date().toISOString(), keep_separate: false, resumable: false,
        restore: {
            origin_name: 'coco', hash_field: 'dhash_8', total: 42000, matched: 41982, matched_hash: 41950,
            only_here: 6, phase: 'ask', decision: null, compared: true, last_error: null, retry_after: null,
        },
    };
    await page.route('**/api/primaries', route => route.fulfill({
        json: {
            primaries: [{
                id: 1, url: 'https://primary.example.com/api', follower_name: 'lab', created_at: new Date().toISOString(),
                last_synced_at: null, last_error: null, mirrors: [held],
            }],
            unlinked_mirrors: [],
        },
    }));
    let decision: unknown;
    await page.route('**/api/primaries/mirrors/9001/restore-decision', async route => {
        decision = route.request().postDataJSON();
        await route.fulfill({ json: { decision: 'resume' } });
    });
    await page.goto('/settings/primaries');
    const row = page.getByTestId('mirror-row').filter({ hasText: 'coco (lab)' });
    await expect(row.getByTestId('mirror-state')).toHaveText('On hold');
    await expect(row.getByTestId('mirror-restore-counts')).toHaveText(
        /41,982 of 42,000 samples in “coco” match it by file path \(41,950 also by image hash\)\./,
    );
    await expect(row.getByTestId('mirror-restore')).toContainText('18 don\'t match: they\'re copied fresh.');
    await expect(row.getByTestId('mirror-restore')).toContainText('6 here that the primary no longer has go.');
    await expect(row.getByTestId('mirror-error')).toHaveCount(0);  // held, not failing
    // Resuming deletes the copy's samples the primary doesn't have: it says how many before it does.
    await row.getByTestId('mirror-restore-resume').click();
    await expect(row.getByTestId('mirror-restore-confirm')).toContainText('deletes 6 samples here');
    expect(decision).toBeUndefined();
    await row.getByTestId('mirror-restore-confirm-resume').click();
    await expect.poll(() => decision).toEqual({ decision: 'resume' });
});
