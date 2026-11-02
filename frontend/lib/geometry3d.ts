// Mirrored by python/fridge/core/geometry3d.py; both consume cuboid-projection.json.
import type { Calibration, Cuboid } from './labels';

export type Point3 = [number, number, number];
export type Point2 = [number, number];
export type Segment = [Point2, Point2];
export const NEAR_PLANE = 0.1;
export const EDGES = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4],
    [0, 4], [1, 5], [2, 6], [3, 7]];
const FRONT_DIAGONALS = [[0, 2], [1, 3]];

function finiteVector(value: unknown, length: number): value is number[] {
    return Array.isArray(value) && value.length === length && value.every(v => typeof v === 'number' && Number.isFinite(v));
}

export function isCalibration(value: unknown): value is Calibration {
    if (!value || typeof value !== 'object') return false;
    const c = value as Calibration;
    return c.type === 'calibration' && finiteVector(c.P, 12) && finiteVector(c.image_size, 2)
        && c.image_size.every(v => Number.isInteger(v) && v > 0);
}

export function cuboidCorners(box: Cuboid): Point3[] {
    const [l, w, h] = box.size.map(v => v / 2);
    const signs = [[1, -1, -1], [1, -1, 1], [1, 1, 1], [1, 1, -1],
        [-1, -1, -1], [-1, -1, 1], [-1, 1, 1], [-1, 1, -1]];
    const c = Math.cos(box.yaw), s = Math.sin(box.yaw);
    return signs.map(([a, b, d]) => {
        const x = a * l, y = b * (box.frame === 'camera' ? h : w), z = d * (box.frame === 'camera' ? w : h);
        const rotated = box.frame === 'camera' ? [c * x + s * z, y, -s * x + c * z] : [c * x - s * y, s * x + c * y, z];
        return rotated.map((v, i) => v + box.center[i]) as Point3;
    });
}

function multiply(matrix: number[], point: Point3): Point3 {
    return [0, 1, 2].map(r => matrix[4 * r] * point[0] + matrix[4 * r + 1] * point[1]
        + matrix[4 * r + 2] * point[2] + matrix[4 * r + 3]) as Point3;
}

export function cameraPoint(point: Point3, box: Cuboid, calibration: Calibration): Point3 | null {
    if (box.frame === 'camera') return point;
    const transform = calibration.transforms?.['lidar->camera'];
    if (box.frame !== 'lidar' || !finiteVector(transform, 16)
        || transform.slice(12).some((v, i) => v !== (i === 3 ? 1 : 0))) return null;
    return multiply(transform, point);
}

function clippedSegments(points: Point3[], indices: number[][], scale: number): Segment[] {
    const result: Segment[] = [];
    for (const [start, end] of indices) {
        let a = points[start], b = points[end];
        if (a[2] < NEAR_PLANE && b[2] < NEAR_PLANE) continue;
        if (a[2] < NEAR_PLANE || b[2] < NEAR_PLANE) {
            const behind = a[2] < NEAR_PLANE ? a : b;
            const ahead = a[2] < NEAR_PLANE ? b : a;
            const t = (NEAR_PLANE - behind[2]) / (ahead[2] - behind[2]);
            const clipped: Point3 = [behind[0] + t * (ahead[0] - behind[0]),
                behind[1] + t * (ahead[1] - behind[1]), NEAR_PLANE];
            if (a[2] < NEAR_PLANE) a = clipped; else b = clipped;
        }
        result.push([[a[0] / a[2] * scale, a[1] / a[2] * scale], [b[0] / b[2] * scale, b[1] / b[2] * scale]]);
    }
    return result;
}

export function projectCuboid(box: Cuboid, calibration: Calibration, displayedWidth = calibration.image_size[0]) {
    if (!isCalibration(calibration) || box._type !== 'cuboid' || !finiteVector(box.center, 3)
        || !finiteVector(box.size, 3) || box.size.some(v => v <= 0) || !Number.isFinite(box.yaw)
        || !Number.isFinite(displayedWidth) || displayedWidth <= 0) return null;
    const center = cameraPoint(box.center, box, calibration);
    if (!center) return null;
    const points = cuboidCorners(box).map(p => multiply(calibration.P, cameraPoint(p, box, calibration)!));
    if (!points.every(p => p.every(Number.isFinite))) return null;
    // The SVG viewBox uses loaded natural pixels, which can be a resized thumbnail.
    const scale = displayedWidth / calibration.image_size[0];
    const edges = clippedSegments(points, EDGES, scale);
    const heading = clippedSegments(points, FRONT_DIAGONALS, scale);
    const pixels = edges.flat();
    let extent: [number, number, number, number] | null = null;
    if (pixels.length) {
        const x0 = Math.max(0, Math.min(...pixels.map(p => p[0])));
        const y0 = Math.max(0, Math.min(...pixels.map(p => p[1])));
        const x1 = Math.min(displayedWidth, Math.max(...pixels.map(p => p[0])));
        const y1 = Math.min(calibration.image_size[1] * scale, Math.max(...pixels.map(p => p[1])));
        if (x1 > x0 && y1 > y0) extent = [x0, y0, x1 - x0, y1 - y0];
    }
    return { edges, heading, extent, distance: Math.hypot(...center) };
}

/** Monotone-chain hull for the transparent SVG hit target. */
export function convexHull(points: Point2[]): Point2[] {
    const sorted = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (o: Point2, a: Point2, b: Point2) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const half = (pts: Point2[]) => {
        const hull: Point2[] = [];
        for (const p of pts) {
            while (hull.length >= 2 && cross(hull[hull.length - 2], hull[hull.length - 1], p) <= 0) hull.pop();
            hull.push(p);
        }
        return hull.slice(0, -1);
    };
    return [...half(sorted), ...half(sorted.reverse())];
}
