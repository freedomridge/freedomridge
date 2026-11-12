'use client';

import { segmentationLabelChoices } from '@/lib/labels';

import * as Dialog from '@radix-ui/react-dialog';
import { useState, useMemo } from 'react';
import { Dataset, updateDataset, getDatasetSchema, promoteField, demoteField, setFieldDisplayType, setFieldType, SegmentationConfig, saveOverlayConfig, notifySamplesMutated, type DatasetMirror, type SchemaField, type DeclaredFieldType } from '@/lib/api';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { SAMPLE_COUNT_KEY } from '@/hooks/useSampleCount';
import { SegmentationSettingsPanel } from './SegmentationSettingsPanel';
import { Rocket } from 'lucide-react';
import Link from 'next/link';
import { useUser } from '@/contexts/UserContext';
import { IndexAdvisor } from './index-advisor';
import { primaryHost } from './mirror-badge';
import { revertQuestion, versionMaker, type ViewChanges } from '@/lib/version-revert';

const API_BASE = '/api';

// Per-heatmap-field overlay display config (colormap / range / opacity), edited in the
// settings "Visualization" tab. Saves per dataset-field; applied to the grid composite
// and the sample-view canvas.
function HeatmapFieldRow({ datasetId, field, readOnly, onSaved }: {
    datasetId: number; field: SchemaField; readOnly?: boolean; onSaved: () => void;
}) {
    const cfg = field.config || {};
    const [colormap, setColormap] = useState(cfg.colormap || 'viridis');
    const [rangeMin, setRangeMin] = useState(cfg.range ? String(cfg.range[0]) : '');
    const [rangeMax, setRangeMax] = useState(cfg.range ? String(cfg.range[1]) : '');
    const [opacity, setOpacity] = useState<number>(cfg.opacity ?? 0.6);
    const [saving, setSaving] = useState(false);
    const [saved, setSaved] = useState(false);

    const save = async () => {
        setSaving(true);
        try {
            const payload: { colormap?: string; range?: [number, number]; opacity?: number } = { colormap, opacity };
            if (rangeMin !== '' && rangeMax !== '') payload.range = [Number(rangeMin), Number(rangeMax)];
            await saveOverlayConfig(datasetId, field.name, payload);
            onSaved();
            setSaved(true);
            setTimeout(() => setSaved(false), 1500);
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="p-3 bg-zinc-800/40 border border-zinc-700 rounded-lg space-y-2">
            <div className="text-sm font-medium text-zinc-200">{field.name}</div>
            <div className="grid grid-cols-[5rem_1fr] gap-x-3 gap-y-2 items-center text-xs">
                <label className="text-zinc-400">Colormap</label>
                <select value={colormap} disabled={readOnly} onChange={e => setColormap(e.target.value)}
                    className="w-40 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-zinc-200 outline-none disabled:opacity-60">
                    {['viridis', 'magma', 'plasma', 'inferno', 'turbo', 'gray'].map(c => <option key={c} value={c}>{c}</option>)}
                </select>
                <label className="text-zinc-400">Range</label>
                <div className="flex items-center gap-2">
                    <input type="number" value={rangeMin} disabled={readOnly} onChange={e => setRangeMin(e.target.value)} placeholder="min"
                        className="w-20 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-zinc-200 outline-none disabled:opacity-60" />
                    <span className="text-zinc-600">–</span>
                    <input type="number" value={rangeMax} disabled={readOnly} onChange={e => setRangeMax(e.target.value)} placeholder="max"
                        className="w-20 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-zinc-200 outline-none disabled:opacity-60" />
                    <span className="text-zinc-600 hidden sm:inline">blank = use the heatmap&apos;s own range</span>
                </div>
                <label className="text-zinc-400">Opacity</label>
                <div className="flex items-center gap-2">
                    <input type="range" min={0} max={1} step={0.05} value={opacity} disabled={readOnly}
                        onChange={e => setOpacity(Number(e.target.value))} className="flex-1 max-w-[220px]" />
                    <span className="text-zinc-500 w-9 text-right">{opacity.toFixed(2)}</span>
                </div>
            </div>
            {!readOnly && (
                <div className="flex justify-end">
                    <button onClick={save} disabled={saving}
                        className="px-3 py-1 text-xs bg-cyan-700 hover:bg-cyan-600 text-white rounded disabled:opacity-50">
                        {saving ? 'Saving…' : saved ? 'Saved ✓' : 'Save'}
                    </button>
                </div>
            )}
        </div>
    );
}

function HeatmapOverlayConfig({ datasetId, schema, readOnly, onSaved }: {
    datasetId: number; schema: SchemaField[]; readOnly?: boolean; onSaved: () => void;
}) {
    const heatmaps = schema.filter(f => f.type === 'heatmap');
    if (heatmaps.length === 0) return null;
    return (
        <div className="space-y-3">
            <div>
                <h3 className="text-sm font-semibold text-zinc-200">Heatmap Overlays</h3>
                <p className="text-xs text-zinc-500 mt-0.5">Colormap, value range, and opacity per heatmap — applied to the grid and the sample view.</p>
            </div>
            {heatmaps.map(f => (
                <HeatmapFieldRow key={f.name} datasetId={datasetId} field={f} readOnly={readOnly} onSaved={onSaved} />
            ))}
        </div>
    );
}

// Per-mask-overlay-field opacity (RGB/index masks like semantic_mask), edited in the
// "Visualization" tab. Masks have no colormap/range — just opacity. Saves per
// dataset-field via the same overlay_config store as heatmaps; applied to the
// grid composite and the sample-view canvas.
function MaskFieldRow({ datasetId, field, readOnly, onSaved }: {
    datasetId: number; field: SchemaField; readOnly?: boolean; onSaved: () => void;
}) {
    const cfg = field.config || {};
    const [opacity, setOpacity] = useState<number>(cfg.opacity ?? 0.6);
    const [saving, setSaving] = useState(false);
    const [saved, setSaved] = useState(false);

    const save = async () => {
        setSaving(true);
        try {
            await saveOverlayConfig(datasetId, field.name, { opacity });
            onSaved();
            setSaved(true);
            setTimeout(() => setSaved(false), 1500);
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="p-3 bg-zinc-800/40 border border-zinc-700 rounded-lg space-y-2">
            <div className="text-sm font-medium text-zinc-200">{field.name}</div>
            <div className="grid grid-cols-[5rem_1fr] gap-x-3 gap-y-2 items-center text-xs">
                <label className="text-zinc-400">Opacity</label>
                <div className="flex items-center gap-2">
                    <input type="range" min={0} max={1} step={0.05} value={opacity} disabled={readOnly}
                        onChange={e => setOpacity(Number(e.target.value))} className="flex-1 max-w-[220px]" />
                    <span className="text-zinc-500 w-9 text-right">{opacity.toFixed(2)}</span>
                </div>
            </div>
            {!readOnly && (
                <div className="flex justify-end">
                    <button onClick={save} disabled={saving}
                        className="px-3 py-1 text-xs bg-cyan-700 hover:bg-cyan-600 text-white rounded disabled:opacity-50">
                        {saving ? 'Saving…' : saved ? 'Saved ✓' : 'Save'}
                    </button>
                </div>
            )}
        </div>
    );
}

function MaskOverlayConfig({ datasetId, schema, readOnly, onSaved }: {
    datasetId: number; schema: SchemaField[]; readOnly?: boolean; onSaved: () => void;
}) {
    // Class-probability fields take an opacity too (ClassProbabilityRenderer).
    const masks = schema.filter(f => f.type === 'segmentation' || f.type === 'class_probabilities');
    if (masks.length === 0) return null;
    return (
        <div className="space-y-3">
            <div>
                <h3 className="text-sm font-semibold text-zinc-200">Mask Overlays</h3>
                <p className="text-xs text-zinc-500 mt-0.5">Opacity per segmentation mask — applied to the grid and the sample view.</p>
            </div>
            {masks.map(f => (
                <MaskFieldRow key={f.name} datasetId={datasetId} field={f} readOnly={readOnly} onSaved={onSaved} />
            ))}
        </div>
    );
}

// Shape of a dataset version row as returned by /datasets/{id}/versions.
interface DatasetVersion {
    id: number;
    name: string;
    description?: string | null;
    sample_count: number;
    created_at: string;
    // Who made it; null when unknown (a version made before versions recorded it, or history brought in
    // without an author).
    created_by?: string | null;
    // Who brought it in, when its date or author was stated by the request that made it; null otherwise.
    imported_by?: string | null;
    include_metadata?: boolean;
}

// The view changes in words and, for a tooltip, by name; null when there are none to report.
function describeViewChanges(changes: ViewChanges | null | undefined): { text: string; names: string } | null {
    if (!changes) return null;
    const kinds: [string[], string, string][] = [
        [changes.added, 'new saved view', 'new saved views'],
        [changes.removed, 'saved view removed', 'saved views removed'],
        [changes.filters_changed, 'saved view with changed filters', 'saved views with changed filters'],
    ];
    const changed = kinds.filter(([names]) => names.length > 0);
    if (changed.length === 0) return null;
    return {
        text: changed.map(([names, one, many]) => `${names.length} ${names.length === 1 ? one : many}`).join(', '),
        names: changed.map(([names, one]) => `${one}: ${names.join(', ')}`).join('\n'),
    };
}

function VersionsPanel({ datasetId, mirror, role, onViewSamples }: { datasetId: number; mirror?: DatasetMirror | null; role?: string; onViewSamples?: (token: string, total: number, label: string) => void }) {
    // What the server allows: anyone with the dataset browses and compares versions, a power user makes them,
    // an admin reverts to one or deletes one. What a role can't do is disabled here and says why.
    const isAdmin = role === 'admin';
    const canMakeVersions = isAdmin || role === 'power_user';
    const queryClient = useQueryClient();
    const [newName, setNewName] = useState('');
    const [newDesc, setNewDesc] = useState('');
    const [includeMetadata, setIncludeMetadata] = useState(true);
    const [creating, setCreating] = useState(false);
    // A revert puts the dataset back to exactly the version unless this is turned off.
    const [exactMembership, setExactMembership] = useState(true);
    const [restoreSettings, setRestoreSettings] = useState(false);
    // The version whose revert is being counted, before its question is asked.
    const [countingRevert, setCountingRevert] = useState<number | null>(null);
    const [diffPair, setDiffPair] = useState<[number, number] | null>(null);
    const [diffVsCurrent, setDiffVsCurrent] = useState<number | null>(null);

    const { data: versions, isLoading } = useQuery({
        queryKey: ['versions', datasetId],
        queryFn: async () => {
            const res = await fetch(`${API_BASE}/datasets/${datasetId}/versions`);
            return res.json();
        },
    });

    const { data: versionStatus } = useQuery({
        queryKey: ['version-status', datasetId],
        queryFn: async () => {
            const res = await fetch(`${API_BASE}/datasets/${datasetId}/versions/status`);
            return res.json();
        },
    });

    const { data: diffData } = useQuery({
        queryKey: ['version-diff', datasetId, diffPair, diffVsCurrent],
        queryFn: async () => {
            if (diffPair) {
                const res = await fetch(`${API_BASE}/datasets/${datasetId}/versions/diff/${diffPair[0]}/${diffPair[1]}`);
                return res.json();
            }
            if (diffVsCurrent !== null) {
                const res = await fetch(`${API_BASE}/datasets/${datasetId}/versions/current-diff/${diffVsCurrent}`);
                return res.json();
            }
            return null;
        },
        enabled: diffPair !== null || diffVsCurrent !== null,
    });

    const [createError, setCreateError] = useState<string | null>(null);

    const handleCreate = async () => {
        if (!newName.trim()) return;
        setCreating(true);
        setCreateError(null);
        try {
            const resp = await fetch(`${API_BASE}/datasets/${datasetId}/versions`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ name: newName.trim(), description: newDesc.trim() || null, include_metadata: includeMetadata }),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({ detail: resp.statusText }));
                setCreateError(err.detail || 'Failed to create version');
                return;
            }
            setNewName('');
            setNewDesc('');
            queryClient.invalidateQueries({ queryKey: ['versions', datasetId] });
        } catch (e: unknown) {
            setCreateError((e instanceof Error && e.message) || 'Network error');
        } finally {
            setCreating(false);
        }
    };

    // Counts what the revert would change, asks with those numbers, then reverts. Without the numbers it doesn't ask.
    const handleRevert = async (v: DatasetVersion) => {
        setCountingRevert(v.id);
        let question: string;
        try {
            const [diffRes, datasetRes] = await Promise.all([
                fetch(`${API_BASE}/datasets/${datasetId}/versions/current-diff/${v.id}`, { credentials: 'include' }),
                fetch(`${API_BASE}/datasets/${datasetId}`, { credentials: 'include' }),
            ]);
            const failed = [diffRes, datasetRes].find(res => !res.ok);
            if (failed) {
                const err = await failed.json().catch(() => ({}));
                throw new Error(err.detail || `status ${failed.status}`);
            }
            const defaultView: string | null = (await datasetRes.json()).default_view?.name ?? null;
            question = revertQuestion(v, await diffRes.json(), exactMembership, restoreSettings, defaultView);
        } catch (e: unknown) {
            alert(`Could not count what this revert would change, so nothing was reverted: ${e instanceof Error ? e.message : e}`);
            return;
        } finally {
            setCountingRevert(null);
        }
        if (!confirm(question)) return;
        const res = await fetch(`${API_BASE}/datasets/${datasetId}/versions/${v.id}/revert?exact_membership=${exactMembership}&restore_settings=${restoreSettings}`, {
            method: 'POST', credentials: 'include',
        });
        if (res.ok) {
            // The counts are asked again by notifySamplesMutated (SAMPLE_COUNT_KEY).
            queryClient.invalidateQueries({ predicate: query => query.queryKey[0] !== SAMPLE_COUNT_KEY });
            // invalidateQueries can't reach the grid's manual
            // page cache — signal it so reverted rows show.
            notifySamplesMutated(datasetId);
        } else {
            const err = await res.json().catch(() => ({}));
            alert(err.detail || 'Revert failed');
        }
    };

    const handleDelete = async (versionId: number) => {
        try {
            const res = await fetch(`${API_BASE}/datasets/${datasetId}/versions/${versionId}`, {
                method: 'DELETE', credentials: 'include',
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `status ${res.status}`);
            }
        } catch (e: unknown) {
            alert(`The version was not deleted: ${e instanceof Error ? e.message : e}`);
            return;
        }
        queryClient.invalidateQueries({ queryKey: ['versions', datasetId] });
        if (diffVsCurrent === versionId) setDiffVsCurrent(null);
    };

    const statusViewChanges = describeViewChanges(versionStatus?.view_changes);
    const diffViewChanges = describeViewChanges(diffData?.view_changes);

    const primaryVersions = mirror?.primary_versions ?? 0;

    return (
        <div className="space-y-4">
            {mirror && primaryVersions > 0 && (
                <p data-testid="primary-versions-note" className="text-xs text-zinc-400 bg-zinc-800/50 rounded border border-zinc-800 p-2">
                    {primaryHost(mirror)} keeps {primaryVersions === 1 ? '1 version' : `${primaryVersions.toLocaleString()} versions`} of
                    this dataset, and {primaryVersions === 1 ? 'it stays' : 'they stay'} there: a mirror follows the dataset as
                    it is now, not its history. Versions listed here are ones made on this server.
                </p>
            )}
            {/* Current status */}
            {versionStatus && (
                <div className={`border rounded-lg p-3 ${
                    versionStatus.current_version
                        ? 'border-green-800/50 bg-green-900/10'
                        : versionStatus.has_changes
                            ? 'border-yellow-800/50 bg-yellow-900/10'
                            : 'border-zinc-800'
                }`}>
                    <div className="text-sm">
                        {versionStatus.current_version ? (
                            <span className="text-green-400">
                                Current state matches <strong>{versionStatus.current_version.name}</strong>
                            </span>
                        ) : versionStatus.has_changes ? (
                            <span className="text-yellow-400">
                                Unsaved changes {versionStatus.latest_version && <>since <strong>{versionStatus.latest_version.name}</strong></>}
                            </span>
                        ) : (
                            <span className="text-zinc-400">No versions yet</span>
                        )}
                    </div>
                    {statusViewChanges && (
                        <p className="text-xs text-yellow-300/80 mt-1" title={statusViewChanges.names} data-testid="status-view-changes">
                            {statusViewChanges.text}
                        </p>
                    )}
                </div>
            )}

            {/* Create new version */}
            <fieldset disabled={!canMakeVersions} title={canMakeVersions ? undefined : 'Making a version takes a power user'} className="min-w-0 border border-zinc-800 rounded-lg p-3 space-y-2 disabled:opacity-60">
                <div className="text-sm font-medium text-zinc-200">Create Version</div>
                <input
                    type="text"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    placeholder="Version name (e.g., v2.0)"
                    className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:ring-1 focus:ring-cyan-500"
                    onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
                />
                <input
                    type="text"
                    value={newDesc}
                    onChange={(e) => setNewDesc(e.target.value)}
                    placeholder="Description (optional)"
                    className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:ring-1 focus:ring-cyan-500"
                />
                <div className="space-y-2">
                    <div className="text-xs font-medium text-zinc-400">Snapshot Mode</div>
                    <div className="space-y-1.5">
                        <label className="flex items-start gap-2 cursor-pointer p-2 rounded border transition-colors ${includeMetadata ? 'border-green-500/30 bg-green-500/5' : 'border-zinc-800 hover:border-zinc-700'}">
                            <input
                                type="radio"
                                checked={includeMetadata}
                                onChange={() => setIncludeMetadata(true)}
                                className="accent-cyan-500 mt-0.5"
                            />
                            <div>
                                <div className="text-sm text-zinc-200">Full snapshot</div>
                                <div className="text-xs text-zinc-500">Preserves sample data and dataset display settings for historical browsing. Storage scales with metadata size.</div>
                            </div>
                        </label>
                        <label className="flex items-start gap-2 cursor-pointer p-2 rounded border transition-colors ${!includeMetadata ? 'border-yellow-500/30 bg-yellow-500/5' : 'border-zinc-800 hover:border-zinc-700'}">
                            <input
                                type="radio"
                                checked={!includeMetadata}
                                onChange={() => setIncludeMetadata(false)}
                                className="accent-cyan-500 mt-0.5"
                            />
                            <div>
                                <div className="text-sm text-zinc-200">Hash-only (lightweight)</div>
                                <div className="text-xs text-zinc-500">Detects what changed but cannot restore old data. ~40 bytes per sample. Best for large datasets.</div>
                            </div>
                        </label>
                    </div>
                    {createError && (
                        <div className="text-xs text-red-400 bg-red-900/20 border border-red-800/50 rounded px-2 py-1">
                            {createError}
                        </div>
                    )}
                    <button
                        onClick={handleCreate}
                        disabled={!newName.trim() || creating}
                        className="w-full px-3 py-1.5 text-sm bg-cyan-500 text-white rounded hover:bg-cyan-600 disabled:opacity-50 transition-colors"
                    >
                        {creating ? 'Creating snapshot...' : 'Create Version'}
                    </button>
                </div>
            </fieldset>

            {/* Version list */}
            {isLoading && <div className="text-sm text-zinc-500">Loading versions...</div>}

            {versions && versions.length === 0 && (
                <div className="text-sm text-zinc-500 text-center py-4">
                    No versions yet. Create one to start tracking changes.
                </div>
            )}

            {/* What a revert does; a revert takes an admin, so only an admin chooses. */}
            {!!versions?.length && isAdmin && <div className="space-y-1 text-xs text-zinc-400">
                <label className="flex items-center gap-2">
                    <input type="checkbox" checked={exactMembership} onChange={event => setExactMembership(event.target.checked)} />
                    A revert puts the dataset back to exactly the version: samples and public saved views added since are removed, deleted ones restored
                </label>
                <label className="flex items-center gap-2">
                    <input type="checkbox" checked={restoreSettings} onChange={event => setRestoreSettings(event.target.checked)} />
                    Also restore the dataset&apos;s settings: description, media fields, class names and colours, and the view it opens on
                </label>
            </div>}
            {versions && versions.map((v: DatasetVersion, idx: number) => (
                <div key={v.id} className={`border rounded-lg p-3 ${idx === 0 ? 'border-cyan-800/50 bg-cyan-900/5' : 'border-zinc-800'}`}>
                    <div className="flex items-center justify-between">
                        <div>
                            <div className="flex items-center gap-2">
                                <span className="text-sm font-medium text-zinc-200">{v.name}</span>
                                {idx === 0 && <span className="text-[10px] px-1.5 py-0.5 bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 rounded">latest</span>}
                                <span className={`text-[10px] px-1.5 py-0.5 rounded border ${
                                    v.include_metadata !== false
                                        ? 'bg-green-500/10 text-green-400 border-green-500/30'
                                        : 'bg-zinc-800 text-zinc-500 border-zinc-700'
                                }`}>
                                    {v.include_metadata !== false ? 'Full snapshot' : 'Hash-only'}
                                </span>
                            </div>
                            {v.description && <div className="text-xs text-zinc-500 mt-0.5">{v.description}</div>}
                            <div className="text-xs text-zinc-600 mt-0.5">
                                {v.sample_count.toLocaleString()} samples • {new Date(v.created_at).toLocaleDateString()}
                                {' '}• {versionMaker(v)}
                            </div>
                        </div>
                        <div className="flex items-center gap-2">
                            <button
                                onClick={() => { setDiffVsCurrent(v.id); setDiffPair(null); }}
                                className={`text-xs px-2 py-1 rounded transition-colors ${
                                    diffVsCurrent === v.id
                                        ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/50'
                                        : 'text-zinc-400 hover:text-cyan-400 border border-zinc-700 hover:border-cyan-500/50'
                                }`}
                            >
                                Diff vs Current
                            </button>
                            {v.include_metadata !== false && <Link href={`/datasets/${datasetId}/versions/${v.id}`} className="text-xs text-cyan-400 px-2 py-1">Browse snapshot</Link>}
                            {v.include_metadata !== false && versionStatus?.current_version?.id !== v.id && (
                                <button
                                    onClick={() => handleRevert(v)}
                                    disabled={!isAdmin || countingRevert !== null}
                                    title={isAdmin ? undefined : 'Reverting a dataset takes an admin: it can delete samples and saved views'}
                                    className="text-xs text-zinc-400 hover:text-amber-400 border border-zinc-700 hover:border-amber-500/50 px-2 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                    {countingRevert === v.id ? 'Counting…' : exactMembership ? 'Revert to this snapshot' : 'Restore sample data'}
                                </button>
                            )}
                            {versionStatus?.current_version?.id === v.id && (
                                <span className="text-xs text-green-400 px-2 py-1">current</span>
                            )}
                            <button
                                onClick={() => {
                                    // A deleted version cannot be restored; revert already asks first.
                                    if (!confirm(`Delete version "${v.name}"?\n\nThis cannot be undone.`)) return;
                                    handleDelete(v.id);
                                }}
                                disabled={!isAdmin}
                                title={isAdmin ? undefined : 'Deleting a version takes an admin: it cannot be undone'}
                                className="text-xs text-zinc-600 hover:text-red-400 transition-colors disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:text-zinc-600"
                            >
                                Delete
                            </button>
                        </div>
                    </div>
                </div>
            ))}

            {/* Diff results */}
            {diffData && (
                <div className="border border-cyan-800/50 bg-cyan-900/10 rounded-lg p-3 space-y-2">
                    <div className="text-sm font-medium text-cyan-400">
                        {diffData.vs === 'current' ? 'Changes since this version' : `Diff: v${diffData.version_a} → v${diffData.version_b}`}
                    </div>
                    {diffData.dataset_changed === true && <p className="text-xs text-amber-300">Dataset schema or display settings also changed.</p>}
                    {diffViewChanges && (
                        <p className="text-xs text-amber-300" title={diffViewChanges.names} data-testid="diff-view-changes">
                            Saved views also changed: {diffViewChanges.text}.
                        </p>
                    )}
                    <div className="grid grid-cols-2 gap-2 text-sm">
                        {[
                            { type: 'added', color: 'green', count: diffData.added || 0 },
                            { type: 'removed', color: 'red', count: diffData.removed || 0 },
                            { type: 'modified', color: 'yellow', count: diffData.modified || 0 },
                            { type: 'unchanged', color: 'zinc', count: diffData.unchanged || 0 },
                        ].map(({ type, color, count }) => (
                            <button
                                key={type}
                                disabled={count === 0 || !onViewSamples || type === 'unchanged'}
                                onClick={async () => {
                                    if (!onViewSamples || count === 0) return;
                                    const versionId = diffVsCurrent || diffPair?.[0];
                                    if (!versionId) return;
                                    if (diffPair || type !== 'added') {
                                        const viewedVersion = diffPair && type === 'added' ? diffPair[1] : versionId;
                                        const params = new URLSearchParams({ change_type: type, diff_base: String(versionId) });
                                        if (diffPair) params.set('diff_target', String(diffPair[1]));
                                        // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- Open the snapshot in a fresh document so live dataset panel state cannot carry over.
                                        window.location.assign(`/datasets/${datasetId}/versions/${viewedVersion}?${params}`);
                                        return;
                                    }
                                    const url = diffData.vs === 'current'
                                        ? `${API_BASE}/datasets/${datasetId}/versions/current-diff/${versionId}/select?change_type=${type}`
                                        : `${API_BASE}/datasets/${datasetId}/versions/diff/${diffPair![0]}/${diffPair![1]}/select?change_type=${type}`;
                                    // Materialize the FULL change set server-side into a selection
                                    // token; the grid scopes via in_selection — no id list is paged
                                    // to or held by the client (gate-pass V5 paged ALL ids).
                                    try {
                                        const res = await fetch(url, { method: 'POST' });
                                        if (!res.ok) {
                                            if (res.status === 401) window.dispatchEvent(new Event('fridge:unauthorized'));
                                            throw new Error(`HTTP ${res.status}`);
                                        }
                                        const data = await res.json();
                                        if (data.token) onViewSamples(data.token, data.total ?? count, `${type} (${count.toLocaleString()})`);
                                    } catch (e) {
                                        alert(`Couldn't open these samples: ${(e as Error).message}`);
                                    }
                                }}
                                className={`flex items-center gap-2 text-left ${count > 0 && onViewSamples && type !== 'unchanged' ? 'cursor-pointer hover:underline' : ''}`}
                            >
                                <span className={`w-2 h-2 rounded-full bg-${color}-500`}></span>
                                <span className={`text-${color}-400`}>{count.toLocaleString()} {type}</span>
                            </button>
                        ))}
                    </div>
                    {onViewSamples && <div className="text-xs text-zinc-600">Click a count to view those samples</div>}
                </div>
            )}

            <div className="text-xs text-zinc-500">
                Versions capture a snapshot of all sample metadata. Compare versions to see what changed.
            </div>
        </div>
    );
}

