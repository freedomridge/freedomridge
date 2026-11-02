'use client';

import { useEffect, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, ArrowLeft, Loader2, RefreshCw } from 'lucide-react';
import { Header } from '@/components/header';
import { ProtectedRoute } from '@/components/ProtectedRoute';
import { GlobalJobs } from '@/components/job-history';
import { IndexAdvisor } from '@/components/index-advisor';
import { formatGB, useSystemStatus } from '@/components/system-status';
import { useToast } from '@/contexts/ToastContext';
import { useUser } from '@/contexts/UserContext';
import { runBackupNow, saveBackupSchedule, type BackupStatus, type BackupVersions } from '@/lib/api';

type Tone = 'ok' | 'warn' | 'bad' | 'none';

const TONE: Record<Tone, string> = {
    ok: 'bg-green-500/10 text-green-400 border-green-500/30',
    warn: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
    bad: 'bg-red-500/10 text-red-300 border-red-500/30',
    none: 'bg-zinc-800 text-zinc-400 border-zinc-700',
};

function Row({ testId, title, badge, tone, children }: { testId: string; title: string; badge: string; tone: Tone; children: ReactNode }) {
    return (
        <div data-testid={testId} className="p-5 bg-zinc-900/50 border border-zinc-800 rounded-xl">
            <div className="flex items-center justify-between gap-4 mb-2">
                <h2 className="text-lg font-semibold text-white">{title}</h2>
                <span data-testid={`${testId}-badge`} className={`px-2 py-0.5 text-xs font-semibold rounded border ${TONE[tone]}`}>
                    {badge}
                </span>
            </div>
            <div className="text-sm text-zinc-300 space-y-1">{children}</div>
        </div>
    );
}

function usedOf(free: number, total: number) {
    return `${formatGB(free)} free of ${formatGB(total)} (${Math.round(100 * (1 - free / total))}% used)`;
}

const BACKUP_BADGE: Record<BackupStatus['level'], [string, Tone]> = {
    ok: ['OK', 'ok'],
    overdue: ['OVERDUE', 'warn'],
    failed: ['FAILED', 'bad'],
    none: ['NO BACKUP', 'warn'],
    unreadable: ["CAN'T READ", 'bad'],
    not_configured: ['NOT SET UP', 'none'],
};

// backup_local.py and its log count decimal gigabytes; the card uses the same so the numbers match.
const decimalGB = (bytes: number) => `${(bytes / 1e9).toFixed(1)} GB`;
const sentence = (text: string) => text.replace(/[.\s]+$/, '');

// "0002_some_change.sql" -> "0002"
function schemaNumber(version: string | null) {
    return version ? version.match(/^\d+/)?.[0] ?? version : 'unknown';
}

// "16.13 (Debian 16.13-1.pgdg120+1)" -> "16.13"
function postgres(version: string | null) {
    return version?.split(' ')[0] || 'unknown';
}

function schemaLine(backup: BackupVersions, now: BackupVersions, same: boolean | undefined) {
    const [taken, current] = [schemaNumber(backup.schema_version), schemaNumber(now.schema_version)];
    if (same) return `Schema ${taken}, the same as the database now`;
    if (!backup.schema_version || !now.schema_version) return `Schema ${taken}; the database is at ${current}`;
    if (backup.schema_version === now.schema_version) {
        return `Schema ${taken} with ${backup.migrations_applied ?? 'unknown'} migrations recorded; the database has ${now.migrations_applied ?? 'unknown'}`;
    }
    // Migration file names start with a zero-padded number, so they compare as text.
    return backup.schema_version < now.schema_version
        ? `Schema ${taken}; the database is now at ${current}, so run scripts/apply_migrations.sh after a restore`
        : `Schema ${taken}, newer than the database (${current}): restore it only with code at least as new`;
}

function took(seconds: number) {
    return seconds < 90 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
}

type FolderBackups = Extract<BackupStatus, { kept: unknown }>;

const INPUT = 'px-2 py-1 bg-zinc-800 border border-zinc-700 rounded text-white focus:outline-none focus:border-cyan-500';
const hourLabel = (hour: number) => `${String(hour).padStart(2, '0')}:00`;

