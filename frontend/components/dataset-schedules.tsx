'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Info } from 'lucide-react';
import { useUser } from '@/contexts/UserContext';
import {
    createSchedule,
    deleteSchedule,
    getEmbeddingFields,
    getSchedules,
    runSchedule,
    updateSchedule,
    type DatasetSchedule,
    type ScheduleJobType,
    type ScheduleRule,
} from '@/lib/api';

const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const STATES: Record<string, string> = {
    queued: 'waiting',
    running: 'running',
    succeeded: 'succeeded',
    failed: 'failed',
    cancelled: 'cancelled',
};

// A new schedule is written in the browser's time zone (UTC when the browser names none).
export function browserTimeZone(): string {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}

// Times show in the schedule's own zone, the one its rule is written in.
function when(value: string | null | undefined, timeZone: string, withDate = true) {
    if (!value) return '';
    return new Intl.DateTimeFormat(undefined, {
        timeZone,
        ...(withDate ? { month: 'short', day: 'numeric' } : {}),
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
    }).format(new Date(value));
}

function ruleText(rule: ScheduleRule) {
    const parts = [rule.frequency === 'weekly' ? `Weekly on ${WEEKDAYS[rule.weekday ?? 0]} at ${rule.time}` : `Daily at ${rule.time}`];
    if (rule.min_new_samples) parts.push(`only when ≥ ${rule.min_new_samples.toLocaleString()} new samples`);
    if (rule.min_days_between) parts.push(`at most every ${rule.min_days_between} day${rule.min_days_between === 1 ? '' : 's'}`);
    if (rule.window_minutes !== 180) parts.push(`starts within ${rule.window_minutes / 60} h`);
    return parts.join(' · ');
}

function lastText(schedule: DatasetSchedule) {
    const zone = schedule.timezone;
    const job = schedule.last_job;
    if (!job) return 'Last: never';
    const started = when(job.started_at ?? job.created_at, zone);
    if (job.state === 'queued' || job.state === 'running') return `Last: ${started} → ${STATES[job.state]}`;
    const finished = job.finished_at ? ` → finished ${when(job.finished_at, zone, false)}` : '';
    const skipped = schedule.last_result?.skipped;
    return `Last: ${started}${finished} (${skipped ? `skipped: ${skipped}` : STATES[job.state]})`;
}

export const REFIT_NOTE =
    'While a re-fit runs (about an hour per map), new samples for this map wait to be placed on it. '
    + 'When it finishes, the new map is swapped in: writes to this dataset wait, or are retried, meanwhile '
    + '(about a minute per million points on the map). A run that cannot start within its window is skipped.';

export const REVIEW_NOTE =
    'A read-only review of this dataset\'s indexes: suggested, possibly unused and overlapping indexes, shown in '
    + 'Dataset Settings → Indexes. It never adds or removes an index.';

