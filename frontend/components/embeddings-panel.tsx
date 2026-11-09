'use client';

import { useEffect, useRef, useState, useMemo, useCallback } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getEmbeddings, getEmbeddingFields, reduceEmbeddings, selectEmbeddingRegion, lookupEmbeddingCoordinates, lookupPatchCoordinates, MAP_REFITTED_EVENT, type MapRefitted, type SchemaField, type ComplexFilter, type EmbeddingsResponse } from '@/lib/api';
import { crashedBefore, currentPageLoad, loadMarker } from '@/lib/load-marker';
import { useUser } from '@/contexts/UserContext';
import { withGrain } from '@/contexts/FilterContext';

interface Props {
    datasetId: number;
    schema: SchemaField[];
    // ids = visible points for immediate highlight; region = the FULL lasso as a
    // server-side filter (object to set, null to clear, omitted = highlight only).
    onSelection: (ids: number[], region?: { field: string; polygon: number[][]; token: string; total: number; modelId?: string | null } | null) => void;
    selectedIds: number[];
    filters?: ComplexFilter;
    hasObjects?: boolean;  // dataset has object rows -> cross-grain link applies to the scatter
    highlightedIds?: number[];
    patchHighlightKeys?: Set<string>;  // Set of "sampleId_fieldName" keys for patch search results
    onPatchSelection?: (patches: Map<number, Array<{ row: number; col: number; scale: string }>>) => void;
}

interface Point {
    sample_id: number;
    x: number;
    y: number;
    [key: string]: unknown;
}

// Stable empty-array reference so `embeddingsData?.points ?? EMPTY_POINTS` keeps the
// same identity across renders when there are no points (a fresh `[]` would change the
// identity of basePoints every render and re-fire the effects/memos that depend on it).
const EMPTY_POINTS: Point[] = [];

const PALETTE = [
    '#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231', '#911eb4', '#46f0f0', '#f032e6', '#bcf60c', '#fabebe',
    '#008080', '#e6beff', '#9a6324', '#fffac8', '#800000', '#aaffc3', '#808000', '#ffd8b1', '#000075', '#808080'
];

function finiteNumericValue(value: unknown): number | null {
    if (typeof value !== 'number' && (typeof value !== 'string' || !value.trim())) return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
}

