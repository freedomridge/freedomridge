// Utility functions for annotation rendering

import type { BoundingBox, Detection } from './labels';

interface ImageSize {
    width: number;
    height: number;
}

interface NormalizedBbox {
    x: number;
    y: number;
    width: number;
    height: number;
}

/**
 * Normalize bounding box to pixel coordinates.
 * Handles both normalized (0-1) and pixel value formats.
 * Boxes are always [x, y, width, height].
 */
export function normalizeBbox(
    bbox: BoundingBox | number[],
    imageSize: ImageSize
): NormalizedBbox {
    const [a, b, c, d] = Array.isArray(bbox)
        ? bbox
        : [bbox.x, bbox.y, bbox.width, bbox.height];

    // Detect if values are pixel coordinates (>2) or normalized (0-1)
    const isPixelCoords = a > 2 || b > 2 || c > 2 || d > 2;

    let x, y, width, height;

    if (isPixelCoords) {
        // Boxes are always [x, y, width, height]. The old [x1,y1,x2,y2] corner
        // heuristic misread valid pixel boxes whenever width>x and height>y,
        // computing width=c-a/height=d-b and silently shrinking them.
        x = a;
        y = b;
        width = c;
        height = d;
    } else {
        // Normalized coordinates (0-1) - always in [x, y, width, height] format
        x = a * imageSize.width;
        y = b * imageSize.height;
        width = c * imageSize.width;
        height = d * imageSize.height;
    }

    return { x, y, width, height };
}

// Default color palette for segmentation
export const DEFAULT_COLOR_POOL = [
    '#22c55e', '#3b82f6', '#a855f7', '#ef4444', '#eab308',
    '#f97316', '#06b6d4', '#ec4899', '#10b981', '#6366f1',
    '#84cc16', '#14b8a6', '#f43f5e', '#8b5cf6', '#0ea5e9',
];

// Hash function for consistent default color assignment
export function hashString(str: string): number {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        const char = str.charCodeAt(i);
        hash = ((hash << 5) - hash) + char;
        hash = hash & hash;
    }
    return Math.abs(hash);
}

/**
 * Generate consistent color for a label
 * Same label always gets same color
 */
export function generateColor(label: string): string {
    return DEFAULT_COLOR_POOL[hashString(label) % DEFAULT_COLOR_POOL.length];
}

/**
 * Create color map for multiple labels
 */
export function createColorMap(labels: string[]): Map<string, string> {
    const map = new Map<string, string>();
    labels.forEach(label => {
        map.set(label, generateColor(label));
    });
    return map;
}

/**
 * Get color by attribute value
 * For attribute-based coloring
 */
export function getColorByAttribute(
    detection: Detection,
    attributeKey: string,
    colorMap?: Map<string, string>
): string | null {
    if (!detection.attributes || !(attributeKey in detection.attributes)) {
        return null;
    }

    const value = String(detection.attributes[attributeKey]);
    return colorMap?.get(value) || generateColor(value);
}

/**
 * Estimate label text width for backdrop sizing
 */
export function estimateLabelWidth(label: string, confidence?: number): number {
    const baseWidth = label.length * 7; // Approximate char width
    const confidenceWidth = confidence !== undefined ? 30 : 0;
    return baseWidth + confidenceWidth + 8; // + padding
}

/**
 * Format confidence as percentage
 */
export function formatConfidence(confidence: number): string {
    return `${(confidence * 100).toFixed(0)}%`;
}

/**
 * Get unique labels from detections
 */
export function getUniqueLabels(detections: Detection[]): string[] {
    return Array.from(new Set(detections.map(d => d.label)));
}

/**
 * Filter detections by confidence threshold
 */
export function filterByConfidence(
    detections: Detection[],
    threshold: number
): Detection[] {
    return detections.filter(d =>
        d.confidence === undefined || d.confidence >= threshold
    );
}

/**
 * Group detections by label
 */
export function groupByLabel(detections: Detection[]): Map<string, Detection[]> {
    const groups = new Map<string, Detection[]>();

    detections.forEach(detection => {
        const existing = groups.get(detection.label) || [];
        existing.push(detection);
        groups.set(detection.label, existing);
    });

    return groups;
}