function ScheduleForm({ datasetId, schedule, defaults, fields, onDone, canReview = false, reviewDefaults }: {
    datasetId: number;
    schedule?: DatasetSchedule;
    defaults: ScheduleRule;
    fields: string[];
    onDone: () => void;
    canReview?: boolean;
    reviewDefaults?: ScheduleRule;
}) {
    const queryClient = useQueryClient();
    const [jobType, setJobType] = useState<ScheduleJobType>(schedule?.job_type ?? (fields.length ? 'map_refit' : 'index_review'));
    const review = jobType === 'index_review';
    const start = schedule ?? defaults;
    const zone = schedule?.timezone ?? browserTimeZone();
    const [target, setTarget] = useState(schedule?.target ?? fields[0] ?? '');
    const [frequency, setFrequency] = useState<ScheduleRule['frequency']>(start.frequency);
    const [weekday, setWeekday] = useState(start.weekday ?? 0);
    const [time, setTime] = useState(start.time);
    const [useSamples, setUseSamples] = useState(start.min_new_samples != null);
    const [samples, setSamples] = useState(start.min_new_samples ?? defaults.min_new_samples ?? 20000);
    const [useDays, setUseDays] = useState(start.min_days_between != null);
    const [days, setDays] = useState(start.min_days_between ?? defaults.min_days_between ?? 7);  // shown when switched on
    const [windowHours, setWindowHours] = useState((start.window_minutes ?? defaults.window_minutes ?? 180) / 60);
    const save = useMutation({
        mutationFn: () => {
            const rule: ScheduleRule = {
                enabled: schedule ? schedule.enabled : true,
                frequency,
                weekday: frequency === 'weekly' ? weekday : null,
                time,
                timezone: zone,
                min_new_samples: useSamples && !review ? samples : null,
                min_days_between: useDays && !review ? days : null,
                window_minutes: Math.round(windowHours * 60),
            };
            return schedule ? updateSchedule(datasetId, schedule.id, rule) : createSchedule(datasetId, target, rule, jobType);
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['dataset-schedules', datasetId] });
            onDone();
        },
    });
    const input = 'bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm text-white';
    return (
        <form
            data-testid="schedule-form"
            className="mt-2 space-y-2 rounded border border-zinc-700 p-3 text-sm text-zinc-300"
            onSubmit={event => { event.preventDefault(); save.mutate(); }}
        >
            <div className="font-medium text-white">{schedule ? 'Edit schedule' : 'Add schedule'}</div>
            <label className="flex items-center gap-2">
                <span className="w-24 text-zinc-400">Job</span>
                <select
                    className={input}
                    data-testid="schedule-job-type"
                    value={jobType}
                    disabled={!!schedule || !canReview || !fields.length}
                    onChange={event => {
                        const next = event.target.value as ScheduleJobType;
                        setJobType(next);
                        // Each job type has its own default slot (a review runs after the re-fit's window).
                        const preset = next === 'index_review' ? reviewDefaults : defaults;
                        if (preset) setTime(preset.time);
                    }}
                >
                    {(!!fields.length || jobType === 'map_refit') && <option value="map_refit">Map re-fit</option>}
                    {(canReview || jobType === 'index_review') && <option value="index_review">Index review</option>}
                </select>
            </label>
            {!review && <label className="flex items-center gap-2">
                <span className="w-24 text-zinc-400">Map</span>
                <select
                    className={input}
                    data-testid="schedule-target"
                    value={target}
                    disabled={!!schedule}
                    onChange={event => setTarget(event.target.value)}
                >
                    {(schedule ? [schedule.target] : fields).map(field => <option key={field} value={field}>{field}</option>)}
                </select>
            </label>}
            <div className="flex flex-wrap items-center gap-2">
                <span className="w-24 text-zinc-400">Run</span>
                <label className="flex items-center gap-1">
                    <input type="radio" checked={frequency === 'daily'} onChange={() => setFrequency('daily')} /> Daily
                </label>
                <label className="flex items-center gap-1">
                    <input type="radio" checked={frequency === 'weekly'} onChange={() => setFrequency('weekly')} /> Weekly on
                </label>
                <select
                    className={input}
                    aria-label="Weekday"
                    value={weekday}
                    disabled={frequency !== 'weekly'}
                    onChange={event => setWeekday(Number(event.target.value))}
                >
                    {WEEKDAYS.map((day, index) => <option key={day} value={index}>{day.slice(0, 3)}</option>)}
                </select>
                <span>at</span>
                <input className={input} type="time" aria-label="Time" value={time} required onChange={event => setTime(event.target.value)} />
                <span className="text-xs text-zinc-500">{zone}</span>
            </div>
            {!review && <><label className="flex flex-wrap items-center gap-2">
                <span className="w-24 text-zinc-400">Only when</span>
                <input type="checkbox" checked={useSamples} onChange={event => setUseSamples(event.target.checked)} />
                <span>at least</span>
                <input
                    className={`${input} w-28`}
                    type="number"
                    min={1}
                    aria-label="Minimum new samples"
                    value={samples}
                    disabled={!useSamples}
                    onChange={event => setSamples(Number(event.target.value))}
                />
                <span title="The growth of the map's point count: samples placed on it minus samples removed">
                    new samples since the last re-fit
                </span>
            </label>
            <label
                className="flex flex-wrap items-center gap-2"
                title="Off by default. With a gap, the next check moves straight to the first time the gap since the last re-fit allows; the nights in between are not checked."
            >
                <span className="w-24 text-zinc-400">At most</span>
                <input
                    type="checkbox"
                    aria-label="Limit how often it re-fits"
                    checked={useDays}
                    onChange={event => setUseDays(event.target.checked)}
                />
                <span>once every</span>
                <input
                    className={`${input} w-20`}
                    type="number"
                    min={1}
                    aria-label="Minimum days between runs"
                    value={days}
                    disabled={!useDays}
                    onChange={event => setDays(Number(event.target.value))}
                />
                <span>days</span>
            </label>
            <label className="flex flex-wrap items-center gap-2">
                <span className="w-24 text-zinc-400">Start</span>
                <span>within</span>
                <input
                    className={`${input} w-20`}
                    type="number"
                    min={1}
                    max={168}
                    aria-label="Start window in hours"
                    value={windowHours}
                    onChange={event => setWindowHours(Number(event.target.value))}
                />
                <span>hours of that time, or skip that run</span>
            </label>
            </>}
            <p className="flex gap-2 text-xs text-zinc-400" data-testid="schedule-pause-note">
                <Info size={14} className="shrink-0 mt-0.5" aria-hidden="true" />
                {review ? REVIEW_NOTE : REFIT_NOTE}
            </p>
            {save.isError && <p className="text-xs text-red-300" role="alert">{save.error.message}</p>}
            <div className="flex justify-end gap-2">
                <button type="button" className="px-3 py-1 rounded text-zinc-300 hover:text-white" onClick={onDone}>Cancel</button>
                <button
                    type="submit"
                    data-testid="schedule-save"
                    disabled={save.isPending || (!review && !target)}
                    className="px-3 py-1 rounded bg-cyan-600 hover:bg-cyan-500 text-white disabled:opacity-50"
                >
                    {save.isPending ? 'Saving…' : 'Save'}
                </button>
            </div>
        </form>
    );
}

