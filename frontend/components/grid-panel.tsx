'use client';

import { useState, useEffect, useRef, useSyncExternalStore } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
    deleteUnlinkedObjects, findUnlinkedObjects, getDatasetCoverage, missingFieldsStage, notifyJobSubmitted, UNLINKED_KIND_LABELS,
    UNLINKED_KINDS, unlinkedObjectsStage, type Sample, type ComplexFilter, type SchemaField, type Dataset,
} from '@/lib/api';
import { useFilters } from '@/contexts/FilterContext';
import { DatasetGrid, type GridMore } from '@/components/dataset-grid';
import { Slider } from '@/components/ui/slider';
import { Tag, Download } from 'lucide-react';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { SelectAllButton } from '@/components/select-all-button';

// Field types the grid's server sort orders by (_grid_order_by); anything else falls back to id.
const SORTABLE_TYPES = new Set(['number', 'string', 'enum', 'datetime']);
// Datasets whose coverage warning was closed: module state, so it stays hidden when the grid
// panel remounts and comes back on a page reload; every open grid panel subscribes to it.
const dismissedCoverage = new Set<number>();
const dismissListeners = new Set<() => void>();
const subscribeDismissed = (listener: () => void) => {
    dismissListeners.add(listener);
    return () => { dismissListeners.delete(listener); };
};
const dismissCoverage = (datasetId: number) => {
    dismissedCoverage.add(datasetId);
    dismissListeners.forEach(listener => listener());
};

const gridSortKey = (datasetId: number) => `fridge:gridSort:${datasetId}`;
type GridSortListener = (datasetId: number, by: string | undefined, order: 'asc' | 'desc') => void;
const gridSortListeners = new Set<GridSortListener>();
/** The sort the dataset's grids open on (a link to the dataset sets it before they open). */
export function rememberGridSort(datasetId: number, by: string | undefined, order: 'asc' | 'desc') {
    try { localStorage.setItem(gridSortKey(datasetId), JSON.stringify({ by, order })); } catch { /* ignore */ }
    gridSortListeners.forEach(listener => listener(datasetId, by, order));
}
/**
 * Calls `listener` with each sort a grid in this tab is set to (the page's address names it): the remembered one is the
 * last chosen in any tab. Returns the unsubscribe.
 */
export function subscribeGridSort(listener: GridSortListener): () => void {
    gridSortListeners.add(listener);
    return () => { gridSortListeners.delete(listener); };
}
/** The sort the dataset's grids are on (the last one changed); `by` unset is the default order. */
export function readGridSort(datasetId: number): { by?: string; order?: 'asc' | 'desc' } {
    try { return JSON.parse(localStorage.getItem(gridSortKey(datasetId)) || '{}'); } catch { return {}; }
}

interface GridPanelProps {
    datasetId: number;
    dataset: Dataset | undefined;
    filters: ComplexFilter;
    filtersReady?: boolean;  // false: the filters' final shape is not known yet; hold the grid's requests
    grainMode?: 'image' | 'object';  // 'object' shows per-detection crop rows (the patch view)
    onGrainModeChange?: (mode: 'image' | 'object') => void;
    // The View picker's choice is saved as the dataset's grid media field (the field every grid opens on).
    onMediaFieldChange?: (field: string) => void;
    onShowObjects?: () => void;  // image grain: pivot to the objects of the selected images
    onShowImages?: () => void;  // object grain: pivot to the parent images of the selected objects
    onFindUnlabeledClick?: () => void;  // object grain: find unlabeled patch look-alikes of the selected object
    pivotImageCount?: number;  // object grain: # images the parent_id pivot is scoping to
    onClearPivot?: () => void;  // clear the image->objects pivot (show all objects)
    rankOrderIds?: number[];  // similarity/patch search rank order, applied to the grid
    rankByParent?: boolean;  // rankOrderIds holds IMAGE ids; rank these OBJECT rows by their parent
    highlightBoxes?: Map<number, [number, number, number, number]>;  // per-image crop_bbox to outline
    schema: SchemaField[];
    selectedIds: number[];
    selectAllActive?: boolean;   // Select-All (token) mode — all rows selected minus deselectedIds
    deselectedIds?: number[];
    selectionCount?: number;     // effective count spanning both modes
    // A selection made in a grid of the other grain: not shown, tagged or exported here.
    otherSelection?: { count: number; grain: 'image' | 'object' };
    activeOverlayFields: string[];
    overlayConfigs?: Record<string, { colormap?: string; range?: [number, number]; opacity?: number } | null>;
    onSampleClick: (sample: Sample) => void;
    onToggleSelection: (id: number) => void;
    onRangeSelection: (ids: number[], select?: boolean) => void;
    onClearSelection: () => void;
    onSelectAll: () => void;
    guest?: boolean;
    onTagClick?: () => void;
    onExportClick?: () => void;
    canExport?: boolean;
    onFindSimilarClick?: () => void;
    hashFields?: string[];
    onShowDuplicates?: (mode: string) => void;
    canTag?: boolean;
    gridSamplesRef?: React.MutableRefObject<Sample[]>;
    gridMoreRef?: React.MutableRefObject<GridMore | null>;
    selectedPatches?: Map<number, Array<{ row: number; col: number; scale: string }>>;
    onPatchSelect?: (sampleId: number, row: number, col: number, scale: string) => void;
    availablePatchScales?: string[];
    patchScaleLabels?: Record<string, string>;  // how each scale reads, e.g. "8x8 · on desktop" for a follower's
    gridPatchMode?: string | null;
    onGridPatchModeChange?: (mode: string | null) => void;
    onClearPatches?: () => void;
    canDeleteObjects?: boolean;  // an admin, not on a mirror: offers deleting the objects whose image is gone
}

