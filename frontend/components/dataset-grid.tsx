'use client';

import { type Sample, type ComplexFilter, type SegmentationConfig, getSamples, isRescoringRefusal, RESCORING_RETRY_MS, SAMPLES_MUTATED_EVENT } from '@/lib/api';
import { getSampleMediaUrl } from '@/lib/media-utils';
import { useRef, useState, useEffect, useLayoutEffect, useMemo, useCallback, memo } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useVirtualizer } from '@tanstack/react-virtual';
import * as Slider from '@radix-ui/react-slider';
import { AnnotationOverlay } from './annotations/AnnotationOverlay';
import { PatchGridOverlay } from './annotations/PatchGridOverlay';
import { RetryImg } from './retry-img';
import { resetSampleCount, useSampleCount } from '@/hooks/useSampleCount';
import { parseAnnotations, isOverlayActive, isDerivedLabelField } from '@/lib/labels';
import type { PatchInfo } from '@/hooks/usePatchState';

interface GridCellProps {
    sample: Sample;
    imageSrc: string;
    isSelected: boolean;
    activeOverlayFields: string[];
    segmentationConfig?: SegmentationConfig;
    overlayConfigs?: Record<string, { colormap?: string; range?: [number, number]; opacity?: number } | null>;
    gridPatchMode?: string | null;
    samplePatches?: PatchInfo[];
    highlightBox?: [number, number, number, number] | null;  // outline the matching object on this image
    onPatchClick?: (row: number, col: number, scale: string) => void;
    onClick: (e: React.MouseEvent) => void;
    onCheckboxClick: (e: React.MouseEvent) => void;
    onOpen?: () => void;  // open the sample modal (reachable even when the patch overlay eats clicks)
    textOverlayFields: string[];
}

