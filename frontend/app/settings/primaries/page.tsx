'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { Header } from '@/components/header';
import { PermissionGate } from '@/components/PermissionGate';
import { ProtectedRoute } from '@/components/ProtectedRoute';

// This instance as a follower (docs/FOLLOWERS.md): the primaries it follows and its mirrors of their datasets.
interface Mirror {
    dataset_id: number;
    name: string;
    origin_dataset_id: number;
    state: 'copying' | 'following' | 'stopped';
    tags: 'primary' | 'both';
    views: 'primary' | 'both';
    images: boolean;
    copy_total: number;
    copy_left: number;
    samples: number;
    concept_views: string[];
    last_synced_at: string | null;
    last_error: string | null;
    retry_after: string | null;
    stopped_at: string | null;
    keep_separate: boolean;
    resumable: boolean;  // a new copy of its dataset from its primary resumes into it
    restore: Restore | null;  // held after its primary was restored, until its admin chooses
}
// A copy held after its primary was restored into a new database: compared with it, then a question (docs/FOLLOWERS.md).
interface Restore {
    origin_name: string | null;
    hash_field: string | null;
    total: number;
    matched: number;
    matched_hash: number;
    only_here: number | null;
    phase: string;
    decision: 'resume' | 'new' | 'drop' | 'redo' | null;
    compared: boolean;
    last_error: string | null;  // a step failed: tried again later, as a mirror is
    retry_after: string | null;
}
interface Primary {
    id: number;
    url: string;
    follower_name: string | null;
    created_at: string;
    last_synced_at: string | null;
    last_error: string | null;
    mirrors: Mirror[];
}

const STATE_STYLES: Record<Mirror['state'], string> = {
    copying: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
    following: 'bg-green-500/10 text-green-400 border-green-500/30',
    stopped: 'bg-zinc-800 text-zinc-400 border-zinc-700',
};

