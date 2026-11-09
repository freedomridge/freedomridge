'use client';

import React, { useRef, useEffect, useState } from 'react';
import type { FieldColorConfig } from '@/lib/api';
import { getSampleMediaUrl } from '@/lib/media-utils';

const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;

/**
 * RGB-mask mode: when mask_targets is keyed by hex color (e.g. semantic_mask) the
 * mask PNG stores real RGB per pixel, so we paint each pixel's own color
 * directly instead of treating the red channel as a category index. Index
 * masks (integer-keyed targets) keep the original red-channel-as-ID path.
 */
function isRgbMaskTargets(targets?: Record<string, string>): boolean {
    if (!targets) return false;
    const keys = Object.keys(targets);
    return keys.length > 0 && keys.every(k => HEX_COLOR_RE.test(k));
}

function rgbToHex(r: number, g: number, b: number): string {
    return '#' + [r, g, b].map(c => c.toString(16).padStart(2, '0')).join('');
}

/** An index mask's class colour, as a class-probability overlay draws its classes too: the field's colour for the
 * class ID, then for its name, then a built-in palette. */
export function classColor(
    id: number,
    colorConfig?: FieldColorConfig,
    maskTargets?: Record<string, string>,
): [number, number, number] {
    const idStr = String(id);
    const label = maskTargets?.[idStr] || idStr;

    // 1. Check ID-based colors first (stable when labels change)
    let hex = colorConfig?.idColors?.[idStr];

    // 2. Fallback to label-based colors (legacy)
    if (!hex) {
        hex = colorConfig?.valueColors?.[label];
    }

    // 3. Fallback to ID-based default color palette
    if (!hex) {
        const palette = [
            '#e6194B', '#3cb44b', '#ffe119', '#4363d8', '#f58231',
            '#911eb4', '#42d4f4', '#f032e6', '#bfef45', '#fabed4',
            '#469990', '#dcbeff', '#9A6324', '#fffac8', '#800000',
            '#aaffc3', '#808000', '#ffd8b1', '#000075', '#a9a9a9'
        ];
        hex = palette[id % palette.length] || '#888888';
    }

    // Hex to RGB
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return [r, g, b];
}

interface Props {
    /** Freedom Ridge sample ID; used to build the media-proxy URL for the mask PNG. */
    sampleId: number;
    versionId?: number;
    /** Metadata field name holding the mask descriptor ({maskPath: "..."}). */
    fieldName: string;
    imageSize: { width: number; height: number };
    colorConfig?: FieldColorConfig;
    maskTargets?: Record<string, string>;
    opacity?: number;
    onHover?: (label: string | null, x: number, y: number, event?: React.MouseEvent) => void;
    hiddenIds?: Set<number>;
}

/**
 * Renders a raster segmentation mask using a canvas overlay.
 *
 * Fetches the mask PNG via the canonical per-dataset media route
 * (`/api/samples/{id}/media?field={fieldName}.maskPath`) so it inherits
 * storage-config routing and access control. The older `/media/{path}` URL
 * bypassed per-dataset config; that code was unreachable in production
 * (nothing wrote `maskPath` to metadata) so the contract change is safe.
 *
 * Pixel values in the mask are treated as category IDs and looked up via
 * maskTargets + colorConfig to choose an RGB color.
 */
