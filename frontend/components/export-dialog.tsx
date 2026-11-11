'use client';

import { isDerivedLabelField } from '@/lib/labels';

import * as Dialog from '@radix-ui/react-dialog';
import { Cross2Icon } from '@radix-ui/react-icons';
import { Download, Rocket, X } from 'lucide-react';
import { useState, useMemo, useCallback, useRef, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useFilters, withGrain } from '@/contexts/FilterContext';
import { getSamples, type ComplexFilter, type SchemaField, type Dataset } from '@/lib/api';
import { isLocalField, localValue, type LocalFieldsHolder } from '@/lib/local-fields';

const SYSTEM_FIELDS = ['filepath', 'id', 'media_type', 'tags', 'created_at', 'last_modified_at'] as const;
const SYSTEM_FIELDS_SET = new Set<string>(SYSTEM_FIELDS);
const NON_EXPORTABLE_FIELDS = new Set(['location', 'metadata', 'segmentation.labels']);
const BATCH_SIZE = 5000;

type ExportScope = 'applied' | 'current_view' | 'selected';

interface ExportDialogProps {
    datasetId: number;
    schema: SchemaField[];
    dataset?: Dataset;
    appliedCount?: number;
    /**
     * Whether this dataset contains object/patch rows. Required so the export can apply the same
     * IMAGE grain every other consumer applies (see `toExportFilter`); without it the CSV silently
     * contained image rows PLUS every matching `media_type='object'` patch row.
     */
    hasObjects?: boolean;
    /** False when the grid it was opened from doesn't show the selection (one of the other grain). */
    offerSelection?: boolean;
    onClose: () => void;
}

function escapeCsvField(value: unknown): string {
    if (value === null || value === undefined) return '';
    if (Array.isArray(value)) {
        const joined = value.map(v => (v === null || v === undefined) ? '' : String(v)).join(';');
        if (joined.includes(',') || joined.includes('"') || joined.includes('\n') || joined.includes('\r')) {
            return `"${joined.replace(/"/g, '""')}"`;
        }
        return joined;
    }
    const str = typeof value === 'object' ? JSON.stringify(value) : String(value);
    if (str.includes(',') || str.includes('"') || str.includes('\n') || str.includes('\r')) {
        return `"${str.replace(/"/g, '""')}"`;
    }
    return str;
}

// Slim sample row from /samples/query: id + requested top-level fields, with
// metadata fields nested under `metadata`.
type ExportItem = { id: number; metadata?: Record<string, unknown> } & Record<string, unknown>;

function getFieldValue(item: ExportItem, field: string): unknown {
    if (SYSTEM_FIELDS_SET.has(field)) return item[field];
    if (isLocalField(field)) return localValue(item as LocalFieldsHolder, field);  // a mirror's own field
    // Metadata fields live under item.metadata, but native array columns
    // (e.g. inferred_tags) are top-level. Check both.
    return item.metadata?.[field] ?? item[field];
}

