import { test, expect, type Page } from '@playwright/test';
import { AUTH_STATE } from './helpers';
import { adminLogin, apiFetch } from './api';

// Settings → System status (admins): where an admin goes after an ingest 507 ("not enough free
// disk"), plus a banner on every page while the database disk is low. The real disk is whatever
// the runner has, so the low / paused / restart-needed states are faked at the API.
const GB = 1024 ** 3;

function fakeStatus(level: 'ok' | 'low' | 'paused', freeGB: number) {
    return {
        checked_at: new Date().toISOString(),
        database_disk: { total_bytes: 172 * GB, free_bytes: freeGB * GB, level, low_below_bytes: 5 * GB, ingest_paused_below_bytes: GB },
        cache_disks: [
            {
                folders: [{ label: 'S3 image cache', path: '/tmp/freedomridge-s3-cache' }],
                total_bytes: 172 * GB,
                free_bytes: freeGB * GB,
                level: freeGB < 5 ? 'low' : 'ok',
            },
        ],
        database: { reachable: true, size_bytes: 140 * GB, pending_migrations: [], unknown_migrations: [], stale_functions: [] },
        workers: { available: true, server: { online: 3, last_seen: new Date().toISOString() }, compute: { online: 0, last_seen: null } },
        server: {
            version: '1.0.0',
            started_at: new Date().toISOString(),
            running: { commit: 'aaaaaaa', subject: 'the code it started on', committed_at: '2026-10-11T00:00:00+00:00', backend_tree: 't1' },
            checkout: { commit: 'bbbbbbb', subject: 'a merge since', committed_at: '2026-10-12T00:00:00+00:00', backend_tree: 't2' },
            restart_needed: true,
        },
    };
}

const HOUR = 3_600_000;
const versions = (server: string, schema: string, applied: number) => ({ server_version: server, schema_version: schema, migrations_applied: applied });

// What backend/system_status.py reads from FRIDGE_BACKUP_DIR (scripts/backup_local.py's folder).
function fakeBackups(level: 'ok' | 'overdue' | 'failed' | 'none', overrides: Record<string, unknown> = {}) {
    const latest = level === 'none' ? null : {
        file: 'freedomridge_20261019T055351Z.sql.gz',
        created_at: new Date(Date.now() - (level === 'overdue' ? 40 : 13) * HOUR).toISOString(),
        size_bytes: 20.04e9,
        dump_seconds: 1019,
        verify_seconds: 205,
        tables: 30,
        ...versions('16.13 (Debian 16.13-1.pgdg12+1)', '0002_b.sql', 48),
    };
    return {
        level,
        folder: '/mnt/data/freedomridge_backups',
        overdue_after_hours: 36,
        latest,
        last_failure: level === 'failed'
            ? { failed_at: new Date(Date.now() - HOUR).toISOString(), error: 'BackupError: free space 3 GB, need 40 GB' }
            : null,
        kept: { count: latest ? 2 : 0, total_bytes: latest ? 40.08e9 : 0, oldest_created_at: null },
        unverified_dumps: 0,
        disk: { total_bytes: 8e12, free_bytes: 2e12 },
        ...(latest ? { current: versions('16.13', '0002_b.sql', 48), same_schema: true, same_postgres_major: true } : {}),
        ...overrides,
    };
}

async function fakeDisk(page: Page, level: 'ok' | 'low' | 'paused', freeGB: number) {
    await page.unroute('**/api/admin/status');
    await page.route('**/api/admin/status', route => route.fulfill({ json: fakeStatus(level, freeGB) }));
}

