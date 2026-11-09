import type { FieldColorConfig, SegmentationConfig } from './api';

// Label type definitions for ML annotations
// Label type definitions

export interface BoundingBox {
    x: number;
    y: number;
    width: number;
    height: number;
}

export interface Detection {
    label: string;
    confidence?: number;
    bbox: BoundingBox | number[];  // Normalized (0-1) or pixel values [x, y, w, h]
    attributes?: Record<string, unknown>;
}

export interface Classification {
    label: string;
    confidence?: number;
    attributes?: Record<string, unknown>;
}

export interface Cuboid {
    _type: 'cuboid';
    id: string;
    label: string;
    center: [number, number, number];
    size: [number, number, number];
    yaw: number;
    frame: 'camera' | 'lidar';
    confidence?: number;
    truncated?: number;
    occluded?: number;
    alpha?: number;
    attributes?: Record<string, unknown>;
}

export interface Calibration {
    type: 'calibration';
    P: number[];
    image_size: [number, number];
    camera?: string;
    transforms?: Record<string, number[]>;
    kitti?: Record<string, number[]>;
}

// A set of keypoints for one object (e.g. a pose). Carries an explicit
// "_type" marker so it can be told apart from polygons (both have `points`).
export interface Keypoint {
    points: [number, number][];        // normalized (0-1) or pixel coordinates
    label?: string;
    confidence?: number | number[];
    _type?: 'keypoints';
    attributes?: Record<string, unknown>;
}

export interface Polygon {
    label: string;
    points?: [number, number][];     // Simple polygon (single ring)
    paths?: [number, number][][];    // Complex polygon (exterior + holes)
    closed?: boolean;
    id?: number;
}

export interface SegmentationMask {
    maskPath: string;
    labels?: string[];
}

// Heatmap overlay (single-class prediction probability, terrain, etc.).
// Stored in sample metadata as a dict with the explicit "heatmap" type marker
// so we can discriminate it from regular segmentation masks and annotations
// without relying on structural detection.
export interface Heatmap {
    type: 'heatmap';
    map_path: string;          // s3:// or local path to the PNG
    range: [number, number];   // min, max of meaningful pixel values
    colormap?: string;         // optional; frontend falls back to DEFAULT_COLORMAP
}

export type LabelType =
    | 'detections'
    | 'polygons'
    | 'segmentation'
    | 'classification'
    | 'classifications'
    | 'keypoints'
    | 'cuboids'
    | 'heatmap'
    | 'class_probabilities'
    | null;

export interface AnnotationField {
    fieldName: string;
    type: LabelType;
    data: unknown;
}

/**
 * Auto-detect label type from metadata value
 */
export function detectLabelType(value: unknown): LabelType {
    if (!value) return null;

    // Heatmap: explicit {type: "heatmap", ...} marker. Checked first because the
    // shape (dict with map_path) would otherwise be ambiguous with new label types.
    if (value && typeof value === 'object' && !Array.isArray(value) && (value as Record<string, unknown>).type === 'heatmap') {
        return 'heatmap';
    }
    // Class probabilities (lib/class-rules.ts): an explicit marker too, so probPath is never read as a mask.
    if (value && typeof value === 'object' && !Array.isArray(value)
        && (value as Record<string, unknown>).type === 'class_probabilities') {
        return 'class_probabilities';
    }

    // First element of an array-of-objects label list (null for non-arrays/empties).
    const candidate = Array.isArray(value) ? value[0] : null;
    const first = candidate && typeof candidate === 'object' && !Array.isArray(candidate)
        ? candidate as Record<string, unknown> : null;
    if (!Array.isArray(value) && typeof value === 'object' &&
        (value as Record<string, unknown>)._type === 'cuboid') return null;

    if (first?._type === 'cuboid') return 'cuboids';

    // Detections: array of objects with bbox
    if (first?.bbox !== null && typeof first?.bbox === 'object') {
        return 'detections';
    }

    // Keypoints: array of objects with the explicit "keypoints" marker. Checked
    // BEFORE polygons because keypoint sets also carry a `points` array.
    if (first?._type === 'keypoints') {
        return 'keypoints';
    }

    // Polygons: array of objects with points or paths (multi-ring with holes)
    if (first && (
        (first.points && Array.isArray(first.points)) ||
        (first.paths && Array.isArray(first.paths))
    )) {
        return 'polygons';
    }

    // Classifications: array of {label, confidence} objects (not plain strings)
    if (first && 'label' in first) {
        return 'classifications';
    }

    // Single classification: {label, confidence} object. Exclude {maskPath} so a
    // mask dict that also carries a label resolves to 'segmentation' — mirroring the
    // server's discovery, which excludes maskPath from classification matching.
    if (value && typeof value === 'object' && !Array.isArray(value) && 'label' in value && !('maskPath' in value)
        && (value as Record<string, unknown>)._type !== 'cuboid') {
        return 'classification';
    }

    // Segmentation mask: {maskPath: string}
    if (value && typeof value === 'object' && 'maskPath' in value) {
        return 'segmentation';
    }

    return null;
}

