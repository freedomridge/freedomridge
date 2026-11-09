'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { Header } from '@/components/header';
import { PermissionGate } from '@/components/PermissionGate';
import { ProtectedRoute } from '@/components/ProtectedRoute';
import { getDatasets } from '@/lib/api';

// A follower instance of this server (docs/FOLLOWERS.md) and the datasets it follows.
type Sync = 'primary' | 'both';
interface FollowedDataset {
    dataset_id: number;
    name: string;
    tags: Sync;
    views: Sync;
    start_txid: number | null;
    acked_txid: number | null;
    held_rows: number;
}
interface Follower {
    id: number;
    name: string;
    created_at: string;
    last_seen_at: string | null;
    stale: boolean;
    datasets: FollowedDataset[];
}

const SYNC_LABELS: Record<Sync, string> = { primary: 'Primary only', both: 'Both ways' };

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

function SyncSelect({ value, onChange, label }: { value: Sync; onChange: (value: Sync) => void; label: string }) {
    return (
        <label className="flex items-center gap-2 text-sm text-zinc-300">
            {label}
            <select
                aria-label={label}
                value={value}
                onChange={event => onChange(event.target.value as Sync)}
                className="bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-zinc-100"
            >
                {(Object.keys(SYNC_LABELS) as Sync[]).map(option => (
                    <option key={option} value={option}>{SYNC_LABELS[option]}</option>
                ))}
            </select>
        </label>
    );
}

function FollowerCard({ follower, datasets }: { follower: Follower; datasets: { id: number; name: string }[] }) {
    const queryClient = useQueryClient();
    const [adding, setAdding] = useState<number | ''>('');
    const [confirmRemove, setConfirmRemove] = useState(false);
    const refresh = () => queryClient.invalidateQueries({ queryKey: ['followers'] });
    const follow = useMutation({
        mutationFn: ({ datasetId, tags, views }: { datasetId: number; tags: Sync; views: Sync }) =>
            send('PUT', `/followers/${follower.id}/datasets/${datasetId}`, { tags, views }),
        onSuccess: refresh,
    });
    const unfollow = useMutation({
        mutationFn: (datasetId: number) => send('DELETE', `/followers/${follower.id}/datasets/${datasetId}`),
        onSuccess: refresh,
    });
    const remove = useMutation({ mutationFn: () => send('DELETE', `/followers/${follower.id}`), onSuccess: refresh });
    const error = follow.error || unfollow.error || remove.error;
    const followed = new Set(follower.datasets.map(dataset => dataset.dataset_id));

    return (
        <div data-testid="follower-row" className="p-4 border border-zinc-800 rounded-xl bg-zinc-900 space-y-3">
            <div className="flex items-center justify-between gap-4">
                <h3 className="font-semibold text-white">{follower.name}</h3>
                <span
                    data-testid="follower-seen"
                    className={`px-2 py-0.5 text-xs font-semibold rounded border ${follower.stale
                        ? 'bg-amber-500/10 text-amber-300 border-amber-500/30'
                        : 'bg-green-500/10 text-green-400 border-green-500/30'}`}
                >
                    {follower.last_seen_at
                        ? `Last checked in ${new Date(follower.last_seen_at).toLocaleString()}`
                        : 'Never checked in'}
                </span>
            </div>
            {follower.datasets.length === 0 ? (
                <p className="text-sm text-zinc-500">Follows no datasets yet.</p>
            ) : (
                <div className="space-y-2">
                    {follower.datasets.map(dataset => (
                        <div key={dataset.dataset_id} data-testid="followed-dataset" className="flex flex-wrap items-center gap-4 text-sm">
                            <span className="text-zinc-100 min-w-40">{dataset.name}</span>
                            <SyncSelect
                                label="Tags"
                                value={dataset.tags}
                                onChange={tags => follow.mutate({ datasetId: dataset.dataset_id, tags, views: dataset.views })}
                            />
                            <SyncSelect
                                label="Views"
                                value={dataset.views}
                                onChange={views => follow.mutate({ datasetId: dataset.dataset_id, tags: dataset.tags, views })}
                            />
                            <span className="text-zinc-500" title="Changes kept on this server until the follower applies them">
                                {dataset.held_rows.toLocaleString()} changes held
                            </span>
                            <button
                                onClick={() => unfollow.mutate(dataset.dataset_id)}
                                className="text-zinc-400 hover:text-red-300"
                            >
                                Stop following
                            </button>
                        </div>
                    ))}
                </div>
            )}
            <div className="flex flex-wrap items-center gap-3 pt-2 border-t border-zinc-800">
                <select
                    aria-label="Dataset to follow"
                    value={adding}
                    onChange={event => setAdding(event.target.value ? Number(event.target.value) : '')}
                    className="bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-sm text-zinc-100"
                >
                    <option value="">Follow a dataset…</option>
                    {datasets.filter(dataset => !followed.has(dataset.id)).map(dataset => (
                        <option key={dataset.id} value={dataset.id}>{dataset.name}</option>
                    ))}
                </select>
                <button
                    disabled={adding === '' || follow.isPending}
                    onClick={() => {
                        if (adding !== '') follow.mutate({ datasetId: adding, tags: 'primary', views: 'primary' });
                        setAdding('');
                    }}
                    className="px-3 py-1 rounded bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-white text-sm"
                >
                    Follow
                </button>
                <div className="flex-1" />
                {confirmRemove ? (
                    <span className="flex items-center gap-2 text-sm">
                        <span className="text-zinc-300">Remove {follower.name} and everything kept for it?</span>
                        <button onClick={() => remove.mutate()} className="px-3 py-1 rounded bg-red-600 hover:bg-red-500 text-white">
                            Remove
                        </button>
                        <button onClick={() => setConfirmRemove(false)} className="text-zinc-400 hover:text-white">Cancel</button>
                    </span>
                ) : (
                    <button onClick={() => setConfirmRemove(true)} className="text-sm text-zinc-400 hover:text-red-300">
                        Remove follower…
                    </button>
                )}
            </div>
            {error && <p className="text-sm text-red-300">{(error as Error).message}</p>}
        </div>
    );
}

