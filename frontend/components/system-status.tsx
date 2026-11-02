'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle } from 'lucide-react';
import { useUser } from '@/contexts/UserContext';
import { getSystemStatus } from '@/lib/api';

// Admin-only: GET /api/admin/status 403s for everyone else, so the query never runs for them.
// No timer: disk space changes slowly (ingest refuses on its own when it runs low), so the page
// checks when it opens and when you come back to the tab (staleTime 0), plus its Refresh button.
// The exception is a backup in progress, which the Backups card follows until it ends.
export function useSystemStatus() {
    const { user } = useUser();
    return useQuery({
        queryKey: ['system-status'],
        queryFn: getSystemStatus,
        enabled: user?.role === 'admin',
        staleTime: 0,
        refetchInterval: query => {
            const backups = query.state.data?.backups;
            return backups && 'active_job' in backups && backups.active_job ? 5_000 : false;
        },
    });
}

// The banner's own copy of the query, which never errors: a failure here (a backend not yet
// restarted onto this endpoint, a 500) would otherwise raise the global "Couldn't load data" toast
// on every page and again on every check. The status page shows the error itself. Every 5 minutes
// is soon enough to warn while a sync eats the disk.
function useBannerStatus() {
    const { user } = useUser();
    return useQuery({
        queryKey: ['system-status', 'banner'],
        queryFn: () => getSystemStatus().catch(() => null),
        enabled: user?.role === 'admin',
        refetchInterval: 5 * 60_000,
    });
}

export function formatGB(bytes: number): string {
    const gb = bytes / 1024 ** 3;
    return `${gb.toFixed(gb < 10 ? 2 : 1)} GB`;
}

/** A strip under the header, admins only, while the database disk is Low or ingest is paused —
 *  so nobody has to go looking for Settings → System status before a sync starts failing. */
export function LowDiskBanner() {
    const { data } = useBannerStatus();
    const disk = data?.database_disk;
    if (!disk || disk.free_bytes == null || (disk.level !== 'low' && disk.level !== 'paused')) return null;

    const paused = disk.level === 'paused';
    return (
        <div
            data-testid="low-disk-banner"
            data-level={disk.level}
            className={`flex items-center gap-2 px-6 py-1.5 text-sm border-t ${paused ? 'bg-red-500/10 border-red-500/30 text-red-300' : 'bg-amber-500/10 border-amber-500/30 text-amber-300'}`}
        >
            <AlertTriangle size={14} className="shrink-0" />
            <span>
                {paused
                    ? `Database disk is nearly full: ${formatGB(disk.free_bytes)} free. Adding samples, embeddings, coordinates and hashes is refused until space is freed.`
                    : `Database disk is low: ${formatGB(disk.free_bytes)} free. Ingest stops at about ${formatGB(disk.ingest_paused_below_bytes)} free.`}
            </span>
            <Link href="/settings/status" className="ml-auto shrink-0 underline underline-offset-2 hover:text-white">
                System status
            </Link>
        </div>
    );
}
