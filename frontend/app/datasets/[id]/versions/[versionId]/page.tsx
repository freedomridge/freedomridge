'use client';

import { Suspense, useState } from 'react';
import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import type { Dataset, Sample } from '@/lib/api';
import { getSampleMediaUrl } from '@/lib/media-utils';
import { parseAnnotations } from '@/lib/labels';
import { AnnotationOverlay } from '@/components/annotations/AnnotationOverlay';
import { SampleModal } from '@/components/sample-modal';
import { DatasetJobs } from '@/components/dataset-jobs';

interface SnapshotInfo {
    id: number;
    name: string;
    description?: string;
    sample_count: number;
    include_metadata: boolean;
    snapshot_format: number;
    created_at: string;
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

function SnapshotBrowser() {
    const { id, versionId } = useParams<{ id: string; versionId: string }>();
    const searchParams = useSearchParams();
    const [draft, setDraft] = useState('');
    const [query, setQuery] = useState('');
    const [offset, setOffset] = useState(0);
    const [selectedId, setSelectedId] = useState<number | null>(null);
    const [mediaField, setMediaField] = useState<string | null>(null);
    const limit = 60;
    const base = `/api/datasets/${id}/versions/${versionId}`;
    const version = useQuery({
        queryKey: ['snapshot', id, versionId],
        queryFn: () => readSnapshot<SnapshotInfo>(base),
    });
    const params = new URLSearchParams({ full: 'true', limit: String(limit), offset: String(offset), q: query });
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
    const error = version.error || samples.error;

    return (
        <main className="min-h-screen bg-zinc-950 text-zinc-100 p-6 space-y-5">
            <Link href={`/datasets/${id}`} className="text-cyan-400 hover:underline">Back to current dataset</Link>
            {version.data && <DatasetJobs key={id} datasetId={Number(id)} />}
            <div className="rounded border border-cyan-800 bg-cyan-950/30 p-4 space-y-2">
                <h1 className="text-xl font-semibold">{settings?.name || 'Dataset'} · {version.data?.name || 'Loading snapshot…'}</h1>
                <p className="text-sm text-cyan-300">Read-only snapshot</p>
                {version.data && <p className="text-sm text-zinc-400">{version.data.sample_count.toLocaleString()} saved samples · {new Date(version.data.created_at).toLocaleString()}</p>}
                {version.data?.description && <p>{version.data.description}</p>}
                {searchParams.has('change_type') && <p className="text-sm">Showing {searchParams.get('change_type')} samples as they were in this saved version.</p>}
                {version.data && !settings && <p className="text-sm text-amber-300">This older snapshot did not capture dataset display settings. Unrecorded historical fields remain unknown.</p>}
            </div>
            {error && <p role="alert" className="text-red-400">{error.message}</p>}
            {version.data?.include_metadata === false && <p role="alert">Hash-only versions cannot be browsed. Create a full snapshot to preserve historical samples.</p>}
            {version.data?.include_metadata && <>
                <form className="flex gap-3" onSubmit={event => { event.preventDefault(); setQuery(draft); setOffset(0); setSelectedId(null); }}>
                    <input aria-label="Search snapshot" placeholder="Search saved paths, tags, or metadata" value={draft} maxLength={1000} onChange={event => setDraft(event.target.value)} className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-3 py-2" />
                    <button className="rounded bg-cyan-800 px-4 py-2" type="submit">Search</button>
                </form>
                {samples.isPending && <p role="status">Loading saved samples…</p>}
                {samples.data && <p>{samples.data.total.toLocaleString()} matching samples</p>}
                <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-6 gap-3">
                    {items.map(sample => <button key={sample.id} onClick={() => setSelectedId(sample.id)} className="rounded border border-zinc-800 overflow-hidden text-left hover:border-cyan-600" aria-label={`Open saved sample ${sample.id}`}>
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
                    <button disabled={offset === 0 || samples.isFetching} onClick={() => { setOffset(Math.max(0, offset - limit)); setSelectedId(null); }} className="disabled:opacity-40">Previous page</button>
                    <span>Page {Math.floor(offset / limit) + 1}</span>
                    <button disabled={!samples.data || offset + limit >= samples.data.total || samples.isFetching} onClick={() => { setOffset(offset + limit); setSelectedId(null); }} className="disabled:opacity-40">Next page</button>
                </div>
                {settings && <details className="text-sm text-zinc-400">
                    <summary>Saved dataset schema and display settings</summary>
                    <pre className="overflow-auto p-3">{JSON.stringify(settings, null, 2)}</pre>
                </details>}
            </>}
            {selected && <SampleModal
                key={`${versionId}-${selected.id}`}
                sample={selected}
                onClose={() => setSelectedId(null)}
                activeMediaField={mediaField || settings?.modal_media_field || 'filepath'}
                availableMediaFields={settings?.media_fields || ['filepath']}
                onMediaFieldChange={setMediaField}
                segmentationConfig={settings?.segmentation_config}
                overlayConfigs={overlayConfigs}
                activeOverlayFields={Array.from(new Set(parseAnnotations(selected.metadata).flatMap(annotation => [annotation.fieldName, annotation.type]).filter((field): field is string => field !== null)))}
                hasPrevious={selectedIndex > 0}
                hasNext={selectedIndex < items.length - 1}
                onPrevious={() => setSelectedId(items[selectedIndex - 1].id)}
                onNext={() => setSelectedId(items[selectedIndex + 1].id)}
            />}
        </main>
    );
}

export default function SnapshotPage() {
    return <Suspense fallback={<p>Loading snapshot…</p>}><SnapshotBrowser /></Suspense>;
}