test.describe('as an admin', () => {
    test.use({ storageState: AUTH_STATE });

    test('worker health labels slots, warns for missing heartbeats and recovers without a permanent banner', async ({ page }) => {
        let online = 0;
        await page.route('**/api/admin/status', route => {
            const response = fakeStatus('ok', 50);
            response.workers.server.online = online;
            return route.fulfill({ json: response });
        });
        await page.goto('/settings/status');
        await expect(page.getByTestId('status-workers-badge')).toHaveText('OFFLINE');
        await expect(page.getByTestId('status-workers')).toContainText('queued jobs remain saved');
        online = 3;
        await page.getByRole('button', { name: 'Refresh', exact: true }).click();
        await expect(page.getByTestId('status-workers-badge')).toHaveText('ONLINE');
        await expect(page.getByTestId('status-workers')).toContainText('3 server execution slots online');
        await expect(page.getByTestId('status-workers')).toContainText('not runner processes');
        await expect(page.getByTestId('status-workers')).not.toContainText('No server heartbeat');
        await expect(page.getByTestId('low-disk-banner')).toHaveCount(0);
    });

    test('the Backups card shows the latest verified backup and says what is wrong when it is not', async ({ page }) => {
        let backups: unknown = fakeBackups('ok', { unverified_dumps: 2 });
        await page.route('**/api/admin/status', route => route.fulfill({ json: { ...fakeStatus('ok', 50), backups } }));
        const card = page.getByTestId('status-backups');
        const badge = page.getByTestId('status-backups-badge');
        const check = async (next: unknown) => {
            backups = next;
            await page.getByRole('button', { name: 'Refresh', exact: true }).click();
        };

        await page.goto('/settings/status');
        await expect(badge).toHaveText('OK');
        await expect(page.getByTestId('status-backups-latest')).toContainText('20.0 GB · took 20 min · read back, 30 tables, every row count matched');
        await check(fakeBackups('ok', { latest: { ...fakeBackups('ok').latest, dump_seconds: 1, verify_seconds: 0 } }));
        await expect(page.getByTestId('status-backups-latest')).toContainText('took 1 s');
        await check(fakeBackups('ok', { unverified_dumps: 2 }));
        await expect(page.getByTestId('status-backups-version')).toHaveText('Schema 0002, the same as the database now · Postgres 16.13');
        await expect(card).toContainText('/mnt/data/freedomridge_backups · keeping 2 verified (40.1 GB) · 2000.0 GB free of 8000.0 GB');
        await expect(card).toContainText('Also in this folder: 2 dumps without a usable manifest');
        await expect(page.getByTestId('status-backups-failure')).toHaveCount(0);

        await check(fakeBackups('failed'));
        await expect(badge).toHaveText('FAILED');
        await expect(page.getByTestId('status-backups-failure')).toContainText('BackupError: free space 3 GB, need 40 GB. The cron log has the details.');
        await expect(page.getByTestId('status-backups-latest')).toBeVisible();  // the last good one is still there

        await check(fakeBackups('overdue'));
        await expect(badge).toHaveText('OVERDUE');
        await expect(card).toContainText('No verified backup in the last 36 hours, 1.5 times the schedule.');

        await check(fakeBackups('none'));
        await expect(badge).toHaveText('NO BACKUP');
        await expect(card).toContainText('No verified backup in this folder yet.');
        await expect(page.getByTestId('status-backups-version')).toHaveCount(0);

        const version = page.getByTestId('status-backups-version');
        await check(fakeBackups('ok', { current: versions('17.0', '0004_d.sql', 50), same_schema: false, same_postgres_major: false }));
        await expect(version).toHaveText(
            'Schema 0002; the database is now at 0004, so run scripts/apply_migrations.sh after a restore · taken on Postgres 16.13, but this server runs 17.0',
        );
        await check(fakeBackups('ok', { current: versions('16.13', '0001_a.sql', 47), same_schema: false }));
        await expect(version).toHaveText('Schema 0002, newer than the database (0001): restore it only with code at least as new · Postgres 16.13');
        await check(fakeBackups('ok', { current: versions('16.13', '0002_b.sql', 50), same_schema: false }));
        await expect(version).toHaveText('Schema 0002 with 48 migrations recorded; the database has 50 · Postgres 16.13');

        await check({ level: 'unreadable', folder: '/mnt/data/freedomridge_backups', error: 'FileNotFoundError: No such file or directory' });
        await expect(badge).toHaveText("CAN'T READ");
        await expect(card).toContainText('FileNotFoundError: No such file or directory. If the backup drive isn\'t mounted, the nightly backup fails too.');

        await check({ level: 'not_configured', folder: null });
        await expect(badge).toHaveText('NOT SET UP');
        await expect(card).toContainText('Set FRIDGE_BACKUP_DIR in .env');

        await check({ level: 'something_new', folder: '/x' });  // a newer backend: the rest of the page still renders
        await expect(badge).toHaveText('SOMETHING_NEW');

        await check(undefined);  // a backend from before the card
        await expect(card).toHaveCount(0);
        await expect(page.getByTestId('status-database-badge')).toHaveText('OK');  // and the page didn't crash
    });

    test('an admin sets the backup schedule and backs up now from the card', async ({ page }) => {
        const nextRun = new Date(Date.now() + 10 * HOUR).toISOString();
        let schedule: Record<string, unknown> = { interval_hours: null, start_hour: 2, keep: 7, timing_changed_at: new Date().toISOString(), next_run_at: null };
        let activeJob: unknown = null;
        const saves: unknown[] = [];
        let runs = 0;
        await page.route('**/api/admin/status', route => route.fulfill({
            json: { ...fakeStatus('ok', 50), backups: fakeBackups('ok', { schedule, active_job: activeJob }) },
        }));
        await page.route('**/api/admin/backups/schedule', route => {
            const body = route.request().postDataJSON();
            saves.push(body);
            schedule = { ...body, timing_changed_at: new Date().toISOString(), next_run_at: body.interval_hours ? nextRun : null };
            return route.fulfill({ json: schedule });
        });
        let jobLists = 0;
        await page.route('**/api/admin/jobs**', route => {
            jobLists += 1;
            return route.fulfill({ json: { items: [], next_cursor: null } });
        });
        await page.route('**/api/admin/backups/run', route => {
            runs += 1;
            activeJob = { id: 'j1', state: 'running', created_at: new Date().toISOString(), started_at: new Date().toISOString(), cancel_requested: false };
            return route.fulfill({ status: 202, json: { job_id: 'j1', state: 'queued', started: true } });
        });

        await page.goto('/settings/status');
        const controls = page.getByTestId('status-backups-schedule');
        await expect(page.getByTestId('status-backups-next')).toHaveText('No scheduled backups.');
        const save = controls.getByRole('button', { name: 'Save', exact: true });
        await expect(save).toBeDisabled();  // nothing changed yet

        await controls.getByLabel('Backup schedule').selectOption('every');
        await expect(controls.getByLabel('Backup interval unit')).toHaveValue('days');
        await expect(controls.getByLabel('Backup start hour')).toHaveValue('2');
        await controls.getByLabel('Backup interval', { exact: true }).fill('12');
        await controls.getByLabel('Backup interval unit').selectOption('hours');
        await controls.getByLabel('Backup start hour').selectOption('3');
        await controls.getByLabel('Backups to keep').fill('5');
        await save.click();
        await expect.poll(() => saves).toEqual([{ interval_hours: 12, start_hour: 3, keep: 5 }]);
        await expect(page.getByTestId('status-backups-next')).toContainText('Next backup:');
        await expect(save).toBeDisabled();

        await controls.getByLabel('Backups to keep').fill('0');
        await expect(save).toBeDisabled();  // 1-100
        await controls.getByLabel('Backups to keep').fill('5');

        await controls.getByLabel('Backup schedule').selectOption('off');
        await save.click();
        await expect.poll(() => saves.length).toBe(2);
        expect(saves[1]).toEqual({ interval_hours: null, start_hour: 3, keep: 5 });
        await expect(page.getByTestId('status-backups-next')).toHaveText('No scheduled backups.');

        const jobListLoads = () => jobLists;
        const before = jobLists;
        await controls.getByRole('button', { name: 'Back up now' }).click();
        await expect.poll(() => runs).toBe(1);
        await expect(page.getByTestId('status-backups-active')).toContainText('Backing up now, started');
        await expect(controls.getByRole('button', { name: 'Back up now' })).toBeDisabled();
        await expect.poll(jobListLoads).toBeGreaterThan(before);  // Background jobs shows the new job

        // The card follows the backup to its end without a Refresh, and the jobs list follows it too.
        activeJob = null;
        const running = jobLists;
        await expect(page.getByTestId('status-backups-active')).toHaveCount(0, { timeout: 10_000 });
        await expect(controls.getByRole('button', { name: 'Back up now' })).toBeEnabled();
        await expect.poll(jobListLoads).toBeGreaterThan(running);
    });

    test('a saved schedule loads into the controls', async ({ page }) => {
        const schedule = { interval_hours: 24, start_hour: 2, keep: 7, timing_changed_at: new Date().toISOString(), next_run_at: new Date(Date.now() + 10 * HOUR).toISOString() };
        await page.route('**/api/admin/status', route => route.fulfill({
            json: { ...fakeStatus('ok', 50), backups: fakeBackups('ok', { schedule, active_job: null }) },
        }));
        await page.goto('/settings/status');
        const controls = page.getByTestId('status-backups-schedule');
        await expect(controls.getByLabel('Backup schedule')).toHaveValue('every');
        await expect(controls.getByLabel('Backup interval', { exact: true })).toHaveValue('1');
        await expect(controls.getByLabel('Backup interval unit')).toHaveValue('days');
        await expect(controls.getByLabel('Backup start hour')).toHaveValue('2');
        await expect(controls.getByLabel('Backups to keep')).toHaveValue('7');
        const save = controls.getByRole('button', { name: 'Save', exact: true });
        await expect(save).toBeDisabled();
        await controls.getByLabel('Backups to keep').fill('101');
        await expect(save).toBeDisabled();  // 1-100
        await controls.getByLabel('Backups to keep').fill('8');
        await expect(save).toBeEnabled();
    });

    test('unavailable worker health is not reported as healthy', async ({ page }) => {
        await page.route('**/api/admin/status', route => route.fulfill({
            json: { ...fakeStatus('ok', 50), workers: { available: false, error: 'migration missing' } },
        }));
        await page.goto('/settings/status');
        await expect(page.getByTestId('status-workers-badge')).toHaveText('UNKNOWN');
        await expect(page.getByTestId('status-workers')).toContainText('migration missing');
    });

    test('a database newer than the code says so instead of asking for a migration', async ({ page }) => {
        const database = { reachable: true, size_bytes: 140 * GB, pending_migrations: [], unknown_migrations: ['0003_newer.sql'], stale_functions: [] };
        await page.route('**/api/admin/status', route => route.fulfill({ json: { ...fakeStatus('ok', 50), database } }));
        await page.goto('/settings/status');
        await expect(page.getByTestId('status-database-badge')).toHaveText('NEWER THAN CODE');
        await expect(page.getByTestId('status-database')).toContainText('schema newer than this version');
        await expect(page.getByTestId('status-database')).toContainText("Migrations this version doesn't have: 0003_newer.sql.");
        await expect(page.getByTestId('status-database')).not.toContainText('apply_migrations.sh');
    });

    test('Settings → System status shows the disk, database, cache disk and server', async ({ page }) => {
        await page.goto('/settings');
        await page.getByTestId('settings-system-status').click();
        await expect(page).toHaveURL(/\/settings\/status$/);

        const disk = page.getByTestId('status-database-disk');
        await expect(disk).toBeVisible({ timeout: 15_000 });
        await expect(page.getByTestId('status-database-disk-badge')).toHaveText(/^(OK|LOW|INGEST PAUSED|UNKNOWN)$/);
        await expect(disk).toContainText(/free of .* used|can't be read here/);
        await expect(disk).toContainText('refused at about 1.00 GB free');

        await expect(page.getByTestId('status-database-badge')).toHaveText('OK');
        await expect(page.getByTestId('status-database')).toContainText(/Reachable · .* · schema up to date/);
        await expect(page.getByTestId('status-backups-badge')).toHaveText(/^(OK|OVERDUE|FAILED|NO BACKUP|CAN'T READ|NOT SET UP)$/);
        await expect(page.getByTestId('status-cache-disk').first()).toContainText('S3 image cache');
        await expect(page.getByTestId('status-server')).toContainText(/Version \d+\.\d+\.\d+/);
        const actual = await (await page.request.get('/api/admin/status')).json();
        await expect(page.getByTestId('status-workers')).toContainText(`${actual.workers.server.online} server execution slots online`);
    });

    test('the low-disk banner warns on every page, links to the status page, and goes away with room', async ({ page }) => {
        await fakeDisk(page, 'low', 3);
        await page.goto('/');
        const banner = page.getByTestId('low-disk-banner');
        await expect(banner).toHaveAttribute('data-level', 'low');
        await expect(banner).toContainText('Database disk is low: 3.00 GB free. Ingest stops at about 1.00 GB free.');

        await banner.getByRole('link', { name: 'System status' }).click();
        await expect(page).toHaveURL(/\/settings\/status$/);
        await expect(page.getByTestId('status-database-disk-badge')).toHaveText('LOW');
        await expect(page.getByTestId('status-database-disk')).toContainText('3.00 GB free of 172.0 GB (98% used)');
        await expect(page.getByTestId('status-server-badge')).toHaveText('RESTART NEEDED');
        await expect(page.getByTestId('status-server')).toContainText('The checkout is now at bbbbbbb — a merge since.');

        await fakeDisk(page, 'paused', 0.5);
        await page.reload();
        await expect(banner).toHaveAttribute('data-level', 'paused');
        await expect(banner).toContainText('nearly full: 0.50 GB free');
        await expect(page.getByTestId('status-database-disk-badge')).toHaveText('INGEST PAUSED');

        await fakeDisk(page, 'ok', 50);
        await page.reload();
        await expect(page.getByTestId('status-database-disk-badge')).toHaveText('OK');
        await expect(banner).toHaveCount(0);
    });
    test('the page has no refresh timer; the banner re-checks every 5 minutes', async ({ page }) => {
        // Disk space moves slowly and ingest refuses on its own when it runs low, so the page checks
        // when it opens (and on Refresh / coming back to the tab) and the banner every 5 minutes.
        await page.clock.install();
        let asked = 0;
        await page.route('**/api/admin/status', route => {
            asked += 1;
            return route.fulfill({ json: fakeStatus('ok', 50) });
        });
        await page.goto('/settings/status');
        await expect(page.getByTestId('status-database-disk-badge')).toHaveText('OK');
        await page.waitForTimeout(500);
        const opened = asked;  // the page's check and the banner's

        await page.clock.runFor(4 * 60_000);
        await page.waitForTimeout(1000);
        expect(asked).toBe(opened);

        await page.clock.runFor(90_000);
        await expect.poll(() => asked).toBe(opened + 1);  // the banner, at 5 minutes
    });

    test('a failing status endpoint keeps the banner silent; the page says why', async ({ page }) => {
        // Right after this merges the frontend has the banner but a not-yet-restarted backend 404s
        // the endpoint: no "Couldn't load data" toast on every page, every minute.
        let asked = 0;
        await page.route('**/api/admin/status', route => {
            asked += 1;
            return route.fulfill({ status: 404, json: { detail: 'Not Found' } });
        });
        await page.goto('/');
        await expect.poll(() => asked).toBeGreaterThan(0);
        await page.waitForTimeout(1000);
        await expect(page.getByText(/Couldn't load data/)).toHaveCount(0);
        await expect(page.getByTestId('low-disk-banner')).toHaveCount(0);

        await page.goto('/settings/status');
        await expect(page.getByTestId('status-error')).toHaveText("Couldn't load the system status: Failed to load system status: Not Found");
    });
});

test('non-admins get no System status card, no banner, and a 403', async ({ browser }) => {
    const cookie = await adminLogin();
    const username = `e2e_status_${Date.now()}`;
    const password = 'E2e-Status-Pw-2026!';
    const user = await apiFetch(cookie, '/users', { method: 'POST', body: JSON.stringify({ username, password, role: 'power_user' }) });
    try {
        await apiFetch(cookie, `/users/${user.id}`, { method: 'PATCH', body: JSON.stringify({ must_change_password: false }) }).catch(() => undefined);
        const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
        const page = await ctx.newPage();
        const login = await page.request.post('/api/auth/login', { data: { username, password } });
        expect(login.ok()).toBeTruthy();

        let asked = 0;
        await page.route('**/api/admin/status', route => {
            asked += 1;
            return route.fulfill({ json: fakeStatus('paused', 0.5) });
        });
        await page.goto('/settings');
        await expect(page.getByText('API Tokens')).toBeVisible();
        await expect(page.getByTestId('settings-system-status')).toHaveCount(0);
        await expect(page.getByTestId('low-disk-banner')).toHaveCount(0);
        expect(asked).toBe(0);  // the banner's query never runs for a non-admin

        expect((await page.request.get('/api/admin/status')).status()).toBe(403);
        await page.goto('/settings/status');
        await expect(page.getByText('Only administrators can see the system status.')).toBeVisible();
        expect(asked).toBe(0);
        await ctx.close();
    } finally {
        await apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' });
    }
});
