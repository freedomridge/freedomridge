'use client';

import React, { createContext, useContext, useState, useCallback, useMemo, useEffect, useRef } from 'react';
import type { Feature, Polygon } from 'geojson';
import { ComplexFilter, ViewStage } from '@/lib/api';
import { convertSimpleFiltersToComplex, describeFilter, upgradeDraftKeys } from '@/lib/filter-utils';
import { type DefaultView, defaultViewStages, isDefaultViewStage, opensOnDefaultView } from '@/lib/default-view';
import { type DatasetLink, type LinkSort, linkSetsFilters, openingOf, withSample, withSort } from '@/lib/links';
import { type ExpiredSelection, expiredSelection, filterHasInSelection } from '@/lib/filter-query';
import { lassoFilter } from '@/lib/stale-lasso';
import { type SourceView, sourceOf } from '@/lib/source-view';

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
// layoutId: the layout of the map the lasso was drawn on (the scatter's layout_id), recorded on the filter it makes, with
// modelId until 2.12 (lib/stale-lasso.ts lassoFilter).
type EmbeddingRegion = { field: string; polygon: number[][]; token: string; total: number; layoutId?: string | null; modelId?: string | null };

// The spelling of simpleFilters' keys a saved state uses: 2 marks exclusions and bounds with ':' (filter-utils); a
// state saved without it used `exclude_` and `_min` / `_max`, and is read through upgradeDraftKeys.
const DRAFT_KEYS_VERSION = 2;

interface PersistedFilterState {
    viewStages: ViewStage[];
    filterMode: 'simple' | 'advanced';
    simpleFilters: SimpleFilters;
    draftKeysVersion?: number;
    advancedFilters: ComplexFilter;
    brushedIds: number[];
    // Kept with brushedIds or not at all: the ids alone would come back as a selection of the visible points only.
    currentEmbeddingRegion?: EmbeddingRegion | null;
    selectedIds: number[];
    // Kept with selectedIds (see selectionGrain). Absent in a state saved before it was.
    selectionGrain?: Grain | null;
    currentPolygon: MapPolygon;
    selectionSource: string;
    // The default view this browser's viewer chose to see past (Show all images, or removing its filters):
    // the dataset doesn't open on it again until they clear the filters.
    dismissedDefaultViewId?: number | null;
    // The saved view the filters came from (lib/source-view.ts): kept with them, so after a reload they can still
    // update it.
    sourceView?: SourceView | null;
    // The chips of applied selections that couldn't be saved (isEphemeralSelectionStage), shown expired.
    expiredSelections?: ExpiredSelection[];
}

function loadFilterState(datasetId: number | undefined): PersistedFilterState | null {
    if (!datasetId || typeof window === 'undefined') return null;
    try {
        const raw = localStorage.getItem(`${STORAGE_PREFIX}${datasetId}`);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        // Basic validation
        if (!Array.isArray(parsed.viewStages)) return null;
        if (parsed.draftKeysVersion !== DRAFT_KEYS_VERSION && parsed.simpleFilters && typeof parsed.simpleFilters === 'object') {
            parsed.simpleFilters = upgradeDraftKeys(parsed.simpleFilters);
        }
        if (!Array.isArray(parsed.expiredSelections)) parsed.expiredSelections = [];
        return parsed as PersistedFilterState;
    } catch {
        return null;
    }
}

const MAX_PERSISTED_IDS = 50_000;  // Skip persisting transient selections larger than this
const MAX_STORAGE_BYTES = 2 * 1024 * 1024;  // 2MB hard cap per dataset

// A selection stage that must NOT be written to history state or localStorage, because it
// wouldn't survive being restored:
//   - an inline id list too large to carry (browsers cap history.state near ~16MB, so a raw
//     2M-id stage overflows it and pushState throws); or
//   - a server-side in_selection token — region_selections tokens are TTL-pruned after 2h
//     (create_selection_token), so a restored stage referencing an expired token silently
//     returns 0 rows.
// It stays live for the rest of the page session. What is saved instead is its chip, expired
// (ExpiredSelection): restored, it filters nothing and says so, rather than vanish silently.
function isEphemeralSelectionStage(s: ViewStage): boolean {
    if (s.type === 'selection' && Array.isArray(s.filter?.value) && s.filter.value.length > MAX_PERSISTED_IDS) return true;
    return filterHasInSelection(s.filter);
}

// The expired chips to save: those already shown, then one for each applied stage that can't be saved.
function withExpired(expired: ExpiredSelection[], stages: ViewStage[]): ExpiredSelection[] {
    return [...expired, ...stages.filter(isEphemeralSelectionStage).map(expiredSelection)];
}

