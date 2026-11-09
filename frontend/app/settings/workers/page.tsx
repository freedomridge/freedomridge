'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { Header } from '@/components/header';
import { PermissionGate } from '@/components/PermissionGate';
import { ProtectedRoute } from '@/components/ProtectedRoute';
import { OnDemandWorkers } from '@/components/on-demand-workers';

// A compute worker (python -m fridge.worker on a machine with a worker token), or one of the server's job runners.
interface Worker {
    id: string;
    placement: 'server' | 'compute';
    name: string | null;
    hostname: string | null;
    owner: string | null;
    sdk_version: string | null;
    capabilities: { kinds?: string[]; memory_bytes?: number | null; cpus?: number | null; gpu?: boolean };
    heartbeat_at: string;
    current_job_id: string | null;
    online: boolean;
}

function machine(capabilities: Worker['capabilities']): string {
    const parts = [];
    if (capabilities.memory_bytes) parts.push(`${Math.round(capabilities.memory_bytes / 1024 ** 3)} GB RAM`);
    if (capabilities.cpus) parts.push(`${capabilities.cpus} CPUs`);
    if (capabilities.gpu) parts.push('NVIDIA GPU');
    return parts.join(', ');
}

export default function WorkersPage() {
    const { data: workers, isLoading, isError } = useQuery<Worker[]>({
        queryKey: ['workers'],
        queryFn: async () => {
            const res = await fetch('/api/workers', { credentials: 'include' });
            if (!res.ok) throw new Error('Failed to load the workers');
            return res.json();
        },
        refetchInterval: 15_000,
    });
    const compute = (workers ?? []).filter(worker => worker.placement === 'compute');
    const runners = (workers ?? []).filter(worker => worker.placement === 'server');

    return (
        <ProtectedRoute>
            <div className="min-h-screen bg-zinc-950 flex flex-col">
                <Header />
                <main className="flex-1 container mx-auto px-6 py-8 max-w-4xl">
                    <Link href="/settings" className="inline-flex items-center gap-1 text-sm text-zinc-400 hover:text-white mb-4">
                        <ArrowLeft size={14} /> Back to Settings
                    </Link>
                    <h1 className="text-3xl font-bold text-white">Compute Workers</h1>
                    <p className="text-zinc-400 mt-1 mb-6">
                        Machines that run heavy jobs, such as map re-fits, for this server. An admin sees every worker; you see yours.
                    </p>
                    <PermissionGate
                        role="power_user"
                        fallback={<p className="text-zinc-400 text-sm">Guest users cannot run compute workers.</p>}
                    >
                        <div className="mb-6 p-4 border border-zinc-800 rounded-xl bg-zinc-900/50 text-sm text-zinc-300 space-y-2">
                            <h2 className="font-semibold text-white">Connect a machine</h2>
                            <p>
                                1. Create a token on <Link href="/settings/tokens" className="text-cyan-400 hover:underline">API Tokens</Link>,
                                {' '}used by <span className="text-zinc-100">a compute worker</span>.
                            </p>
                            <p>2. On the machine, with the Freedom Ridge Python SDK installed, run:</p>
                            <code data-testid="worker-command" className="block p-2 bg-zinc-950 border border-zinc-800 rounded font-mono text-xs break-all">
                                {/* Rendered only once the signed-in user is known, so in the browser. */}
                                FRIDGE_API_URL={typeof window === 'undefined' ? '' : window.location.origin}/api FRIDGE_API_TOKEN=&lt;worker token&gt; python -m fridge.worker
                            </code>
                            <p className="text-zinc-400">
                                The worker shows here within a minute. It keeps going while the server restarts, and stops when its token is revoked.
                            </p>
                        </div>
                        <PermissionGate role="admin" fallback={null}>
                            <OnDemandWorkers />
                        </PermissionGate>
                        {isLoading ? (
                            <p className="text-zinc-500">Loading workers…</p>
                        ) : isError ? (
                            <p className="text-red-300">Could not load the workers.</p>
                        ) : (
                            <>
                                {compute.length === 0 && (
                                    <p data-testid="no-workers" className="text-zinc-500 mb-6">No compute workers have connected in the last day.</p>
                                )}
                                <div className="space-y-3">
                                    {[...compute, ...runners].map(worker => (
                                        <div key={worker.id} data-testid="worker-row" className="p-4 border border-zinc-800 rounded-xl bg-zinc-900">
                                            <div className="flex items-center justify-between gap-4">
                                                <h3 className="font-semibold text-white">
                                                    {worker.name || worker.id}
                                                    <span className="ml-2 text-xs font-normal text-zinc-500">
                                                        {worker.placement === 'compute' ? 'Compute worker' : 'Server job runner'}
                                                    </span>
                                                </h3>
                                                <span
                                                    data-testid="worker-status"
                                                    className={`px-2 py-0.5 text-xs font-semibold rounded border ${worker.online
                                                        ? 'bg-green-500/10 text-green-400 border-green-500/30'
                                                        : 'bg-zinc-800 text-zinc-400 border-zinc-700'}`}
                                                >
                                                    {worker.online ? 'ONLINE' : 'OFFLINE'}
                                                </span>
                                            </div>
                                            <div className="mt-2 text-sm text-zinc-400 space-y-1">
                                                <p>
                                                    {[worker.hostname, worker.owner && `owner ${worker.owner}`, machine(worker.capabilities),
                                                        worker.sdk_version && `SDK ${worker.sdk_version}`].filter(Boolean).join(' · ')}
                                                </p>
                                                {worker.capabilities.kinds && worker.capabilities.kinds.length > 0 && (
                                                    <p>Takes: {worker.capabilities.kinds.join(', ')}</p>
                                                )}
                                                <p>
                                                    Last heard from {new Date(worker.heartbeat_at).toLocaleString()}
                                                    {worker.current_job_id && ` · running job ${worker.current_job_id}`}
                                                </p>
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </>
                        )}
                    </PermissionGate>
                </main>
            </div>
        </ProtectedRoute>
    );
}