async function send(method: string, path: string, body?: unknown) {
    const res = await fetch(`/api${path}`, {
        method,
        credentials: 'include',
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) {
        const detail = (await res.json().catch(() => ({}))).detail;
        throw new Error(typeof detail === 'string' ? detail : `${method} ${path} failed: ${res.status}`);
    }
    return res.json();
}

function RestoreQuestion({ mirror, restore }: { mirror: Mirror; restore: Restore }) {
    const queryClient = useQueryClient();
    const decide = useMutation({
        mutationFn: (decision: 'resume' | 'new') =>
            send('POST', `/primaries/mirrors/${mirror.dataset_id}/restore-decision`, { decision }),
        onSuccess: () => queryClient.invalidateQueries({ queryKey: ['primaries'] }),
    });
    const [confirming, setConfirming] = useState(false);
    const fresh = restore.total - restore.matched;
    const failed = restore.last_error && (
        <p data-testid="mirror-restore-error" className="text-xs text-red-300">
            {restore.last_error}
            {restore.retry_after && new Date(restore.retry_after) > new Date() &&
                ` (next try at ${new Date(restore.retry_after).toLocaleTimeString()})`}
        </p>
    );
    if (restore.decision === 'redo' || !restore.compared) {
        return (
            <div data-testid="mirror-restore" className="text-xs text-amber-200 space-y-1">
                <p>
                    The primary was restored from a backup. Comparing this copy with it
                    {restore.total > 0 ? ` (${restore.total.toLocaleString()} samples so far)` : ''}…
                </p>
                {failed}
            </div>
        );
    }
    if (restore.decision === 'resume') {
        return <p data-testid="mirror-restore" className="text-xs text-amber-200">Resuming into this copy…</p>;
    }
    if (restore.decision === 'new') {
        return (
            <p data-testid="mirror-restore" className="text-xs text-zinc-400">
                Making a new copy: this one stays as it is.
            </p>
        );
    }
    if (restore.decision === 'drop') return null;
    return (
        <div data-testid="mirror-restore" className="text-xs text-amber-100 space-y-2 border border-amber-500/30 rounded p-2">
            <p>
                The primary was restored from a backup, so this copy can&apos;t simply carry on.{' '}
                <span data-testid="mirror-restore-counts">
                    {restore.matched.toLocaleString()} of {restore.total.toLocaleString()} samples
                    {restore.origin_name ? ` in “${restore.origin_name}”` : ''} match it by file path
                    {restore.hash_field ? ` (${restore.matched_hash.toLocaleString()} also by image hash)` : ''}.
                </span>{' '}
                Resuming keeps this instance&apos;s own data on the matched ones: its own vector fields, views and
                concepts.
                {fresh > 0 ? ` ${fresh.toLocaleString()} don't match: they're copied fresh.` : ''}
                {restore.only_here ? ` ${restore.only_here.toLocaleString()} here that the primary no longer has go.` : ''}
            </p>
            {failed}
            {confirming && restore.only_here ? (
                <div data-testid="mirror-restore-confirm" className="flex flex-wrap items-center gap-2">
                    <span>
                        Resuming deletes {restore.only_here.toLocaleString()} samples here that the primary doesn&apos;t have,
                        with what this instance added to them.
                    </span>
                    <button
                        data-testid="mirror-restore-confirm-resume"
                        onClick={() => decide.mutate('resume')}
                        disabled={decide.isPending}
                        className="px-3 py-1 rounded bg-red-600 hover:bg-red-500 text-white disabled:opacity-50"
                    >
                        Resume, and delete {restore.only_here.toLocaleString()} samples here
                    </button>
                    <button onClick={() => setConfirming(false)} className="text-zinc-300 hover:text-white underline">
                        Cancel
                    </button>
                </div>
            ) : null}
            <div className="flex flex-wrap gap-2">
                <button
                    data-testid="mirror-restore-resume"
                    onClick={() => (restore.only_here ? setConfirming(true) : decide.mutate('resume'))}
                    disabled={decide.isPending || restore.matched === 0 || confirming}
                    title={restore.matched === 0 ? 'Nothing in this copy matches the restored primary: make a new copy' : undefined}
                    className="px-3 py-1 rounded bg-cyan-600 hover:bg-cyan-500 text-white disabled:opacity-50"
                >
                    Resume into this copy
                </button>
                <button
                    data-testid="mirror-restore-new"
                    onClick={() => decide.mutate('new')}
                    disabled={decide.isPending}
                    className="px-3 py-1 rounded border border-zinc-600 text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
                >
                    Make a new copy
                </button>
                {decide.error && <span className="text-red-300">{(decide.error as Error).message}</span>}
            </div>
        </div>
    );
}

function MirrorRow({ mirror }: { mirror: Mirror }) {
    const queryClient = useQueryClient();
    const keep = useMutation({
        mutationFn: () => send('POST', `/primaries/mirrors/${mirror.dataset_id}/keep-separate`),
        onSuccess: () => queryClient.invalidateQueries({ queryKey: ['primaries'] }),
    });
    const held = mirror.restore !== null && mirror.restore.decision !== 'drop';
    const copied = mirror.copy_total - mirror.copy_left;
    return (
        <div data-testid="mirror-row" className="text-sm space-y-1">
            <div className="flex flex-wrap items-center gap-3">
                <Link href={`/datasets/${mirror.dataset_id}`} className="text-cyan-400 hover:underline">{mirror.name}</Link>
                <span
                    data-testid="mirror-state"
                    className={`px-2 py-0.5 text-xs font-semibold rounded border ${held ? STATE_STYLES.copying : STATE_STYLES[mirror.state]}`}
                >
                    {held
                        ? 'On hold'
                        : mirror.state === 'copying'
                            ? `Copying ${copied.toLocaleString()} of ${mirror.copy_total.toLocaleString()} blocks`
                            : mirror.state === 'following' ? 'Following' : 'No longer following'}
                </span>
                <span className="text-zinc-500">{mirror.samples.toLocaleString()} samples</span>
                <span className="text-zinc-500">
                    Tags {mirror.tags === 'both' ? 'both ways' : 'from the primary'}, views{' '}
                    {mirror.views === 'both' ? 'both ways' : 'from the primary'}
                    {mirror.images ? ', images the primary serves when this instance can\'t read them' : ''}
                </span>
                {mirror.last_synced_at && (
                    <span className="text-zinc-500">synced {new Date(mirror.last_synced_at).toLocaleString()}</span>
                )}
            </div>
            {mirror.concept_views.length > 0 && (
                <p className="text-xs text-zinc-400">
                    Not copied, because they use a concept (each instance has its own) or a range of sample ids (numbered
                    differently here): {mirror.concept_views.join(', ')}
                </p>
            )}
            {held && mirror.restore && <RestoreQuestion mirror={mirror} restore={mirror.restore} />}
            {mirror.state === 'stopped' && !held && (
                <p data-testid="mirror-resume" className="text-xs text-zinc-400 flex flex-wrap items-center gap-2">
                    {mirror.resumable ? (
                        <>
                            <span>
                                If its primary shares this dataset again, the copy resumes here: this instance&apos;s
                                own fields stay, and samples gone from the primary go.
                            </span>
                            <button
                                data-testid="mirror-keep-separate"
                                onClick={() => keep.mutate()}
                                disabled={keep.isPending}
                                className="text-zinc-300 hover:text-white underline"
                            >
                                Keep as a separate copy
                            </button>
                        </>
                    ) : mirror.keep_separate ? (
                        <span>Kept as a separate copy: a new copy of its dataset makes a new dataset.</span>
                    ) : null}
                    {keep.error && <span className="text-red-300">{(keep.error as Error).message}</span>}
                </p>
            )}
            {mirror.last_error && !held && (
                <p data-testid="mirror-error" className="text-xs text-red-300">
                    {mirror.last_error}
                    {mirror.retry_after && new Date(mirror.retry_after) > new Date() &&
                        ` (next try at ${new Date(mirror.retry_after).toLocaleTimeString()})`}
                </p>
            )}
        </div>
    );
}

// What Stop following answers: whether the primary heard, so it stops keeping changes for this instance.
interface Removed {
    removed: number;
    told_primary: boolean;
    detail?: string;
}

function PrimaryCard({ primary, onRemoved }: { primary: Primary; onRemoved: (url: string, result: Removed) => void }) {
    const queryClient = useQueryClient();
    const [confirmRemove, setConfirmRemove] = useState(false);
    const remove = useMutation({
        mutationFn: (): Promise<Removed> => send('DELETE', `/primaries/${primary.id}`),
        onSuccess: result => {
            onRemoved(primary.url, result);
            queryClient.invalidateQueries({ queryKey: ['primaries'] });
        },
    });
    return (
        <div data-testid="primary-row" className="p-4 border border-zinc-800 rounded-xl bg-zinc-900 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <h3 className="font-semibold text-white break-all">{primary.url}</h3>
                    <p className="text-xs text-zinc-500">
                        Following as <span className="text-zinc-300">{primary.follower_name}</span>
                        {' · '}
                        {primary.last_synced_at ? `last synced ${new Date(primary.last_synced_at).toLocaleString()}` : 'not synced yet'}
                    </p>
                </div>
                {confirmRemove ? (
                    <span className="flex items-center gap-2 text-sm">
                        <span className="text-zinc-300">Stop following? The copies stay here, read-only.</span>
                        <button onClick={() => remove.mutate()} className="px-3 py-1 rounded bg-red-600 hover:bg-red-500 text-white">
                            Stop following
                        </button>
                        <button onClick={() => setConfirmRemove(false)} className="text-zinc-400 hover:text-white">Cancel</button>
                    </span>
                ) : (
                    <button onClick={() => setConfirmRemove(true)} className="text-sm text-zinc-400 hover:text-red-300">
                        Stop following…
                    </button>
                )}
            </div>
            {primary.last_error && <p data-testid="primary-error" className="text-sm text-red-300">{primary.last_error}</p>}
            {primary.mirrors.length === 0 ? (
                <p className="text-sm text-zinc-500">
                    The primary shares no datasets with this follower yet. Its admin chooses them under Settings → Followers there.
                </p>
            ) : (
                <div className="space-y-2">{primary.mirrors.map(mirror => <MirrorRow key={mirror.dataset_id} mirror={mirror} />)}</div>
            )}
            {remove.error && <p className="text-sm text-red-300">{(remove.error as Error).message}</p>}
        </div>
    );
}

export default function PrimariesPage() {
    const queryClient = useQueryClient();
    const [url, setUrl] = useState('');
    const [token, setToken] = useState('');
    const { data, isLoading, isError } = useQuery<{ primaries: Primary[]; unlinked_mirrors: Mirror[] }>({
        queryKey: ['primaries'],
        queryFn: async () => {
            const res = await fetch('/api/primaries', { credentials: 'include' });
            if (!res.ok) throw new Error('Failed to load the primaries');
            return res.json();
        },
        refetchInterval: 15_000,
    });
    // The card goes with its primary, so what Stop following answered shows here.
    const [stopped, setStopped] = useState<{ url: string; result: Removed } | null>(null);
    const add = useMutation({
        mutationFn: () => send('POST', '/primaries', { url: url.trim(), token: token.trim() }),
        onSuccess: () => {
            setUrl('');
            setToken('');
            queryClient.invalidateQueries({ queryKey: ['primaries'] });
        },
    });

    return (
        <ProtectedRoute>
            <div className="min-h-screen bg-zinc-950 flex flex-col">
                <Header />
                <main className="flex-1 container mx-auto px-6 py-8 max-w-4xl">
                    <Link href="/settings" className="inline-flex items-center gap-1 text-sm text-zinc-400 hover:text-white mb-4">
                        <ArrowLeft size={14} /> Back to Settings
                    </Link>
                    <h1 className="text-3xl font-bold text-white">Primaries</h1>
                    <p className="text-zinc-400 mt-1 mb-6">
                        Freedom Ridge instances this one follows. A primary&apos;s admin chooses which datasets it shares with this
                        follower; each arrives here as a read-only mirror, kept up to date every minute.
                    </p>
                    <PermissionGate role="admin" fallback={<p className="text-zinc-400 text-sm">Only admins manage primaries.</p>}>
                        <div className="mb-6 p-4 border border-zinc-800 rounded-xl bg-zinc-900/50 space-y-3">
                            <h2 className="font-semibold text-white">Follow a primary</h2>
                            <p className="text-sm text-zinc-400">
                                On the primary, an admin adds this instance under Settings → Followers and gives you its address and token.
                            </p>
                            <form
                                className="flex flex-wrap items-center gap-3"
                                onSubmit={event => {
                                    event.preventDefault();
                                    add.mutate();
                                }}
                            >
                                <input
                                    aria-label="Primary address"
                                    value={url}
                                    onChange={event => setUrl(event.target.value)}
                                    placeholder="https://primary.example.com/api"
                                    className="flex-1 min-w-64 bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-sm text-zinc-100"
                                />
                                <input
                                    aria-label="Follower token"
                                    type="password"
                                    autoComplete="off"
                                    value={token}
                                    onChange={event => setToken(event.target.value)}
                                    placeholder="Follower token"
                                    className="min-w-48 bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-sm text-zinc-100"
                                />
                                <button
                                    type="submit"
                                    disabled={!url.trim() || !token.trim() || add.isPending}
                                    className="px-3 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-white text-sm"
                                >
                                    {add.isPending ? 'Checking…' : 'Follow'}
                                </button>
                            </form>
                            {add.error && <p className="text-sm text-red-300">{(add.error as Error).message}</p>}
                        </div>
                        {isLoading ? (
                            <p className="text-zinc-500">Loading primaries…</p>
                        ) : isError ? (
                            <p className="text-red-300">Could not load the primaries.</p>
                        ) : (
                            <div className="space-y-3">
                                {stopped && (
                                    <p
                                        data-testid="stopped-notice"
                                        className={`text-sm ${stopped.result.told_primary ? 'text-zinc-400' : 'text-amber-300'}`}
                                    >
                                        {stopped.result.told_primary
                                            ? `Stopped following ${stopped.url}. The primary knows, and no longer keeps changes for this instance.`
                                            : `Stopped following ${stopped.url}, but the primary wasn't told: ${stopped.result.detail}. `
                                              + 'Its admin can remove this follower under Settings → Followers there.'}
                                    </p>
                                )}
                                {(data?.primaries ?? []).length === 0 && (
                                    <p data-testid="no-primaries" className="text-zinc-500">This instance follows no primary.</p>
                                )}
                                {(data?.primaries ?? []).map(primary => (
                                    <PrimaryCard key={primary.id} primary={primary} onRemoved={(url, result) => setStopped({ url, result })} />
                                ))}
                                {(data?.unlinked_mirrors ?? []).length > 0 && (
                                    <div className="p-4 border border-zinc-800 rounded-xl bg-zinc-900/50 space-y-2">
                                        <h3 className="font-semibold text-white">Copies no longer following</h3>
                                        <p className="text-sm text-zinc-400">
                                            Read-only copies whose primary was removed here. Keep them as they are, or delete them from
                                            their dataset&apos;s settings.
                                        </p>
                                        {data!.unlinked_mirrors.map(mirror => <MirrorRow key={mirror.dataset_id} mirror={mirror} />)}
                                    </div>
                                )}
                            </div>
                        )}
                    </PermissionGate>
                </main>
            </div>
        </ProtectedRoute>
    );
}