// How often the job runner backs up the whole database, and "Back up now". Off still lets an admin
// back up by hand, e.g. once for a dataset that doesn't change.
function BackupSchedule({ backups }: { backups: FolderBackups }) {
    const queryClient = useQueryClient();
    const { addToast } = useToast();
    const saved = backups.schedule;
    const savedHours = saved?.interval_hours ?? null;
    const [enabled, setEnabled] = useState(savedHours !== null);
    const [unit, setUnit] = useState<'hours' | 'days'>(savedHours === null || savedHours % 24 === 0 ? 'days' : 'hours');
    const [every, setEvery] = useState(savedHours === null ? 1 : savedHours % 24 === 0 ? savedHours / 24 : savedHours);
    const [startHour, setStartHour] = useState(saved?.start_hour ?? 2);
    const [keep, setKeep] = useState(saved?.keep ?? 7);
    const refresh = () => {
        queryClient.invalidateQueries({ queryKey: ['system-status'] });
        queryClient.invalidateQueries({ queryKey: ['global-jobs'] });
    };
    const active = backups.active_job;
    // The card polls while a backup is queued or running; the Background jobs list below doesn't, so
    // refresh it whenever the backup starts, runs or ends.
    useEffect(() => {
        queryClient.invalidateQueries({ queryKey: ['global-jobs'] });
    }, [queryClient, active?.id, active?.state]);

    const intervalHours = enabled ? every * (unit === 'days' ? 24 : 1) : null;
    const valid = Number.isInteger(every) && every >= 1 && (intervalHours ?? 1) <= 8760 && Number.isInteger(keep) && keep >= 1 && keep <= 100;
    const changed = !saved || intervalHours !== saved.interval_hours || startHour !== saved.start_hour || keep !== saved.keep;
    const save = useMutation({
        mutationFn: () => saveBackupSchedule({ interval_hours: intervalHours, start_hour: startHour, keep }),
        onSuccess: () => {
            addToast(intervalHours === null ? 'Scheduled backups turned off' : 'Backup schedule saved', 'success');
            refresh();
        },
        onError: (error: Error) => addToast(error.message, 'error'),
    });
    const run = useMutation({
        mutationFn: runBackupNow,
        onSuccess: (result) => {
            addToast(result.started ? 'Backup started' : 'A backup is already running', 'info');
            refresh();
        },
        onError: (error: Error) => addToast(error.message, 'error'),
    });

    if (!saved) {
        return <p className="text-zinc-400">Scheduling needs the latest database migrations (scripts/apply_migrations.sh).</p>;
    }
    return (
        <div data-testid="status-backups-schedule" className="mt-3 pt-3 border-t border-zinc-800 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
                <span>Back up</span>
                <select aria-label="Backup schedule" value={enabled ? 'every' : 'off'} onChange={e => setEnabled(e.target.value === 'every')} className={INPUT}>
                    <option value="off">only when I ask</option>
                    <option value="every">every</option>
                </select>
                {enabled && (
                    <>
                        <input aria-label="Backup interval" type="number" min={1} value={every} onChange={e => setEvery(Number(e.target.value))} className={`${INPUT} w-20`} />
                        <select aria-label="Backup interval unit" value={unit} onChange={e => setUnit(e.target.value as 'hours' | 'days')} className={INPUT}>
                            <option value="hours">hours</option>
                            <option value="days">days</option>
                        </select>
                        <span>starting at</span>
                        <select aria-label="Backup start hour" value={startHour} onChange={e => setStartHour(Number(e.target.value))} className={INPUT}>
                            {Array.from({ length: 24 }, (_, hour) => (
                                <option key={hour} value={hour}>{hourLabel(hour)}</option>
                            ))}
                        </select>
                        <span className="text-zinc-400">server time</span>
                    </>
                )}
                <span>· keep the newest</span>
                <input aria-label="Backups to keep" type="number" min={1} max={100} value={keep} onChange={e => setKeep(Number(e.target.value))} className={`${INPUT} w-20`} />
                <button
                    onClick={() => save.mutate()}
                    disabled={!changed || !valid || save.isPending}
                    className="px-3 py-1 bg-cyan-600 hover:bg-cyan-500 text-white rounded transition-colors disabled:opacity-50"
                >
                    Save
                </button>
            </div>
            <p data-testid="status-backups-next" className="text-zinc-400">
                {saved.next_run_at ? `Next backup: ${new Date(saved.next_run_at).toLocaleString()}` : 'No scheduled backups.'}
            </p>
            <div className="flex flex-wrap items-center gap-3">
                <button
                    onClick={() => run.mutate()}
                    disabled={!!active || run.isPending}
                    className="px-3 py-1 bg-zinc-800 hover:bg-zinc-700 text-white rounded transition-colors disabled:opacity-50"
                >
                    Back up now
                </button>
                {active && (
                    <span data-testid="status-backups-active" className="text-cyan-300">
                        {active.state === 'running' && active.started_at
                            ? `Backing up now, started ${new Date(active.started_at).toLocaleTimeString()}.`
                            : 'A backup is queued.'}{' '}
                        It&apos;s listed under Background jobs below.
                    </span>
                )}
            </div>
        </div>
    );
}

