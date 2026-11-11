// A job put back because something it needs is busy (QueueContended: another index build, a database lock, a backup)
// is waiting, not failing. Its last "error" is a status, and its attempts only count how often it looked. How long it
// has waited tells a stuck wait from a fresh one.
export function waitingNote(
    job: { state: string; error?: unknown; started_at?: string | null; created_at: string },
    now: number = Date.now(),
): string | null {
    if (job.state !== 'queued' && job.state !== 'running') return null;
    const error = job.error as { type?: unknown; message?: unknown } | null | undefined;
    if (error == null || typeof error !== 'object' || error.type !== 'QueueContended') return null;
    const since = Date.parse(job.started_at ?? job.created_at);
    const reason = String(error.message ?? 'something it needs is busy').replace(/^waiting\s+/i, '');
    return `Waiting ${waitedFor(now - since)}: ${reason}`;
}

function waitedFor(ms: number): string {
    const minutes = Math.floor(Math.max(0, ms) / 60_000);
    if (minutes < 1) return 'under a minute';
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
}

type IndexJob = { kind: string; state: string; dataset_id: number | null; field?: string | null; created_at: string };

// A failed or cancelled vector index build can be started again by whoever may build one (a power user or an admin),
// unless a later build of the same field is waiting, running or done.
export function canRetryIndexBuild(job: IndexJob, listed: IndexJob[], role: string | undefined): boolean {
    if (job.kind !== 'ann_index_build' || (job.state !== 'failed' && job.state !== 'cancelled')) return false;
    if (!job.field || job.dataset_id == null || (role !== 'admin' && role !== 'power_user')) return false;
    const after = Date.parse(job.created_at);
    return !listed.some(other => other.kind === job.kind && other.dataset_id === job.dataset_id && other.field === job.field
        && Date.parse(other.created_at) > after && ['queued', 'running', 'succeeded'].includes(other.state));
}