function ScheduleRow({ schedule, datasetId, canEdit, defaults }: {
    schedule: DatasetSchedule;
    datasetId: number;
    canEdit: boolean;
    defaults: ScheduleRule;
}) {
    const queryClient = useQueryClient();
    const [editing, setEditing] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState(false);
    const refresh = () => {
        queryClient.invalidateQueries({ queryKey: ['dataset-schedules', datasetId] });
        queryClient.invalidateQueries({ queryKey: ['dataset-active-jobs', datasetId] });
        queryClient.invalidateQueries({ queryKey: ['dataset-job-history', datasetId] });
    };
    const toggle = useMutation({
        mutationFn: () => updateSchedule(datasetId, schedule.id, { ...schedule, enabled: !schedule.enabled }),
        onSuccess: refresh,
    });
    const run = useMutation({ mutationFn: () => runSchedule(datasetId, schedule.id), onSuccess: refresh });
    const remove = useMutation({ mutationFn: () => deleteSchedule(datasetId, schedule.id), onSuccess: refresh });
    const error = toggle.error ?? run.error ?? remove.error;
    const zone = schedule.timezone;
    const map = schedule.map;
    const review = schedule.job_type === 'index_review';
    return (
        <li data-testid="schedule-row" data-schedule-id={schedule.id} className="py-3 border-b border-zinc-800 last:border-0">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className={schedule.enabled ? 'text-green-400' : 'text-zinc-500'} aria-hidden="true">{schedule.enabled ? '●' : '○'}</span>
                <span className="text-sm text-zinc-200">{review ? 'Index review · this dataset\'s indexes' : `Map re-fit · ${schedule.target}`}</span>
                {canEdit ? (
                    <button
                        data-testid="schedule-toggle"
                        className={`ml-auto px-2 py-0.5 rounded border text-xs ${schedule.enabled ? 'border-green-500/40 text-green-300' : 'border-zinc-700 text-zinc-400'}`}
                        disabled={toggle.isPending}
                        onClick={() => toggle.mutate()}
                        aria-pressed={schedule.enabled}
                    >
                        {schedule.enabled ? 'On' : 'Off'}
                    </button>
                ) : <span className="ml-auto text-xs text-zinc-400">{schedule.enabled ? 'On' : 'Off'}</span>}
            </div>
            <div className="mt-1 text-xs text-zinc-400" data-testid="schedule-rule">{ruleText(schedule)}</div>
            <div className="mt-0.5 text-xs text-zinc-400" data-testid="schedule-last-next">
                {lastText(schedule)}
                {' · '}
                {schedule.enabled && schedule.next_run_at ? `Next check: ${when(schedule.next_run_at, zone)}` : 'Next check: off'}
            </div>
            {map && <div className="mt-0.5 text-xs text-zinc-500">
                {map.refit_running
                    ? 'A re-fit is running; new samples wait to be placed on the map.'
                    : map.refitted_at
                        ? `Map re-fitted ${when(map.refitted_at, zone)} · ${map.new_samples.toLocaleString()} new samples since`
                        : `${map.new_samples.toLocaleString()} points on the map; not re-fitted on a schedule yet`}
                {schedule.last_check && !schedule.last_check.ran && ` · Checked ${when(schedule.last_check.at, zone)}: ${schedule.last_check.reason}`}
            </div>}
            {canEdit && !editing && (
                <div className="mt-1 flex flex-wrap justify-end gap-3 text-xs">
                    <button data-testid="schedule-run-now" className="underline hover:text-white text-zinc-300" disabled={run.isPending} onClick={() => run.mutate()}>
                        {run.isPending ? 'Starting…' : 'Run now'}
                    </button>
                    <button data-testid="schedule-edit" className="underline hover:text-white text-zinc-300" onClick={() => setEditing(true)}>Edit</button>
                    {confirmDelete ? (
                        <span className="flex gap-2">
                            <span className="text-zinc-300">Delete this schedule?</span>
                            <button data-testid="schedule-delete-confirm" className="underline text-red-300" disabled={remove.isPending} onClick={() => remove.mutate()}>Delete</button>
                            <button className="underline text-zinc-300" onClick={() => setConfirmDelete(false)}>Keep it</button>
                        </span>
                    ) : (
                        <button data-testid="schedule-delete" className="underline hover:text-white text-zinc-300" onClick={() => setConfirmDelete(true)}>Delete</button>
                    )}
                </div>
            )}
            {run.data && (
                <p className="mt-1 text-xs text-zinc-400" role="status">
                    {run.data.created ? 'Queued; it shows under Running.'
                        : review ? 'A review of this dataset is already queued or running.' : 'A re-fit of this map is already queued or running.'}
                </p>
            )}
            {error && <p className="mt-1 text-xs text-red-300" role="alert">{error.message}</p>}
            {editing && (
                <ScheduleForm datasetId={datasetId} schedule={schedule} defaults={defaults} fields={[schedule.target]} onDone={() => setEditing(false)} />
            )}
        </li>
    );
}