function Backups({ backups }: { backups: BackupStatus }) {
    const [badge, tone] = BACKUP_BADGE[backups.level] ?? [String(backups.level).toUpperCase(), 'none'];
    if (backups.level === 'not_configured') {
        return (
            <Row testId="status-backups" title="Backups" badge={badge} tone={tone}>
                <p>
                    Not set up. Set <span className="font-mono">FRIDGE_BACKUP_DIR</span> in .env to a folder for backups, on another
                    disk than the database if you can, then restart the backend.
                </p>
            </Row>
        );
    }
    if (backups.level === 'unreadable') {
        return (
            <Row testId="status-backups" title="Backups" badge={badge} tone={tone}>
                <p className="text-red-300">
                    Can&apos;t read <span className="font-mono">{backups.folder}</span>: {sentence(backups.error)}. If the backup drive
                    isn&apos;t mounted, the nightly backup fails too.
                </p>
            </Row>
        );
    }
    if (!('kept' in backups)) {
        return <Row testId="status-backups" title="Backups" badge={badge} tone={tone}><p>This backup state isn&apos;t known to this page yet.</p></Row>;
    }
    const { latest, last_failure: failure, kept, disk, current } = backups;
    return (
        <Row testId="status-backups" title="Backups" badge={badge} tone={tone}>
            {latest ? (
                <p data-testid="status-backups-latest">
                    Latest verified: {new Date(latest.created_at).toLocaleString()}
                    {latest.size_bytes != null && ` · ${decimalGB(latest.size_bytes)}`}
                    {latest.dump_seconds != null && ` · took ${took(latest.dump_seconds + (latest.verify_seconds ?? 0))}`}
                    {latest.tables != null && ` · read back, ${latest.tables} tables, every row count matched`}
                </p>
            ) : (
                <p>No verified backup in this folder yet.</p>
            )}
            {failure && (
                <p data-testid="status-backups-failure" className="text-red-300">
                    The last attempt failed {new Date(failure.failed_at).toLocaleString()}: {sentence(failure.error)}. The cron log has the details.
                </p>
            )}
            {backups.level === 'overdue' && (
                <p className="text-amber-300">
                    No verified backup in the last {backups.overdue_after_hours} hours, 1.5 times the schedule. Check Background jobs below
                    and backup.log in the backup folder.
                </p>
            )}
            {latest && current && (
                <p data-testid="status-backups-version" className={backups.same_postgres_major ? undefined : 'text-amber-300'}>
                    {schemaLine(latest, current, backups.same_schema)} ·{' '}
                    {backups.same_postgres_major
                        ? `Postgres ${postgres(latest.server_version)}`
                        : `taken on Postgres ${postgres(latest.server_version)}, but this server runs ${postgres(current.server_version)}`}
                </p>
            )}
            <p className="text-zinc-400">
                <span className="font-mono">{backups.folder}</span> · keeping {kept.count} verified ({decimalGB(kept.total_bytes)}) ·{' '}
                {decimalGB(disk.free_bytes)} free of {decimalGB(disk.total_bytes)}
            </p>
            {backups.unverified_dumps > 0 && (
                <p className="text-zinc-400">
                    Also in this folder: {backups.unverified_dumps} dump{backups.unverified_dumps === 1 ? '' : 's'} without a usable manifest
                    (made by hand, still being written, or changed since it was checked); not counted.
                </p>
            )}
            {backups.schedule !== undefined && <BackupSchedule backups={backups} />}
        </Row>
    );
}

