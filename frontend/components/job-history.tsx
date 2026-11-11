'use client';

import { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Cross2Icon } from '@radix-ui/react-icons';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { buildAnnIndex, discardBulkJob, getDatasetJobHistory, getGlobalJobs, resumeBulkJob, type JobRecord, type JobRecordPage } from '@/lib/api';
import { useUser } from '@/contexts/UserContext';
import { canRetryIndexBuild, waitingNote } from '@/lib/job-status';
import { jobName } from '@/components/dataset-jobs';
import { DatasetSchedules } from '@/components/dataset-schedules';

const STATE: Record<JobRecord['state'], { label: string; tone: string }> = {
    queued: { label: 'Waiting', tone: 'text-zinc-300 border-zinc-700' },
    running: { label: 'Running', tone: 'text-cyan-300 border-cyan-500/30' },
    succeeded: { label: 'Succeeded', tone: 'text-green-400 border-green-500/30' },
    failed: { label: 'Failed', tone: 'text-red-300 border-red-500/30' },
    cancelled: { label: 'Cancelled', tone: 'text-zinc-400 border-zinc-700' },
};

function duration(startedAt: string, finishedAt: string) {
    const seconds = Math.max(0, Math.round((Date.parse(finishedAt) - Date.parse(startedAt)) / 1000));
    if (seconds < 60) return `${seconds} s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} min ${seconds % 60} s`;
    return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

// The runner stores {type, message}; anything else is shown as JSON.
function errorText(error: unknown) {
    if (error == null) return null;
    if (typeof error === 'object' && 'message' in error) {
        const { type, message } = error as { type?: unknown; message?: unknown };
        return type ? `${String(type)}: ${String(message)}` : String(message);
    }
    return typeof error === 'string' ? error : JSON.stringify(error);
}

export function BulkJobOutcome({ job, canResume, resume, discard, pending }: {
    job: JobRecord; canResume: boolean; resume: () => void; discard: () => void; pending: boolean;
}) {
    if (job.kind !== 'bulk_mutation') return null;
    const stopped = job.state === 'cancelled' || job.state === 'failed';
    const result = job.result as {
        updated_count?: number; deleted_count?: number; discarded?: { by?: string | null };
    } | null | undefined;
    const count = result?.deleted_count ?? result?.updated_count;
    return <p className="mt-1 text-xs text-zinc-400" role="status">
        {count != null && <span>{count.toLocaleString()} samples {result?.deleted_count != null ? 'deleted' : 'updated'}. </span>}
        {stopped && (result?.discarded
            ? <>Discarded{result.discarded.by ? ` by ${result.discarded.by}` : ''}: completed chunks have been kept and the rest will not run.</>
            : <>Completed chunks have been kept. {canResume && <>
                <button className="underline" disabled={pending} onClick={resume}>Resume</button>{' '}
                <button className="underline" disabled={pending} onClick={discard} title="Keep completed chunks, never run the rest, and stop blocking revert">Discard</button>
            </>}</>)}
    </p>;
}

function JobHistoryItem({ job, listed }: { job: JobRecord; listed: JobRecord[] }) {
    const { user } = useUser();
    const queryClient = useQueryClient();
    const resume = useMutation({
        mutationFn: () => resumeBulkJob(job.id),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['dataset-job-history', job.dataset_id] });
            queryClient.invalidateQueries({ queryKey: ['dataset-active-jobs', job.dataset_id] });
        },
    });
    const discard = useMutation({
        mutationFn: () => discardBulkJob(job.id),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['dataset-job-history', job.dataset_id] });
        },
    });
    const canResume = user?.role === 'admin' || (user?.role === 'power_user' && user?.id === job.created_by);
    const retry = useMutation({
        mutationFn: () => buildAnnIndex(job.dataset_id as number, job.field as string),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['dataset-job-history', job.dataset_id] });
            queryClient.invalidateQueries({ queryKey: ['dataset-active-jobs', job.dataset_id] });
            queryClient.invalidateQueries({ queryKey: ['global-jobs'] });
        },
    });
    const canRetry = canRetryIndexBuild(job, listed, user?.role);
    const state = job.state === 'running' && job.cancel_requested
        ? { label: 'Cancelling', tone: STATE.cancelled.tone }
        : STATE[job.state];
    const waiting = waitingNote(job);
    const error = waiting ? null : errorText(job.error);
    return (
        <li data-testid="job-history-item" data-job-id={job.id} className="py-2 border-b border-zinc-800 last:border-0">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-400">
                <span className="text-sm text-zinc-200">{jobName(job.kind)}</span>
                {job.field && <span className="font-mono text-zinc-300">{job.field}</span>}
                <span className={`px-1.5 py-0.5 rounded border ${state.tone}`}>{state.label}</span>
                {job.started_by && <span>by {job.started_by}</span>}
                <span>{new Date(job.started_at ?? job.created_at).toLocaleString()}</span>
                {job.started_at && job.finished_at && <span>took {duration(job.started_at, job.finished_at)}</span>}
                {job.state === 'running' && job.progress.total != null && (
                    <span>{job.progress.done.toLocaleString()} / {job.progress.total.toLocaleString()}</span>
                )}
                {job.attempt > 1 && !waiting && <span>{job.attempt} attempts</span>}
            </div>
            <BulkJobOutcome
                job={job}
                canResume={canResume}
                resume={() => resume.mutate()}
                discard={() => discard.mutate()}
                pending={resume.isPending || discard.isPending}
            />
            {job.kind === 'map_refit' && job.progress.message && job.state !== 'running' && (
                <p className="mt-1 text-xs text-zinc-400" data-testid="map-refit-outcome">{job.progress.message}</p>
            )}
            {canRetry && (
                <p className="mt-1 text-xs text-zinc-400">
                    <button className="underline hover:text-white" disabled={retry.isPending} onClick={() => retry.mutate()} data-testid="job-retry-index">
                        {retry.isPending ? 'Starting…' : 'Retry'}
                    </button>{' '}building this index
                </p>
            )}
            {retry.error && <p role="alert" className="mt-1 text-xs text-red-300/90">{retry.error.message}</p>}
            {resume.error && <p role="alert">{resume.error.message}</p>}
            {discard.error && <p role="alert">{discard.error.message}</p>}
            {waiting && <p className="mt-1 text-xs text-zinc-400" data-testid="job-waiting">{waiting}</p>}
            {error && <p className="mt-1 text-xs text-red-300/90 font-mono whitespace-pre-wrap break-words">{error}</p>}
        </li>
    );
}

