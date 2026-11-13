'use client';

import React, { useRef, useEffect, useMemo, useState } from 'react';
import type { FieldColorConfig } from '@/lib/api';
import { maskColorKey, maskColorTable, paintMask } from '@/lib/mask-paint';
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

    // The mask's pixels, decoded once per mask: the paint below and hover lookups both read them.
    const maskDataRef = useRef<Uint8ClampedArray | null>(null);
    const [maskData, setMaskData] = useState<ImageData | null>(null);
    useEffect(() => {
        if (!maskImage) {
            maskDataRef.current = null;
            // eslint-disable-next-line react-hooks/set-state-in-effect -- derived from the loaded bitmap
            setMaskData(null);
            return;
        }
        const offCanvas = document.createElement('canvas');
        offCanvas.width = maskImage.width;
        offCanvas.height = maskImage.height;
        const ctx = offCanvas.getContext('2d', { willReadFrequently: true });
        if (!ctx) return;
        ctx.drawImage(maskImage, 0, 0);
        const imageData = ctx.getImageData(0, 0, maskImage.width, maskImage.height);
        maskDataRef.current = imageData.data;
        setMaskData(imageData);
    }, [maskImage]);

    // Colours by value, not by object: the settings objects are made again on every render of the sample view (each
    // pan and zoom step), and repainting on each of them made moving around a masked image crawl.
    const rgbMode = isRgbMaskTargets(maskTargets);
    const colorKey = maskColorKey(colorConfig, maskTargets);
    const hiddenKey = hiddenIds ? [...hiddenIds].sort((a, b) => a - b).join(',') : '';
    const table = useMemo(
        () => (rgbMode ? null : maskColorTable(colorKey, hiddenKey ? hiddenKey.split(',').map(Number) : [], opacity)),
        [rgbMode, colorKey, hiddenKey, opacity],
    );

    // Paint the mask once per mask and colour setting: at the mask's own resolution offscreen, then stretched onto the
    // image (one drawImage, nearest-neighbour), so a mask of another size or shape covers its image as before. The
    // canvas is the image's natural size, which zooming and panning don't change: the view scales it, never repaints.
    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        if (!maskData || imageSize.width === 0 || imageSize.height === 0) {
            // Nothing to paint (e.g. between samples while the new mask loads) —
            // wipe any prior overlay so it doesn't linger over the new image.
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            return;
        }
        const off = document.createElement('canvas');
        off.width = maskData.width;
        off.height = maskData.height;
        const offCtx = off.getContext('2d');
        if (!offCtx) return;
        const overlay = offCtx.createImageData(maskData.width, maskData.height);
        paintMask(maskData.data, overlay.data, table, opacity);
        offCtx.putImageData(overlay, 0, 0);
        canvas.width = imageSize.width;
        canvas.height = imageSize.height;
        ctx.imageSmoothingEnabled = false;
        ctx.drawImage(off, 0, 0, imageSize.width, imageSize.height);
    }, [maskData, table, opacity, imageSize.width, imageSize.height]);

    // Handle Hover
    const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
        if (!onHover || !maskImage || !maskDataRef.current) return;

        const rect = e.currentTarget.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;

        // The canvas is the image's size, shown with object-fit: contain, so the image occupies a letterboxed
        // sub-rect of the element. Map the cursor through the image's contained rect, then to the mask per axis (the
        // mask is stretched onto the image, so its shape may differ): fitting by the mask's shape read the wrong
        // pixel whenever the two differed.
        const imgW = imageSize.width || maskImage.width;
        const imgH = imageSize.height || maskImage.height;
        const scale = Math.min(rect.width / imgW, rect.height / imgH);
        const offsetX = (rect.width - imgW * scale) / 2;
        const offsetY = (rect.height - imgH * scale) / 2;

        const maskX = Math.floor(((x - offsetX) / (imgW * scale)) * maskImage.width);
        const maskY = Math.floor(((y - offsetY) / (imgH * scale)) * maskImage.height);

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
                objectPosition: 'center',
                imageRendering: 'pixelated'
            }}
        />
    );
}
