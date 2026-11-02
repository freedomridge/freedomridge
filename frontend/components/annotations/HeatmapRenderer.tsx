'use client';

import React, { useEffect, useRef, useState } from 'react';
import { COLORMAPS, DEFAULT_COLORMAP, type ColormapName } from '@/lib/colormaps';
import { getSampleMediaUrl } from '@/lib/media-utils';

interface Props {
    /** Freedom Ridge sample ID; used to build the media-proxy URL that fetches the heatmap PNG. */
    sampleId: number;
    versionId?: number;
    /** Top-level metadata key holding the heatmap object. */
    fieldName: string;
    /** Size of the base image as rendered in the DOM (canvas matches this). */
    imageSize: { width: number; height: number };
    /** [min, max] meaningful pixel-value range; values outside clamp to endpoints. */
    range: [number, number];
    /** Colormap name (see lib/colormaps.ts). Falls back to viridis for unknown names. */
    colormap?: string;
    /** Overlay opacity at nonzero pixels, 0-1. Pixel value 0 is always transparent. */
    opacity?: number;
    /** Optional hover callback for showing the value at cursor. */
    onHover?: (value: number | null, x: number, y: number, event?: React.MouseEvent) => void;
}

/**
 * Canvas-based heatmap overlay for the sample modal.
 *
 * The grid view goes through the backend's composite route instead (see
 * /api/samples/{id}/media?overlay=...) because running one Canvas per grid
 * cell blows up memory at 100+ visible cells.
 *
 * Painting convention (shared with backend/compositing.py):
 *   - pixel value 0 is fully transparent (base image shows through)
 *   - values in [rangeMin, rangeMax] map to colormap indices [0, 255]
 *   - values outside the range clamp to the endpoints
 *   - alpha is constant per nonzero pixel
 *
 * Identical math to `backend/compositing.py::composite_heatmap` — the two paths
 * must agree pixel-for-pixel so modal and grid look the same.
 */
