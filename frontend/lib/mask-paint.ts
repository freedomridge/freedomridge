// Painting a raster segmentation mask: its pixels' class colours, from a table made once per colour setting, at the
// mask's own resolution. The browser scales the result when the sample view zooms or pans, so moving around never
// repaints it (it used to repaint every display pixel, looking each one's colour up again, on every mouse move).
import type { FieldColorConfig } from './api';

const PALETTE = [
    '#e6194B', '#3cb44b', '#ffe119', '#4363d8', '#f58231',
    '#911eb4', '#42d4f4', '#f032e6', '#bfef45', '#fabed4',
    '#469990', '#dcbeff', '#9A6324', '#fffac8', '#800000',
    '#aaffc3', '#808000', '#ffd8b1', '#000075', '#a9a9a9',
];

/** An index mask's class colour, as a class-probability overlay draws its classes too: the field's colour for the
 * class ID, then for its name, then a built-in palette. */
export function classColor(
    id: number,
    colorConfig?: FieldColorConfig,
    maskTargets?: Record<string, string>,
): [number, number, number] {
    const idStr = String(id);
    const label = maskTargets?.[idStr] || idStr;
    // 1. ID-based colours first (stable when labels change), 2. label-based (legacy), 3. the palette by ID.
    const hex = colorConfig?.idColors?.[idStr] || colorConfig?.valueColors?.[label] || PALETTE[id % PALETTE.length] || '#888888';
    return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
}

/** What an index mask's colours depend on, as a string: equal settings give equal keys, so a settings object made
 * again on every render doesn't repaint the mask. */
export function maskColorKey(colorConfig?: FieldColorConfig, maskTargets?: Record<string, string>): string {
    return JSON.stringify([colorConfig?.idColors ?? null, colorConfig?.valueColors ?? null, maskTargets ?? null]);
}

/** RGBA for each of an index mask's 256 class IDs: 0 (background) and hidden IDs transparent, the rest their class
 * colour at `opacity`. Made from maskColorKey's string, so it is computed once per colour setting. */
export function maskColorTable(colorKey: string, hiddenIds: readonly number[], opacity: number): Uint8ClampedArray {
    const [idColors, valueColors, maskTargets] = JSON.parse(colorKey) as [
        Record<string, string> | null, Record<string, string> | null, Record<string, string> | null,
    ];
    const config = { idColors: idColors ?? undefined, valueColors: valueColors ?? undefined } as FieldColorConfig;
    const hidden = new Set(hiddenIds);
    const table = new Uint8ClampedArray(256 * 4);
    const alpha = Math.round(opacity * 255);
    for (let id = 1; id < 256; id++) {
        if (hidden.has(id)) continue;
        const [r, g, b] = classColor(id, config, maskTargets ?? undefined);
        table.set([r, g, b, alpha], id * 4);
    }
    return table;
}

/**
 * Paints a decoded mask (RGBA, as getImageData gives it) into `out`, pixel for pixel at the mask's size. An index mask
 * reads its red channel as the class ID through `table`; an RGB mask (`table` null) keeps each pixel's colour at
 * `opacity`. A transparent pixel, ID 0 and pure black (an RGB mask's background) stay transparent.
 */
export function paintMask(
    src: Uint8ClampedArray,
    out: Uint8ClampedArray,
    table: Uint8ClampedArray | null,
    opacity: number,
): void {
    const alpha = Math.round(opacity * 255);
    for (let i = 0; i < src.length; i += 4) {
        if (src[i + 3] === 0) continue;
        if (table) {
            const t = src[i] * 4;
            if (table[t + 3] === 0) continue;
            out[i] = table[t];
            out[i + 1] = table[t + 1];
            out[i + 2] = table[t + 2];
            out[i + 3] = table[t + 3];
        } else {
            if (src[i] === 0 && src[i + 1] === 0 && src[i + 2] === 0) continue;
            out[i] = src[i];
            out[i + 1] = src[i + 1];
            out[i + 2] = src[i + 2];
            out[i + 3] = alpha;
        }
    }
}
