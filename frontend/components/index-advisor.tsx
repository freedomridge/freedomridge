'use client';

import { createContext, useContext, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import {
    ApiError,
    getIndexAdvisor,
    promoteField,
    removeIndex,
    restoreIndex,
    reviewIndexes,
    saveSystemReviewSchedule,
    type AdvisorIndex,
    type AdvisorOverlap,
    type AdvisorRemoval,
    type AdvisorSuggestion,
    type IndexReview,
    type ReviewSchedule,
} from '@/lib/api';
import { overlapPreferenceNote } from '@/lib/index-advisor-notes';
import { browserTimeZone } from '@/components/dataset-schedules';

// The index advisor (backend/index_advisor.py), for administrators: Dataset Settings → Indexes
// (datasetId given) and Settings → System status (core indexes). A review is a read-only job; this
// view shows the newest one and follows a review while it runs. Every change is an explicit click that
// runs as its own job: Remove (DROP INDEX CONCURRENTLY, confirmed in the page), Restore and Index this
// field (the field index build).

const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const USAGE_LABELS: Record<string, string> = { filter: 'filter', sort: 'sort', facet: 'facet', histogram: 'histogram' };
const PAGE = 10;

export function formatBytes(bytes: number) {
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
    if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function when(value: string | null | undefined) {
    if (!value) return '';
    return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
        .format(new Date(value));
}

function day(value: string | null | undefined) {
    if (!value) return '';
    return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(new Date(value));
}

function duration(milliseconds: number | null | undefined) {
    if (milliseconds == null) return '—';
    if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
    return `${(milliseconds / 1000).toFixed(1)} s`;
}

function seconds(value: number) {
    if (value < 90) return `${Math.round(value)} s`;
    if (value < 90 * 60) return `${Math.round(value / 60)} min`;
    return `${(value / 3600).toFixed(1)} h`;
}

function days(from: string, to: string) {
    return Math.max(0, Math.floor((new Date(to).getTime() - new Date(from).getTime()) / 86_400_000));
}

function ruleText(rule: ReviewSchedule) {
    const at = rule.frequency === 'weekly' ? `weekly on ${WEEKDAYS[rule.weekday ?? 0]} at ${rule.time}` : `daily at ${rule.time}`;
    return `${at} (${rule.timezone})`;
}

export function useIndexAdvisor(datasetId?: number) {
    return useQuery({
        queryKey: ['index-advisor', datasetId ?? 'system'],
        queryFn: () => getIndexAdvisor(datasetId),
        staleTime: 0,
        // Follow a review, a removal or a restore while it runs; otherwise read it when the view opens.
        refetchInterval: query => {
            const data = query.state.data;
            const busy = data?.pending_actions?.length || data?.removed?.some(item => item.state === 'removing' || item.state === 'restoring');
            return data?.active_job || busy ? 2_000 : false;
        },
        retry: false,
        meta: { errorHandledLocally: true },
    });
}

// What the action buttons need: the scope's query key and its open removals.
const Scope = createContext<{ datasetId?: number; removed: Map<string, AdvisorRemoval>; pending: Set<string> }>({
    removed: new Map(),
    pending: new Set(),
});

function useRefresh() {
    const queryClient = useQueryClient();
    const { datasetId } = useContext(Scope);
    return () => {
        queryClient.invalidateQueries({ queryKey: ['index-advisor', datasetId ?? 'system'] });
        if (datasetId !== undefined) {
            queryClient.invalidateQueries({ queryKey: ['dataset-schema', datasetId] });
            queryClient.invalidateQueries({ queryKey: ['dataset-active-jobs', datasetId] });
            queryClient.invalidateQueries({ queryKey: ['dataset-job-history', datasetId] });
        }
    };
}

const BUTTON = 'px-2 py-0.5 rounded border text-xs disabled:opacity-50';

// Remove, confirmed in the page (no browser dialog): what goes, what it cost, and how to undo it.
const VECTOR_METHODS = ['hnsw', 'ivfflat'];
const REMOVAL_LABELS: Record<AdvisorRemoval['state'], string> = {
    removing: 'Removing…',
    removed: 'Removed (see Removed indexes)',
    restoring: 'Restoring…',
    stopped: 'Removal stopped',
    back: 'Back',
};

function RemoveIndex({ index, reason }: { index: AdvisorIndex; reason: 'unused' | 'overlap' | 'invalid' }) {
    const { removed, pending } = useContext(Scope);
    const refresh = useRefresh();
    const [confirming, setConfirming] = useState(false);
    const [gone, setGone] = useState(false);
    const remove = useMutation({
        mutationFn: () => removeIndex(index.name, index.definition, reason),
        onSuccess: () => { setConfirming(false); refresh(); },
        onError: error => {
            // A review taken before the index went: say so instead of offering Remove again.
            if (error instanceof ApiError && error.status === 404) { setGone(true); setConfirming(false); refresh(); }
        },
    });
    const open = removed.get(index.name);
    if (index.needed.length) return null;
    if (index.kind === 'ann' || VECTOR_METHODS.includes(index.method)) {
        return <span className="text-xs text-zinc-500" data-testid="index-remove-managed">Managed by the embedding index controls</span>;
    }
    if (index.kind === 'array') {
        return <span className="text-xs text-zinc-500" data-testid="index-remove-managed">Managed with its array field</span>;
    }
    if (gone) {
        return <span className="text-xs text-zinc-400" data-testid="index-remove-state">Already removed; review again</span>;
    }
    // Queued and not yet recorded (or recorded as stopped, and queued again).
    if (pending.has(index.name) && (!open || open.state === 'stopped' || open.state === 'back')) {
        return <span className="text-xs text-zinc-400" data-testid="index-remove-state">Removing…</span>;
    }
    // A removal already recorded (and not merely stopped, or its index back): show where it stands.
    if (open && open.state !== 'stopped' && open.state !== 'back') {
        return <span className="text-xs text-zinc-400" data-testid="index-remove-state">{REMOVAL_LABELS[open.state]}</span>;
    }
    if (!confirming) {
        return (
            <button data-testid="index-remove" className={`${BUTTON} border-red-500/40 text-red-300 hover:bg-red-500/10`} onClick={() => setConfirming(true)}>
                {open ? 'Remove again' : 'Remove'}
            </button>
        );
    }
    return (
        <div data-testid="index-remove-confirm" className="mt-1 space-y-1 rounded border border-red-500/30 bg-red-500/5 p-2 text-xs text-zinc-300">
            <p>
                Remove <span className="font-mono">{index.name}</span> ({formatBytes(index.size_bytes)}, {index.scans.toLocaleString()} uses
                since {day(index.observed_since)}{index.last_scan ? `, last ${when(index.last_scan)}` : ', never used'})?
            </p>
            <p className="text-zinc-400">
                It is dropped online (DROP INDEX CONCURRENTLY, after any running backup); reads and ingest continue. Queries that used it
                get slower. Restore rebuilds it online from its recorded definition.
                {index.kind === 'field' && ' The field is marked not indexed first.'}
                {index.kind === 'core' && ' A core index: new installations still create it.'}
            </p>
            <div className="flex gap-2">
                <button
                    data-testid="index-remove-confirm-button"
                    className={`${BUTTON} border-red-500/60 bg-red-500/20 text-red-200`}
                    disabled={remove.isPending}
                    onClick={() => remove.mutate()}
                >
                    {remove.isPending ? 'Removing…' : 'Remove index'}
                </button>
                <button className={`${BUTTON} border-zinc-700 text-zinc-300`} onClick={() => { setConfirming(false); remove.reset(); }}>
                    Keep it
                </button>
            </div>
            {remove.isError && <p className="text-red-300" role="alert">{remove.error.message}</p>}
        </div>
    );
}

function IndexField({ field }: { field: string }) {
    const { datasetId } = useContext(Scope);
    const refresh = useRefresh();
    const index = useMutation({
        mutationFn: () => promoteField(datasetId as number, field),
        onSuccess: refresh,
    });
    if (datasetId === undefined) return null;
    return (
        <>
            <button
                data-testid="index-field"
                className={`${BUTTON} border-yellow-400/40 text-yellow-300 hover:bg-yellow-400/10`}
                disabled={index.isPending || index.isSuccess}
                onClick={() => index.mutate()}
            >
                {index.isPending ? 'Building…' : index.isSuccess ? 'Indexed' : 'Index this field'}
            </button>
            {index.isError && <div className="text-red-300" role="alert">{index.error.message}</div>}
        </>
    );
}

function Removed({ items }: { items: AdvisorRemoval[] }) {
    const refresh = useRefresh();
    // Refreshed after an error too: another administrator may have restored it already (404).
    const restore = useMutation({ mutationFn: (name: string) => restoreIndex(name), onSettled: refresh });
    if (!items.length) return null;
    return (
        <Section testId="index-advisor-removed" title="Removed indexes" count={items.length}>
            <ul className="space-y-2">
                {items.map(item => (
                    <li key={item.id} data-testid="index-advisor-removal" data-index={item.index_name} className="border border-zinc-800 rounded-lg px-3 py-2">
                        <div className="flex flex-wrap items-center gap-2">
                            <span className="font-mono text-zinc-200 break-all">{item.index_name}</span>
                            <span className="text-zinc-400">
                                {item.size_bytes != null && `${formatBytes(item.size_bytes)} · `}removed {when(item.removed_at)}
                                {item.removed_by ? ` by ${item.removed_by}` : ''}
                            </span>
                            <span data-testid="index-removal-state" className="text-zinc-300">
                                {{
                                    removing: 'Removing…',
                                    removed: 'Removed',
                                    restoring: 'Restoring…',
                                    stopped: 'Removal stopped; the index is still there',
                                    back: 'Back: re-created outside the advisor',
                                }[item.state]}
                                {item.state === 'restoring' && item.kind === 'field' && ' (the field index is being built)'}
                            </span>
                            {item.state === 'removed' && (
                                <button
                                    data-testid="index-restore"
                                    className={`ml-auto ${BUTTON} border-cyan-500/40 text-cyan-300 hover:bg-cyan-500/10`}
                                    disabled={restore.isPending}
                                    onClick={() => restore.mutate(item.index_name)}
                                >
                                    Restore
                                </button>
                            )}
                        </div>
                        <div className="text-xs text-zinc-500 font-mono break-all">{item.definition}</div>
                        {item.restore_failed && <div className="text-xs text-red-300">The last restore failed: {item.restore_failed}</div>}
                    </li>
                ))}
            </ul>
            {restore.isError && <p className="text-red-300" role="alert">{restore.error.message}</p>}
        </Section>
    );
}

function Section({ testId, title, count, children }: { testId: string; title: string; count?: number; children: ReactNode }) {
    return (
        <div data-testid={testId} className="space-y-1">
            <h4 className="text-sm font-semibold text-white">
                {title}{count !== undefined && <span className="ml-2 text-xs font-normal text-zinc-400">{count}</span>}
            </h4>
            {children}
        </div>
    );
}

function Table({ head, children }: { head: string[]; children: ReactNode }) {
    return (
        <div className="overflow-x-auto border border-zinc-800 rounded-lg">
            <table className="w-full text-xs">
                <thead>
                    <tr className="bg-zinc-800/50 text-zinc-400 text-left">
                        {head.map(label => <th key={label} className="px-2 py-1.5 font-medium whitespace-nowrap">{label}</th>)}
                    </tr>
                </thead>
                <tbody>{children}</tbody>
            </table>
        </div>
    );
}

function Suggested({ items, buildSeconds, rebuilding }: {
    items: AdvisorSuggestion[];
    buildSeconds: number | null | undefined;
    rebuilding: Set<string>;
}) {
    return (
        <Section testId="index-advisor-suggested" title="Suggested indexes" count={items.length}>
            {items.length === 0 ? (
                <p className="text-zinc-500">No field needs an index: none is filtered, sorted or faceted slowly enough, often enough.</p>
            ) : (
                <Table head={['Field', 'Used (window)', 'Time per use', 'Indexed fields', 'Extra waiting', '']}>
                    {items.map(item => {
                        const usages = Object.entries(item.usages);
                        return (
                            <tr key={item.field} data-testid="index-advisor-suggestion" className="border-t border-zinc-800 align-top">
                                <td className="px-2 py-1.5 font-mono text-zinc-200">
                                    {item.field}
                                    {item.reason === 'index_missing' && (
                                        <div className="font-sans text-amber-300">Marked indexed, but its index is missing</div>
                                    )}
                                </td>
                                <td className="px-2 py-1.5 text-zinc-300">
                                    {usages.map(([usage, value]) => <div key={usage}>{USAGE_LABELS[usage] ?? usage} {value?.uses.toLocaleString()}</div>)}
                                </td>
                                <td className="px-2 py-1.5 text-zinc-300">
                                    {usages.map(([usage, value]) => (
                                        <div key={usage} title={`average ${duration(value?.mean_ms)}`}>
                                            {duration(value?.p50_ms)}, slowest {duration(value?.p95_ms)}
                                        </div>
                                    ))}
                                </td>
                                <td className="px-2 py-1.5 text-zinc-400">
                                    {usages.map(([usage, value]) => <div key={usage}>{duration(value?.reference_ms)}</div>)}
                                </td>
                                <td className="px-2 py-1.5 text-zinc-300">
                                    {item.excess_seconds > 0 ? seconds(item.excess_seconds) : '—'}
                                    {item.build_seconds != null && <div className="text-zinc-500">a build takes ~{seconds(item.build_seconds)}</div>}
                                </td>
                                <td className="px-2 py-1.5 text-right">
                                    {rebuilding.has(item.field)
                                        ? <span data-testid="index-field-rebuilding" className="text-xs text-cyan-300">Being rebuilt</span>
                                        : <IndexField field={item.field} />}
                                </td>
                            </tr>
                        );
                    })}
                </Table>
            )}
            <p className="text-xs text-zinc-500">
                Suggested when a field&apos;s requests are slower than this dataset&apos;s indexed fields and, over the window, people waited
                longer on them than an index takes to build. A request&apos;s time is shared among the fields it filters by.
                {buildSeconds == null && (
                    <span data-testid="index-advisor-build-unmeasured">
                        {' '}Build time not measured yet (it is, the first time a field index is built here): until then only fields whose
                        requests take on average at least twice as long as the reference are listed.
                    </span>
                )}
            </p>
        </Section>
    );
}

function IndexRows({ items, statsSince }: { items: AdvisorIndex[]; statsSince: string }) {
    const [all, setAll] = useState(false);
    const shown = all ? items : items.slice(0, PAGE);
    return (
        <>
            <Table head={['Index', 'Size', `Uses since ${day(statsSince)}`, 'Last use', '']}>
                {shown.map(item => (
                    <tr key={item.name} data-testid="index-advisor-unused" data-index={item.name} className="border-t border-zinc-800 align-top">
                        <td className="px-2 py-1.5">
                            <div className="font-mono text-zinc-200 break-all">{item.name}</div>
                            <div className="text-zinc-500">
                                {item.table} · {item.method}{item.field ? ` · ${item.field}` : ''}
                                {item.observed_since !== statsSince && ` · watched since ${day(item.observed_since)}`}
                                {item.idle_days != null && ` · unused for ${Math.floor(item.idle_days)} days`}
                            </div>
                            {item.orphan && <div className="text-amber-300">Its dataset was deleted</div>}
                            {item.invalid && <div className="text-amber-300">Invalid (left by an interrupted build): never used by queries, still written</div>}
                        </td>
                        <td className="px-2 py-1.5 text-zinc-300 whitespace-nowrap">{formatBytes(item.size_bytes)}</td>
                        <td className="px-2 py-1.5 text-zinc-300">{item.scans.toLocaleString()}</td>
                        <td className="px-2 py-1.5 text-zinc-300 whitespace-nowrap">{item.last_scan ? when(item.last_scan) : 'never'}</td>
                        <td className="px-2 py-1.5 text-right"><RemoveIndex index={item} reason={item.invalid ? 'invalid' : 'unused'} /></td>
                    </tr>
                ))}
            </Table>
            {items.length > PAGE && (
                <button className="text-xs underline text-zinc-300 hover:text-white" onClick={() => setAll(!all)}>
                    {all ? 'Show fewer' : `Show all ${items.length}`}
                </button>
            )}
        </>
    );
}

function Unused({ review }: { review: IndexReview }) {
    const items = review.possibly_unused;
    return (
        <Section testId="index-advisor-possibly-unused" title="Possibly unused indexes" count={items.length}>
            {!review.enough_history && (
                <p data-testid="index-advisor-not-enough-history" className="text-amber-300">
                    Not enough history: index usage is known since {when(review.history_since ?? review.stats_since)}
                    {' '}({days(review.history_since ?? review.stats_since, review.reviewed_at)} days); an index needs{' '}
                    {review.idle_days_required ?? review.window_days} days without use before it is listed here.
                </p>
            )}
            {items.length ? <IndexRows items={items} statsSince={review.stats_since} /> : (
                review.enough_history && (
                    <p className="text-zinc-500" data-testid="index-advisor-none-unused">
                        No index here has gone {review.idle_days_required ?? review.window_days} days without use
                        {review.too_new.length > 0 ? `; ${review.too_new.length} are watched for too short a time to judge` : ''}
                        {review.scope === 'system' ? ' (needed indexes are never listed)' : ''}.
                    </p>
                )
            )}
            {review.too_new.length > 0 && (
                <p className="text-xs text-zinc-500" data-testid="index-advisor-too-new">
                    {review.too_new.length} unused so far but watched for less than {review.idle_days_required ?? review.window_days} days:{' '}
                    {review.too_new.map(item => item.name).join(', ')}
                </p>
            )}
        </Section>
    );
}

function Overlapping({ items }: { items: AdvisorOverlap[] }) {
    return (
        <Section testId="index-advisor-overlapping" title="Overlapping indexes" count={items.length}>
            {items.length === 0 ? <p className="text-zinc-500">No index duplicates another.</p> : (
                <ul className="space-y-2">
                    {items.map(pair => {
                        const preferred = overlapPreferenceNote(pair);
                        return (
                            <li key={pair.redundant.name} data-testid="index-advisor-overlap" data-index={pair.redundant.name} className="border border-zinc-800 rounded-lg px-3 py-2">
                                <div>
                                    <span className="font-mono text-zinc-200 break-all">{pair.redundant.name}</span>
                                    <span className="text-zinc-400"> · {formatBytes(pair.redundant.size_bytes)} · {pair.redundant.scans.toLocaleString()} uses</span>
                                </div>
                                <div className="text-zinc-400">{pair.reason}.</div>
                                <div className="text-zinc-500">
                                    Would be replaced by: <span className="font-mono">{pair.kept.name}</span> · {formatBytes(pair.kept.size_bytes)} · {pair.kept.scans.toLocaleString()} uses
                                </div>
                                {preferred && <div data-testid="index-advisor-overlap-preferred" className="text-amber-300">{preferred}</div>}
                                <div className="mt-1"><RemoveIndex index={pair.redundant} reason="overlap" /></div>
                            </li>
                        );
                    })}
                </ul>
            )}
        </Section>
    );
}

function SystemExtras({ review }: { review: IndexReview }) {
    const missing = review.missing_core ?? [];
    const needed = review.needed ?? [];
    return (
        <>
            {missing.length > 0 && (
                <Section testId="index-advisor-missing-core" title="Missing core indexes" count={missing.length}>
                    <p className="text-amber-300">
                        backend/db/schema.sql defines these, but the database has none of them and they were not removed here:{' '}
                        <span className="font-mono">{missing.map(item => `${item.name} (${item.table})`).join(', ')}</span>
                    </p>
                </Section>
            )}
            {review.building.length > 0 && (
                <p className="text-zinc-400">Being built now: {review.building.map(item => item.name).join(', ')}</p>
            )}
            {needed.length > 0 && (
                <details data-testid="index-advisor-needed" className="text-zinc-400">
                    <summary className="cursor-pointer select-none">{needed.length} indexes are needed and never offered for removal</summary>
                    <ul className="mt-1 space-y-0.5 text-xs">
                        {needed.map(item => (
                            <li key={item.name}><span className="font-mono text-zinc-300">{item.name}</span>: {item.needed.join('; ')}</li>
                        ))}
                    </ul>
                </details>
            )}
        </>
    );
}

function SystemSchedule({ schedule }: { schedule: ReviewSchedule | null }) {
    const queryClient = useQueryClient();
    const [enabled, setEnabled] = useState(schedule?.enabled ?? false);
    const [weekday, setWeekday] = useState(schedule?.weekday ?? 0);
    const [time, setTime] = useState(schedule?.time ?? '03:00');
    const save = useMutation({
        mutationFn: () => saveSystemReviewSchedule({
            enabled,
            frequency: 'weekly',
            weekday,
            time,
            timezone: schedule?.timezone ?? browserTimeZone(),
        }),
        onSuccess: () => queryClient.invalidateQueries({ queryKey: ['index-advisor', 'system'] }),
    });
    const input = 'px-2 py-1 bg-zinc-800 border border-zinc-700 rounded text-white';
    const changed = !schedule || enabled !== schedule.enabled || weekday !== (schedule.weekday ?? 0) || time !== schedule.time;
    return (
        <div data-testid="index-advisor-schedule" className="flex flex-wrap items-center gap-2 text-zinc-300">
            <span>Weekly review</span>
            <select aria-label="Weekly review" value={enabled ? 'on' : 'off'} onChange={event => setEnabled(event.target.value === 'on')} className={input}>
                <option value="off">off</option>
                <option value="on">on</option>
            </select>
            {enabled && (
                <>
                    <select aria-label="Review weekday" value={weekday} onChange={event => setWeekday(Number(event.target.value))} className={input}>
                        {WEEKDAYS.map((name, index) => <option key={name} value={index}>{name}</option>)}
                    </select>
                    <span>at</span>
                    <input aria-label="Review time" type="time" value={time} required onChange={event => setTime(event.target.value)} className={input} />
                </>
            )}
            <button
                onClick={() => save.mutate()}
                disabled={!changed || save.isPending}
                className="px-3 py-1 bg-cyan-600 hover:bg-cyan-500 text-white rounded disabled:opacity-50"
            >
                Save
            </button>
            {schedule?.enabled && schedule.next_run_at && <span className="text-zinc-500">next {when(schedule.next_run_at)}</span>}
            {save.isError && <span className="text-red-300" role="alert">{save.error.message}</span>}
        </div>
    );
}

export function IndexAdvisor({ datasetId }: { datasetId?: number }) {
    const scope = datasetId === undefined ? 'system' : 'dataset';
    const queryClient = useQueryClient();
    const advisor = useIndexAdvisor(datasetId);
    const review = useMutation({
        mutationFn: () => reviewIndexes(datasetId),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['index-advisor', datasetId ?? 'system'] });
            if (datasetId !== undefined) {
                queryClient.invalidateQueries({ queryKey: ['dataset-active-jobs', datasetId] });
                queryClient.invalidateQueries({ queryKey: ['dataset-job-history', datasetId] });
            }
        },
    });
    if (advisor.isError) {
        return (
            <p data-testid={`index-advisor-${scope}`} className="text-sm text-zinc-400">
                Index review unavailable: {advisor.error.message}{' '}
                <button className="underline hover:text-white" onClick={() => advisor.refetch()}>Retry</button>
            </p>
        );
    }
    if (!advisor.data) return <Loader2 className="w-4 h-4 animate-spin text-zinc-500" />;
    const { active_job: active, last_failure: failure, schedule } = advisor.data;
    const result = advisor.data.review;
    const removed = advisor.data.removed ?? [];
    const rebuilding = advisor.data.rebuilding ?? [];
    return (
        <Scope.Provider value={{ datasetId, removed: new Map(removed.map(item => [item.index_name, item])), pending: new Set(advisor.data.pending_actions ?? []) }}>
        <section data-testid={`index-advisor-${scope}`} className="space-y-4 text-sm">
            <div className="flex flex-wrap items-center gap-3">
                <h3 className="text-base font-semibold text-white">Index review</h3>
                <span data-testid="index-review-status" className="text-zinc-400">
                    {active ? (
                        <span className="inline-flex items-center gap-1 text-cyan-300">
                            <Loader2 className="w-3 h-3 animate-spin" />
                            {active.state === 'running' ? 'Reviewing…' : 'Review queued…'}
                        </span>
                    ) : result ? `Last reviewed ${when(result.reviewed_at)}` : 'Not reviewed yet'}
                </span>
                <button
                    data-testid="index-review-now"
                    onClick={() => review.mutate()}
                    disabled={!!active || review.isPending}
                    className="ml-auto px-3 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-white disabled:opacity-50"
                >
                    Review now
                </button>
            </div>
            {review.isError && <p className="text-red-300" role="alert">{review.error.message}</p>}
            {failure && !active && (
                <p className="text-red-300" data-testid="index-review-failure">
                    The last review {failure.state === 'cancelled' ? 'was cancelled' : 'failed'} {when(failure.finished_at)}
                    {failure.message ? `: ${failure.message}` : ''}
                </p>
            )}
            {scope === 'system' ? <SystemSchedule key={JSON.stringify(schedule)} schedule={schedule} /> : (
                <p className="text-zinc-400" data-testid="index-advisor-schedule">
                    Weekly review: {schedule?.enabled ? `on, ${ruleText(schedule)}` : 'off'}. Change it in the dataset&apos;s Jobs → Schedules.
                </p>
            )}
            {rebuilding.length > 0 && (
                <p className="text-cyan-300" data-testid="index-advisor-rebuilding">
                    Being rebuilt in the background: <span className="font-mono">{rebuilding.join(', ')}</span>. Each is not indexed
                    until its new index is built (Jobs shows the build); filters and sorts on it stay correct, only slower.
                </p>
            )}
            {result?.skipped && <p className="text-zinc-400">{result.skipped}</p>}
            {result && !result.skipped && (
                <>
                    <p className="text-xs text-zinc-500" data-testid="index-advisor-window">
                        Index usage statistics since {when(result.stats_since)}
                        {result.scope === 'dataset' && (result.telemetry_since
                            ? ` · this dataset's requests timed since ${day(result.telemetry_since)} (the last ${result.window_days} days are used)`
                            : ' · no requests to this dataset timed yet')}
                    </p>
                    {result.scope === 'dataset' && (
                        <Suggested items={result.suggested ?? []} buildSeconds={result.build_seconds} rebuilding={new Set(rebuilding)} />
                    )}
                    <Unused review={result} />
                    <Overlapping items={result.overlapping} />
                    {!!result.recreated?.length && (
                        <p className="text-amber-300" data-testid="index-advisor-recreated">
                            Back although removed here: {result.recreated.map(item => `${item.index_name} (${item.note})`).join(', ')}.
                        </p>
                    )}
                    {result.scope === 'system' && <SystemExtras review={result} />}
                </>
            )}
            <Removed items={removed} />
        </section>
        </Scope.Provider>
    );
}