function saveFilterState(datasetId: number | undefined, state: PersistedFilterState) {
    if (!datasetId || typeof window === 'undefined') return;
    try {
        const toSave: PersistedFilterState = {
            ...state,
            draftKeysVersion: DRAFT_KEYS_VERSION,
            // Cap transient selections — large lasso/brush selections don't persist
            brushedIds: state.brushedIds.length <= MAX_PERSISTED_IDS ? state.brushedIds : [],
            currentEmbeddingRegion: state.brushedIds.length <= MAX_PERSISTED_IDS ? state.currentEmbeddingRegion ?? null : null,
            selectedIds: state.selectedIds.length <= MAX_PERSISTED_IDS ? state.selectedIds : [],
            // Applied selection stages that can't be saved are kept as their expired chips
            viewStages: state.viewStages.filter(s => !isEphemeralSelectionStage(s)),
            expiredSelections: withExpired(state.expiredSelections ?? [], state.viewStages),
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
    expiredSelections?: ExpiredSelection[];
}): string {
    return JSON.stringify([s.viewStages, s.brushedIds, s.selectedIds, s.currentPolygon, s.selectionSource, s.currentEmbeddingRegion ?? null, s.expiredSelections ?? []]);
}

// Every entry we write KEEPS the app router's own keys (__NA + its tree). Next reloads the page
// on Back/Forward to an entry without them, and ours never had them: the router stamps the entry
// in an insertion effect, then this mount-time replaceState overwrote it with a bare object
// (Next's history patch, which would copy the keys, is installed in a later effect), and every
// push copied from that bare entry. So every Back was a full reload landing on the latest,
// persisted filters.
const withRouterState = (snap: object) => ({ ...(window.history.state ?? {}), ...snap });

// A default view is public (the server takes no other as one), which the dataset's answer doesn't say: applied as it
// is, its address is ?view=<slug>.
const publicView = (view: DefaultView) => ({ ...view, is_public: true });

// The address the browser shows: its path and query.
const shownAddress = () => `${window.location.pathname}${window.location.search}`;

// An id for each entry written: an address computed for one entry lands only on that entry, never on a later one with
// the same filters (A, then B, then A again) or another sort. (crypto.randomUUID needs https.)
const newEntryId = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

/** What the address of a state is made from (the page's AddressOf): its applied stages, expired chips and source view. */
export interface AddressState {
    stages: ViewStage[];
    expired: ExpiredSelection[];
    source: SourceView | null;
}

/** The address of a state, as a path and query (lib/links.ts datasetAddress). */
export type AddressOf = (state: AddressState) => Promise<string>;

/**
 * The sample whose view is open, as a history entry records it: the row opened (an object in an object grid, which
 * shows as its parent image) and the grid it was opened from, whose rows next and previous walk (null: another panel;
 * unset: not known yet, a link's sample before it is shown).
 */
export interface OpenSample {
    id: number;
    panelId?: string | null;
}

// The entry the address is written for: its id, its state as the address takes it, and the sample open in it.
type AddressEntry = AddressState & { id: string; sample: number | null };

// The sample an entry has open, from the entry.
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- history.state, as the browser gives it back
const sampleOf = (state: any): OpenSample | null => (Number.isSafeInteger(state?.sample?.id) ? state.sample : null);

// An entry's state as its address takes it, from the entry (a snapshot holds no selection that can't be kept: its chip
// is among the expired ones).
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- history.state, as the browser gives it back
const addressEntryOf = (state: any): AddressEntry => ({
    id: state.entryId,
    stages: state.viewStages ?? [],
    expired: state.expiredSelections ?? [],
    source: state.sourceView ?? null,
    sample: sampleOf(state)?.id ?? null,
});

interface FilterContextType {
    // Global filters - derived from view stages (+ the grain predicate)
    globalFilters: ComplexFilter;

    // View-stage filters WITHOUT any grain predicate. Grain (image vs object) is applied
    // PER-PANEL via grainPredicate()/withGrain() — see those helpers — so panels can show
    // different grains independently. Same object as globalFilters.
    userFilters: ComplexFilter;

    // View Stages
    viewStages: ViewStage[];
    // Stages put in place of all the applied ones (not an update of them) aren't a saved view's: the source view is
    // cleared. openView applies a saved view's stages and makes it the source.
    setViewStages: React.Dispatch<React.SetStateAction<ViewStage[]>>;
    // The saved view the applied filters came from (Saved Views, the default view, a ?view= link), or null:
    // lib/source-view.ts. Its owner or an admin can update it with the applied filters while they differ.
    sourceView: SourceView | null;
    setSourceView: (source: SourceView | null) => void;
    openView: (view: Parameters<typeof sourceOf>[0], stages: ViewStage[]) => void;
    // Chips of applied selections that couldn't be kept across a reload or Back: shown expired, applying nothing.
    // Kept apart from viewStages, so nothing that reads the stages (the queries, saved views, links) sees them.
    expiredSelections: ExpiredSelection[];
    setExpiredSelections: React.Dispatch<React.SetStateAction<ExpiredSelection[]>>;
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

    // The link the page was opened with (lib/links.ts), read once as it opened; null without one, and when the page
    // reopened one of its own history entries. The page opens it and hands its filter stages to settleLink (null
    // when the link sets none or can't be opened), which puts them in place of the restored filters.
    // A `?view=` link's stages come with their view, which becomes the source view.
    link: DatasetLink | null;
    settleLink: (stages: ViewStage[] | null, source?: SourceView | null) => void;
    // A link that sets the filters is still being opened: the page holds its panels back, so the restored filters
    // aren't queried (and shown) only to be replaced.
    linkPending: boolean;
    // The grids' sort the address opened with (lib/links.ts openingOf): the page gives it to the grids before they
    // open. Undefined: they keep the one they had.
    openingSort: LinkSort | undefined;

    // The address (issue #391): each history entry this page writes gets the address of its state, and so does the
    // entry Back or Forward restores. The page says how it is made (setAddressOf: lib/links.ts datasetAddress, with
    // the grids' sort in this tab) and gives each new sort (setAddressSort), which the current address names at once.
    setAddressOf: (addressOf: AddressOf) => void;
    setAddressSort: (sort: { by?: string; order?: 'asc' | 'desc' }) => void;

    // The sample whose view is open is in the address too (`sample=`), recorded on each entry. The page says what the
    // user does with the view: opening a sample is an entry of its own (openSample); next and previous rewrite it
    // (stepSample); closing it goes Back to the entry it was opened over, or rewrites the entry without it when there
    // is none (closeSample). A view the page closes itself (a search showing its results), or a link's sample shown
    // or not found, rewrites the entry (replaceSample), never going Back; the search's results then take its place.
    openSample: (sample: OpenSample) => void;
    stepSample: (sample: OpenSample) => void;
    closeSample: () => void;
    replaceSample: (sample: OpenSample | null) => void;
    // The sample (null: none) the entry Back, Forward or a reload went to has open, when it isn't the one open before:
    // the page shows it, fetched by id, or closes its view. A new object at each such step.
    restoredSample: { sample: OpenSample | null } | null;

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
    // The grain of the grid the selection (ids or Select All) was made in: its rows are that grain's.
    // null with no selection made yet, or for a selection saved before this was kept.
    selectionGrain: Grain | null;
    setSelectionGrain: (grain: Grain | null) => void;

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

// `search`: the query the router opened the page with, read once by the page (not window.location: after login the app
// navigates here, and the address still shows the login page while this page first renders).
export function FilterProvider({ children, datasetId, search }: { children: React.ReactNode; datasetId?: number; search: string }): React.ReactNode {
    // How the page opens (lib/links.ts openingOf), decided at its first render so nothing is fetched for a state about
    // to be replaced: on one of its own history entries (a reload, or Back or Forward into a page that was unloaded),
    // whose state it restores; on the link in the address; or on the filters the browser saved for the dataset. The
    // link is kept: the address changes with the filters, and nothing reads it from there later.
    const [opening] = useState(() => openingOf(
        typeof window === 'undefined' ? null : window.history.state,
        typeof window === 'undefined' ? '' : shownAddress(),
        `/datasets/${datasetId}${search ? `?${search}` : ''}`,
    ));
    const link: DatasetLink | null = opening.link;

    // Load persisted state (runs once per datasetId). Reopening one of its entries, the page takes the entry's state
    // (what Back or Forward to it would restore), and from the saved state only what the entry doesn't hold: the draft.
    const savedState = useMemo((): Partial<PersistedFilterState> | null => {
        const saved = loadFilterState(datasetId);
        // eslint-disable-next-line @typescript-eslint/no-explicit-any -- history.state, as the browser gives it back
        const entry = opening.entry as any;
        if (!entry) return saved;
        return {
            ...saved,
            viewStages: entry.viewStages ?? [],
            expiredSelections: entry.expiredSelections ?? [],
            brushedIds: entry.brushedIds ?? [],
            currentEmbeddingRegion: entry.currentEmbeddingRegion ?? null,
            selectedIds: entry.selectedIds ?? [],
            selectionGrain: entry.selectionGrain ?? null,
            currentPolygon: entry.currentPolygon ?? null,
            selectionSource: entry.selectionSource ?? 'Manual',
            sourceView: entry.sourceView ?? null,
        };
    }, [datasetId, opening]);

    // While a link that sets the filters is opened, the default view waits: it isn't put on top of the link's
    // filters, and a link that can't be opened leaves the dataset as it would open without it.
    const [linkPending, setLinkPending] = useState(() => linkSetsFilters(link));
    // The filters came from the address (a link's, or the reopened entry's): the default view isn't put on top.
    const linkAppliedRef = useRef(opening.entry !== null);
    const linkSettledRef = useRef(link === null);

    // View Stages - The source of truth for global filters
    const [viewStages, setViewStages] = useState<ViewStage[]>(savedState?.viewStages ?? []);
    const [sourceView, setSourceView] = useState<SourceView | null>(savedState?.sourceView ?? null);
    const replaceViewStages = useCallback((next: React.SetStateAction<ViewStage[]>) => {
        if (typeof next !== 'function') setSourceView(null);
        setViewStages(next);
    }, []);
    const openView = useCallback((view: Parameters<typeof sourceOf>[0], stages: ViewStage[]) => {
        setViewStages(stages);
        setSourceView(sourceOf(view, stages));
    }, []);
    const [expiredSelections, setExpiredSelections] = useState<ExpiredSelection[]>(savedState?.expiredSelections ?? []);

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

    // The dataset's default view, from the page's dataset answer. Its id changes when an administrator picks another
    // view, and its updated_at when its filters are replaced (Update this view): together they say whether it is new.
    const [defaultView, setDefaultViewState] = useState<DefaultView | null | undefined>(undefined);
    const setDefaultView = useCallback((view: DefaultView | null) => {
        setDefaultViewState(prev => (
            prev !== undefined && (prev?.id ?? null) === (view?.id ?? null) && (prev?.updated_at ?? null) === (view?.updated_at ?? null)
                ? prev : view
        ));
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
    const [selectionGrain, setSelectionGrain] = useState<Grain | null>(savedState?.selectionGrain ?? null);

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
    // The history write due (below), written before the filters are saved: a reload reopens its entry, which is then
    // never older than the filters this browser saved.
    const writeHistoryNowRef = useRef<(() => void) | null>(null);
    useEffect(() => {
        if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
        saveTimerRef.current = setTimeout(() => {
            writeHistoryNowRef.current?.();
            saveFilterState(datasetId, {
                viewStages, filterMode, simpleFilters, advancedFilters,
                brushedIds, currentEmbeddingRegion, selectedIds, selectionGrain, currentPolygon, selectionSource, dismissedDefaultViewId,
                expiredSelections, sourceView,
            });
        }, 300);
        return () => { if (saveTimerRef.current) clearTimeout(saveTimerRef.current); };
    }, [datasetId, viewStages, filterMode, simpleFilters, advancedFilters, brushedIds, currentEmbeddingRegion, selectedIds, selectionGrain, currentPolygon, selectionSource, dismissedDefaultViewId, expiredSelections, sourceView]);

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
    // The default view applied on opening, or a link's filters, replace the opening entry: Back doesn't step to "no
    // filters", nor to filters the address never said.
    const replaceHistoryRef = useRef(false);

    // --- The address of each entry (issue #391) ---
    // An entry's address is made from its own state, never the reverse (except a link, which opens as a link). It is
    // computed after the entry is written (asynchronously: q= text is asked of the server) and put on the entry while
    // it is still the current one. The entry is the page's own once the opening is decided: a link settled, the
    // default view applied or not. Before that it records no address, so a reload (or Back from the login page) opens
    // the dataset as it would have, and nothing is written over the link being opened.
    const ownedRef = useRef(opening.entry !== null);
    const [addressOf, setAddressOfState] = useState<AddressOf | null>(null);
    const setAddressOf = useCallback((of: AddressOf) => setAddressOfState(() => of), []);
    const [addressEntry, setAddressEntry] = useState<AddressEntry | null>(null);

    // The sample whose view is open, which each entry written records (a link's from the start, so its address keeps
    // it while the sample is fetched). `samplePushed` marks the entry that opening it pushed, over the same state
    // without it: only from that one does closing the view go Back. Any other write of an entry drops the mark.
    const sampleRef = useRef<OpenSample | null>(
        sampleOf(opening.entry) ?? (Number.isSafeInteger(Number(link?.sample)) ? { id: Number(link!.sample) } : null),
    );
    // The view's own entry was just rewritten without it by the page (a search from the view, Find similar): the next
    // filters' push takes its place instead, so Back from the search's results goes straight to the state before the
    // view, not to a copy of it. Any other entry write, or Back or Forward, ends it.
    const collapseNextPushRef = useRef(false);

    // Writes `snap` as the current entry (replace) or a new one (push), with its id, the sample open, and the address it
    // holds as written (a push holds the previous entry's until its own arrives), then asks for its own address.
    const writeEntry = useCallback((snap: object, push: boolean, state: AddressState, samplePushed = false) => {
        collapseNextPushRef.current = false;
        const sample = sampleRef.current;
        const entry = { ...snap, sample, samplePushed: samplePushed && !!sample, entryId: newEntryId(), address: ownedRef.current ? shownAddress() : null };
        if (push) window.history.pushState(withRouterState(entry), '');
        else window.history.replaceState(withRouterState(entry), '');
        if (ownedRef.current) setAddressEntry({ id: entry.entryId, ...state, sample: sample?.id ?? null });
    }, []);

    // Puts `address` on the entry `id`, if it is still the current one. The state goes in without the router's own mark
    // (__NA): Next's replaceState then takes the new address as its own and copies its keys back beside our snapshot.
    // With the mark it would pass the call through unseen, keep the old address, and write that back at its next state
    // change. The entry records the address it then holds, as the browser shows it: a reload tells its own entry by it.
    const writeAddress = useCallback((id: string, address: string) => {
        const state = window.history.state;
        if (!state?._fridge || state.entryId !== id || window.location.pathname !== `/datasets/${datasetId}`) return;
        const shown = shownAddress();
        const url = new URL(address, window.location.href);
        const to = `${url.pathname}${url.search}`;
        try {
            if (to !== shown) {
                const next = { ...state, address: to };
                delete next.__NA;
                window.history.replaceState(next, '', to);
            } else if (state.address !== shown) {
                // The URL stays, so the router isn't told: only the entry's record of it changes.
                window.history.replaceState({ ...state, address: shown }, '');
            }
        } catch (e) {
            console.warn('Skipped the address write:', e);
        }
    }, [datasetId]);

    // The sort changed (it isn't history): the current address names it at once, its filters as they are, so a reload
    // right after opens on it. The entry's address is then made again: one in flight may hold the sort before.
    const setAddressSort = useCallback((sort: { by?: string; order?: 'asc' | 'desc' }) => {
        const state = window.history.state;
        if (!ownedRef.current || !state?._fridge || !state.entryId) return;
        writeAddress(state.entryId, withSort(shownAddress(), sort));
        setAddressEntry(entry => entry && { ...entry });
    }, [writeAddress]);

    useEffect(() => {
        if (!addressEntry || !addressOf) return;
        let current = true;  // a later entry, sort or way of writing it takes over
        addressOf(addressEntry).then(
            (address) => { if (current) writeAddress(addressEntry.id, withSample(address, addressEntry.sample)); },
            (error: unknown) => console.warn("Couldn't make the address:", error),
        );
        return () => { current = false; };
    }, [addressEntry, addressOf, writeAddress]);

    // The opening is decided (a link settled, the default view applied or not) and no replace of the opening entry is
    // due, which would give the entry its state: from here the entry is the page's, and its address is written.
    const openedRef = useRef(false);  // the default-view decision below has run
    const ownIfOpened = useCallback(() => {
        if (ownedRef.current || !linkSettledRef.current || !openedRef.current || replaceHistoryRef.current) return;
        ownedRef.current = true;
        const state = window.history.state;
        if (state?._fridge && state.entryId) setAddressEntry(addressEntryOf(state));
    }, []);

    const buildHistorySnapshot = useCallback(() => ({
        _fridge: true,  // marker to distinguish our entries from Next.js'
        // Exclude oversized id-list selection stages — a raw 2M-id stage overflows the
        // browser history.state limit and makes pushState throw — and token stages, keeping
        // their chips expired instead (mirrors persistence).
        viewStages: viewStages.filter(s => !isEphemeralSelectionStage(s)),
        expiredSelections: withExpired(expiredSelections, viewStages),
        brushedIds: brushedIds.length <= MAX_PERSISTED_IDS ? brushedIds : [],
        currentEmbeddingRegion: brushedIds.length <= MAX_PERSISTED_IDS ? currentEmbeddingRegion : null,
        selectedIds: selectedIds.length <= MAX_PERSISTED_IDS ? selectedIds : [],
        selectionGrain,
        currentPolygon,
        selectionSource,
        // Each entry keeps the view its filters came from: Back to them can update it again, Back past it can't.
        sourceView,
    }), [viewStages, expiredSelections, brushedIds, currentEmbeddingRegion, selectedIds, selectionGrain, currentPolygon, selectionSource, sourceView]);

    // Set initial history entry on mount (its address too, when the page reopened one of its own entries, which keeps its
    // sample and whether opening it pushed the entry)
    useEffect(() => {
        if (typeof window === 'undefined' || historyInitializedRef.current) return;
        historyInitializedRef.current = true;
        writeEntry(buildHistorySnapshot(), false, { stages: viewStages, expired: expiredSelections, source: sourceView }, opening.entry?.samplePushed === true);
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    // Push to history on meaningful state changes (debounced)
    useEffect(() => {
        if (typeof window === 'undefined' || !historyInitializedRef.current) return;
        if (historyTimerRef.current) clearTimeout(historyTimerRef.current);
        const write = () => {
            writeHistoryNowRef.current = null;
            const snap = buildHistorySnapshot();
            const state = { stages: viewStages, expired: expiredSelections, source: sourceView };
            const cur = window.history.state;
            const same = !!cur?._fridge && historyStateSig(cur) === historyStateSig(snap);
            // The opening's own replace (a link's filters, the default view): the entry is the page's from here.
            const opening = replaceHistoryRef.current;
            replaceHistoryRef.current = false;  // or the next real change would replace an entry
            if (opening && linkSettledRef.current && openedRef.current) ownedRef.current = true;
            // Right after a Back/Forward restore, its cascade of updates isn't recorded. A real change in that window
            // (a chip removed at once) replaces the restored entry rather than pushing over the forward history:
            // dropped, it left the entry and its address on the old state, which a reload brought back.
            if (opening || Date.now() < suppressPushUntilRef.current) {
                if (opening || !same) writeEntry(snap, false, state);
                return;
            }
            // Don't push a duplicate of the current entry (mount echo / no-op churn). The same filters from another
            // source view (just saved as one, say) replace it: Back goes to other filters, not to the same ones.
            if (same) {
                if ((cur.sourceView?.id ?? null) !== (snap.sourceView?.id ?? null)) writeEntry(snap, false, state);
                return;
            }
            // pushState can still throw if the snapshot is unexpectedly large (browser
            // history.state cap) — never let a history push crash the app.
            try {
                writeEntry(snap, !collapseNextPushRef.current, state);  // a replace in place of a view the page closed
            } catch (e) {
                console.warn('Skipped history push (state too large):', e);
            }
        };
        historyTimerRef.current = setTimeout(write, 400);
        writeHistoryNowRef.current = () => {
            if (historyTimerRef.current) clearTimeout(historyTimerRef.current);
            write();
        };
        return () => {
            if (historyTimerRef.current) clearTimeout(historyTimerRef.current);
            writeHistoryNowRef.current = null;
        };
    }, [viewStages, expiredSelections, brushedIds, currentEmbeddingRegion, selectedIds, currentPolygon, selectionSource, sourceView, buildHistorySnapshot, writeEntry]);

    // --- The sample whose view is open (issue #391) ---
    // A reload of an entry with a sample open reopens it.
    const [restoredSample, setRestoredSample] = useState<{ sample: OpenSample | null } | null>(() => {
        const sample = sampleOf(opening.entry);
        return sample ? { sample } : null;
    });
    // Next and previous rewrite the entry once the stepping pauses: browsers refuse (WebKit) or drop (Chrome) history
    // calls past about a hundred in a short while, and each address is two, ours and the router's.
    const sampleStepRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    // A close that went Back, until its popstate arrives: an open meanwhile waits for it (a push before it would put the
    // entry after the one being left, and Back would then land on the wrong entry).
    const closingRef = useRef(false);
    const openAfterCloseRef = useRef<OpenSample | null>(null);

    // Writes `sample` (null: none) on the current entry, or on a new entry after it (`push`: opening it), the filters'
    // write due going first so the entry holds them. A step due is superseded.
    const writeSample = useCallback((sample: OpenSample | null, push: boolean) => {
        if (sampleStepRef.current) clearTimeout(sampleStepRef.current);
        sampleStepRef.current = null;
        writeHistoryNowRef.current?.();
        sampleRef.current = sample;
        const cur = window.history.state;
        if (!cur?._fridge) return;
        const { stages, expired, source } = addressEntryOf(cur);
        try {
            writeEntry(cur, push, { stages, expired, source }, push || cur.samplePushed === true);
        } catch (e) {
            console.warn('Skipped the history write:', e);
        }
    }, [writeEntry]);

    const openSample = useCallback((sample: OpenSample) => {
        if (closingRef.current) openAfterCloseRef.current = sample;
        else writeSample(sample, true);
    }, [writeSample]);

    const stepSample = useCallback((sample: OpenSample) => {
        if (closingRef.current) {
            if (openAfterCloseRef.current) openAfterCloseRef.current = sample;
            return;
        }
        sampleRef.current = sample;  // a filter entry written meanwhile (Space) records it
        if (sampleStepRef.current) clearTimeout(sampleStepRef.current);
        sampleStepRef.current = setTimeout(() => writeSample(sampleRef.current, false), 300);
    }, [writeSample]);

    // Only the entry opening the sample pushed goes Back: from any other (a selection toggled in the view pushed its own,
    // or the page opened on a sample= link) Back would undo something else, or leave the page.
    const closeSample = useCallback(() => {
        if (closingRef.current) {
            openAfterCloseRef.current = null;
            return;
        }
        if (sampleStepRef.current) writeSample(sampleRef.current, false);  // Forward reopens the sample last shown
        else writeHistoryNowRef.current?.();
        if (window.history.state?._fridge && window.history.state.samplePushed === true) {
            sampleRef.current = null;
            closingRef.current = true;
            window.history.back();
        } else {
            writeSample(null, false);
        }
    }, [writeSample]);

    // Never Back: the page closing the view itself goes on to set new filters, which a Back arriving after would undo.
    const replaceSample = useCallback((sample: OpenSample | null) => {
        if (closingRef.current) {
            openAfterCloseRef.current = null;
            return;
        }
        if (!sample && !sampleRef.current && !sampleStepRef.current) return;
        writeHistoryNowRef.current?.();
        const collapse = !sample && window.history.state?.samplePushed === true;
        writeSample(sample, false);
        collapseNextPushRef.current = collapse;
    }, [writeSample]);

    useEffect(() => () => { if (sampleStepRef.current) clearTimeout(sampleStepRef.current); }, []);

    // Listen for popstate (back/forward)
    useEffect(() => {
        if (typeof window === 'undefined') return;
        const handler = (event: PopStateEvent) => {
            collapseNextPushRef.current = false;
            // A step due was for the entry left.
            if (sampleStepRef.current) clearTimeout(sampleStepRef.current);
            sampleStepRef.current = null;
            const closing = closingRef.current;
            closingRef.current = false;
            const state = event.state;
            if (!state || !state._fridge) return;  // Not our entry
            const sample = sampleOf(state);
            const opened = sampleRef.current;
            sampleRef.current = sample;
            if (closing) {
                // The Back of a close: the entry the sample was opened over, with the state on screen (nothing to
                // restore, so nothing done since the close is undone). An open asked for meanwhile goes after it now.
                // It isn't the entry a Back or Forward just before restored: a change from here is pushed after it,
                // not put in its place.
                suppressPushUntilRef.current = 0;
                if (ownedRef.current && state.entryId) setAddressEntry(addressEntryOf(state));
                const open = openAfterCloseRef.current;
                openAfterCloseRef.current = null;
                if (open) writeSample(open, true);
                return;
            }
            if ((sample?.id ?? null) !== (opened?.id ?? null)) setRestoredSample({ sample });
            // Suppress pushes briefly so the re-renders/refetches this restore triggers
            // don't re-push the restored state and clobber the forward history.
            suppressPushUntilRef.current = Date.now() + 600;
            setViewStages(state.viewStages ?? []);
            setExpiredSelections(state.expiredSelections ?? []);
            setBrushedIds(state.brushedIds ?? []);
            setCurrentEmbeddingRegion(state.currentEmbeddingRegion ?? null);
            setSelectedIds(state.selectedIds ?? []);
            setSelectionGrain(state.selectionGrain ?? null);
            setCurrentPolygon(state.currentPolygon ?? null);
            setSelectionSource(state.selectionSource ?? 'Manual');
            setSourceView(state.sourceView ?? null);
            // Its address again, made from its state with the sort the grids have now (the sort isn't history), and
            // made at all if its own was dropped (Back before it arrived). Unchanged, nothing is written.
            if (ownedRef.current && state.entryId) setAddressEntry(addressEntryOf(state));
        };
        window.addEventListener('popstate', handler);
        return () => window.removeEventListener('popstate', handler);
    }, [writeSample]);

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
            base.filters?.push(lassoFilter(currentEmbeddingRegion));
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
                filter: lassoFilter(currentEmbeddingRegion),
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
        setSelectionGrain(null);
        setSelectionSource('Manual');

    }, [simpleFilters, advancedFilters, filterMode, brushedIds, selectedIds, currentPolygon, currentEmbeddingRegion, selectionSource, addStage, selectAll, deselectedIds, selectionCount]);

    const resetFilters = useCallback(() => {
        // Clear only global filters (view stages), back to the dataset's default view.
        // Preserve draft sidebar filters and selections
        const stages = defaultViewStages(defaultView);
        setViewStages(stages);
        setSourceView(defaultView && stages.length ? sourceOf(publicView(defaultView), stages) : null);
        setExpiredSelections([]);
        setDismissedDefaultViewId(null);
    }, [defaultView]);

    const showAllImages = useCallback(() => {
        setViewStages([]);
        setSourceView(null);
        setExpiredSelections([]);
        if (defaultView) setDismissedDefaultViewId(defaultView.id);
    }, [defaultView]);

    // Open on the default view once the dataset's answer says which it is (see opensOnDefaultView), and once a link
    // that sets the filters has settled: never on top of the link's filters, nor under an expired selection's chip.
    // Decided once per visit: removing that chip later, or Back/Forward (which restore their entry), never applies it.
    // The opening is then decided, and the entry becomes the page's (ownIfOpened): its address is written once, for
    // the state it opened on, not first for the saved filters and then for the default view.
    useEffect(() => {
        if (defaultView === undefined || openedRef.current || linkPending) return;
        openedRef.current = true;
        if (!linkAppliedRef.current && defaultView && opensOnDefaultView(viewStages, dismissedDefaultViewId, defaultView, expiredSelections)) {
            replaceHistoryRef.current = true;
            openView(publicView(defaultView), defaultViewStages(defaultView));
        }
        ownIfOpened();
    }, [defaultView, linkPending]); // eslint-disable-line react-hooks/exhaustive-deps -- once, with the filters this visit opened with

    // The link's stages replace the restored ones in the opening entry (replaced, as the default view's are, not
    // pushed): its address is then its own, and Back doesn't step to filters the address never said. The filters the
    // browser saved for the dataset become the link's at the next save, as at any change. Settled, and the opening
    // decided, the entry is the page's and gets the address of the state it shows, the link's own parameters giving
    // way to it, also when the link couldn't be opened.
    const settleLink = useCallback((stages: ViewStage[] | null, source: SourceView | null = null) => {
        if (stages) {
            linkAppliedRef.current = true;
            replaceHistoryRef.current = true;
            setViewStages(stages);
            setSourceView(source);
            setExpiredSelections([]);
        }
        linkSettledRef.current = true;
        setLinkPending(false);
        ownIfOpened();
    }, [ownIfOpened]);

    // A search's result set becomes THE view. It replaces the applied stages AND every other
    // input the grid would otherwise AND with it: draft sidebar / Query Builder filters, the
    // embeddings brush and region, the map polygon and the selection. Before this, a forgotten
    // draft filter silently intersected a 250-result similarity search down to a handful of
    // images while the chip still said the search had succeeded.
    const showResultsAsView = useCallback((stage: ViewStage) => {
        setViewStages([stage]);
        setSourceView(null);
        setExpiredSelections([]);
        setSimpleFilters({});
        setAdvancedFilters({ logic: 'AND', filters: [] });
        setBrushedIds([]);
        setCurrentPolygon(null);
        setCurrentEmbeddingRegion(null);
        setSelectedIds([]);
        setSelectAll(null);
        setDeselectedIds([]);
        setSelectionGrain(null);
    }, []);

    const clearSelection = useCallback(() => {
        setSelectedIds([]);
        setSelectAll(null);
        setDeselectedIds([]);
        setSelectionGrain(null);
        // Do not reset source here as it might be used for re-selection
    }, []);

    const value = {
        globalFilters,
        userFilters,
        viewStages,
        setViewStages: replaceViewStages,
        sourceView,
        setSourceView,
        openView,
        expiredSelections,
        setExpiredSelections,
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
        selectionGrain,
        setSelectionGrain,
        selectionSource,
        setSelectionSource,
        showResultsAsView,
        link,
        settleLink,
        linkPending,
        openingSort: opening.sort,
        setAddressOf,
        setAddressSort,
        openSample,
        stepSample,
        closeSample,
        replaceSample,
        restoredSample,
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
