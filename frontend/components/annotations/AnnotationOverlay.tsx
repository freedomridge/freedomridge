'use client';

import { useState, useEffect } from 'react';
import { parseAnnotations, isOverlayActive, getOverlayColorConfig } from '@/lib/labels';
import { isCalibration } from '@/lib/geometry3d';
import type { AnnotationField, Heatmap, Calibration } from '@/lib/labels';
import { AnnotationRenderer } from './AnnotationRenderer';
import { AnnotationTooltip } from './AnnotationTooltip';
import { HeatmapRenderer } from './HeatmapRenderer';
import type { SegmentationConfig, FieldColorConfig } from '@/lib/api';

interface Props {
    imageSrc: string;
    /** Freedom Ridge sample ID — needed by renderers that fetch media via /api/samples/{id}/media
     *  (heatmaps, and the fixed-up MaskRenderer). Missing id means overlays that rely on
     *  the proxy won't render; the base image still does. */
    sampleId?: number;
    versionId?: number;
    metadata: Record<string, unknown>;
    className?: string;
    colorByAttribute?: string;
    confidenceThreshold?: number;
    activeOverlayFields?: string[];
    /** When false, hide the vector LABELS (detections/polygons/keypoints/classifications)
     *  while leaving mask and heatmap overlays visible. Defaults to true. */
    showLabels?: boolean;
    /** Compact overlay variant for small grid tiles (currently scopes classification
     *  badges to a small bottom-left chip instead of the modal-sized box). */
    compactOverlays?: boolean;
    overlayConfigs?: Record<string, { colormap?: string; range?: [number, number]; opacity?: number } | null>;
    segmentationConfig?: SegmentationConfig;
    hiddenAnnotations?: Record<string, Set<number>>;
    // Optional hover callback - when provided, tooltip is handled by parent
    onHover?: (label: string | null, clientX: number, clientY: number) => void;
    onSelect?: (field: string, index: number) => void;
    selectedAnnotation?: { field: string; index: number } | null;
    /** A box to emphasize over the image (e.g. the object whose parent this is), as
     *  [x, y, w, h]; values <= 2 are treated as normalized, like the detection renderer. */
    highlightBox?: [number, number, number, number] | null;
    /** Reports the loaded image's natural pixel size (fires on every image load).
     *  The modal uses it to size its zoom wrapper to the contained dimensions. */
    onImageLoad?: (width: number, height: number) => void;
}

/**
 * Main annotation overlay container
 * Automatically detects and renders all annotation fields from metadata
 */