const GridCell = memo(function GridCell({
    sample, imageSrc, isSelected,
    activeOverlayFields, segmentationConfig, overlayConfigs,
    gridPatchMode, samplePatches, highlightBox, onPatchClick,
    onClick, onCheckboxClick, onOpen, textOverlayFields,
}: GridCellProps) {
    const hasPatchOverlay = gridPatchMode || (samplePatches && samplePatches.length > 0);

    // Heatmaps are already baked into `imageSrc` server-side (see getImageSrc).
    // Strip them from the overlay list passed to AnnotationOverlay so the
    // HeatmapRenderer canvas doesn't double-paint on top of the composite.
    const overlaysWithoutHeatmaps = useMemo(
        () => activeOverlayFields.filter(f => {
            const v = sample.metadata?.[f];
            return !(v && typeof v === 'object' && v.type === 'heatmap');
        }),
        [activeOverlayFields, sample.metadata],
    );

    // Decide from this sample's annotation types and the field's own toggle.
    const cellShowAnnotations = useMemo(
        () => parseAnnotations(sample.metadata || {}).some(ann =>
            ann.type !== 'heatmap' && isOverlayActive(ann.fieldName, activeOverlayFields, sample.metadata)),
        [activeOverlayFields, sample.metadata],
    );

    // Object-valued overlay fields (masks, heatmaps) render visually, not as a
    // raw "[object Object]" text label — keep only scalar/array text overlays.
    const cellTextOverlays = useMemo(
        () => textOverlayFields.filter(f => {
            const v = sample.metadata?.[f];
            return !isDerivedLabelField(f, sample.metadata) && !(v && typeof v === 'object');
        }),
        [textOverlayFields, sample.metadata],
    );

    return (
        <div className="aspect-square select-none" data-testid="grid-cell" data-selected={isSelected ? 'true' : 'false'}>
            <div
                className={`w-full h-full relative cursor-pointer group bg-zinc-900 border rounded-lg overflow-hidden transition-all ${isSelected
                    ? 'border-cyan-500 ring-2 ring-cyan-500 ring-offset-2 ring-offset-zinc-950'
                    : 'border-zinc-800 hover:border-zinc-700 hover:scale-[1.02]'
                    }`}
                onClick={onClick}
            >
                {/* Image rendering. A highlightBox (the matching object on a ranked parent image)
                    also routes through AnnotationOverlay, which draws the image + the box even when
                    no annotation overlays are active. */}
                {cellShowAnnotations || highlightBox ? (
                    <AnnotationOverlay
                        imageSrc={imageSrc}
                        sampleId={sample.id}
                        metadata={sample.metadata || {}}
                        className="w-full h-full"
                        activeOverlayFields={overlaysWithoutHeatmaps}
                        overlayConfigs={overlayConfigs}
                        compactOverlays
                        segmentationConfig={segmentationConfig}
                        highlightBox={highlightBox ?? null}
                    />
                ) : hasPatchOverlay ? (
                    <div className="w-full h-full flex items-center justify-center bg-zinc-800">
                        <div className="relative max-w-full max-h-full" style={{ aspectRatio: '16/9' }}>
                            <RetryImg
                                src={imageSrc}
                                alt=""
                                className="w-full h-full"
                                loading="lazy"
                                onLoad={(e) => {
                                    const img = e.target as HTMLImageElement;
                                    if (img.naturalWidth > 0) {
                                        const wrapper = img.parentElement;
                                        if (wrapper) wrapper.style.aspectRatio = `${img.naturalWidth}/${img.naturalHeight}`;
                                    }
                                }}
                                // Retries exhausted: a failed media load may mean the session expired.
                                onError={() => window.dispatchEvent(new Event('fridge:check-session'))}
                            />
                            {gridPatchMode && (
                                <PatchGridOverlay
                                    scale={gridPatchMode}
                                    selectedPatches={samplePatches?.filter(p => p.scale === gridPatchMode)}
                                    onPatchClick={(row, col) => onPatchClick?.(row, col, gridPatchMode)}
                                    mode="select"
                                />
                            )}
                            {!gridPatchMode && samplePatches?.map(sp => (
                                <PatchGridOverlay key={`${sp.row}-${sp.col}`}
                                    scale={sp.scale}
                                    highlightedPatch={{ row: sp.row, col: sp.col }}
                                    isGridSelected={true} mode="highlight" />
                            ))}
                        </div>
                    </div>
                ) : (
                    <RetryImg
                        src={imageSrc}
                        alt=""
                        className="w-full h-full object-contain bg-zinc-800"
                        loading="lazy"
                        // Retries exhausted (see RetryImg): a failed media load may mean the session
                        // expired (these <img> 401s bypass react-query). Ask the watchdog to verify.
                        onError={() => window.dispatchEvent(new Event('fridge:check-session'))}
                    />
                )}

                {/* Selection overlay */}
                <div className={`absolute inset-0 transition-colors ${isSelected ? 'bg-cyan-500/10' : 'bg-black/0 group-hover:bg-black/20'}`} />

                {/* Open the sample modal — only shown in a patch view, where the patch
                    overlay intercepts the image click (so the cell's onClick can't open it).
                    Outside patch mode a normal image click already opens the modal, so the
                    eye would just be redundant clutter. */}
                {onOpen && hasPatchOverlay && (
                    <button
                        type="button"
                        title="Open image"
                        onClick={(e) => { e.stopPropagation(); onOpen(); }}
                        className="absolute top-2 left-2 w-6 h-6 rounded-full border-2 border-white/50 bg-black/40 text-white flex items-center justify-center opacity-0 group-hover:opacity-100 hover:bg-black/60 hover:border-white transition-all z-20"
                    >
                        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                        </svg>
                    </button>
                )}

                {/* Selection checkbox */}
                <div
                    data-testid="cell-checkbox"
                    className={`absolute top-2 right-2 w-6 h-6 rounded-full border-2 flex items-center justify-center transition-all z-10 ${isSelected
                        ? 'bg-cyan-500 border-cyan-500 opacity-100'
                        : 'bg-black/40 border-white/50 opacity-0 group-hover:opacity-100 hover:bg-black/60 hover:border-white'
                        }`}
                    onClick={(e) => { e.stopPropagation(); onCheckboxClick(e); }}
                >
                    {isSelected && (
                        <svg className="w-3.5 h-3.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                        </svg>
                    )}
                </div>

                {/* Text overlay fields */}
                {cellTextOverlays.length > 0 && (
                    <div className="absolute bottom-0 left-0 right-0 flex flex-col gap-0.5 p-1 pointer-events-none">
                        {cellTextOverlays.map(field => {
                            // Check top-level fields first (tags, id, filepath), then metadata
                            const val: unknown = field === 'id' ? sample.id
                                : field === 'filepath' ? sample.filepath
                                : (sample as unknown as Record<string, unknown>)[field] ?? sample.metadata?.[field];

                            if (Array.isArray(val) && val.length > 0) {
                                return (
                                    <div key={field} className="bg-black/60 backdrop-blur-sm px-1.5 py-0.5 rounded max-w-full">
                                        <span className="text-[10px] text-white/50 font-mono mr-1">{field}:</span>
                                        <div className="flex flex-wrap gap-0.5 mt-0.5">
                                            {val.map((v: string) => (
                                                <span key={v} className="text-[9px] bg-white/15 text-white rounded px-1 py-px font-mono">{v}</span>
                                            ))}
                                        </div>
                                    </div>
                                );
                            }

                            return (
                                <div key={field} className="bg-black/60 backdrop-blur-sm px-1.5 py-0.5 rounded w-fit max-w-full truncate">
                                    <span className="text-[10px] text-white font-mono">
                                        <span className="opacity-50 mr-1">{field}:</span>
                                        {String(val ?? '-')}
                                    </span>
                                </div>
                            );
                        })}
                    </div>
                )}
            </div>
        </div>
    );
});

export interface GridMore {
    hasMore: () => boolean;
    loadNextPage: () => Promise<Sample[]>;
}

