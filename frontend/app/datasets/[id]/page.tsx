'use client';

import { toggleOverlayField } from '@/lib/labels';

import { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import type { Feature, Polygon } from 'geojson';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, isRescoringRefusal, getSamples, getDatasetSchema, getDataset, getEmbeddingFields, searchSimilar, searchByImage, searchObjectToPatches, updateDataset, setClassRule, sampleDuplicates, notifySamplesMutated, createSelectionToken, type Dataset, type Filters, type Sample, type SchemaField, type ComplexFilter, type ViewStage } from '@/lib/api';
import { describeFilter, convertSimpleFiltersToComplex } from '@/lib/filter-utils';
import { usePatchState } from '@/hooks/usePatchState';
import { useSampleCount } from '@/hooks/useSampleCount';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { FilterSidebar } from '@/components/filter-sidebar';
import { SampleModal } from '@/components/sample-modal';
import { DatasetSettingsModal } from '@/components/dataset-settings-modal';
import { DatasetDescription } from '@/components/dataset-description';
import { SELECT_ALL_TOKEN_THRESHOLD, GUEST_SELECT_ALL_MESSAGE } from '@/components/select-all-button';
import { SimilaritySearchModal } from '@/components/similarity-search-modal';
import { FindUnlabeledModal } from '@/components/find-unlabeled-modal';
import { TagManagementModal } from '@/components/TagManagementModal';
import ViewManager from '@/components/ViewManager';
import { GearIcon, CheckIcon, Cross2Icon } from '@radix-ui/react-icons';
import { PanelLayout } from '@/components/panel-layout';
import { QueryBuilderBar } from '@/components/query-builder-bar';
import { FindSimilar, DEFAULT_SEARCH_LIMIT, searchLimitOf, type ImageSearchInput, type SearchScope } from '@/components/find-similar';
import { Rocket } from 'lucide-react';
import dynamic from 'next/dynamic';
const MapPanel = dynamic(() => import('@/components/map-panel').then(mod => mod.MapPanel), { ssr: false });

// Above this many matches, Select-All switches from an explicit id list to a server-side
// selection token (weightless). Mirrors FilterContext's 50k persistence cap: at or below it
// an id array is cheap in state/history/POST, and id-based ops (concepts/similarity) work.
import { EmbeddingsPanel } from '@/components/embeddings-panel';
import { DuplicatesPanel } from '@/components/duplicates-panel';
import { ConceptsPanel } from '@/components/concepts-panel';
import { HistogramPanel } from '@/components/histogram-panel';
import { GridPanel } from '@/components/grid-panel';
import type { GridMore } from '@/components/dataset-grid';
import { SparseSamplePanel } from '@/components/sparse-sample-panel';
import { ExportDialog } from '@/components/export-dialog';
import { PanelType } from '@/components/panel-content';
import { FilterProvider, useFilters, withGrain, type Grain } from '@/contexts/FilterContext';
import { useUser } from '@/contexts/UserContext';

import { ProtectedRoute } from '@/components/ProtectedRoute';
import { Header } from '@/components/header';
import { DatasetJobs } from '@/components/dataset-jobs';
import { DatasetDeletion } from '@/components/dataset-deletion';
import { DatasetJobHistoryDialog } from '@/components/job-history';
import { MirrorBadge } from '@/components/mirror-badge';

// When the machine's network changes (an address coming or going, e.g. a docker container starting), Chromium
// fails every request still waiting for one of its six connections to the server (net::ERR_NETWORK_CHANGED),
// and right after a dataset opens many wait (thumbnails, counts, the sidebar). fetch then rejects with a
// TypeError before any answer. React Query asks a dropped query again; Select All's requests are its own,
// so it asks again too, instead of failing the whole Select All with an alert.
async function retryDropped<T>(request: () => Promise<T>): Promise<T> {
    for (let attempt = 1; ; attempt++) {
        try {
            return await request();
        } catch (error) {
            if (!(error instanceof TypeError) || attempt === 3) throw error;
            await new Promise(resolve => setTimeout(resolve, 500 * attempt));
        }
    }
}

// A draft-filter chip with its own clear (✕) button, so each transient selection /
// filter group in the draft bar can be cleared independently (not just "Clear Draft").
function DraftChip({ label, tone, onClear }: {
    label: string;
    tone: 'cyan' | 'indigo' | 'purple' | 'zinc';
    onClear: () => void;
}) {
    const tones = {
        cyan: 'bg-cyan-900/40 text-cyan-300 border-cyan-700/50',
        indigo: 'bg-indigo-900/40 text-indigo-300 border-indigo-700/50',
        purple: 'bg-purple-900/40 text-purple-300 border-purple-700/50',
        zinc: 'bg-zinc-800/60 text-zinc-400 border-zinc-700/50',
    } as const;
    return (
        <span className={`px-2 py-1 rounded border flex items-center gap-1.5 ${tones[tone]}`}>
            <span>{label}</span>
            <button onClick={onClear} title="Clear this" className="opacity-70 hover:opacity-100 transition-opacity">
                <Cross2Icon className="w-3 h-3" />
            </button>
        </span>
    );
}

export default function DatasetPage() {
    const params = useParams();
    const idParam = params?.id;
    const datasetId = idParam ? parseInt(Array.isArray(idParam) ? idParam[0] : idParam) : undefined;

    return (
        <FilterProvider datasetId={datasetId}>
            <ProtectedRoute>
                <DatasetPageContent />
            </ProtectedRoute>
        </FilterProvider>
    );
}