export default function SystemStatusPage() {
    const { user } = useUser();
    const { data, error, isFetching, refetch } = useSystemStatus();
    const queryClient = useQueryClient();

    if (user?.role !== 'admin') {
        return (
            <ProtectedRoute>
                <div className="min-h-screen bg-zinc-950 flex flex-col">
                    <Header />
                    <main className="flex-1 container mx-auto px-6 py-8">
                        <div className="text-center py-12">
                            <AlertCircle className="mx-auto h-12 w-12 text-red-400" />
                            <h2 className="mt-4 text-xl font-semibold text-white">Access Denied</h2>
                            <p className="mt-2 text-zinc-400">Only administrators can see the system status.</p>
                        </div>
                    </main>
                </div>
            </ProtectedRoute>
        );
    }

    const disk = data?.database_disk;
    const database = data?.database;
    const server = data?.server;
    const workers = data?.workers;

    return (
        <ProtectedRoute>
            <div className="min-h-screen bg-zinc-950 flex flex-col">
                <Header />

                <main className="flex-1 container mx-auto px-6 py-8 max-w-4xl">
                    <div className="flex items-center gap-4 mb-8">
                        <Link href="/settings" className="p-2 hover:bg-zinc-800 rounded-lg transition-colors">
                            <ArrowLeft className="w-5 h-5 text-zinc-400" />
                        </Link>
                        <div className="flex-1">
                            <h1 className="text-3xl font-bold text-white">System Status</h1>
                            <p className="text-zinc-400 mt-1">Checked when you open this page or come back to it</p>
                        </div>
                        <div className="flex items-center gap-3 text-sm text-zinc-400">
                            {data && <span data-testid="status-checked-at">checked {new Date(data.checked_at).toLocaleTimeString()}</span>}
                            <button
                                onClick={() => {
                                    refetch();
                                    queryClient.invalidateQueries({ queryKey: ['global-jobs'] });
                                }}
                                disabled={isFetching}
                                className="flex items-center gap-2 px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-white rounded-lg transition-colors disabled:opacity-60"
                            >
                                <RefreshCw className={`w-4 h-4 ${isFetching ? 'animate-spin' : ''}`} />
                                Refresh
                            </button>
                        </div>
                    </div>

                    {error && !data && (
                        <p data-testid="status-error" className="text-red-300">Couldn&apos;t load the system status: {(error as Error).message}</p>
                    )}
                    {!data && !error && <Loader2 className="w-6 h-6 animate-spin text-zinc-500" />}

                    {disk && database && server && (
                        <div className="space-y-4">
                            <Row
                                testId="status-database-disk"
                                title="Database disk"
                                badge={{ ok: 'OK', low: 'LOW', paused: 'INGEST PAUSED', unknown: 'UNKNOWN' }[disk.level]}
                                tone={{ ok: 'ok', low: 'warn', paused: 'bad', unknown: 'none' }[disk.level] as Tone}
                            >
                                {disk.free_bytes != null && disk.total_bytes != null ? (
                                    <p>{usedOf(disk.free_bytes, disk.total_bytes)}</p>
                                ) : (
                                    <p>Free space can&apos;t be read (a managed database, a database user without superuser access, or the check itself failed), so ingest is never refused for it.</p>
                                )}
                                <p className="text-zinc-400">
                                    Adding samples, embeddings, coordinates and hashes is refused at about {formatGB(disk.ingest_paused_below_bytes)} free
                                    (a little more for a big batch), so the database can&apos;t fill the disk and crash; this shows Low below{' '}
                                    {formatGB(disk.low_below_bytes)}.
                                </p>
                                {(disk.level === 'low' || disk.level === 'paused') && (
                                    <p className={disk.level === 'paused' ? 'text-red-300' : 'text-amber-300'}>
                                        Free space on this disk (or move the database to a bigger one); ingest resumes on its own once there&apos;s room.
                                    </p>
                                )}
                            </Row>

                            {database.reachable ? (
                                <Row
                                    testId="status-database"
                                    title="Database"
                                    badge={database.unknown_migrations.length ? 'NEWER THAN CODE'
                                        : database.pending_migrations.length || database.stale_functions.length ? 'NEEDS MIGRATION' : 'OK'}
                                    tone={database.unknown_migrations.length || database.pending_migrations.length || database.stale_functions.length ? 'warn' : 'ok'}
                                >
                                    <p>
                                        Reachable · {formatGB(database.size_bytes)} ·{' '}
                                        {database.unknown_migrations.length ? 'schema newer than this version'
                                            : database.pending_migrations.length || database.stale_functions.length ? 'schema out of date' : 'schema up to date'}
                                    </p>
                                    {database.unknown_migrations.length > 0 && (
                                        <p className="text-amber-300">
                                            Migrations this version doesn&apos;t have: {database.unknown_migrations.join(', ')}. A newer version ran
                                            against this database, or a newer backup was restored: run that version, or restore a backup taken with this one.
                                        </p>
                                    )}
                                    {database.pending_migrations.length > 0 && (
                                        <p className="text-amber-300">Migrations not applied: {database.pending_migrations.join(', ')}</p>
                                    )}
                                    {database.stale_functions.length > 0 && (
                                        <p className="text-amber-300">Functions out of date: {database.stale_functions.join(', ')}</p>
                                    )}
                                    {(database.pending_migrations.length > 0 || database.stale_functions.length > 0) && !database.unknown_migrations.length && (
                                        <p className="text-zinc-400">Run scripts/apply_migrations.sh (no restart needed).</p>
                                    )}
                                </Row>
                            ) : (
                                <Row testId="status-database" title="Database" badge="DOWN" tone="bad">
                                    <p>Can&apos;t reach the database: {database.error}</p>
                                </Row>
                            )}

                            <div data-testid="status-indexes" className="p-5 bg-zinc-900/50 border border-zinc-800 rounded-xl">
                                <p className="mb-3 text-zinc-400 text-sm">
                                    Indexes of the whole database that no dataset owns (each dataset&apos;s field indexes are reviewed in its
                                    Dataset Settings → Indexes). A review only reads; it never adds or removes an index.
                                </p>
                                <IndexAdvisor />
                            </div>

                            {data.backups && <Backups backups={data.backups} />}

                            <Row
                                testId="status-workers"
                                title="Background execution slots"
                                badge={!workers?.available ? 'UNKNOWN' : workers.server.online > 0 ? 'ONLINE' : 'OFFLINE'}
                                tone={!workers?.available ? 'none' : workers.server.online > 0 ? 'ok' : 'warn'}
                            >
                                {workers?.available ? (
                                    <>
                                        <p>{workers.server.online} server execution slots online (heartbeat within 60 seconds).</p>
                                        {workers.server.online === 0 && (
                                            <p className="text-amber-300">No server heartbeat. Check the local launcher or the Docker job-runner service; queued jobs remain saved.</p>
                                        )}
                                        {workers.server.last_seen && <p className="text-zinc-400">Last server heartbeat: {new Date(workers.server.last_seen).toLocaleString()}</p>}
                                        <p className="text-zinc-400">{workers.compute.online} compute execution slots online. A remote worker is not required for local maintenance.</p>
                                    </>
                                ) : (
                                    <p>Worker health is unavailable. {workers && !workers.available ? workers.error : 'The backend may need an update.'}</p>
                                )}
                                <p className="text-zinc-400">These counts are slots, not runner processes. Pending jobs alone do not indicate a worker failure.</p>
                            </Row>

                            {data.cache_disks.map((cache, i) => (
                                <Row
                                    key={i}
                                    testId="status-cache-disk"
                                    title={data.cache_disks.length > 1 ? `Image cache disk ${i + 1}` : 'Image cache disk'}
                                    badge={cache.level === 'low' ? 'LOW' : 'OK'}
                                    tone={cache.level === 'low' ? 'warn' : 'ok'}
                                >
                                    <p>{usedOf(cache.free_bytes, cache.total_bytes)}</p>
                                    <p className="text-zinc-400">
                                        {cache.folders.map(f => `${f.label} (${f.path})`).join(' · ')} — derived copies, safe to clear when space is short.
                                    </p>
                                </Row>
                            ))}

                            <Row
                                testId="status-server"
                                title="Server"
                                badge={server.restart_needed ? 'RESTART NEEDED' : 'OK'}
                                tone={server.restart_needed ? 'warn' : 'ok'}
                            >
                                <p>
                                    Version {server.version}
                                    {server.running && (
                                        <> · running <span className="font-mono">{server.running.commit}</span> — {server.running.subject}</>
                                    )}
                                </p>
                                <p className="text-zinc-400">Started {new Date(server.started_at).toLocaleString()}</p>
                                {server.restart_needed && server.checkout && (
                                    <p className="text-amber-300">
                                        The checkout is now at <span className="font-mono">{server.checkout.commit}</span> — {server.checkout.subject}.
                                        Restart the backend to run it.
                                    </p>
                                )}
                            </Row>
                        </div>
                    )}

                    <div className="mt-4">
                        <GlobalJobs />
                    </div>
                </main>
            </div>
        </ProtectedRoute>
    );
}
