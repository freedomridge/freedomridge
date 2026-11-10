'use client';

import React, { createContext, useContext, useState, useCallback, useMemo, useEffect, useRef } from 'react';
import type { Feature, Polygon } from 'geojson';
import { ComplexFilter, MAP_REFITTED_EVENT, type MapRefitted, ViewStage } from '@/lib/api';
import { useToast } from '@/contexts/ToastContext';
import { convertSimpleFiltersToComplex, describeFilter } from '@/lib/filter-utils';
import { type DefaultView, defaultViewStages, isDefaultViewStage, opensOnDefaultView } from '@/lib/default-view';

// The map lasso selection — a GeoJSON polygon feature (or none).
type MapPolygon = Feature<Polygon> | null;

// Draft per-field filter map. Values are heterogeneous (scalars, arrays, {min,max}
// ranges, {_op} markers) and are read via untyped index access by sidebar/histogram
// panels we don't own, so the value side stays `any`.
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- dynamic draft-filter map consumed by untyped panels
type SimpleFilters = Record<string, any>;

// --- Persistence helpers ---
const STORAGE_PREFIX = 'fridge-filters-';

// A lasso on a sampled scatter: the polygon is the selection, and brushedIds holds only its visible points.
type EmbeddingRegion = { field: string; polygon: number[][]; token: string; total: number; modelId?: string | null };

interface PersistedFilterState {
    viewStages: ViewStage[];
    filterMode: 'simple' | 'advanced';
    simpleFilters: SimpleFilters;
    advancedFilters: ComplexFilter;
    brushedIds: number[];
    // Kept with brushedIds or not at all: the ids alone would come back as a selection of the visible points only.
    currentEmbeddingRegion?: EmbeddingRegion | null;
    selectedIds: number[];
    currentPolygon: MapPolygon;
    selectionSource: string;
    // The default view this browser's viewer chose to see past (Show all images, or removing its filters):
    // the dataset doesn't open on it again until they clear the filters.
    dismissedDefaultViewId?: number | null;
}

function loadFilterState(datasetId: number | undefined): PersistedFilterState | null {
    if (!datasetId || typeof window === 'undefined') return null;
    try {
        const raw = localStorage.getItem(`${STORAGE_PREFIX}${datasetId}`);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        // Basic validation
        if (!Array.isArray(parsed.viewStages)) return null;
        return parsed as PersistedFilterState;
    } catch {
        return null;
    }
}

const MAX_PERSISTED_IDS = 50_000;  // Skip persisting transient selections larger than this
const MAX_STORAGE_BYTES = 2 * 1024 * 1024;  // 2MB hard cap per dataset

export function filterHasInSelection(f: ComplexFilter | undefined): boolean {
    if (!f) return false;
    if (f.operator === 'in_selection') return true;
    return (f.filters || []).some(filterHasInSelection);
}

// A selection stage that must NOT be written to history state or localStorage, because it
// wouldn't survive being restored:
//   - an inline id list too large to carry (browsers cap history.state near ~16MB, so a raw
//     2M-id stage overflows it and pushState throws); or
//   - a server-side in_selection token — region_selections tokens are TTL-pruned after 2h
//     (create_selection_token), so a restored stage referencing an expired token silently
//     returns 0 rows. Better to keep these session-only than to persist a stub / a stage that
//     later matches nothing with no signal.
function isEphemeralSelectionStage(s: ViewStage): boolean {
    if (s.type === 'selection' && Array.isArray(s.filter?.value) && s.filter.value.length > MAX_PERSISTED_IDS) return true;
    return filterHasInSelection(s.filter);
}

function saveFilterState(datasetId: number | undefined, state: PersistedFilterState) {
    if (!datasetId || typeof window === 'undefined') return;
    try {
        const toSave: PersistedFilterState = {
            ...state,
            // Cap transient selections — large lasso/brush selections don't persist
            brushedIds: state.brushedIds.length <= MAX_PERSISTED_IDS ? state.brushedIds : [],
            currentEmbeddingRegion: state.brushedIds.length <= MAX_PERSISTED_IDS ? state.currentEmbeddingRegion ?? null : null,
            selectedIds: state.selectedIds.length <= MAX_PERSISTED_IDS ? state.selectedIds : [],
            // Drop applied selection stages with too many IDs (rather than save a broken stub)
            viewStages: state.viewStages.filter(s => !isEphemeralSelectionStage(s)),
        };

        const json = JSON.stringify(toSave);
        if (json.length > MAX_STORAGE_BYTES) return; // Safety valve — don't save if still too large

        localStorage.setItem(`${STORAGE_PREFIX}${datasetId}`, json);
    } catch {
        // localStorage full or unavailable — silently skip
    }
}