/**
 * Parse metadata to find all annotation fields
 */
export function parseAnnotations(metadata: Record<string, unknown>): AnnotationField[] {
    return Object.entries(metadata)
        .map(([fieldName, value]) => ({
            fieldName,
            type: detectLabelType(value),
            data: value
        }))
        .filter(field => field.type !== null);
}

type OverlayContext = readonly { name: string; type: string; origin?: string }[] | Record<string, unknown>;
const labelListTypes = new Set(['detections', 'polygons', 'keypoints', 'classifications']);
const legacyLabelFields = new Set(['detections', 'polygons', 'ground_truth', 'predictions']);

/** Resolve only known label-list aliases. A registered dotted key is a real field. */
export function overlayFieldName(field: string, context?: OverlayContext): string {
    const schema = Array.isArray(context) ? context : undefined;
    const metadata = context && !schema ? context as Record<string, unknown> : undefined;
    if (metadata && Object.hasOwn(metadata, field)) return field;
    if (schema?.some(f => f.name === field && f.origin && f.origin !== 'system')) return field;
    if (field === 'segmentation.labels') return 'segmentation';
    if (!field.endsWith('.label')) return field;
    const parent = field.slice(0, -6);
    const type = schema?.find(f => f.name === parent)?.type ?? (metadata ? detectLabelType(metadata[parent]) : null);
    const classificationList = type === 'classification' && schema?.some(f => f.name === field && f.origin === 'system');
    return legacyLabelFields.has(parent) || classificationList || (type && labelListTypes.has(type)) ? parent : field;
}

export function isDerivedLabelField(field: string, context?: OverlayContext): boolean {
    return overlayFieldName(field, context) !== field;
}

export function isOverlayActive(field: string, activeFields?: string[], context?: OverlayContext): boolean {
    return !activeFields || activeFields.some(active => overlayFieldName(active, context) === overlayFieldName(field, context));
}

export function toggleOverlayField(activeFields: string[], field: string, context?: OverlayContext): string[] {
    return isOverlayActive(field, activeFields, context)
        ? activeFields.filter(active => overlayFieldName(active, context) !== overlayFieldName(field, context))
        : [...activeFields, overlayFieldName(field, context)];
}

/** Field colours win; legacy detection schemes remain the default for other box fields. */
export function getOverlayColorConfig(config: SegmentationConfig | undefined, field: string, type?: LabelType): FieldColorConfig | undefined {
    const fields = config?.color_scheme?.fields;
    const fallback = type === 'detections' ? fields?.detections : undefined;
    const own = fields?.[field] ?? fields?.[`${field}.label`];
    const colors = own ? { ...fallback, ...own, valueColors: { ...fallback?.valueColors, ...own.valueColors } } : fallback;
    const opacity = colors?.opacity ?? config?.color_scheme?.opacity;
    return colors || opacity !== undefined ? { colorByAttribute: 'label', ...colors, opacity } : undefined;
}

export function segmentationLabelChoices(schemaLabels: string[], config: SegmentationConfig): string[] {
    return [...new Set([...schemaLabels, ...Object.values(config.mask_targets || {})
        .flatMap(targets => targets && typeof targets === 'object' ? Object.values(targets) : []).filter((label): label is string => typeof label === 'string')])];
}
