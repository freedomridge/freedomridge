'use client';

import { useEffect } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getJob, resumeDatasetDeletion, type Dataset, type JobRecord } from '@/lib/api';
import { useUser } from '@/contexts/UserContext';

export function DeletionMessage({ job, canResume, resume, pending }: {
    job?: JobRecord; canResume: boolean; resume: () => void; pending: boolean;
}) {
    const stopped = job?.state === 'cancelled' || job?.state === 'failed';
    if (job?.state === 'succeeded') return <p role="status">Dataset deleted.</p>;
    return <>
        <p role="status">{stopped ? 'Deletion stopped. This dataset remains read-only.' : 'Dataset deletion in progress. Writes are disabled.'}</p>
        {job?.progress && <p>{job.progress.message} · {job.progress.done.toLocaleString()} rows removed</p>}
        {stopped && <p>Already removed data cannot be recovered.</p>}
        {stopped && canResume && <button className="underline" onClick={resume} disabled={pending}>
            {pending ? 'Resuming…' : 'Resume deletion'}
        </button>}
    </>;
}

export function DatasetDeletion({ dataset }: { dataset: Dataset }) {
    const queryClient = useQueryClient();
    const { user } = useUser();
    const queryKey = ['dataset-deletion', dataset.deletion_job_id];
    const status = useQuery({
        queryKey,
        queryFn: () => getJob(dataset.deletion_job_id!),
        enabled: !!dataset.deletion_job_id,
        refetchInterval: query => ['succeeded', 'failed', 'cancelled'].includes(query.state.data?.state ?? '') ? false : 2000,
    });
    const resume = useMutation({
        mutationFn: () => resumeDatasetDeletion(dataset.id),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey });
            queryClient.invalidateQueries({ queryKey: ['dataset-active-jobs', dataset.id] });
        },
    });
    useEffect(() => {
        if (status.data?.state === 'succeeded') {
            queryClient.invalidateQueries({ queryKey: ['datasets'] });
        }
    }, [status.data?.state, queryClient]);
    return <div className="border border-amber-800/50 bg-amber-950/20 p-3 text-sm text-amber-200 space-y-1">
        <DeletionMessage job={status.data} canResume={user?.role === 'admin'} resume={() => resume.mutate()} pending={resume.isPending} />
        {(status.error || resume.error) && <p role="alert">{(resume.error || status.error)?.message}</p>}
    </div>;
}