// Signature used to de-dupe browser-history entries: never push an entry identical to
// the current one (the mount echo, or the cascade of re-renders/refetches a Back/Forward
// restore triggers — which would otherwise pin Back on the latest filter).
function historyStateSig(s: {
    viewStages: ViewStage[]; brushedIds: number[]; selectedIds: number[];
    currentPolygon: MapPolygon; selectionSource: string; currentEmbeddingRegion?: EmbeddingRegion | null;
}): string {
    return JSON.stringify([s.viewStages, s.brushedIds, s.selectedIds, s.currentPolygon, s.selectionSource, s.currentEmbeddingRegion ?? null]);
}

interface FilterContextType {
    // Global filters - derived from view stages (+ the grain predicate)
    globalFilters: ComplexFilter;

    // View-stage filters WITHOUT any grain predicate. Grain (image vs object) is applied
    // PER-PANEL via grainPredicate()/withGrain() — see those helpers — so panels can show
    // different grains independently. Same object as globalFilters.
    userFilters: ComplexFilter;

    // View Stages
    viewStages: ViewStage[];
    setViewStages: React.Dispatch<React.SetStateAction<ViewStage[]>>;
    addStage: (stage: ViewStage) => void;
    removeStage: (index: number) => void;
    updateStage: (index: number, stage: ViewStage) => void;

    // Staged filters (affects grid preview only)
    stagedFilters: ComplexFilter;

    // Draft state (what's being edited in filter panels)
    filterMode: 'simple' | 'advanced';
    simpleFilters: SimpleFilters;
    advancedFilters: ComplexFilter;

    // Actions
    setFilterMode: (mode: 'simple' | 'advanced') => void;
    setSimpleFilters: (filters: SimpleFilters) => void;
    setAdvancedFilters: (filters: ComplexFilter) => void;

    // Apply/Reset. resetFilters goes back to the dataset's default view (no filters when it has none).
    applyFilters: () => void;
    resetFilters: () => void;

    // The dataset's default view (undefined until the dataset has loaded; null when it has none). The page
    // passes it in; the dataset opens on it, and showAllImages removes it and every other filter.
    defaultView: DefaultView | null | undefined;
    setDefaultView: (view: DefaultView | null) => void;
    showAllImages: () => void;
    clearSelection: () => void;
    showResultsAsView: (stage: ViewStage) => void;
    hasUnappliedChanges: boolean;

    // Selection state (from embeddings/map - part of staged filters)
    selectedIds: number[];
    setSelectedIds: (ids: number[]) => void;

    // Select-All mode: instead of materializing every matching id, hold a server-side
    // selection token + count and a small set of exceptions ("all except these"). Keeps a
    // million-sample Select-All weightless (no giant array in state / on the wire / in history).
    selectAll: { token: string; count: number } | null;
    setSelectAll: (s: { token: string; count: number } | null) => void;
    deselectedIds: number[];
    setDeselectedIds: (ids: number[]) => void;
    // Unified helpers spanning both modes.
    isSampleSelected: (id: number) => boolean;
    selectionCount: number;
    toggleSampleSelected: (id: number) => void;

    // Embedding/Map Brush Selection
    brushedIds: number[];
    setBrushedIds: (ids: number[]) => void;

    // Map Polygon Selection
    currentPolygon: MapPolygon;
    setCurrentPolygon: (polygon: MapPolygon) => void;

    // Embeddings region (UMAP lasso) — applied as a SERVER-SIDE filter, never an
    // id list, so the full selection scopes the grid without capping/freezing.
    currentEmbeddingRegion: EmbeddingRegion | null;
    setCurrentEmbeddingRegion: (region: EmbeddingRegion | null) => void;

    // Selection Source
    selectionSource: string;
    setSelectionSource: (source: string) => void;

    // DEPRECATED - use stagedFilters for grid, globalFilters for other panels
    activeFilters: ComplexFilter;
}

const FilterContext = createContext<FilterContextType | undefined>(undefined);

export type Grain = 'image' | 'object';

// Objects are an opt-in grain WITHIN a dataset (media_type='object' rows). 'image' excludes
// them; 'object' includes only them, plus an optional image->objects pivot filter (e.g.
// parent_id IN [...selected image ids]). Applied per-panel so grids can differ.
export function grainPredicate(grain: Grain, pivot?: ComplexFilter | null): ComplexFilter[] {
    if (grain === 'object') {
        const fs: ComplexFilter[] = [{ field: 'media_type', operator: '=', value: 'object' }];
        if (pivot) fs.push(pivot);
        return fs;
    }
    return [{ field: 'media_type', operator: '!=', value: 'object' }];
}