export function JobHistoryList({ queryKey, fetchPage, emptyText, poll = false }: {
    queryKey: readonly unknown[];
    fetchPage: (cursor?: string) => Promise<JobRecordPage>;
    emptyText: string;
    poll?: boolean;
}) {
    const [cursors, setCursors] = useState<string[]>([]);
    const cursor = cursors.at(-1);
    const page = useQuery({
        queryKey: [...queryKey, cursor],
        queryFn: () => fetchPage(cursor),
        placeholderData: keepPreviousData,
        refetchInterval: poll ? 10_000 : false,
        refetchIntervalInBackground: false,
        retry: false,
        staleTime: 0,
        meta: { errorHandledLocally: true },
    });
    if (page.isError) {
        return (
            <p className="text-sm text-zinc-400" data-testid="job-history-unavailable">
                Job history unavailable. <button className="underline hover:text-white" onClick={() => page.refetch()}>Retry</button>
            </p>
        );
    }
    if (!page.data) return <p className="text-sm text-zinc-500">Loading job history…</p>;
    const nextCursor = page.data.next_cursor;
    return (
        <div data-testid="job-history">
            {page.data.items.length ? (
                <ul>{page.data.items.map(job => <JobHistoryItem key={job.id} job={job} listed={page.data.items} />)}</ul>
            ) : (
                <p className="text-sm text-zinc-500">{cursor ? 'No older jobs.' : emptyText}</p>
            )}
            {(cursor || nextCursor) && (
                <div className="mt-3 flex items-center gap-3 text-xs text-zinc-400">
                    {cursor && (
                        <button className="underline hover:text-white" disabled={page.isFetching} onClick={() => setCursors(previous => previous.slice(0, -1))}>
                            Newer jobs
                        </button>
                    )}
                    {nextCursor && (
                        <button className="underline hover:text-white" disabled={page.isFetching} onClick={() => setCursors(previous => [...previous, nextCursor])}>
                            Older jobs
                        </button>
                    )}
                </div>
            )}
        </div>
    );
}

