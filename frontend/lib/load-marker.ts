// A load-in-progress marker kept in sessionStorage: written before a load and cleared once it has rendered, so a
// marker still there on the next mount means that load crashed the tab (out of memory, say). sessionStorage is the
// tab's own (another tab loading the same dataset doesn't count) and survives reloading a crashed tab. Each marker
// records the page load it came from (performance.timeOrigin). A crash always means a new page load, so only a marker
// from another one counts: one from this page load is a remount mid-load (a search remounting the panel, say). A
// reload or navigation mid-load clears it on pagehide, which a crash doesn't fire.

export function loadMarker(samplePct: number, pageLoad: number): string {
    return JSON.stringify({ pct: samplePct, origin: pageLoad });
}

export function crashedBefore(stored: string | null, pageLoad: number): boolean {
    if (stored === null) return false;
    try {
        const marker = JSON.parse(stored);
        // An older marker (a bare percentage) has no origin: it was left by an earlier page load.
        return typeof marker?.origin !== 'number' || marker.origin !== pageLoad;
    } catch {
        return true;
    }
}

export function currentPageLoad(): number {
    return typeof performance === 'undefined' ? 0 : performance.timeOrigin;
}