export function GridPanel({
    datasetId,
    dataset,
    filters,
    onMediaFieldChange,
    filtersReady = true,
    grainMode = 'image',
    onGrainModeChange,
    onShowObjects,
    onShowImages,
    onFindUnlabeledClick,
    pivotImageCount = 0,
    onClearPivot,
    rankOrderIds,
    rankByParent,
    highlightBoxes,
    schema,
    selectedIds,
    selectAllActive = false,
    deselectedIds = [],
    selectionCount,
    otherSelection,
    activeOverlayFields,
    overlayConfigs,
    onSampleClick,
    onToggleSelection,
    onRangeSelection,
    onClearSelection,
    onSelectAll,
    guest = false,
    onTagClick,
    onExportClick,
    canExport = false,
    onFindSimilarClick,
    hashFields = [],
    onShowDuplicates,
    canTag = false,
    gridSamplesRef,
    gridMoreRef,
    selectedPatches,
    onPatchSelect,
    availablePatchScales = [],
    patchScaleLabels,
    gridPatchMode,
    onGridPatchModeChange,
    onClearPatches,
    canDeleteObjects = false,
}: GridPanelProps) {
    // Effective selection spanning explicit-id and Select-All (token) modes.
    const effectiveSelectionCount = selectionCount ?? selectedIds.length;
    const hasSelection = effectiveSelectionCount > 0;

    // Partial derived-field coverage is otherwise invisible: the scatter/duplicates silently
    // show only the covered subset, which reads as "that's the whole dataset". Surface a
    // compact warning when any embedding/hash field or the location is partially covered.
    // Clicking it makes the view the samples behind it: one filter with a `<field>: no value` rule
    // per gap, OR'ed; ✕ on the warning hides it until the page reloads (no GPS can be permanent
    // for some images). Fully-missing artifacts get per-panel empty states.
    // Mixed-grain datasets are excluded: object rows never carry image-grain artifacts,
    // so per-field coverage vs total_samples reads as "partial" forever (false positive).
    const isMixedGrain = !!onGrainModeChange;
    const { showResultsAsView } = useFilters();
    const coverageDismissed = useSyncExternalStore(subscribeDismissed, () => dismissedCoverage.has(datasetId), () => false);
    const { data: coverage } = useQuery({
        queryKey: ['coverage', datasetId],
        queryFn: () => getDatasetCoverage(datasetId),
        enabled: !!datasetId && !isMixedGrain,
        // Coverage counts scan embeddings/hashes server-side — not for hot polling.
        staleTime: 30 * 60_000,
        refetchOnWindowFocus: false,
    });
    const coverageGaps = (() => {
        if (!coverage || isMixedGrain) return [];
        // `field` is the filter name (see derivedFieldNames); `label` what the warning says.
        const gaps: Array<{ kind: string; field: string; label: string; missing: number }> = [];
        for (const e of coverage.embeddings) {
            if (e.present > 0 && e.missing > 0) gaps.push({ kind: 'embedding', field: `embeddings.${e.field}`, label: e.field, missing: e.missing });
        }
        for (const h of coverage.hashes) {
            if (h.present > 0 && h.missing > 0) gaps.push({ kind: 'hash', field: `hashes.${h.field}`, label: h.field, missing: h.missing });
        }
        const loc = coverage.location;
        if (loc.present > 0 && loc.missing > 0) gaps.push({ kind: 'location', field: 'location', label: 'location', missing: loc.missing });
        return gaps;
    })();

    // Objects with no image (object grain): "N objects have no image", which opens them as a view; an admin can
    // delete those whose image is gone. Nothing is shown when there are none. idx_samples_unlinked_objects makes
    // the count cheap.
    const { data: unlinked } = useQuery({
        queryKey: ['unlinked-objects', datasetId],
        queryFn: () => findUnlinkedObjects(datasetId),
        enabled: !!datasetId && isMixedGrain && grainMode === 'object',
        staleTime: 60_000,
        refetchOnWindowFocus: false,
    });
    const [deletingUnlinked, setDeletingUnlinked] = useState(false);
    const deleteGoneObjects = async () => {
        if (!unlinked) return;
        const gone = unlinked.kinds.image_gone;
        const others = unlinked.total - gone;
        const kept = others > 0
            ? `\n\n${others.toLocaleString()} other object${others === 1 ? ' with no image stays' : 's with no image stay'} (${UNLINKED_KINDS
                .filter(kind => kind !== 'image_gone' && unlinked.kinds[kind] > 0)
                .map(kind => `${unlinked.kinds[kind].toLocaleString()} ${UNLINKED_KIND_LABELS[kind]}`).join(', ')}).`
            : '';
        if (!confirm(`Delete the ${gone.toLocaleString()} object${gone === 1 ? '' : 's'} whose image is gone?${kept}`)) return;
        setDeletingUnlinked(true);
        try {
            const result = await deleteUnlinkedObjects(datasetId, ['image_gone']);
            if (result.job_id) notifyJobSubmitted(datasetId, result.job_id);
        } catch (error) {
            alert(error instanceof Error ? error.message : 'Could not delete the objects with no image');
        } finally {
            setDeletingUnlinked(false);
        }
    };

    // Per-instance view settings. The sort is remembered per dataset, like the filters and the
    // selection (a reload went back to "Default (ID)").
    const [savedSort] = useState<{ by?: string; order?: 'asc' | 'desc' }>(() => {
        if (typeof window === 'undefined') return {};
        return readGridSort(datasetId);
    });
    const [sortBy, setSortBy] = useState<string | undefined>(savedSort.by || undefined);
    const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>(savedSort.order === 'desc' ? 'desc' : 'asc');
    useEffect(() => rememberGridSort(datasetId, sortBy, sortOrder), [datasetId, sortBy, sortOrder]);
    // Only what the server can sort: an array field (tags) or a boolean fell back to the default
    // order with the dropdown still claiming the sort.
    const sortableFields = (schema ?? []).filter(f => f.is_promoted && f.sortable !== false && SORTABLE_TYPES.has(f.type));
    // A remembered sort on a field that is no longer sortable here (demoted, removed) is dropped.
    const effectiveSortBy = sortBy && (!schema?.length || sortableFields.some(f => f.name === sortBy)) ? sortBy : undefined;
    const [imageScale, setImageScale] = useState(25);
    const [mediaField, setMediaField] = useState<string>(dataset?.grid_media_field || 'filepath');
    // null while the grid's COUNT is in flight: show that, not the previous view's number.
    const [totalCount, setTotalCount] = useState<number | null>(null);
    // The COUNT failed: no total is coming (the grid offers Retry), so it no longer says "Counting…".
    const [countFailed, setCountFailed] = useState(false);

    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- deliberate prop-sync: adopt the dataset's configured grid media field when it loads/changes
        if (dataset?.grid_media_field) setMediaField(dataset.grid_media_field);
    }, [dataset?.grid_media_field]);

    const localRef = useRef<Sample[]>([]);
    const ref = gridSamplesRef || localRef;

    return (
        <div className="h-full flex flex-col bg-zinc-900">
            {/* Grid Header */}
            <div className="px-4 py-2 border-b border-zinc-800 flex items-center justify-between shrink-0 gap-2">
                <div className="flex items-center gap-3 flex-wrap">
                    {/* Grain toggle: image view vs object/patch view (per-detection crops).
                        Objects are a lens on THIS dataset, not a separate dataset. */}
                    {onGrainModeChange && (
                        <div className="flex items-center rounded border border-zinc-700 overflow-hidden text-xs" data-testid="grain-toggle">
                            {(['image', 'object'] as const).map(mode => (
                                <button
                                    key={mode}
                                    onClick={() => onGrainModeChange(mode)}
                                    className={`px-2 py-1 transition-colors ${grainMode === mode
                                        ? 'bg-cyan-500/20 text-cyan-300'
                                        : 'text-zinc-400 hover:text-white hover:bg-zinc-800'}`}
                                >
                                    {mode === 'image' ? 'Images' : 'Objects'}
                                </button>
                            ))}
                        </div>
                    )}

                    {/* Count in the current view — always shown (persists through selection,
                        unlike the Select All button below). */}
                    <span data-testid="grid-view-count" className="text-xs text-zinc-400 whitespace-nowrap">
                        {totalCount === null
                            ? (countFailed ? 'Count unavailable' : 'Counting…')
                            :`${totalCount.toLocaleString()} ${grainMode === 'object' ? 'object' : 'image'}${totalCount === 1 ? '' : 's'}`}
                    </span>

                    {/* Partial-coverage warning (see coverageGaps above) */}
                    {coverageGaps.length > 0 && !coverageDismissed && (
                        <span className="flex items-center text-[11px] text-amber-300/90 bg-amber-500/10 border border-amber-500/30 rounded whitespace-nowrap">
                            <button
                                data-testid="grid-coverage-warning"
                                onClick={() => showResultsAsView(missingFieldsStage(coverageGaps.map(g => g.field)))}
                                className="pl-2 pr-1 py-1 hover:bg-amber-500/20 rounded-l transition-colors"
                                title={`Samples missing a derived field (panels using it show just the covered subset):\n\n${coverageGaps.map(g => `${g.label}${g.kind === 'location' ? '' : ` (${g.kind})`}: ${g.missing.toLocaleString()}`).join('\n')}\n\nClick to view them.`}
                            >
                                ⚠ {coverageGaps[0].missing.toLocaleString()} sample{coverageGaps[0].missing === 1 ? '' : 's'} missing {coverageGaps[0].label}
                                {coverageGaps.length > 1 ? ` +${coverageGaps.length - 1} more` : ''}
                            </button>
                            <button
                                data-testid="grid-coverage-dismiss"
                                aria-label="Hide the coverage warning"
                                title="Hide until the page reloads"
                                onClick={() => dismissCoverage(datasetId)}
                                className="px-1.5 py-1 hover:bg-amber-500/20 hover:text-amber-100 rounded-r transition-colors"
                            >
                                ✕
                            </button>
                        </span>
                    )}

                    {/* Objects with no image (object grain only; see `unlinked` above) */}
                    {grainMode === 'object' && unlinked && unlinked.total > 0 && (
                        <span className="flex items-center text-[11px] text-amber-300/90 bg-amber-500/10 border border-amber-500/30 rounded whitespace-nowrap">
                            <button
                                data-testid="grid-unlinked-objects"
                                onClick={() => showResultsAsView(unlinkedObjectsStage(unlinked.filter))}
                                className="px-2 py-1 hover:bg-amber-500/20 rounded transition-colors"
                                title={`Objects whose parent_id names no image of this dataset:\n\n${UNLINKED_KINDS
                                    .filter(kind => unlinked.kinds[kind] > 0)
                                    .map(kind => `${unlinked.kinds[kind].toLocaleString()} ${UNLINKED_KIND_LABELS[kind]}`).join('\n')}\n\nClick to view them.`}
                            >
                                ⚠ {unlinked.total.toLocaleString()} object{unlinked.total === 1 ? '' : 's'} {unlinked.total === 1 ? 'has' : 'have'} no image
                            </button>
                            {canDeleteObjects && !unlinked.mirror && unlinked.kinds.image_gone > 0 && (
                                <button
                                    data-testid="grid-unlinked-objects-delete"
                                    onClick={deleteGoneObjects}
                                    disabled={deletingUnlinked}
                                    className="px-1.5 py-1 border-l border-amber-500/30 hover:bg-amber-500/20 hover:text-amber-100 rounded-r transition-colors disabled:opacity-50"
                                    title="Delete the objects whose image is gone (a background job; Job history shows the result)"
                                >
                                    Delete…
                                </button>
                            )}
                        </span>
                    )}

                    {/* Image->objects pivot breadcrumb (object grain only) */}
                    {grainMode === 'object' && pivotImageCount > 0 && onClearPivot && (
                        <button
                            onClick={onClearPivot}
                            className="bg-purple-500/20 hover:bg-purple-500/30 text-purple-300 border border-purple-500/50 text-xs px-2 py-1 rounded flex items-center gap-2 transition-colors"
                            title="Show objects from all images"
                        >
                            <span>Objects of {pivotImageCount.toLocaleString()} image{pivotImageCount === 1 ? '' : 's'}</span>
                            <span className="text-xs">&times;</span>
                        </button>
                    )}

                    {/* Selection indicator */}
                    {hasSelection && (
                        <button
                            onClick={onClearSelection}
                            data-testid="grid-selection-count"
                            className="bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-400 hover:text-cyan-300 border border-cyan-500/50 text-xs px-2 py-1 rounded flex items-center gap-2 transition-colors"
                        >
                            <span>{effectiveSelectionCount.toLocaleString()} selected{selectAllActive ? ' (all)' : ''}</span>
                            <span className="text-xs">&times;</span>
                        </button>
                    )}
                    {!hasSelection && otherSelection && otherSelection.count > 0 && (
                        <span
                            data-testid="grid-other-selection"
                            className="text-xs text-zinc-500 whitespace-nowrap"
                            title="Selected in a panel of the other grain, so not shown, tagged or exported here. Selecting here starts a new selection."
                        >
                            {otherSelection.count.toLocaleString()} {otherSelection.grain === 'object' ? 'object' : 'image'}{otherSelection.count === 1 ? '' : 's'} selected in another panel
                        </span>
                    )}

                    {/* Patch selection indicator */}
                    {selectedPatches && selectedPatches.size > 0 && (() => {
                        const totalPatches = Array.from(selectedPatches.values()).reduce((sum, arr) => sum + arr.length, 0);
                        return totalPatches > 0 ? (
                        <button
                            onClick={onClearPatches}
                            className="bg-amber-500/20 hover:bg-amber-500/30 text-amber-400 hover:text-amber-300 border border-amber-500/50 text-xs px-2 py-1 rounded flex items-center gap-2 transition-colors"
                        >
                            <span>{totalPatches} patch{totalPatches !== 1 ? 'es' : ''}</span>
                            <span className="text-xs">&times;</span>
                        </button>
                    ) : null;
                    })()}

                    {/* Tag Button */}
                    {hasSelection && canTag && onTagClick && (
                        <button
                            onClick={onTagClick}
                            data-testid="grid-tag-button"
                            className="text-xs bg-green-500/20 hover:bg-green-500/30 text-green-400 hover:text-green-300 border border-green-500/50 px-2 py-1 rounded flex items-center gap-1 transition-colors"
                            title="Tag selected samples"
                        >
                            <Tag className="w-3 h-3" />
                            Tag
                        </button>
                    )}

                    {/* Export Button */}
                    {canExport && onExportClick && (
                        <button
                            onClick={onExportClick}
                            className="text-xs bg-blue-500/20 hover:bg-blue-500/30 text-blue-400 hover:text-blue-300 border border-blue-500/50 px-2 py-1 rounded flex items-center gap-1 transition-colors"
                            title="Export samples as CSV"
                        >
                            <Download className="w-3 h-3" />
                            Export
                        </button>
                    )}

                    {/* Find Similar Button */}
                    {selectedIds.length >= 1 && onFindSimilarClick && (
                        <button
                            onClick={onFindSimilarClick}
                            className="text-xs bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-400 hover:text-cyan-300 border border-cyan-500/50 px-2 py-1 rounded transition-colors"
                            title={selectedIds.length > 1 ? `Find samples similar to ${selectedIds.length} selected` : "Find visually similar samples"}
                        >
                            Find Similar{selectedIds.length > 1 ? ` (${selectedIds.length})` : ''}
                        </button>
                    )}

                    {/* Show Objects — pivot to the objects of the selected images */}
                    {grainMode === 'image' && selectedIds.length >= 1 && onShowObjects && (
                        <button
                            onClick={onShowObjects}
                            className="text-xs bg-purple-500/20 hover:bg-purple-500/30 text-purple-300 hover:text-purple-200 border border-purple-500/50 px-2 py-1 rounded transition-colors"
                            title={`Show the objects in ${selectedIds.length} selected image${selectedIds.length === 1 ? '' : 's'}`}
                        >
                            Show Objects{selectedIds.length > 1 ? ` (${selectedIds.length})` : ''}
                        </button>
                    )}

                    {/* Show Images — pivot to the parent images of the selected objects */}
                    {grainMode === 'object' && selectedIds.length >= 1 && onShowImages && (
                        <button
                            onClick={onShowImages}
                            className="text-xs bg-purple-500/20 hover:bg-purple-500/30 text-purple-300 hover:text-purple-200 border border-purple-500/50 px-2 py-1 rounded transition-colors"
                            title={`Show the parent images of ${selectedIds.length} selected object${selectedIds.length === 1 ? '' : 's'}`}
                        >
                            Show Images{selectedIds.length > 1 ? ` (${selectedIds.length})` : ''}
                        </button>
                    )}

                    {/* Find Unlabeled — from one selected object, surface similar UNlabeled regions */}
                    {grainMode === 'object' && selectedIds.length === 1 && onFindUnlabeledClick && (
                        <button
                            onClick={onFindUnlabeledClick}
                            className="text-xs bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 hover:text-amber-200 border border-amber-500/50 px-2 py-1 rounded transition-colors"
                            title="Find unlabeled regions that look like this object"
                        >
                            Find Unlabeled
                        </button>
                    )}

                    {/* Show Duplicates — exact, by hash (of the selected sample) */}
                    {selectedIds.length >= 1 && hashFields.length > 0 && onShowDuplicates && (
                        hashFields.length === 1 ? (
                            <button
                                onClick={() => onShowDuplicates('_any')}
                                className="text-xs bg-yellow-500/20 hover:bg-yellow-500/30 text-yellow-400 hover:text-yellow-300 border border-yellow-500/50 px-2 py-1 rounded transition-colors"
                                title="Select exact duplicates of the selected sample"
                            >
                                Show Duplicates
                            </button>
                        ) : (
                            <DropdownMenu.Root>
                                <DropdownMenu.Trigger asChild>
                                    <button
                                        className="text-xs bg-yellow-500/20 hover:bg-yellow-500/30 text-yellow-400 hover:text-yellow-300 border border-yellow-500/50 px-2 py-1 rounded transition-colors outline-none"
                                        title="Select exact duplicates of the selected sample"
                                    >
                                        Show Duplicates ▾
                                    </button>
                                </DropdownMenu.Trigger>
                                <DropdownMenu.Portal>
                                    <DropdownMenu.Content
                                        className="min-w-[200px] bg-zinc-900 border border-zinc-800 rounded-md shadow-xl py-1 z-[100] animate-in fade-in zoom-in-95 duration-100"
                                        sideOffset={5}
                                        align="start"
                                    >
                                        <div className="px-3 py-1.5 text-[11px] uppercase tracking-wide text-zinc-500">Match on</div>
                                        <DropdownMenu.Item className="outline-none cursor-pointer" onSelect={() => onShowDuplicates('_any')}>
                                            <div className="w-full px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 hover:text-white transition-colors">Any field</div>
                                        </DropdownMenu.Item>
                                        <DropdownMenu.Item className="outline-none cursor-pointer" onSelect={() => onShowDuplicates('_all')}>
                                            <div className="w-full px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 hover:text-white transition-colors">All fields</div>
                                        </DropdownMenu.Item>
                                        <DropdownMenu.Separator className="h-px bg-zinc-800 my-1" />
                                        {hashFields.map(f => (
                                            <DropdownMenu.Item key={f} className="outline-none cursor-pointer" onSelect={() => onShowDuplicates(f)}>
                                                <div className="w-full px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 hover:text-white transition-colors truncate">{f}</div>
                                            </DropdownMenu.Item>
                                        ))}
                                    </DropdownMenu.Content>
                                </DropdownMenu.Portal>
                            </DropdownMenu.Root>
                        )
                    )}

                    {/* Sort */}
                    <div className="flex items-center gap-1.5">
                        <label className="text-[11px] text-zinc-500">Sort:</label>
                        <select
                            value={effectiveSortBy || ''}
                            onChange={(e) => setSortBy(e.target.value || undefined)}
                            data-testid="grid-sort"
                            className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:ring-1 focus:ring-cyan-500"
                        >
                            <option value="">Default (ID)</option>
                            {sortableFields.map(field => (
                                <option key={field.name} value={field.name}>{field.name}</option>
                            ))}
                        </select>
                        {effectiveSortBy && (
                            <button
                                onClick={() => setSortOrder(prev => prev === 'asc' ? 'desc' : 'asc')}
                                className="px-1.5 py-1 bg-zinc-800 border border-zinc-700 rounded text-xs text-zinc-300 hover:text-white hover:bg-zinc-700"
                            >
                                {sortOrder === 'asc' ? '↑' : '↓'}
                            </button>
                        )}
                    </div>

                    {/* Media field */}
                    {dataset?.media_fields && dataset.media_fields.length > 1 && (
                        <div className="flex items-center gap-1.5">
                            <label className="text-[11px] text-zinc-500">View:</label>
                            <select
                                value={mediaField}
                                onChange={(e) => { setMediaField(e.target.value); onMediaFieldChange?.(e.target.value); }}
                                className="text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-white focus:outline-none focus:border-zinc-500"
                            >
                                {dataset.media_fields.map(field => (
                                    <option key={field} value={field}>{field}</option>
                                ))}
                            </select>
                        </div>
                    )}

                    {/* Patch mode — patches are a sub-grid of IMAGES, not of object crops. */}
                    {grainMode !== 'object' && availablePatchScales.length > 0 && (
                        <div className="flex items-center gap-1.5">
                            <label className="text-[11px] text-zinc-500">Patches:</label>
                            <select
                                value={gridPatchMode || ''}
                                onChange={(e) => onGridPatchModeChange?.(e.target.value || null)}
                                className="text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-white focus:outline-none focus:border-zinc-500"
                            >
                                <option value="">Off</option>
                                {availablePatchScales.map(s => (
                                    <option key={s} value={s}>{patchScaleLabels?.[s] ?? s}</option>
                                ))}
                            </select>
                        </div>
                    )}

                    {/* Image size */}
                    <div className="flex items-center gap-2 w-28">
                        <span className="text-[11px] text-zinc-500 whitespace-nowrap">Size:</span>
                        <Slider
                            defaultValue={[imageScale]}
                            max={100}
                            step={1}
                            value={[imageScale]}
                            onValueChange={(value) => setImageScale(value[0])}
                        />
                    </div>
                </div>

                {/* Select All */}
                {!hasSelection && (
                    <SelectAllButton total={totalCount} guest={guest} onSelectAll={onSelectAll} />
                )}
            </div>

            {/* Grid — DatasetGrid owns its own data fetching via useInfiniteQuery */}
            <div className="flex-1 overflow-hidden">
                <DatasetGrid
                    datasetId={datasetId}
                    filters={filters}
                    filtersReady={filtersReady}
                    total={0}
                    initialSamples={[]}
                    activeMediaField={mediaField}
                    activeOverlayFields={activeOverlayFields}
                    overlayConfigs={overlayConfigs}
                    segmentationConfig={dataset?.segmentation_config}
                    onSampleClick={onSampleClick}
                    selectedIds={selectedIds}
                    selectAllActive={selectAllActive}
                    deselectedIds={deselectedIds}
                    onToggleSelection={onToggleSelection}
                    onRangeSelection={onRangeSelection}
                    sortBy={effectiveSortBy}
                    sortOrder={sortOrder}
                    rankOrderIds={rankOrderIds}
                    rankByParent={rankByParent}
                    highlightBoxes={highlightBoxes}
                    imageScale={imageScale}
                    gridSamplesRef={ref}
                    gridMoreRef={gridMoreRef}
                    onTotalChange={setTotalCount}
                    onCountFailed={setCountFailed}
                    selectedPatches={selectedPatches}
                    onPatchSelect={onPatchSelect}
                    gridPatchMode={gridPatchMode}
                />
            </div>
        </div>
    );
}