export function AnnotationOverlay({
    imageSrc,
    sampleId,
    versionId,
    metadata,
    className = '',
    colorByAttribute,
    confidenceThreshold = 0,
    activeOverlayFields,
    showLabels = true,
    compactOverlays = false,
    overlayConfigs,
    segmentationConfig,
    hiddenAnnotations = {},
    onHover: parentHoverCallback,
    onSelect,
    selectedAnnotation,
    highlightBox,
    onImageLoad
}: Props) {
    const [imageSize, setImageSize] = useState({ width: 0, height: 0 });
    const [annotations, setAnnotations] = useState<AnnotationField[]>([]);

    // Tooltip hover state
    const [tooltipState, setTooltipState] = useState<{
        label: string | null;
        x: number;
        y: number;
        visible: boolean;
    }>({ label: null, x: 0, y: 0, visible: false });

    // Parse metadata for annotation fields
    useEffect(() => {
        const detected = parseAnnotations(metadata);
        // eslint-disable-next-line react-hooks/set-state-in-effect -- deliberate sync: re-derive annotation fields whenever the sample's metadata prop changes
        setAnnotations(detected);
    }, [metadata]);

    // Handle image load to get dimensions
    const handleImageLoad = (e: React.SyntheticEvent<HTMLImageElement>) => {
        const img = e.currentTarget;
        const dimensions = {
            width: img.naturalWidth,
            height: img.naturalHeight
        };
        setImageSize(dimensions);
        onImageLoad?.(dimensions.width, dimensions.height);
    };

    // Handle annotation hover for tooltip
    // If parentHoverCallback is provided, use raw SVG-relative coords for internal state
    // but also forward client coords to parent for proper tooltip positioning during zoom
    const handleAnnotationHover = (label: string | null, x: number, y: number, event?: React.MouseEvent) => {
        // Update internal state (for fallback tooltip)
        setTooltipState({
            label,
            x,
            y,
            visible: label !== null
        });

        // If parent callback provided, forward with client coordinates
        if (parentHoverCallback && event) {
            parentHoverCallback(label, event.clientX, event.clientY);
        } else if (parentHoverCallback) {
            // No event available (e.g., mouse leave), still notify parent
            parentHoverCallback(label, 0, 0);
        }
    };

    // Get color config for a specific field
    const getFieldColorConfig = (fieldName: string): FieldColorConfig | undefined => {
        return getOverlayColorConfig(segmentationConfig, fieldName, annotations.find(ann => ann.fieldName === fieldName)?.type);
    };

    // Get mask targets for a field (for ID -> name lookups)
    const getFieldMaskTargets = (fieldName: string): Record<string, string> | undefined => {
        return segmentationConfig?.mask_targets?.[fieldName] ||
            segmentationConfig?.default_mask_targets;
    };

    const hasAnnotations = annotations.length > 0;

    // A media load that fails (missing file, not an image) showed only the browser's broken-image
    // glyph on black. Reset per image, the React "adjust state while
    // rendering" form.
    const [failedSrc, setFailedSrc] = useState<string | null>(null);
    const failed = failedSrc === imageSrc;

    return (
        <div className={`relative ${className}`}>
            {/* Base image */}
            {/* eslint-disable-next-line @next/next/no-img-element -- authenticated media proxy, next/image not applicable */}
            <img
                src={imageSrc}
                alt="Sample"
                className="w-full h-full object-contain"
                onLoad={handleImageLoad}
                onError={() => setFailedSrc(imageSrc)}
            />
            {failed && (
                <div
                    className="absolute inset-0 flex items-center justify-center bg-zinc-950 text-sm text-zinc-400"
                    data-testid="media-unavailable"
                >
                    This sample&apos;s media could not be loaded
                </div>
            )}

            {/* SVG overlay for detections (bounding boxes) */}
            {showLabels && hasAnnotations && imageSize.width > 0 && (
                <svg
                    className="absolute inset-0 pointer-events-none"
                    viewBox={`0 0 ${imageSize.width} ${imageSize.height}`}
                    preserveAspectRatio="xMidYMid meet"
                    style={{
                        width: '100%',
                        height: '100%',
                        position: 'absolute',
                        top: 0,
                        left: 0,
                        overflow: 'visible'
                    }}
                >
                    {annotations
                        .filter(ann => ann.type === 'detections')
                        .filter(ann => isOverlayActive(ann.fieldName, activeOverlayFields, metadata))
                        .map((ann, idx) => (
                            <AnnotationRenderer
                                key={`${ann.fieldName}-${idx}`}
                                type={ann.type}
                                data={ann.data}
                                imageSize={imageSize}
                                colorByAttribute={colorByAttribute}
                                confidenceThreshold={confidenceThreshold}
                                colorConfig={getFieldColorConfig(ann.fieldName)}
                                hiddenIndices={hiddenAnnotations[ann.fieldName]}
                                onHover={handleAnnotationHover}
                                onSelect={(index) => onSelect?.(ann.fieldName, index)}
                                selectedIndex={selectedAnnotation?.field === ann.fieldName ? selectedAnnotation.index : undefined}
                            />
                        ))}
                </svg>
            )}

            {showLabels && imageSize.width > 0 && isCalibration(metadata.calibration) && (
                <svg data-testid="cuboid-overlay" className="absolute inset-0 pointer-events-none"
                    viewBox={`0 0 ${imageSize.width} ${imageSize.height}`} preserveAspectRatio="xMidYMid meet"
                    style={{ width: '100%', height: '100%', overflow: 'hidden' }}>
                    {annotations.filter(ann => ann.type === 'cuboids')
                        .filter(ann => isOverlayActive(ann.fieldName, activeOverlayFields, metadata))
                        .map(ann => <AnnotationRenderer key={ann.fieldName} type={ann.type} data={ann.data}
                            calibration={metadata.calibration as Calibration} imageSize={imageSize}
                            colorConfig={getFieldColorConfig(ann.fieldName)} hiddenIndices={hiddenAnnotations[ann.fieldName]}
                            onHover={handleAnnotationHover} onSelect={onSelect ? index => onSelect(ann.fieldName, index) : undefined}
                            selectedIndex={selectedAnnotation?.field === ann.fieldName ? selectedAnnotation.index : undefined} />)}
                </svg>
            )}

            {/* Emphasis box — the clicked object's region drawn over its parent image. */}
            {highlightBox && imageSize.width > 0 && (() => {
                const W = imageSize.width;
                const H = imageSize.height;
                const norm = Math.max(...highlightBox) <= 2;
                const [bx, by, bw, bh] = highlightBox;
                const sw = Math.max(2, W * 0.005);
                return (
                    <svg
                        className="absolute inset-0 pointer-events-none"
                        viewBox={`0 0 ${W} ${H}`}
                        preserveAspectRatio="xMidYMid meet"
                        style={{ width: '100%', height: '100%', position: 'absolute', top: 0, left: 0, overflow: 'visible' }}
                    >
                        <rect
                            x={norm ? bx * W : bx}
                            y={norm ? by * H : by}
                            width={norm ? bw * W : bw}
                            height={norm ? bh * H : bh}
                            fill="none"
                            stroke="#22d3ee"
                            strokeWidth={sw}
                            strokeDasharray={`${sw * 3} ${sw * 2}`}
                        />
                    </svg>
                );
            })()}

            {/* SVG overlay for keypoints */}
            {showLabels && hasAnnotations && imageSize.width > 0 && annotations.some(ann => ann.type === 'keypoints') && (
                <svg
                    className="absolute inset-0 pointer-events-none"
                    viewBox={`0 0 ${imageSize.width} ${imageSize.height}`}
                    preserveAspectRatio="xMidYMid meet"
                    style={{
                        width: '100%',
                        height: '100%',
                        position: 'absolute',
                        top: 0,
                        left: 0,
                        overflow: 'visible'
                    }}
                >
                    {annotations
                        .filter(ann => ann.type === 'keypoints')
                        .filter(ann => !activeOverlayFields || activeOverlayFields.includes('keypoints') || activeOverlayFields.includes(ann.fieldName))
                        .map((ann, idx) => (
                            <AnnotationRenderer
                                key={`${ann.fieldName}-${idx}`}
                                type={ann.type}
                                data={ann.data}
                                imageSize={imageSize}
                                colorConfig={getFieldColorConfig('keypoints')}
                                hiddenIndices={hiddenAnnotations[ann.fieldName]}
                                onSelect={(index) => onSelect?.(ann.fieldName, index)}
                                selectedIndex={selectedAnnotation?.field === ann.fieldName ? selectedAnnotation.index : undefined}
                            />
                        ))}
                </svg>
            )}

            {/* Polygon overlay */}
            {showLabels && hasAnnotations && annotations
                .filter(ann => ann.type === 'polygons')
                .filter(ann => isOverlayActive(ann.fieldName, activeOverlayFields, metadata))
                .map((ann, idx) => (
                    <AnnotationRenderer
                        key={`${ann.fieldName}-${idx}`}
                        type={ann.type}
                        data={ann.data}
                        imageSize={imageSize}
                        colorConfig={getFieldColorConfig(ann.fieldName)}
                        maskTargets={getFieldMaskTargets(ann.fieldName)}
                        hiddenIndices={hiddenAnnotations[ann.fieldName]}
                        onHover={handleAnnotationHover}
                        onSelect={(index) => onSelect?.(ann.fieldName, index)}
                        selectedIndex={selectedAnnotation?.field === ann.fieldName ? selectedAnnotation.index : undefined}
                    />
                ))}

            {/* DOM overlay for classifications — gated on activeOverlayFields like every
                other type, so it only shows when the user enabled that field (was always
                rendering, producing phantom classification labels). */}
            {showLabels && hasAnnotations && (() => {
                const cls = annotations
                    .filter(ann => ann.type === 'classification' || ann.type === 'classifications')
                    .filter(ann => isOverlayActive(ann.fieldName, activeOverlayFields, metadata));
                if (cls.length === 0) return null;
                // One badge per active field, stacked in a single positioned column so
                // multiple classification fields don't overlap (each labeled by field
                // name). Compact = small bottom-left chips for grid tiles; otherwise the
                // modal-sized badge boxes pinned top-right. pointer-events-none so the
                // info badges never block zoom/pan or cell clicks.
                return (
                    <div
                        className={compactOverlays
                            ? 'absolute bottom-1 left-1 z-10 flex flex-col gap-0.5 items-start pointer-events-none max-w-[92%]'
                            : 'absolute top-4 right-4 z-10 flex flex-col gap-2 items-end pointer-events-none'}
                    >
                        {cls.map((ann, idx) => (
                            <AnnotationRenderer
                                key={`${ann.fieldName}-${idx}`}
                                type={ann.type}
                                data={ann.data}
                                imageSize={imageSize}
                                fieldName={ann.fieldName}
                                compact={compactOverlays}
                            />
                        ))}
                    </div>
                );
            })()}

            {/* Canvas overlay for segmentation masks */}
            {hasAnnotations && imageSize.width > 0 && annotations
                .filter(ann => ann.type === 'segmentation')
                // Field-precise: a mask shows only when ITS OWN field name is active.
                // (The canonical field named 'segmentation' additionally honors the
                // 'segmentation.labels' handle.) Earlier field-agnostic 'segmentation'/
                // 'mask' clauses turned EVERY mask on at once — a cross-mask leak.
                .filter(ann => !activeOverlayFields || activeOverlayFields.includes(ann.fieldName) || (ann.fieldName === 'segmentation' && activeOverlayFields.includes('segmentation.labels')))
                .map((ann, idx) => (
                    <div
                        key={`${ann.fieldName}-${idx}`}
                        className="absolute inset-0 pointer-events-auto"
                    >
                        <AnnotationRenderer
                            type={ann.type}
                            data={ann.data}
                            imageSize={imageSize}
                            sampleId={sampleId}
                            versionId={versionId}
                            fieldName={ann.fieldName}
                            colorConfig={getFieldColorConfig(ann.fieldName)}
                            maskTargets={getFieldMaskTargets(ann.fieldName)}
                            opacity={overlayConfigs?.[ann.fieldName]?.opacity}
                            hiddenIndices={hiddenAnnotations[ann.fieldName]}
                            onHover={handleAnnotationHover}
                        />
                    </div>
                ))}

            {/* Canvas overlay for heatmap predictions (probability maps, terrain, etc.).
                Explicit field-name toggle — heatmaps don't have a .label sub-field to
                piggyback on, so each overlay name is its own activeOverlayFields entry.
                Gated on `sampleId` because the renderer fetches via /api/samples/{id}/media. */}
            {hasAnnotations && imageSize.width > 0 && sampleId !== undefined && annotations
                .filter(ann => ann.type === 'heatmap')
                .filter(ann => !activeOverlayFields || activeOverlayFields.includes(ann.fieldName))
                .map((ann, idx) => {
                    const hm = ann.data as Heatmap;
                    const cfg = overlayConfigs?.[ann.fieldName];  // UI config overrides metadata defaults
                    return (
                        <div
                            key={`${ann.fieldName}-${idx}`}
                            className="absolute inset-0 pointer-events-auto"
                        >
                            <HeatmapRenderer
                                sampleId={sampleId}
                                versionId={versionId}
                                fieldName={ann.fieldName}
                                imageSize={imageSize}
                                range={cfg?.range ?? hm.range}
                                colormap={cfg?.colormap ?? hm.colormap}
                                opacity={cfg?.opacity}
                                onHover={(value, x, y, event) => {
                                    const label = value !== null ? `${ann.fieldName}: ${value}` : null;
                                    handleAnnotationHover(label, x, y, event);
                                }}
                            />
                        </div>
                    );
                })}

            {/* Hover tooltip - only shown when parent is NOT handling tooltip */}
            {!parentHoverCallback && (
                <AnnotationTooltip
                    label={tooltipState.label}
                    x={tooltipState.x}
                    y={tooltipState.y}
                    visible={tooltipState.visible}
                />
            )}
        </div>
    );
}
