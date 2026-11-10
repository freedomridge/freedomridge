'use client';

import { useState, useEffect, useMemo, useCallback, useRef, type ReactNode } from 'react';
import { type Sample, type SegmentationConfig } from '@/lib/api';
import { getSampleMediaUrl } from '@/lib/media-utils';
import { parseAnnotations, isOverlayActive, toggleOverlayField } from '@/lib/labels';
import * as Dialog from '@radix-ui/react-dialog';
import { EyeOpenIcon, EyeNoneIcon } from '@radix-ui/react-icons';
import { AnnotationOverlay } from './annotations/AnnotationOverlay';
import { ClassRuleEditor } from './annotations/ClassRuleEditor';
import { defaultRule, type ClassProbabilities, type ClassRule, type ProbabilityView } from '@/lib/class-rules';
import { PatchGridOverlay } from './annotations/PatchGridOverlay';

interface Props {
    sample: Sample;
    activeMediaField?: string;
    availableMediaFields?: string[];
    activeOverlayFields?: string[];
    overlayConfigs?: Record<string, { colormap?: string; range?: [number, number]; opacity?: number } | null>;
    segmentationConfig?: SegmentationConfig;
    onMediaFieldChange?: (field: string) => void;
    onClose: () => void;
    availablePatchScales?: string[];
    patchScaleLabels?: Record<string, string>;  // how each scale reads, e.g. "8x8 · on desktop" for a follower's
    patchFieldName?: string;
    onPatchSearch?: (sampleId: number, fieldName: string, scale: string, row: number, col: number, limit: number) => void;
    patchScale?: string | null;
    onPatchScaleChange?: (scale: string | null) => void;
    onNext?: () => void;
    onPrevious?: () => void;
    hasNext?: boolean;
    hasPrevious?: boolean;
    isSelected?: boolean;
    onToggleSelection?: () => void;
    // Find Similar from the sample on screen (the modal had no actions).
    onFindSimilar?: () => void;
    onTag?: () => void;  // tag THIS sample (the one shown) without leaving the view
    // A dialog layered over this view (the tag dialog). Rendered INSIDE the Radix content so it is
    // inside its focus trap and click-outside boundary; while it is open, Escape closes it (not the
    // view) and the view's arrow/Space shortcuts pause.
    overlay?: ReactNode;
    onCloseOverlay?: () => void;
    /** Emphasize this box over the image — e.g. the object whose parent is shown. */
    highlightCuboidField?: string | null;
    /** Present when the user may save a class-probability field's shown rule as its default. */
    onSaveClassRule?: (fieldName: string, rule: ClassRule) => Promise<void>;
    highlightCuboidId?: string | null;
    highlightBox?: [number, number, number, number] | null;
}