export function MaskRenderer({
    sampleId,
    versionId,
    fieldName,
    imageSize,
    colorConfig,
    maskTargets,
    opacity = 0.6,
    onHover,
    hiddenIds
}: Props) {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const [maskImage, setMaskImage] = useState<HTMLImageElement | null>(null);

    // Load the mask image.
    //
    // Cancel flag protects against sampleId/fieldName changing mid-download —
    // otherwise a stale onload could overwrite the current sample's mask with
    // the previous one's.
    useEffect(() => {
        let cancelled = false;
        // Drop the previous sample's bitmap so the canvas blanks (see the draw
        // effect's clear-on-empty) until this sample's mask downloads — otherwise
        // the prior overlay lingers over the new image during navigation.
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setMaskImage(null);
        const src = getSampleMediaUrl(sampleId, `${fieldName}.maskPath`, undefined, undefined, undefined, versionId);
        const img = new Image();
        img.crossOrigin = "Anonymous";
        img.src = src;
        img.onload = () => {
            if (!cancelled) setMaskImage(img);
        };
        img.onerror = (e) => {
            if (!cancelled) console.error("Failed to load mask:", src, e);
        };
        return () => { cancelled = true; };
    }, [sampleId, fieldName, versionId]);

    // Draw the mask on canvas
    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;

        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        if (!ctx) return;
        if (!maskImage || imageSize.width === 0 || imageSize.height === 0) {
            // Nothing to paint (e.g. between samples while the new mask loads) —
            // wipe any prior overlay so it doesn't linger over the new image.
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            return;
        }

        // Match canvas size to display image size
        canvas.width = imageSize.width;
        canvas.height = imageSize.height;

        // Draw offscreen first to access pixel data
        const offCanvas = document.createElement('canvas');
        offCanvas.width = maskImage.width;
        offCanvas.height = maskImage.height;
        const offCtx = offCanvas.getContext('2d');
        if (!offCtx) return;

        offCtx.drawImage(maskImage, 0, 0);
        const imageData = offCtx.getImageData(0, 0, maskImage.width, maskImage.height);
        const data = imageData.data;

        // Create new image data for the overlay
        const overlayData = ctx.createImageData(canvas.width, canvas.height);
        const output = overlayData.data;

        const getColor = (id: number) => classColor(id, colorConfig, maskTargets);

        // Process pixels
        // We need to scale from mask coordinates to canvas coordinates
        const scaleX = canvas.width / maskImage.width;
        const scaleY = canvas.height / maskImage.height;

        // Nearest-neighbor scaling for sharp edges
        ctx.imageSmoothingEnabled = false;

        const rgbMode = isRgbMaskTargets(maskTargets);

        // We'll iterate over the OUTPUT pixels to avoid holes
        for (let y = 0; y < canvas.height; y++) {
            for (let x = 0; x < canvas.width; x++) {
                // Map back to mask coordinate
                const maskX = Math.floor(x / scaleX);
                const maskY = Math.floor(y / scaleY);

                const maskIdx = (maskY * maskImage.width + maskX) * 4;
                const alpha = data[maskIdx + 3];

                let r: number, g: number, b: number;
                if (rgbMode) {
                    // Paint the pixel's own RGB. Background is transparent or pure black.
                    r = data[maskIdx];
                    g = data[maskIdx + 1];
                    b = data[maskIdx + 2];
                    if (alpha === 0 || (r === 0 && g === 0 && b === 0)) continue;
                } else {
                    // Red channel is the category ID for index masks.
                    const id = data[maskIdx];
                    if (alpha === 0 || id === 0) continue;
                    if (hiddenIds?.has(id)) continue;
                    [r, g, b] = getColor(id);
                }

                const outIdx = (y * canvas.width + x) * 4;
                output[outIdx] = r;
                output[outIdx + 1] = g;
                output[outIdx + 2] = b;
                output[outIdx + 3] = opacity * 255;
            }
        }

        ctx.putImageData(overlayData, 0, 0);

    }, [maskImage, imageSize, maskTargets, colorConfig, opacity, hiddenIds]);



    // Cache the mask pixel data for efficient hover lookups
    const maskDataRef = useRef<Uint8ClampedArray | null>(null);
    useEffect(() => {
        if (!maskImage) {
            maskDataRef.current = null;
            return;
        }
        const offCanvas = document.createElement('canvas');
        offCanvas.width = maskImage.width;
        offCanvas.height = maskImage.height;
        const ctx = offCanvas.getContext('2d');
        if (ctx) {
            ctx.drawImage(maskImage, 0, 0);
            const imageData = ctx.getImageData(0, 0, maskImage.width, maskImage.height);
            maskDataRef.current = imageData.data;
        }
    }, [maskImage]);

    // Handle Hover
    const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
        if (!onHover || !maskImage || !maskDataRef.current) return;

        const rect = e.currentTarget.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;

        // The canvas renders with object-fit: contain, so the mask bitmap occupies
        // a letterboxed sub-rect of the element — not the full box. Map the cursor
        // through that contained rect (uniform scale + centered offsets), or hover
        // reads the wrong pixel / is offset from the image.
        const scale = Math.min(rect.width / maskImage.width, rect.height / maskImage.height);
        const offsetX = (rect.width - maskImage.width * scale) / 2;
        const offsetY = (rect.height - maskImage.height * scale) / 2;

        const maskX = Math.floor((x - offsetX) / scale);
        const maskY = Math.floor((y - offsetY) / scale);

        // Check bounds
        if (maskX < 0 || maskX >= maskImage.width || maskY < 0 || maskY >= maskImage.height) {
            onHover(null, 0, 0);
            return;
        }

        const idx = (maskY * maskImage.width + maskX) * 4;
        const data = maskDataRef.current;
        const alpha = data[idx + 3];

        if (isRgbMaskTargets(maskTargets)) {
            // RGB mask: map the pixel's own color to a label.
            const r = data[idx], g = data[idx + 1], b = data[idx + 2];
            if (alpha > 0 && !(r === 0 && g === 0 && b === 0)) {
                const hex = rgbToHex(r, g, b);  // always lowercase
                // mask_targets keys may be any case (#ff0000 / #FF0000 / #Ff0000);
                // match case-insensitively rather than guessing all-lower/all-upper.
                let label: string = hex;
                if (maskTargets) {
                    const k = Object.keys(maskTargets).find(kk => kk.toLowerCase() === hex);
                    if (k) label = maskTargets[k];
                }
                onHover(label, x, y, e);
                return;
            }
            onHover(null, 0, 0);
            return;
        }

        const id = data[idx];
        if (alpha > 0 && id !== 0) {
            const idStr = String(id);
            const label = maskTargets?.[idStr] || idStr;
            onHover(label, x, y, e);
            return;
        }

        onHover(null, 0, 0);
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
                objectPosition: 'center'
            }}
        />
    );
}
