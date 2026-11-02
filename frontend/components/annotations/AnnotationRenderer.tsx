'use client';

import type { LabelType, Detection, Polygon, Keypoint, Classification, Cuboid, Calibration } from '@/lib/labels';
import { CuboidRenderer } from './CuboidRenderer';
import type { FieldColorConfig } from '@/lib/api';
import { DetectionRenderer } from './DetectionRenderer';
import { ClassificationRenderer } from './ClassificationRenderer';
import { MaskRenderer } from './MaskRenderer';
import { KeypointRenderer } from './KeypointRenderer';
import { PolygonOverlay } from '../PolygonRenderer';

interface Props {
    type: LabelType;
    data: unknown;
    imageSize: { width: number; height: number };
    calibration?: Calibration;
    /** Sample ID — required for segmentation (and any future) types that fetch
     *  derived media through `/api/samples/{id}/media`. Unused for SVG types. */
    sampleId?: number;
    versionId?: number;
    /** Metadata field name this annotation lives under — pairs with sampleId for
     *  constructing media URLs (e.g. `?field=polygon_semantic_viz.maskPath`). */
    fieldName?: string;
    colorByAttribute?: string;
    confidenceThreshold?: number;
    colorConfig?: FieldColorConfig;
    maskTargets?: Record<string, string>;
    /** Overlay opacity (segmentation masks). Falls back to MaskRenderer's default. */
    opacity?: number;
    /** Compact rendering for small grid tiles (classifications). */
    compact?: boolean;
    hiddenIndices?: Set<number>;
    onHover?: (label: string | null, x: number, y: number, event?: React.MouseEvent) => void;
    onSelect?: (index: number) => void;
    selectedIndex?: number;
}

/**
 * Routes to appropriate renderer based on label type
 */
export function AnnotationRenderer({
    type,
    data,
    imageSize,
    calibration,
    sampleId,
    versionId,
    fieldName,
    colorByAttribute,
    colorConfig,
    maskTargets,
    opacity,
    compact,
    hiddenIndices,
    onHover,
    onSelect,
    selectedIndex
}: Props) {
    if (!type || !data) return null;

    switch (type) {
        case 'cuboids':
            return calibration ? <CuboidRenderer cuboids={data as Cuboid[]} calibration={calibration}
                imageSize={imageSize} colorConfig={colorConfig} hiddenIndices={hiddenIndices}
                selectedIndex={selectedIndex} onSelect={onSelect} onHover={onHover} /> : null;
        case 'detections':
            return (
                <DetectionRenderer
                    detections={data as Detection[]}
                    imageSize={imageSize}
                    colorByAttribute={colorByAttribute}
                    colorConfig={colorConfig}
                    hiddenIndices={hiddenIndices}
                    onSelect={onSelect}
                    selectedIndex={selectedIndex}
                />
            );

        case 'polygons':
            return (
                <PolygonOverlay
                    polygons={data as Polygon[]}
                    imageWidth={imageSize.width}
                    imageHeight={imageSize.height}
                    colorConfig={colorConfig}
                    maskTargets={maskTargets}
                    hiddenIndices={hiddenIndices}
                    onSelect={onSelect}
                    selectedIndex={selectedIndex}
                    onHover={onHover}
                />
            );

        case 'segmentation':
            if (sampleId === undefined || !fieldName) {
                // MaskRenderer needs both to build the media-proxy URL. Without
                // them the renderer can't fetch anything; log and bail rather
                // than letting it call into a broken code path silently.
                console.error('AnnotationRenderer: segmentation requires sampleId and fieldName');
                return null;
            }
            return (
                <MaskRenderer
                    sampleId={sampleId}
                    versionId={versionId}
                    fieldName={fieldName}
                    imageSize={imageSize}
                    colorConfig={colorConfig}
                    maskTargets={maskTargets}
                    opacity={opacity}
                    onHover={onHover}
                    hiddenIds={hiddenIndices}
                />
            );

        case 'keypoints':
            return (
                <KeypointRenderer
                    keypoints={data as Keypoint[]}
                    imageSize={imageSize}
                    colorConfig={colorConfig}
                    hiddenIndices={hiddenIndices}
                    onSelect={onSelect}
                    selectedIndex={selectedIndex}
                />
            );

        case 'classification':
            // Single classification -> wrap in array
            return <ClassificationRenderer classifications={[data as Classification]} fieldName={fieldName} compact={compact} />;

        case 'classifications':
            return <ClassificationRenderer classifications={data as Classification[]} fieldName={fieldName} compact={compact} />;

        default:
            return null;
    }
}