export function SampleModal({ sample, activeMediaField = 'filepath', availableMediaFields = ['filepath'], activeOverlayFields: initialOverlayFields = [], overlayConfigs, segmentationConfig, onMediaFieldChange, onClose, availablePatchScales = [], patchScaleLabels, patchFieldName = 'clip', onPatchSearch, patchScale: externalPatchScale, onPatchScaleChange, onNext, onPrevious, hasNext = false, hasPrevious = false, isSelected = false, onToggleSelection, onFindSimilar, onTag, overlay, onCloseOverlay, highlightBox, highlightCuboidId, highlightCuboidField, onSaveClassRule }: Props) {
    // How each class-probability field is shown while the modal is open: edited rules redraw at once, write nothing,
    // and stay while stepping through samples.
    const [probabilityViews, setProbabilityViews] = useState<Record<string, ProbabilityView>>({});

    // Modal owns its own overlay state, seeded from the page's active overlays
    // when the modal opens. Toggling overlays inside the modal no longer
    // re-renders the grid (which used to repaint ~200 thumbnails per toggle).
    const [activeOverlayFields, setActiveOverlayFields] = useState<string[]>(initialOverlayFields);
    const onToggleOverlay = (field: string) => {
        setActiveOverlayFields(prev =>
            toggleOverlayField(prev, field, sample.metadata)
        );
    };

    const [showLabels, setShowLabels] = useState(true);

    // The "Hide Labels" button hides the vector annotations (detections, polygons,
    // keypoints, classifications) without touching mask/heatmap overlays — those have
    // their own per-overlay toggles. Only surface it when at least one such label is
    // active; the gate mirrors AnnotationOverlay's per-type render checks.
    const hasActiveLabels = useMemo(
        () =>
            parseAnnotations(sample.metadata || {}).some(ann => {
                if (ann.type === 'detections' || ann.type === 'polygons') {
                    return isOverlayActive(ann.fieldName, activeOverlayFields, sample.metadata);
                }
                if (ann.type === 'keypoints') {
                    return activeOverlayFields.includes('keypoints') || activeOverlayFields.includes(ann.fieldName);
                }
                if (ann.type === 'cuboids' || ann.type === 'classification' || ann.type === 'classifications') {
                    return activeOverlayFields.includes(ann.fieldName);
                }
                return false;
            }),
        [sample.metadata, activeOverlayFields],
    );

    // Zoom and pan state
    const [zoom, setZoom] = useState(1);
    const [pan, setPan] = useState({ x: 0, y: 0 });
    const [isDragging, setIsDragging] = useState(false);
    const [dragStart, setDragStart] = useState({ x: 0, y: 0 });

    // Fit-to-view: size the zoom wrapper to the image CONTAINED in the viewport, so
    // zoom=1 ("100%") always shows the whole image. Without this the wrapper is
    // unsized and the image renders at viewport WIDTH — any image taller than the
    // viewport's aspect opens with its top/bottom cropped off. Sizing the wrapper
    // (rather than letterboxing inside a fixed box) keeps every overlay that
    // positions itself relative to the wrapper box (masks, patch grid, highlight
    // boxes) exactly aligned with the image.
    const viewportRef = useRef<HTMLDivElement>(null);
    const naturalSizeRef = useRef<{ width: number; height: number } | null>(null);
    const [fitSize, setFitSize] = useState<{ width: number; height: number } | null>(null);
    const applyFitSize = useCallback(() => {
        const vp = viewportRef.current?.getBoundingClientRect();
        const nat = naturalSizeRef.current;
        if (!vp || !nat || vp.width === 0 || nat.width === 0) return;
        const scale = Math.min(vp.width / nat.width, vp.height / nat.height);
        setFitSize({ width: nat.width * scale, height: nat.height * scale });
    }, []);
    const handleImageNaturalSize = useCallback((width: number, height: number) => {
        naturalSizeRef.current = { width, height };
        applyFitSize();
    }, [applyFitSize]);
    useEffect(() => {
        window.addEventListener('resize', applyFitSize);
        return () => window.removeEventListener('resize', applyFitSize);
    }, [applyFitSize]);

    // Annotation state is sample-local; declare it before the reset effects.
    const [hiddenAnnotations, setHiddenAnnotations] = useState<Record<string, Set<number>>>({});
    const [selectedAnnotation, setSelectedAnnotation] = useState<{ key: string; index: number } | null>(null);
    const [selectedPatch, setSelectedPatch] = useState<{ row: number; col: number } | null>(null);

    // Reset per-sample state when sample changes. hiddenAnnotations and
    // selectedAnnotation are keyed by field+index — meaningless across samples —
    // so clear them too, or hiding/selecting item N on one sample leaks to item N
    // on the next (the modal instance is reused across next/previous navigation).
    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
        setZoom(1);
        setPan({ x: 0, y: 0 });
        setSelectedPatch(null);
        setHiddenAnnotations({});
        setSelectedAnnotation(null);
    }, [sample.id]);

    // Re-sync modal overlays from the page state when the page toggles them
    // (e.g. via the filter sidebar). Serialized to avoid firing on every
    // parent re-render.
    const initialOverlayKey = initialOverlayFields.join('|');
    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
        setActiveOverlayFields(initialOverlayFields);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [initialOverlayKey]);

    // Object rows select their exact cuboid, including sibling objects of one parent.
    useEffect(() => {
        if (!highlightCuboidId) return;
        const annotation = parseAnnotations(sample.metadata || {}).find(ann => ann.type === 'cuboids'
            && (!highlightCuboidField || ann.fieldName === highlightCuboidField)
            && (ann.data as { id?: string }[]).some(box => box?.id === highlightCuboidId));
        if (annotation) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
            setSelectedAnnotation({ key: `metadata.${annotation.fieldName}`,
                index: (annotation.data as { id?: string }[]).findIndex(box => box?.id === highlightCuboidId) });
            setActiveOverlayFields(prev => prev.includes(annotation.fieldName) ? prev : [...prev, annotation.fieldName]);
        } else {
            setSelectedAnnotation(null);
        }
    }, [sample.id, sample.metadata, highlightCuboidId, highlightCuboidField]);

    // Keyboard navigation: arrows for samples and media fields
    useEffect(() => {
        const handler = (e: KeyboardEvent) => {
            // Typing in the modal's own inputs (patch-search "Number of results", the media-field
            // select) jumped to another sample and dropped the patch selection.
            // Leave keys inside form controls and editable text alone.
            const target = e.target as HTMLElement | null;
            if (overlay || e.isComposing || target?.isContentEditable || target?.closest('input, select, textarea')) return;
            if (e.key === 'ArrowLeft' && onPrevious && hasPrevious) {
                e.preventDefault();
                onPrevious();
            } else if (e.key === 'ArrowRight' && onNext && hasNext) {
                e.preventDefault();
                onNext();
            } else if (e.key === ' ' && onToggleSelection) {
                e.preventDefault();
                onToggleSelection();
            } else if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && availableMediaFields.length > 1 && onMediaFieldChange) {
                e.preventDefault();
                const idx = availableMediaFields.indexOf(activeMediaField);
                const next = e.key === 'ArrowUp'
                    ? (idx - 1 + availableMediaFields.length) % availableMediaFields.length
                    : (idx + 1) % availableMediaFields.length;
                onMediaFieldChange(availableMediaFields[next]);
            }
        };
        window.addEventListener('keydown', handler);
        return () => window.removeEventListener('keydown', handler);
    }, [overlay, onNext, onPrevious, hasNext, hasPrevious, activeMediaField, availableMediaFields, onMediaFieldChange, onToggleSelection]);

    // Annotation tooltip state - rendered outside zoom container for proper positioning
    const [tooltipState, setTooltipState] = useState<{
        label: string | null;
        x: number;
        y: number;
        visible: boolean;
    }>({ label: null, x: 0, y: 0, visible: false });

    // Patch grid state — scale is controlled by parent so it persists across samples
    const patchScale = externalPatchScale ?? null;
    const setPatchScale = (scale: string | null) => onPatchScaleChange?.(scale);
    const [patchSearchLimit, setPatchSearchLimit] = useState(50);

    const toggleAnnotationVisibility = (field: string, index: number) => {
        setHiddenAnnotations(prev => {
            const next = { ...prev };
            if (!next[field]) {
                next[field] = new Set();
            } else {
                // Clone the set to avoid mutation
                next[field] = new Set(next[field]);
            }

            if (next[field].has(index)) {
                next[field].delete(index);
            } else {
                next[field].add(index);
            }
            return next;
        });
    };

    // Handle selection from AnnotationOverlay (fields are from metadata, so add prefix)
    const handleAnnotationSelect = (field: string, index: number) => {
        // AnnotationOverlay fields correspond to metadata fields
        const key = `metadata.${field}`;
        setSelectedAnnotation({ key, index });

        // Ensure the sidebar expands to show this property if it's collapsed (not implemented yet, but good for future)
    };

    // Handle hover from annotation overlay - uses client coordinates
    const handleAnnotationHover = (label: string | null, clientX: number, clientY: number) => {
        setTooltipState({
            label,
            x: clientX,
            y: clientY,
            visible: label !== null
        });
    };

    // All media fields route through the sample-media proxy. The backend reads
    // metadata->>activeMediaField for non-filepath fields, applies per-dataset
    // storage config + access control, and either serves bytes or 302-redirects
    // for external http URLs.
    const imageSrc = getSampleMediaUrl(sample.id, activeMediaField, undefined, undefined, undefined, sample.version_id);

    const clampZoom = (value: number) => Math.min(Math.max(value, 0.5), 5);

    // Zoom handlers
    const handleZoomIn = () => setZoom(clampZoom(zoom + 0.25));
    const handleZoomOut = () => setZoom(clampZoom(zoom - 0.25));
    const handleZoomReset = () => {
        setZoom(1);
        setPan({ x: 0, y: 0 });
    };

    // Mouse wheel zoom (zoom toward cursor position)
    const handleWheel = (e: React.WheelEvent) => {
        const delta = e.deltaY * -0.001;
        const nextZoom = clampZoom(zoom + delta);
        if (nextZoom === zoom) return;

        const rect = e.currentTarget.getBoundingClientRect();
        const mouseX = e.clientX - rect.left - rect.width / 2;
        const mouseY = e.clientY - rect.top - rect.height / 2;
        const zoomRatio = nextZoom / zoom;

        setPan({
            x: mouseX - (mouseX - pan.x) * zoomRatio,
            y: mouseY - (mouseY - pan.y) * zoomRatio
        });
        setZoom(nextZoom);
    };

    // Pan handlers - need to prevent event propagation to annotation overlays
    const handleMouseDown = (e: React.MouseEvent) => {
        e.preventDefault();
        setIsDragging(true);
        setDragStart({
            x: e.clientX - pan.x,
            y: e.clientY - pan.y
        });
    };

    const handleMouseMove = (e: React.MouseEvent) => {
        if (!isDragging) return;
        e.preventDefault();
        setPan({
            x: e.clientX - dragStart.x,
            y: e.clientY - dragStart.y
        });
    };

    const handleMouseUp = (e: React.MouseEvent) => {
        if (isDragging) {
            e.preventDefault();
        }
        setIsDragging(false);
    };

    const handleMouseLeave = () => {
        setIsDragging(false);
    };

    if (!sample) return null;

    return (
        <Dialog.Root open={true} onOpenChange={onClose}>
            <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50" />
                <Dialog.Content
                    className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[90vw] h-[90vh] bg-zinc-900 border border-zinc-800 rounded-lg shadow-2xl flex flex-col z-50"
                    onEscapeKeyDown={(e) => {
                        if (overlay && onCloseOverlay) {
                            e.preventDefault();
                            onCloseOverlay();
                        }
                    }}
                >
                    <div className="flex items-center justify-between p-4 border-b border-zinc-800">
                        <div className="flex items-center gap-4">
                            {onToggleSelection && (
                                <button
                                    onClick={onToggleSelection}
                                    className={`w-6 h-6 rounded-full border-2 flex items-center justify-center shrink-0 transition-all ${
                                        isSelected
                                            ? 'bg-cyan-500 border-cyan-500'
                                            : 'bg-zinc-800 border-zinc-600 hover:border-zinc-400'
                                    }`}
                                    title="Toggle selection (Space)"
                                >
                                    {isSelected && (
                                        <svg className="w-3.5 h-3.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                                        </svg>
                                    )}
                                </button>
                            )}
                            <Dialog.Title className="text-lg font-semibold">
                                {sample.metadata?.filename || `Sample ${sample.id}`}
                            </Dialog.Title>
                            {sample.filepath && (
                                <button
                                    onClick={() => navigator.clipboard?.writeText(sample.filepath)}
                                    className="text-xs text-zinc-400 hover:text-white border border-zinc-700 rounded px-2 py-1"
                                    title={sample.filepath}
                                    data-testid="modal-copy-path"
                                >
                                    Copy path
                                </button>
                            )}
                            {onTag && (
                                <button
                                    onClick={onTag}
                                    className="text-xs text-zinc-400 hover:text-white border border-zinc-700 rounded px-2 py-1"
                                    data-testid="modal-tag"
                                >
                                    Tag
                                </button>
                            )}
                            {onFindSimilar && (
                                <button
                                    onClick={onFindSimilar}
                                    className="text-xs text-zinc-400 hover:text-white border border-zinc-700 rounded px-2 py-1"
                                    data-testid="modal-find-similar"
                                >
                                    Find Similar
                                </button>
                            )}

                            {/* Media Field Selector */}
                            {availableMediaFields.length > 1 && onMediaFieldChange && (
                                <div className="flex items-center gap-2">
                                    <label className="text-xs text-zinc-500">View:</label>
                                    <select
                                        value={activeMediaField}
                                        onChange={(e) => onMediaFieldChange(e.target.value)}
                                        className="text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-white focus:outline-none focus:border-zinc-500"
                                    >
                                        {availableMediaFields.map(field => (
                                            <option key={field} value={field}>
                                                {field}
                                            </option>
                                        ))}
                                    </select>
                                </div>
                            )}

                            {/* Zoom Controls */}
                            <div className="flex items-center gap-2 border-l border-zinc-700 pl-4 ml-2">
                                <button
                                    onClick={handleZoomOut}
                                    disabled={zoom <= 0.5}
                                    className="p-1.5 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30 disabled:hover:bg-zinc-800 border border-zinc-700 rounded text-xs text-white transition-colors"
                                    title="Zoom Out"
                                >
                                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 12H4" />
                                    </svg>
                                </button>
                                <span className="text-xs text-zinc-400 font-mono w-12 text-center">
                                    {Math.round(zoom * 100)}%
                                </span>
                                <button
                                    onClick={handleZoomIn}
                                    disabled={zoom >= 5}
                                    className="p-1.5 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30 disabled:hover:bg-zinc-800 border border-zinc-700 rounded text-xs text-white transition-colors"
                                    title="Zoom In"
                                >
                                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                                    </svg>
                                </button>
                                <button
                                    onClick={handleZoomReset}
                                    disabled={zoom === 1 && pan.x === 0 && pan.y === 0}
                                    className="p-1.5 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30 disabled:hover:bg-zinc-800 border border-zinc-700 rounded text-xs text-white transition-colors"
                                    title="Reset Zoom"
                                >
                                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
                                    </svg>
                                </button>
                            </div>
                        </div>
                        {/* Patch Controls */}
                        <div className="flex items-center gap-2">
                            {availablePatchScales.length > 0 && (
                                <div className="flex items-center gap-2 border-l border-zinc-700 pl-4 ml-2">
                                    <label className="text-xs text-zinc-500">Patches:</label>
                                    <select
                                        value={patchScale || ''}
                                        onChange={(e) => { setPatchScale(e.target.value || null); setSelectedPatch(null); }}
                                        className="text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-white"
                                    >
                                        <option value="">Off</option>
                                        {availablePatchScales.map(s => (
                                            <option key={s} value={s}>{patchScaleLabels?.[s] ?? s}</option>
                                        ))}
                                    </select>
                                    {selectedPatch && onPatchSearch && (
                                        <>
                                            <input
                                                type="text"
                                                inputMode="numeric"
                                                value={patchSearchLimit}
                                                onChange={(e) => {
                                                    const v = parseInt(e.target.value);
                                                    if (!isNaN(v)) setPatchSearchLimit(Math.max(1, Math.min(500, v)));
                                                }}
                                                className="w-12 bg-zinc-800 border border-zinc-700 text-xs text-white rounded px-1.5 py-1 text-center"
                                                title="Number of results"
                                            />
                                            <button
                                                onClick={() => onPatchSearch(sample.id, patchFieldName, patchScale!, selectedPatch.row, selectedPatch.col, patchSearchLimit)}
                                                className="px-3 py-1 bg-cyan-600 hover:bg-cyan-500 text-white text-xs rounded transition-colors"
                                            >
                                                Search Similar Patches
                                            </button>
                                        </>
                                    )}
                                </div>
                            )}

                        <Dialog.Close className="text-zinc-400 hover:text-zinc-50 transition-colors">
                            <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                            </svg>
                        </Dialog.Close>
                        </div>
                    </div>

                    <div className="flex-1 flex overflow-hidden">
                        <div
                            ref={viewportRef}
                            className="flex-1 relative bg-black overflow-hidden flex items-center justify-center"
                            onWheel={handleWheel}
                            onMouseDown={handleMouseDown}
                            onMouseMove={handleMouseMove}
                            onMouseUp={handleMouseUp}
                            onMouseLeave={handleMouseLeave}
                            style={{
                                cursor: isDragging ? 'grabbing' : 'grab'
                            }}
                        >
                            {/* Navigation arrows */}
                            {hasPrevious && onPrevious && (
                                <button
                                    onClick={(e) => { e.stopPropagation(); onPrevious(); }}
                                    className="absolute left-2 top-1/2 -translate-y-1/2 z-20 p-2 bg-black/50 hover:bg-black/80 rounded-full text-white/70 hover:text-white transition-all"
                                    title="Previous sample (Left arrow)"
                                >
                                    <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
                                    </svg>
                                </button>
                            )}
                            {hasNext && onNext && (
                                <button
                                    onClick={(e) => { e.stopPropagation(); onNext(); }}
                                    className="absolute right-2 top-1/2 -translate-y-1/2 z-20 p-2 bg-black/50 hover:bg-black/80 rounded-full text-white/70 hover:text-white transition-all"
                                    title="Next sample (Right arrow)"
                                >
                                    <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                                    </svg>
                                </button>
                            )}
                            {/* Global show/hide for vector labels (detections, polygons,
                                keypoints, classifications). Mask & heatmap overlays are
                                unaffected — they have their own per-overlay toggles. Lives
                                OUTSIDE the zoom/pan transform so it stays pinned and
                                fixed-size; shown only when at least one label is active. */}
                            {hasActiveLabels && (
                                <button
                                    onClick={() => setShowLabels(!showLabels)}
                                    className="absolute top-4 left-4 z-20 px-3 py-1.5 bg-black/60 hover:bg-black/80 backdrop-blur-sm border border-white/20 rounded text-xs text-white transition-colors"
                                    title={showLabels ? 'Hide labels' : 'Show labels'}
                                >
                                    {showLabels ? '👁️ Hide Labels' : '👁️‍🗨️ Show Labels'}
                                </button>
                            )}
                            <div
                                style={{
                                    transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
                                    transformOrigin: 'center',
                                    transition: isDragging ? 'none' : 'transform 0.1s ease-out',
                                    pointerEvents: 'auto',
                                    // Contained dimensions (see fitSize): the box aspect matches the
                                    // image exactly, so object-contain fills it edge-to-edge and all
                                    // box-relative overlays stay aligned.
                                    ...(fitSize ? { width: `${fitSize.width}px`, height: `${fitSize.height}px` } : {})
                                }}
                            >
                                <AnnotationOverlay
                                    imageSrc={imageSrc}
                                    sampleId={sample.id}
                                    versionId={sample.version_id}
                                    metadata={sample.metadata || {}}
                                    className="w-full h-full"
                                    onImageLoad={handleImageNaturalSize}
                                    activeOverlayFields={activeOverlayFields}
                                    showLabels={showLabels}
                                    overlayConfigs={overlayConfigs}
                                    segmentationConfig={segmentationConfig}
                                    probabilityViews={probabilityViews}
                                    hiddenAnnotations={Object.fromEntries(
                                        Object.entries(hiddenAnnotations).map(([key, val]) => [
                                            key.replace(/^metadata\./, ''),
                                            val
                                        ])
                                    )}
                                    onHover={handleAnnotationHover}
                                    onSelect={handleAnnotationSelect}
                                    selectedAnnotation={selectedAnnotation ? {
                                        field: selectedAnnotation.key.replace(/^metadata\./, ''),
                                        index: selectedAnnotation.index
                                    } : null}
                                    highlightBox={highlightBox}
                                />

                                {/* Patch grid overlay */}
                                {patchScale && (
                                    <PatchGridOverlay
                                        scale={patchScale}
                                        selectedPatches={selectedPatch ? [selectedPatch] : []}
                                        onPatchClick={(row, col) => setSelectedPatch(
                                            selectedPatch?.row === row && selectedPatch?.col === col ? null : { row, col }
                                        )}
                                        mode="select"
                                    />
                                )}

                            </div>
                            {/* Close transform wrapper */}

                            {/* Annotation tooltip - rendered OUTSIDE zoom container for proper cursor tracking */}
                            {tooltipState.visible && tooltipState.label && (
                                <div
                                    className="fixed z-[100] pointer-events-none px-2 py-1 rounded bg-black/90 text-white text-xs font-medium whitespace-nowrap shadow-lg border border-zinc-700"
                                    style={{
                                        left: tooltipState.x + 12,
                                        top: tooltipState.y - 24,
                                    }}
                                >
                                    {tooltipState.label}
                                </div>
                            )}
                        </div>

                        <div className="w-80 border-l border-zinc-800 overflow-y-auto p-6">
                            <h3 className="text-sm font-semibold mb-4 text-zinc-400">Properties</h3>
                            <dl className="space-y-3">
                                <PropertyViewer
                                    data={sample}
                                    hiddenAnnotations={hiddenAnnotations}
                                    selectedAnnotation={selectedAnnotation}
                                    onToggleVisibility={toggleAnnotationVisibility}
                                    onSelect={(key, index) => setSelectedAnnotation({ key, index })}
                                    segmentationConfig={segmentationConfig}
                                    activeOverlayFields={activeOverlayFields}
                                    onToggleOverlay={onToggleOverlay}
                                    probabilityViews={probabilityViews}
                                    onProbabilityView={(field, view) => setProbabilityViews(views => ({ ...views, [field]: view }))}
                                    onSaveClassRule={onSaveClassRule}
                                />
                            </dl>
                        </div>
                    </div>
                    {overlay}
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}

