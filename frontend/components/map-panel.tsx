'use client';

import { useEffect, useMemo, useState, useCallback, useRef } from 'react';
import Map, { Source, Layer, ScaleControl, MapRef, type MapEvent, type MapMouseEvent, type LayerProps } from 'react-map-gl/mapbox';
import { useQuery } from '@tanstack/react-query';
import { getMapPoints, getMapExtent, getDatasetCoverage, type ComplexFilter } from '@/lib/api';
import type { Feature, FeatureCollection, Point, Polygon } from 'geojson';
import mapboxgl, { type LngLatBounds } from 'mapbox-gl';
import type MapboxDraw from '@mapbox/mapbox-gl-draw';

import '@mapbox/mapbox-gl-draw/dist/mapbox-gl-draw.css';
import 'mapbox-gl/dist/mapbox-gl.css';

// Bubbles and unselected points are amber (Tailwind amber-500, used across the UI) so they stand
// out on the dark basemap and contrast with the blue selected-point highlight.
const DEFAULT_MAP_CONFIG = {
    mapboxAccessToken: process.env.NEXT_PUBLIC_MAPBOX_TOKEN,
    clusterColor: '#f59e0b',
};

const EMPTY_FC: FeatureCollection<Point> = { type: 'FeatureCollection', features: [] };

// Mirror of backend _MAP_POINT_ZOOM (docs/MAP_REQUIREMENTS.md #1): at/above this zoom
// the server returns individual points, so we DON'T enlarge the fetch region there
// (it would risk the point cap).
const MAP_POINT_ZOOM = 12;

// Tile the viewport: snap the fetched bbox out to a fixed per-zoom world grid so that
// panning WITHIN a tile produces the same request and hits the react-query cache,
// instead of refetching on every pixel of pan. The grid depends only on zoom, so the
// same tiles recur as you pan (and the whole-world view snaps to one stable tile).
// Returns the viewport unchanged at points zooms. See docs/MAP_REQUIREMENTS.md #13.
function tileBbox(bbox: [number, number, number, number], zoom: number): [number, number, number, number] {
    if (zoom >= MAP_POINT_ZOOM) return bbox;
    const step = 360 / Math.pow(2, Math.max(0, zoom - 1)); // ~2 web-mercator tiles wide
    const [w, s, e, n] = bbox;
    return [
        Math.max(-180, Math.floor(w / step) * step),
        Math.max(-85, Math.floor(s / step) * step),
        Math.min(180, Math.ceil(e / step) * step),
        Math.min(85, Math.ceil(n / step) * step),
    ];
}

// A map_points item: with an `id` it's an individual sample point; without one it's
// a cluster bubble (cell centroid + count, and the radius in px the server sized it for).
interface MapPointItem { id: number; lon: number; lat: number; heading?: number }
interface MapClusterItem { id?: null; lon: number; lat: number; count: number; radius: number }
type MapItem = MapPointItem | MapClusterItem;

// Stable empty-array identity so `items` (and the geojson useMemo that depends on it)
// don't change reference every render when there are no points yet.
const EMPTY_ITEMS: MapItem[] = [];

interface MapPanelConfig {
    mapboxAccessToken?: string;
    clusterColor?: string;
}

interface Props {
    datasetId: number;
    filters?: ComplexFilter;
    onSelection: (ids: number[], polygon?: Feature | null) => void;
    selectedIds: number[];
    config?: MapPanelConfig;
    initialPolygon?: Feature<Polygon> | null;
}