interface Props {
    datasetId: number;
    filters: ComplexFilter;
    filtersReady?: boolean;  // false: the filters' final shape is not known yet; send no count or page
    total: number;
    initialSamples: Sample[];
    activeMediaField?: string;
    activeOverlayFields?: string[];
    overlayConfigs?: Record<string, { colormap?: string; range?: [number, number]; opacity?: number } | null>;
    segmentationConfig?: SegmentationConfig;
    onSampleClick: (sample: Sample) => void;
    selectedIds?: number[];
    selectAllActive?: boolean;   // Select-All (token) mode: all rows selected minus deselectedIds
    deselectedIds?: number[];
    onToggleSelection?: (id: number) => void;
    onRangeSelection?: (ids: number[], select?: boolean) => void;
    sortBy?: string;
    sortOrder?: 'asc' | 'desc';
    rankOrderIds?: number[];  // similarity/patch search: render results in this rank order
    rankByParent?: boolean;  // rankOrderIds holds IMAGE ids; rank these OBJECT rows by their parent
    highlightBoxes?: Map<number, [number, number, number, number]>;  // imageId -> crop_bbox to outline
    imageScale?: number;
    selectedPatches?: Map<number, Array<{ row: number; col: number; scale: string }>>;
    onPatchSelect?: (sampleId: number, row: number, col: number, scale: string) => void;
    gridPatchMode?: string | null;  // e.g. "2x2", "4x4" — draws grid overlay on all images
    gridSamplesRef?: React.MutableRefObject<Sample[]>;
    // null while the COUNT is in flight, then the real total — 0 included.
    onTotalChange?: (total: number | null) => void;
    // Lets the sample modal walk past the rows loaded so far (see gridMoreRef in page.tsx).
    gridMoreRef?: React.MutableRefObject<GridMore | null>;
}

const PAGE_SIZE = 200;

// useLayoutEffect on the client (measure before paint → no flash), useEffect on the
// server (avoids the SSR warning).
const useIsoLayoutEffect = typeof window !== 'undefined' ? useLayoutEffect : useEffect;

// Rows of cells rendered BEYOND the viewport in each direction. Deliberately large
// so normal scrolling never reveals a blank cell — virtualization stays invisible —
// while still bounding DOM/memory (vs. keeping every scrolled-past image mounted,
// which is what OOM'd the tab on a multi-million-row dataset).
const OVERSCAN_ROWS = 24;

// Cap the random-access page cache (metadata only — the DOM is already bounded by
// virtualization). 200 pages × 200 = 40k samples kept; far more than any viewport
// needs, but prevents unbounded growth when scrubbing all over a huge dataset.
const MAX_CACHED_PAGES = 200;

// Browsers cap an element's height (~17M px Firefox, ~33M Chrome), so a scroll
// container can't physically span millions of rows. We virtualize only a WINDOW
// of rows that fits under this safe ceiling, and the Jump bar slides that window
// through the full dataset — so any position is reachable even though a single
// native scrollbar can't represent all of it.
const SAFE_MAX_PX = 15_000_000;

// A "jump to position" bar shown above the grid for datasets too large to fit in
// one window. Drag it to scrub anywhere in the dataset; the grid loads that
// region and you scroll normally around it. The thumb tracks the live scroll
// position (except while you're dragging it).
function JumpBar({ fraction, total, onJump }: {
    fraction: number;
    total: number;
    onJump: (fraction: number) => void;
}) {
    const [val, setVal] = useState(fraction);
    const dragging = useRef(false);
    useEffect(() => { if (!dragging.current) setVal(fraction); }, [fraction]);
    // eslint-disable-next-line react-hooks/refs -- intentional: show local value while dragging, prop value otherwise
    const shown = dragging.current ? val : fraction;
    const approxIndex = Math.round(shown * Math.max(0, total - 1)) + 1;
    return (
        <div className="flex items-center gap-3 px-4 py-2 border-b border-zinc-800 bg-zinc-900/80 shrink-0">
            <span className="text-xs text-zinc-400 whitespace-nowrap">Jump</span>
            <Slider.Root
                className="relative flex items-center select-none touch-none grow h-5"
                min={0}
                max={1}
                step={0.0005}
                value={[shown]}
                onValueChange={([v]) => { dragging.current = true; setVal(v); }}
                onValueCommit={([v]) => { dragging.current = false; onJump(v); }}
            >
                <Slider.Track className="bg-zinc-800 relative grow rounded-full h-1">
                    <Slider.Range className="absolute bg-cyan-500 rounded-full h-full" />
                </Slider.Track>
                <Slider.Thumb className="block w-4 h-4 bg-cyan-500 rounded-full hover:bg-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-500" />
            </Slider.Root>
            <span className="text-xs text-zinc-400 font-mono whitespace-nowrap tabular-nums">
                {Math.round(shown * 100)}% · {approxIndex.toLocaleString()} / {total.toLocaleString()}
            </span>
        </div>
    );
}

