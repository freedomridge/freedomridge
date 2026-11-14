'use client';

import { Suspense, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import type { Dataset, Sample } from '@/lib/api';
import { getSampleMediaUrl } from '@/lib/media-utils';
import { parseAnnotations } from '@/lib/labels';
import { AnnotationOverlay } from '@/components/annotations/AnnotationOverlay';
import { SampleModal } from '@/components/sample-modal';
import { DatasetJobs } from '@/components/dataset-jobs';
import { versionMaker } from '@/lib/version-revert';
import { useLiveAddress } from '@/lib/runtime-config';

interface SnapshotInfo {
    id: number;
    name: string;
    description?: string;
    sample_count: number;
    include_metadata: boolean;
    snapshot_format: number;
    created_at: string;
    // Who made it; null when unknown (a version made before versions recorded it, or history brought in
    // without an author).
    created_by?: string | null;
    // Who brought it in, when its date or author was stated by the request that made it; null otherwise.
    imported_by?: string | null;
    dataset_snapshot?: Pick<Dataset, 'name' | 'description' | 'media_fields' | 'grid_media_field' | 'modal_media_field' | 'segmentation_config'> & {
        fields: { field_name: string; field_type: string; overlay_config?: { colormap?: string; range?: [number, number]; opacity?: number } }[];
    };
}

async function readSnapshot<ResponseType>(url: string): Promise<ResponseType> {
    const response = await fetch(url);
    if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event('fridge:unauthorized'));
        const error = await response.json().catch(() => ({}));
        throw Object.assign(new Error(error.detail || `Could not load snapshot (${response.status})`), { status: response.status });
    }
    return response.json();
}

// This page's address is a link to what it shows: /datasets/<id>/versions/<version>?<parameters>, each optional.
//   search=<text>   the search applied to the saved samples
//   view=<name>     a saved view the version recorded, by its name
//   page=<n>        the page of samples, from 1
//   sample=<id>     the sample whose view is open, one of that page's
// change_type, diff_base and diff_target come with a Compare link and are kept as they are.
// With the live address off (FRIDGE_LIVE_ADDRESS, `live` false below), each history entry holds them instead
// (ENTRY_QUERY) and the address is the page's path alone: a link's move onto its entry as the page opens. Writing an
// entry's then tells the page (ENTRY_WRITTEN), as the router does for the address.
const ENTRY_QUERY = 'fridgeSnapshotQuery';
const ENTRY_WRITTEN = 'fridge:snapshot-entry-written';

// Marks the history entry that opening a sample added. Closing the sample goes back from that entry only.
const OPENED_SAMPLE = 'fridgeSnapshotSampleOpened';
// How long the address waits to follow Previous and Next. Holding an arrow key would otherwise write history faster
// than WebKit allows (about 100 calls in a short window; each write here is two).
const STEP_ADDRESS_DELAY_MS = 250;

/** A whole number from 1 in an address parameter, or null. */
function positiveInteger(text: string | null): number | null {
    return text !== null && /^[1-9]\d*$/.test(text) ? Number(text) : null;
}

/** The query of the address shown, written as useSearchParams writes its own; off, the entry's. */
function shownQuery(live: boolean): string {
    if (!live) return new URLSearchParams(window.history.state?.[ENTRY_QUERY] ?? '').toString();
    return new URLSearchParams(window.location.search).toString();
}

/**
 * Off, the parameters the page opens on: the address's (a link's), else, when the router opened the page's path
 * alone and that is the address shown, those of the entry a reload or Back or Forward reopened (none on a new entry).
 */
function openingQuery(path: string, opened: string): string {
    return opened || (window.location.pathname === path && !window.location.search ? shownQuery(false) : '');
}

/** Whether the current history entry is the one that opening a sample added. */
function onOpenedSampleEntry(): boolean {
    return window.history.state?.[OPENED_SAMPLE] === true;
}

/**
 * Puts `changes` in the address (null removes a parameter; off, in the entry's), on a new history entry or on the
 * current one. The state is a new object each time: Next's history patch then takes the address as its own and adds
 * its own keys. Handed its own keys back, it would pass the call through and keep its old address.
 */
