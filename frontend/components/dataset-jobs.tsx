'use client';

import { useEffect, useRef, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity } from 'lucide-react';
import { useToast } from '@/contexts/ToastContext';
import { ApiError, cancelJob, getActiveDatasetJobs, getDatasetJobHistory, JOB_SUBMITTED_EVENT, notifySamplesMutated, type ActiveDatasetJob } from '@/lib/api';
import { SAMPLE_COUNT_KEY } from '@/hooks/useSampleCount';

const NAMES: Record<string, string> = {
    dataset_deletion: 'Deleting dataset',
    bulk_mutation: 'Editing or deleting samples',
    concept_rescore: 'Scoring concept',
    field_index_build: 'Building field index',
    ann_index_build: 'Building ANN index',
    statistics: 'Updating statistics',
    concept_scoring: 'Scoring concepts',
    duplicate_maintenance: 'Updating duplicate groups',
    refresh_stats: 'Updating statistics',
    statistics_refresh: 'Updating statistics',
    create_version: 'Creating version',
    revert_version: 'Reverting version',
    bulk_tag: 'Bulk tagging',
    umap_fit: 'Fitting UMAP',
    map_refit: 'Re-fitting the map',
    map_place: 'Placing new points on the map',
    backup: 'Backing up the database',
    index_review: 'Reviewing indexes',
    index_drop: 'Removing an index',
    index_restore: 'Restoring an index',
};

// Jobs whose end changes what the page shows: their completion refreshes the dataset's views,
// including jobs submitted elsewhere (SDK, another tab) that finish between active-job polls.
const REFRESHING_KINDS = new Set(['bulk_mutation', 'concept_rescore', 'field_index_build', 'ann_index_build', 'duplicate_maintenance', 'index_drop']);

export function jobName(kind: string) {
    return NAMES[kind] || kind.replace(/[_-]+/g, ' ');
}

function name(job: ActiveDatasetJob) {
    return jobName(job.kind);
}

function stateLabel(job: ActiveDatasetJob, owners: ActiveDatasetJob[]) {
    if (job.stalled) return job.cancel_requested ? 'Stalled — cancellation pending' : 'Stalled — waiting for a worker';
    if (job.cancel_requested) return 'Cancellation requested';
    if (job.state === 'running') return 'Running';
    if (job.retry_delayed) return 'Waiting to retry';
    if (owners.some(owner => owner.id === job.id)) {
        return job.placement === 'compute' ? 'Waiting to resume on compute' : 'Waiting to resume';
    }
    // A job that owns no dataset lane (an index review) neither waits behind the lane's owner nor blocks it.
    const blocking = job.dataset_lane === false ? undefined
        : owners.find(owner => owner.lane === job.lane && owner.dataset_lane !== false);
    if (blocking) return `Waiting behind ${blocking.stalled ? 'stalled ' : ''}${name(blocking)}`;
    return job.placement === 'compute' ? 'Queued for compute' : 'Waiting for an execution slot';
}

function CancelJob({ job, datasetId }: { job: ActiveDatasetJob; datasetId: number }) {
    const queryClient = useQueryClient();
    const { addToast } = useToast();
    const [confirming, setConfirming] = useState(false);
    // isPending only updates on the next render, so a fast double click would send two requests.
    const sending = useRef(false);
    const cancel = useMutation({
        mutationFn: () => cancelJob(job.id),
        onSettled: () => { sending.current = false; },
        onSuccess: result => {
            if (result.state === 'succeeded' || result.state === 'failed') {
                addToast(`${jobName(job.kind)} had already finished before it could be cancelled`, 'info', 5000);
            }
            setConfirming(false);
            queryClient.invalidateQueries({ queryKey: ['dataset-active-jobs', datasetId] });
            queryClient.invalidateQueries({ queryKey: ['dataset-job-history', datasetId] });
        },
    });
    if (!job.can_cancel || job.cancel_requested) return null;
    if (!confirming) {
        return <button className="underline hover:text-white" onClick={() => setConfirming(true)}>Cancel</button>;
    }
    return (
        <span className="flex flex-wrap items-center gap-2" data-testid="cancel-job-confirm">
            <span className="text-zinc-200">{job.kind === 'bulk_mutation' ? 'Stop after this chunk? Changes already made will be kept.' : job.state === 'running' ? 'Stop this job after its current step?' : 'Remove this job from the queue?'}</span>
            <button
                className="underline text-red-300 hover:text-red-200"
                disabled={cancel.isPending}
                onClick={() => {
                    if (sending.current) return;
                    sending.current = true;
                    cancel.mutate();
                }}
            >
                {cancel.isPending ? 'Cancelling…' : 'Cancel job'}
            </button>
            <button className="underline hover:text-white" disabled={cancel.isPending} onClick={() => { setConfirming(false); cancel.reset(); }}>
                Keep it
            </button>
            {cancel.isError && (
                <span className="text-red-300" role="status">
                    {cancel.error instanceof ApiError && cancel.error.status === 409
                        ? 'The job is busy; try again in a moment.'
                        : cancel.error.message}
                </span>
            )}
        </span>
    );
}