function DatasetPageContent() {
    const params = useParams();
    const idParam = params?.id;
    const datasetId = idParam ? parseInt(Array.isArray(idParam) ? idParam[0] : idParam) : null;

    const [isMounted, setIsMounted] = useState(false);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
    useEffect(() => setIsMounted(true), []);

    const [isSettingsOpen, setIsSettingsOpen] = useState(false);
    const [isJobHistoryOpen, setIsJobHistoryOpen] = useState(false);
    const [selectedSample, setSelectedSample] = useState<Sample | null>(null);
    // The grid panel the open modal was launched from — its sample list drives prev/next.
    const [modalSourcePanelId, setModalSourcePanelId] = useState<string | null>(null);
    // The clicked ROW's id (the object id in an object grid, even though the modal then shows
    // its PARENT image) — this, not the displayed sample's id, is what indexes prev/next.
    const [modalSourceId, setModalSourceId] = useState<number | null>(null);
    // When the modal shows an object's PARENT image, the object's box to outline.
    const [highlightCuboidField, setHighlightCuboidField] = useState<string | null>(null);
    const [highlightCuboidId, setHighlightCuboidId] = useState<string | null>(null);
    const [highlightBox, setHighlightBox] = useState<[number, number, number, number] | null>(null);

    // Per-panel grain: each grid panel (by layout id) independently shows images or objects
    // (+ an optional image->objects pivot), so an image grid and an object grid coexist.
    // Default is image. The scatter/sidebar/map/count default to image grain too.
    const [panelGrain, setPanelGrain] = useState<Record<string, { grain: Grain; pivot: ComplexFilter | null }>>({});
    const getGrain = useCallback(
        (id: string) => panelGrain[id] ?? { grain: 'image' as Grain, pivot: null },
        [panelGrain],
    );
    // Per-panel grid sample lists: each grid panel writes the rows it currently shows into its
    // OWN slot (keyed by panel id), so an action reads the rows of the panel it fired from —
    // not whichever grid last wrote a single shared ref. The slots are plain mutable containers
    // (not React refs) kept in a ref'd registry, so each is stable across renders.
    const gridSamplesRefs = useRef<Record<string, React.MutableRefObject<Sample[]>>>({});
    const getGridSamplesRef = useCallback((panelId: string): React.MutableRefObject<Sample[]> => {
        const reg = gridSamplesRefs.current;
        if (!reg[panelId]) reg[panelId] = { current: [] };
        return reg[panelId];
    }, []);
    // Per-panel "load more" hooks, registered the same way: the modal walks a grid's loaded rows
    // and asks that grid for its next page at the end.
    const gridMoreRefs = useRef<Record<string, React.MutableRefObject<GridMore | null>>>({});
    const getGridMoreRef = useCallback((panelId: string): React.MutableRefObject<GridMore | null> => {
        const reg = gridMoreRefs.current;
        if (!reg[panelId]) reg[panelId] = { current: null };
        return reg[panelId];
    }, []);
    const [similaritySearchSample, setSimilaritySearchSample] = useState<Sample | null>(null);
    // The seeds of the search being set up, fixed when the dialog opens: the grid's selection when
    // several are ticked (their embeddings are averaged), else just the dialog's sample. It used to
    // be re-read from the selection at search time, so the modal's Find Similar averaged whatever
    // the grid had ticked instead of the sample on screen.
    const [similaritySeedIds, setSimilaritySeedIds] = useState<number[] | null>(null);
    // Find-unlabeled dialog target (object seed + the panel that triggered it), or null.
    const [findUnlabeled, setFindUnlabeled] = useState<{ objectId: number; panelId: string } | null>(null);
    const [isSearching, setIsSearching] = useState(false);
    const textSearchSeq = useRef(0);
    // Find similar: how many of the closest images to show (shared with the search's chip), whether a search stays
    // within the filters, and the latest search, which its chip's "top N" runs again.
    const [searchLimit, setSearchLimit] = useState(DEFAULT_SEARCH_LIMIT);
    const [searchScope, setSearchScope] = useState<SearchScope>('filtered');
    const [lastSearch, setLastSearch] = useState<{ id: string; scope: SearchScope; text?: string; image?: ImageSearchInput } | null>(null);
    const [showTagModal, setShowTagModal] = useState(false);
    // Tag dialog opened from the single-image view: tags exactly the image shown (without it,
    // tagging it meant closing the view, ticking its tile, using the grid's Tag).
    const [tagSampleId, setTagSampleId] = useState<number | null>(null);
    const [showExportDialog, setShowExportDialog] = useState(false);
    const queryClient = useQueryClient();
    const { user } = useUser();
    const [selectionNotice, setSelectionNotice] = useState<{ view: string; message: string } | null>(null);
    // currentPolygon moved to context

    // Use Global Filter Context
    const {
        stagedFilters,
        globalFilters,
        userFilters,
        viewStages,
        setViewStages,
        addStage,
        removeStage,
        updateStage,
        filterMode,
        setFilterMode,
        simpleFilters,
        setSimpleFilters,
        advancedFilters,
        setAdvancedFilters,
        applyFilters,
        resetFilters,
        defaultView,
        setDefaultView,
        showAllImages,
        showResultsAsView,
        hasUnappliedChanges,
        selectedIds,
        setSelectedIds,
        clearSelection,
        selectAll,
        setSelectAll,
        deselectedIds,
        setDeselectedIds,
        selectionCount,
        toggleSampleSelected,
        isSampleSelected,
        brushedIds,
        setBrushedIds,
        currentPolygon,
        setCurrentPolygon,
        currentEmbeddingRegion,
        setCurrentEmbeddingRegion,
        selectionSource,
        setSelectionSource,
    } = useFilters();

    const { data: dataset, error: datasetError, isFetchedAfterMount: datasetFetchedOnOpen } = useQuery({
        queryKey: ['dataset', datasetId],
        queryFn: () => getDataset(datasetId!),
        enabled: !!datasetId,
        // Asked again each time the dataset opens, also after in-app navigation: the sidebar reads
        // whether its sections open expanded from this visit's answer, not from one cached before an
        // ingest took the dataset across the line.
        staleTime: 0,
        refetchOnWindowFocus: false,
    });

    const [modalMediaField, setModalMediaFieldLocal] = useState<string>('filepath');
    const [activeOverlayFields, setActiveOverlayFields] = useState<string[]>([]);

    // A grid's View choice is saved as the field the dataset's grids open on next time. The cached dataset is left
    // as it is, so the other open grids keep their own choice.
    const saveGridMediaField = (field: string) => {
        if (datasetId) updateDataset(datasetId, { grid_media_field: field }).catch(() => {});
    };
    const setModalMediaField = (field: string) => {
        setModalMediaFieldLocal(field);
        if (datasetId) {
            updateDataset(datasetId, { modal_media_field: field }).catch(() => {});
            queryClient.setQueryData(['dataset', datasetId], (old: Dataset | undefined) =>
                old ? { ...old, modal_media_field: field } : old
            );
        }
    };

    // Fast mode (default true; `lightningMode` / `lightning_mode` in code and the API)
    const [lightningMode, setLightningMode] = useState(true);

    // Initialize media fields from saved dataset preferences
    useEffect(() => {
        if (dataset) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
            setModalMediaFieldLocal(dataset.modal_media_field || 'filepath');
        }
    }, [dataset]);

    // The dataset opens on its default view (FilterContext applies it once it knows which it is).
    useEffect(() => {
        if (dataset) setDefaultView(dataset.default_view ?? null);
    }, [dataset, setDefaultView]);


    // Does this dataset have object rows at all? The cross-grain link only applies when it
    // does (else withGrain emits a plain grain filter, preserving regression negation).
    const { data: datasetHasObjects, isPending: hasObjectsPending, failureCount: hasObjectsFailures } = useQuery({
        queryKey: ['hasObjects', datasetId],
        queryFn: async () => {
            // limit 0: the count alone (an index lookup). limit 1 also read a first row ORDER BY id,
            // which walked every id when the dataset had no objects.
            const r = await getSamples(datasetId!, 0, 0, {
                logic: 'AND',
                filters: [{ field: 'media_type', operator: '=', value: 'object' }],
            });
            return (r.total ?? 0) > 0;
        },
        enabled: !!datasetId,
        staleTime: 300_000,
        refetchOnWindowFocus: false,
    });
    const hasObjects = !!datasetHasObjects;
    // The answer changes the shape of a FILTERED request (withGrain's cross-grain link), so a filtered
    // count or first page sent before it is sent again after it. Those wait for it (or its first failure).
    const grainKnown = !hasObjectsPending || hasObjectsFailures > 0;

    // The top-bar count, sidebar/facets, map, and duplicates show the dataset at IMAGE
    // grain (objects are an opt-in per-grid grain), so wrap the grain-less globalFilters.
    const imageGrainFilters = useMemo(() => withGrain(globalFilters, 'image', null, hasObjects), [globalFilters, hasObjects]);

    // Fetch Global Count (Independent of Draft/Staged Filters)
    // This drives the "X samples" count in the top bar, ensuring it doesn't jump
    // when using transient selections (like embeddings or map) until applied.
    // Shared with any grid asking the same (useSampleCount): with nothing staged, one request answers both.
    // After a tag edit or a finished job it keeps the number while the same filters are counted again.
    const { data: appliedCount, failureReason: globalCountFailure } = useSampleCount(datasetId, imageGrainFilters, {
        enabled: grainKnown || !globalFilters.filters?.length,
        keepWhileRecounting: true,
    });

    // Schema query uses different filters based on lightning mode:
    // - Lightning mode ON: use globalFilters (cached, performance-focused)
    // - Lightning mode OFF: use globalFilters + transient selections (brushedIds, polygon)
    //   NOTE: We do NOT include simple filter drafts to avoid reloading the panel on every change
    const schemaFilters = useMemo(() => {
        if (lightningMode) {
            return imageGrainFilters;
        }

        // Build filters that include globalFilters + transient selections
        // but NOT simple filter drafts
        const transientFilters: ComplexFilter[] = [];

        // Add brushed IDs from embeddings — but NOT when an embeddings region is active.
        // With a region, the in_selection token below is the authoritative (full) filter and
        // brushedIds is only the visible-point highlight; adding it here would wrongly
        // intersect the selection down to just the on-screen points.
        if (brushedIds.length > 0 && !currentEmbeddingRegion) {
            transientFilters.push({
                field: 'id',
                operator: 'in',
                value: brushedIds
            });
        }

        // Add selection IDs (selected samples)
        if (selectedIds.length > 0) {
            transientFilters.push({
                field: 'id',
                operator: 'in',
                value: selectedIds
            });
        }

        // Add polygon from map
        if (currentPolygon) {
            transientFilters.push({
                field: 'location',
                operator: 'geo_polygon',
                value: currentPolygon.geometry.coordinates[0]
            });
        }

        // Add embeddings lasso region as a LIVE geometric filter (embedding_polygon),
        // so it composes with the other filters and stays consistent with the applied
        // stage. (See FilterContext for why this isn't the frozen in_selection token.)
        if (currentEmbeddingRegion) {
            transientFilters.push({
                field: currentEmbeddingRegion.field,
                operator: 'embedding_polygon',
                value: currentEmbeddingRegion.polygon,
                model_id: currentEmbeddingRegion.modelId ?? null,
            });
        }

        // If no transient selections, just use the image-grain filters
        if (transientFilters.length === 0) {
            return imageGrainFilters;
        }

        // Combine image-grain filters with transient selections
        return {
            logic: 'AND' as const,
            filters: [
                ...(imageGrainFilters.filters || []),
                ...transientFilters
            ]
        };
    }, [lightningMode, imageGrainFilters, brushedIds, selectedIds, currentPolygon, currentEmbeddingRegion]);

    const { data: schema, isLoading: schemaLoading, isPlaceholderData: schemaIsPrevious } = useQuery<SchemaField[]>({
        queryKey: ['schema', datasetId, schemaFilters, lightningMode],
        queryFn: ({ signal }) => getDatasetSchema(datasetId!, schemaFilters, lightningMode, signal),
        enabled: !!datasetId,
        staleTime: 60_000,
        refetchOnWindowFocus: false,
        // Field-stats can take a long time on large filtered datasets. Stacking
        // retries triggers Postgres LockManager LWLock contention and ECONNRESET
        // cascades — one in-flight request is always better than four piling up.
        // A concept being rescored is the exception: retried every few seconds until its job ends.
        retry: (_count: number, error: unknown) => isRescoringRefusal(error),
        // Keep serving the previous schema while a filtered recompute is in flight —
        // applying a filter changes the queryKey, and without this every consumer
        // (sidebar, stage editor field types/operators) sees schema=[] until the slow
        // filtered field-stats call returns.
        placeholderData: (prev: SchemaField[] | undefined) => prev,
    });

    // Per-field overlay display config (heatmap colormap/range/opacity), keyed by field
    // name, derived from the schema and passed to the renderers (grid composite URL +
    // modal canvas) so UI-set config is applied.
    const overlayConfigs = useMemo(() => {
        const m: Record<string, { colormap?: string; range?: [number, number]; opacity?: number }> = {};
        (schema || []).forEach((f: SchemaField) => { if (f.config) m[f.name] = f.config; });
        return m;
    }, [schema]);

    // Fetch available embedding and hash fields for similarity search
    const { data: embeddingFields } = useQuery({
        queryKey: ['embedding-fields', datasetId],
        queryFn: () => getEmbeddingFields(datasetId!),
        enabled: !!datasetId,
        staleTime: 60_000,
        refetchOnWindowFocus: false,
    });

    // Cross-grain highlight, BOTH directions, so a selection lights up its partners in any
    // panel regardless of grain (mirroring how a patch lights up its image):
    //   forward — selected OBJECTS -> their parent images (highlighted in an image scatter)
    //   reverse — selected IMAGES  -> their objects (highlighted in an object scatter)
    // It's a HIGHLIGHT, not a filter, so it never gates which data is reachable. The forward
    // (parent) lookup is slim id-only and runs at ANY selection size — no silent disable. The
    // reverse (children of selected images) can explode on select-all of an image grid (every
    // object of every image), so it keeps an explicit, generous fetch ceiling to avoid pulling
    // millions of rows for a cosmetic highlight; the underlying data is never capped.
    const REVERSE_HIGHLIGHT_MAX = 200_000;
    const { data: selectionCrossGrainIds } = useQuery({
        queryKey: ['selection-crossgrain', datasetId, selectedIds],
        queryFn: async () => {
            const partners = new Set<number>();
            // forward: read the selected rows' parent_id (set on object rows) — slim id+parent_id.
            const sel = await getSamples(
                datasetId!, selectedIds.length, 0,
                { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: selectedIds }] },
                undefined, undefined, true, true, 'metadata.parent_id',
            );
            for (const s of sel.items) {
                const pid = (s.metadata as Record<string, unknown> | undefined)?.parent_id;
                if (pid != null) partners.add(Number(pid));
            }
            // reverse: objects whose parent is one of the selected ids (no-op for an object
            // selection, since an object's parent is an image, never another object).
            const kids = await getSamples(
                datasetId!,
                REVERSE_HIGHLIGHT_MAX,
                0,
                {
                    logic: 'AND',
                    filters: [
                        { field: 'media_type', operator: '=', value: 'object' },
                        { field: 'parent_id', operator: 'in', value: selectedIds },
                    ],
                },
                undefined,
                undefined,
                true, // skip_count
                true, // slim
                'id', // ids only
            );
            for (const s of kids.items) partners.add(s.id);
            return [...partners];
        },
        enabled: !!datasetId && selectedIds.length > 0,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
    });
    // Object ids and image ids don't collide, so the union safely highlights objects in an
    // object scatter and their parents/children in an image/object scatter.
    const scatterHighlightIds = useMemo(
        () => [...new Set([...selectedIds, ...(selectionCrossGrainIds || [])])],
        [selectedIds, selectionCrossGrainIds],
    );

    const patchState = usePatchState({
        datasetId,
        onSearchStart: () => setIsSearching(true),
        onSearchEnd: () => setIsSearching(false),
        onSearchResults: (ids, description) => {
            showResultsAsView({
                type: 'similarity',
                description,
                filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: ids }] },
            });
        },
        onCloseModal: () => setSelectedSample(null),
        viewStages,
    });


    const { data: hashFieldsData } = useQuery({
        queryKey: ['hashFields', datasetId],
        queryFn: async () => {
            const resp = await fetch(`/api/datasets/${datasetId}/hashes/fields`);
            if (!resp.ok) return [];
            const data: Array<{ field_name: string; count: number }> = await resp.json();
            return data.map((f) => f.field_name);
        },
        enabled: !!datasetId,
        staleTime: 60_000,
        refetchOnWindowFocus: false,
    });

    // Auto-switch modes
    const handleSimpleFilterChange = (newFilters: Filters) => {
        setSimpleFilters(newFilters);
        setFilterMode('simple');
    };

    // Grain-aware sample click: clicking an OBJECT (a per-detection crop row) opens its
    // PARENT image in the modal — the object->image link. The parent's detections (incl.
    // this object's box) render via the normal overlay. Images open as themselves.
    const handleSampleClick = useCallback(async (sample: Sample, panelId: string) => {
        setModalSourcePanelId(panelId);
        setModalSourceId(sample.id);  // index prev/next by the clicked row, not the (maybe parent) shown sample
        const parentId = sample.media_type === 'object' ? sample.metadata?.parent_id : null;
        if (parentId != null) {
            try {
                const resp = await getSamples(datasetId!, 1, 0, {
                    logic: 'AND',
                    filters: [{ field: 'id', operator: 'in', value: [Number(parentId)] }],
                });
                if (resp.items?.[0]) {
                    setHighlightCuboidField(typeof sample.metadata?.cuboid_field === 'string' ? sample.metadata.cuboid_field : null);
                    setHighlightCuboidId(typeof sample.metadata?.cuboid_id === 'string' ? sample.metadata.cuboid_id : null);
                    const cb = sample.metadata?.crop_bbox;
                    setHighlightBox(Array.isArray(cb) && cb.length === 4 ? (cb as [number, number, number, number]) : null);
                    setSelectedSample(resp.items[0]);
                    return;
                }
            } catch {
                // fall through to showing the object crop itself
            }
        }
        setHighlightBox(null);
        setHighlightCuboidId(null);
        setHighlightCuboidField(null);
        setSelectedSample(sample);
    }, [datasetId]);

    // Image -> objects pivot (for the grid panel `panelId`): show every object whose
    // parent_id is one of the selected images. Switches THAT panel to object grain with the
    // parent_id filter in its pivot slot; clears the shared selection (it's grain-scoped).
    const handleShowObjectsOfSelected = useCallback((panelId: string) => {
        if (selectedIds.length === 0) return;
        const pivot: ComplexFilter = { field: 'parent_id', operator: 'in', value: [...selectedIds] };
        setPanelGrain(prev => ({ ...prev, [panelId]: { grain: 'object', pivot } }));
        clearSelection();
        setBrushedIds([]);
        setCurrentEmbeddingRegion(null);
    }, [selectedIds, clearSelection, setBrushedIds, setCurrentEmbeddingRegion]);

    // Object -> parent-images pivot (the mirror of Show Objects): flip `panelId` to image grain
    // showing exactly the selected objects' parent images. Image grain has no pivot slot
    // (grainPredicate ignores pivot off the object grain), so stage an id-IN view of the parents,
    // mirroring the find-unlabeled flip-to-grain flow. Look the parents up via a slim metadata fetch.
    const handleShowImagesOfSelected = useCallback(async (panelId: string) => {
        if (selectedIds.length === 0) return;
        const res = await getSamples(
            datasetId!, selectedIds.length, 0,
            { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: [...selectedIds] }] },
            undefined, undefined, true, true, 'metadata',
        );
        const parentIds = [...new Set(
            res.items.map(s => Number(s.metadata?.parent_id)).filter(n => Number.isFinite(n)),
        )];
        if (parentIds.length === 0) return;
        clearSelection();
        setBrushedIds([]);
        setCurrentEmbeddingRegion(null);
        setPanelGrain(prev => ({ ...prev, [panelId]: { grain: 'image', pivot: null } }));
        setViewStages([{
            type: 'filter',
            description: `Images of ${parentIds.length} object${parentIds.length === 1 ? '' : 's'}`,
            filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: parentIds }] },
        }]);
    }, [selectedIds, datasetId, clearSelection, setBrushedIds, setCurrentEmbeddingRegion, setViewStages]);

    // Find-unlabeled: from a labeled object, surface similar UNLABELED patch regions across
    // the dataset (object's clip_obj vector vs the patch space, IoU-excluding covered tiles).
    // Results are parent images, ranked, with the matched tile highlighted via the patch overlay;
    // the triggering panel flips to image grain to show them.
    const handleFindUnlabeled = useCallback(async (
        objectId: number,
        panelId: string,
        patchScale?: string,
        limit: number = 60,
    ) => {
        setIsSearching(true);
        try {
            const hits = await searchObjectToPatches({ datasetId: datasetId!, objectSampleId: objectId, patchScale, limit });
            if (!hits.length) {
                alert('No unlabeled look-alikes found. (Have patch embeddings been computed for this dataset?)');
                return;
            }
            // Show the matched parent images in image grain, ranked by similarity.
            clearSelection();
            setBrushedIds([]);
            setCurrentEmbeddingRegion(null);
            setPanelGrain(prev => ({ ...prev, [panelId]: { grain: 'image', pivot: null } }));
            const ids = hits.map(h => h.sample_id);
            showResultsAsView({
                type: 'similarity',
                description: `Unlabeled look-alikes (${ids.length})`,
                filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: ids }] },
            });
            // Highlight the matched tile per image (reuses the patch-overlay highlight path).
            const patchMap = new Map<number, Array<{ row: number; col: number; scale: string }>>();
            for (const h of hits) {
                if (h.patch_scale) patchMap.set(h.sample_id, [{ row: h.patch_row, col: h.patch_col, scale: h.patch_scale }]);
            }
            patchState.handleEmbeddingsPatchSelection(patchMap);
        } catch (e) {
            alert(`Find-unlabeled failed: ${e instanceof Error ? e.message : 'Unknown error'}`);
        } finally {
            setIsSearching(false);
        }
    }, [datasetId, clearSelection, setBrushedIds, setCurrentEmbeddingRegion, showResultsAsView, patchState]);

    // Calculate Map Display Filters (Global + Current Polygon)
    // Must be done at top level to avoid Rules of Hooks violation
    const mapDisplayFilters = useMemo(() => {
        const mapFilters = { ...imageGrainFilters };
        if (currentPolygon) {
            mapFilters.filters = [
                ...(mapFilters.filters || []),
                {
                    field: 'location',
                    operator: 'geo_polygon',
                    value: currentPolygon.geometry.coordinates[0]
                }
            ];
        }
        return mapFilters;
    }, [imageGrainFilters, currentPolygon]);

    // Rank order for the grid when a similarity / patch search is active: the search returned its
    // matches ranked, so render them in that order (not default id order). The rank is GRAIN-
    // specific. An OBJECT search ranks objects directly (primaryIds) and ranks their PARENT images
    // by each image's best (first / most-similar) object — parentRankIds, computed at search time
    // since objects have no clip_img of their own to rank an image by. An image/patch search ranks
    // images directly. rankForGrain picks the list that matches a given grid's grain.
    const similarityRank = useMemo(() => {
        const sim = viewStages.find(s => s.type === 'similarity');
        const inner = sim?.filter?.filters?.find((f) => f?.field === 'id' && f?.operator === 'in');
        const primaryIds = Array.isArray(inner?.value) && inner.value.length > 0 ? (inner.value as number[]) : undefined;
        const parentRankIds = Array.isArray(sim?.metadata?.parentRankIds) ? (sim.metadata.parentRankIds as number[]) : undefined;
        // parentBoxes (imageId -> [x,y,w,h]) lets the cross-grain IMAGE grid outline the matching
        // object on each parent. Materialized into a Map keyed by number (JSON keys are strings).
        const pb = sim?.metadata?.parentBoxes as Record<string, [number, number, number, number]> | undefined;
        const parentBoxMap = pb && typeof pb === 'object'
            ? new Map<number, [number, number, number, number]>(
                Object.entries(pb).map(([k, v]) => [Number(k), v]),
            )
            : undefined;
        return { primaryIds, parentRankIds, wasObjectSearch: !!parentRankIds, parentBoxMap };
    }, [viewStages]);

    // Returns the rank list for a grid of the given grain, plus byParent when the list holds IMAGE
    // ids but the grid shows OBJECT rows (the backend then ranks each object by its parent's rank).
    const rankForGrain = useCallback((grain: Grain): { ids?: number[]; byParent?: boolean } => {
        const { primaryIds, parentRankIds, wasObjectSearch } = similarityRank;
        if (grain === 'object') {
            // Object search ranks objects directly; an image/patch search ranks the cross-grain
            // object grid by each object's parent image (byParent).
            return wasObjectSearch ? { ids: primaryIds } : { ids: primaryIds, byParent: true };
        }
        // Image grid: an object search ranks parents by their best object; otherwise the image ids.
        return { ids: wasObjectSearch ? parentRankIds : primaryIds };
    }, [similarityRank]);

    // A Select All IS the result set of the view it was made in (an id list or a server token of
    // exactly those rows). When the grid's filters change it no longer means "all": the token
    // mode painted every tile of the new view as selected and the badge kept the old count, and
    // Tag / Export acted on rows the user could no longer see.
    // So a Select All is dropped when the staged view changes; hand-picked selections persist
    // across views. Keyed on the view, not on selectionSource — unticking one
    // tile after a Select All relabels the source 'Grid' but the set is still view-bound.
    const selectAllViewRef = useRef<string | null>(null);
    const stagedViewKey = useMemo(() => JSON.stringify(stagedFilters), [stagedFilters]);
    if (selectionNotice && selectionNotice.view !== stagedViewKey) setSelectionNotice(null);
    useEffect(() => {
        if (selectAllViewRef.current === null) return;
        if (selectionCount === 0) {
            selectAllViewRef.current = null;
        } else if (selectAllViewRef.current !== stagedViewKey) {
            selectAllViewRef.current = null;
            clearSelection();
        }
    }, [stagedViewKey, selectionCount, clearSelection]);

    // Select all samples matching current filters.
    // `filters` is the calling grid panel's grain-scoped staged filters, so Select All
    // selects exactly that grid's grain (e.g. its objects), not the grain-less superset.
    const selectAllVisible = async (filters: ComplexFilter) => {
        setSelectionNotice(null);
        try {
            // Small selections keep the explicit id list (so id-based ops — concepts,
            // similarity, duplicates — still work); large ones materialize a server-side
            // token so a million-sample Select-All stays weightless (no giant array in
            // state / on the wire / in history). SELECT_ALL_TOKEN_THRESHOLD mirrors the
            // 50k persistence cap: below it an id array is cheap everywhere.
            const total = (await retryDropped(() => getSamples(datasetId!, 1, 0, filters))).total;
            // The count may have changed since the button rendered. Never send a guest
            // to the write-only token route, even during that race.
            if (user?.role === 'guest' && total > SELECT_ALL_TOKEN_THRESHOLD) {
                setSelectionNotice({ view: stagedViewKey, message: GUEST_SELECT_ALL_MESSAGE });
                return;
            }
            setSelectAll(null);
            setSelectedIds([]);
            setDeselectedIds([]);
            if (!total || total <= 0) {
                setSelectionSource('Select All');
                return;
            }
            if (total <= SELECT_ALL_TOKEN_THRESHOLD) {
                const response = await retryDropped(() => getSamples(
                    datasetId!, total, 0, filters,
                    undefined, undefined,
                    true,  // skip_count
                    true,  // slim
                    'id',  // ids only
                ));
                setSelectedIds(response.items.map((s: Sample) => s.id));
            } else {
                const { token, count } = await retryDropped(() => createSelectionToken(datasetId!, filters));
                setSelectAll({ token, count });
            }
            selectAllViewRef.current = stagedViewKey;
            setSelectionSource('Select All');
        } catch (error) {
            console.error('Failed to select all:', error);
            alert(`Select All failed: ${(error as Error).message}`);
        }
    };

    // Handle embedding similarity search
    const handleSimilaritySearch = async (field: string, limit: number, useCurrentView: boolean) => {
        if (!similaritySearchSample || !datasetId) return;

        setIsSearching(true);
        try {
            // Grain-aware search field, keyed on the SEED, not a global grain: an object
            // only has the clip_obj embedding (not the image field), so similarity on an
            // object must search clip_obj — else the backend errors "No <field> embedding".
            const isObjectSeed = similaritySearchSample.media_type === 'object';
            const searchField = isObjectSeed ? 'clip_obj' : field;
            const filters = useCurrentView ? globalFilters : undefined;
            const results = await searchSimilar({
                datasetId,
                sampleId: similaritySeedIds ? undefined : similaritySearchSample.id,
                sampleIds: similaritySeedIds ?? undefined,
                embeddingField: searchField,
                limit,
                filters
            });

            // Stay in the seed's grain: an object seed returns similar OBJECTS (shown in the
            // object grid + clip_obj scatter, where the id filter keeps them visible — NOT
            // collapsed to parents, which blanked the object view). An image seed returns
            // similar images. Object->image is reachable via the per-object parent link.
            const resultIds = results.map(s => s.id);
            // Say how many came back vs. how many were asked for: a short result is then explained
            // on the chip instead of looking like a broken search.
            const got = (resultIds.length < limit ? `${resultIds.length} of ${limit} requested` : `${resultIds.length}`)
                + (results.incomplete ? ', more may match' : '');
            const description = isObjectSeed ? `Similar objects (${got})` : `Similarity Search (${got})`;

            const similarityFilter: ComplexFilter = {
                logic: 'AND',
                filters: [{
                    field: 'id',
                    operator: 'in',
                    value: resultIds
                }]
            };

            // For an OBJECT search, rank the PARENT images by each image's best (first / most-
            // similar) object, so the cross-grain image grid orders by similarity too. Dedupe
            // parent_ids preserving rank order: the image holding the top-ranked object comes first.
            // In the same pass, capture each parent's best object's crop_bbox so the image grid can
            // outline WHICH object drove the rank (parentBoxes: imageId -> [x,y,w,h] normalized).
            let parentRankIds: number[] | undefined;
            let parentBoxes: Record<number, [number, number, number, number]> | undefined;
            if (isObjectSeed) {
                const seen = new Set<number>();
                parentRankIds = [];
                parentBoxes = {};
                for (const s of results) {
                    const meta = s.metadata as Record<string, unknown> | undefined;
                    const pid = Number(meta?.parent_id);
                    if (Number.isFinite(pid) && !seen.has(pid)) {
                        seen.add(pid);
                        parentRankIds.push(pid);
                        const cb = meta?.crop_bbox;
                        if (Array.isArray(cb) && cb.length === 4) {
                            parentBoxes[pid] = cb as [number, number, number, number];
                        }
                    }
                }
            }

            // The results ARE the view: replaces applied stages, draft filters, brush, region,
            // map polygon and selection (see FilterContext.showResultsAsView).
            showResultsAsView({
                type: 'similarity',
                description,
                filter: similarityFilter,
                ...(parentRankIds ? { metadata: { parentRankIds, parentBoxes } } : {}),
            });

            console.log('Set similarity results:', description, resultIds.length);
        } catch (error) {
            console.error('Similarity search failed:', error);
            // Show user-friendly error message
            const errorMsg = error instanceof Error ? error.message : 'Unknown error';
            if (errorMsg.includes('embedding found')) {
                alert(`❌ No "${field}" embedding found for this sample.\n\nPlease select a different sample or ensure embeddings have been computed for this dataset.`);
            } else {
                alert(`❌ Similarity search failed: ${errorMsg}`);
            }
        } finally {
            setIsSearching(false);
        }
    };


    // A text or image search stays within the applied filters (an earlier search excepted: a new one replaces it),
    // or searches the whole dataset and replaces them (showResultsAsView).
    const searchKept = (scope: SearchScope) => (scope === 'filtered' ? viewStages.filter(s => s.type !== 'similarity') : []);
    const searchFilters = (kept: ViewStage[]) => (kept.length
        ? withGrain({ logic: 'AND', filters: kept.map(s => s.filter) }, 'image', null, hasObjects)
        : undefined);
    // The search's chip. Its label, the count it asked for and the count it found are kept, so the page's latest
    // search can offer "top N" on its chip.
    const searchStage = (label: string, kept: ViewStage[], resultIds: number[], limit: number, searchId: string, incomplete = false): ViewStage => {
        const scoped = kept.length ? `${label} · within filters` : label;
        const got = (resultIds.length < limit ? `${resultIds.length} of ${limit} requested` : `${resultIds.length}`)
            + (incomplete ? ', more may match' : '');
        return {
            type: 'similarity',
            description: `${scoped} · top ${got}`,
            filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: resultIds }] },
            metadata: { search: { id: searchId, label: scoped, found: resultIds.length, limit, incomplete } },
        };
    };
    // The results land on the stages as they are when they arrive, so a chip added or removed while the search ran
    // stays that way. A re-run (the chip's "top N") replaces only its own chip, whatever its scope.
    const showSearchResults = (stage: ViewStage, scope: SearchScope, rerun: boolean) => {
        const searchId = stage.metadata?.search?.id;
        if (rerun) {
            setViewStages(current => {
                const at = current.findIndex(s => s.metadata?.search?.id === searchId);
                return at < 0 ? [...current, stage] : current.map((s, i) => (i === at ? stage : s));
            });
            return;
        }
        if (scope === 'all') {
            showResultsAsView(stage);
            return;
        }
        // As showResultsAsView, but the filters it searched within stay.
        setViewStages(current => [...current.filter(s => s.type !== 'similarity'), stage]);
        setSimpleFilters({});
        setAdvancedFilters({ logic: 'AND', filters: [] });
        setBrushedIds([]);
        setCurrentPolygon(null);
        setCurrentEmbeddingRegion(null);
        clearSelection();
    };

    // Natural-language search: the server CLIP-text-encodes the prompt and returns the
    // closest samples; shown as a similarity stage (same ranked-grid path as Find Similar).
    const handleTextSearch = async (prompt: string, limit: number, scope: SearchScope, searchId: string, rerun = false) => {
        if (!datasetId) return;
        const kept = searchKept(scope);
        setIsSearching(true);
        // Sequence guard: a slow earlier query must not clobber a newer one's results.
        const seq = ++textSearchSeq.current;
        try {
            const field = (embeddingFields || []).includes('clip')
                ? 'clip'
                : (embeddingFields || []).find(f => !f.includes('_patch_') && f !== 'clip_obj') || (embeddingFields || [])[0];
            const results = await searchSimilar({ datasetId, text: prompt, embeddingField: field, limit, filters: searchFilters(kept) });
            if (seq !== textSearchSeq.current) return;
            const resultIds = results.map(s => s.id);
            showSearchResults(searchStage(`Similar to “${prompt}”`, kept, resultIds, limit, searchId, results.incomplete), scope, rerun);
        } catch (error) {
            // The server explains 501 (encoder not installed) / 400 (non-CLIP field) precisely.
            alert(`❌ Text search failed: ${error instanceof Error ? error.message : error}`);
        } finally {
            setIsSearching(false);
        }
    };

    // Reverse image search: rank the grid by CLIP similarity to an uploaded image or an
    // image URL. Mirrors handleTextSearch (same CLIP field pick, sequence guard, and
    // 'similarity' view stage), differing only in the query source.
    const handleImageSearch = async (input: ImageSearchInput, limit: number, scope: SearchScope, searchId: string, rerun = false) => {
        if (!datasetId) return;
        const kept = searchKept(scope);
        setIsSearching(true);
        const seq = ++textSearchSeq.current;
        try {
            const field = (embeddingFields || []).includes('clip')
                ? 'clip'
                : (embeddingFields || []).find(f => !f.includes('_patch_') && f !== 'clip_obj') || (embeddingFields || [])[0];
            const results = await searchByImage({
                datasetId,
                imageBase64: input.imageBase64,
                imageUrl: input.imageUrl,
                embeddingField: field,
                limit,
                filters: searchFilters(kept),
            });
            if (seq !== textSearchSeq.current) return;
            const resultIds = results.map(s => s.id);
            const shortLabel = input.label.length > 40 ? `${input.label.slice(0, 37)}…` : input.label;
            showSearchResults(searchStage(`Similar to 🖼 ${shortLabel}`, kept, resultIds, limit, searchId, results.incomplete), scope, rerun);
        } catch (error) {
            // The server explains 501 (encoder not installed) / 400 (non-CLIP field / bad image) precisely.
            alert(`❌ Image search failed: ${error instanceof Error ? error.message : error}`);
        } finally {
            setIsSearching(false);
        }
    };

    // Show exact (hash) duplicates of the selected sample
    const handleShowDuplicates = async (mode: string) => {
        if (!datasetId || selectedIds.length < 1) return;
        setIsSearching(true);
        try {
            const { sample_ids } = await sampleDuplicates(datasetId, selectedIds[0], mode);
            // The backend returns just the seed itself when there are no duplicates.
            if (!sample_ids || sample_ids.length <= 1) {
                alert('No exact duplicates found for this image.');
                return;
            }
            // "Show duplicates" is a fresh selection — clear any active embeddings lasso /
            // map polygon first, or the brushed id-filter is suppressed and nothing changes.
            setCurrentEmbeddingRegion(null);
            setCurrentPolygon(null);
            setSelectionSource('Duplicates');
            setBrushedIds(sample_ids);
        } catch (error) {
            console.error('Show duplicates failed:', error);
            alert(`❌ Show duplicates failed: ${error instanceof Error ? error.message : 'Unknown error'}`);
        } finally {
            setIsSearching(false);
        }
    };


    // Panel Renderer
    const renderPanel = (type: PanelType, panelId: string) => {
        const g = getGrain(panelId);  // this panel's grain (image|object) + optional pivot
        const pivotCount = Array.isArray(g.pivot?.value) ? (g.pivot.value as unknown[]).length : 0;
        const rank = rankForGrain(g.grain);  // similarity rank list for this grid's grain
        // On an object→image ranked grid, outline the matching object's box on each parent image.
        const highlightBoxes = g.grain === 'image' ? similarityRank.parentBoxMap : undefined;
        switch (type) {
            case 'grid':
                return (
                    <GridPanel
                        datasetId={datasetId!}
                        dataset={dataset}
                        onMediaFieldChange={saveGridMediaField}
                        filters={withGrain(stagedFilters, g.grain, g.pivot, hasObjects)}
                        filtersReady={grainKnown || !stagedFilters.filters?.length}
                        grainMode={g.grain}
                        onGrainModeChange={hasObjects ? (mode) => {
                            // Per-panel grain switch. Only the grid's own checkbox selection is
                            // grain-scoped (it highlights cells by id, meaningless across grains),
                            // so clear that. The embeddings region and brush selection are
                            // server-side filters that cross-link via withGrain (a clip_img region
                            // resolves to those images' objects on the object grid), so they PERSIST.
                            setPanelGrain(prev => ({ ...prev, [panelId]: { grain: mode, pivot: null } }));
                            clearSelection();
                        } : undefined}
                        onShowObjects={hasObjects ? () => handleShowObjectsOfSelected(panelId) : undefined}
                        onShowImages={hasObjects ? () => handleShowImagesOfSelected(panelId) : undefined}
                        onFindUnlabeledClick={selectedIds.length === 1 ? () => setFindUnlabeled({ objectId: selectedIds[0], panelId }) : undefined}
                        pivotImageCount={pivotCount}
                        onClearPivot={() => setPanelGrain(prev => ({ ...prev, [panelId]: { grain: 'object', pivot: null } }))}
                        rankOrderIds={rank.ids}
                        rankByParent={rank.byParent}
                        highlightBoxes={highlightBoxes}
                        schema={schema || []}
                        selectedIds={selectedIds}
                        selectAllActive={!!selectAll}
                        deselectedIds={deselectedIds}
                        selectionCount={selectionCount}
                        activeOverlayFields={activeOverlayFields}
                        overlayConfigs={overlayConfigs}
                        onSampleClick={(s) => handleSampleClick(s, panelId)}
                        onToggleSelection={(sid) => {
                            setSelectionSource('Grid');
                            toggleSampleSelected(sid);
                        }}
                        onRangeSelection={(ids, select = true) => {
                            setSelectionSource('Grid');
                            if (selectAll) {
                                // In select-all mode a range toggles EXCEPTIONS: "deselect" adds to
                                // the exception set, "select" removes from it.
                                const s = new Set(ids);
                                setDeselectedIds(select
                                    ? deselectedIds.filter(id => !s.has(id))
                                    : [...new Set([...deselectedIds, ...ids])]);
                            } else if (select) {
                                setSelectedIds([...new Set([...selectedIds, ...ids])]);
                            } else {
                                const remove = new Set(ids);
                                setSelectedIds(selectedIds.filter(sid => !remove.has(sid)));
                            }
                        }}
                        onClearSelection={clearSelection}
                        onSelectAll={() => selectAllVisible(withGrain(stagedFilters, g.grain, g.pivot, hasObjects))}
                        guest={user?.role === 'guest'}
                        onTagClick={() => setShowTagModal(true)}
                        onExportClick={() => setShowExportDialog(true)}
                        // Always: with nothing filtered or selected it exports the whole dataset (the
                        // button only appeared once a filter, draft or selection existed).
                        canExport
                        onFindSimilarClick={(embeddingFields && embeddingFields.length > 0) ? () => {
                            // Seed = the selected row from THIS panel's own sample list. Fall back
                            // to a minimal seed carrying the panel's grain (media_type) when the
                            // selection isn't on the loaded page — all the search needs is the id +
                            // grain (object seed -> clip_obj).
                            const found = getGridSamplesRef(panelId).current.find(s => s.id === selectedIds[0]);
                            setSimilaritySeedIds(selectedIds.length > 1 ? [...selectedIds] : null);
                            setSimilaritySearchSample(found ?? ({ id: selectedIds[0], media_type: g.grain } as unknown as Sample));
                        } : undefined}
                        hashFields={hashFieldsData || []}
                        onShowDuplicates={handleShowDuplicates}
                        // A mirror's tags are the primary's; with tags both ways, a tag edit here is made there.
                        canTag={(user?.role === 'admin' || user?.role === 'power_user')
                            && (!dataset?.mirror || (dataset.mirror.tags === 'both' && dataset.mirror.state === 'following'))}
                        gridSamplesRef={getGridSamplesRef(panelId)}
                        gridMoreRef={getGridMoreRef(panelId)}
                        // Patches are a sub-grid of IMAGES only. An object-grain grid never shows
                        // them — even though gridPatchMode is shared state, so selecting a scale on
                        // one (image) grid doesn't leak overlays onto an object grid.
                        selectedPatches={g.grain === 'object' ? undefined : patchState.selectedPatches}
                        onPatchSelect={g.grain === 'object' ? undefined : patchState.handlePatchGridSelect}
                        availablePatchScales={g.grain === 'object' ? [] : patchState.availablePatchScales}
                        patchScaleLabels={patchState.patchScaleLabels}
                        gridPatchMode={g.grain === 'object' ? null : patchState.gridPatchMode}
                        onGridPatchModeChange={patchState.setGridPatchMode}
                        onClearPatches={patchState.clearSelectedPatches}
                    />
                );
            case 'filters':
                return (
                    <div className="h-full w-full overflow-y-auto bg-zinc-900">
                        {/* Mounted once this visit's dataset answer is in (not a cached one): whether its
                            sections open expanded is read then, so no section moves afterwards. */}
                        {(datasetFetchedOnOpen || datasetError) && <FilterSidebar
                            key={datasetId}
                            expandAll={dataset?.sidebar_expand_all ?? false}
                            datasetId={datasetId!}
                            schema={schema || []}
                            isLoading={schemaLoading}
                            filters={simpleFilters}
                            globalFilters={imageGrainFilters}
                            onChange={handleSimpleFilterChange}
                            activeOverlayFields={activeOverlayFields}
                            onToggleOverlay={(field) => {
                                setActiveOverlayFields(prev =>
                                    toggleOverlayField(prev, field, schema)
                                );
                            }}
                            onSchemaUpdate={() => {
                                queryClient.invalidateQueries({ queryKey: ['schema', datasetId] });
                            }}
                            lightningMode={lightningMode}
                            filterActive={(globalFilters.filters?.length ?? 0) > 0}
                            appliedFilters={globalFilters}
                            valuesUpdating={schemaIsPrevious}
                        />}
                    </div>
                );
            case 'embeddings':
                return (
                    <div className="h-full w-full overflow-hidden bg-zinc-900">
                        <EmbeddingsPanel
                            datasetId={datasetId!}
                            schema={schema || []}
                            selectedIds={brushedIds}
                            highlightedIds={scatterHighlightIds}
                            // userFilters (grain-less): the panel grain-wraps internally by its
                            // selected embedding field (clip_img/clip_patch -> image, clip_obj ->
                            // object), so a cross-grain filter still populates each scatter from its
                            // partners (e.g. find-similar objects shows their parent images in the
                            // clip_img scatter). hasObjects gates the cross-grain link on.
                            filters={userFilters}
                            hasObjects={hasObjects}
                            onSelection={(ids, region) => {
                                setSelectionSource('Embeddings');
                                if (region) {
                                    // Lasso: the region token is the FULL server-side
                                    // selection filter (incl. off-screen points). Keep the
                                    // visible ids as brushedIds so the scatter stays
                                    // HIGHLIGHTED and the panel's clear button shows — the
                                    // filter builder uses the token (not brushedIds) while a
                                    // region is active, so this does NOT narrow the selection.
                                    setCurrentEmbeddingRegion(region);
                                    setBrushedIds(ids);
                                } else if (region === null) {
                                    // Cleared lasso.
                                    setCurrentEmbeddingRegion(null);
                                    setBrushedIds([]);
                                } else {
                                    // Immediate visible-point highlight (bounded).
                                    setBrushedIds(ids);
                                }
                            }}
                            patchHighlightKeys={patchState.embeddingHighlightKeys}
                            onPatchSelection={patchState.handleEmbeddingsPatchSelection}
                        />
                    </div>
                );
            case 'map':
                return (
                    <div className="h-full w-full relative bg-zinc-900">
                        {currentPolygon && (
                            <div className="absolute top-3 left-3 z-10">
                                <button
                                    onClick={() => setCurrentPolygon(null)}
                                    className="bg-purple-500/20 hover:bg-purple-500/30 text-purple-200 border border-purple-500/50 text-xs px-2 py-1 rounded flex items-center gap-2 transition-colors"
                                    title="Clear polygon selection"
                                >
                                    <span>Polygon selected</span>
                                    <span className="text-xs">✕</span>
                                </button>
                            </div>
                        )}
                        <MapPanel
                            datasetId={datasetId!}
                            filters={mapDisplayFilters}
                            selectedIds={selectedIds}
                            initialPolygon={currentPolygon}
                            onSelection={(ids, polygon) => {
                                setSelectionSource('Map');
                                // MapPanel's lasso only ever emits a Polygon feature.
                                setCurrentPolygon((polygon as Feature<Polygon> | null | undefined) ?? null);
                                // Only auto-select individual IDs if under 10K to prevent browser crash
                                // The polygon filter already scopes the grid regardless
                                if (ids.length > 0 && ids.length <= 10000) {
                                    setSelectedIds(ids);
                                }
                            }}
                        />
                    </div>

                );
            case 'histogram':
                return (
                    <div className="h-full w-full overflow-hidden bg-zinc-900">
                        <HistogramPanel
                            datasetId={datasetId!}
                            schema={schema || []}
                            globalFilters={imageGrainFilters}
                            activeOverlayFields={activeOverlayFields}
                            onToggleOverlay={(field) => {
                                setActiveOverlayFields(prev =>
                                    toggleOverlayField(prev, field, schema)
                                );
                            }}
                        />
                    </div>
                );
            case 'duplicates':
                return (
                    <div className="h-full w-full overflow-hidden bg-zinc-900">
                        <DuplicatesPanel
                            datasetId={datasetId!}
                            hasObjects={hasObjects}
                            selectedIds={selectedIds}
                            onSelection={(ids) => {
                                setSelectionSource('Duplicates');
                                // A lasso still held would keep filtering and these ids would be ignored.
                                setCurrentEmbeddingRegion(null);
                                setBrushedIds(ids);
                            }}
                            onSampleClick={(s) => { setModalSourcePanelId(null); setSelectedSample(s); }}
                            onToggleSelection={(id) => {
                                setSelectionSource('Duplicates');
                                setSelectedIds(
                                    selectedIds.includes(id)
                                        ? selectedIds.filter(sid => sid !== id)
                                        : [...selectedIds, id]
                                );
                            }}
                        />
                    </div>
                );
            case 'concepts':
                return (
                    <div className="h-full w-full overflow-hidden bg-zinc-900">
                        <ConceptsPanel
                            datasetId={datasetId!}
                            onSampleClick={(s) => { setModalSourcePanelId(null); setSelectedSample(s); }}
                        />
                    </div>
                );
            case 'sparse-sample':
                return (
                    <div className="h-full w-full overflow-hidden bg-zinc-900">
                        <SparseSamplePanel
                            datasetId={datasetId!}
                            filters={imageGrainFilters}
                            onSelection={(ids) => {
                                setSelectionSource('Sparse Sample');
                                // A lasso still held would keep filtering and these ids would be ignored.
                                setCurrentEmbeddingRegion(null);
                                setBrushedIds(ids);
                            }}
                        />
                    </div>
                );
            default:
                return (
                    <div className="flex items-center justify-center h-full text-zinc-500 text-sm">
                        Select a panel type from the dropdown above
                    </div>
                );
        }
    };

    // Find similar: a search remembers what it searched for, so its chip's "top N" can run it again.
    const searchable = (embeddingFields || []).some(f => !f.includes('_patch_') && f !== 'clip_obj');
    const newSearchId = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const runTextSearch = (prompt: string) => {
        const search = { id: newSearchId(), scope: searchScope, text: prompt };
        setLastSearch(search);
        void handleTextSearch(prompt, searchLimitOf(searchLimit), search.scope, search.id);
    };
    const runImageSearch = (input: ImageSearchInput) => {
        const search = { id: newSearchId(), scope: searchScope, image: input };
        setLastSearch(search);
        void handleImageSearch(input, searchLimitOf(searchLimit), search.scope, search.id);
    };
    const changeSearchLimit = (limit: number) => {
        setSearchLimit(limit);
        if (lastSearch?.text != null) void handleTextSearch(lastSearch.text, limit, lastSearch.scope, lastSearch.id, true);
        else if (lastSearch?.image) void handleImageSearch(lastSearch.image, limit, lastSearch.scope, lastSearch.id, true);
    };
    const hasDraft = hasUnappliedChanges || brushedIds.length > 0 || selectedIds.length > 0 || !!currentPolygon;

    if (!isMounted) return null;

    // No access (403) or no such dataset (404): one clear page, instead of every panel repeating
    // "Couldn't load data: Access denied for this dataset".
    if (datasetError instanceof ApiError && (datasetError.status === 403 || datasetError.status === 404)) {
        return (
            <div className="h-screen flex flex-col bg-zinc-950">
                <Header />
                <div data-testid="dataset-unavailable" className="flex-1 flex flex-col items-center justify-center gap-3 text-center">
                    <p className="text-zinc-300">
                        {datasetError.status === 403 ? "You don't have access to this dataset." : "This dataset doesn't exist."}
                    </p>
                    <Link href="/" className="text-sm text-cyan-400 hover:text-cyan-300">← Back to datasets</Link>
                </div>
            </div>
        );
    }

    return (
        <div className="h-screen flex flex-col bg-zinc-950">
            <Header />
            {dataset && datasetId != null && <DatasetJobs key={datasetId} datasetId={datasetId} onShowHistory={() => setIsJobHistoryOpen(true)} />}
            {dataset?.deleting && <DatasetDeletion dataset={dataset} />}

            {/* Sub-header: the dataset, its draft (not yet applied) filters, and its tools */}
            <div className="border-b border-zinc-800 bg-zinc-900/50 shrink-0">
                <div className="px-6 py-2.5 flex items-center gap-4">
                    <Link href="/" className="text-zinc-400 hover:text-white transition-colors flex items-center gap-1 text-sm font-medium shrink-0">
                        ← Back
                    </Link>
                    <div className="h-4 w-px bg-zinc-800" />
                    <div className="flex-1 min-w-[10rem]">
                        <h1 className="text-lg font-bold text-white truncate flex items-center gap-2" title={dataset?.name}>
                            <span className="truncate">{dataset?.name || `Dataset ${datasetId}`}</span>
                            {dataset?.mirror && <MirrorBadge mirror={dataset.mirror} />}
                        </h1>
                        <DatasetDescription description={dataset?.description} />
                        {selectionNotice?.view === stagedViewKey && <p role="status" className="text-xs text-zinc-400">{selectionNotice.message}</p>}
                    </div>

                    {/* Draft (not yet applied) filters: one chip each, each clearable (✕), then Clear Draft / Apply. */}
                    {hasDraft && (
                        <div data-testid="draft-filters" className="flex flex-wrap items-center justify-end gap-2 text-xs">
                            <span className="text-[11px] text-zinc-500">Not applied:</span>
                            {/* Embeddings lasso = an AREA. Always shown as a "region" chip
                                (never individual points), whether or not the server token
                                has resolved yet. */}
                            {(currentEmbeddingRegion || (selectionSource === 'Embeddings' && brushedIds.length > 0)) && (
                                <DraftChip
                                    tone="indigo"
                                    label="Embeddings region"
                                    onClear={() => { setCurrentEmbeddingRegion(null); setBrushedIds([]); }}
                                />
                            )}
                            {/* Brush/point selection from other panels (map / duplicates / sparse) */}
                            {brushedIds.length > 0 && selectionSource !== 'Embeddings' && !currentEmbeddingRegion && (
                                <DraftChip
                                    tone={selectionSource === 'Map' ? 'purple' : 'indigo'}
                                    label={`${selectionSource && selectionSource !== 'Manual' ? selectionSource : 'Selection'} (${brushedIds.length.toLocaleString()} selected)`}
                                    onClear={() => setBrushedIds([])}
                                />
                            )}
                            {/* Grid checkbox selection */}
                            {selectedIds.length > 0 && (
                                <DraftChip
                                    tone="cyan"
                                    label={`Grid (${selectedIds.length.toLocaleString()} selected)`}
                                    onClear={clearSelection}
                                />
                            )}
                            {/* Token Select-All (large selection held server-side; selectedIds is empty) */}
                            {selectAll && (
                                <DraftChip
                                    tone="cyan"
                                    label={`Select All (${selectionCount.toLocaleString()} selected)`}
                                    onClear={clearSelection}
                                />
                            )}
                            {/* Map polygon region */}
                            {currentPolygon && (
                                <DraftChip tone="purple" label="Map region" onClear={() => setCurrentPolygon(null)} />
                            )}
                            {/* The sidebar's picks, one chip per field, so each says what it is and can go alone */}
                            {Object.entries(simpleFilters).map(([key, value]) => {
                                // One filter comes back as itself; nothing as an empty AND group.
                                const filter = convertSimpleFiltersToComplex({ [key]: value });
                                if (filter.filters && filter.filters.length === 0) return null;
                                return (
                                    <DraftChip
                                        key={key}
                                        tone="zinc"
                                        label={describeFilter(filter)}
                                        onClear={() => setSimpleFilters(Object.fromEntries(Object.entries(simpleFilters).filter(([other]) => other !== key)))}
                                    />
                                );
                            })}
                            {(advancedFilters.filters?.length ?? 0) > 0 && (
                                <DraftChip tone="zinc" label="Advanced filters" onClear={() => setAdvancedFilters({ logic: 'AND', filters: [] })} />
                            )}
                            <button
                                onClick={() => {
                                    setSimpleFilters({});
                                    setAdvancedFilters({ logic: 'AND', filters: [] });
                                    setBrushedIds([]);
                                    setCurrentPolygon(null);
                                    setCurrentEmbeddingRegion(null);
                                    clearSelection();
                                }}
                                className="px-3 py-1.5 text-xs font-medium text-zinc-400 hover:text-white bg-zinc-800 hover:bg-zinc-700 rounded transition-colors flex items-center gap-1"
                            >
                                <Cross2Icon className="w-3 h-3" />
                                Clear Draft
                            </button>
                            {hasUnappliedChanges && (
                                <button
                                    onClick={applyFilters}
                                    data-testid="apply-filters"
                                    className="px-3 py-1.5 text-xs font-medium text-white bg-cyan-600 hover:bg-cyan-500 rounded transition-colors flex items-center gap-1"
                                >
                                    <CheckIcon className="w-3 h-3" />
                                    Apply
                                </button>
                            )}
                        </div>
                    )}

                    <div className="flex shrink-0 items-center gap-2">
                        {datasetId && (
                            <ViewManager
                                datasetId={datasetId}
                                currentFilters={globalFilters} // View content filters (legacy/compiled)
                                currentViewStages={viewStages} // New: Stages history
                                defaultViewId={dataset?.default_view_id ?? null}
                                mirror={dataset?.mirror ?? null}
                                onDefaultViewChanged={() => queryClient.invalidateQueries({ queryKey: ['dataset', datasetId] })}
                                onLoadView={(view) => {
                                    if (view.content.viewStages) {
                                        setViewStages(view.content.viewStages);
                                    }
                                }}
                            />
                        )}
                        <button
                            onClick={() => setIsJobHistoryOpen(true)}
                            data-testid="open-job-history"
                            className="px-3 py-1.5 text-xs font-medium text-zinc-400 hover:text-white bg-zinc-800 hover:bg-zinc-700 rounded transition-colors"
                            title="Job history"
                        >
                            Jobs
                        </button>
                        <button
                            onClick={() => setIsSettingsOpen(true)}
                            className="p-2 text-zinc-400 hover:text-white hover:bg-zinc-800 rounded-full transition-colors"
                            title="Dataset Settings"
                        >
                            <GearIcon className="w-5 h-5" />
                        </button>
                        <button
                            onClick={() => setLightningMode(prev => !prev)}
                            className={`p-1.5 rounded-md transition-colors ${lightningMode
                                ? 'text-yellow-400 bg-yellow-400/10 hover:bg-yellow-400/20'
                                : 'text-zinc-600 hover:bg-zinc-900 hover:text-zinc-300'
                            }`}
                            title={`Fast mode: ${lightningMode ? 'ON' : 'OFF'}`}
                        >
                            <Rocket className="w-4 h-4" />
                        </button>
                    </div>
                </div>

                {/* Filter bar: Add Filter and Find similar, the applied filters as chips, and what they come to */}
                <div className="px-6 pb-3 pt-0">
                    <QueryBuilderBar
                        stages={viewStages}
                        onRemoveStage={removeStage}
                        onUpdateStage={updateStage}
                        onAddStage={addStage}
                        onClearAll={resetFilters}
                        defaultView={defaultView}
                        onShowAll={showAllImages}
                        datasetId={datasetId!}
                        schema={schema || []}
                        actions={
                            <FindSimilar
                                onTextSearch={searchable ? runTextSearch : undefined}
                                onImageSearch={searchable ? runImageSearch : undefined}
                                isSearching={isSearching}
                                allowImageUrl={user?.role !== 'guest'}
                                limit={searchLimit}
                                onLimitChange={setSearchLimit}
                                scope={searchScope}
                                onScopeChange={setSearchScope}
                                hasFilters={viewStages.some(s => s.type !== 'similarity')}
                            />
                        }
                        trailing={
                            <div data-testid="total-samples" className="whitespace-nowrap px-2 text-sm text-zinc-300">
                                {/* While the COUNT runs this said "0 samples" — 13 s on main for one filter.
                                    On a dataset with object rows it counts the IMAGE grain, so
                                    say images: "2000 samples" sat next to "11,072 objects". */}
                                {appliedCount !== undefined
                                    ? `${appliedCount} ${hasObjects ? 'images' : 'samples'}`
                                    : isRescoringRefusal(globalCountFailure)
                                        ? 'Waiting for concept scoring…'
                                        : 'Counting…'}
                            </div>
                        }
                        activeSearchId={lastSearch?.id ?? null}
                        onSearchLimit={changeSearchLimit}
                    />
                </div>
            </div>

            <div className="flex-1 overflow-hidden bg-zinc-950">
                <PanelLayout
                    renderPanel={renderPanel}
                    getIsActive={(type) => {
                        if (type === 'filters' && filterMode === 'simple') return true;
                        return false;
                    }}
                />
            </div>



            {
                // eslint-disable-next-line react-hooks/refs -- modal navigation snapshots the independently subscribed grid cache
                selectedSample && (() => {
                    // Prev/next walks the sample list of the panel the modal was opened from,
                    // indexed by the CLICKED row (modalSourceId) — not selectedSample, which for
                    // an object grid is the parent IMAGE and isn't in the object-id list. Stepping
                    // re-runs handleSampleClick so the next row re-derives its shown sample/box.
                    const samples = modalSourcePanelId ? getGridSamplesRef(modalSourcePanelId).current : [];
                    const idx = samples.findIndex(s => s.id === modalSourceId);
                    // At the last LOADED row, Next fetches the grid's next page instead of dead-ending.
                    const more = modalSourcePanelId ? getGridMoreRef(modalSourcePanelId).current : null;
                    const atLoadedEnd = idx >= 0 && idx === samples.length - 1;
                    const canLoadNext = atLoadedEnd && !!more && more.hasMore();
                    // Select the row that was CLICKED: in object grain the modal shows the parent
                    // image, and toggling that id selected an image no object grid highlights
                    //. The context helpers also honour token Select-All.
                    const selectionTarget = modalSourceId ?? selectedSample.id;
                    const sampleIsSelected = isSampleSelected(selectionTarget);
                    return (
                        <SampleModal
                            sample={selectedSample}
                            activeMediaField={modalMediaField}
                            availableMediaFields={dataset?.media_fields || ['filepath']}
                            activeOverlayFields={activeOverlayFields}
                            overlayConfigs={overlayConfigs}
                            segmentationConfig={dataset?.segmentation_config}
                            // A mirror's settings, its default rules included, are its primary's: trying one is fine, saving isn't.
                            onSaveClassRule={datasetId != null && (user?.role === 'admin' || user?.role === 'power_user')
                                && !dataset?.mirror
                                ? async (fieldName, rule) => {
                                    await setClassRule(datasetId, fieldName, rule);
                                    await queryClient.invalidateQueries({ queryKey: ['dataset', datasetId] });
                                }
                                : undefined}
                            onMediaFieldChange={setModalMediaField}
                            highlightBox={highlightBox}
                            highlightCuboidField={highlightCuboidField}
                            highlightCuboidId={highlightCuboidId}
                            onClose={() => { setSelectedSample(null); setHighlightBox(null); setHighlightCuboidId(null); setTagSampleId(null); }}
                            hasPrevious={idx > 0}
                            hasNext={(idx >= 0 && idx < samples.length - 1) || canLoadNext}
                            onPrevious={idx > 0 ? () => { handleSampleClick(samples[idx - 1], modalSourcePanelId!); } : undefined}
                            onNext={idx >= 0 && idx < samples.length - 1
                                ? () => { handleSampleClick(samples[idx + 1], modalSourcePanelId!); }
                                : canLoadNext
                                    ? async () => {
                                        const longer = await more!.loadNextPage();
                                        const next = longer[longer.findIndex(s => s.id === modalSourceId) + 1];
                                        if (next) handleSampleClick(next, modalSourcePanelId!);
                                    }
                                    : undefined}
                            isSelected={sampleIsSelected}
                            onToggleSelection={() => {
                                setSelectionSource('Grid');
                                toggleSampleSelected(selectionTarget);
                            }}
                            onFindSimilar={embeddingFields && embeddingFields.length > 0 ? () => {
                                // Seed with the CLICKED row (an object in object grain), like Space does.
                                const seed = samples[idx] ?? selectedSample;
                                setSelectedSample(null);
                                setSimilaritySeedIds(null);
                                setSimilaritySearchSample(seed);
                            } : undefined}
                            onTag={() => setTagSampleId(selectedSample.id)}
                            onCloseOverlay={() => setTagSampleId(null)}
                            overlay={tagSampleId !== null && datasetId ? (
                                <TagManagementModal
                                    selectedSampleIds={[tagSampleId]}
                                    selectionCount={1}
                                    datasetId={datasetId}
                                    onClose={() => setTagSampleId(null)}
                                    onUpdate={async () => {
                                        notifySamplesMutated(datasetId);
                                        queryClient.invalidateQueries({ queryKey: ['schema', datasetId] });
                                        // The view shows a snapshot of the sample: re-read it so its Properties
                                        // list the new tags (the grid refreshes via notifySamplesMutated).
                                        const id = tagSampleId;
                                        const resp = await getSamples(datasetId, 1, 0, { field: 'id', operator: 'in', value: [id] });
                                        const fresh = resp.items?.[0];
                                        if (fresh) setSelectedSample(cur => (cur && cur.id === id ? fresh : cur));
                                    }}
                                />
                            ) : undefined}
                            availablePatchScales={patchState.availablePatchScales}
                            patchScaleLabels={patchState.patchScaleLabels}
                            onPatchSearch={patchState.handlePatchSearch}
                            patchScale={patchState.gridPatchMode}
                            onPatchScaleChange={patchState.setGridPatchMode}
                        />
                    );
                })()
            }

            {datasetId != null && (
                <DatasetJobHistoryDialog datasetId={datasetId} open={isJobHistoryOpen} onOpenChange={setIsJobHistoryOpen} />
            )}

            {
                isSettingsOpen && dataset && (
                    <DatasetSettingsModal
                        dataset={dataset}
                        onClose={() => setIsSettingsOpen(false)}
                        readOnly={user?.role !== 'admin'}
                        onViewSamples={(token, total, label) => {
                            // Apply the version-diff selection as a server-side stage
                            // (in_selection token) — no id list materialized client-side.
                            setSelectionSource(`Version: ${label}`);
                            setCurrentEmbeddingRegion(null);
                            setBrushedIds([]);
                            setViewStages([{
                                type: 'filter',
                                description: `Version: ${label}`,
                                filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in_selection', value: token }] },
                            }]);
                            setIsSettingsOpen(false);
                        }}
                    />
                )
            }

            {
                similaritySearchSample && embeddingFields && embeddingFields.length > 0 && (
                    <SimilaritySearchModal
                        sample={similaritySearchSample}
                        seedCount={similaritySeedIds?.length ?? 1}
                        embeddingFields={embeddingFields || []}
                        currentFilters={globalFilters}
                        currentViewCount={appliedCount}
                        hasDraftFilters={hasUnappliedChanges}
                        onSearch={handleSimilaritySearch}
                        onClose={() => setSimilaritySearchSample(null)}
                    />
                )
            }

            {
                findUnlabeled && (
                    <FindUnlabeledModal
                        availablePatchScales={patchState.availablePatchScales}
                        patchScaleLabels={patchState.patchScaleLabels}
                        onSearch={(patchScale, limit) =>
                            handleFindUnlabeled(findUnlabeled.objectId, findUnlabeled.panelId, patchScale, limit)}
                        onClose={() => setFindUnlabeled(null)}
                    />
                )
            }

            {
                showTagModal && datasetId && (
                    <TagManagementModal
                        selectedSampleIds={selectedIds}
                        selectionToken={selectAll?.token ?? null}
                        excludedIds={deselectedIds}
                        selectionCount={selectionCount}
                        datasetId={datasetId}
                        onClose={() => setShowTagModal(false)}
                        onUpdate={() => {
                            // The grid's page cache lives outside React Query — signal it
                            // directly so tag changes show without a manual filter change.
                            // (['samples']/['grid-samples'] were dead keys nothing used.)
                            notifySamplesMutated(datasetId);
                            queryClient.invalidateQueries({ queryKey: ['schema', datasetId] });
                        }}
                    />
                )
            }

            {showExportDialog && datasetId && (
                <ExportDialog
                    datasetId={datasetId}
                    schema={schema || []}
                    dataset={dataset}
                    appliedCount={appliedCount}
                    // Export applies the same IMAGE grain `appliedCount` is measured at, so the
                    // displayed count and the exported rows describe the same set.
                    hasObjects={hasObjects}
                    onClose={() => setShowExportDialog(false)}
                />
            )}
        </div >
    );
}
