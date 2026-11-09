import type { DatasetMirror } from '@/lib/api';

// The primary's host name, for "Mirror of <host>".
export function primaryHost(mirror: DatasetMirror): string {
    if (!mirror.primary_url) return 'another instance';
    try {
        return new URL(mirror.primary_url).hostname;
    } catch {
        return mirror.primary_url;
    }
}

// A dataset that mirrors one on a primary instance (docs/FOLLOWERS.md): read-only here.
export function MirrorBadge({ mirror }: { mirror: DatasetMirror }) {
    const host = primaryHost(mirror);
    const label = mirror.state === 'stopped'
        ? `Copy from ${host}, no longer following`
        : mirror.state === 'copying' ? `Mirror of ${host}, copying` : `Mirror of ${host}`;
    const title = mirror.state === 'stopped'
        ? `A read-only copy of a dataset on ${host}. It no longer follows it: keep it as it is, or delete it.`
        : `A read-only mirror of a dataset on ${host}. Its samples, fields and settings change there and arrive here `
          + 'within a minute; maps, statistics and vector fields only this instance has are its own.';
    return (
        <span
            data-testid="mirror-badge"
            title={title}
            className={`shrink-0 px-2 py-0.5 text-xs font-medium rounded border ${mirror.state === 'stopped'
                ? 'bg-zinc-800 text-zinc-400 border-zinc-700'
                : 'bg-sky-500/10 text-sky-300 border-sky-500/30'}`}
        >
            {label}
        </span>
    );
}