export function MapPanel({
    datasetId,
    filters,
    onSelection,
    selectedIds,
    config = DEFAULT_MAP_CONFIG,
}: Props) {
    const [mounted, setMounted] = useState(false);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Mount the browser-only map after hydration.
    useEffect(() => { setMounted(true); }, []);

    const mapRef = useRef<MapRef>(null);
    const containerRef = useRef<HTMLDivElement>(null);
    const [viewState, setViewState] = useState({ longitude: 0, latitude: 0, zoom: 1 });
    const [isDrawing, setIsDrawing] = useState(false);

    // Server-side clustering view: {zoom, bbox} drives what we fetch. Updated
    // (debounced) on map move so we fetch clusters/points for the current viewport.
    const [view, setView] = useState<{ zoom: number; bbox: [number, number, number, number] } | null>(null);
    const [forceMode, setForceMode] = useState<'auto' | 'points'>('auto');  // auto = per-cell hybrid
    const [zooming, setZooming] = useState(false);   // zoom-to-data fetch in flight
    const [showBusy, setShowBusy] = useState(false); // delayed "Loading…" pill (see effect below)
    const [msg, setMsg] = useState<string | null>(null);
    const msgTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const moveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    useEffect(() => () => {
        if (msgTimerRef.current) clearTimeout(msgTimerRef.current);
        if (moveTimerRef.current) clearTimeout(moveTimerRef.current);
    }, []);
    const flashMsg = useCallback((text: string) => {
        setMsg(text);
        if (msgTimerRef.current) clearTimeout(msgTimerRef.current);
        msgTimerRef.current = setTimeout(() => setMsg(null), 4000);
    }, []);

    // Capture the current VISIBLE viewport (zoom + bbox) into `view`, which drives the
    // query. The server decides points-vs-clusters from this visible count and pads
    // the points data itself, so we send the exact visible bounds (no client padding).
    const captureView = useCallback(() => {
        const map = mapRef.current?.getMap();
        if (!map) return;
        const b = map.getBounds();
        if (!b) return;
        setView({
            zoom: map.getZoom(),
            bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()],
        });
    }, []);

    // Resize the canvas AND refetch for the new visible area whenever the panel /
    // container changes size (panel splits, layout changes, window resize).
    const refitToContainer = useCallback(() => {
        mapRef.current?.resize();
        captureView();
    }, [captureView]);

    useEffect(() => {
        const handler = () => refitToContainer();
        window.addEventListener('resize', handler);
        return () => window.removeEventListener('resize', handler);
    }, [refitToContainer]);

    // Hybrid by default (server: dense cells -> cluster bubble, sparse cells ->
    // individual points). The server ALSO auto-returns ALL individual points once
    // zoom >= _MAP_POINT_ZOOM (12, the old clusterMaxZoom) — a hard requirement, see
    // docs/MAP_REQUIREMENTS.md. "De-cluster view" forces all-points; "Cluster" returns
    // to the hybrid (auto) view.
    // Snap to a tile so panning reuses cached fetches, and keep seen tiles in memory
    // (gcTime) long enough that returning to a view — especially the whole-dataset
    // "all samples" view — is instant instead of a multi-second reload. #13.
    const qzoom = view ? Math.round(view.zoom) : 1;
    const qbbox = view ? tileBbox(view.bbox, qzoom) : undefined;
    const { data: resp, isFetching, isError, refetch } = useQuery<{ mode?: string; items?: MapItem[]; capped?: boolean }>({
        queryKey: ['map-points', datasetId, filters, forceMode, qzoom,
            qbbox ? qbbox.map(v => v.toFixed(3)).join(',') : ''],
        queryFn: () => getMapPoints(datasetId, filters, {
            zoom: qzoom,
            bbox: qbbox,
            points: forceMode === 'points',
        }),
        enabled: !!datasetId && !!view,
        staleTime: 30_000,        // re-show instantly; background-refresh after 30s so edits still surface
        gcTime: 5 * 60_000,       // keep seen tiles ~5min (bounds browser memory; global view re-fetch is ~750ms)
        refetchOnWindowFocus: false,
        placeholderData: (prev) => prev,
    });

    // Coverage drives the "no locations at all" empty state — an empty world map is
    // otherwise indistinguishable from "zoomed to the wrong place". Only fetched while
    // the map is empty, and only says something when the DATASET truly has 0 locations.
    const { data: coverage } = useQuery({
        queryKey: ['coverage', datasetId],
        queryFn: () => getDatasetCoverage(datasetId),
        enabled: !!datasetId && !!resp && (resp.items?.length ?? 0) === 0,
        // Coverage counts scan embeddings/hashes server-side — not for hot polling.
        staleTime: 30 * 60_000,
        refetchOnWindowFocus: false,
    });

    const items: MapItem[] = resp?.items ?? EMPTY_ITEMS;
    const hasClusters = items.some((it) => it.id == null);
    // "Showing points" = forced, or the hybrid view came back with no cluster bubbles.
    const showingPoints = forceMode === 'points' || (items.length > 0 && !hasClusters);

    // Background load over already-visible data (cluster click re-clusters finer,
    // Zoom-to-Data, pan/zoom). The full "Loading map data..." overlay only covers the
    // FIRST load (!resp); these refetches keep prev data via placeholderData, so without
    // this the slow ones (whole-US cluster ~3s) give no feedback. Show a subtle pill,
    // but only after 150ms so quick zoomed-in loads (~100ms) don't flicker.
    const loadingMore = !!resp && (isFetching || zooming);
    const busyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- Clear the loading indicator as soon as the request settles.
        if (!loadingMore) { setShowBusy(false); return; }
        busyTimerRef.current = setTimeout(() => setShowBusy(true), 150);
        return () => { if (busyTimerRef.current) clearTimeout(busyTimerRef.current); };
    }, [loadingMore]);

    // If the forced all-points view overflowed the cap, fall back to the hybrid view.
    useEffect(() => {
        if (resp?.capped) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- Reset the explicit point mode when the server requires clustering.
            setForceMode('auto');
            flashMsg('Too many points to show individually — showing clusters. Zoom in.');
        }
    }, [resp, flashMsg]);

    // Items are a MIX: an item with `id` is an individual point; otherwise a cluster.
    const geojson = useMemo<FeatureCollection<Point>>(() => {
        if (!items.length) return EMPTY_FC;
        return {
            type: 'FeatureCollection',
            features: items.map((it) => it.id != null
                ? {
                    type: 'Feature',
                    geometry: { type: 'Point', coordinates: [it.lon, it.lat] },
                    properties: { id: it.id, ...(typeof it.heading === 'number' ? { heading: it.heading } : {}) },
                }
                : {
                    type: 'Feature',
                    geometry: { type: 'Point', coordinates: [it.lon, it.lat] },
                    properties: { cluster_count: it.count, radius: it.radius },
                }
            ),
        };
    }, [items]);

    // Hide the busy pill the MOMENT fresh data renders (geojson changed), even if a
    // follow-up background fetch is still settling — so it doesn't linger once you can
    // already see the new data. Pairs with the delayed show above.
    useEffect(() => {
        if (busyTimerRef.current) { clearTimeout(busyTimerRef.current); busyTimerRef.current = null; }
        // eslint-disable-next-line react-hooks/set-state-in-effect -- Fresh geometry ends the busy indicator before request bookkeeping settles.
        setShowBusy(false);
    }, [geojson]);
    const clusterColor = config?.clusterColor || DEFAULT_MAP_CONFIG.clusterColor;

    // Initial view + refetch on move (debounced so a pan/zoom drag doesn't spam).
    const handleMoveEnd = useCallback(() => {
        if (moveTimerRef.current) clearTimeout(moveTimerRef.current);
        moveTimerRef.current = setTimeout(() => captureView(), 250);
    }, [captureView]);

    // Deps MUST stay [] so this keeps a stable identity. flyToCluster is a dependency of
    // the MapboxDraw init effect; if it changes on every zoom (as it did when it closed
    // over viewState.zoom) that effect tears down and recreates the draw control mid-zoom,
    // wiping the user's drawn polygon. Read the live zoom from the map ref instead.
    const flyToCluster = useCallback((coords: [number, number]) => {
        const z = mapRef.current?.getMap()?.getZoom() ?? 1;
        mapRef.current?.flyTo({ center: coords, zoom: Math.min(z + 2.5, 22), duration: 500 });
    }, []);

    const zoomToData = useCallback(async () => {
        if (!mapRef.current) return;
        // Fit to the TRUE data extent (min/max lon/lat) for the CURRENT filters — coarse
        // cluster centroids collapse to a point for small/polygon selections and make
        // fitBounds over-zoom. Respects filters (incl. the polygon), no stale state.
        setZooming(true);
        try {
            const ext = await getMapExtent(datasetId, filters);
            if (!ext.count || ext.w == null || ext.e == null || ext.s == null || ext.n == null) return;
            let { w, e, s, n } = ext as { w: number; e: number; s: number; n: number };
            // Degenerate box (points coincide) -> pad so it doesn't slam to maxZoom.
            if (e - w < 1e-6) { w -= 0.0005; e += 0.0005; }
            if (n - s < 1e-6) { s -= 0.0005; n += 0.0005; }
            const LngLatBounds = mapboxgl.LngLatBounds;
            mapRef.current.fitBounds(new LngLatBounds([w, s], [e, n]), { padding: 60, maxZoom: 16, duration: 600 });
        } catch { /* ignore */ } finally { setZooming(false); }
    }, [datasetId, filters]);

    // Per-point selected highlight via mapbox feature-state — O(1) per feature in the
    // paint expression, vs the old ['literal', selectedIds] that rescanned up to 10k ids
    // per feature per frame. Re-applied whenever the selection or the rendered data
    // changes. (The Source has promoteId="id" so a feature's id IS its sample id.)
    const MAX_HIGHLIGHT_SELECTION = 10000;
    useEffect(() => {
        const map = mapRef.current?.getMap();
        if (!map || !map.getSource('points')) return;
        try { map.removeFeatureState({ source: 'points' }); } catch { /* source not ready yet */ }
        if (selectedIds.length === 0 || selectedIds.length > MAX_HIGHLIGHT_SELECTION) return;
        for (const id of selectedIds) map.setFeatureState({ source: 'points', id }, { selected: true });
    }, [selectedIds, geojson]);

    // Selecting samples (grid checkboxes) fits the map to them: 1 -> fly in close,
    // multiple -> fitBounds so all selected are in view. REQUIRED, see
    // docs/MAP_REQUIREMENTS.md #12. Under server clustering the full point set isn't on
    // the client, so fetch the selected samples' locations on demand. Only on actual
    // selection CHANGE (not every refetch), and skipped for very large selections
    // (lasso-scale, not the click-a-few case — and would just zoom out to nothing).
    const prevSelectedRef = useRef<number[]>([]);
    useEffect(() => {
        const prev = prevSelectedRef.current;
        prevSelectedRef.current = selectedIds;
        // Cheap guards FIRST so a huge (e.g. select-all 2M) selection never runs the O(N) every().
        if (selectedIds.length === 0 || selectedIds.length > 2000 || !mapRef.current) return;
        if (selectedIds.length === prev.length && selectedIds.every((id, i) => id === prev[i])) return;
        let cancelled = false;
        (async () => {
            try {
                const r: { items?: MapPointItem[] } = await getMapPoints(datasetId, { field: 'id', operator: 'in', value: selectedIds }, { points: true });
                if (cancelled || !mapRef.current) return;
                const pts: MapPointItem[] = (r?.items || []).filter((p) => typeof p.lon === 'number' && typeof p.lat === 'number');
                if (!pts.length) return;
                if (pts.length === 1) {
                    mapRef.current.flyTo({ center: [pts[0].lon, pts[0].lat], zoom: 18, duration: 800 });
                } else {
                    const LngLatBounds = mapboxgl.LngLatBounds;
                    const bounds = pts.reduce((b: LngLatBounds, p) => b.extend([p.lon, p.lat] as [number, number]), new LngLatBounds());
                    mapRef.current.fitBounds(bounds, { padding: 80, maxZoom: 17, duration: 800 });
                }
            } catch { /* ignore */ }
        })();
        return () => { cancelled = true; };
    }, [selectedIds, datasetId]);

    // --- Layers ---------------------------------------------------------------
    const clusterLayer = useMemo<LayerProps>(() => ({
        id: 'clusters', type: 'circle', filter: ['has', 'cluster_count'],
        paint: {
            'circle-color': clusterColor,
            // Slightly more transparent than before since the circles now overlap when
            // they fill their cells.
            'circle-opacity': 0.6,
            // Sized to roughly FILL the cluster's grid cell (a cell is ~128px on screen by
            // design) so a cluster visibly COVERS its area — restoring the pre-server-
            // clustering behavior (clusterRadius=50 + ~40px circles) that the regression
            // migration shrank to a tiny dot. Grows with count (52-84 px). The server
            // sends the radius: the loose points within it count in the bubble instead of
            // being drawn under it (_map_bubble_radius_px in backend/routers/datasets.py).
            'circle-radius': ['get', 'radius'],
        }
    }), [clusterColor]);

    const clusterCountLayer = useMemo<LayerProps>(() => ({
        id: 'cluster-count', type: 'symbol', filter: ['has', 'cluster_count'],
        layout: {
            // ~3 significant digits with k/M suffix (e.g. 999, 1.23k, 15.2k, 218k,
            // 1.51M, 21.1M). Plain round/divide — number-format wasn't rounding here.
            'text-field': [
                'let', 'c', ['get', 'cluster_count'],
                ['case',
                    ['<', ['var', 'c'], 1000], ['to-string', ['var', 'c']],
                    ['<', ['var', 'c'], 10000], ['concat', ['to-string', ['/', ['round', ['/', ['var', 'c'], 10]], 100]], 'k'],
                    ['<', ['var', 'c'], 100000], ['concat', ['to-string', ['/', ['round', ['/', ['var', 'c'], 100]], 10]], 'k'],
                    ['<', ['var', 'c'], 1000000], ['concat', ['to-string', ['round', ['/', ['var', 'c'], 1000]]], 'k'],
                    ['<', ['var', 'c'], 10000000], ['concat', ['to-string', ['/', ['round', ['/', ['var', 'c'], 10000]], 100]], 'M'],
                    ['<', ['var', 'c'], 100000000], ['concat', ['to-string', ['/', ['round', ['/', ['var', 'c'], 100000]], 10]], 'M'],
                    ['concat', ['to-string', ['round', ['/', ['var', 'c'], 1000000]]], 'M']
                ]
            ],
            'text-font': ['DIN Offc Pro Medium', 'Arial Unicode MS Bold'],
            'text-size': 12,
        },
        paint: { 'text-color': '#ffffff' }
    }), []);

    const pointCircleLayer = useMemo<LayerProps>(() => ({
        id: 'unclustered-point-circle', type: 'circle',
        filter: ['all', ['has', 'id'], ['!', ['has', 'heading']]],
        paint: {
            'circle-opacity': 0.8, 'circle-radius': 4,
            'circle-color': ['case', ['boolean', ['feature-state', 'selected'], false], '#3b82f6', clusterColor],
        }
    }), [clusterColor]);

    const pointArrowLayer = useMemo<LayerProps>(() => ({
        id: 'unclustered-point-arrow', type: 'symbol',
        filter: ['all', ['has', 'id'], ['has', 'heading']],
        layout: {
            'icon-image': 'arrow', 'icon-size': 0.15, 'icon-rotate': ['get', 'heading'],
            'icon-allow-overlap': true, 'icon-ignore-placement': true, 'icon-rotation-alignment': 'map',
        },
        paint: {
            'icon-color': ['case', ['boolean', ['feature-state', 'selected'], false], '#3b82f6', clusterColor],
        }
    }), [clusterColor]);

    // --- MapboxDraw (lasso polygon) -------------------------------------------
    const [MapboxDrawClass, setMapboxDrawClass] = useState<typeof MapboxDraw | null>(null);
    const drawRef = useRef<MapboxDraw | null>(null);
    const [mapInstance, setMapInstance] = useState<mapboxgl.Map | null>(null);

    const onCreateRef = useRef<((e: MapboxDraw.DrawCreateEvent) => void) | undefined>(undefined);
    const onUpdateRef = useRef<((e: MapboxDraw.DrawUpdateEvent) => void) | undefined>(undefined);
    const onDeleteRef = useRef<((e: MapboxDraw.DrawDeleteEvent) => void) | undefined>(undefined);
    const onModeChangeRef = useRef<((e: MapboxDraw.DrawModeChangeEvent) => void) | undefined>(undefined);
    const onCreate = useCallback((e: MapboxDraw.DrawCreateEvent) => onCreateRef.current?.(e), []);
    const onUpdate = useCallback((e: MapboxDraw.DrawUpdateEvent) => onUpdateRef.current?.(e), []);
    const onDelete = useCallback((e: MapboxDraw.DrawDeleteEvent) => onDeleteRef.current?.(e), []);
    const onModeChange = useCallback((e: MapboxDraw.DrawModeChangeEvent) => onModeChangeRef.current?.(e), []);
    const setViewStateRef = useRef(setViewState);
    useEffect(() => { setViewStateRef.current = setViewState; }, [setViewState]);

    useEffect(() => {
        if (typeof window === 'undefined') return;
        import('@mapbox/mapbox-gl-draw').then((mod) => setMapboxDrawClass(() => mod.default))
            .catch(err => console.error('Failed to load MapboxDraw:', err));
    }, []);

    // Lasso handlers. Selection is applied SERVER-SIDE via the polygon (geo_polygon
    // filter) — we never ray-cast all points client-side (there is no full point set
    // in clustered mode, and the server filter is the source of truth).
    const fitToPolygon = useCallback((coords: number[][]) => {
        const LngLatBounds = mapboxgl.LngLatBounds;
        const bounds = coords.reduce((b: LngLatBounds, c) => b.extend(c as [number, number]), new LngLatBounds());
        mapRef.current?.fitBounds(bounds, { padding: 50, maxZoom: 17, duration: 800 });
    }, []);

    // Fit to the actual DATA inside the drawn polygon — the true server-side extent for
    // the current filters + this polygon — not the drawn shape. Restores the pre-server-
    // clustering behavior where lassoing zoomed INTO the data (a loose lasso around a tight
    // cluster zooms to the cluster, not the empty area you circled). Falls back to the shape
    // if the region has no data or the lookup fails.
    const fitToDataInPolygon = useCallback(async (coords: number[][]) => {
        try {
            const base = (filters?.filters || []).filter(
                (f) => f?.operator !== 'geo_polygon' && f?.operator !== 'geo_polygon_exclude'
            );
            const combined: ComplexFilter = {
                logic: 'AND',
                filters: [...base, { field: 'location', operator: 'geo_polygon', value: coords }],
            };
            const ext = await getMapExtent(datasetId, combined);
            if (mapRef.current && ext.count && ext.w != null && ext.e != null && ext.s != null && ext.n != null) {
                let { w, e, s, n } = ext as { w: number; e: number; s: number; n: number };
                if (e - w < 1e-6) { w -= 0.0005; e += 0.0005; }
                if (n - s < 1e-6) { s -= 0.0005; n += 0.0005; }
                const LngLatBounds = mapboxgl.LngLatBounds;
                mapRef.current.fitBounds(new LngLatBounds([w, s], [e, n]), { padding: 60, maxZoom: 17, duration: 800 });
                return;
            }
        } catch { /* fall back to the drawn shape */ }
        fitToPolygon(coords);
    }, [datasetId, filters, fitToPolygon]);

    useEffect(() => {
        onCreateRef.current = (e: MapboxDraw.DrawCreateEvent) => {
            const draw = drawRef.current;
            if (!draw) return;
            const polygon = e.features[0];
            onSelection([], polygon);  // apply as a server-side geo_polygon filter (scopes grid + map)
            try { fitToDataInPolygon((polygon.geometry as Polygon).coordinates[0]); } catch { /* ignore */ }
            // The outline is transient: drop it once the region is applied so you can keep
            // drilling into sub-regions. deleteAll() is programmatic — it does NOT fire
            // draw.delete, so the applied filter (currentPolygon) persists.
            try { draw.deleteAll(); } catch { /* ignore */ }
            setIsDrawing(false);
        };
        onUpdateRef.current = (e: MapboxDraw.DrawUpdateEvent) => {
            const draw = drawRef.current;
            if (!draw || !e.features?.length) return;
            const poly = draw.getAll().features[0];
            onSelection([], poly);
            try { fitToDataInPolygon((poly.geometry as Polygon).coordinates[0]); } catch { /* ignore */ }
        };
        onDeleteRef.current = () => onSelection([]);
        onModeChangeRef.current = (e: MapboxDraw.DrawModeChangeEvent) => setIsDrawing(e.mode === 'draw_polygon');
    }, [onSelection, fitToDataInPolygon]);

    const onMapLoad = useCallback((e: MapEvent) => {
        const map = e.target;
        const size = 128;
        const arrowImage = new Image(size, size);
        arrowImage.onload = () => { if (!map.hasImage('arrow')) map.addImage('arrow', arrowImage, { sdf: true }); };
        arrowImage.onerror = (err) => console.error('Failed to load arrow image', err);
        arrowImage.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(`
            <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M64 16 L24 112 L64 96 L104 112 L64 16 Z" fill="white" />
            </svg>`);
        // Seed the initial viewport so the first fetch happens.
        captureView();

        const handleKeyDown = (ev: KeyboardEvent) => {
            if (!drawRef.current) return;
            if (ev.key === 'Enter' && isDrawing && drawRef.current.getMode() === 'draw_polygon') {
                drawRef.current.changeMode('simple_select'); setIsDrawing(false); ev.preventDefault();
            }
            if (ev.key === 'Escape' && isDrawing) {
                drawRef.current.deleteAll(); drawRef.current.changeMode('simple_select');
                setIsDrawing(false); onSelection([], null); ev.preventDefault();
            }
            if ((ev.key === 'Delete' || ev.key === 'Backspace') && drawRef.current) {
                try {
                    const selected = drawRef.current.getSelected();
                    if (selected.features.length > 0) {
                        drawRef.current.delete(selected.features.map((f) => f.id as string));
                        onSelection([], null); ev.preventDefault();
                    }
                } catch { /* draw not ready */ }
            }
        };
        document.addEventListener('keydown', handleKeyDown);
        return () => document.removeEventListener('keydown', handleKeyDown);
    }, [isDrawing, onSelection, captureView]);

    // Placing a polygon vertex on a cluster bubble also flew the map in 2.5 zoom levels mid-draw
    //: while drawing, a click belongs to the polygon.
    const isDrawingPolygon = () => {
        const draw = drawRef.current;
        return !!draw && typeof draw.getMode === 'function' && draw.getMode() === 'draw_polygon';
    };

    const onMapClick = (event: MapMouseEvent) => {
        if (event.defaultPrevented || !mapRef.current) return;
        if (isDrawingPolygon()) return;
        const bbox: [[number, number], [number, number]] = [[event.point.x - 5, event.point.y - 5], [event.point.x + 5, event.point.y + 5]];
        const feature = mapRef.current.queryRenderedFeatures(bbox)
            .find(f => ['clusters', 'cluster-count'].includes(f.layer?.id || ''));
        if (feature) {  // cluster click -> zoom in (server re-clusters finer)
            flyToCluster((feature.geometry as Point).coordinates as [number, number]);
            return;
        }
        // Background click: start drawing a lasso (if not already). Keep the current
        // region active while drawing the next one — completing it replaces the filter —
        // so drilling down doesn't flash unfiltered data between regions. (Clearing is
        // still available via the region chip's ✕, or Escape to cancel a draw.)
        const draw = drawRef.current;
        if (!draw) return;
        try {
            if (typeof draw.getMode === 'function' && draw.getMode() === 'draw_polygon') return;
            if (typeof draw.changeMode === 'function') { draw.changeMode('draw_polygon'); setIsDrawing(true); }
        } catch (err) { console.error('Error starting drawing mode:', err); }
    };

    useEffect(() => {
        if (!mapInstance || !MapboxDrawClass || drawRef.current) return;
        try {
            const draw = new MapboxDrawClass({
                displayControlsDefault: false, controls: { polygon: false, trash: false }, defaultMode: 'simple_select',
            });
            mapInstance.addControl(draw, 'top-left');
            drawRef.current = draw;
            mapInstance.on('draw.create', onCreate);
            mapInstance.on('draw.update', onUpdate);
            mapInstance.on('draw.delete', onDelete);
            mapInstance.on('draw.modechange', onModeChange);
            const onClusterClick = (e: MapMouseEvent) => {
                if (isDrawingPolygon()) return;  // the click is a polygon vertex
                e.preventDefault();
                const f = e.features?.[0];
                if (f) flyToCluster((f.geometry as Point).coordinates as [number, number]);
            };
            mapInstance.on('click', 'clusters', onClusterClick);
            mapInstance.on('click', 'cluster-count', onClusterClick);
            const onEnter = () => mapInstance.getCanvas().style.cursor = 'pointer';
            const onLeave = () => mapInstance.getCanvas().style.cursor = '';
            ['clusters', 'cluster-count'].forEach(l => { mapInstance.on('mouseenter', l, onEnter); mapInstance.on('mouseleave', l, onLeave); });
            return () => {
                try {
                    mapInstance.off('draw.create', onCreate); mapInstance.off('draw.update', onUpdate);
                    mapInstance.off('draw.delete', onDelete); mapInstance.off('draw.modechange', onModeChange);
                    mapInstance.off('click', 'clusters', onClusterClick); mapInstance.off('click', 'cluster-count', onClusterClick);
                    ['clusters', 'cluster-count'].forEach(l => { mapInstance.off('mouseenter', l, onEnter); mapInstance.off('mouseleave', l, onLeave); });
                    if (mapInstance.hasControl(draw)) mapInstance.removeControl(draw);
                } catch (e) { console.warn('Error cleaning up MapboxDraw:', e); }
                drawRef.current = null;
            };
        } catch (err) { console.error('Failed to initialize MapboxDraw:', err); }
    }, [MapboxDrawClass, mapInstance, onCreate, onUpdate, onDelete, onModeChange, flyToCluster]);

    if (!mounted) return <div className="h-full w-full bg-zinc-950" />;

    return (
        <div ref={containerRef} className="h-full w-full min-h-0 relative group">
            {!resp && isError && (
                <div className="absolute inset-0 flex items-center justify-center bg-zinc-900/80 backdrop-blur-sm z-40">
                    <div className="text-center">
                        <p className="text-red-300 font-medium mb-3">Failed to load map data</p>
                        <button
                            onClick={() => refetch()}
                            className="px-3 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm font-medium"
                        >
                            Retry
                        </button>
                    </div>
                </div>
            )}
            {!resp && !isError && (
                <div className="absolute inset-0 flex items-center justify-center bg-zinc-900/80 backdrop-blur-sm z-40">
                    <div className="text-center">
                        <div className="w-12 h-12 border-4 border-cyan-500 border-t-transparent rounded-full animate-spin mx-auto mb-3"></div>
                        <p className="text-white font-medium">Loading map data...</p>
                    </div>
                </div>
            )}
            {showBusy && (
                <div className="absolute top-3 left-1/2 -translate-x-1/2 z-40 flex items-center gap-2 bg-zinc-900/90 text-xs text-zinc-200 border border-zinc-700 rounded-full px-3 py-1.5 shadow-md pointer-events-none">
                    <div className="w-3.5 h-3.5 border-2 border-cyan-400 border-t-transparent rounded-full animate-spin"></div>
                    Loading…
                </div>
            )}

            <Map
                ref={mapRef}
                {...viewState}
                onMove={evt => setViewState(evt.viewState)}
                onMoveEnd={handleMoveEnd}
                onLoad={(e) => { onMapLoad(e); setMapInstance(e.target); }}
                mapStyle="mapbox://styles/mapbox/dark-v11"
                mapboxAccessToken={process.env.NEXT_PUBLIC_MAPBOX_TOKEN}
                onClick={onMapClick}
                interactiveLayerIds={['clusters', 'cluster-count', 'unclustered-point-circle', 'unclustered-point-arrow']}
                cursor={isDrawing ? 'crosshair' : 'auto'}
                projection={{ name: 'mercator' }}
                style={{ width: '100%', height: '100%' }}
            >
                <ScaleControl />
                <Source id="points" type="geojson" data={geojson} promoteId="id">
                    <Layer {...clusterLayer} />
                    <Layer {...clusterCountLayer} />
                    <Layer {...pointCircleLayer} />
                    <Layer {...pointArrowLayer} />
                </Source>
            </Map>

            <div className="absolute top-3 right-3 flex flex-col items-end gap-1">
                <div className="flex gap-2">
                    <button
                        onClick={() => setForceMode(showingPoints ? 'auto' : 'points')}
                        className={`text-xs border rounded px-3 py-1 shadow-sm ${showingPoints
                            ? 'bg-cyan-700/80 hover:bg-cyan-600 text-white border-cyan-600'
                            : 'bg-zinc-900/80 hover:bg-zinc-800 text-zinc-200 border-zinc-700'}`}
                        title={showingPoints ? 'Cluster the map' : 'Show individual points'}
                    >
                        {showingPoints ? 'Cluster' : 'De-cluster view'}
                    </button>
                    <button
                        onClick={zoomToData}
                        className="bg-zinc-900/80 hover:bg-zinc-800 text-xs text-zinc-200 border border-zinc-700 rounded px-3 py-1 shadow-sm"
                        title="Zoom to data extent"
                    >
                        Zoom to Data
                    </button>
                    <button
                        onClick={refitToContainer}
                        className="bg-zinc-900/80 hover:bg-zinc-800 text-xs text-zinc-200 border border-zinc-700 rounded px-3 py-1 shadow-sm"
                        title="Resize map to panel"
                    >
                        Resize Map
                    </button>
                </div>
                {msg && (
                    <div className="bg-zinc-900/90 text-[11px] text-amber-300 border border-zinc-700 rounded px-2 py-1 shadow-sm max-w-[260px] text-right">
                        {msg}
                    </div>
                )}
            </div>

            {/* Whole-dataset "no locations" empty state — filters may legitimately empty the
                map, so this only shows when coverage says 0 of N samples have a location. */}
            {coverage && coverage.location.present === 0 && coverage.total_samples > 0 && items.length === 0 && (
                <div className="absolute inset-0 flex items-center justify-center pointer-events-none" data-testid="map-empty-no-locations">
                    <div className="bg-zinc-900/90 border border-zinc-700 rounded-lg p-4 text-center max-w-sm pointer-events-auto">
                        <div className="text-sm text-zinc-300 mb-2">
                            {coverage.total_objects
                                ? `0 of ${(coverage.total_samples - coverage.total_objects).toLocaleString()} images have a location`
                                : `0 of ${coverage.total_samples.toLocaleString()} samples have a location`}
                        </div>
                        <div className="text-xs text-zinc-500">
                            Locations are set at ingest — pass{' '}
                            <code className="text-zinc-400">location=(lon, lat)</code> on each{' '}
                            <code className="text-zinc-400">Sample</code> in{' '}
                            <code className="text-zinc-400">add_samples()</code>
                        </div>
                    </div>
                </div>
            )}

            {!process.env.NEXT_PUBLIC_MAPBOX_TOKEN && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/80 text-white p-4 text-center">
                    <div>
                        <p className="font-bold text-red-500 mb-2">Missing Mapbox Token</p>
                        <p className="text-sm">Please add NEXT_PUBLIC_MAPBOX_TOKEN to your .env.local file.</p>
                    </div>
                </div>
            )}
        </div>
    );
}