export function HeatmapRenderer({
    sampleId,
    versionId,
    fieldName,
    imageSize,
    range,
    colormap,
    opacity = 0.6,
    onHover,
}: Props) {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const [heatmapImage, setHeatmapImage] = useState<HTMLImageElement | null>(null);
    // Raw grayscale pixel data cached for hover lookups (we re-decode once per PNG load).
    const heatmapDataRef = useRef<Uint8ClampedArray | null>(null);

    // Load the heatmap PNG via the media proxy.
    //
    // Cancel flag: if sampleId/fieldName changes while the old image is still
    // downloading, we MUST NOT let its onload fire after the new one — that'd
    // leave us rendering the previous sample's heatmap over the current one.
    // React runs the cleanup before re-running the effect, so flipping the flag
    // there beats any in-flight handler.
    useEffect(() => {
        let cancelled = false;
        // Blank the previous sample's heatmap (see the paint effect's clear-on-empty)
        // until this sample's PNG downloads, so the old overlay doesn't linger over
        // the new image during navigation.
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setHeatmapImage(null);
        const src = getSampleMediaUrl(sampleId, `${fieldName}.map_path`, undefined, undefined, undefined, versionId);
        const img = new Image();
        img.crossOrigin = 'Anonymous';
        img.src = src;
        img.onload = () => {
            if (!cancelled) setHeatmapImage(img);
        };
        img.onerror = (e) => {
            if (!cancelled) console.error('HeatmapRenderer: failed to load', src, e);
        };
        return () => { cancelled = true; };
    }, [sampleId, fieldName, versionId]);

    // Cache decoded grayscale pixel data for hover value lookups.
    useEffect(() => {
        if (!heatmapImage) {
            heatmapDataRef.current = null;
            return;
        }
        const off = document.createElement('canvas');
        off.width = heatmapImage.width;
        off.height = heatmapImage.height;
        const ctx = off.getContext('2d', { willReadFrequently: true });
        if (!ctx) return;
        ctx.drawImage(heatmapImage, 0, 0);
        heatmapDataRef.current = ctx.getImageData(0, 0, heatmapImage.width, heatmapImage.height).data;
    }, [heatmapImage]);

    // Paint the colormap onto the overlay canvas.
    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        if (!ctx) return;
        if (!heatmapImage || imageSize.width === 0 || imageSize.height === 0) {
            // Blank the prior sample's heatmap while the new one loads.
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            return;
        }

        canvas.width = imageSize.width;
        canvas.height = imageSize.height;

        // Decode the heatmap once into an offscreen canvas so we can read its
        // raw pixel values. Canvas gives 8-bit RGBA, matching the 8-bit heatmap
        // PNG format without losing precision.
        const off = document.createElement('canvas');
        off.width = heatmapImage.width;
        off.height = heatmapImage.height;
        const offCtx = off.getContext('2d');
        if (!offCtx) return;
        offCtx.drawImage(heatmapImage, 0, 0);
        const srcData = offCtx.getImageData(0, 0, heatmapImage.width, heatmapImage.height).data;

        const overlay = ctx.createImageData(canvas.width, canvas.height);
        const out = overlay.data;

        // LUT lookup: unknown colormap names silently fall back to viridis.
        // We take the single-channel value from the red byte (same as the backend).
        const lut = COLORMAPS[(colormap as ColormapName) in COLORMAPS ? (colormap as ColormapName) : DEFAULT_COLORMAP];
        // `range` is [start, end] where order matters: start > end inverts the
        // colormap (value-close-to-start → LUT[0], value-close-to-end → LUT[255]).
        // Keeping the signed span is what makes that work; the `|| 1` only fires
        // on exactly-zero (degenerate) spans, not negative ones.
        // `range` comes from raw per-sample JSONB and the heatmap-field toggle is offered even when
        // it's absent/malformed; guard so a range-less heatmap renders (0..255) instead of throwing
        // and white-screening the modal.
        const [rangeStart, rangeEnd] = Array.isArray(range) && range.length === 2 ? range : [0, 255];
        const rangeSpan = rangeEnd - rangeStart || 1;
        const alphaByte = Math.max(0, Math.min(255, Math.round(opacity * 255)));

        // Resize strategy:
        //   - Any axis downsampling → max-pool. Taking MAX over each source
        //     block preserves hot pixels (a 3x3 80%-probability cluster won't
        //     disappear when the canvas is small enough that it maps to a
        //     single output pixel).
        //   - All axes upsampling → nearest. Max-pool would just be nearest
        //     with extra overhead since each source pixel covers ≥1 output.
        //   - Mixed (one down, one up) → max-pool. The max-pool block guard
        //     collapses to a single source pixel on the up-axis, so the code
        //     degenerates to nearest-on-that-axis + max-pool-on-the-other.
        // Mirrors backend/compositing.py's gate so grid and modal stay in sync.
        const srcW = heatmapImage.width;
        const srcH = heatmapImage.height;
        const dstW = canvas.width;
        const dstH = canvas.height;
        const downsampling = srcW >= dstW || srcH >= dstH;

        if (!downsampling) {
            // Upsample / same-size: single-pixel nearest lookup.
            const scaleX = srcW / dstW;
            const scaleY = srcH / dstH;
            for (let y = 0; y < dstH; y++) {
                const sy = Math.floor(y * scaleY);
                for (let x = 0; x < dstW; x++) {
                    const sx = Math.floor(x * scaleX);
                    const v = srcData[(sy * srcW + sx) * 4];
                    if (v === 0) continue;
                    const normalized = Math.max(0, Math.min(1, (v - rangeStart) / rangeSpan));
                    // Math.floor matches backend's `(n * 255).astype(np.uint8)` truncation —
                    // using Math.round diverged by 1 LUT index at .5 boundaries.
                    const idx = Math.max(0, Math.min(255, Math.floor(normalized * 255))) * 3;
                    const outIdx = (y * dstW + x) * 4;
                    out[outIdx] = lut[idx];
                    out[outIdx + 1] = lut[idx + 1];
                    out[outIdx + 2] = lut[idx + 2];
                    out[outIdx + 3] = alphaByte;
                }
            }
        } else {
            // Downsample: pre-compute block edges with integer-drift resistance
            // (same approach as backend/compositing.py::_max_pool_resize).
            const yEdges = new Int32Array(dstH + 1);
            const xEdges = new Int32Array(dstW + 1);
            for (let i = 0; i <= dstH; i++) yEdges[i] = Math.floor((i * srcH) / dstH);
            for (let j = 0; j <= dstW; j++) xEdges[j] = Math.floor((j * srcW) / dstW);

            for (let y = 0; y < dstH; y++) {
                const y0 = yEdges[y];
                let y1 = yEdges[y + 1];
                if (y1 === y0) y1 = y0 + 1; // guard degenerate zero-height blocks
                for (let x = 0; x < dstW; x++) {
                    const x0 = xEdges[x];
                    let x1 = xEdges[x + 1];
                    if (x1 === x0) x1 = x0 + 1;
                    let v = 0;
                    for (let yy = y0; yy < y1; yy++) {
                        const row = yy * srcW;
                        for (let xx = x0; xx < x1; xx++) {
                            const p = srcData[(row + xx) * 4];
                            if (p > v) v = p;
                        }
                    }
                    if (v === 0) continue;
                    const normalized = Math.max(0, Math.min(1, (v - rangeStart) / rangeSpan));
                    const idx = Math.max(0, Math.min(255, Math.floor(normalized * 255))) * 3;
                    const outIdx = (y * dstW + x) * 4;
                    out[outIdx] = lut[idx];
                    out[outIdx + 1] = lut[idx + 1];
                    out[outIdx + 2] = lut[idx + 2];
                    out[outIdx + 3] = alphaByte;
                }
            }
        }

        ctx.putImageData(overlay, 0, 0);
    }, [heatmapImage, imageSize, range, colormap, opacity]);

    // Hover: report the raw heatmap value at the cursor. Useful for verifying
    // "does this pixel represent 50% probability?" without a pixel-inspector.
    const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
        if (!onHover || !heatmapImage || !heatmapDataRef.current) return;
        const rect = e.currentTarget.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const sx = Math.floor((x / rect.width) * heatmapImage.width);
        const sy = Math.floor((y / rect.height) * heatmapImage.height);
        if (sx < 0 || sy < 0 || sx >= heatmapImage.width || sy >= heatmapImage.height) {
            onHover(null, 0, 0);
            return;
        }
        const idx = (sy * heatmapImage.width + sx) * 4;
        const v = heatmapDataRef.current[idx];
        // Report as null when pixel is 0 (no overlay) so the tooltip hides.
        onHover(v === 0 ? null : v, x, y, e);
    };

    const handleMouseLeave = () => {
        if (onHover) onHover(null, 0, 0);
    };

    return (
        <canvas
            ref={canvasRef}
            className="absolute inset-0 w-full h-full pointer-events-auto cursor-crosshair opacity-transition"
            onMouseMove={handleMouseMove}
            onMouseLeave={handleMouseLeave}
            style={{
                objectFit: 'contain',
                objectPosition: 'center',
            }}
        />
    );
}