// The Schedules tab of the dataset's Jobs panel; fetched and polled only while open.
export function DatasetSchedules({ datasetId }: { datasetId: number }) {
    const { user } = useUser();
    const canEdit = user?.role === 'admin' || user?.role === 'power_user';
    const isAdmin = user?.role === 'admin';
    const [adding, setAdding] = useState(false);
    const schedules = useQuery({
        queryKey: ['dataset-schedules', datasetId],
        queryFn: () => getSchedules(datasetId),
        // Every time the tab opens, and every 5 s while one of its jobs is queued or running.
        staleTime: 0,
        refetchInterval: query => (query.state.data?.items.some(item => item.last_job?.state === 'queued'
            || item.last_job?.state === 'running') ? 5_000 : 15_000),
        retry: false,
        meta: { errorHandledLocally: true },
    });
    const fields = useQuery({
        queryKey: ['embedding-fields-for-schedules', datasetId],
        queryFn: () => getEmbeddingFields(datasetId),
        enabled: adding,
    });
    if (schedules.isError) {
        return (
            <p className="text-sm text-zinc-400" data-testid="schedules-unavailable">
                Schedules unavailable. <button className="underline hover:text-white" onClick={() => schedules.refetch()}>Retry</button>
            </p>
        );
    }
    if (!schedules.data) return <p className="text-sm text-zinc-500">Loading schedules…</p>;
    const scheduled = new Set(schedules.data.items.filter(item => item.job_type === 'map_refit').map(item => item.target));
    const available = (fields.data ?? []).filter(field => !field.includes('_patch_') && !scheduled.has(field));
    // The index review is an administrator's tool; one schedule per dataset.
    const canReview = isAdmin && !schedules.data.items.some(item => item.job_type === 'index_review');
    return (
        <div data-testid="dataset-schedules">
            <div className="flex items-center justify-between">
                <p className="text-sm text-zinc-400">Recurring jobs for this dataset. Runs show under Running and History.</p>
                {canEdit && !adding && (
                    <button data-testid="add-schedule" className="px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-xs text-zinc-200" onClick={() => setAdding(true)}>
                        + Add schedule
                    </button>
                )}
            </div>
            {adding && (
                fields.isLoading ? <p className="text-sm text-zinc-500">Loading maps…</p>
                    : fields.isError ? (
                        <p className="mt-2 text-sm text-red-300" role="alert">
                            Could not list this dataset&apos;s maps. <button className="underline" onClick={() => setAdding(false)}>Close</button>
                        </p>
                    )
                    : available.length || canReview ? (
                        <ScheduleForm
                            datasetId={datasetId}
                            defaults={available.length ? schedules.data.defaults : schedules.data.review_defaults ?? schedules.data.defaults}
                            fields={available}
                            canReview={canReview}
                            reviewDefaults={schedules.data.review_defaults}
                            onDone={() => setAdding(false)}
                        />
                    ) : (
                        <p className="mt-2 text-sm text-zinc-400">
                            Every map already has a schedule. <button className="underline" onClick={() => setAdding(false)}>Close</button>
                        </p>
                    )
            )}
            {schedules.data.items.length ? (
                <ul className="mt-2">
                    {schedules.data.items.map(schedule => (
                        <ScheduleRow
                            key={schedule.id}
                            schedule={schedule}
                            datasetId={datasetId}
                            canEdit={schedule.job_type === 'index_review' ? isAdmin : canEdit}
                            defaults={schedules.data.defaults}
                        />
                    ))}
                </ul>
            ) : !adding && <p className="mt-3 text-sm text-zinc-500">No schedules yet.</p>}
        </div>
    );
}
