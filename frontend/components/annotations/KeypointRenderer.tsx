'use client';

import { useState } from 'react';
import type { Keypoint } from '@/lib/labels';
import type { FieldColorConfig } from '@/lib/api';
import { generateColor } from '@/lib/annotation-utils';

interface Props {
    keypoints: Keypoint[];
    imageSize: { width: number; height: number };
    colorConfig?: FieldColorConfig;
    hiddenIndices?: Set<number>;
    onSelect?: (index: number) => void;
    selectedIndex?: number;
}

/** Normalize a point to pixel coords. Matches normalizeBbox: values > 2 are
 *  treated as already-pixel; otherwise normalized (0-1). */
function toPixels(p: [number, number], size: { width: number; height: number }): [number, number] {
    const [x, y] = p;
    const isPixel = Math.abs(x) > 2 || Math.abs(y) > 2;
    return isPixel ? [x, y] : [x * size.width, y * size.height];
}

/**
 * Renders keypoint sets as SVG circles (+ label). Returns bare <g> elements,
 * so it must be placed inside the AnnotationOverlay's <svg viewBox=...>.
 */
export function KeypointRenderer({
    keypoints,
    imageSize,
    colorConfig,
    hiddenIndices,
    onSelect,
    selectedIndex,
}: Props) {
    const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);
    const baseOpacity = colorConfig?.opacity ?? 0.9;

    return (
        <>
            {keypoints.map((kp, idx) => {
                if (hiddenIndices?.has(idx)) return null;

                const color = (kp.label && colorConfig?.valueColors?.[kp.label])
                    || generateColor(kp.label || 'keypoint');
                const pts = (kp.points || []).map((p) => toPixels(p, imageSize));
                const selected = idx === selectedIndex;
                const active = selected || hoveredIdx === idx;
                const r = active ? 5 : 4;

                return (
                    <g
                        key={idx}
                        className="pointer-events-auto cursor-pointer"
                        onMouseEnter={() => setHoveredIdx(idx)}
                        onMouseLeave={() => setHoveredIdx(null)}
                        onClick={(e) => {
                            e.stopPropagation();
                            onSelect?.(idx);
                        }}
                    >
                        {pts.map(([x, y], pi) => (
                            <circle
                                key={pi}
                                cx={x}
                                cy={y}
                                r={r}
                                fill={color}
                                fillOpacity={baseOpacity}
                                stroke={selected ? 'cyan' : 'white'}
                                strokeWidth={selected ? 2 : 1}
                            />
                        ))}

                        {kp.label && pts.length > 0 && (
                            <text
                                x={pts[0][0] + 6}
                                y={pts[0][1] - 6}
                                fill={color}
                                fontSize={12}
                                fontWeight="600"
                                stroke="black"
                                strokeWidth={0.5}
                                paintOrder="stroke"
                                fontFamily="system-ui, -apple-system, sans-serif"
                            >
                                {kp.label}
                            </text>
                        )}
                    </g>
                );
            })}
        </>
    );
}
