'use client';

import React from 'react';
import { FieldColorConfig } from '@/lib/api';
import { generateColor } from '@/lib/annotation-utils';

export interface Polygon {
    label?: string | null;
    points?: [number, number][];   // Simple polygon (single ring, legacy)
    paths?: [number, number][][];  // Complex polygon (exterior + holes)
    closed?: boolean;
    id?: number;
}

interface PolygonRendererProps {
    polygon: Polygon;
    imageWidth: number;
    imageHeight: number;
    colorConfig?: FieldColorConfig;
    maskTargets?: Record<string, string>;
    onHover?: (label: string | null, x: number, y: number, event?: React.MouseEvent) => void;
    onClick?: (event: React.MouseEvent) => void;
    strokeWidth?: number;
    isSelected?: boolean;
}

function getPolygonColor(label: string, colorConfig?: FieldColorConfig, isSelected?: boolean): { color: string; opacity: number } {
    const defaultOpacity = colorConfig?.opacity ?? 0.4;

    if (isSelected) {
        return { color: 'cyan', opacity: 0.8 };
    }

    if (colorConfig?.valueColors?.[label]) {
        return { color: colorConfig.valueColors[label], opacity: defaultOpacity };
    }

    return { color: generateColor(label), opacity: defaultOpacity };
}

/**
 * Build an SVG path `d` string from one or more rings.
 * The first ring is the exterior; subsequent rings are holes.
 * Using evenodd fill-rule, the holes are automatically cut out.
 */
function buildPathD(
    paths: [number, number][][],
    imageWidth: number,
    imageHeight: number,
): string {
    return paths
        .map((ring) => {
            const segments = ring.map(([x, y], i) => {
                const px = x * imageWidth;
                const py = y * imageHeight;
                return i === 0 ? `M${px},${py}` : `L${px},${py}`;
            });
            segments.push('Z');
            return segments.join(' ');
        })
        .join(' ');
}

export function PolygonRenderer({
    polygon,
    imageWidth,
    imageHeight,
    colorConfig,
    maskTargets,
    onHover,
    onClick,
    strokeWidth = 2,
    isSelected = false
}: PolygonRendererProps) {
    const label = polygon.label ?? '';
    const displayLabel = maskTargets?.[label] || label;
    const { color, opacity } = getPolygonColor(label, colorConfig, isSelected);

    const isArea = polygon.closed ?? label.startsWith('area/');

    // Resolve paths: use `paths` if available, fall back to wrapping `points`
    const paths: [number, number][][] = polygon.paths
        ? polygon.paths
        : polygon.points
            ? [polygon.points]
            : [];

    if (paths.length === 0) return null;

    const handleMouseEnter = (e: React.MouseEvent<SVGElement>) => {
        if (onHover) {
            const rect = (e.target as SVGElement).ownerSVGElement?.getBoundingClientRect();
            if (rect) {
                onHover(displayLabel, e.clientX - rect.left, e.clientY - rect.top, e);
            }
        }
    };

    const handleMouseMove = (e: React.MouseEvent<SVGElement>) => {
        if (onHover) {
            const rect = (e.target as SVGElement).ownerSVGElement?.getBoundingClientRect();
            if (rect) {
                onHover(displayLabel, e.clientX - rect.left, e.clientY - rect.top, e);
            }
        }
    };

    const handleMouseLeave = () => {
        if (onHover) {
            onHover(null, 0, 0);
        }
    };

    const d = buildPathD(paths, imageWidth, imageHeight);

    if (isArea) {
        return (
            <path
                d={d}
                fillRule="evenodd"
                fill={color}
                fillOpacity={opacity}
                stroke={isSelected ? 'white' : color}
                strokeWidth={isSelected ? strokeWidth * 2 : strokeWidth}
                className="pointer-events-auto cursor-pointer hover:fill-opacity-60 transition-opacity"
                data-label={displayLabel}
                onMouseEnter={handleMouseEnter}
                onMouseMove={handleMouseMove}
                onMouseLeave={handleMouseLeave}
                onClick={onClick}
            />
        );
    } else {
        // Each open path is an independent segment; never connect gaps or close it.
        return (
            <g>
                {paths.map((segment, index) => (
                    <polyline
                        key={index}
                        points={segment.map(([x, y]) => `${x * imageWidth},${y * imageHeight}`).join(' ')}
                        fill="none"
                        stroke={isSelected ? 'cyan' : color}
                        strokeWidth={isSelected ? strokeWidth * 3 : strokeWidth * 1.5}
                        strokeLinecap="round"
                        strokeOpacity={Math.min(1, opacity + 0.3)}
                        className="pointer-events-auto cursor-pointer hover:opacity-80 transition-opacity"
                        data-label={displayLabel}
                        onMouseEnter={handleMouseEnter}
                        onMouseMove={handleMouseMove}
                        onMouseLeave={handleMouseLeave}
                        onClick={onClick}
                    />
                ))}
            </g>
        );
    }
}

interface PolygonOverlayProps {
    polygons: Polygon[];
    imageWidth: number;
    imageHeight: number;
    visible?: boolean;
    colorConfig?: FieldColorConfig;
    maskTargets?: Record<string, string>;
    onHover?: (label: string | null, x: number, y: number, event?: React.MouseEvent) => void;
}

export function PolygonOverlay({
    polygons,
    imageWidth,
    imageHeight,
    visible = true,
    colorConfig,
    maskTargets,
    onHover,
    hiddenIndices,
    onSelect,
    selectedIndex
}: PolygonOverlayProps & { hiddenIndices?: Set<number>; onSelect?: (index: number) => void; selectedIndex?: number }) {
    if (!visible || !polygons || polygons.length === 0) {
        return null;
    }

    return (
        <svg
            className="absolute inset-0 pointer-events-none"
            viewBox={`0 0 ${imageWidth} ${imageHeight}`}
            preserveAspectRatio="xMidYMid meet"
            style={{
                width: '100%',
                height: '100%',
                position: 'absolute',
                top: 0,
                left: 0
            }}
        >
            {polygons.map((polygon, index) => {
                if (hiddenIndices?.has(index)) return null;

                return (
                    <PolygonRenderer
                        key={polygon.id || index}
                        polygon={polygon}
                        imageWidth={imageWidth}
                        imageHeight={imageHeight}
                        colorConfig={colorConfig}
                        maskTargets={maskTargets}
                        onHover={onHover}
                        onClick={() => onSelect?.(index)}
                        isSelected={index === selectedIndex}
                    />
                );
            })}
        </svg>
    );
}
