'use client';

import { useState } from 'react';
import type { Detection } from '@/lib/labels';
import { FieldColorConfig } from '@/lib/api';
import { normalizeBbox, generateColor, estimateLabelWidth, formatConfidence, getColorByAttribute } from '@/lib/annotation-utils';

interface Props {
    detections: Detection[];
    imageSize: { width: number; height: number };
    colorByAttribute?: string;  // Optional: color boxes by attribute value
    confidenceThreshold?: number;
    colorConfig?: FieldColorConfig;
}

export function DetectionRenderer({
    detections,
    imageSize,
    colorByAttribute,
    confidenceThreshold = 0,
    colorConfig,
    hiddenIndices,
    onSelect,
    selectedIndex
}: Props & { hiddenIndices?: Set<number>; onSelect?: (index: number) => void; selectedIndex?: number }) {
    const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);

    // Get opacity from config or default to 0.4 (same as polygons)
    // For detections, we often want the fill to be subtler than the stroke
    const baseOpacity = colorConfig?.opacity ?? 0.4;
    const fillOpacity = Math.max(0.1, baseOpacity * 0.4); // Fill is 40% of base opacity
    const strokeOpacity = Math.min(1.0, baseOpacity + 0.4); // Stroke is stronger

    return (
        <>
            {detections.map((detection, idx) => {
                // Filter by confidence
                if (detection.confidence !== undefined && detection.confidence < confidenceThreshold) {
                    return null;
                }

                // Filter by hidden state - using original index for stability
                if (hiddenIndices?.has(idx)) {
                    return null;
                }

                const label = detection.label ?? "";

                // Determine box color
                let color: string;
                if (colorByAttribute) {
                    color = getColorByAttribute(detection, colorByAttribute) || generateColor(label);
                } else if (colorConfig?.valueColors?.[label]) {
                    color = colorConfig.valueColors[label];
                } else {
                    color = generateColor(label);
                }

                const bbox = normalizeBbox(detection.bbox, imageSize);
                const labelText = label;
                const confidenceText = detection.confidence !== undefined
                    ? ` ${formatConfidence(detection.confidence)}`
                    : '';
                const labelWidth = estimateLabelWidth(labelText, detection.confidence);

                return (
                    <g key={idx}>
                        {/* Bounding box fill (semi-transparent) */}
                        <rect
                            x={bbox.x}
                            y={bbox.y}
                            width={bbox.width}
                            height={bbox.height}
                            fill={color}
                            fillOpacity={fillOpacity}
                            stroke="none"
                            className="pointer-events-auto cursor-pointer"
                            onClick={(e) => {
                                e.stopPropagation();
                                onSelect?.(idx);
                            }}
                        />

                        {/* Bounding box stroke */}
                        <rect
                            x={bbox.x}
                            y={bbox.y}
                            width={bbox.width}
                            height={bbox.height}
                            fill="none"
                            stroke={idx === selectedIndex ? 'white' : color}
                            strokeWidth={idx === selectedIndex ? 3 : 2}
                            strokeOpacity={idx === selectedIndex ? 1 : strokeOpacity}
                            className="pointer-events-auto cursor-pointer transition-all"
                            style={{
                                strokeWidth: idx === selectedIndex ? 4 : (hoveredIdx === idx ? 3 : 2),
                                stroke: idx === selectedIndex ? 'cyan' : color // Extra bright for selection
                            }}
                            onMouseEnter={() => setHoveredIdx(idx)}
                            onMouseLeave={() => setHoveredIdx(null)}
                            onClick={(e) => {
                                e.stopPropagation();
                                onSelect?.(idx);
                            }}
                        />

                        {/* Label background */}
                        <rect
                            x={bbox.x}
                            y={bbox.y - 22}
                            width={labelWidth}
                            height={20}
                            fill={color}
                            opacity={Math.min(1.0, baseOpacity + 0.5)} // Label always visible
                            rx={3}
                            className="pointer-events-auto cursor-pointer"
                            onClick={(e) => {
                                e.stopPropagation();
                                onSelect?.(idx);
                            }}
                        />

                        {/* Label text */}
                        <text
                            x={bbox.x + 4}
                            y={bbox.y - 8}
                            fill="white"
                            fontSize={12}
                            fontWeight="600"
                            fontFamily="system-ui, -apple-system, sans-serif"
                        >
                            {labelText}{confidenceText}
                        </text>

                        {/* Highlight fill on hover */}
                        {hoveredIdx === idx && (
                            <rect
                                x={bbox.x}
                                y={bbox.y}
                                width={bbox.width}
                                height={bbox.height}
                                fill={color}
                                opacity={0.2}
                                className="pointer-events-none"
                            />
                        )}

                        {/* Attribute tooltip on hover */}
                        {hoveredIdx === idx && detection.attributes && Object.keys(detection.attributes).length > 0 && (
                            <g>
                                <foreignObject
                                    x={bbox.x + bbox.width + 8}
                                    y={bbox.y}
                                    width={200}
                                    height={100}
                                >
                                    <div className="bg-black/90 backdrop-blur-sm rounded-lg p-2 text-xs text-white border border-white/20">
                                        {Object.entries(detection.attributes).slice(0, 5).map(([key, value]) => (
                                            <div key={key} className="flex justify-between gap-2">
                                                <span className="text-zinc-400">{key}:</span>
                                                <span className="font-medium">{String(value)}</span>
                                            </div>
                                        ))}
                                    </div>
                                </foreignObject>
                            </g>
                        )}
                    </g>
                );
            })}
        </>
    );
}