export function DatasetGrid({ datasetId, filters, filtersReady = true, activeMediaField = 'filepath', activeOverlayFields = [], overlayConfigs, segmentationConfig, onSampleClick, selectedIds = [], selectAllActive = false, deselectedIds = [], onToggleSelection, onRangeSelection, sortBy, sortOrder, rankOrderIds, rankByParent, highlightBoxes, imageScale = 50, selectedPatches, onPatchSelect, gridPatchMode, gridSamplesRef, onTotalChange, gridMoreRef }: Props) {
    const scrollRef = useRef<HTMLDivElement>(null);
    const [containerWidth, setContainerWidth] = useState(0);
    // Absolute sample index anchored at the top of the rendered window (moved by
    // the Jump bar). Stored in samples (not rows) so it survives column-count /
    // zoom changes.
    const [windowAnchorSample, setWindowAnchorSample] = useState(0);

    const selectedIdSet = useMemo(() => new Set(selectedIds), [selectedIds]);
    const deselectedIdSet = useMemo(() => new Set(deselectedIds), [deselectedIds]);
    // Select-All mode: every row is selected except the exceptions; otherwise use the id set.
    const isSampleSelected = useCallback(
        (id: number) => (selectAllActive ? !deselectedIdSet.has(id) : selectedIdSet.has(id)),
        [selectAllActive, deselectedIdSet, selectedIdSet],
    );
    const [lastSelectedIndex, setLastSelectedIndex] = useState<number | null>(null);

    const textOverlayFields = activeOverlayFields;

    // Calculate grid columns from image scale
    const minSize = 100;
    const maxSize = 600;
    const targetSize = minSize + (imageScale / 100) * (maxSize - minSize);

    // --- Random-access page cache --------------------------------------------
    // The grid is virtualized over the WHOLE (filtered) dataset, not just the
    // pages loaded so far — so the scrollbar spans everything and you can scrub
    // straight to, say, 90% and that region loads on demand (an offset fetch).
    // Pages are cached by index; a cell whose page isn't loaded yet shows a
    // skeleton until it arrives.
    // Bumped when a mutation (tag/revert/bulk-set) fires notifySamplesMutated for
    // this dataset. Folded into queryKeyStr so the page cache resets and refetches
    // (the count is asked again by useSampleCount, which follows the same signal).
    const [mutationVersion, setMutationVersion] = useState(0);
    useEffect(() => {
        const onMutated = (e: Event) => {
            if ((e as CustomEvent<{ datasetId: number }>).detail?.datasetId === datasetId) {
                setMutationVersion(v => v + 1);
            }
        };
        window.addEventListener(SAMPLES_MUTATED_EVENT, onMutated);
        return () => window.removeEventListener(SAMPLES_MUTATED_EVENT, onMutated);
    }, [datasetId]);

    const queryKeyStr = useMemo(
        () => JSON.stringify([datasetId, filters ?? null, sortBy ?? null, sortOrder ?? null, rankOrderIds ?? null, rankByParent ?? null, mutationVersion]),
        [datasetId, filters, sortBy, sortOrder, rankOrderIds, rankByParent, mutationVersion]
    );

    // Total count drives the scrollbar length (fast COUNT, runs in parallel and
    // does not block first paint — page 0 is kicked immediately below). The same
    // query as the top bar's when both ask the same thing, so one request answers both.
    const queryClient = useQueryClient();
    const { data: countData, error: countError, failureReason: countFailure } = useSampleCount(datasetId, filters, { enabled: filtersReady });
    // The first page's failure, for the current query only: without it a refused view would sit on
    // "Loading samples..." because page errors are otherwise left for a later scroll to retry.
    const [firstPageError, setFirstPageError] = useState<{ key: string; message: string; refusal: boolean } | null>(null);

    const pagesRef = useRef<Map<number, Sample[]>>(new Map());
    const fetchingRef = useRef<Set<number>>(new Set());
    const fetchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const [pageVersion, setPageVersion] = useState(0);

    // Latest query params, read inside the (stable) fetcher so it never goes stale,
    // and so a response from a superseded filter/sort can be discarded.
    const paramsRef = useRef({ datasetId, filters, sortBy, sortOrder, rankOrderIds, rankByParent, key: queryKeyStr });
    paramsRef.current = { datasetId, filters, sortBy, sortOrder, rankOrderIds, rankByParent, key: queryKeyStr };

    const fetchPage = useCallback((p: number) => {
        if (p < 0 || pagesRef.current.has(p) || fetchingRef.current.has(p)) return;
        const { datasetId, filters, sortBy, sortOrder, rankOrderIds, rankByParent, key } = paramsRef.current;
        fetchingRef.current.add(p);
        getSamples(datasetId, PAGE_SIZE, p * PAGE_SIZE, filters, sortBy, sortOrder, true, undefined, undefined, rankOrderIds, rankByParent)
            .then(res => {
                if (paramsRef.current.key !== key) return;   // filters/sort changed → discard
                pagesRef.current.set(p, res.items);
                // Evict the pages farthest from the one just fetched (which is in
                // view) so the cache stays bounded during heavy scrubbing.
                if (pagesRef.current.size > MAX_CACHED_PAGES) {
                    const farthest = [...pagesRef.current.keys()]
                        .sort((a, b) => Math.abs(b - p) - Math.abs(a - p))
                        .slice(0, pagesRef.current.size - MAX_CACHED_PAGES);
                    for (const k of farthest) pagesRef.current.delete(k);
                }
                setPageVersion(v => v + 1);
            })
            .catch((error: unknown) => {
                // Leave unfetched; a later scroll retries. A failed first page is shown instead.
                if (p === 0 && paramsRef.current.key === key) {
                    setFirstPageError({
                        key,
                        message: error instanceof Error ? error.message : 'Request failed',
                        refusal: isRescoringRefusal(error),
                    });
                }
            })
            .finally(() => { fetchingRef.current.delete(p); });
    }, []);

    // Reset the cache (and scroll to top) whenever the query identity changes, and
    // kick page 0 immediately so the first screen paints without waiting on COUNT.
    useEffect(() => {
        pagesRef.current = new Map();
        fetchingRef.current = new Set();
        setPageVersion(v => v + 1);
        setWindowAnchorSample(0);
        // The shift-click anchor is an index into the OLD ordering — after a sort/filter change a
        // shift-click ranged from a stale position (jumpTo already resets it).
        setLastSelectedIndex(null);
        scrollRef.current?.scrollTo({ top: 0 });
        if (filtersReady) fetchPage(0);
    }, [queryKeyStr, fetchPage, filtersReady]);

    const totalCount = countData ?? 0;
    // Before COUNT resolves, fall back to page 0's length so the first screen shows.
    const effectiveCount = totalCount > 0 ? totalCount : (pagesRef.current.get(0)?.length ?? 0);

    const getSampleAt = useCallback((index: number): Sample | undefined => {
        return pagesRef.current.get(Math.floor(index / PAGE_SIZE))?.[index % PAGE_SIZE];
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [pageVersion]);

    const idsInRange = useCallback((a: number, b: number): number[] => {
        const lo = Math.min(a, b), hi = Math.max(a, b);
        const ids: number[] = [];
        for (let i = lo; i <= hi; i++) { const s = getSampleAt(i); if (s) ids.push(s.id); }
        return ids;
    }, [getSampleAt]);

    // Shift-range selection that doesn't silently drop samples whose pages aren't
    // cached: if any index in [lo,hi] is uncached, fetch the ids for the whole
    // range via the slim id-only path before applying the selection.
    const selectRange = useCallback(async (a: number, b: number, additive: boolean) => {
        if (!onRangeSelection) return;
        const lo = Math.min(a, b), hi = Math.max(a, b);
        // Guard accidental huge ranges (e.g. click index 0, jump to the end, shift-
        // click): fetching a multi-hundred-k id list + deduping on the main thread
        // would freeze the tab. Past a threshold require explicit confirmation — the
        // full range stays reachable (opt-in), it's just not silent.
        const MAX_RANGE_SELECT = 50000;
        if (hi - lo + 1 > MAX_RANGE_SELECT && typeof window !== 'undefined' &&
            !window.confirm(`Select ${(hi - lo + 1).toLocaleString()} samples? Large selections may be slow.`)) {
            return;
        }
        const cached: number[] = [];
        let complete = true;
        for (let i = lo; i <= hi; i++) {
            const s = getSampleAt(i);
            if (s) { cached.push(s.id); } else { complete = false; break; }
        }
        if (complete) { onRangeSelection(cached, additive); return; }
        // Fetch the uncached window in the SAME order the grid renders — including an active
        // similarity/patch rank (rankOrderIds/rankByParent). Without them the server falls back to
        // ORDER BY id and the [lo,hi] window is a different, off-screen set than what's displayed,
        // so a shift-select grabs the wrong ids.
        const { datasetId, filters, sortBy, sortOrder, rankOrderIds, rankByParent } = paramsRef.current;
        try {
            const res = await getSamples(datasetId, hi - lo + 1, lo, filters, sortBy, sortOrder, true, true, 'id', rankOrderIds, rankByParent);
            onRangeSelection(res.items.map(s => s.id), additive);
        } catch {
            onRangeSelection(idsInRange(lo, hi), additive);  // fall back to cached
        }
    }, [getSampleAt, idsInRange, onRangeSelection]);

    // Loaded samples in index order — used by the parent modal (prev/next) and the
    // selection helpers. Spans only what's currently cached.
    // Only the LONGEST contiguous run of cached pages. After a jump-to-position
    // the cache can briefly hold non-contiguous islands (e.g. page 0 plus a
    // window around the jump); concatenating all of them made modal prev/next
    // silently skip across the gap to the wrong sample. Restricting
    // to one contiguous run keeps prev/next on truly adjacent samples.
    const contiguousRun = useCallback((): { pages: number[]; samples: Sample[] } => {
        const keys = [...pagesRef.current.keys()].sort((a, b) => a - b);
        if (keys.length === 0) return { pages: [], samples: [] };
        let bestStart = 0, bestLen = 1, curStart = 0, curLen = 1;
        for (let i = 1; i < keys.length; i++) {
            if (keys[i] === keys[i - 1] + 1) {
                curLen++;
            } else {
                curStart = i; curLen = 1;
            }
            if (curLen > bestLen) { bestLen = curLen; bestStart = curStart; }
        }
        const pages = keys.slice(bestStart, bestStart + bestLen);
        const samples: Sample[] = [];
        for (const k of pages) for (const s of pagesRef.current.get(k)!) samples.push(s);
        return { pages, samples };
    }, []);
    const loadedInOrder = useMemo(
        () => contiguousRun().samples,
        // eslint-disable-next-line react-hooks/exhaustive-deps
        [pageVersion],
    );
    // While a reload has emptied the page cache (a tag edit's notifySamplesMutated, a new view), keep
    // publishing the last loaded list: the single-image view steps Previous/Next through it, and a
    // re-render in that gap found its sample in an empty list and hid its arrows for good — the page
    // isn't re-rendered when the grid refills. An empty list is published once the count says the
    // view IS empty.
    if (gridSamplesRef && (loadedInOrder.length > 0 || countData === 0)) gridSamplesRef.current = loadedInOrder;
    // The modal's Next stopped at the last LOADED row — row 200 of 27,000 — because pages are
    // only fetched for rows the grid scrolls past. Expose "is there
    // more after the loaded run" and "load the next page and hand back the longer run".
    if (gridMoreRef) {
        gridMoreRef.current = {
            hasMore: () => {
                const { pages } = contiguousRun();
                return pages.length > 0 && countData !== undefined && (pages[pages.length - 1] + 1) * PAGE_SIZE < countData;
            },
            loadNextPage: async () => {
                const { pages } = contiguousRun();
                if (pages.length === 0) return [];
                const next = pages[pages.length - 1] + 1;
                const { datasetId, filters, sortBy, sortOrder, rankOrderIds, rankByParent, key } = paramsRef.current;
                const res = await getSamples(datasetId, PAGE_SIZE, next * PAGE_SIZE, filters, sortBy, sortOrder, true, undefined, undefined, rankOrderIds, rankByParent);
                if (paramsRef.current.key !== key) return contiguousRun().samples;
                pagesRef.current.set(next, res.items);
                const run = contiguousRun().samples;
                // Publish the longer run now: the page renders BEFORE this grid does, and it looks
                // the modal's row up in this list — a stale one hid Next on the row it just loaded.
                if (gridSamplesRef) gridSamplesRef.current = run;
                setPageVersion(v => v + 1);
                return run;
            },
        };
    }

    // Notify the parent of the total — null while counting, then the real number. It only ever
    // reported totals > 0, so a filter matching nothing kept the previous count in the header
    // ("27,000 images", "Select All (27,000)") next to "No samples found".
    useEffect(() => {
        if (onTotalChange) onTotalChange(countData ?? null);
    }, [countData, onTotalChange]);

    // --- Virtualized rows -----------------------------------------------------
    // Measure the scroll container so we can compute the real column count and the
    // square cell size (cells are aspect-square and stretch to fill the row width).
    useIsoLayoutEffect(() => {
        const el = scrollRef.current;
        if (!el) return;
        const measure = () => setContainerWidth(el.clientWidth);
        measure();
        const ro = new ResizeObserver(measure);
        ro.observe(el);
        return () => ro.disconnect();
    }, []);

    const GAP = 12;      // gap-3
    const PADDING = 16;  // p-4 on the scroll container (both sides)
    const gridWidth = Math.max(0, containerWidth - PADDING * 2);
    const columnCount = gridWidth > 0
        ? Math.max(1, Math.floor((gridWidth + GAP) / (targetSize + GAP)))
        : 1;
    const cellSize = gridWidth > 0
        ? (gridWidth - (columnCount - 1) * GAP) / columnCount
        : targetSize;
    const rowHeight = cellSize + GAP;
    const totalRowCount = Math.ceil(effectiveCount / columnCount);
    // Render only a WINDOW of rows tall enough to stay under the browser height cap.
    const maxWindowRows = Math.max(1, Math.floor(SAFE_MAX_PX / rowHeight));
    const windowRowCount = Math.min(totalRowCount, maxWindowRows);
    const maxWindowStartRow = Math.max(0, totalRowCount - windowRowCount);
    // The window's first row, from the sample anchor, clamped into the dataset.
    const windowStartRow = Math.min(
        Math.max(0, Math.floor(windowAnchorSample / columnCount)),
        maxWindowStartRow
    );
    const windowStartIndex = windowStartRow * columnCount;
    // True when the dataset is too tall to fit one window → show the Jump bar.
    const windowed = totalRowCount > windowRowCount;

    // eslint-disable-next-line react-hooks/incompatible-library -- Keep TanStack Virtual’s imperative scroll state outside compiler memoization.
    const rowVirtualizer = useVirtualizer({
        count: windowRowCount,
        getScrollElement: () => scrollRef.current,
        estimateSize: () => rowHeight,
        overscan: OVERSCAN_ROWS,
    });

    // Recompute positions when the layout changes (resize, or the image-scale slider).
    useEffect(() => {
        rowVirtualizer.measure();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [columnCount, rowHeight]);

    // Fetch whatever pages the currently-visible (overscanned) rows need. This is
    // what makes scrubbing work: jump anywhere and the pages under the viewport
    // load, in any direction — not just sequentially from the top. The overscan
    // window also prefetches the pages just outside the viewport.
    const virtualRows = rowVirtualizer.getVirtualItems();
    useEffect(() => {
        if (virtualRows.length === 0 || effectiveCount === 0) return;
        // Debounce: while dragging/scrolling, the visible range changes rapidly.
        // Fetching on every change fires a storm of slow deep-offset queries for
        // positions you're just passing through, which backs up the pool and
        // delays the page you actually land on. Wait for the scroll to settle,
        // then fetch only the pages under the final viewport (+ overscan).
        if (fetchDebounceRef.current) clearTimeout(fetchDebounceRef.current);
        fetchDebounceRef.current = setTimeout(() => {
            // Window-relative rows → absolute sample indices.
            const firstIdx = (windowStartRow + virtualRows[0].index) * columnCount;
            const lastIdx = Math.min(effectiveCount - 1, (windowStartRow + virtualRows[virtualRows.length - 1].index + 1) * columnCount - 1);
            for (let p = Math.floor(firstIdx / PAGE_SIZE); p <= Math.floor(lastIdx / PAGE_SIZE); p++) {
                fetchPage(p);
            }
        }, 150);
        return () => { if (fetchDebounceRef.current) clearTimeout(fetchDebounceRef.current); };
    }, [virtualRows, columnCount, effectiveCount, windowStartRow, fetchPage]);

    // Where the top of the viewport currently sits in the WHOLE dataset — drives
    // the Jump bar's thumb + label.
    const firstVisibleSample = (windowStartRow + (virtualRows[0]?.index ?? 0)) * columnCount;
    const posFraction = effectiveCount > 1 ? Math.min(1, firstVisibleSample / (effectiveCount - 1)) : 0;

    const jumpTo = useCallback((fraction: number) => {
        setWindowAnchorSample(Math.round(fraction * Math.max(0, effectiveCount - 1)));
        // A jump breaks index contiguity — clear the shift-anchor so a later
        // shift-click can't span the jump and select a giant accidental range.
        setLastSelectedIndex(null);
        scrollRef.current?.scrollTo({ top: 0 });   // show the jumped-to region from the window top
    }, [effectiveCount]);

    const isEmpty = countData === 0;
    // The count retries a refusal by itself (query defaults); the manually fetched first page is
    // retried on the same cadence, so the grid fills in when the concept's scoring job ends.
    const refusedPage = firstPageError?.key === queryKeyStr && firstPageError.refusal;
    useEffect(() => {
        if (!refusedPage) return;
        const timer = setTimeout(() => { setFirstPageError(null); fetchPage(0); }, RESCORING_RETRY_MS);
        return () => clearTimeout(timer);
    }, [refusedPage, fetchPage]);
    const countProblem = countError ?? (isRescoringRefusal(countFailure) ? countFailure : null);
    const unavailable = effectiveCount === 0 && !isEmpty
        ? (countProblem instanceof Error ? countProblem.message : null)
            ?? (firstPageError?.key === queryKeyStr ? firstPageError.message : null)
        : null;

    const getImageSrc = useCallback((sample: Sample) => {
        // All media fields (filepath + any metadata.<field>) route through the
        // sample-media proxy so per-dataset storage + access control apply.
        //
        // Heatmap overlays: if any active overlay field is a heatmap on this
        // sample, ask the backend for a pre-composited PNG (first active
        // heatmap wins; stacking multiple is modal-only). Using a server
        // composite here keeps the grid as plain <img> tags — 100+ canvases
        // with decoded PNGs would otherwise OOM the browser.
        const activeHeatmap = activeOverlayFields.find(f => {
            const v = sample.metadata?.[f];
            return v && typeof v === 'object' && v.type === 'heatmap';
        });
        const cfg = activeHeatmap ? overlayConfigs?.[activeHeatmap] : undefined;
        // Request a THUMBNAIL sized to the tile (×DPR, capped), not the full-size original. Full
        // originals are ~2-2.5MB each; a screenful (50-100 tiles) is hundreds of MB — slow to load,
        // and scroll-aborted <img> loads fire onError, which the session watchdog turns into a
        // /api/auth/me storm. A ~256-1024px JPEG is ~15-150KB. Width is bucketed to 128px steps so
        // the server crop-cache stays effective across zoom. The overlay/composite path now honors w
        // too (resized JPEG tile instead of a full-res PNG); the modal still loads full-res separately.
        const dpr = typeof window !== 'undefined' ? Math.min(window.devicePixelRatio || 1, 2) : 1;
        const want = Math.ceil(targetSize * dpr);
        const w = Math.min(1024, Math.max(256, Math.ceil(want / 128) * 128));
        return getSampleMediaUrl(sample.id, activeMediaField, activeHeatmap,
            cfg ? { colormap: cfg.colormap, alpha: cfg.opacity, range: cfg.range } : undefined,
            { w });
    }, [activeMediaField, activeOverlayFields, overlayConfigs, targetSize]);

    // NOTE: the scroll container (scrollRef) is ALWAYS rendered — even while
    // loading/empty/measuring — so the width-measurement effect always has the
    // node. Early-returning a different element here used to leave scrollRef
    // unmounted, so containerWidth stayed 0 → 1 full-width column at the wrong
    // row height → giant overlapping images.
    return (
        <div className="h-full flex flex-col">
            {windowed && !isEmpty && containerWidth > 0 && effectiveCount > 0 && (
                <JumpBar fraction={posFraction} total={effectiveCount} onJump={jumpTo} />
            )}
            <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto p-4">
            {isEmpty ? (
                <div className="flex items-center justify-center h-full text-zinc-400">No samples found</div>
            ) : unavailable ? (
                <div className="flex flex-col items-center justify-center gap-2 h-full text-center text-sm text-zinc-400" data-testid="grid-unavailable" role="status">
                    <span className="text-amber-300/90 max-w-md">{unavailable}</span>
                    <span className="text-xs text-zinc-500">The view loads again by itself when the job finishes, or retry now.</span>
                    <button
                        className="text-xs underline hover:text-white"
                        onClick={() => {
                            setMutationVersion(v => v + 1);
                            // The count may be waiting out a retry, which the invalidation below does not cut short.
                            resetSampleCount(queryClient, datasetId, filters);
                            // The dataset's other views (count, sidebar, map, histogram) retry too.
                            queryClient.invalidateQueries({ predicate: query => query.queryKey[1] === datasetId && !String(query.queryKey[0]).includes('job') });
                        }}
                    >
                        Retry
                    </button>
                </div>
            ) : containerWidth === 0 || effectiveCount === 0 ? (
                // Measuring width, or waiting on the first page / COUNT. The layout
                // effect measures before paint; the empty-count frame is brief.
                <div className="flex items-center justify-center h-full text-zinc-400">Loading samples...</div>
            ) : (
                <div style={{ height: `${rowVirtualizer.getTotalSize()}px`, width: '100%', position: 'relative' }}>
                    {virtualRows.map(virtualRow => {
                        const startIndex = windowStartIndex + virtualRow.index * columnCount;
                        return (
                            <div
                                key={virtualRow.key}
                                style={{
                                    position: 'absolute',
                                    top: 0,
                                    left: 0,
                                    width: '100%',
                                    transform: `translateY(${virtualRow.start}px)`,
                                }}
                            >
                                <div
                                    className="grid gap-3"
                                    style={{ gridTemplateColumns: `repeat(${columnCount}, minmax(0, 1fr))` }}
                                >
                                    {Array.from({ length: columnCount }, (_, colIdx) => {
                                        const index = startIndex + colIdx;
                                        if (index >= effectiveCount) return null;
                                        const sample = getSampleAt(index);
                                        if (!sample) {
                                            // Page not loaded yet — skeleton tile keeps the grid
                                            // geometry stable while the region streams in.
                                            return <div key={`sk-${index}`} className="aspect-square rounded-lg bg-zinc-800/60 animate-pulse" />;
                                        }
                                        return (
                                            <GridCell
                                                key={sample.id}
                                                sample={sample}
                                                imageSrc={getImageSrc(sample)}
                                                isSelected={isSampleSelected(sample.id)}

                                                activeOverlayFields={activeOverlayFields}
                                                segmentationConfig={segmentationConfig}
                                                overlayConfigs={overlayConfigs}
                                                gridPatchMode={gridPatchMode}
                                                samplePatches={selectedPatches?.get(sample.id)}
                                                highlightBox={highlightBoxes?.get(sample.id) ?? null}
                                                onPatchClick={(row, col, scale) => onPatchSelect?.(sample.id, row, col, scale)}
                                                onClick={(e) => {
                                                    if (e.shiftKey && onRangeSelection && lastSelectedIndex !== null) {
                                                        selectRange(lastSelectedIndex, index, !selectedIdSet.has(sample.id));
                                                        setLastSelectedIndex(index);
                                                    } else if ((e.ctrlKey || e.metaKey) && onToggleSelection) {
                                                        // Ctrl/Cmd-click toggles, as in every file browser (it opened the
                                                        // modal) — and is an anchor for a following shift-click.
                                                        onToggleSelection(sample.id);
                                                        setLastSelectedIndex(index);
                                                    } else {
                                                        // A plain click OPENS the sample; it selects nothing, so it must not
                                                        // move the shift-range anchor. (It did: shift-click then ranged from
                                                        // the last image opened instead of the last one selected.)
                                                        onSampleClick(sample);
                                                    }
                                                }}
                                                onOpen={() => onSampleClick(sample)}
                                                onCheckboxClick={(e) => {
                                                    if (e.shiftKey && onRangeSelection && lastSelectedIndex !== null) {
                                                        selectRange(lastSelectedIndex, index, !selectedIdSet.has(sample.id));
                                                    } else {
                                                        onToggleSelection?.(sample.id);
                                                    }
                                                    setLastSelectedIndex(index);
                                                }}
                                                textOverlayFields={textOverlayFields}
                                            />
                                        );
                                    })}
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}
            </div>
        </div>
    );
}