export default function FollowersPage() {
    const queryClient = useQueryClient();
    const [name, setName] = useState('');
    const [created, setCreated] = useState<{ name: string; token: string } | null>(null);
    const { data: followers, isLoading, isError } = useQuery<Follower[]>({
        queryKey: ['followers'],
        queryFn: async () => {
            const res = await fetch('/api/followers', { credentials: 'include' });
            if (!res.ok) throw new Error('Failed to load the followers');
            return res.json();
        },
        refetchInterval: 30_000,
    });
    const { data: datasets } = useQuery({ queryKey: ['datasets'], queryFn: getDatasets });
    const add = useMutation({
        mutationFn: (followerName: string) => send('POST', '/followers', { name: followerName }),
        onSuccess: result => {
            setCreated({ name: result.name, token: result.token });
            setName('');
            queryClient.invalidateQueries({ queryKey: ['followers'] });
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
                    <h1 className="text-3xl font-bold text-white">Followers</h1>
                    <p className="text-zinc-400 mt-1 mb-6">
                        Other Freedom Ridge instances that mirror datasets of this one and add what it doesn&apos;t hold, such as
                        patch vectors. Each follows the datasets you choose; tags and views can sync from here only, or both ways.
                    </p>
                    <PermissionGate role="admin" fallback={<p className="text-zinc-400 text-sm">Only admins manage followers.</p>}>
                        <div className="mb-6 p-4 border border-zinc-800 rounded-xl bg-zinc-900/50 space-y-3">
                            <h2 className="font-semibold text-white">Add a follower</h2>
                            <form
                                className="flex flex-wrap items-center gap-3"
                                onSubmit={event => {
                                    event.preventDefault();
                                    add.mutate(name.trim());
                                }}
                            >
                                <input
                                    aria-label="Follower name"
                                    value={name}
                                    onChange={event => setName(event.target.value)}
                                    placeholder="e.g. tailscale-local"
                                    pattern="[a-z0-9][a-z0-9-]{0,62}"
                                    title="Lowercase letters, digits and hyphens"
                                    className="bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-sm text-zinc-100"
                                />
                                <button
                                    type="submit"
                                    disabled={!name.trim() || add.isPending}
                                    className="px-3 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-white text-sm"
                                >
                                    Add
                                </button>
                            </form>
                            {add.error && <p className="text-sm text-red-300">{(add.error as Error).message}</p>}
                            {created && (
                                <div data-testid="follower-token" className="text-sm text-zinc-300 space-y-2">
                                    <p>
                                        <span className="text-white font-semibold">{created.name}</span>&apos;s token is shown only now. Give it to
                                        the follower with this server&apos;s address:
                                    </p>
                                    <code className="block p-2 bg-zinc-950 border border-zinc-800 rounded font-mono text-xs break-all">
                                        {typeof window === 'undefined' ? '' : window.location.origin}/api {created.token}
                                    </code>
                                    <button onClick={() => setCreated(null)} className="text-zinc-400 hover:text-white">Done</button>
                                </div>
                            )}
                        </div>
                        {isLoading ? (
                            <p className="text-zinc-500">Loading followers…</p>
                        ) : isError ? (
                            <p className="text-red-300">Could not load the followers.</p>
                        ) : (followers ?? []).length === 0 ? (
                            <p data-testid="no-followers" className="text-zinc-500">No followers yet.</p>
                        ) : (
                            <div className="space-y-3">
                                {(followers ?? []).map(follower => (
                                    <FollowerCard
                                        key={follower.id}
                                        follower={follower}
                                        datasets={(datasets ?? []).map(dataset => ({ id: dataset.id, name: dataset.name }))}
                                    />
                                ))}
                            </div>
                        )}
                    </PermissionGate>
                </main>
            </div>
        </ProtectedRoute>
    );
}