// Apply a panel's grain to a base (grain-less) filter, COUPLING the two grains by parent_id.
//
// A panel shows rows of its grain that EITHER match the base filter directly OR are the
// cross-grain partner of a matching row — so an OBJECT grid shows the objects of whatever
// IMAGES match, and an IMAGE grid shows the parent images of whatever OBJECTS match. This
// holds for ANY filter or selection (simple filters, query builder, embeddings lasso, grid
// checkboxes), because they all flow through here as the base filter.
//
// With no active filter the base is empty -> just the grain predicate (show all of this
// grain; there's nothing to cross-link and we skip the cross-grain subquery entirely).
export function withGrain(
    base: ComplexFilter,
    grain: Grain,
    pivot?: ComplexFilter | null,
    hasObjects: boolean = true,
): ComplexFilter {
    const baseFilters = base.filters || [];
    if (baseFilters.length === 0) {
        return { logic: 'AND', filters: grainPredicate(grain, pivot) };
    }
    // A dataset with no object rows can't cross-link, so emit the plain grain-filtered query —
    // this keeps NULL-tolerant negation intact for ordinary image datasets and only
    // applies the cross-grain link (with its presence guard) where objects actually exist.
    if (!hasObjects) {
        return { logic: 'AND', filters: [...grainPredicate(grain, pivot), ...baseFilters] };
    }
    const crossOp = grain === 'object' ? 'parent_matches' : 'has_matching_object';
    // Embedding regions carry a KNOWN grain (clip_obj is per-object; clip_img / clip_patch_* are
    // per-image). When a region's grain differs from the panel's — e.g. a clip_img lasso while an
    // OBJECT grid shows an object-similarity result — it must couple to the PARTNER row (objects
    // whose parent image lies in the region), not be AND'd as if it were this grain (which matches
    // nothing, since objects have no clip_img). The whole-filter OR below can't reconcile that: it
    // would require ONE row to satisfy both the same-grain similarity AND an off-grain region, so
    // it returns empty. Pull each off-grain region out and couple it on its own; the remaining
    // same-grain / ambiguous leaves flow through the OR.
    const fieldGrain = (f: string | undefined): Grain => (f === 'clip_obj' ? 'object' : 'image');
    const isOffGrainEmb = (f: ComplexFilter): boolean =>
        f.operator === 'embedding_polygon' && fieldGrain(f.field) !== grain;
    // An excluded region (a map region or a lasso, excluded) keeps the rows with no location or no point on its map.
    // Coupled like the other leaves, such a row on the partner grain (an object with no point on an image map) would
    // vacuously let its partner through. So it is coupled as NOT (the row or its partner is inside the region).
    const isRegionExclude = (f: ComplexFilter): boolean =>
        f.operator === 'geo_polygon_exclude' || f.operator === 'embedding_polygon_exclude';
    // One with fewer than 3 points is no polygon and excludes nothing, as the server's own exclude does.
    const excluded = baseFilters.filter((f) => isRegionExclude(f) && Array.isArray(f.value) && f.value.length >= 3);
    const offGrainEmb = baseFilters.filter((f) => !isRegionExclude(f) && isOffGrainEmb(f));
    const rest = baseFilters.filter((f) => !isRegionExclude(f) && !isOffGrainEmb(f));
    const parts: ComplexFilter[] = [...grainPredicate(grain, pivot)];
    if (rest.length > 0) {
        // A row matches if its OWN grain matches (cg_self — guarded so an exclusion on a field this
        // grain LACKS doesn't vacuously match every row) OR its cross-grain partner does
        // (parent_matches: an object whose parent image matches; has_matching_object: an image with
        // a child object that matches).
        const baseNode: ComplexFilter = { logic: 'AND', filters: rest };
        parts.push({
            logic: 'OR',
            filters: [
                { field: '_cgself', operator: 'cg_self', value: baseNode },
                { field: '_crossgrain', operator: crossOp, value: baseNode },
            ],
        });
    }
    for (const emb of offGrainEmb) {
        parts.push({ field: '_crossemb', operator: crossOp, value: { logic: 'AND', filters: [emb] } });
    }
    for (const region of excluded) {
        const inside: ComplexFilter = {
            logic: 'AND',
            filters: [{ ...region, operator: region.operator === 'geo_polygon_exclude' ? 'geo_polygon' : 'embedding_polygon' }],
        };
        parts.push({
            logic: 'NOT',
            filters: [{
                logic: 'OR',
                filters: [
                    { field: '_cgself', operator: 'cg_self', value: inside },
                    { field: '_crossgrain', operator: crossOp, value: inside },
                ],
            }],
        });
    }
    return { logic: 'AND', filters: parts };
}