export function EmbeddingsPanel({ datasetId, schema, onSelection, selectedIds, filters, hasObjects = false, highlightedIds = [], patchHighlightKeys, onPatchSelection }: Props) {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    // The lasso outline lives on its own canvas above the points, so drawing it never repaints them.
    const overlayRef = useRef<HTMLCanvasElement>(null);
    const containerRef = useRef<HTMLDivElement>(null);
    const { user } = useUser();

    const [colorField, setColorField] = useState<string>('');
    const [embeddingField, setEmbeddingField] = useState<string>('clip');

    // The scatter's grain IS its embedding field: clip_obj is object-grain, clip_img / clip_patch_*
    // are image-grain. When a filter is active, grain-wrap it so a cross-grain filter populates
    // this scatter from its partners — e.g. a find-similar-OBJECTS filter (object ids) still shows
    // the PARENT images in the clip_img scatter, instead of an empty plot. No active filter -> pass
    // the raw (empty) filter so the fast unfiltered embeddings path is kept.
    const scatterFilters = useMemo(() => {
        if (!filters || !(filters.filters && filters.filters.length > 0)) return filters;
        const grain = embeddingField === 'clip_obj' ? 'object' : 'image';
        return withGrain(filters, grain, null, hasObjects);
    }, [filters, embeddingField, hasObjects]);

    // Sample percentage persisted only after a successful render. A separate "attempt"
    // sentinel is written BEFORE each load and cleared on success (and on normal unmount);
    // if a fresh mount still sees it, the previous load crashed the tab (likely OOM) — so
    // recover with a small sample instead of re-loading the same amount and crashing again.
    const storageKey = `fridge:samplePct:${user?.id ?? 'anon'}:${datasetId}`;
    const attemptKey = `fridge:samplePctAttempt:${user?.id ?? 'anon'}:${datasetId}`;
    // Only a marker from another page load is a crash (lib/load-marker.ts): this mount's init runs before the
    // previous instance's cleanup, so a remount mid-load (a Find similar search) still sees that instance's marker.
    const [crashedWarning, setCrashedWarning] = useState<boolean>(
        () => typeof window !== 'undefined' && crashedBefore(sessionStorage.getItem(attemptKey), currentPageLoad()),
    );
    const [samplePct, setSamplePct] = useState<number>(() => {
        if (typeof window === 'undefined') return 30;
        if (crashedBefore(sessionStorage.getItem(attemptKey), currentPageLoad())) {
            sessionStorage.removeItem(attemptKey);  // last load didn't finish -> recover small
            return 5;
        }
        const stored = localStorage.getItem(storageKey);
        return stored ? Number(stored) : 30;
    });

    const [samplePickerOpen, setSamplePickerOpen] = useState(false);
    const samplePickerRef = useRef<HTMLDivElement>(null);

    // Transform state
    const [transform, setTransform] = useState({ k: 1, x: 0, y: 0 });
    const [isDragging, setIsDragging] = useState(false);
    const [lastPos, setLastPos] = useState({ x: 0, y: 0 });

    // Selection state
    const [isSelecting, setIsSelecting] = useState(false);
    const [selectionPolygon, setSelectionPolygon] = useState<{ x: number, y: number }[]>([]);

    // Convert prop to Set for O(1) lookup
    const selectedIdSet = useMemo(() => new Set(selectedIds), [selectedIds]);
    const highlightedIdSet = useMemo(() => new Set(highlightedIds), [highlightedIds]);

    // For patch embeddings: track which specific patches are selected (not just sample_ids)
    const [selectedPatchKeys, setSelectedPatchKeys] = useState<Set<string>>(new Set());
    const isPatchField = embeddingField.includes('_patch_');

    const { data: availableFields } = useQuery({
        queryKey: ['embedding-fields', datasetId],
        queryFn: () => getEmbeddingFields(datasetId),
        enabled: !!datasetId,
        staleTime: 15_000,
        refetchOnWindowFocus: false,
    });

    // "Run UMAP reduction" nudge (vectors exist but no coords yet — see the empty states).
    const queryClient = useQueryClient();
    const [reducing, setReducing] = useState(false);
    const [reduceError, setReduceError] = useState<string | null>(null);
    const runReduction = useCallback(async () => {
        setReducing(true);
        setReduceError(null);
        try {
            await reduceEmbeddings(datasetId, embeddingField);
            await queryClient.invalidateQueries({ queryKey: ['embeddings', datasetId] });
        } catch (e) {
            // The 413 detail names the SDK fallback (compute_visualization) — show it verbatim.
            setReduceError(e instanceof Error ? e.message : String(e));
        } finally {
            setReducing(false);
        }
    }, [datasetId, embeddingField, queryClient]);

    // Auto-select first field if current selection is invalid
    useEffect(() => {
        if (availableFields && availableFields.length > 0 && !availableFields.includes(embeddingField)) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
            setEmbeddingField(availableFields[0]);
        }
    }, [availableFields, embeddingField]);


    const { data: embeddingsData, isLoading, isFetching, isPlaceholderData, status, refetch } = useQuery({
        queryKey: ['embeddings', datasetId, embeddingField, scatterFilters, colorField, samplePct],
        queryFn: () => {
            // Mark this load in-progress; cleared on successful render / normal unmount. A
            // leftover marker on the next mount means the tab crashed mid-load (see init).
            try { sessionStorage.setItem(attemptKey, loadMarker(samplePct, currentPageLoad())); } catch { /* ignore */ }
            // patch_position is a synthetic field added by the backend for patch queries — don't request it as include_field
            const includeFields = colorField && colorField !== 'patch_position' ? [colorField] : undefined;
            return getEmbeddings(datasetId, embeddingField, scatterFilters, includeFields, samplePct);
        },
        // Gate on the field actually being one of availableFields: the default
        // embeddingField ('clip') may not exist for this dataset, and firing a query for
        // an invalid field wastes a request + flashes the empty state before the
        // auto-select effect switches to a real field.
        enabled: !!datasetId && !!availableFields && availableFields.includes(embeddingField),
        staleTime: 60_000,
        refetchOnWindowFocus: false,
        placeholderData: (previousData: EmbeddingsResponse | undefined) => previousData,
    });

    const basePoints = embeddingsData?.points ?? EMPTY_POINTS;
    const totalPoints = embeddingsData?.total || 0;
    const isSampled = embeddingsData?.sampled || false;
    const rankStale = embeddingsData?.rank_stale === true;
    const refittedAt = embeddingsData?.map_state?.refitted_at;
    const mapModel = embeddingsData?.map_state?.model_id;


    // Extra points fetched for IDs not in the base sample (e.g., grid selections)
    const [extraPoints, setExtraPoints] = useState<Point[]>([]);

    // Fetch coordinates for highlighted/selected IDs missing from base points
    useEffect(() => {
        if (!datasetId || !embeddingField) return;
        let cancelled = false;

        const baseIdSet = new Set(basePoints.map((p: Point) => p.sample_id));
        const missingIds = [
            ...highlightedIds.filter(id => !baseIdSet.has(id)),
            ...selectedIds.filter(id => !baseIdSet.has(id)),
        ];
        const uniqueMissing = [...new Set(missingIds)];

        if (uniqueMissing.length === 0) return;

        const lookupFields = colorField && colorField !== 'patch_position' ? [colorField] : undefined;
        lookupEmbeddingCoordinates(datasetId, embeddingField, uniqueMissing, lookupFields)
            .then(data => {
                if (cancelled) return;
                if (data.points.length > 0) {
                    setExtraPoints(prev => {
                        const existingIds = new Set(prev.map(p => p.sample_id));
                        const newPoints = data.points.filter(p => !existingIds.has(p.sample_id));
                        return newPoints.length > 0 ? [...prev, ...newPoints] : prev;
                    });
                }
            })
            .catch(() => {});

        return () => { cancelled = true; };
    }, [highlightedIds, selectedIds, basePoints, datasetId, embeddingField, colorField]);

    // Clear extra points and patch selection when embedding field changes
    // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
    useEffect(() => { setExtraPoints([]); setSelectedPatchKeys(new Set()); }, [embeddingField]);
    // A re-fitted map moves every point: drop extra points looked up on the previous map, and a lasso
    // drawn on it (its polygon would now select other samples; the server refuses it too).
    // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
    useEffect(() => { setExtraPoints([]); }, [mapModel]);
    // The map model each field was last shown with, so a re-fit is noticed even across a field switch.
    const shownModels = useRef<Record<string, string | null>>({});
    const lassoMap = useRef<{ field: string; model: string | null } | null>(null);  // the draft lasso this panel drew
    const [mapChangedNotice, setMapChangedNotice] = useState(false);
    // A cleared or applied lasso is no longer this panel's draft (an applied one is a view stage, which
    // FilterContext drops when the server refuses it): only a live draft is cleared below.
    useEffect(() => { if (selectedIds.length === 0) lassoMap.current = null; }, [selectedIds]);
    useEffect(() => {
        if (!embeddingsData || isPlaceholderData) return;  // placeholder data may belong to another field
        const model = mapModel ?? null;
        const seen = embeddingField in shownModels.current;
        const previous = shownModels.current[embeddingField];
        shownModels.current[embeddingField] = model;
        const lasso = lassoMap.current;
        if (seen && previous !== model && lasso && lasso.field === embeddingField && lasso.model !== model) {
            lassoMap.current = null;
            onSelection([], null);
            setMapChangedNotice(true);
        }
    }, [embeddingsData, isPlaceholderData, embeddingField, mapModel, onSelection]);
    // An applied lasso stage (or a saved view's) on a re-fitted map was refused and dropped: say so here,
    // where the selection is drawn again.
    useEffect(() => {
        const onRefitted = (event: Event) => {
            if ((event as CustomEvent<MapRefitted>).detail.datasetId === datasetId) setMapChangedNotice(true);
        };
        window.addEventListener(MAP_REFITTED_EVENT, onRefitted);
        return () => window.removeEventListener(MAP_REFITTED_EVENT, onRefitted);
    }, [datasetId]);

    // Fetch coordinates for selected/highlighted patches not in the base sample
    useEffect(() => {
        if (!isPatchField || !datasetId) return;
        const allKeys = new Set<string>();
        if (patchHighlightKeys) for (const k of patchHighlightKeys) allKeys.add(k);
        for (const k of selectedPatchKeys) allKeys.add(k);
        if (allKeys.size === 0) return;

        // Find keys not in base points
        const baseKeySet = new Set(basePoints.map((p: Point) => p.field_name ? `${p.sample_id}_${p.field_name}` : ''));
        const extraKeySet = new Set(extraPoints.map((p: Point) => p.field_name ? `${p.sample_id}_${p.field_name}` : ''));
        const missing: Array<{ sample_id: number; field_name: string }> = [];
        for (const key of allKeys) {
            if (baseKeySet.has(key) || extraKeySet.has(key)) continue;
            const sepIdx = key.indexOf('_');
            missing.push({ sample_id: parseInt(key.substring(0, sepIdx)), field_name: key.substring(sepIdx + 1) });
        }
        if (missing.length === 0) return;

        let cancelled = false;
        lookupPatchCoordinates(datasetId, missing)
            .then(data => {
                if (cancelled) return;
                if (data.points.length > 0) {
                    setExtraPoints(prev => {
                        const existingKeys = new Set(prev.map(p => p.field_name ? `${p.sample_id}_${p.field_name}` : ''));
                        const newPts = data.points.filter(p => !existingKeys.has(`${p.sample_id}_${p.field_name}`));
                        return newPts.length > 0 ? [...prev, ...newPts] : prev;
                    });
                }
            })
            .catch(() => {});
        return () => { cancelled = true; };
        // eslint-disable-next-line react-hooks/exhaustive-deps -- extraPoints is read only to skip already-fetched keys; the effect is triggered by the requested keys/field/dataset, and re-running on extraPoints (which this effect itself sets) would issue redundant lookups
    }, [isPatchField, datasetId, patchHighlightKeys, selectedPatchKeys, basePoints]);

    // Close sample picker on outside click
    useEffect(() => {
        if (!samplePickerOpen) return;
        const handler = (e: MouseEvent) => {
            if (samplePickerRef.current && !samplePickerRef.current.contains(e.target as Node)) {
                setSamplePickerOpen(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [samplePickerOpen]);

    // Merge base + extra points
    const points = useMemo(() => {
        if (extraPoints.length === 0) return basePoints;
        // For patches, dedup by (sample_id, field_name); for whole-image, by sample_id
        if (isPatchField) {
            const baseKeySet = new Set(basePoints.map((p: Point) => p.field_name ? `${p.sample_id}_${p.field_name}` : `${p.sample_id}`));
            const deduped = extraPoints.filter(p => {
                const key = p.field_name ? `${p.sample_id}_${p.field_name}` : `${p.sample_id}`;
                return !baseKeySet.has(key);
            });
            return [...basePoints, ...deduped];
        }
        const baseIdSet = new Set(basePoints.map((p: Point) => p.sample_id));
        const deduped = extraPoints.filter(p => !baseIdSet.has(p.sample_id));
        return [...basePoints, ...deduped];
    }, [basePoints, extraPoints, isPatchField]);

    // Compute extent from base points only (stable across extra point additions)
    const extent = useMemo(() => {
        if (basePoints.length === 0) return null;
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        for (const p of basePoints) {
            if (p.x < minX) minX = p.x;
            if (p.x > maxX) maxX = p.x;
            if (p.y < minY) minY = p.y;
            if (p.y > maxY) maxY = p.y;
        }
        return { minX, maxX, minY, maxY };
    }, [basePoints]);

    // Normalize all points using the base extent. Coords live in two parallel
    // Float32Arrays indexed against `points` (NOT a copied object array): at millions
    // of points the old `points.map(p => ({...p, nx, ny}))` spread cost ~240MB of new
    // objects on top of the base array; two Float32Arrays are ~8MB each. Read coords as
    // norm.nx[i]/norm.ny[i] and the rest of the point as points[i].
    const norm = useMemo(() => {
        if (!extent || points.length === 0) return { nx: new Float32Array(0), ny: new Float32Array(0) };
        const rangeX = extent.maxX - extent.minX || 1;
        const rangeY = extent.maxY - extent.minY || 1;
        const n = points.length;
        const nx = new Float32Array(n);
        const ny = new Float32Array(n);
        for (let i = 0; i < n; i++) {
            nx[i] = (points[i].x - extent.minX) / rangeX;
            ny[i] = (points[i].y - extent.minY) / rangeY;
        }
        return { nx, ny };
    }, [points, extent]);

    // Compute color stats and legend data. Keyed on the coloured field's type, not the whole schema:
    // the schema gets a new identity each time a sidebar field's stats arrive, and a new colorStats
    // repaints every point (also with no colour field chosen, and in the middle of a lasso drag).
    const colorFieldType = schema.find(f => f.name === colorField)?.type;
    const colorStats = useMemo(() => {
        if (!points || !colorField) return { min: 0, max: 1, hasRange: false, type: 'string' as const, uniqueValues: [] as string[] };

        const aliasedField = colorField.replace('.', '_');
        const values = points.map((p: Point) => p[colorField] ?? p[aliasedField]).filter((v: unknown) => v !== undefined && v !== null);
        const isNumber = colorFieldType === 'number';

        if (isNumber) {
            let min = Infinity, max = -Infinity;
            let hasRange = false;
            for (const value of values) {
                const numericValue = finiteNumericValue(value);
                if (numericValue === null) continue;
                hasRange = true;
                if (numericValue < min) min = numericValue;
                if (numericValue > max) max = numericValue;
            }
            return { min: hasRange ? min : 0, max: hasRange ? max : 0, hasRange, type: 'number' as const, uniqueValues: [] as string[] };
        }

        // Categorical — compute unique values for legend
        const uniqueSet = new Set<string>();
        values.forEach((v: unknown) => {
            if (Array.isArray(v)) {
                if (v.length > 0) uniqueSet.add(String(v[v.length - 1]));
            } else {
                uniqueSet.add(String(v));
            }
        });

        return {
            min: 0, max: 1, hasRange: false,
            type: 'string' as const,
            uniqueValues: Array.from(uniqueSet).sort(),
        };
    }, [points, colorField, colorFieldType]);

    // value -> position in the sorted unique list, as a Map so the lookup is O(1).
    //
    // This used to be `uniqueValues.indexOf(str)` inside getColorForValue — O(U) — and
    // renderLayers resolves a colour for EVERY point inside a memo whose deps include
    // selectedIdSet, so the whole O(N·U) pass was repaid on every selection change (each grid
    // checkbox tick). `uniqueValues` is built from the data with no cardinality bound and the
    // Color-by dropdown offers every string/enum/number field, so colouring samples by a
    // high-cardinality field (filename, source_id) approached O(N²) and froze the tab.
    //
    // Same ordering as before — index into the same sorted array — so every point keeps the exact
    // colour it had, and the legend still agrees with the points.
    const colorIndex = useMemo(
        () => new Map(colorStats.uniqueValues.map((v, i) => [v, i])),
        [colorStats],
    );

    // Helper to get color for a value (used by both points and legend)
    const getColorForValue = useCallback((val: unknown) => {
        if (val === undefined || val === null) return '#52525b';

        if (colorStats.type === 'string') {
            const str = String(val);
            // Use index in sorted unique values for deterministic, collision-free colors
            const idx = colorIndex.get(str);
            // `!== undefined`, not a truthiness check — index 0 is a valid palette slot.
            if (idx !== undefined) return PALETTE[idx % PALETTE.length];
            // Fallback to hash for values not in the unique set
            let hash = 0;
            for (let i = 0; i < str.length; i++) {
                hash = str.charCodeAt(i) + ((hash << 5) - hash);
            }
            return PALETTE[Math.abs(hash) % PALETTE.length];
        }

        if (colorStats.type === 'number') {
            const num = finiteNumericValue(val);
            if (num === null || !colorStats.hasRange) return '#52525b';
            const range = colorStats.max - colorStats.min || 1;
            const t = Math.max(0, Math.min(1, (num - colorStats.min) / range));
            // Red (low) → Yellow (mid) → Green (high)
            const r = t < 0.5 ? 255 : Math.round(255 * (1 - (t - 0.5) * 2));
            const g = t < 0.5 ? Math.round(255 * t * 2) : 255;
            return `rgb(${r}, ${g}, 60)`;
        }

        return '#3b82f6';
    }, [colorStats, colorIndex]);

    const getColor = useCallback((point: Point) => {
        if (!colorField) return '#3b82f6'; // Default blue

        // Backend aliases dotted fields: "detections.label" → "detections_label"
        let val = point[colorField] ?? point[colorField.replace('.', '_')];

        // Array fields (tags, inferred_tags, …) - color by the last/most-specific value
        if (Array.isArray(val)) {
            val = val.length > 0 ? val[val.length - 1] : null;
        }

        return getColorForValue(val);
    }, [colorField, getColorForValue]);

    // Point in Polygon (Ray Casting)
    const isPointInPolygon = (x: number, y: number, poly: { x: number, y: number }[]) => {
        let inside = false;
        for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
            const xi = poly[i].x, yi = poly[i].y;
            const xj = poly[j].x, yj = poly[j].y;

            const intersect = ((yi > y) !== (yj > y))
                && (x < (xj - xi) * (y - yi) / (yj - yi) + xi);
            if (intersect) inside = !inside;
        }
        return inside;
    };

    // Canvas backing-store size tracked in state so BOTH the layer precompute and the
    // draw can use it (a canvas doesn't reflow like the DOM grid — it needs an explicit
    // resize + redraw). ResizeObserver is rAF-coalesced and skips no-op size changes.
    const [canvasSize, setCanvasSize] = useState({ w: 0, h: 0 });
    useEffect(() => {
        const el = containerRef.current;
        if (!el) return;
        const rect = el.getBoundingClientRect();
        setCanvasSize({ w: rect.width, h: rect.height });
        let raf = 0;
        const ro = new ResizeObserver(() => {
            cancelAnimationFrame(raf);
            raf = requestAnimationFrame(() => {
                const r = el.getBoundingClientRect();
                setCanvasSize(prev => (prev.w === r.width && prev.h === r.height ? prev : { w: r.width, h: r.height }));
            });
        });
        ro.observe(el);
        return () => { cancelAnimationFrame(raf); ro.disconnect(); };
    }, []);

    // Precompute draw layers in PIXEL space (offset by +20, scaled by the canvas size),
    // keyed off everything EXCEPT the transform. Pan/zoom only change `transform`, which
    // is applied with ctx.translate/scale at draw time — so the per-point work (color
    // resolution incl. the O(unique) indexOf in getColor, selection tests, bucketing) is
    // done ONCE here per data/color/selection/size change instead of on every frame.
    const renderLayers = useMemo(() => {
        const drawW = canvasSize.w - 40;
        const drawH = canvasSize.h - 40;
        const empty = {
            bg: [] as Array<{ color: string; coords: Float32Array }>,
            highlight: new Float32Array(0),
            patch: new Float32Array(0),
            selected: [] as Array<{ color: string; coords: Float32Array }>,
        };
        if (drawW <= 0 || drawH <= 0 || points.length === 0 || norm.nx.length !== points.length) return empty;

        const { nx, ny } = norm;
        const hasSelection = selectedIdSet.size > 0;
        const usePatchSelection = isPatchField && selectedPatchKeys.size > 0;
        const isPointSelected = (p: Point) =>
            usePatchSelection && p.field_name
                ? selectedPatchKeys.has(`${p.sample_id}_${p.field_name}`)
                : selectedIdSet.has(p.sample_id);

        const bgMap = new Map<string, number[]>();
        const selMap = new Map<string, number[]>();
        const highlight: number[] = [];
        const patch: number[] = [];

        for (let i = 0; i < points.length; i++) {
            const p = points[i];
            const px = nx[i] * drawW + 20;
            const py = ny[i] * drawH + 20;
            const selected = hasSelection && isPointSelected(p);
            const isHi = highlightedIdSet.has(p.sample_id);

            // 1. Background — everything not selected and not (checkbox-)highlighted
            if (!selected && !isHi) {
                const color = getColor(p);
                let b = bgMap.get(color);
                if (!b) { b = []; bgMap.set(color, b); }
                b.push(px, py);
            }
            // 2. Highlighted checkboxes — highlighted and not sample-selected
            if (isHi && !(hasSelection && selectedIdSet.has(p.sample_id))) {
                highlight.push(px, py);
            }
            // 2b. Patch search results
            if (p.field_name && patchHighlightKeys && patchHighlightKeys.has(`${p.sample_id}_${p.field_name}`)) {
                patch.push(px, py);
            }
            // 3. Selected (brushed)
            if (selected) {
                const color = getColor(p);
                let b = selMap.get(color);
                if (!b) { b = []; selMap.set(color, b); }
                b.push(px, py);
            }
        }

        const toLayers = (m: Map<string, number[]>) =>
            Array.from(m, ([color, arr]) => ({ color, coords: Float32Array.from(arr) }));
        return {
            bg: toLayers(bgMap),
            highlight: Float32Array.from(highlight),
            patch: Float32Array.from(patch),
            selected: toLayers(selMap),
        };
    }, [norm, points, getColor, selectedIdSet, highlightedIdSet, patchHighlightKeys, selectedPatchKeys, isPatchField, canvasSize]);

    // Draw loop — replays the precomputed layers under the current transform. rAF-coalesced
    // so a burst of pan/zoom events (wheel, mousemove) collapses to one redraw per frame.
    useEffect(() => {
        const canvas = canvasRef.current;
        const ctx = canvas?.getContext('2d');
        if (!canvas || !ctx) return;
        const { w, h } = canvasSize;
        if (w <= 0 || h <= 0) return;

        const raf = requestAnimationFrame(() => {
            // Reassigning canvas.width/height resets all context state each frame (incl.
            // line dash) and clears the backing store.
            canvas.width = w;
            canvas.height = h;

            ctx.fillStyle = '#18181b'; // zinc-950
            ctx.fillRect(0, 0, w, h);

            ctx.save();
            ctx.translate(transform.x, transform.y);
            ctx.scale(transform.k, transform.k);

            const hasSelection = selectedIdSet.size > 0;
            const pointSize = Math.max(1, 2 / transform.k);
            const half = pointSize / 2;

            // 1. Background — one OPAQUE fillRect per point, grouped by color.
            //
            // NOT one big path of rect()s + a single fill(): filling a path made of N subpaths
            // is superlinear in N, and this redraws on EVERY pan/zoom frame (transform is a dep
            // of this effect). Individual fillRect calls avoid building that combined path.
            for (const { color, coords } of renderLayers.bg) {
                ctx.fillStyle = color;
                for (let i = 0; i < coords.length; i += 2) {
                    ctx.fillRect(coords[i] - half, coords[i + 1] - half, pointSize, pointSize);
                }
            }

            // 1b. Dim the background when there's a selection — as ONE scrim over the layer
            // just drawn, NOT via globalAlpha on each point. Per-point alpha composites once
            // per OVERLAPPING point (1-0.85^n), so dense clusters saturate back to full
            // brightness and the dim silently stops working; the old single-path fill got this
            // right for free because it filled the union once. The scrim is exact and O(1):
            // over the #18181b backdrop, painting C opaque then 85% backdrop on top yields
            // 0.15*C + 0.85*backdrop — pixel-identical to drawing C at globalAlpha 0.15, for
            // overlapping and non-overlapping points alike. Screen space, so it covers the
            // canvas regardless of pan/zoom, and it lands BEFORE the highlight/selected layers
            // so those stay at full opacity.
            if (hasSelection) {
                ctx.save();
                ctx.setTransform(1, 0, 0, 1, 0, 0);
                ctx.fillStyle = 'rgba(24, 24, 27, 0.85)'; // #18181b (zinc-950) at 1 - 0.15
                ctx.fillRect(0, 0, w, h);
                ctx.restore();
            }

            // 2. Highlighted checkboxes — cyan dots
            const hl = renderLayers.highlight;
            if (hl.length > 0) {
                ctx.fillStyle = '#00ffff';
                ctx.strokeStyle = '#ffffff';
                ctx.lineWidth = 2 / transform.k;
                const r = 8 / transform.k;
                for (let i = 0; i < hl.length; i += 2) {
                    ctx.beginPath();
                    ctx.arc(hl[i], hl[i + 1], r, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.stroke();
                }
            }

            // 2b. Patch search results — amber dots
            const pa = renderLayers.patch;
            if (pa.length > 0) {
                ctx.fillStyle = '#f59e0b';
                ctx.strokeStyle = '#ffffff';
                ctx.lineWidth = 1.5 / transform.k;
                const r = 6 / transform.k;
                for (let i = 0; i < pa.length; i += 2) {
                    ctx.beginPath();
                    ctx.arc(pa[i], pa[i + 1], r, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.stroke();
                }
            }

            // 3. Selected (brushed) — full opacity, point color, white stroke.
            //
            // Square markers (fillRect + strokeRect), NOT circles: a lasso over the dense
            // centre selects hundreds of thousands of points, and beginPath+arc+fill+stroke
            // per point cost 877ms/frame at 252k selected vs 269ms for this — on every pan
            // frame. Keeps the white outline that makes a small selection easy to spot.
            if (hasSelection) {
                ctx.strokeStyle = '#ffffff';
                ctx.lineWidth = 1 / transform.k;
                const r = 5 / transform.k;
                const d = r * 2;
                for (const { color, coords } of renderLayers.selected) {
                    ctx.fillStyle = color;
                    for (let i = 0; i < coords.length; i += 2) {
                        ctx.fillRect(coords[i] - r, coords[i + 1] - r, d, d);
                        ctx.strokeRect(coords[i] - r, coords[i + 1] - r, d, d);
                    }
                }
            }

            ctx.restore();
        });
        return () => cancelAnimationFrame(raf);
    }, [renderLayers, transform, selectedIdSet, canvasSize]);

    // Lasso outline (screen space) on the overlay canvas. Each mouse move during a shift-drag adds a
    // vertex; drawing it on the points canvas would repaint every point per move.
    // This redraws only the outline.
    useEffect(() => {
        const overlay = overlayRef.current;
        const ctx = overlay?.getContext('2d');
        if (!overlay || !ctx) return;
        const { w, h } = canvasSize;
        if (w <= 0 || h <= 0) return;

        const raf = requestAnimationFrame(() => {
            if (overlay.width !== w || overlay.height !== h) {
                overlay.width = w;
                overlay.height = h;
            }
            ctx.clearRect(0, 0, w, h);
            if (selectionPolygon.length === 0) return;
            ctx.beginPath();
            ctx.moveTo(selectionPolygon[0].x, selectionPolygon[0].y);
            for (let i = 1; i < selectionPolygon.length; i++) {
                ctx.lineTo(selectionPolygon[i].x, selectionPolygon[i].y);
            }
            if (!isSelecting) ctx.closePath();

            ctx.strokeStyle = 'white';
            ctx.lineWidth = 1;
            ctx.setLineDash([5, 5]);
            ctx.stroke();

            ctx.fillStyle = 'rgba(255, 255, 255, 0.1)';
            ctx.fill();
        });
        return () => cancelAnimationFrame(raf);
    }, [selectionPolygon, isSelecting, canvasSize]);

    // Persist the sample % as the last working value ONLY once the CURRENT sample's data
    // has settled (!isFetching, so not stale placeholder data mid-refetch) and rendered,
    // and clear the crash sentinel. The draw effect above runs first, so a render crash
    // leaves the sentinel set -> small-sample recovery on reload.
    useEffect(() => {
        if (!isFetching && norm.nx.length > 0) {
            localStorage.setItem(storageKey, String(samplePct));
            try { sessionStorage.removeItem(attemptKey); } catch { /* ignore */ }
        }
    }, [samplePct, storageKey, attemptKey, norm.nx.length, isFetching]);

    // Normal teardown clears the attempt sentinel; a crash (tab killed) skips this cleanup,
    // so the leftover sentinel triggers small-sample recovery on the next mount. A reload or
    // navigation mid-load skips React's cleanup too, but fires pagehide, which a crash doesn't.
    useEffect(() => {
        const clear = () => { try { sessionStorage.removeItem(attemptKey); } catch { /* ignore */ } };
        window.addEventListener('pagehide', clear);
        return () => { window.removeEventListener('pagehide', clear); clear(); };
    }, [attemptKey]);

    // Interaction Handlers
    const handleMouseDown = (e: React.MouseEvent) => {
        if (e.shiftKey) {
            setIsSelecting(true);
            setSelectionPolygon([{ x: e.nativeEvent.offsetX, y: e.nativeEvent.offsetY }]);
        } else {
            setIsDragging(true);
            setLastPos({ x: e.clientX, y: e.clientY });
        }
    };

    const handleMouseMove = (e: React.MouseEvent) => {
        if (isDragging) {
            const dx = e.clientX - lastPos.x;
            const dy = e.clientY - lastPos.y;
            setTransform(t => ({ ...t, x: t.x + dx, y: t.y + dy }));
            setLastPos({ x: e.clientX, y: e.clientY });
        } else if (isSelecting) {
            const nx = e.nativeEvent.offsetX, ny = e.nativeEvent.offsetY;
            setSelectionPolygon(prev => {
                if (prev.length > 0) {
                    const last = prev[prev.length - 1];
                    if (Math.abs(nx - last.x) + Math.abs(ny - last.y) < 3) return prev;
                }
                return [...prev, { x: nx, y: ny }];
            });
        }
    };

    const handleMouseUp = async () => {
        if (isSelecting && selectionPolygon.length > 2 && containerRef.current) {
            const { width, height } = containerRef.current.getBoundingClientRect();
            const drawW = width - 40;
            const drawH = height - 40;

            // First, find visible sample IDs inside polygon (instant feedback)
            const visibleSelectedIds: number[] = [];
            const patchKeys = new Set<string>();
            const { nx, ny } = norm;
            for (let i = 0; i < points.length; i++) {
                const p = points[i];
                const px = (nx[i] * drawW + 20) * transform.k + transform.x;
                const py = (ny[i] * drawH + 20) * transform.k + transform.y;
                if (isPointInPolygon(px, py, selectionPolygon)) {
                    visibleSelectedIds.push(p.sample_id);
                    if (p.field_name) patchKeys.add(`${p.sample_id}_${p.field_name}`);
                }
            }

            // For patch embeddings, track specific patches and notify parent
            if (isPatchField) {
                setSelectedPatchKeys(patchKeys);
                if (onPatchSelection) {
                    // Parse patch keys back into a Map for the grid
                    const patchMap = new Map<number, Array<{ row: number; col: number; scale: string }>>();
                    const rcRe = /_patch_(\d+x\d+)_r(\d+)c(\d+)$/;
                    for (const key of patchKeys) {
                        const sepIdx = key.indexOf('_');
                        const sampleId = parseInt(key.substring(0, sepIdx));
                        const fn = key.substring(sepIdx + 1);
                        const m = rcRe.exec(fn);
                        if (m) {
                            const arr = patchMap.get(sampleId) || [];
                            arr.push({ row: parseInt(m[2]), col: parseInt(m[3]), scale: m[1] });
                            patchMap.set(sampleId, arr);
                        }
                    }
                    onPatchSelection(patchMap);
                }
            }

            // Show visible selection immediately (deduplicate sample_ids)
            const dedupVisibleIds = [...new Set(visibleSelectedIds)];
            onSelection(dedupVisibleIds);

            // If sampled, the scatter shows only a subset, so the lasso must select the FULL
            // area (not just the visible points). Convert the screen polygon to UMAP coords.
            if (isSampled && extent) {
                const rangeX = extent.maxX - extent.minX || 1;
                const rangeY = extent.maxY - extent.minY || 1;
                const umapPolygon: [number, number][] = selectionPolygon.map(sp => {
                    const nx = ((sp.x - transform.x) / transform.k - 20) / drawW;
                    const ny = ((sp.y - transform.y) / transform.k - 20) / drawH;
                    return [extent.minX + nx * rangeX, extent.minY + ny * rangeY];
                });

                if (!isPatchField) {
                    // Whole-image: the live `embedding_polygon` filter does all the work at
                    // query time, so there's NO server round-trip — the lasso completes
                    // instantly. (token/total unused; the polygon itself is the filter.)
                    lassoMap.current = { field: embeddingField, model: mapModel ?? null };
                    onSelection(dedupVisibleIds, { field: embeddingField, polygon: umapPolygon, token: '', total: 0, modelId: mapModel ?? null });
                } else {
                    // Patch fields still need the server to enumerate the matched patches.
                    try {
                        const result = await selectEmbeddingRegion(datasetId, embeddingField, umapPolygon, scatterFilters);
                        onSelection(dedupVisibleIds, { field: embeddingField, polygon: umapPolygon, token: result.token ?? '', total: result.total });
                        if (result.patches && onPatchSelection) {
                            const fullPatchMap = new Map<number, Array<{ row: number; col: number; scale: string }>>();
                            const rcRe = /_patch_(\d+x\d+)_r(\d+)c(\d+)$/;
                            for (const p of result.patches) {
                                const m = rcRe.exec(p.field_name);
                                if (m) {
                                    const arr = fullPatchMap.get(p.sample_id) || [];
                                    arr.push({ row: parseInt(m[2]), col: parseInt(m[3]), scale: m[1] });
                                    fullPatchMap.set(p.sample_id, arr);
                                }
                            }
                            onPatchSelection(fullPatchMap);
                            const fullKeys = new Set<string>();
                            for (const p of result.patches) {
                                fullKeys.add(`${p.sample_id}_${p.field_name}`);
                            }
                            setSelectedPatchKeys(fullKeys);
                        }
                    } catch (e) {
                        console.error('Region selection failed, using visible points only:', e);
                    }
                }
            }

            setSelectionPolygon([]);
        } else if (isSelecting) {
            setSelectionPolygon([]);
            if (selectionPolygon.length < 3) {
                onSelection([], null);  // cancelled lasso → clear the region filter too
                setSelectedPatchKeys(new Set());
                onPatchSelection?.(new Map<number, Array<{ row: number; col: number; scale: string }>>());
            }
        }

        setIsDragging(false);
        setIsSelecting(false);
    };

    // Handle wheel with non-passive listener to allow preventDefault
    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;

        const onWheel = (e: WheelEvent) => {
            e.preventDefault();

            // Reduced sensitivity for smoother zooming
            const scaleAmount = -e.deltaY * 0.0005;

            setTransform(prevTransform => {
                const newScale = Math.max(0.1, Math.min(50, prevTransform.k * (1 + scaleAmount)));

                // Mouse position relative to canvas
                const mx = e.offsetX;
                const my = e.offsetY;

                // Calculate new position to keep mouse point stationary
                const newX = mx - ((mx - prevTransform.x) / prevTransform.k) * newScale;
                const newY = my - ((my - prevTransform.y) / prevTransform.k) * newScale;

                return { k: newScale, x: newX, y: newY };
            });
        };

        canvas.addEventListener('wheel', onWheel, { passive: false });
        return () => canvas.removeEventListener('wheel', onWheel);
    }, []); // Empty dependency array - listener doesn't need to be recreated

    const handleResetZoom = () => {
        setTransform({ k: 1, x: 0, y: 0 });
    };

    return (
        <div className="h-full flex flex-col bg-zinc-900">
            <div className="px-3 py-2 border-b border-zinc-800 shrink-0 space-y-1.5">
                {crashedWarning && (
                    <div className="flex items-center justify-between gap-2 px-2 py-1 bg-amber-900/40 border border-amber-700/50 rounded text-[11px] text-amber-200">
                        <span>Reduced to a small sample — the previous load may have run out of memory. Increase it when ready.</span>
                        <button onClick={() => setCrashedWarning(false)} className="text-amber-300 hover:text-amber-100 shrink-0" title="Dismiss">✕</button>
                    </div>
                )}
                {/* Row 1: Dropdowns + controls + hint */}
                <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                        <h2 className="text-sm font-bold text-white shrink-0">Embeddings</h2>
                        <select
                            value={embeddingField}
                            onChange={(e) => setEmbeddingField(e.target.value)}
                            className="bg-zinc-800 border border-zinc-700 text-xs text-white rounded px-2 py-1"
                        >
                            {availableFields && availableFields.length > 0 ? (
                                availableFields.map((f: string) => (
                                    <option key={f} value={f}>{f}</option>
                                ))
                            ) : (
                                <option value="clip">clip (default)</option>
                            )}
                        </select>
                        <select
                            value={colorField}
                            onChange={(e) => setColorField(e.target.value)}
                            className="bg-zinc-800 border border-zinc-700 text-xs text-white rounded px-2 py-1"
                        >
                            <option value="">Color by...</option>
                            {embeddingField.includes('_patch_') && (
                                <option value="patch_position">Patch Position</option>
                            )}
                            {schema
                                .filter(f => {
                                    if (f.name === 'tags') return true;
                                    return f.type === 'string' || f.type === 'enum' || f.type === 'number';
                                })
                                .map(f => (
                                    <option key={f.name} value={f.name}>{f.name}</option>
                                ))
                            }
                        </select>
                        <div className="relative" ref={samplePickerRef}>
                            {samplePickerOpen ? (
                                <select
                                    autoFocus
                                    value={samplePct}
                                    onChange={(e) => { setSamplePct(Number(e.target.value)); setSamplePickerOpen(false); }}
                                    onBlur={() => setSamplePickerOpen(false)}
                                    className="bg-zinc-800 border border-zinc-700 text-xs text-white rounded px-2 py-1"
                                >
                                    {[5, 10, 20, 30, 50, 75, 100].map(pct => (
                                        <option key={pct} value={pct}>{pct >= 100 ? 'All' : `${pct}%`}</option>
                                    ))}
                                </select>
                            ) : (
                                <button
                                    onClick={() => setSamplePickerOpen(true)}
                                    className="text-xs px-2 py-1 rounded border bg-zinc-800 border-zinc-700 text-zinc-400 hover:text-white hover:border-zinc-600 transition-colors"
                                >
                                    {!isSampled && points.length > 0
                                        ? 'Sample: All'
                                        : `Sample: ${samplePct >= 100 ? 'All' : `${samplePct}%`}`
                                    }
                                </button>
                            )}
                            {rankStale && (
                                <span
                                    className="text-[10px] text-amber-400"
                                    title="Coordinates were re-projected after the coverage ranks were built. Every point is drawn at its current position; the even-coverage sampling is approximate until the next re-rank."
                                >
                                    coverage stale
                                </span>
                            )}
                            {mapChangedNotice && (
                                <span
                                    data-testid="map-changed-notice"
                                    className="text-[10px] text-amber-400 whitespace-nowrap"
                                    role="status"
                                    title="The map was re-fitted, so a lasso selection drawn on the old map was removed. Draw your lasso selection again."
                                >
                                    Map re-fitted: draw the lasso again
                                    <button className="ml-1 underline" onClick={() => setMapChangedNotice(false)}>OK</button>
                                </span>
                            )}
                            {refittedAt && (
                                <span
                                    data-testid="map-refitted-at"
                                    className="text-[10px] text-zinc-500"
                                    title="This map was re-fitted from all of the field's embeddings (Jobs → Schedules). New samples are placed on it as they arrive."
                                >
                                    Map re-fitted on {new Date(refittedAt).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}
                                </span>
                            )}
                        </div>
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                        <button
                            onClick={handleResetZoom}
                            className="p-1.5 hover:bg-zinc-800 rounded text-zinc-400 hover:text-white transition-colors"
                            title="Reset Zoom"
                        >
                            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                                <rect x="2" y="2" width="11" height="11" rx="1" />
                                <path d="M7.5 5v5M5 7.5h5" />
                            </svg>
                        </button>
                    </div>
                </div>
                {/* Row 2: Selection + point count + hint */}
                <div className="flex items-center gap-2">
                    {selectedIds.length > 0 && (
                        <button
                            onClick={() => onSelection([], null)}
                            data-testid="embeddings-selected-count"
                            className="bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-400 hover:text-cyan-300 border border-cyan-500/50 text-xs px-2 py-0.5 rounded flex items-center gap-1 transition-colors"
                            title="Click to clear selection"
                        >
                            <span>{selectedIds.length.toLocaleString()} selected</span>
                            <span>✕</span>
                        </button>
                    )}
                    {points.length > 0 && (
                        <span data-testid="embeddings-point-count" className="text-xs text-zinc-500 whitespace-nowrap">
                            {isSampled
                                ? `${points.length.toLocaleString()} / ${totalPoints.toLocaleString()} points`
                                : `${points.length.toLocaleString()} points`
                            }
                        </span>
                    )}
                    <span className="flex-1" />
                    <span className="text-[10px] text-zinc-600 whitespace-nowrap">Shift+Drag to lasso • Scroll to zoom • Drag to pan</span>
                </div>
            </div>
            <div
                ref={containerRef}
                className="flex-1 relative overflow-hidden cursor-crosshair"
            >
                <canvas
                    ref={canvasRef}
                    data-testid="embeddings-canvas"
                    onMouseDown={handleMouseDown}
                    onMouseMove={handleMouseMove}
                    onMouseUp={handleMouseUp}
                    onMouseLeave={handleMouseUp}
                    // onWheel handled by useEffect for non-passive listener
                    className="absolute inset-0"
                />
                <canvas
                    ref={overlayRef}
                    data-testid="embeddings-lasso-overlay"
                    className="absolute inset-0 pointer-events-none"
                />

                {/* Loading State */}
                {(isLoading || isFetching) && (
                    <div className="absolute inset-0 flex items-center justify-center bg-zinc-900/80 backdrop-blur-sm">
                        <div className="text-center">
                            <svg className="w-8 h-8 text-cyan-400 animate-spin mx-auto mb-2" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                            </svg>
                            <div className="text-sm text-zinc-400">Loading embeddings...</div>
                        </div>
                    </div>
                )}

                {/* Error State — distinct from the empty state so a failed load isn't read as
                    "this dataset has no embeddings" (and isn't a silently blank canvas). */}
                {status === 'error' && !isFetching && (
                    <div className="absolute inset-0 flex items-center justify-center bg-zinc-900/80 backdrop-blur-sm">
                        <div className="text-center">
                            <div className="text-sm text-red-300 mb-3">Failed to load embeddings</div>
                            <button
                                onClick={() => refetch()}
                                className="px-3 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm font-medium"
                            >
                                Retry
                            </button>
                        </div>
                    </div>
                )}

                {/* Empty State: dataset has NO embedding fields at all (the bare-add_samples
                    case). Without this branch the points query never runs (it's gated on
                    availableFields) and the canvas is a silently blank rectangle. */}
                {availableFields && availableFields.length === 0 && (
                    <div className="absolute inset-0 flex items-center justify-center" data-testid="embeddings-empty-no-fields">
                        <div className="text-center text-zinc-500 max-w-md px-4">
                            <div className="text-sm mb-2 text-zinc-400">No embedding fields on this dataset yet</div>
                            <div className="text-xs mb-3">Compute and upload them with the Python SDK:</div>
                            <pre className="text-left text-xs bg-zinc-900 border border-zinc-700 rounded p-3 text-zinc-300 overflow-x-auto">{`from fridge import Dataset
ds = Dataset.from_id(${datasetId})
ds.compute_embeddings()      # local CLIP — pip install "freedomridge[embeddings]"
ds.compute_visualization()   # UMAP → this scatter`}</pre>
                            <div className="text-xs mt-3">
                                …or ship your own model&apos;s vectors inline at ingest:{' '}
                                <code className="text-zinc-400">add_samples(embeddings=&#123;&quot;myfield&quot;: vec&#125;)</code>
                            </div>
                        </div>
                    </div>
                )}

                {/* Empty State (fields exist, loading completed, zero points). Two causes with
                    different fixes: active filters excluding everything, vs vectors uploaded but
                    never UMAP-reduced (x/y still NULL) — offer the reduction inline for the latter. */}
                {!isLoading && !isFetching && status === 'success' && embeddingField && availableFields && availableFields.length > 0 && (!points || points.length === 0) && (
                    <div className="absolute inset-0 flex items-center justify-center">
                        <div className="text-center text-zinc-500 max-w-md px-4" data-testid="embeddings-empty-no-points">
                            {scatterFilters && scatterFilters.filters && scatterFilters.filters.length > 0 ? (
                                <>
                                    <div className="text-sm mb-2">No points match the current filters</div>
                                    <div className="text-xs">Clear or loosen the filters to see this field&apos;s points</div>
                                </>
                            ) : (
                                <>
                                    <div className="text-sm mb-2 text-zinc-400">
                                        <span className="font-mono">{embeddingField}</span> has vectors but no 2-D coordinates yet
                                    </div>
                                    {/* The reduce endpoint needs power_user — don't show viewers a button that can only 403 */}
                                    {(user?.role === 'admin' || user?.role === 'power_user') && (
                                    <button
                                        onClick={runReduction}
                                        disabled={reducing}
                                        data-testid="embeddings-run-reduction"
                                        className="px-3 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white text-sm font-medium"
                                    >
                                        {reducing ? 'Reducing… (can take minutes)' : 'Run UMAP reduction'}
                                    </button>
                                    )}
                                    {reduceError && (
                                        <div className="text-xs text-red-300 mt-3 whitespace-pre-wrap">{reduceError}</div>
                                    )}
                                    <div className="text-xs mt-3">
                                        Large field? Run it client-side instead:{' '}
                                        <code className="text-zinc-400">Dataset.from_id({datasetId}).compute_visualization(field_name=&quot;{embeddingField}&quot;)</code>
                                    </div>
                                </>
                            )}
                        </div>
                    </div>
                )}

                {/* Legend */}
                {colorField && colorStats && (
                    <div className="absolute top-4 right-4 bg-zinc-900/90 backdrop-blur-sm border border-zinc-700 rounded-lg p-3 text-xs max-h-64 overflow-y-auto">
                        <div className="font-semibold text-white mb-2">{colorField}</div>
                        {colorStats.type === 'number' ? colorStats.hasRange ? (
                            <div className="space-y-1">
                                <div className="flex items-center gap-2">
                                    <div className="w-4 h-4 rounded" style={{ backgroundColor: getColorForValue(colorStats.max) }} />
                                    <span className="text-zinc-300">{colorStats.max.toFixed(2)}</span>
                                </div>
                                <div className="w-4 h-16 rounded" style={{
                                    background: `linear-gradient(to bottom, ${getColorForValue(colorStats.max)}, ${getColorForValue((colorStats.max + colorStats.min) / 2)}, ${getColorForValue(colorStats.min)})`
                                }} />
                                <div className="flex items-center gap-2">
                                    <div className="w-4 h-4 rounded" style={{ backgroundColor: getColorForValue(colorStats.min) }} />
                                    <span className="text-zinc-300">{colorStats.min.toFixed(2)}</span>
                                </div>
                            </div>
                        ) : <div className="text-zinc-500">No compatible numeric values</div> : (
                            <div className="space-y-1 max-w-[200px]">
                                {colorStats.uniqueValues.slice(0, 20).map((value: string) => (
                                    <div key={value} className="flex items-center gap-2">
                                        <div className="w-3 h-3 rounded-full flex-shrink-0" style={{ backgroundColor: getColorForValue(value) }} />
                                        <span className="text-zinc-300 truncate" title={value}>{value}</span>
                                    </div>
                                ))}
                                {colorStats.uniqueValues.length > 20 && (
                                    <div className="text-zinc-500 italic">+{colorStats.uniqueValues.length - 20} more</div>
                                )}
                            </div>
                        )}
                    </div>
                )}

            </div>
        </div>
    );
}