interface Props {
    dataset: Dataset;
    onClose: () => void;
    readOnly?: boolean;
    onViewSamples?: (token: string, total: number, label: string) => void;
}

const DECLARED_TYPES: DeclaredFieldType[] = ['unknown', 'boolean', 'number', 'string', 'datetime', 'list', 'object'];
const REBUILDING_NOTE = 'Its index is being rebuilt in the background (Jobs shows its progress). Until then it filters and sorts correctly, only slower.';

function FieldTypeRow({ datasetId, field, readOnly, onSaved }: {
    datasetId: number; field: SchemaField; readOnly: boolean; onSaved: () => Promise<void>;
}) {
    const [fieldType, setType] = useState<DeclaredFieldType>(field.declared_type || 'unknown');
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const disabled = readOnly || saving || field.is_promoted;

    const save = async () => {
        setSaving(true);
        setError(null);
        try {
            await setFieldType(datasetId, field.name, fieldType);
            await onSaved();
        } catch (failure) {
            setError(failure instanceof Error ? failure.message : 'Could not change the field type');
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="border border-zinc-800 rounded-lg p-3 space-y-2">
            <div className="flex items-center gap-3">
                <span className="flex-1 text-sm font-mono text-zinc-200 truncate" title={field.name}>{field.name}</span>
                <select aria-label={`Declared type for ${field.name}`} value={fieldType} disabled={disabled}
                    onChange={event => { setType(event.target.value as DeclaredFieldType); setError(null); }}
                    className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm text-zinc-200 disabled:opacity-50">
                    {DECLARED_TYPES.map(type => <option key={type} value={type}>{type}</option>)}
                </select>
                {!readOnly && <button aria-label={`Save type for ${field.name}`} onClick={save}
                    disabled={disabled || fieldType === field.declared_type}
                    className="px-3 py-1 text-sm bg-cyan-700 hover:bg-cyan-600 rounded text-white disabled:opacity-40">
                    {saving ? 'Saving…' : 'Save'}
                </button>}
            </div>
            {field.is_promoted && <p className="text-xs text-zinc-500">Remove this field&apos;s index in the Indexes tab before changing its type.</p>}
            {error && <p role="alert" className="text-xs text-red-400">{error}</p>}
        </div>
    );
}

type SettingsTab = 'general' | 'fields' | 'indexes' | 'versions' | 'segmentation';

export function DatasetSettingsModal({ dataset, onClose, readOnly = false, onViewSamples }: Props) {
    const queryClient = useQueryClient();
    const { user } = useUser();
    const [activeTab, setActiveTab] = useState<SettingsTab>('general');
    const [name, setName] = useState(dataset.name);
    const [description, setDescription] = useState(dataset.description || '');
    const [isGlobal, setIsGlobal] = useState(dataset.is_global);
    const [mediaFields, setMediaFields] = useState<string[]>(dataset.media_fields || ['filepath']);
    const [selectedField, setSelectedField] = useState('');
    const [segmentationConfig, setSegmentationConfig] = useState<SegmentationConfig>(
        dataset.segmentation_config || {}
    );

    // Fetch schema to get available metadata fields and polygon labels
    const { data: schema } = useQuery({
        queryKey: ['dataset-schema', dataset.id],
        queryFn: () => getDatasetSchema(dataset.id)
    });

    // Filter for filepath fields that aren't already selected
    const availableFields = schema?.filter(field =>
        field.type === 'filepath' &&
        !mediaFields.includes(field.name)
    ) || [];

    const labelFields = useMemo(() => (schema || [])
        .filter(field => field.type === 'detections' || field.type === 'polygons')
        .map(field => ({ name: field.name, labels: schema?.find(f => f.name === `${field.name}.label`)?.values || [] })), [schema]);
    const segmentationLabels = segmentationLabelChoices(
        schema?.find(f => f.name === 'segmentation.labels')?.values || [], segmentationConfig);


    const [togglingField, setTogglingField] = useState<string | null>(null);
    const [indexStatus, setIndexStatus] = useState<string | null>(null);

    // Indexable fields: number, string, enum and datetime types (not arrays, objects, filepath)
    const indexableFields = useMemo(() => {
        if (!schema) return [];
        const mediaFieldSet = new Set(dataset.media_fields || ['filepath']);
        return schema.filter(f => {
            const origType = (f as SchemaField & { original_type?: string }).original_type || f.type;
            return (origType === 'number' || origType === 'enum' || origType === 'string' || origType === 'datetime' || f.type === 'number' || f.type === 'enum') &&
                !['id', 'created_at', 'last_modified_at', 'media_type'].includes(f.name) &&
                !mediaFieldSet.has(f.name) &&
                !f.name.includes('.');
        });
    }, [schema, dataset.media_fields]);

    const handleToggleIndex = async (fieldName: string, currentlyPromoted: boolean) => {
        setTogglingField(fieldName);
        const action = currentlyPromoted ? 'Removing' : 'Creating';
        setIndexStatus(`${action} index for "${fieldName}"...`);
        try {
            if (currentlyPromoted) {
                await demoteField(dataset.id, fieldName);
            } else {
                await promoteField(dataset.id, fieldName, waitingFor => setIndexStatus(waitingFor
                    ? `The index for "${fieldName}" is waiting ${waitingFor}`
                    : `${action} index for "${fieldName}"...`));
            }
            setIndexStatus(`Done — "${fieldName}" ${currentlyPromoted ? 'demoted' : 'promoted'}`);
            queryClient.invalidateQueries({ queryKey: ['dataset-schema', dataset.id] });
            queryClient.invalidateQueries({ queryKey: ['schema'] });
            setTimeout(() => setIndexStatus(null), 3000);
        } catch (e: unknown) {
            setIndexStatus(`Failed: ${(e instanceof Error && e.message) || 'unknown error'}`);
            setTimeout(() => setIndexStatus(null), 5000);
        } finally {
            setTogglingField(null);
        }
    };

    const updateMutation = useMutation({
        mutationFn: (data: Partial<Dataset>) => updateDataset(dataset.id, data),
        onSuccess: (updated) => {
            // Reopening the dialog before the refetch lands must show the saved values, or the next
            // Save writes the old ones back. PATCH returns no sidebar_expand_all, so keep the cached one.
            queryClient.setQueryData<Dataset>(['dataset', dataset.id], old => old && { ...updated, sidebar_expand_all: old.sidebar_expand_all });
            queryClient.invalidateQueries({ queryKey: ['dataset', dataset.id] });
            onClose();
        }
    });

    const handleAddField = () => {
        if (selectedField && !mediaFields.includes(selectedField)) {
            setMediaFields([...mediaFields, selectedField]);
            setSelectedField('');
        }
    };

    const handleRemoveField = (field: string) => {
        setMediaFields(mediaFields.filter(f => f !== field));
    };

    const handleSave = () => {
        updateMutation.mutate({
            name,
            description,
            is_global: isGlobal,
            media_fields: mediaFields,
            segmentation_config: segmentationConfig
        });
    };

    return (
        <Dialog.Root open={true} onOpenChange={onClose}>
            <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50" />
                <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-full max-w-2xl bg-zinc-900 border border-zinc-800 rounded-xl shadow-2xl z-50 max-h-[85vh] flex flex-col">
                    <Dialog.Title className="text-xl font-bold text-white p-6 pb-0">
                        Dataset Settings
                    </Dialog.Title>

                    {/* Tab Navigation */}
                    <div className="flex gap-1 px-6 pt-4 border-b border-zinc-800">
                        <button
                            onClick={() => setActiveTab('general')}
                            className={`px-4 py-2 text-sm font-medium rounded-t transition-colors ${activeTab === 'general'
                                ? 'bg-zinc-800 text-white border-b-2 border-cyan-500'
                                : 'text-zinc-400 hover:text-white hover:bg-zinc-800/50'
                                }`}
                        >
                            General
                        </button>
                        <button
                            onClick={() => setActiveTab('fields')}
                            className={`px-4 py-2 text-sm font-medium rounded-t transition-colors ${activeTab === 'fields'
                                ? 'bg-zinc-800 text-white border-b-2 border-cyan-500'
                                : 'text-zinc-400 hover:text-white hover:bg-zinc-800/50'
                                }`}
                        >
                            Fields
                        </button>
                        <button
                            onClick={() => setActiveTab('indexes')}
                            className={`px-4 py-2 text-sm font-medium rounded-t transition-colors ${activeTab === 'indexes'
                                ? 'bg-zinc-800 text-white border-b-2 border-cyan-500'
                                : 'text-zinc-400 hover:text-white hover:bg-zinc-800/50'
                                }`}
                        >
                            Indexes
                        </button>
                        <button
                            onClick={() => setActiveTab('versions')}
                            className={`px-4 py-2 text-sm font-medium rounded-t transition-colors ${activeTab === 'versions'
                                ? 'bg-zinc-800 text-white border-b-2 border-cyan-500'
                                : 'text-zinc-400 hover:text-white hover:bg-zinc-800/50'
                                }`}
                        >
                            Versions
                        </button>
                        <button
                            onClick={() => setActiveTab('segmentation')}
                            className={`px-4 py-2 text-sm font-medium rounded-t transition-colors ${activeTab === 'segmentation'
                                ? 'bg-zinc-800 text-white border-b-2 border-cyan-500'
                                : 'text-zinc-400 hover:text-white hover:bg-zinc-800/50'
                                }`}
                        >
                            Visualization
                        </button>
                    </div>

                    {/* Tab Content */}
                    <div className="flex-1 overflow-y-auto p-6">
                        {activeTab === 'fields' && (
                            <div className="space-y-3">
                                <p className="text-sm text-zinc-400">
                                    Correct how metadata fields are interpreted. This does not convert or rewrite sample values.
                                    Display overrides and cached statistics are reset; statistics refresh separately.
                                    Native and system fields have fixed types.
                                </p>
                                {!schema && <p className="text-sm text-zinc-500">Loading field definitions…</p>}
                                {schema?.filter(field => field.origin === 'metadata' && field.declared_type).map(field => (
                                    <FieldTypeRow key={`${field.name}:${field.declared_type}`} datasetId={dataset.id}
                                        field={field} readOnly={readOnly} onSaved={async () => {
                                            await Promise.all([
                                                queryClient.invalidateQueries({ queryKey: ['dataset-schema', dataset.id] }),
                                                queryClient.invalidateQueries({ queryKey: ['schema'] }),
                                                queryClient.invalidateQueries({ queryKey: ['version-status', dataset.id] }),
                                            ]);
                                        }} />
                                ))}
                                {schema && !schema.some(field => field.origin === 'metadata' && field.declared_type) && (
                                    <p className="text-sm text-zinc-500">No metadata fields to edit.</p>
                                )}
                            </div>
                        )}
                        {activeTab === 'general' && (
                            <div className="space-y-4">
                                <div>
                                    <label className="block text-sm font-medium text-zinc-400 mb-1">Name</label>
                                    <input
                                        type="text"
                                        value={name}
                                        onChange={e => setName(e.target.value)}
                                        disabled={readOnly}
                                        className={`w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:outline-none focus:border-zinc-500 ${readOnly ? 'opacity-60 cursor-not-allowed' : ''}`}
                                    />
                                </div>

                                <div>
                                    <label className="block text-sm font-medium text-zinc-400 mb-1">Description</label>
                                    <textarea
                                        value={description}
                                        onChange={e => setDescription(e.target.value)}
                                        disabled={readOnly}
                                        className={`w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:outline-none focus:border-zinc-500 h-24 ${readOnly ? 'opacity-60 cursor-not-allowed' : ''}`}
                                    />
                                </div>

                                <div>
                                    <label htmlFor="dataset-visibility" className="block text-sm font-medium text-zinc-400 mb-1">Visibility</label>
                                    <select
                                        id="dataset-visibility"
                                        value={isGlobal ? 'everyone' : 'private'}
                                        onChange={e => setIsGlobal(e.target.value === 'everyone')}
                                        disabled={readOnly}
                                        className={`w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:outline-none focus:border-zinc-500 ${readOnly ? 'opacity-60 cursor-not-allowed' : ''}`}
                                    >
                                        <option value="private">Private</option>
                                        <option value="everyone">Everyone signed in</option>
                                    </select>
                                    <p className="mt-1 text-xs text-zinc-500">
                                        {isGlobal
                                            ? 'Every signed-in user can see this dataset, demo guests included.'
                                            : 'Only admins, its owner and users granted access can see this dataset.'}
                                    </p>
                                </div>

                                <div>
                                    <label className="block text-sm font-medium text-zinc-400 mb-1">Media Fields</label>
                                    {!readOnly && (
                                        <div className="flex gap-2 mb-2">
                                            <select
                                                value={selectedField}
                                                onChange={e => setSelectedField(e.target.value)}
                                                className="flex-1 bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white focus:outline-none focus:border-zinc-500"
                                            >
                                                <option value="">Select a field...</option>
                                                {availableFields.map(field => (
                                                    <option key={field.name} value={field.name}>
                                                        {field.name}
                                                    </option>
                                                ))}
                                            </select>
                                            <button
                                                onClick={handleAddField}
                                                disabled={!selectedField}
                                                className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-white rounded border border-zinc-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                            >
                                                Add
                                            </button>
                                        </div>
                                    )}
                                    <div className="flex flex-wrap gap-2">
                                        {mediaFields.map(field => (
                                            <div key={field} className="flex items-center gap-2 bg-zinc-800 rounded px-2 py-1 text-sm text-zinc-300 border border-zinc-700">
                                                <span>{field}</span>
                                                {!readOnly && (
                                                    <button
                                                        onClick={() => handleRemoveField(field)}
                                                        className="text-zinc-500 hover:text-red-400"
                                                    >
                                                        ×
                                                    </button>
                                                )}
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </div>
                        )}

                        {activeTab === 'indexes' && (
                            <div className="space-y-4">
                                <div>
                                    <p className="text-sm text-zinc-400 mb-3">
                                        Indexed fields use database indexes for fast filtering on large datasets.
                                        Promote fields you filter on frequently.
                                    </p>
                                </div>

                                <div className="border border-zinc-800 rounded-lg overflow-hidden">
                                    <table className="w-full text-sm">
                                        <thead>
                                            <tr className="bg-zinc-800/50 text-zinc-400 text-left">
                                                <th className="px-3 py-2 font-medium">Field</th>
                                                <th className="px-3 py-2 font-medium">Type</th>
                                                <th className="px-3 py-2 font-medium text-center">Display</th>
                                                <th className="px-3 py-2 font-medium text-center">Indexed</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {indexableFields.map(field => {
                                                const isPromoted = field.is_promoted || false;
                                                // Not indexed until its index is rebuilt in the background (after an upgrade).
                                                const isRebuilding = !isPromoted && !!field.index_rebuilding;
                                                // Not started while a bulk job keeps the dataset's fields (a stopped one too): say which.
                                                const rebuildNote = field.index_rebuild_waiting
                                                    ? `Its index rebuild is waiting: ${field.index_rebuild_waiting}`
                                                    : REBUILDING_NOTE;
                                                const rebuildLabel = field.index_rebuild_waiting ? 'Waiting' : 'Rebuilding';
                                                const isToggling = togglingField === field.name;
                                                return (
                                                    <tr key={field.name} className="border-t border-zinc-800 hover:bg-zinc-800/30">
                                                        <td className="px-3 py-2 text-zinc-200 font-mono text-xs">{field.name}</td>
                                                        <td className="px-3 py-2 text-zinc-400 text-xs">{field.type}</td>
                                                        <td className="px-3 py-2 text-zinc-400 text-xs text-center">
                                                            {((field as SchemaField & { original_type?: string }).original_type === 'number' || field.type === 'number') && !readOnly ? (
                                                                <select
                                                                    value={field.display_type || 'slider'}
                                                                    onChange={async (e) => {
                                                                        const val = e.target.value === 'slider' ? null : e.target.value;
                                                                        await setFieldDisplayType(dataset.id, field.name, val);
                                                                        queryClient.invalidateQueries({ queryKey: ['dataset-schema', dataset.id] });
                                                                        queryClient.invalidateQueries({ queryKey: ['schema'] });
                                                                    }}
                                                                    className="bg-zinc-800 border border-zinc-700 text-xs text-zinc-300 rounded px-1 py-0.5"
                                                                >
                                                                    <option value="slider">Slider</option>
                                                                    <option value="enum">Dropdown</option>
                                                                </select>
                                                            ) : (
                                                                <span className="text-zinc-500">
                                                                    {field.type === 'enum' ? 'Dropdown' : field.type === 'number' ? 'Slider' : '—'}
                                                                </span>
                                                            )}
                                                        </td>
                                                        <td className="px-3 py-2 text-center">
                                                            {readOnly ? (
                                                                <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${
                                                                    isPromoted
                                                                        ? 'bg-yellow-400/15 text-yellow-400 border border-yellow-400/30'
                                                                        : isRebuilding ? 'text-cyan-400' : 'text-zinc-600'
                                                                }`} title={isRebuilding ? rebuildNote : undefined}>
                                                                    {isPromoted ? <><Rocket className="w-3 h-3" /> Yes</> : isRebuilding ? rebuildLabel : 'No'}
                                                                </span>
                                                            ) : (
                                                                <button
                                                                    onClick={() => handleToggleIndex(field.name, isPromoted)}
                                                                    disabled={isToggling}
                                                                    className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium transition-colors ${
                                                                        isToggling ? 'opacity-50 cursor-wait' :
                                                                        isPromoted
                                                                            ? 'bg-yellow-400/15 text-yellow-400 border border-yellow-400/30 hover:bg-yellow-400/25'
                                                                            : isRebuilding
                                                                                ? 'bg-cyan-900/20 text-cyan-400 border border-cyan-800 hover:text-yellow-400 hover:border-yellow-400/30'
                                                                                : 'bg-zinc-800 text-zinc-500 border border-zinc-700 hover:text-yellow-400 hover:border-yellow-400/30'
                                                                    }`}
                                                                    title={isPromoted ? 'Click to remove index' : isRebuilding ? `${rebuildNote} Click to build it now.` : 'Click to add index'}
                                                                    data-testid={isRebuilding ? 'index-rebuilding' : undefined}
                                                                >
                                                                    <Rocket className="w-3 h-3" />
                                                                    {isToggling ? '...' : isPromoted ? 'Yes' : isRebuilding ? rebuildLabel : 'No'}
                                                                </button>
                                                            )}
                                                        </td>
                                                    </tr>
                                                );
                                            })}
                                            {indexableFields.length === 0 && (
                                                <tr>
                                                    <td colSpan={4} className="px-3 py-4 text-center text-zinc-500 text-sm">
                                                        No indexable fields found. Add samples with metadata first.
                                                    </td>
                                                </tr>
                                            )}
                                        </tbody>
                                    </table>
                                </div>

                                {indexStatus && (
                                    <div className={`text-sm px-3 py-2 rounded border ${
                                        indexStatus.startsWith('Done')
                                            ? 'bg-green-900/20 border-green-800 text-green-400'
                                            : indexStatus.startsWith('Failed')
                                                ? 'bg-red-900/20 border-red-800 text-red-400'
                                                : 'bg-cyan-900/20 border-cyan-800 text-cyan-400 animate-pulse'
                                    }`}>
                                        {indexStatus}
                                    </div>
                                )}

                                <div className="text-xs text-zinc-500 space-y-1">
                                    <p>Indexed fields are faster to filter and sort, especially on large datasets.</p>
                                    <p>Non-indexed fields use cached global statistics and may show stale filter ranges.</p>
                                </div>

                                {user?.role === 'admin' && (
                                    <div className="border-t border-zinc-800 pt-4">
                                        <IndexAdvisor datasetId={dataset.id} />
                                    </div>
                                )}
                            </div>
                        )}

                        {activeTab === 'versions' && (
                            <VersionsPanel datasetId={dataset.id} mirror={dataset.mirror} role={user?.role} onViewSamples={onViewSamples} />
                        )}

                        {activeTab === 'segmentation' && (
                            <div className="space-y-8">
                                <HeatmapOverlayConfig
                                    datasetId={dataset.id}
                                    schema={schema || []}
                                    readOnly={readOnly}
                                    onSaved={() => {
                                        queryClient.invalidateQueries({ queryKey: ['dataset-schema', dataset.id] });
                                        queryClient.invalidateQueries({ queryKey: ['schema'] });
                                    }}
                                />
                                <MaskOverlayConfig
                                    datasetId={dataset.id}
                                    schema={schema || []}
                                    readOnly={readOnly}
                                    onSaved={() => {
                                        queryClient.invalidateQueries({ queryKey: ['dataset-schema', dataset.id] });
                                        queryClient.invalidateQueries({ queryKey: ['schema'] });
                                    }}
                                />
                                <SegmentationSettingsPanel
                                    config={segmentationConfig}
                                    onChange={readOnly ? () => { } : setSegmentationConfig}
                                    labelFields={labelFields}
                                    availableSegmentationLabels={segmentationLabels}
                                    readOnly={readOnly}
                                />
                            </div>
                        )}
                    </div>

                    {/* Footer */}
                    <div className="p-6 pt-0 flex justify-end gap-3 border-t border-zinc-800 mt-auto">
                        {readOnly && (
                            <span className="text-sm text-zinc-500 mr-auto">
                                View only — contact an admin to make changes
                            </span>
                        )}
                        <button
                            onClick={onClose}
                            className="px-4 py-2 text-zinc-400 hover:text-white transition-colors"
                        >
                            {(activeTab === 'versions' || activeTab === 'indexes' || readOnly) ? 'Close' : 'Cancel'}
                        </button>
                        {!readOnly && (activeTab === 'general' || activeTab === 'segmentation') && (
                            <button
                                onClick={handleSave}
                                disabled={updateMutation.isPending}
                                className="px-4 py-2 bg-white text-black rounded hover:bg-zinc-200 transition-colors disabled:opacity-50"
                            >
                                {updateMutation.isPending ? 'Saving...' : 'Save Changes'}
                            </button>
                        )}
                    </div>
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}