export function DatasetJobs({ datasetId, onShowHistory }: { datasetId: number; onShowHistory?: () => void }) {
    const queryClient = useQueryClient();
    const [cursors, setCursors] = useState<string[]>([]);
    const cursor = cursors.at(-1);
    const jobs = useQuery({
        queryKey: ['dataset-active-jobs', datasetId, cursor],
        queryFn: () => getActiveDatasetJobs(datasetId, cursor),
        placeholderData: keepPreviousData,
        refetchInterval: 5_000,
        refetchIntervalInBackground: false,
        retry: false,
        staleTime: 0,
        gcTime: 30_000,
        meta: { errorHandledLocally: true },
    });
    const refreshed = useRef({ datasetId, initialized: false, keys: new Set<string>() });
    const hadBulk = useRef(false);
    const active = [...(jobs.data?.running ?? []), ...(jobs.data?.resuming ?? []), ...(jobs.data?.waiting ?? [])];
    // Routine (System) upkeep is excluded: only work a person asked for speeds up history polling.
    const hasBulk = active.some(job => REFRESHING_KINDS.has(job.kind) && job.started_by !== 'System');
    // A map re-fit's end changes the embeddings map: read history at once to see it finished.
    const watched = hasBulk || active.some(job => job.kind === 'map_refit');
    useEffect(() => {
        if (typeof window === 'undefined') return;
        // A long action answered 202 here: show its job now rather than at the next poll.
        const submitted = (event: Event) => {
            if ((event as CustomEvent<{ datasetId: number }>).detail?.datasetId !== datasetId) return;
            queryClient.invalidateQueries({ queryKey: ['dataset-active-jobs', datasetId] });
            queryClient.invalidateQueries({ queryKey: ['dataset-job-history', datasetId] });
        };
        window.addEventListener(JOB_SUBMITTED_EVENT, submitted);
        return () => window.removeEventListener(JOB_SUBMITTED_EVENT, submitted);
    }, [datasetId, queryClient]);
    useEffect(() => {
        if (hadBulk.current && !watched) {
            queryClient.invalidateQueries({ queryKey: ['dataset-job-history', datasetId] });
        }
        hadBulk.current = watched;
    }, [watched, datasetId, queryClient]);
    const recent = useQuery({
        queryKey: ['dataset-job-history', datasetId, undefined],
        queryFn: () => getDatasetJobHistory(datasetId),
        // Slowly while idle, so fast jobs submitted elsewhere (SDK, another tab) still refresh the page.
        refetchInterval: hasBulk ? 5_000 : 30_000,
        // Shares its cache entry with the job-history dialog's first page, so it uses the same error
        // options: the dialog shows an outage at once and in place (no retries, no global toast).
        retry: false,
        meta: { errorHandledLocally: true },
    });
    useEffect(() => {
        if (!recent.data) return;
        if (refreshed.current.datasetId !== datasetId) {
            refreshed.current = { datasetId, initialized: false, keys: new Set<string>() };
        }
        let changed = false;
        let mapChanged = false;
        for (const job of recent.data?.items ?? []) {
            const key = `${job.id}:${job.finished_at}`;
            if (REFRESHING_KINDS.has(job.kind) && ['succeeded', 'failed', 'cancelled'].includes(job.state) && !refreshed.current.keys.has(key)) {
                refreshed.current.keys.add(key);
                changed = refreshed.current.initialized;
            }
            // A re-fit whose rank step failed has still swapped the new map in: refresh on any end.
            if (job.kind === 'map_refit' && ['succeeded', 'failed'].includes(job.state) && !refreshed.current.keys.has(key)) {
                refreshed.current.keys.add(key);
                mapChanged = mapChanged || refreshed.current.initialized;
            }
        }
        refreshed.current.initialized = true;
        if (changed) {
            notifySamplesMutated(datasetId);  // also asks every count again (SAMPLE_COUNT_KEY)
            queryClient.invalidateQueries({ predicate: query => query.queryKey[1] === datasetId && query.queryKey[0] !== SAMPLE_COUNT_KEY && !String(query.queryKey[0]).includes('job') });
        } else if (mapChanged) {
            queryClient.invalidateQueries({ queryKey: ['embeddings', datasetId] });
        }
    }, [recent.data, datasetId, queryClient]);
    if (cursor && jobs.isSuccess && !jobs.isPlaceholderData && jobs.data.waiting.length === 0) {
        setCursors([]);
    }
    if (jobs.isError) {
        return (
            <div className="px-6 py-1 text-xs text-zinc-500" data-testid="dataset-jobs-unavailable">
                Job status unavailable. <button className="underline" onClick={() => jobs.refetch()}>Retry</button>
            </div>
        );
    }
    const owners = [...(jobs.data?.running || []), ...(jobs.data?.resuming || [])];
    const seen = new Set(owners.map(job => job.id));
    const waiting = (jobs.data?.waiting || []).filter(job => {
        if (seen.has(job.id)) return false;
        seen.add(job.id);
        return true;
    });
    if (!owners.length && !waiting.length) return null;
    const nextCursor = jobs.data?.next_cursor;

    return (
        <details className="border-b border-zinc-800 px-6 py-1.5 text-xs text-zinc-400 shrink-0" data-testid="dataset-jobs">
            <summary className="cursor-pointer select-none">
                <Activity size={12} className="inline-block mr-2" aria-hidden="true" />
                {owners.length ? owners.map(name).join(' · ') : 'Dataset jobs'}
                {waiting.length > 0 && ` · ${waiting.length}${nextCursor ? '+' : ''} waiting${cursor ? ' on this page' : ''}`}
            </summary>
            <div className="mt-2 max-h-64 overflow-auto space-y-2">
                {[...owners, ...waiting].map(job => (
                    <div key={job.id} data-testid="dataset-job" data-job-id={job.id} className="flex flex-wrap items-center gap-x-3 gap-y-1">
                        <span className="text-zinc-200">{name(job)}</span>
                        <span>{stateLabel(job, owners)}</span>
                        <span>by {job.started_by}</span>
                        <span>{job.lane === 'maintenance' ? 'Maintenance' : 'User work'}</span>
                        {job.progress.total != null ? (
                            <span>{job.progress.done.toLocaleString()} / {job.progress.total.toLocaleString()}</span>
                        ) : job.progress.done > 0 ? <span>{job.progress.done.toLocaleString()} complete</span> : null}
                        {job.progress.message && <span>{job.progress.message}</span>}
                        <CancelJob job={job} datasetId={datasetId} />
                    </div>
                ))}
                {(cursor || nextCursor) && <div className="flex items-center gap-3">
                    {cursor && <button className="underline" disabled={jobs.isFetching} onClick={() => setCursors(previous => previous.slice(0, -1))}>
                        Previous waiting jobs
                    </button>}
                    <span>Page {cursors.length + 1}</span>
                    {nextCursor && <button className="underline" disabled={jobs.isFetching} onClick={() => setCursors(previous => [...previous, nextCursor])}>
                        Next waiting jobs
                    </button>}
                </div>}
                <p className="text-zinc-500">
                    Ordinary edits remain available. Whole-dataset jobs wait for their execution lane.
                    {onShowHistory && <> <button className="underline hover:text-white" onClick={onShowHistory}>Job history</button></>}
                </p>
            </div>
        </details>
    );
}