// Does this filter tree hold a lasso of `field` drawn on another map than `modelId` (the field's current
// one)? Such a polygon would select other samples now; the server refuses it (409 map_refitted).
function holdsStaleLasso(node: ComplexFilter | undefined, field: string, modelId: string | null): boolean {
    if (!node) return false;
    if ((node.operator === 'embedding_polygon' || node.operator === 'embedding_polygon_exclude') && node.field === field && (node.model_id ?? null) !== modelId) return true;
    if ((node.filters ?? []).some((child) => holdsStaleLasso(child, field, modelId))) return true;
    const value = node.value as unknown;
    return typeof value === 'object' && value !== null && !Array.isArray(value)
        && holdsStaleLasso(value as ComplexFilter, field, modelId);
}

export function FilterProvider({ children, datasetId }: { children: React.ReactNode; datasetId?: number }): React.ReactNode {
    // Load persisted state (runs once per datasetId)
    const savedState = useMemo(() => loadFilterState(datasetId), [datasetId]);

    // View Stages - The source of truth for global filters
    const [viewStages, setViewStages] = useState<ViewStage[]>(savedState?.viewStages ?? []);

    // Grain-less view-stage filters. The grain predicate (image vs object) is applied
    // PER-PANEL by each consumer (see grainPredicate/withGrain), so an image grid and an
    // object grid can show different grains side by side — grain is no longer global state.
    const globalFilters = useMemo<ComplexFilter>(() => ({
        logic: 'AND',
        filters: viewStages.map(s => s.filter),
    }), [viewStages]);
    // Alias kept for the embeddings scatter, which selects grain by its embedding field
    // (clip_img / clip_obj / clip_patch_*) and so wants the grain-less filters.
    const userFilters = globalFilters;

    const addStage = useCallback((stage: ViewStage) => {
        setViewStages(prev => [...prev, stage]);
    }, []);

    // The dataset's default view, from the page's dataset answer. Its id only changes when an administrator
    // picks another view, and a saved view's content never changes, so the id says whether it is new.
    const [defaultView, setDefaultViewState] = useState<DefaultView | null | undefined>(undefined);
    const setDefaultView = useCallback((view: DefaultView | null) => {
        setDefaultViewState(prev => (prev !== undefined && (prev?.id ?? null) === (view?.id ?? null) ? prev : view));
    }, []);
    const [dismissedDefaultViewId, setDismissedDefaultViewId] = useState<number | null>(savedState?.dismissedDefaultViewId ?? null);

    const removeStage = useCallback((index: number) => {
        // Removing a default-view filter is choosing to see past it, also after a reload.
        if (defaultView && isDefaultViewStage(viewStages[index], defaultView)) setDismissedDefaultViewId(defaultView.id);
        setViewStages(prev => prev.filter((_, i) => i !== index));
    }, [viewStages, defaultView]);

    const updateStage = useCallback((index: number, newStage: ViewStage) => {
        setViewStages(prev => prev.map((s, i) => i === index ? newStage : s));
    }, []);

    // Draft state for filter panels
    const [filterMode, setFilterMode] = useState<'simple' | 'advanced'>(savedState?.filterMode ?? 'simple');
    const [simpleFilters, setSimpleFilters] = useState<SimpleFilters>(savedState?.simpleFilters ?? {});
    const [advancedFilters, setAdvancedFilters] = useState<ComplexFilter>(savedState?.advancedFilters ?? { logic: 'AND', filters: [] });

    // Selection from embeddings/map (Brush)
    const [brushedIds, setBrushedIds] = useState<number[]>(savedState?.brushedIds ?? []);

    // Map Polygon Selection
    const [currentPolygon, setCurrentPolygon] = useState<MapPolygon>(savedState?.currentPolygon ?? null);

    // Embeddings region (UMAP lasso). Persisted with its brushedIds, or, like a large brush, not at all.
    const [currentEmbeddingRegion, setCurrentEmbeddingRegion] = useState<EmbeddingRegion | null>(savedState?.currentEmbeddingRegion ?? null);

    // Grid Selection (Checkboxes)
    const [selectedIds, setSelectedIds] = useState<number[]>(savedState?.selectedIds ?? []);

    // Select-All mode (transient — a 2h server token, never persisted, like the embeddings region).
    const [selectAll, setSelectAll] = useState<{ token: string; count: number } | null>(null);
    const [deselectedIds, setDeselectedIds] = useState<number[]>([]);

    const isSampleSelected = useCallback(
        (id: number) => (selectAll ? !deselectedIds.includes(id) : selectedIds.includes(id)),
        [selectAll, deselectedIds, selectedIds],
    );
    const selectionCount = selectAll ? Math.max(0, selectAll.count - deselectedIds.length) : selectedIds.length;
    const toggleSampleSelected = useCallback((id: number) => {
        // In select-all mode a toggle adds/removes an exception; otherwise it toggles the id list.
        if (selectAll) {
            setDeselectedIds(prev => (prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]));
        } else {
            setSelectedIds(prev => (prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]));
        }
    }, [selectAll]);

    // Selection Source (for labeling)
    const [selectionSource, setSelectionSource] = useState<string>(savedState?.selectionSource ?? 'Manual');

    // Persist filter state on changes (debounced)
    const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    useEffect(() => {
        if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
        saveTimerRef.current = setTimeout(() => {
            saveFilterState(datasetId, {
                viewStages, filterMode, simpleFilters, advancedFilters,
                brushedIds, currentEmbeddingRegion, selectedIds, currentPolygon, selectionSource, dismissedDefaultViewId,
            });
        }, 300);
        return () => { if (saveTimerRef.current) clearTimeout(saveTimerRef.current); };
    }, [datasetId, viewStages, filterMode, simpleFilters, advancedFilters, brushedIds, currentEmbeddingRegion, selectedIds, currentPolygon, selectionSource, dismissedDefaultViewId]);

    // --- Browser history (back/forward) for filter state ---
    // Each settled filter change pushes a history entry; Back/Forward restores that
    // snapshot. Two guards keep this correct under the App Router:
    //  1) we never push an entry identical to the current one (kills the on-mount echo); and
    //  2) for a short window AFTER a popstate restore we suppress pushes, so the cascade of
    //     re-renders/refetches the restore triggers can't re-push the just-restored state —
    //     which previously clobbered forward history and pinned Back on the latest filter.
    const historyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const historyInitializedRef = useRef(false);
    const suppressPushUntilRef = useRef(0);
    // The default view applied on opening replaces the opening entry: Back doesn't step to "no filters".
    const replaceHistoryRef = useRef(false);

    const buildHistorySnapshot = useCallback(() => ({
        _fridge: true,  // marker to distinguish our entries from Next.js'
        // Exclude oversized id-list selection stages — a raw 2M-id stage overflows the
        // browser history.state limit and makes pushState throw (mirrors persistence).
        viewStages: viewStages.filter(s => !isEphemeralSelectionStage(s)),
        brushedIds: brushedIds.length <= MAX_PERSISTED_IDS ? brushedIds : [],
        currentEmbeddingRegion: brushedIds.length <= MAX_PERSISTED_IDS ? currentEmbeddingRegion : null,
        selectedIds: selectedIds.length <= MAX_PERSISTED_IDS ? selectedIds : [],
        currentPolygon,
        selectionSource,
    }), [viewStages, brushedIds, currentEmbeddingRegion, selectedIds, currentPolygon, selectionSource]);

    // Every entry we write KEEPS the app router's own keys (__NA + its tree). Next reloads the page
    // on Back/Forward to an entry without them, and ours never had them: the router stamps the entry
    // in an insertion effect, then this mount-time replaceState overwrote it with a bare object
    // (Next's history patch, which would copy the keys, is installed in a later effect), and every
    // push copied from that bare entry. So every Back was a full reload landing on the latest,
    // persisted filters.
    const withRouterState = (snap: object) => ({ ...(window.history.state ?? {}), ...snap });

    // Set initial history entry on mount
    useEffect(() => {
        if (typeof window === 'undefined' || historyInitializedRef.current) return;
        historyInitializedRef.current = true;
        window.history.replaceState(withRouterState(buildHistorySnapshot()), '');
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    // Push to history on meaningful state changes (debounced)
    useEffect(() => {
        if (typeof window === 'undefined' || !historyInitializedRef.current) return;
        if (historyTimerRef.current) clearTimeout(historyTimerRef.current);
        historyTimerRef.current = setTimeout(() => {
            // Don't record the cascade of updates a Back/Forward restore just triggered.
            if (Date.now() < suppressPushUntilRef.current) {
                replaceHistoryRef.current = false;  // or the next real change would replace an entry
                return;
            }
            const snap = buildHistorySnapshot();
            if (replaceHistoryRef.current) {
                replaceHistoryRef.current = false;
                window.history.replaceState(withRouterState(snap), '');
                return;
            }
            const cur = window.history.state;
            // Don't push a duplicate of the current entry (mount echo / no-op churn).
            if (cur && cur._fridge && historyStateSig(cur) === historyStateSig(snap)) return;
            // pushState can still throw if the snapshot is unexpectedly large (browser
            // history.state cap) — never let a history push crash the app.
            try {
                window.history.pushState(withRouterState(snap), '');
            } catch (e) {
                console.warn('Skipped history push (state too large):', e);
            }
        }, 400);
        return () => { if (historyTimerRef.current) clearTimeout(historyTimerRef.current); };
    }, [viewStages, brushedIds, currentEmbeddingRegion, selectedIds, currentPolygon, selectionSource, buildHistorySnapshot]);

    // Listen for popstate (back/forward)
    useEffect(() => {
        if (typeof window === 'undefined') return;
        const handler = (event: PopStateEvent) => {
            const state = event.state;
            if (!state || !state._fridge) return;  // Not our entry
            // Suppress pushes briefly so the re-renders/refetches this restore triggers
            // don't re-push the restored state and clobber the forward history.
            suppressPushUntilRef.current = Date.now() + 600;
            setViewStages(state.viewStages ?? []);
            setBrushedIds(state.brushedIds ?? []);
            setCurrentEmbeddingRegion(state.currentEmbeddingRegion ?? null);
            setSelectedIds(state.selectedIds ?? []);
            setCurrentPolygon(state.currentPolygon ?? null);
            setSelectionSource(state.selectionSource ?? 'Manual');
        };
        window.addEventListener('popstate', handler);
        return () => window.removeEventListener('popstate', handler);
    }, []);  

    // Keep track of the last meaningful source to handle race conditions where
    // source might flip to 'Manual' just before applying (e.g. focus/blur events)
    const lastValidSourceRef = React.useRef<string>('Manual');

    useEffect(() => {
        if (selectionSource && selectionSource !== 'Manual') {
            lastValidSourceRef.current = selectionSource;
        }
    }, [selectionSource]);

    // Staged filters = what will be applied (includes unapplied changes)
    const stagedFilters: ComplexFilter = useMemo(() => {
        // Start with global filters (from stages)
        const base: ComplexFilter = {
            logic: 'AND',
            filters: [...(globalFilters.filters || [])]
        };
        // If globalFilters was a single field filter, wrap it
        if (globalFilters.field) {
            base.filters = [globalFilters];
        }

        // Add query builder filters (Draft)
        // These are transient until applied
        if (filterMode === 'simple') {
            const draft = convertSimpleFiltersToComplex(simpleFilters);
            if (draft.logic === 'AND') base.filters?.push(...(draft.filters ?? []));
            else base.filters?.push(draft);
        } else if (filterMode === 'advanced') {
            if (advancedFilters.filters && advancedFilters.filters.length > 0) {
                base.filters?.push(advancedFilters);
            }
        }

        // Use current brush if present (preview) — but NOT when an embeddings region is
        // active: the embedding_polygon below is the full selection, and an id-list of
        // the visible points would AND-narrow it back down to just what's on screen.
        const effectiveBrushedIds = (brushedIds.length > 0 && !currentEmbeddingRegion) ? brushedIds : [];
        if (effectiveBrushedIds.length > 0) {
            base.filters?.push({
                field: 'id',
                operator: 'in',
                value: effectiveBrushedIds
            });
        }

        // Use current polygon if present (preview — always include)
        if (currentPolygon) {
            base.filters?.push({
                field: 'location',
                operator: 'geo_polygon',
                value: currentPolygon.geometry.coordinates[0]
            });
        }

        // Embeddings lasso region — a LIVE geometric filter: "samples whose embedding
        // point for this field is inside the polygon", re-evaluated each query. So it
        // composes with the other filters (remove one -> more of the area shows) and
        // picks up newly-projected samples — and matches what applyFilters bakes into a
        // stage, so draft and applied behave identically. The (field_name, x, y) btree
        // serves the bbox; a GiST point index measured ~3x SLOWER, so we don't add one.
        if (currentEmbeddingRegion) {
            base.filters?.push({
                field: currentEmbeddingRegion.field,
                operator: 'embedding_polygon',
                value: currentEmbeddingRegion.polygon,
                model_id: currentEmbeddingRegion.modelId ?? null,
            });
        }

        return base;
    }, [globalFilters, filterMode, simpleFilters, advancedFilters, brushedIds, currentPolygon, currentEmbeddingRegion]);

    // Check for unapplied changes
    const hasUnappliedChanges = useMemo(() => {
        const hasSimple = Object.keys(simpleFilters).length > 0;
        const hasAdvanced = advancedFilters.filters && advancedFilters.filters.length > 0;
        const hasBrush = brushedIds.length > 0;
        // selectedIds (id-list selection) OR a token Select-All (selectedIds is empty in token mode).
        const hasSelection = selectedIds.length > 0 || selectAll !== null;
        const hasPolygon = currentPolygon !== null;
        const hasRegion = currentEmbeddingRegion !== null;

        return hasSimple || hasAdvanced || hasBrush || hasSelection || hasPolygon || hasRegion;
    }, [simpleFilters, advancedFilters, brushedIds, selectedIds, selectAll, currentPolygon, currentEmbeddingRegion]);

    const applyFilters = useCallback(() => {
        // Convert current draft state into a new Stage

        // 1. Selection Stage (Brush or Checkbox). Skip the visible-brush ids when an
        // embeddings region is active — the embedding_polygon stage (2b) is the full
        // selection; folding the visible ids into an id-list stage would mislabel it
        // "X Selected" AND AND-narrow the region down to just the on-screen points.
        const effectiveBrushedIds = (brushedIds.length > 0 && !currentEmbeddingRegion) ? brushedIds : [];
        const selectionIds = [...new Set([...effectiveBrushedIds, ...selectedIds])];

        const effectiveSource = (selectionSource && selectionSource !== 'Manual')
            ? selectionSource
            : lastValidSourceRef.current;
        const hasSource = effectiveSource && effectiveSource !== 'Manual';

        if (selectAll) {
            // Select-All: apply the server-side token (weightless) instead of an id list. Any
            // exceptions become a small `not in` clause so "all except these" stays exact.
            const inSel: ComplexFilter = { field: 'id', operator: 'in_selection', value: selectAll.token };
            const filter: ComplexFilter = deselectedIds.length > 0
                ? { logic: 'AND', filters: [inSel, { field: 'id', operator: 'not in', value: deselectedIds }] }
                : { logic: 'AND', filters: [inSel] };
            const label = `${selectionCount} Selected`;
            addStage({
                type: 'selection',
                description: hasSource ? `${effectiveSource} (${label})` : label,
                filter,
                metadata: { count: selectionCount, source: selectionSource, token: selectAll.token },
            });
        } else if (selectionIds.length > 0) {
            console.log('Applying selection filters. Source:', effectiveSource, 'Count:', selectionIds.length);
            const description = hasSource
                ? `${effectiveSource} (${selectionIds.length} Selected)`
                : `${selectionIds.length} Selected`;

            addStage({
                type: 'selection',
                description: description,
                filter: {
                    field: 'id',
                    operator: 'in',
                    value: selectionIds
                },
                metadata: { count: selectionIds.length, source: selectionSource }
            });
        }

        // 2. Polygon Stage
        if (currentPolygon) {
            addStage({
                type: 'map_region',
                description: 'Map Region',
                filter: {
                    field: 'location',
                    operator: 'geo_polygon',
                    value: currentPolygon.geometry.coordinates[0]
                }
            });
        }

        // 2b. Embeddings region stage — the FULL lasso selection as a server-side
        // filter (no id list, so it never caps). Mirrors the map polygon stage.
        if (currentEmbeddingRegion) {
            addStage({
                type: 'filter',
                description: 'Embeddings Region',
                filter: {
                    field: currentEmbeddingRegion.field,
                    operator: 'embedding_polygon',
                    value: currentEmbeddingRegion.polygon,
                    model_id: currentEmbeddingRegion.modelId ?? null,
                }
            });
        }

        // 3. Simple/Advanced Filter Stage
        let filterToAdd: ComplexFilter | null = null;
        let description = 'Filter';

        if (filterMode === 'simple' && Object.keys(simpleFilters).length > 0) {
            // Convert simple filters to complex
            filterToAdd = convertSimpleFiltersToComplex(simpleFilters);
            description = describeFilter(filterToAdd);  // names the values, not just the fields
        } else if (filterMode === 'advanced' && advancedFilters.filters && advancedFilters.filters.length > 0) {
            filterToAdd = advancedFilters;
            description = 'Advanced Query';
        }

        if (filterToAdd) {
            addStage({
                type: 'filter',
                description: description,
                filter: filterToAdd
            });
        }

        // Clear temporary states
        setSimpleFilters({});
        setAdvancedFilters({ logic: 'AND', filters: [] });
        setBrushedIds([]);
        setCurrentPolygon(null);
        setCurrentEmbeddingRegion(null);
        setSelectedIds([]);
        // A token Select-All is baked into the in_selection stage above — clear it too, or the
        // grid keeps showing "all selected" and re-applying double-stacks the stage.
        setSelectAll(null);
        setDeselectedIds([]);
        setSelectionSource('Manual');

    }, [simpleFilters, advancedFilters, filterMode, brushedIds, selectedIds, currentPolygon, currentEmbeddingRegion, selectionSource, addStage, selectAll, deselectedIds, selectionCount]);

    const resetFilters = useCallback(() => {
        // Clear only global filters (view stages), back to the dataset's default view.
        // Preserve draft sidebar filters and selections
        setViewStages(defaultViewStages(defaultView));
        setDismissedDefaultViewId(null);
    }, [defaultView]);

    const showAllImages = useCallback(() => {
        setViewStages([]);
        if (defaultView) setDismissedDefaultViewId(defaultView.id);
    }, [defaultView]);

    // Open on the default view once the dataset's answer says which it is (see opensOnDefaultView).
    const openedRef = useRef(false);
    useEffect(() => {
        if (defaultView === undefined || openedRef.current) return;
        openedRef.current = true;
        if (opensOnDefaultView(viewStages, dismissedDefaultViewId, defaultView)) {
            replaceHistoryRef.current = true;
            // eslint-disable-next-line react-hooks/set-state-in-effect -- once per visit, when the dataset's answer arrives
            setViewStages(defaultViewStages(defaultView));
        }
    }, [defaultView]); // eslint-disable-line react-hooks/exhaustive-deps -- once, with the filters this visit opened with

    // A search's result set becomes THE view. It replaces the applied stages AND every other
    // input the grid would otherwise AND with it: draft sidebar / Query Builder filters, the
    // embeddings brush and region, the map polygon and the selection. Before this, a forgotten
    // draft filter silently intersected a 250-result similarity search down to a handful of
    // images while the chip still said the search had succeeded.
    const showResultsAsView = useCallback((stage: ViewStage) => {
        setViewStages([stage]);
        setSimpleFilters({});
        setAdvancedFilters({ logic: 'AND', filters: [] });
        setBrushedIds([]);
        setCurrentPolygon(null);
        setCurrentEmbeddingRegion(null);
        setSelectedIds([]);
        setSelectAll(null);
        setDeselectedIds([]);
    }, []);

    const clearSelection = useCallback(() => {
        setSelectedIds([]);
        setSelectAll(null);
        setDeselectedIds([]);
        // Do not reset source here as it might be used for re-selection
    }, []);

    // A re-fit moved every point of a map: a lasso drawn on the old map (an applied "Embeddings Region"
    // stage, one inside a saved view, or the draft) is refused by every request that carries it. Drop it,
    // so the grid, the sidebar and the scatter load again and the selection can be drawn on the new map.
    const { addToast } = useToast();
    const regionRef = useRef(currentEmbeddingRegion);
    useEffect(() => { regionRef.current = currentEmbeddingRegion; }, [currentEmbeddingRegion]);
    const announced = useRef<string | null>(null);
    useEffect(() => {
        const onRefitted = (event: Event) => {
            const { datasetId: refitted, field, modelId } = (event as CustomEvent<MapRefitted>).detail;
            if (refitted !== datasetId) return;
            setViewStages((prev) => {
                const kept = prev.filter((stage) => !holdsStaleLasso(stage.filter, field, modelId));
                return kept.length === prev.length ? prev : kept;
            });
            const region = regionRef.current;
            if (region && region.field === field && (region.modelId ?? null) !== modelId) {
                setCurrentEmbeddingRegion(null);
                setBrushedIds([]);
            }
            const key = `${field}:${modelId}`;
            if (announced.current !== key) {  // every request carrying the lasso answers alike: say it once
                announced.current = key;
                addToast(`The map of ${field} was re-fitted, so the lasso selection drawn on the old map was removed. Draw it again on the new map.`, 'warning', 10000);
            }
        };
        window.addEventListener(MAP_REFITTED_EVENT, onRefitted);
        return () => window.removeEventListener(MAP_REFITTED_EVENT, onRefitted);
    }, [datasetId, addToast]);

    const value = {
        globalFilters,
        userFilters,
        viewStages,
        setViewStages,
        addStage,

        removeStage,
        updateStage,
        stagedFilters,
        activeFilters: globalFilters,
        filterMode,
        simpleFilters,
        advancedFilters,
        setFilterMode,
        setSimpleFilters,
        setAdvancedFilters,
        applyFilters,
        resetFilters,
        defaultView,
        setDefaultView,
        showAllImages,
        hasUnappliedChanges,
        selectedIds,
        setSelectedIds,
        brushedIds,
        setBrushedIds,
        currentPolygon,
        setCurrentPolygon,
        currentEmbeddingRegion,
        setCurrentEmbeddingRegion,
        clearSelection,
        selectAll,
        setSelectAll,
        deselectedIds,
        setDeselectedIds,
        isSampleSelected,
        selectionCount,
        toggleSampleSelected,
        selectionSource,
        setSelectionSource,
        showResultsAsView,
    };

    return (
        <FilterContext.Provider value={value}>
            {children}
        </FilterContext.Provider>
    );
}

export function useFilters() {
    const context = useContext(FilterContext);
    if (context === undefined) {
        throw new Error('useFilters must be used within a FilterProvider');
    }
    return context;
}