export function ExportDialog({ datasetId, schema, dataset, appliedCount, hasObjects = false, offerSelection = true, onClose }: ExportDialogProps) {
    const {
        globalFilters,
        stagedFilters,
        viewStages,
        selectedIds,
        selectAll,
        deselectedIds,
        selectionCount,
        simpleFilters,
        advancedFilters,
        brushedIds,
        currentPolygon,
    } = useFilters();

    // Available scope conditions
    const hasAppliedFilters = viewStages.length > 0;
    const hasDraftFilters =
        Object.keys(simpleFilters).length > 0 ||
        (advancedFilters.filters && advancedFilters.filters.length > 0) ||
        brushedIds.length > 0 ||
        currentPolygon !== null;
    // A Select-All over a large view is a server-side token, not an id list: the dialog offered
    // only "Applied filters", which silently put the deselected tiles back.
    const hasSelection = offerSelection && selectionCount > 0;

    // Prioritize scope: selection > draft > applied (most specific first)
    const initialScope: ExportScope = hasSelection
        ? 'selected'
        : hasDraftFilters
            ? 'current_view'
            : 'applied';
    const [scope, setScope] = useState<ExportScope>(initialScope);

    // If user's chosen scope disappears mid-session (unlikely but possible), fall back
    const effectiveScope: ExportScope = useMemo(() => {
        if (scope === 'applied' && hasAppliedFilters) return 'applied';
        if (scope === 'current_view' && hasDraftFilters) return 'current_view';
        if (scope === 'selected' && hasSelection) return 'selected';
        if (hasSelection) return 'selected';
        if (hasDraftFilters) return 'current_view';
        return 'applied';
    }, [scope, hasAppliedFilters, hasDraftFilters, hasSelection]);

    // Available schema fields (metadata), filtered to exportable ones
    const metadataFields = useMemo(() => {
        const promoted = new Set<string>();
        const regular: string[] = [];
        schema.forEach(f => {
            if (NON_EXPORTABLE_FIELDS.has(f.name) || isDerivedLabelField(f.name, schema)) return;
            if (SYSTEM_FIELDS_SET.has(f.name)) return;
            if (f.is_promoted) promoted.add(f.name);
            regular.push(f.name);
        });
        regular.sort();
        return { fields: regular, promoted };
    }, [schema]);

    // Field selection state
    const [selectedFields, setSelectedFields] = useState<Set<string>>(() => new Set(['filepath']));

    const toggleField = (field: string) => {
        setSelectedFields(prev => {
            const next = new Set(prev);
            if (next.has(field)) next.delete(field);
            else next.add(field);
            return next;
        });
    };

    const selectAllFields = () => {
        const all = new Set<string>([...SYSTEM_FIELDS, ...metadataFields.fields]);
        setSelectedFields(all);
    };

    const resetFields = () => {
        setSelectedFields(new Set(['filepath']));
    };

    // Export is always IMAGE grain. Objects are an opt-in per-grid grain; every other consumer of
    // a stage tree (top-bar count, grid, map, histogram) wraps it with withGrain before querying,
    // and the export dialog was the one that didn't. On a dataset with object rows that meant the
    // dialog displayed the image-only `appliedCount` while the CSV contained those images PLUS
    // every matching media_type='object' patch row — with no filter set at all, since withGrain's
    // empty-base branch would still have emitted the grain predicate. Both the rows and the count
    // below go through this, so they can't disagree again.
    const toExportFilter = useCallback(
        (base: ComplexFilter) => withGrain(base, 'image', null, hasObjects),
        [hasObjects],
    );

    // Count query for "current view" scope (applied + drafts)
    // The "applied filters" count is passed in as a prop from the page's existing query, which is
    // already image-grained (page.tsx `imageGrainFilters`).
    const currentViewFilter = useMemo(() => toExportFilter(stagedFilters), [toExportFilter, stagedFilters]);
    const { data: currentViewCount, isLoading: loadingCurrentCount } = useQuery({
        queryKey: ['export-count', datasetId, 'current_view', currentViewFilter],
        queryFn: async () => {
            const data = await getSamples(datasetId, 0, 0, currentViewFilter);
            return data.total;
        },
        enabled: !!datasetId && hasDraftFilters,
        staleTime: 10_000,
    });

    const getCountForScope = (s: ExportScope): number | undefined => {
        switch (s) {
            case 'applied': return appliedCount;
            case 'current_view': return currentViewCount;
            case 'selected': return selectionCount;
        }
    };

    const getFilterForScope = useCallback((s: ExportScope): ComplexFilter => {
        switch (s) {
            case 'applied':
                return toExportFilter(globalFilters);
            case 'current_view':
                return currentViewFilter;
            case 'selected':
                // An explicit id list is already grain-resolved — the ids are whatever the user
                // actually picked — so wrapping it in the image grain would drop any selected
                // object rows. Export exactly what was selected. A token Select-All is the same
                // set the grid shows: the token's rows minus the unticked ones.
                if (selectAll) {
                    const inToken: ComplexFilter = { field: 'id', operator: 'in_selection', value: selectAll.token };
                    return {
                        logic: 'AND',
                        filters: deselectedIds.length > 0
                            ? [inToken, { field: 'id', operator: 'not in', value: deselectedIds }]
                            : [inToken],
                    } as ComplexFilter;
                }
                return {
                    logic: 'AND',
                    filters: [{ field: 'id', operator: 'in', value: selectedIds }],
                } as ComplexFilter;
        }
    }, [toExportFilter, globalFilters, currentViewFilter, selectedIds, selectAll, deselectedIds]);

    // Export state
    const [exporting, setExporting] = useState(false);
    const [progress, setProgress] = useState({ fetched: 0, total: 0 });
    const [error, setError] = useState<string | null>(null);
    const abortRef = useRef(false);

    useEffect(() => {
        // If dialog closes or unmounts, abort any in-flight export
        return () => { abortRef.current = true; };
    }, []);

    const handleExport = async () => {
        const filter = getFilterForScope(effectiveScope);
        const fields = Array.from(selectedFields);
        const total = getCountForScope(effectiveScope) ?? 0;

        if (fields.length === 0 || total === 0) return;

        setError(null);
        setExporting(true);
        setProgress({ fetched: 0, total });
        abortRef.current = false;

        // Build API fields list — system fields pass through, metadata fields get `metadata.` prefix
        // A mirror's own `@local.` fields are asked for by their names.
        const apiFields = fields.map(f => SYSTEM_FIELDS_SET.has(f) || isLocalField(f) ? f : `metadata.${f}`);

        // CSV header
        const header = fields.map(escapeCsvField).join(',');
        const csvLines: string[] = [header];

        let afterId: number | null = null;
        let fetched = 0;

        try {
            while (!abortRef.current) {
                const body: Record<string, unknown> = {
                    limit: BATCH_SIZE,
                    filters: filter,
                    fields: apiFields.join(','),
                    slim: true,
                    skip_count: true,
                };
                if (afterId !== null) body.after_id = afterId;

                const res = await fetch(`/api/datasets/${datasetId}/samples/query`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!res.ok) {
                    // Raw fetch bypasses the api.ts ApiError choke point, so dispatch the
                    // session-expiry event ourselves — otherwise a 401 mid-export shows a generic
                    // error and never redirects to login like the rest of the app.
                    if (res.status === 401) window.dispatchEvent(new Event('fridge:unauthorized'));
                    throw new Error(`Export fetch failed: ${res.status}`);
                }
                const data = await res.json();
                const items: ExportItem[] = data.items || [];

                if (items.length === 0) break;

                for (const item of items) {
                    const row = fields.map(f => escapeCsvField(getFieldValue(item, f)));
                    csvLines.push(row.join(','));
                }

                fetched += items.length;
                setProgress({ fetched, total: Math.max(total, fetched) });
                afterId = items[items.length - 1].id;

                if (items.length < BATCH_SIZE) break;
            }

            if (abortRef.current) return;
            if (fetched === 0 && effectiveScope === 'selected' && selectAll) {
                // The server forgets a Select-All token after 2 hours; nothing left means it expired.
                throw new Error('This selection has expired (selections last 2 hours). Select the samples again.');
            }

            // Trigger download
            const content = csvLines.join('\n');
            const blob = new Blob([content], { type: 'text/csv;charset=utf-8;' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            const scopeLabel = effectiveScope === 'applied' ? (hasAppliedFilters ? 'filtered' : 'all')
                : effectiveScope === 'current_view' ? 'view'
                    : 'selected';
            a.download = `${dataset?.name || 'fridge'}_${fetched}_${scopeLabel}.csv`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);

            // Done — close the dialog after a short beat so the user sees 100%
            setTimeout(() => {
                if (!abortRef.current) onClose();
            }, 400);
        } catch (e: unknown) {
            console.error('Export failed:', e);
            setError((e instanceof Error && e.message) || 'Export failed');
        } finally {
            setExporting(false);
        }
    };

    const handleCancel = () => {
        if (exporting) {
            abortRef.current = true;
            setExporting(false);
        } else {
            onClose();
        }
    };

    const exportCount = getCountForScope(effectiveScope);
    const fieldCount = selectedFields.size;
    const canRunExport = !exporting && fieldCount > 0 && typeof exportCount === 'number' && exportCount > 0;

    return (
        <Dialog.Root open={true} onOpenChange={(open) => { if (!open) handleCancel(); }}>
            <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50" />
                <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-full max-w-xl max-h-[85vh] bg-zinc-900 border border-zinc-800 rounded-xl shadow-2xl z-50 flex flex-col">
                    {/* Header */}
                    <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-800 shrink-0">
                        <Dialog.Title className="text-lg font-bold text-white flex items-center gap-2">
                            <Download className="w-4 h-4 text-cyan-400" />
                            Export Samples
                        </Dialog.Title>
                        <Dialog.Close asChild>
                            <button
                                className="text-zinc-500 hover:text-white p-1 rounded hover:bg-zinc-800 transition-colors"
                                disabled={exporting}
                                aria-label="Close"
                            >
                                <Cross2Icon className="w-4 h-4" />
                            </button>
                        </Dialog.Close>
                    </div>

                    {/* Body */}
                    <div className="flex-1 overflow-y-auto px-6 py-4 space-y-5">
                        {/* Scope */}
                        <div>
                            <label className="block text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">
                                Scope
                            </label>
                            <div className="space-y-1">
                                {/* With no stage applied this scope IS the whole dataset. */}
                                <ScopeOption
                                    label={hasAppliedFilters ? 'Applied filters' : 'Whole dataset'}
                                    description={hasAppliedFilters ? 'Samples matching the applied filter stages' : 'Every image in the dataset (no filter applied)'}
                                    count={appliedCount}
                                    checked={effectiveScope === 'applied'}
                                    disabled={exporting}
                                    onSelect={() => setScope('applied')}
                                />
                                <ScopeOption
                                    label="Current view"
                                    description="Applied filters + draft/unapplied filters"
                                    count={currentViewCount}
                                    loading={loadingCurrentCount}
                                    checked={effectiveScope === 'current_view'}
                                    disabled={!hasDraftFilters || exporting}
                                    onSelect={() => setScope('current_view')}
                                />
                                <ScopeOption
                                    label="Selected samples"
                                    description="Only the samples you've checked in the grid"
                                    count={hasSelection ? selectionCount : undefined}
                                    checked={effectiveScope === 'selected'}
                                    disabled={!hasSelection || exporting}
                                    onSelect={() => setScope('selected')}
                                />
                            </div>
                        </div>

                        {/* Fields */}
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <label className="block text-xs font-semibold text-zinc-400 uppercase tracking-wider">
                                    Fields ({fieldCount} selected)
                                </label>
                                <div className="flex gap-3 text-[11px]">
                                    <button
                                        onClick={selectAllFields}
                                        disabled={exporting}
                                        className="text-cyan-400 hover:text-cyan-300 disabled:text-zinc-600"
                                    >Select all</button>
                                    <button
                                        onClick={resetFields}
                                        disabled={exporting}
                                        className="text-zinc-500 hover:text-zinc-300 disabled:text-zinc-600"
                                    >Reset</button>
                                </div>
                            </div>

                            <div className="bg-zinc-950 border border-zinc-800 rounded max-h-64 overflow-y-auto">
                                {/* System fields */}
                                <div className="px-3 py-1.5 text-[10px] font-semibold text-zinc-600 uppercase tracking-wider bg-zinc-900/50 border-b border-zinc-800">
                                    System
                                </div>
                                {SYSTEM_FIELDS.map(field => (
                                    <FieldRow
                                        key={field}
                                        field={field}
                                        checked={selectedFields.has(field)}
                                        onToggle={() => toggleField(field)}
                                        disabled={exporting}
                                    />
                                ))}

                                {/* Metadata fields */}
                                {metadataFields.fields.length > 0 && (
                                    <>
                                        <div className="px-3 py-1.5 text-[10px] font-semibold text-zinc-600 uppercase tracking-wider bg-zinc-900/50 border-y border-zinc-800">
                                            Metadata
                                        </div>
                                        {metadataFields.fields.map(field => (
                                            <FieldRow
                                                key={field}
                                                field={field}
                                                checked={selectedFields.has(field)}
                                                onToggle={() => toggleField(field)}
                                                disabled={exporting}
                                                promoted={metadataFields.promoted.has(field)}
                                            />
                                        ))}
                                    </>
                                )}
                            </div>

                            <p className="mt-2 text-[11px] text-zinc-600">
                                Annotations (detections, polygons, segmentation) are not exportable yet.
                            </p>
                        </div>

                        {/* Progress */}
                        {exporting && (
                            <div className="space-y-1.5">
                                <div className="flex items-center justify-between text-[11px] text-zinc-400">
                                    <span>Fetching samples…</span>
                                    <span>
                                        {progress.fetched.toLocaleString()} / {progress.total.toLocaleString()}
                                    </span>
                                </div>
                                <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
                                    <div
                                        className="h-full bg-cyan-500 transition-all duration-200"
                                        style={{ width: `${progress.total > 0 ? Math.min(100, (progress.fetched / progress.total) * 100) : 0}%` }}
                                    />
                                </div>
                            </div>
                        )}

                        {error && (
                            <div className="text-xs text-red-400 bg-red-950/30 border border-red-900/50 rounded px-3 py-2">
                                {error}
                            </div>
                        )}
                    </div>

                    {/* Footer */}
                    <div className="flex items-center justify-between px-6 py-4 border-t border-zinc-800 shrink-0">
                        <div className="text-xs text-zinc-500">
                            {typeof exportCount === 'number'
                                ? `${exportCount.toLocaleString()} sample${exportCount === 1 ? '' : 's'} × ${fieldCount} field${fieldCount === 1 ? '' : 's'}`
                                : 'Loading…'}
                        </div>
                        <div className="flex gap-2">
                            <button
                                onClick={handleCancel}
                                className="px-4 py-2 text-sm text-zinc-300 hover:text-white bg-zinc-800 hover:bg-zinc-700 rounded transition-colors flex items-center gap-1.5"
                            >
                                {exporting ? (
                                    <>
                                        <X className="w-3.5 h-3.5" />
                                        Cancel
                                    </>
                                ) : (
                                    'Close'
                                )}
                            </button>
                            <button
                                onClick={handleExport}
                                disabled={!canRunExport}
                                className="px-4 py-2 text-sm text-white bg-cyan-600 hover:bg-cyan-500 disabled:bg-zinc-700 disabled:text-zinc-500 rounded transition-colors flex items-center gap-1.5"
                            >
                                <Download className="w-3.5 h-3.5" />
                                {exporting ? 'Exporting…' : 'Export CSV'}
                            </button>
                        </div>
                    </div>
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}

// --- Sub-components ---

interface ScopeOptionProps {
    label: string;
    description: string;
    count?: number;
    loading?: boolean;
    checked: boolean;
    disabled: boolean;
    onSelect: () => void;
}

function ScopeOption({ label, description, count, loading, checked, disabled, onSelect }: ScopeOptionProps) {
    return (
        <label
            className={`flex items-center gap-3 p-3 rounded border transition-colors ${
                disabled
                    ? 'opacity-40 cursor-not-allowed border-zinc-800 bg-zinc-950'
                    : checked
                        ? 'border-cyan-500/50 bg-cyan-500/10 cursor-pointer'
                        : 'border-zinc-800 bg-zinc-950 hover:border-zinc-700 cursor-pointer'
            }`}
        >
            <input
                type="radio"
                name="export-scope"
                checked={checked}
                disabled={disabled}
                onChange={onSelect}
                className="accent-cyan-500"
            />
            <div className="flex-1 min-w-0">
                <div className="text-sm font-medium text-zinc-200">{label}</div>
                <div className="text-[11px] text-zinc-500 truncate">{description}</div>
            </div>
            <div className="text-sm font-medium text-zinc-400 tabular-nums shrink-0">
                {disabled && count === undefined
                    ? '—'
                    : loading
                        ? '…'
                        : typeof count === 'number'
                            ? count.toLocaleString()
                            : '—'}
            </div>
        </label>
    );
}

interface FieldRowProps {
    field: string;
    checked: boolean;
    onToggle: () => void;
    disabled: boolean;
    promoted?: boolean;
}

function FieldRow({ field, checked, onToggle, disabled, promoted }: FieldRowProps) {
    return (
        <label
            className={`flex items-center gap-2 px-3 py-1.5 border-b border-zinc-900 last:border-b-0 ${
                disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer hover:bg-zinc-900/50'
            }`}
        >
            <input
                type="checkbox"
                checked={checked}
                onChange={onToggle}
                disabled={disabled}
                className="accent-cyan-500"
            />
            <span className="text-xs text-zinc-300 font-mono flex-1 truncate">{field}</span>
            {promoted && (
                <span title="Indexed field" className="shrink-0">
                    <Rocket className="w-3 h-3 text-yellow-400" />
                </span>
            )}
        </label>
    );
}