function PropertyViewer({
    data,
    prefix = '',
    hiddenAnnotations = {},
    selectedAnnotation = null,
    onToggleVisibility,
    onSelect,
    segmentationConfig,
    activeOverlayFields = [],
    onToggleOverlay,
    probabilityViews = {},
    onProbabilityView,
    onSaveClassRule,
}: {
    data: unknown;
    prefix?: string;
    hiddenAnnotations?: Record<string, Set<number>>;
    selectedAnnotation?: { key: string; index: number } | null;
    onToggleVisibility?: (field: string, index: number) => void;
    onSelect?: (key: string, index: number) => void;
    segmentationConfig?: SegmentationConfig;
    activeOverlayFields?: string[];
    onToggleOverlay?: (field: string) => void;
    probabilityViews?: Record<string, ProbabilityView>;
    onProbabilityView?: (field: string, view: ProbabilityView) => void;
    onSaveClassRule?: (fieldName: string, rule: ClassRule) => Promise<void>;
}) {
    if (!data || typeof data !== 'object') return null;

    // Alphabetical key order (case-insensitive) so the properties panel reads
    // like the filter sidebar. Sort on localeCompare to handle mixed case
    // predictably; ties fall back to insertion order.
    const entries = Object.entries(data).slice().sort(([a], [b]) =>
        a.localeCompare(b, undefined, { sensitivity: 'base' })
    );

    // Split overlay-shaped values (heatmaps and {maskPath} masks) into a dedicated
    // "Overlays" group so they render under one header with eye toggles — mirroring
    // the filter sidebar's Overlays section. Applied at any nesting level that holds
    // them; in practice the metadata dict.
    const isHeatmap = (v: unknown) =>
        v !== null && typeof v === 'object' && !Array.isArray(v) && (v as { type?: string }).type === 'heatmap';
    const isMask = (v: unknown) =>
        v !== null && typeof v === 'object' && !Array.isArray(v) && 'maskPath' in (v as object);
    const isProbabilities = (v: unknown) =>
        v !== null && typeof v === 'object' && !Array.isArray(v) && (v as { type?: string }).type === 'class_probabilities';
    const heatmapEntries = entries.filter(([, v]) => isHeatmap(v));
    const maskEntries = entries.filter(([, v]) => isMask(v));
    const probabilityEntries = entries.filter(([, v]) => isProbabilities(v));
    const otherEntries = entries.filter(([, v]) => !isHeatmap(v) && !isMask(v) && !isProbabilities(v));

    // A class-probability field: an eye toggle like a mask's, and while shown, how it's drawn (ClassRuleEditor).
    const renderProbabilityOverlay = (key: string, value: ClassProbabilities) => {
        const displayKey = prefix ? `${prefix}.${key}` : key;
        const field = displayKey.replace(/^metadata\./, '');
        const active = activeOverlayFields.includes(key);
        const savedRule = defaultRule(segmentationConfig?.class_rules, field);
        return (
            <div key={displayKey} className="py-0.5">
                <div className="flex items-center justify-between">
                    <span className="text-sm text-zinc-300 truncate" title={`${key} (class probabilities, ${value.classes} classes)`}>
                        {key}
                    </span>
                    {onToggleOverlay ? (
                        <button
                            aria-label={`Toggle ${key} overlay`}
                            aria-pressed={active}
                            onClick={() => onToggleOverlay(key)}
                            className={`p-1 rounded hover:bg-zinc-800 transition-colors ${active ? 'text-cyan-400' : 'text-zinc-500 hover:text-zinc-300'}`}
                            title={active ? 'Hide from image' : 'Show on image'}
                        >
                            {active ? <EyeOpenIcon /> : <EyeNoneIcon />}
                        </button>
                    ) : (
                        <span className="text-xs text-zinc-600">probabilities</span>
                    )}
                </div>
                {active && onProbabilityView && (
                    <ClassRuleEditor
                        fieldName={field}
                        classes={value.classes}
                        maskTargets={segmentationConfig?.mask_targets?.[field] || segmentationConfig?.default_mask_targets}
                        colorConfig={segmentationConfig?.color_scheme?.fields?.[field]}
                        view={probabilityViews[field] ?? { rule: savedRule, show: 'map' }}
                        savedRule={savedRule}
                        onChange={view => onProbabilityView(field, view)}
                        onSave={onSaveClassRule ? rule => onSaveClassRule(field, rule) : undefined}
                    />
                )}
            </div>
        );
    };

    const renderHeatmapRow = (key: string) => {
        const displayKey = prefix ? `${prefix}.${key}` : key;
        const isActive = activeOverlayFields.includes(key);
        return (
            <div key={displayKey} className="flex items-center justify-between py-0.5">
                <span className="text-sm text-zinc-300 truncate" title={`${key} (heatmap)`}>
                    {key}
                </span>
                {onToggleOverlay ? (
                    <button
                        aria-label={`Toggle ${key} overlay`}
                        aria-pressed={isActive}
                        onClick={() => onToggleOverlay(key)}
                        className={`p-1 rounded hover:bg-zinc-800 transition-colors ${isActive ? 'text-cyan-400' : 'text-zinc-500 hover:text-zinc-300'}`}
                        title={isActive ? 'Hide from image' : 'Show on image'}
                    >
                        {isActive ? <EyeOpenIcon /> : <EyeNoneIcon />}
                    </button>
                ) : (
                    <span className="text-xs text-zinc-600">heatmap</span>
                )}
            </div>
        );
    };

    // A mask ({maskPath}) overlay row: name + on/off eye toggle (like the heatmap
    // row), with the per-class legend (RGB swatches / index-class hide rows) beneath.
    const renderMaskOverlay = (key: string) => {
        const displayKey = prefix ? `${prefix}.${key}` : key;
        const maskTargets = segmentationConfig?.mask_targets?.[displayKey.replace(/^metadata\./, '')] || segmentationConfig?.default_mask_targets;
        const overlayActive = activeOverlayFields.includes(key);
        return (
            <div key={displayKey} className="py-0.5">
                <div className="flex items-center justify-between">
                    <span className="text-sm text-zinc-300 truncate" title={`${key} (mask)`}>
                        {key}
                    </span>
                    {onToggleOverlay ? (
                        <button
                            aria-label={`Toggle ${key} overlay`}
                            aria-pressed={overlayActive}
                            onClick={() => onToggleOverlay(key)}
                            className={`p-1 rounded hover:bg-zinc-800 transition-colors ${overlayActive ? 'text-cyan-400' : 'text-zinc-500 hover:text-zinc-300'}`}
                            title={overlayActive ? 'Hide from image' : 'Show on image'}
                        >
                            {overlayActive ? <EyeOpenIcon /> : <EyeNoneIcon />}
                        </button>
                    ) : (
                        <span className="text-xs text-zinc-600">mask</span>
                    )}
                </div>
                {maskTargets && (
                    <div className="space-y-1 mt-1 ml-1">
                        {Object.entries(maskTargets).map(([idStr, label]) => {
                            // RGB masks key mask_targets by hex color (e.g. semantic_mask); index
                            // masks key by integer class ID. parseInt('#ff0000') is NaN, so
                            // branch on the key shape.
                            const isHex = /^#[0-9a-fA-F]{6}$/.test(idStr);
                            if (isHex) {
                                // No per-class hide for RGB masks (MaskRenderer paints pixel
                                // colors directly) — show a color swatch + label.
                                return (
                                    <div key={idStr} className="flex items-center gap-2 text-xs w-full px-2 py-0.5 rounded text-zinc-400">
                                        <span className="w-3 h-3 rounded-sm border border-white/20" style={{ backgroundColor: idStr }} />
                                        <span className="flex-1 truncate">{label}</span>
                                    </div>
                                );
                            }
                            const id = parseInt(idStr);
                            const isHidden = hiddenAnnotations[displayKey]?.has(id);
                            return (
                                <div key={idStr} className="flex items-center gap-2 text-xs w-full px-2 py-0.5 rounded transition-colors text-zinc-400 hover:bg-zinc-800">
                                    <button
                                        onClick={(e) => { e.stopPropagation(); onToggleVisibility?.(displayKey, id); }}
                                        className={`p-1 rounded hover:bg-white/10 ${isHidden ? 'text-zinc-500' : 'text-zinc-400'}`}
                                        title={isHidden ? 'Show' : 'Hide'}
                                    >
                                        <span className="w-4 text-center block">{isHidden ? '○' : '●'}</span>
                                    </button>
                                    <span className={`flex-1 truncate ${isHidden ? 'line-through opacity-50' : ''}`}>
                                        {label} <span className="text-zinc-600 text-[10px] ml-1">(ID: {id})</span>
                                    </span>
                                </div>
                            );
                        })}
                    </div>
                )}
            </div>
        );
    };

    return (
        <>
            {(heatmapEntries.length > 0 || maskEntries.length > 0 || probabilityEntries.length > 0) && (
                <div className="pb-3 mb-3 border-b border-zinc-800">
                    <h4 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-2">
                        Overlays
                    </h4>
                    <div className="space-y-0.5">
                        {heatmapEntries.map(([key]) => renderHeatmapRow(key))}
                        {maskEntries.map(([key]) => renderMaskOverlay(key))}
                        {probabilityEntries.map(([key, value]) => renderProbabilityOverlay(key, value as ClassProbabilities))}
                    </div>
                </div>
            )}
            {otherEntries.map(([key, value]) => {
                // Every field is shown exactly as stored, except a raw embedding vector.
                if (key === 'embedding') return null;

                const displayKey = prefix ? `${prefix}.${key}` : key;

                if (value === null || value === undefined) {
                    return (
                        <div key={displayKey}>
                            <dt className="text-xs text-zinc-500 capitalize mb-1">{key}</dt>
                            <dd className="text-sm text-zinc-500 italic">null</dd>
                        </div>
                    );
                }

                // Special handling for Detections/Polygons arrays
                // Treat any list of objects with 'label' as an annotation list (detections, polygons, classifications)
                if (Array.isArray(value) && value.length > 0 && typeof value[0] === 'object' && value[0] !== null && ('label' in value[0] || 'points' in value[0] || 'paths' in value[0] || 'bbox' in value[0] || value[0]._type === 'cuboid')) {
                    const isDetection = 'bbox' in value[0];
                    // If it has a label but no bbox, treat it as generic labeled annotation (polygon/classification)
                    const isPolygon = !isDetection;

                    if (isPolygon || isDetection) {
                        return (
                            <div key={displayKey} className="border-l-2 border-zinc-800 pl-3 mt-2">
                                <dt className="text-xs text-zinc-400 font-semibold mb-2 flex justify-between">
                                    {key}
                                    <span className="flex items-center gap-2 text-zinc-600 font-normal">
                                        {value.length} items
                                        {onToggleOverlay && <button
                                            onClick={() => onToggleOverlay(key)}
                                            aria-label={`Toggle ${key} overlay`}
                                            data-testid={`modal-overlay-toggle-${key}`}
                                            aria-pressed={isOverlayActive(key, activeOverlayFields, data as Record<string, unknown>)}
                                            title={isOverlayActive(key, activeOverlayFields, data as Record<string, unknown>) ? 'Hide from image' : 'Show on image'}
                                        >
                                            {isOverlayActive(key, activeOverlayFields, data as Record<string, unknown>) ? <EyeOpenIcon /> : <EyeNoneIcon />}
                                        </button>}
                                    </span>
                                </dt>
                                <div className="space-y-1">
                                    {value.map((item: Record<string, unknown>, i: number) => {
                                        const isHidden = hiddenAnnotations[displayKey]?.has(i);
                                        const isSelected = selectedAnnotation?.key === displayKey && selectedAnnotation?.index === i;
                                        const label = typeof item.label === 'string' ? item.label : '';
                                        // Guard the type: keypoint per-point confidence is an array, and .toFixed on it crashes the panel.
                                        const confidence = typeof item.confidence === 'number' ? `(${item.confidence.toFixed(2)})` : '';
                                        const text = `${label} ${confidence}`;

                                        return (
                                            <div
                                                key={i}
                                                className={`flex items-center gap-2 text-xs w-full px-2 py-1 rounded transition-colors group ${isSelected ? 'bg-blue-900/50 text-blue-100' : 'text-zinc-300 hover:bg-zinc-800'
                                                    }`}
                                            >
                                                {/* Visibility Toggle */}
                                                <button
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        onToggleVisibility?.(displayKey, i);
                                                    }}
                                                    className={`p-1.5 rounded hover:bg-white/10 ${isHidden ? 'text-zinc-500' : 'text-zinc-400'}`}
                                                    aria-label={`${isHidden ? "Show" : "Hide"} ${label} ${i + 1}`}
                                                    title={isHidden ? "Show" : "Hide"}
                                                >
                                                    <span className="w-4 text-center block">{isHidden ? '○' : '●'}</span>
                                                </button>

                                                {/* The label selects the annotation; hiding it is the separate toggle button. */}
                                                <button
                                                    onClick={() => onSelect?.(displayKey, i)}
                                                    className={`flex-1 text-left truncate ${isHidden ? 'line-through opacity-50' : ''}`}
                                                    title="Click to select"
                                                >
                                                    {text}
                                                </button>
                                            </div>
                                        );
                                    })}
                                </div>
                            </div>
                        );
                    }
                }

                if (Array.isArray(value)) {
                    return (
                        <div key={displayKey}>
                            <dt className="text-xs text-zinc-500 capitalize mb-1">{key}</dt>
                            <dd className="flex flex-wrap gap-1">
                                {value.length === 0 ? (
                                    <span className="text-sm text-zinc-500 italic">empty list</span>
                                ) : (
                                    value.map((item, i) => (
                                        <span key={i} className="px-2 py-0.5 bg-zinc-800 rounded-full text-xs text-zinc-300">
                                            {String(item)}
                                        </span>
                                    ))
                                )}
                            </dd>
                        </div>
                    );
                }

                if (typeof value === 'object') {
                    return (
                        <div key={displayKey} className="border-l-2 border-zinc-800 pl-3 mt-2">
                            <dt className="text-xs text-zinc-400 font-semibold mb-2">{key}</dt>
                            <div className="space-y-3">
                                <PropertyViewer
                                    data={value}
                                    prefix={displayKey}
                                    hiddenAnnotations={hiddenAnnotations}
                                    selectedAnnotation={selectedAnnotation}
                                    onToggleVisibility={onToggleVisibility}
                                    onSelect={onSelect}
                                    segmentationConfig={segmentationConfig}
                                    activeOverlayFields={activeOverlayFields}
                                    onToggleOverlay={onToggleOverlay}
                                    probabilityViews={probabilityViews}
                                    onProbabilityView={onProbabilityView}
                                    onSaveClassRule={onSaveClassRule}
                                />
                            </div>
                        </div>
                    );
                }

                // Format dates
                let displayValue = String(value);
                if (typeof value === 'string' && (key.includes('_at') || key.includes('date')) && !isNaN(Date.parse(value)) && value.length > 10) {
                    displayValue = new Date(value).toLocaleString();
                }
                // Format numbers
                if (typeof value === 'number') {
                    displayValue = Number.isInteger(value) ? value.toString() : value.toFixed(4);
                }

                return (
                    <div key={displayKey}>
                        <dt className="text-xs text-zinc-500 capitalize mb-1">{key}</dt>
                        <dd className="text-sm text-zinc-200 break-all">{displayValue}</dd>
                    </div>
                );
            })}
        </>
    );
}