// Dialog content unmounts while closed, so history and schedules are only fetched and polled while open.
export function DatasetJobHistoryDialog({ datasetId, open, onOpenChange }: {
    datasetId: number;
    open: boolean;
    onOpenChange: (open: boolean) => void;
}) {
    const [tab, setTab] = useState<'history' | 'schedules'>('history');
    const tabClass = (name: typeof tab) => `px-3 py-1 text-sm border-b-2 ${tab === name
        ? 'border-cyan-400 text-white' : 'border-transparent text-zinc-400 hover:text-white'}`;
    return (
        <Dialog.Root open={open} onOpenChange={onOpenChange}>
            <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50" />
                <Dialog.Content
                    data-testid="job-history-dialog"
                    className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-zinc-900 border border-zinc-800 p-6 rounded-lg shadow-xl w-[calc(100%-2rem)] max-w-2xl max-h-[80vh] flex flex-col z-50 focus:outline-none"
                >
                    <div className="flex justify-between items-center mb-4">
                        <Dialog.Title className="text-xl font-semibold text-white">Jobs</Dialog.Title>
                        <Dialog.Close asChild>
                            <button className="text-zinc-400 hover:text-white transition-colors" aria-label="Close">
                                <Cross2Icon className="w-4 h-4" />
                            </button>
                        </Dialog.Close>
                    </div>
                    <div role="tablist" className="flex gap-1 border-b border-zinc-800 mb-3">
                        <button role="tab" aria-selected={tab === 'history'} data-testid="jobs-history-tab" className={tabClass('history')} onClick={() => setTab('history')}>
                            History
                        </button>
                        <button role="tab" aria-selected={tab === 'schedules'} data-testid="jobs-schedules-tab" className={tabClass('schedules')} onClick={() => setTab('schedules')}>
                            Schedules
                        </button>
                    </div>
                    <Dialog.Description className="text-sm text-zinc-400 mb-3">
                        {tab === 'history' ? 'Background work on this dataset, newest first.' : 'Jobs that run on this dataset on a schedule.'}
                    </Dialog.Description>
                    <div className="overflow-auto">
                        {tab === 'history' ? (
                            <JobHistoryList
                                queryKey={['dataset-job-history', datasetId]}
                                fetchPage={cursor => getDatasetJobHistory(datasetId, cursor)}
                                emptyText="No jobs have run on this dataset yet."
                                poll
                            />
                        ) : <DatasetSchedules datasetId={datasetId} />}
                    </div>
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}

export function GlobalJobs() {
    return (
        <section data-testid="status-global-jobs" className="p-5 bg-zinc-900/50 border border-zinc-800 rounded-xl">
            <h2 className="text-lg font-semibold text-white">Background jobs</h2>
            <p className="text-sm text-zinc-400 mb-2">Jobs that belong to no dataset, such as scheduled cleanup. Newest first.</p>
            <JobHistoryList queryKey={['global-jobs']} fetchPage={getGlobalJobs} emptyText="No background jobs have run yet." />
        </section>
    );
}
