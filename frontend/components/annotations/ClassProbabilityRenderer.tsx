'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';
import type { FieldColorConfig } from '@/lib/api';
import { classColor } from '@/lib/mask-paint';
import { classMap, pixelValues, planeWidth, type ClassRule, type ProbabilityView } from '@/lib/class-rules';
import { COLORMAPS, DEFAULT_COLORMAP } from '@/lib/colormaps';
import { getSampleMediaUrl } from '@/lib/media-utils';

interface Props {
    sampleId: number;
    versionId?: number;
    /** Metadata field holding {type: 'class_probabilities', probPath, classes}. */
    fieldName: string;
    /** The field's path: a new one is loaded again. */
    probPath: string;
    classes: number;
    imageSize: { width: number; height: number };
    /** The class map from a rule, or one class's probability as a heatmap. */
    view: ProbabilityView;
    colorConfig?: FieldColorConfig;
    maskTargets?: Record<string, string>;
    opacity?: number;
    onHover?: (label: string | null, x: number, y: number, event?: React.MouseEvent) => void;
}

interface Decoded {
    data: Uint8ClampedArray;
    width: number;
    height: number;
}

/**
 * Draws a class-probability field (lib/class-rules.ts): the probabilities PNG is fetched through the sample media
 * route, so it inherits storage routing and access control as masks do, decoded once, and redrawn whenever the
 * rule changes. The overlay is scaled to the image without blending classes. Hover names the pixel's class and its
 * most probable classes.
 */
export function ClassProbabilityRenderer({
    sampleId,
    versionId,
    fieldName,
    probPath,
    classes,
    imageSize,
    view,
    colorConfig,
    maskTargets,
    opacity = 0.6,
    onHover,
}: Props) {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const [decoded, setDecoded] = useState<Decoded | null>(null);
    // Keyed by content: a grid tile is handed an equal default view on every render, which mustn't redraw it.
    const ruleKey = JSON.stringify(view.rule);
    const rule = useMemo(() => JSON.parse(ruleKey) as ClassRule, [ruleKey]);
    const show = view.show;
    // The colours too: the overlay hands over an equal colour config on every render (each hover, say).
    const colorKey = JSON.stringify([colorConfig?.idColors, colorConfig?.valueColors, maskTargets]);
    const palette = useMemo(() => {
        const [idColors, valueColors, targets] = JSON.parse(colorKey) as [
            Record<string, string> | undefined, Record<string, string> | undefined, Record<string, string> | undefined,
        ];
        return Array.from({ length: classes }, (_, k) => classColor(k, { idColors, valueColors } as FieldColorConfig, targets));
    }, [colorKey, classes]);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        // eslint-disable-next-line react-hooks/set-state-in-effect -- blank the previous sample's overlay while this one loads
        setDecoded(null);
        setError(null);
        const src = getSampleMediaUrl(sampleId, `${fieldName}.probPath`, undefined, undefined, undefined, versionId);
        const img = new Image();
        img.crossOrigin = 'Anonymous';
        img.src = src;
        img.onload = () => {
            if (cancelled) return;
            try {
                planeWidth(img.width, classes);
                const canvas = document.createElement('canvas');
                canvas.width = img.width;
                canvas.height = img.height;
                const ctx = canvas.getContext('2d', { willReadFrequently: true });
                if (!ctx) throw new Error('This browser gave no canvas to read the probabilities');
                ctx.drawImage(img, 0, 0);
                setDecoded({ data: ctx.getImageData(0, 0, img.width, img.height).data, width: img.width, height: img.height });
            } catch (e) {
                setError(e instanceof Error ? e.message : String(e));
            }
        };
        img.onerror = () => {
            if (!cancelled) setError(`Couldn't load ${fieldName}'s probabilities`);
        };
        return () => {
            cancelled = true;
        };
    }, [sampleId, fieldName, probPath, versionId, classes]);

    useEffect(() => {
        const canvas = canvasRef.current;
        const ctx = canvas?.getContext('2d');
        if (!canvas || !ctx) return;
        if (!decoded || imageSize.width === 0 || imageSize.height === 0) {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            return;
        }
        const w = decoded.width / classes;
        const h = decoded.height;
        const small = new ImageData(w, h);
        const out = small.data;
        if (show === 'map') {
            const map = classMap(decoded.data, decoded.width, h, classes, rule);
            for (let i = 0; i < map.length; i++) {
                const chosen = map[i];
                if (chosen < 0) continue;
                const rgb = palette[chosen] ?? classColor(chosen);
                out[i * 4] = rgb[0];
                out[i * 4 + 1] = rgb[1];
                out[i * 4 + 2] = rgb[2];
                out[i * 4 + 3] = 255;
            }
        } else {
            // One class's probability as a heatmap: the colormap, fading out where the probability is low.
            const lut = COLORMAPS[DEFAULT_COLORMAP];
            const k = show;
            for (let y = 0; y < h; y++) {
                for (let x = 0; x < w; x++) {
                    const value = decoded.data[(y * decoded.width + k * w + x) * 4];
                    const i = (y * w + x) * 4;
                    out[i] = lut[value * 3];
                    out[i + 1] = lut[value * 3 + 1];
                    out[i + 2] = lut[value * 3 + 2];
                    out[i + 3] = value;
                }
            }
        }
        const scratch = document.createElement('canvas');
        scratch.width = w;
        scratch.height = h;
        scratch.getContext('2d')?.putImageData(small, 0, 0);
        canvas.width = imageSize.width;
        canvas.height = imageSize.height;
        ctx.imageSmoothingEnabled = false;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(scratch, 0, 0, canvas.width, canvas.height);
    }, [decoded, imageSize, classes, rule, show, palette]);

    const name = (k: number) => maskTargets?.[String(k)] ?? `class ${k}`;
    const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
        if (!onHover || !decoded) return;
        const rect = e.currentTarget.getBoundingClientRect();
        const w = decoded.width / classes;
        const x = Math.floor(((e.clientX - rect.left) / rect.width) * w);
        const y = Math.floor(((e.clientY - rect.top) / rect.height) * decoded.height);
        if (x < 0 || y < 0 || x >= w || y >= decoded.height) {
            onHover(null, 0, 0);
            return;
        }
        const values = pixelValues(decoded.data, decoded.width, classes, x, y);
        const top = values
            .map((value, k) => ({ k, value }))
            .sort((a, b) => b.value - a.value)
            .slice(0, 3)
            .map(({ k, value }) => `${name(k)} ${(value / 255).toFixed(2)}`)
            .join(' · ');
        onHover(`${fieldName}: ${top}`, e.clientX - rect.left, e.clientY - rect.top, e);
    };

    return (
        <>
            <canvas
                ref={canvasRef}
                data-testid="class-probability-overlay"
                className="absolute inset-0 w-full h-full pointer-events-auto cursor-crosshair"
                style={{ opacity }}
                onMouseMove={handleMouseMove}
                onMouseLeave={() => onHover?.(null, 0, 0)}
            />
            {error && (
                <div
                    data-testid="class-probability-error"
                    className="absolute left-1 top-1 max-w-[90%] px-1.5 py-0.5 rounded bg-red-950/80 text-red-200 text-[10px]"
                >
                    {error}
                </div>
            )}
        </>
    );
}