function writeAddress(live: boolean, changes: Record<string, string | null>, entry: 'push' | 'replace', openedSample = false) {
    const url = new URL(window.location.href);
    if (!live) url.search = shownQuery(false);
    for (const [name, value] of Object.entries(changes)) {
        if (value === null) url.searchParams.delete(name);
        else url.searchParams.set(name, value);
    }
    if (url.searchParams.toString() === shownQuery(live)) return;
    const state = openedSample ? { [OPENED_SAMPLE]: true } : null;
    if (!live) {
        // The address stays as it is, so the router isn't told.
        const held = { ...state, [ENTRY_QUERY]: url.searchParams.toString() };
        if (entry === 'push') window.history.pushState(held, '');
        else window.history.replaceState(held, '');
        window.dispatchEvent(new Event(ENTRY_WRITTEN));
        return;
    }
    if (entry === 'push') window.history.pushState(state, '', `${url.pathname}${url.search}`);
    else window.history.replaceState(state, '', `${url.pathname}${url.search}`);
}

/** A new search, saved view or page: a new history entry, on the first page unless it names one, with no sample open. */
function show(live: boolean, changes: Record<string, string | null>) {
    writeAddress(live, { page: null, ...changes, sample: null }, 'push');
}

function SnapshotBrowser({ liveAddress }: { liveAddress: boolean }) {
    const { id, versionId } = useParams<{ id: string; versionId: string }>();
    const routerParams = useSearchParams();
    // Off: the entry's parameters, read again when one is written and at Back or Forward (below).
    const path = `/datasets/${id}/versions/${versionId}`;
    const [entryQuery, setEntryQuery] = useState(() => (liveAddress ? '' : openingQuery(path, routerParams.toString())));
    const searchParams = liveAddress ? routerParams : new URLSearchParams(entryQuery);
    useEffect(() => {
        if (liveAddress) return;
        // A link's parameters move onto its entry, and the address keeps the page's path alone.
        if (window.location.search) window.history.replaceState({ [ENTRY_QUERY]: entryQuery }, '', window.location.pathname);
        // Not for a Back or Forward that leaves the page: it would show the page once more without its parameters.
        const read = () => { if (window.location.pathname === path) setEntryQuery(shownQuery(false)); };
        window.addEventListener('popstate', read);
        window.addEventListener(ENTRY_WRITTEN, read);
        return () => {
            window.removeEventListener('popstate', read);
            window.removeEventListener(ENTRY_WRITTEN, read);
        };
    }, []); // eslint-disable-line react-hooks/exhaustive-deps -- once, with the parameters the page opened on
    const address = searchParams.toString();
    const query = searchParams.get('search') ?? '';
    // The saved view shown, one the version recorded; '' for every saved sample.
    const viewName = searchParams.get('view') ?? '';
    const pageNumber = positiveInteger(searchParams.get('page')) ?? 1;
    // The text in the search box: the address's search when the page opens, and after Back or Forward to an entry
    // with another search (below).
    const [draft, setDraft] = useState(query);
    // The search of the history entry the page is on. It is kept as the page writes a search and arrives at an entry,
    // not read from the rendered search: Back right after a search can arrive before that search was ever rendered.
    const entrySearch = useRef(query);
    const [mediaField, setMediaField] = useState<string | null>(null);
    // The sample Previous or Next moved to, shown at once; the address follows on the same history entry once they
    // pause. It holds only on the address it was made on, so Back or Forward meanwhile drops it.
    const [step, setStep] = useState<{ id: number; address: string } | null>(null);
    const selectedId = step?.address === address ? step.id : positiveInteger(searchParams.get('sample'));
    useEffect(() => {
        if (!step || step.address !== address) return;
        const timer = setTimeout(() => {
            if (shownQuery(liveAddress) === step.address) writeAddress(liveAddress, { sample: String(step.id) }, 'replace', onOpenedSampleEntry());
        }, STEP_ADDRESS_DELAY_MS);
        return () => clearTimeout(timer);
    }, [step, address, liveAddress]);
    // A close that goes back is under way until its popstate: a second Escape meanwhile would leave the page.
    const closing = useRef(false);
    useEffect(() => {
        // Back or Forward to an entry with another search puts that search in the box. An entry with the same search
        // (a sample closed, another page) leaves the text typed there.
        const arrived = () => {
            closing.current = false;
            const search = new URLSearchParams(shownQuery(liveAddress)).get('search') ?? '';
            if (search !== entrySearch.current) setDraft(search);
            entrySearch.current = search;
        };
        window.addEventListener('popstate', arrived);
        return () => window.removeEventListener('popstate', arrived);
    }, [liveAddress]);
    // Closing goes back when the current entry is the one opening the sample added, so the history is as before the
    // open, with the sample one Forward away. Otherwise (a page opened on a sample's link) the entry loses its sample.
    const closeSample = () => {
        if (closing.current) return;
        if (onOpenedSampleEntry()) {
            // The entry first takes the sample Previous or Next reached, so Forward reopens the one that was closed.
            if (step?.address === address) writeAddress(liveAddress, { sample: String(step.id) }, 'replace', true);
            closing.current = true;
            window.history.back();
        } else {
            writeAddress(liveAddress, { sample: null }, 'replace');
        }
        setStep(null);
    };
    const limit = 60;
    const offset = (pageNumber - 1) * limit;
    const base = `/api/datasets/${id}/versions/${versionId}`;
    const version = useQuery({
        queryKey: ['snapshot', id, versionId],
        queryFn: () => readSnapshot<SnapshotInfo>(base),
    });
    // The dataset's public saved views when the version was made; null when it recorded none.
    const views = useQuery({
        queryKey: ['snapshot-views', id, versionId],
        queryFn: () => readSnapshot<{ views: { name: string; description?: string | null }[] | null }>(`${base}/views`),
        enabled: version.data?.include_metadata === true,
    });
    const params = new URLSearchParams({ full: 'true', limit: String(limit), offset: String(offset), q: query });
    if (viewName) params.set('view', viewName);
    for (const key of ['change_type', 'diff_base', 'diff_target']) {
        const value = searchParams.get(key);
        if (value) params.set(key, value);
    }
    const samples = useQuery({
        queryKey: ['snapshot-samples', id, versionId, params.toString()],
        queryFn: () => readSnapshot<{ items: Sample[]; total: number }>(`${base}/samples?${params}`),
        enabled: version.data?.include_metadata === true,
    });
    const settings = version.data?.dataset_snapshot;
    const items = samples.data?.items || [];
    const selectedIndex = items.findIndex(sample => sample.id === selectedId);
    const selected = items[selectedIndex];
    const overlayConfigs = Object.fromEntries((settings?.fields || []).map(field => [field.field_name, field.overlay_config || null]));
    const error = version.error || views.error || samples.error;
    const savedViews = views.data?.views;

    return (
        <main className="min-h-screen bg-zinc-950 text-zinc-100 p-6 space-y-5">
            <Link href={`/datasets/${id}`} className="text-cyan-400 hover:underline">Back to current dataset</Link>
            {version.data && <DatasetJobs key={id} datasetId={Number(id)} />}
            <div className="rounded border border-cyan-800 bg-cyan-950/30 p-4 space-y-2">
                <h1 className="text-xl font-semibold">{settings?.name || 'Dataset'} · {version.data?.name || 'Loading snapshot…'}</h1>
                <p className="text-sm text-cyan-300">Read-only snapshot</p>
                {version.data && <p className="text-sm text-zinc-400">{version.data.sample_count.toLocaleString()} saved samples · {new Date(version.data.created_at).toLocaleString()} · {versionMaker(version.data)}</p>}
                {version.data?.description && <p>{version.data.description}</p>}
                {searchParams.has('change_type') && <p className="text-sm">Showing {searchParams.get('change_type')} samples as they were in this saved version.</p>}
                {version.data && !settings && <p className="text-sm text-amber-300">This older snapshot did not capture dataset display settings. Unrecorded historical fields remain unknown.</p>}
            </div>
            {error && <p role="alert" className="text-red-400">{error.message}</p>}
            {version.data?.include_metadata === false && <p role="alert">Hash-only versions cannot be browsed. Create a full snapshot to preserve historical samples.</p>}
            {version.data?.include_metadata && <>
                <form className="flex gap-3" onSubmit={event => { event.preventDefault(); entrySearch.current = draft; show(liveAddress, { search: draft || null }); }}>
                    <input aria-label="Search snapshot" placeholder="Search saved paths, tags, or metadata" value={draft} maxLength={1000} onChange={event => setDraft(event.target.value)} className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-3 py-2" />
                    <button className="rounded bg-cyan-800 px-4 py-2" type="submit">Search</button>
                </form>
                {savedViews && savedViews.length > 0 && (
                    <label className="flex items-center gap-3 text-sm">
                        Saved view
                        <select
                            aria-label="Saved view of this version"
                            value={viewName}
                            onChange={event => show(liveAddress, { view: event.target.value || null })}
                            className="rounded border border-zinc-700 bg-zinc-900 px-3 py-2"
                        >
                            <option value="">All saved samples</option>
                            {savedViews.map(view => <option key={view.name} value={view.name} title={view.description || undefined}>{view.name}</option>)}
                        </select>
                        <span className="text-zinc-400">as this version recorded it</span>
                    </label>
                )}
                {savedViews === null && <p className="text-sm text-zinc-400">This version did not record the dataset&apos;s saved views.</p>}
                {samples.isPending && <p role="status">Loading saved samples…</p>}
                {samples.data && <p>{samples.data.total.toLocaleString()} matching samples</p>}
                {samples.data && selectedId !== null && !selected && <p role="alert">Sample {selectedId} is not on this page of the snapshot, so it is not open.</p>}
                <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-6 gap-3">
                    {items.map(sample => <button key={sample.id} onClick={() => writeAddress(liveAddress, { sample: String(sample.id) }, 'push', true)} className="rounded border border-zinc-800 overflow-hidden text-left hover:border-cyan-600" aria-label={`Open saved sample ${sample.id}`}>
                        <AnnotationOverlay
                            imageSrc={getSampleMediaUrl(sample.id, settings?.grid_media_field || 'filepath', undefined, undefined, undefined, Number(versionId))}
                            sampleId={sample.id}
                            versionId={Number(versionId)}
                            metadata={sample.metadata}
                            segmentationConfig={settings?.segmentation_config}
                            overlayConfigs={overlayConfigs}
                            compactOverlays
                            className="aspect-square w-full"
                        />
                        <div className="p-2 text-xs space-y-1">
                            <p className="truncate" title={sample.filepath}>{sample.filepath}</p>
                            <p className="truncate text-zinc-400">{(sample.tags || []).join(', ')}</p>
                        </div>
                    </button>)}
                </div>
                <div className="flex items-center gap-4">
                    <button disabled={pageNumber === 1 || samples.isFetching} onClick={() => show(liveAddress, { page: pageNumber > 2 ? String(pageNumber - 1) : null })} className="disabled:opacity-40">Previous page</button>
                    <span>Page {pageNumber}</span>
                    <button disabled={!samples.data || offset + limit >= samples.data.total || samples.isFetching} onClick={() => show(liveAddress, { page: String(pageNumber + 1) })} className="disabled:opacity-40">Next page</button>
                </div>
                {settings && <details className="text-sm text-zinc-400">
                    <summary>Saved dataset schema and display settings</summary>
                    <pre className="overflow-auto p-3">{JSON.stringify(settings, null, 2)}</pre>
                </details>}
            </>}
            {selected && <SampleModal
                key={`${versionId}-${selected.id}`}
                sample={selected}
                onClose={closeSample}
                activeMediaField={mediaField || settings?.modal_media_field || 'filepath'}
                availableMediaFields={settings?.media_fields || ['filepath']}
                onMediaFieldChange={setMediaField}
                segmentationConfig={settings?.segmentation_config}
                overlayConfigs={overlayConfigs}
                activeOverlayFields={Array.from(new Set(parseAnnotations(selected.metadata).flatMap(annotation => [annotation.fieldName, annotation.type]).filter((field): field is string => field !== null)))}
                hasPrevious={selectedIndex > 0}
                hasNext={selectedIndex < items.length - 1}
                onPrevious={() => setStep({ id: items[selectedIndex - 1].id, address })}
                onNext={() => setStep({ id: items[selectedIndex + 1].id, address })}
            />}
        </main>
    );
}

export default function SnapshotPage() {
    // Where the parameters are kept (FRIDGE_LIVE_ADDRESS) is known before the page reads any: it waits for the app's
    // settings, one small request a page load (lib/runtime-config.ts).
    const liveAddress = useLiveAddress();
    if (liveAddress === undefined) return <p>Loading snapshot…</p>;
    return <Suspense fallback={<p>Loading snapshot…</p>}><SnapshotBrowser liveAddress={liveAddress} /></Suspense>;
}
